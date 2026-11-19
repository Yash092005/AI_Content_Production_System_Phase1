import os
import time

from app.ai import budget
from app.ai.base import ApiError, OutputTruncated, post_json
from app.ai.health import limit_unit, classify, parse_wait
from app.ai.quota import GROUPS
from app.config import runtime
from app.metrics.usage import LOG

URL = "https://api.groq.com/openai/v1/chat/completions"
RESEARCH_STAGES = ("research", "research_blueprint")

# Groq decommissioned groq/compound on 2026-09-21.
# GPT-OSS 120B supports Groq's built-in browser_search tool.
BROWSER_SEARCH_MODELS = {
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-safeguard-20b",
}


class GroqClient:
    """Groq (groq.com), not xAI's Grok.

    Research stages use Groq's server-side browser_search tool (a confirmed, zero-cost search capability). Every call
    records real token usage and the provider's own remaining-token counter, and enforces an output cap."""
    name = "groq"
    live = True

    def __init__(self, api_key=None):
        self.api_key = api_key if api_key is not None else os.getenv("GROQ_API_KEY")
        self.model = runtime.get("provider.groq_model", "GROQ_MODEL", "openai/gpt-oss-120b")
        self.research_model = runtime.get("provider.groq_research_model", "GROQ_RESEARCH_MODEL", "") or self.model
        self.browser_search = runtime.get_bool("research.groq_browser_search", "GROQ_BROWSER_SEARCH", True)
        self.retries = 4

    @property
    def grounded_research(self):
        """Whether research calls can use Groq's built-in browser search."""
        return self.browser_search and self.research_model in BROWSER_SEARCH_MODELS

    @property
    def key_id(self):
        from app.ai.keys import fingerprint
        return fingerprint(self.api_key) if self.api_key else "none"

    def available(self):
        return bool(self.api_key)

    @property
    def group(self):
        """The quota group this key belongs to (Groq organisation): declared, observed or unverified."""
        return GROUPS.label("groq", self.key_id)

    def model_for(self, stage=None):
        return self.research_model if stage in RESEARCH_STAGES else self.model

    def _tpm_limit(self, model, stage=None):
        # Direct (un-routed) use only: revisions keep their own conservative budget. The router plans with headroom instead.
        if stage == "revise":
            return int(runtime.get("provider.groq_revision_tpm", "GROQ_REVISION_TPM", 6500))
        return int(runtime.get("provider.groq_tpm", "GROQ_TPM", 8000))

    def _throttle(self, model, est, stage=None):
        """Legacy guard for un-routed use (groq-only mode): refuse what can never fit, wait for the minute window."""
        limit = self._tpm_limit(model, stage)
        if est > limit:
            raise RuntimeError(
                f"Request is about {est} tokens but the configured Groq limit for "
                f"'{model}' is {limit} tokens/minute. Shorten the rule documents/source "
                f"page or increase {'GROQ_REVISION_TPM' if stage == 'revise' else 'GROQ_TPM'} if your account permits it."
            )
        group = self.group
        while budget.TRACKER.tokens_last("groq", model, group=group) + est > limit:
            time.sleep(min(60.0, budget.TRACKER.seconds_until("groq", model, est, limit, group=group) or 5.0))

    def generate(self, prompt, system=None, stage=None, **opts):
        """opts: model, max_tokens (enforced output cap), planned (True when the router already budgeted this call)."""
        if not self.available():
            raise RuntimeError("GROQ_API_KEY not set")
        is_research = stage in RESEARCH_STAGES
        model = opts.get("model") or self.model_for(stage)
        cap = opts.get("max_tokens")
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        est_in = budget.est_input_tokens(system, prompt, "groq")
        est = est_in + (cap or 1500)
        if not opts.get("planned"):
            self._throttle(model, est, stage=stage)
        payload = {"model": model, "messages": messages}
        if cap:
            payload["max_completion_tokens"] = int(cap)
        search = bool(is_research and self.grounded_research and model in BROWSER_SEARCH_MODELS)
        # GPT-OSS models support Groq's server-side browser_search (replaces the retired groq/compound model).
        if search:
            payload["tools"] = [{"type": "browser_search"}]
            payload["tool_choice"] = "required"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        group = self.group
        row = {"provider": "groq", "model": model, "stage": stage, "key_id": self.key_id, "group": group, "est_in": est_in,
               "chars_in": len(system or "") + len(prompt or ""), "out_cap": cap, "search": search}
        rid = budget.TRACKER.reserve("groq", model, est, group=group)
        t0, meta = time.time(), {}
        try:
            data = self._post(payload, headers, model, meta, 0 if opts.get("planned") else self.retries)
            choice = data["choices"][0]
            text = choice["message"].get("content")
            truncated = choice.get("finish_reason") == "length"
        except Exception as e:
            info = classify(e)
            LOG.record(**row, ok=False, error=f"{info['kind']}: {e}", duration_s=round(time.time() - t0, 2))
            budget.TRACKER.settle("groq", model, rid, 0, group=group, failed=True)
            GROUPS.observe_from_error("groq", self.key_id, e)           # a Groq limit error names the organisation
            budget.TRACKER.note_limit_hit("groq", model, info["kind"], group=self.group, unit=limit_unit(e))
            raise
        u = data.get("usage") or {}
        total = u.get("total_tokens") or ((u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0))
        budget.TRACKER.settle("groq", model, rid, total or est, group=group)
        h = meta.get("headers") or {}
        remaining = h.get("x-ratelimit-remaining-tokens")
        try:
            budget.TRACKER.headers("groq", model, int(float(remaining)) if remaining is not None else None,
                                   parse_wait(h.get("x-ratelimit-reset-tokens", ""), bare=True), group=group)
        except ValueError:
            pass
        details = u.get("prompt_tokens_details") or {}
        LOG.record(**row, ok=not truncated, error="truncated: hit the output cap" if truncated else None,
                   prompt_tokens=u.get("prompt_tokens"), completion_tokens=u.get("completion_tokens"),
                   total_tokens=total or None, cached_tokens=details.get("cached_tokens") or u.get("cached_tokens"),
                   remaining_tokens=int(float(remaining)) if remaining not in (None, "") else None,
                   duration_s=round(time.time() - t0, 2))
        if truncated:
            raise OutputTruncated("Groq stopped at the output cap; the reply is incomplete")
        return text

    def _post(self, payload, headers, model, meta, retries):
        """`retries` is 0 for routed (planned) calls: the Router picks another key instead of waiting. Direct calls keep self.retries."""
        effort = str(runtime.get("provider.groq_reasoning_effort", "GROQ_REASONING_EFFORT", "low") or "").strip()  # less hidden thinking = fewer tokens/day
        if effort and "gpt-oss" in model:
            try:
                return post_json(URL, {**payload, "reasoning_effort": effort}, headers, retries=retries, meta=meta)
            except ApiError as e:
                if e.status != 400:
                    raise  # only retry without the setting if Groq rejected the setting itself
        return post_json(URL, payload, headers, retries=retries, meta=meta)
