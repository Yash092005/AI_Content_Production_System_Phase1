"""Shared test doubles. No network and no API keys anywhere in the suite."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai import budget                                   # noqa: E402
from app.ai.base import ApiError, CapabilityUnavailable      # noqa: E402
from app.ai.health import HEALTH                             # noqa: E402
from app.ai.router import Router                             # noqa: E402
from app.metrics.usage import LOG                            # noqa: E402

GROQ_TPM = 8000
WORDS = " ".join(f"The temple opens early and many devotees visit for darshan every day word{i}." for i in range(120))


def claims(n=16):
    return [{"claim": f"The temple follows documented rule number {i} set by the administration",
             "source_url": f"https://temple.example.org/p/{i}",
             "evidence_excerpt": f"According to the official notice the temple follows rule number {i} during darshan hours"}
            for i in range(n)]


def research_json(n=16):
    return json.dumps({"verified_claims": claims(n), "claims_to_verify": [], "needs_input": [], "research_notes": "Research notes. " * 40})


def blueprint_json(n=16):
    return json.dumps({"sections": ["Introduction", "Timings", "Rules", "FAQ"], "verified_claims": claims(n),
                       "required_information": [], "claims_to_verify": [], "needs_input": [], "research_notes": "notes " * 30})


def api_error(status, body=""):
    return ApiError(status, body or f'{{"error":{{"code":{status},"message":"You exceeded your current quota","status":"RESOURCE_EXHAUSTED"}}}}')


class Stub:
    """A provider double. `script` maps a stage to a list of outcomes consumed in order (a string, an Exception, or a
    callable(prompt, system, opts)); anything not scripted gets a sensible default reply. Every call is recorded."""
    live = True

    def __init__(self, name, model="model-a", chain=None, key_id="k1", search=False, script=None, default=None):
        self.name, self.model, self._chain, self.key_id = name, model, chain or [model], key_id
        self.grounded_research = search
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []
        self.default = default or self._default
        self.retries = 0

    def available(self):
        return True

    def model_for(self, stage=None):
        return self._chain[0]

    def model_chain(self, stage=None):
        return list(self._chain)

    @staticmethod
    def _default(stage, prompt, system, opts):
        if stage in ("research",):
            return research_json()
        if stage in ("research_blueprint", "blueprint"):
            return blueprint_json()
        if stage in ("generate", "revise"):
            return WORDS
        if stage == "qa":
            return "PASS"
        if stage == "extract":
            return "Main :: The temple opens early.\nMain :: Entry is free."
        return "{}"

    def generate(self, prompt, system=None, stage=None, **opts):
        self.calls.append({"stage": stage, "prompt": prompt, "system": system or "", "opts": opts})
        queue = self.script.get(stage)
        out = queue.pop(0) if queue else None
        if out is None:
            out = self.default(stage, prompt, system, opts)
        if callable(out) and not isinstance(out, Exception):
            out = out(prompt, system, opts)
        if isinstance(out, Exception):
            raise out
        return out

    def of(self, stage):
        return [c for c in self.calls if c["stage"] == stage]

    def est(self, call):
        return int((len(call["system"]) + len(call["prompt"])) / 4) + 1


def reset_state():
    from app.ai import pool
    from app.ai.quota import GROUPS
    HEALTH.reset()
    budget.TRACKER.reset()
    LOG.reset()
    GROUPS.reset()
    with pool._LOCK:
        pool._COOLING.clear()


def env_patch(**extra):
    base = {"PIPELINE_CACHE": "false", "CACHE_DIR": tempfile.mkdtemp(), "SOURCE_CHECK": "false",
            "PIPELINE_LEAN": "true", "STRATEGY_PREDICT": "ai", "USAGE_LOG": "off"}
    base.update(extra)
    return mock.patch.dict(os.environ, base)


def make_clients(gemini, groq):
    router = Router(gemini, groq)
    return {k: router for k in ("gemini", "reasoning", "context", "research", "generation", "qa")}, router
