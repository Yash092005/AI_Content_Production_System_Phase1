"""Provider health and capability memory.

* Classifies a failure (per-minute limit, daily limit, unknown quota, overload, request too large, ...), so we know
  whether to wait, switch provider, or stop.
* Keeps a cooldown per (provider, model) so a limited provider is not hit again and again.
* Remembers capabilities per model and key. Google Search grounding is the important one: it is NOT assumed to exist.
  A grounded request is tried at most once per key and model until the memory expires, never repeatedly.

State is in memory (shared by all sessions of the process). It resets when the service restarts, which only means one
more probe, never a loop."""
import re
import threading
import time

from app.ai.base import CapabilityUnavailable, OutputTruncated
from app.config import runtime

_WAIT = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)(?![a-z])")
UNKNOWN_BACKOFF = (60, 300, 900, 3600)      # seconds, escalating for repeated 429s with no explanation
OVERLOAD_BACKOFF = (20, 60, 180)


def parse_wait(text, bare=False):
    """'try again in 2m3.5s' / 'retryDelay: 43s' -> seconds (None if nothing is stated). bare=True: the text is just a
    duration such as '43s'."""
    low = str(text or "").lower()
    m = re.search(r"(?:try again in|retry in|retrydelay\"?:?\s*\"?)\s*([0-9hms.\s]+)", low)
    chunk = low if bare else (m.group(1) if m else "")
    total = 0.0
    for num, unit in _WAIT.findall(chunk):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total or None


def classify(exc):
    """-> {'kind', 'retry_after'}.  kinds: rpm, daily, grounding, unknown_quota, overload, too_large, truncated,
    capability, client, empty."""
    text = f"{exc}".lower()
    status = getattr(exc, "status", None)
    wait = (getattr(exc, "retry_after", None) or parse_wait(getattr(exc, "retry_delay", ""), bare=True)
            or parse_wait(text))
    if isinstance(exc, CapabilityUnavailable):
        return {"kind": "capability", "retry_after": None}
    if isinstance(exc, OutputTruncated):
        return {"kind": "truncated", "retry_after": None}
    if "configured groq limit" in text or status == 413 or "request too large" in text:
        return {"kind": "too_large", "retry_after": None}
    if type(exc).__name__ == "EmptyResponseError":
        return {"kind": "empty", "retry_after": None}
    if status == 429:
        metric = f"{getattr(exc, 'quota_metric', '')} {getattr(exc, 'quota_id', '')}".lower()
        if "search" in metric or "ground" in metric:
            return {"kind": "grounding", "retry_after": wait}
        if any(k in text or k in metric for k in ("perday", "per day", "tokens per day", "(tpd)", "requests per day", "(rpd)")):
            return {"kind": "daily", "retry_after": wait}
        if any(k in text or k in metric for k in ("perminute", "per minute", "(tpm)", "(rpm)", "tokens per minute")) or wait:
            return {"kind": "rpm", "retry_after": wait}
        return {"kind": "unknown_quota", "retry_after": None}
    if status in (500, 502, 503, 504):
        return {"kind": "overload", "retry_after": wait}
    if status in (400, 401, 403, 404, 422):
        return {"kind": "client", "retry_after": None}
    if status is None:                       # network error / timeout
        return {"kind": "overload", "retry_after": None}
    return {"kind": "client", "retry_after": None}


class Health:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._cool = {}      # (provider, model) -> {until, kind, strikes, reason}
        self._ground = {}    # (model, key_id) -> {state, until, detail}
        self.events = []

    # ---- cooldowns ---------------------------------------------------------------------------------------------
    def note_error(self, provider, model, exc, scope="*"):
        """`scope` says what rests: a verified quota group (every key in it) or a single unverified key (see app.ai.quota)."""
        info = classify(exc)
        kind, wait = info["kind"], info["retry_after"]
        key = (provider, model, scope)
        with self._lock:
            prev = self._cool.get(key, {})
            strikes = prev.get("strikes", 0) + 1
            seconds = 0
            if kind == "rpm":
                seconds = max(wait or 65, 30)
            elif kind == "daily":
                seconds = max(wait or 3600, 900)
            elif kind == "unknown_quota":
                seconds = UNKNOWN_BACKOFF[min(strikes - 1, len(UNKNOWN_BACKOFF) - 1)]
            elif kind in ("overload", "empty"):
                seconds = OVERLOAD_BACKOFF[min(strikes - 1, len(OVERLOAD_BACKOFF) - 1)]
            if seconds:
                self._cool[key] = {"until": self.clock() + seconds, "kind": kind, "strikes": strikes,
                                   "reason": str(exc)[:120]}
                self.events.append(f"{provider}/{model} [{scope}]: {kind}, resting {int(seconds)}s")
                del self.events[:-50]
        info["cooldown_s"] = seconds
        return info

    def note_success(self, provider, model, scope="*"):
        with self._lock:
            self._cool.pop((provider, model, scope), None)

    def cooling(self, provider, model, scope="*"):
        """Seconds this provider/model/scope should still rest (0 = available)."""
        with self._lock:
            c = self._cool.get((provider, model, scope))
            return max(0.0, c["until"] - self.clock()) if c else 0.0

    # ---- capabilities ------------------------------------------------------------------------------------------
    def grounding_state(self, model, key_id):
        with self._lock:
            g = self._ground.get((model, key_id))
            if not g or g["until"] < self.clock():
                return "unknown"
            return g["state"]

    def mark_grounding(self, model, key_id, available, ttl_hours=None, detail=""):
        hours = ttl_hours if ttl_hours is not None else _num(runtime.get("research.grounding_retry_hours",
                                                                          "GEMINI_GROUNDING_RETRY_HOURS", 24), 24)
        with self._lock:
            self._ground[(model, key_id)] = {"state": "available" if available else "unavailable",
                                             "until": self.clock() + (hours * 3600 if not available else 7 * 86400),
                                             "detail": detail[:120]}
            self.events.append(f"gemini/{model}: grounding {'available' if available else 'unavailable'}")
            del self.events[:-50]

    def reset(self):
        with self._lock:
            self._cool.clear()
            self._ground.clear()
            self.events.clear()

    def snapshot(self):
        now = self.clock()
        with self._lock:
            cool = [{"provider": p, "model": m, "scope": sc, "kind": c["kind"], "rests_s": int(max(0, c["until"] - now)),
                     "strikes": c["strikes"]} for (p, m, sc), c in self._cool.items() if c["until"] > now]
            ground = [{"model": m, "key": k, "state": g["state"], "hours_left": round(max(0, g["until"] - now) / 3600, 1)}
                      for (m, k), g in self._ground.items() if g["until"] > now]
        return {"cooldowns": cool, "grounding": ground, "events": list(self.events)[-10:]}


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def grounding_policy():
    """'auto' (default: not assumed; tried once per key/model, remembered), 'yes' (confirmed available: allowed, but a
    failure is still remembered and not repeated), 'no' (never)."""
    legacy_on = runtime.get_bool("research.gemini_grounding", "GEMINI_GROUNDING", True)
    if not legacy_on:
        return "no"
    raw = str(runtime.get("research.gemini_grounding_capability", "GEMINI_GROUNDING_CAPABILITY", "auto")).strip().lower()
    return raw if raw in ("auto", "yes", "no") else "auto"


def grounding_allowed(model, key_id, probe=False, health=None):
    """May we send a Gemini request WITH Google Search grounding right now?"""
    health = health or HEALTH
    policy = grounding_policy()
    if policy == "no":
        return False
    state = health.grounding_state(model, key_id)
    if state == "unavailable":
        return False                 # never repeat a request we know is unsupported
    if state == "available":
        return True
    return policy == "yes" or bool(probe)


HEALTH = Health()
