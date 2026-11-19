import os
import time

from app.ai import budget
from app.ai.base import ApiError, OutputTruncated, CapabilityUnavailable, post_json
from app.ai.health import limit_unit, HEALTH, classify, grounding_allowed, grounding_policy
from app.ai.quota import GROUPS
from app.config import runtime
from app.metrics.usage import LOG

_last_call = [0.0]
BASE = "https://generativelanguage.googleapis.com/v1beta/models"
SEARCH_STAGES = ("research", "research_blueprint")
UNGROUNDED_NOTE = "\n[NOTE: web search unavailable; research is ungrounded, treat all facts as UNVERIFIED]"


class GeminiClient:
    """Context-heavy stages: research, generation, revision.

    Google Search grounding is a capability, not an assumption: it is attempted at most once per key and model until the
    memory in app.ai.health expires (a failure is remembered, never repeated). Every call records its real token usage."""
    name = "gemini"
    live = True

    @property
    def grounded_research(self):
        return grounding_policy() != "no"

    def __init__(self, api_key=None):
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY")
        self.model = runtime.get("provider.gemini_model", "GEMINI_MODEL", "gemini-3.5-flash")  # confirm approved model with Atharv
        self.retries = 4
        self.fallback = str(runtime.get("provider.gemini_fallback_model", "GEMINI_FALLBACK_MODEL", "") or "").strip()  # used if the main model is overloaded (503)

    @property
    def key_id(self):
        from app.ai.keys import fingerprint
        return fingerprint(self.api_key) if self.api_key else "none"

    def available(self):
        return bool(self.api_key)

    # ---- models ------------------------------------------------------------------------------------------------
    @property
    def group(self):
        """The quota group this key belongs to (Google project): declared, observed or unverified."""
        return GROUPS.label("gemini", self.key_id)

    def model_for(self, stage=None):
        mapped = runtime.get("provider.gemini_stage_models", None, {}) or {}
        if isinstance(mapped, dict) and mapped.get(stage):
            return str(mapped[stage]).strip()
        from app.ai.keys import stage_model_from_env
        return stage_model_from_env(stage) or self.model

    def model_chain(self, stage=None):
        """Models to try for a stage, in order. Each has its own quota, but ONLY the ones named in Configuration are used:
        nothing is assumed to exist."""
        pool = runtime.get("provider.gemini_model_pool", None, []) or []
        pool = pool if isinstance(pool, (list, tuple)) else []
        out = []
        for m in [self.model_for(stage), self.model, self.fallback, *[str(x).strip() for x in pool]]:
            if m and m not in out:
                out.append(m)
        return out

    # ---- generate ----------------------------------------------------------------------------------------------
    def generate(self, prompt, system=None, stage=None, **opts):
        """opts: model (use exactly this model), max_tokens (output cap), grounded (True: grounded attempt requested,
        False: plain only, None: decide from the capability memory)."""
        if not self.available():
            raise RuntimeError("GEMINI_API_KEY not set")
        explicit_model = opts.get("model")
        planned = bool(opts.get("planned"))          # routed by the Router: it handles a limit by choosing another key, so NO internal retries
        model = explicit_model or self.model_for(stage)
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        cap = opts.get("max_tokens")
        if cap:   # EXACTLY what the planner reserved (its Gemini minimum already includes the hidden thinking tokens)
            payload["generationConfig"] = {"maxOutputTokens": int(min(cap, 65536))}
        headers = {"x-goog-api-key": self.api_key}
        gap = float(runtime.get("provider.gemini_min_interval", "GEMINI_MIN_INTERVAL", 6))  # free tier allows only a few calls/minute
        wait = gap - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()

        searching = stage in SEARCH_STAGES
        ask = opts.get("grounded")
        note = ""
        allowed = searching and ask is not False and grounding_allowed(model, self.key_id, probe=True)
        if searching and ask is True and not allowed:
            # a grounded request was asked for but this key/model is known (or configured) not to offer it: do NOT send it
            raise CapabilityUnavailable("gemini", "Google Search grounding", "(remembered from an earlier failure or switched off)")
        if allowed:
            grounded = {**payload, "tools": [{"google_search": {}}]}  # web grounding
            try:
                text = self._run(grounded, headers, model, stage, prompt, system, cap, search=True, retries=0, explicit=True)
                HEALTH.mark_grounding(model, self.key_id, True)
                return text
            except Exception as e:
                info = classify(e)
                status = getattr(e, "status", None)
                missing = status in (400, 403) or info["kind"] in ("grounding", "unknown_quota")
                if not missing:
                    raise                      # a real rate limit / overload / bad reply: not a capability problem
                HEALTH.mark_grounding(model, self.key_id, False, detail=f"HTTP {status}")
                print(f"[gemini] grounding not usable on {model} ({self.key_id}); it will not be retried for a while", flush=True)
                if ask is True:
                    raise CapabilityUnavailable("gemini", "Google Search grounding", f"(HTTP {status})") from e
                note = UNGROUNDED_NOTE
        elif searching:
            note = UNGROUNDED_NOTE
        text = self._run(payload, headers, model, stage, prompt, system, cap, search=False,
                         retries=0 if planned else self.retries, explicit=bool(explicit_model))
        return text + note

    def _run(self, payload, headers, model, stage, prompt, system, cap, search, retries, explicit):
        est_in = budget.est_input_tokens(system, prompt, "gemini")
        group = self.group
        rid = budget.TRACKER.reserve("gemini", model, est_in + (cap or 0), group=group)
        t0 = time.time()
        row = {"provider": "gemini", "model": model, "stage": stage, "key_id": self.key_id, "group": group, "est_in": est_in,
               "chars_in": len(system or "") + len(prompt or ""), "out_cap": cap, "search": search}
        try:
            data = self._post(payload, headers, model, retries, explicit)
            text = self._text(data)
        except Exception as e:
            info = classify(e)
            LOG.record(**row, ok=False, error=f"{info['kind']}: {e}", duration_s=round(time.time() - t0, 2))
            budget.TRACKER.settle("gemini", model, rid, 0, group=group, failed=True)
            GROUPS.observe_from_error("gemini", self.key_id, e)         # a Google error may name the project
            budget.TRACKER.note_limit_hit("gemini", model, info["kind"], group=self.group, unit=limit_unit(e))
            raise
        u = data.get("usageMetadata") or {}
        prompt_t, cand_t = u.get("promptTokenCount"), u.get("candidatesTokenCount")
        think = u.get("thoughtsTokenCount") or 0
        total = u.get("totalTokenCount") or ((prompt_t or 0) + (cand_t or 0) + think)
        budget.TRACKER.settle("gemini", model, rid, total or est_in, group=group)
        LOG.record(**row, ok=True, prompt_tokens=prompt_t,
                   completion_tokens=((cand_t or 0) + think) if cand_t is not None else None,
                   total_tokens=total or None, cached_tokens=u.get("cachedContentTokenCount"),
                   thinking_tokens=think or None, duration_s=round(time.time() - t0, 2))
        return text

    def _post(self, payload, headers, model, retries, explicit):
        try:
            return self._call_model(model, payload, headers, retries)
        except ApiError as e:
            if not explicit and e.status in (429, 503) and self.fallback and self.fallback != model:
                # quotas are per model, so another Gemini model may still have free requests
                print(f"[gemini] {model} limited/overloaded ({e.status}), trying {self.fallback}", flush=True)
                return self._call_model(self.fallback, payload, headers, retries)
            raise

    def _call_model(self, model, payload, headers, retries):
        try:
            return post_json(f"{BASE}/{model}:generateContent", payload, headers, retries=retries)
        except Exception as e:
            code = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status_code", None)
            if code == 404:
                raise RuntimeError(f"Gemini model '{model}' not found for this key. "
                                   "Run: python scripts/list_models.py and set GEMINI_MODEL in .env") from e
            raise

    @staticmethod
    def _text(data):
        cand = data["candidates"][0]
        text = "".join(p.get("text", "") for p in cand["content"]["parts"])
        if str(cand.get("finishReason", "")).upper() == "MAX_TOKENS":
            raise OutputTruncated("Gemini stopped at the output limit; the reply is incomplete")
        return text
