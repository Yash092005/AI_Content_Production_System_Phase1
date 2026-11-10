"""Token / request budget planner.

Before a request is sent we work out what it will cost: input tokens + the output cap we enforce + any tool/search overhead.
We compare that with every limit that applies (context, tokens/minute including what was already used in the last 60 s,
tokens/day, requests/minute, requests/day) FOR THE QUOTA GROUP THE KEY BELONGS TO (Google project / Groq organisation, see
app.ai.quota) and require headroom:

    target headroom  25%  -> preferred
    floor            20%  -> accepted only when nothing better exists
    hard ceiling     80%  -> never exceeded (a request is not sent above this, ever)

Limits are configurable per (provider, model, group). A limit nobody has verified is NOT guessed optimistically: it is assumed to
be no better than the strictest limit that is known for that provider (and then tightened by whatever a limit error teaches us)."""
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from app.ai.quota import UNVERIFIED
from app.config import runtime
from app.config.limit_defaults import GEMINI_LIMITS
from app.metrics.usage import CHARS_PER_TOKEN, LOG

DAY = 86400.0

# Output caps (tokens) used until real output sizes have been measured. generate/revise scale with the word target.
BASE_OUT_CAP = {"strategy": 400, "reference": 1200, "research": 2000, "research_blueprint": 2800, "blueprint": 2000,
                "extract": 1500, "qa": 800}
DEFAULT_WORDS = 900
GEMINI_MODEL_MAX_OUTPUT = 65536


def _f(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default):
    f = _f(v, None)
    return int(f) if f is not None and f > 0 else default


@dataclass
class Policy:
    target: float = 0.25
    floor: float = 0.20
    ceiling: float = 0.80
    tool_provisional: int = 1000
    max_wait: float = 75.0
    tokens_per_word: float = 2.0
    gemini_min_output: int = 8192


def policy():
    p = Policy(target=_f(runtime.get("budget.target_headroom", "BUDGET_TARGET_HEADROOM", 0.25), 0.25),
               floor=_f(runtime.get("budget.floor_headroom", "BUDGET_FLOOR_HEADROOM", 0.20), 0.20),
               ceiling=_f(runtime.get("budget.hard_ceiling", "BUDGET_HARD_CEILING", 0.80), 0.80),
               tool_provisional=_i(runtime.get("budget.tool_overhead_provisional", None, 1000), 1000),
               max_wait=_f(runtime.get("budget.max_wait_seconds", None, 75), 75.0),
               tokens_per_word=_f(runtime.get("budget.tokens_per_word", None, 2.0), 2.0),
               gemini_min_output=min(_i(runtime.get("budget.gemini_min_output_tokens", None, 8192), 8192), GEMINI_MODEL_MAX_OUTPUT))
    p.ceiling = min(max(p.ceiling, 0.5), 0.95)
    p.floor = max(min(p.floor, 0.5), round(1.0 - p.ceiling, 4))   # the ceiling is a floor on headroom too
    p.target = max(p.target, p.floor)
    return p


@dataclass
class Limits:
    context: int = 0
    tpm: int = 0
    tpd: int = 0
    rpm: int = 0
    rpd: int = 0         # 0 = unknown / not applicable
    source: str = ""     # configured | verified | assumed | official


def _norm_model(name):
    return str(name or "").strip().lower().removeprefix("models/")


def _group_name(group):
    return str(group or UNVERIFIED).split(":", 1)[-1]


def gemini_entries():
    return [e for e in (runtime.get("budget.gemini_limits", None, GEMINI_LIMITS) or []) if isinstance(e, dict) and e.get("model")]


def limits_for(provider, model, group=None, tracker=None):
    """Limits for ONE quota group of ONE model. Gemini: the configured entry for (model, group), else (model, '*'), else the
    strictest known entry as an assumption (budget.unknown_limits = 'conservative'), else unknown (learned from errors)."""
    tracker = tracker or TRACKER
    group = group or f"{provider}:{UNVERIFIED}"
    if provider == "groq":
        lim = Limits(context=_i(runtime.get("budget.groq_context", None, 131072), 131072),
                     tpm=_i(runtime.get("provider.groq_tpm", "GROQ_TPM", 8000), 8000),
                     tpd=_i(runtime.get("budget.groq_tpd", None, 200000), 200000),
                     rpm=_i(runtime.get("budget.groq_rpm", None, 30), 30),
                     rpd=_i(runtime.get("budget.groq_rpd", None, 1000), 1000), source="official (free plan)")
    else:
        lim = Limits(context=_i(runtime.get("budget.gemini_context", None, 1048576), 1048576))
        entries, mine, name = gemini_entries(), None, _norm_model(model)
        for want in (_group_name(group), "*"):
            hit = [e for e in entries if _norm_model(e["model"]) == name and str(e.get("group", UNVERIFIED)).strip() == want]
            if hit:
                mine = hit[0]
                break
        if mine:
            lim.rpm, lim.tpm, lim.rpd = _i(mine.get("rpm"), 0), _i(mine.get("tpm"), 0), _i(mine.get("rpd"), 0)
            lim.source = str(mine.get("source") or "configured")
        elif str(runtime.get("budget.unknown_limits", None, "conservative")).lower() == "conservative" and entries:
            def strictest(key):
                vals = [_i(e.get(key), 0) for e in entries if _i(e.get(key), 0)]
                return min(vals) if vals else 0
            lim.rpm, lim.tpm, lim.rpd = strictest("rpm"), strictest("tpm"), strictest("rpd")
            lim.source = "assumed (not verified for this model/project: strictest known limit)"
    learned = tracker.learned(provider, model, group)
    for field_name in ("rpm", "rpd"):
        seen = learned.get(field_name)
        if seen:
            cur = getattr(lim, field_name)
            setattr(lim, field_name, min(cur, seen) if cur else seen)
    return lim


class Tracker:
    """Rolling usage per (provider, quota group, model): tokens and requests in the last minute / day, the provider's own
    rate-limit headers, and ceilings learned from limit errors."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._rows = {}       # (provider, group, model) -> deque of [ts, tokens, id]
        self._seq = 0
        self._hdr = {}
        self._learned = {}

    def reset(self):
        with self._lock:
            self._rows.clear()
            self._hdr.clear()
            self._learned.clear()

    @staticmethod
    def _k(provider, model, group):
        return (provider, group or f"{provider}:{UNVERIFIED}", model)

    def _dq(self, key):
        dq = self._rows.setdefault(key, deque())
        cutoff = self.clock() - DAY
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        return dq

    def reserve(self, provider, model, tokens, group=None):
        """Count a request the moment it is sent (so parallel sessions see it); settle() corrects it to real usage."""
        with self._lock:
            self._seq += 1
            self._dq(self._k(provider, model, group)).append([self.clock(), int(tokens), self._seq])
            return self._seq

    def settle(self, provider, model, rid, tokens, group=None):
        with self._lock:
            for row in self._dq(self._k(provider, model, group)):
                if row[2] == rid:
                    row[1] = int(tokens)
                    return

    def _window(self, key, seconds):
        cutoff = self.clock() - seconds
        rows = [r for r in self._dq(key) if r[0] >= cutoff]
        return sum(r[1] for r in rows), len(rows)

    def tokens_last(self, provider, model, seconds=60.0, group=None):
        with self._lock:
            return self._window(self._k(provider, model, group), seconds)[0]

    def requests_last(self, provider, model, seconds=60.0, group=None):
        with self._lock:
            return self._window(self._k(provider, model, group), seconds)[1]

    def tokens_today(self, provider, model, group=None):
        with self._lock:
            return self._window(self._k(provider, model, group), DAY)[0]

    def requests_today(self, provider, model, group=None):
        with self._lock:
            return self._window(self._k(provider, model, group), DAY)[1]

    def seconds_until(self, provider, model, extra_tokens, allowed_tokens, window=60.0, group=None):
        """Seconds until (tokens used in the window + extra) <= allowed. 0 = now; None = never within one window."""
        with self._lock:
            now = self.clock()
            rows = sorted(r for r in self._dq(self._k(provider, model, group)) if r[0] >= now - window)
            used = sum(r[1] for r in rows)
            if extra_tokens > allowed_tokens:
                return None
            if used + extra_tokens <= allowed_tokens:
                return 0.0
            for r in rows:
                used -= r[1]
                if used + extra_tokens <= allowed_tokens:
                    return max(0.0, r[0] + window - now) + 0.5
            return window

    def seconds_until_requests(self, provider, model, allowed_count, window=60.0, group=None):
        """Seconds until at most `allowed_count` requests remain in the window. 0 = now; None = impossible."""
        with self._lock:
            now = self.clock()
            rows = sorted(r for r in self._dq(self._k(provider, model, group)) if r[0] >= now - window)
            if allowed_count < 0:
                return None
            excess = len(rows) - allowed_count
            if excess <= 0:
                return 0.0
            return max(0.0, rows[excess - 1][0] + window - now) + 0.5

    # ---- live rate-limit headers (Groq sends remaining tokens and the reset time) ---------------------------------
    def headers(self, provider, model, remaining_tokens, reset_seconds, group=None):
        if remaining_tokens is None:
            return
        with self._lock:
            self._hdr[self._k(provider, model, group)] = (self.clock(), int(remaining_tokens), float(reset_seconds or 60.0))

    def used_from_headers(self, provider, model, limit, group=None):
        """Tokens already used in the current window according to the provider's own counter (None when stale)."""
        with self._lock:
            h = self._hdr.get(self._k(provider, model, group))
        if not h or not limit:
            return None
        ts, remaining, reset = h
        if self.clock() - ts > reset:
            return None
        return max(0, limit - remaining)

    # ---- learned ceilings ---------------------------------------------------------------------------------------
    def note_limit_hit(self, provider, model, kind, group=None):
        """A limit error happened: whatever this group had used then is, at most, its ceiling. Remember it."""
        key = self._k(provider, model, group)
        with self._lock:
            if kind == "rpm":
                n, name = self._window(key, 60.0)[1], "rpm"
            elif kind == "daily":
                n, name = self._window(key, DAY)[1], "rpd"
            else:
                return
            if n >= 1:
                cur = self._learned.setdefault(key, {})
                cur[name] = min(cur.get(name, n), n)

    def learned(self, provider, model, group=None):
        with self._lock:
            return dict(self._learned.get(self._k(provider, model, group), {}))


TRACKER = Tracker()


# ---- estimation ----------------------------------------------------------------------------------------------------
def est_input_tokens(system, prompt, provider, usage=None):
    usage = usage or LOG
    cpt = usage.chars_per_token(provider) or CHARS_PER_TOKEN
    return int(math.ceil((len(system or "") + len(prompt or "")) / cpt))


def out_cap_for(stage, target_words=None, provider=None, usage=None):
    """The output cap we both RESERVE and ENFORCE (the exact max-tokens value sent to the provider).

    Gemini counts its hidden thinking tokens inside the output limit, so a Gemini request needs a minimum
    (budget.gemini_min_output_tokens, default 8192). That minimum is part of the reservation, so the number the planner
    reserved is exactly the number the request carries."""
    usage = usage or LOG
    p = policy()
    if stage in ("generate", "revise"):
        words = target_words or DEFAULT_WORDS
        base = int(math.ceil(words * p.tokens_per_word * 1.25)) + 400
    else:
        base = BASE_OUT_CAP.get(stage, 1500)
        if stage in ("research", "research_blueprint", "blueprint") and target_words:
            base = int(base * (0.7 + 0.3 * target_words / DEFAULT_WORDS))     # a longer piece needs more evidence claims
    seen = usage.completion_p90(provider, stage) if provider else None
    if seen:   # measured: 1.5 x the 90th percentile, kept within half..double of the default
        base = int(min(max(seen * 1.5, base * 0.5), base * 2))
    if provider == "gemini":
        base = min(max(base, p.gemini_min_output), GEMINI_MODEL_MAX_OUTPUT)
    return base


def tool_reserve(provider, stage, search, usage=None):
    if not search:
        return 0
    usage = usage or LOG
    seen = usage.tool_overhead_p90(provider, stage)
    return int(seen * 1.2) if seen is not None else policy().tool_provisional


@dataclass
class Plan:
    provider: str
    model: str
    stage: str
    in_tokens: int
    out_cap: int
    tool: int
    total: int
    headroom: dict = field(default_factory=dict)
    min_headroom: float = 1.0
    binding: str = ""
    wait_s: float = 0.0          # >0: only a per-minute window is short and waiting this long would fix it
    group: str = ""
    limits_source: str = ""

    def fits(self, tier):
        return self.min_headroom >= tier

    def describe(self):
        return (f"{self.provider}/{self.model} [{self.group}] {self.stage}: {self.in_tokens}+{self.out_cap}+{self.tool}="
                f"{self.total} tok, headroom {self.min_headroom:.0%} ({self.binding})")


def plan(provider, model, stage, system, prompt, out_cap=None, search=False, target_words=None, tracker=None,
         usage=None, limits=None, group=None):
    tracker, usage = tracker or TRACKER, usage or LOG
    pol = policy()
    group = group or f"{provider}:{UNVERIFIED}"
    lim = limits or limits_for(provider, model, group, tracker)
    n_in = est_input_tokens(system, prompt, provider, usage)
    cap = out_cap if out_cap is not None else out_cap_for(stage, target_words, provider, usage)
    tool = tool_reserve(provider, stage, search, usage)
    total = n_in + cap + tool
    head = {}
    if lim.context:
        head["context"] = 1.0 - total / lim.context
    if lim.tpm:
        used = max(tracker.tokens_last(provider, model, group=group),
                   tracker.used_from_headers(provider, model, lim.tpm, group=group) or 0)
        head["tpm"] = 1.0 - (used + total) / lim.tpm
    if lim.tpd:
        head["tpd"] = 1.0 - (tracker.tokens_today(provider, model, group=group) + total) / lim.tpd
    if lim.rpm:
        head["rpm"] = 1.0 - (tracker.requests_last(provider, model, group=group) + 1) / lim.rpm
    if lim.rpd:
        head["rpd"] = 1.0 - (tracker.requests_today(provider, model, group=group) + 1) / lim.rpd
    binding = min(head, key=head.get) if head else ""
    worst = head[binding] if head else 1.0
    p = Plan(provider, model, stage, n_in, cap, tool, total, head, worst, binding, group=group, limits_source=lim.source)
    others_ok = lambda skip: all(v >= pol.target for k, v in head.items() if k not in skip)
    if binding in ("tpm", "rpm") and others_ok(("tpm", "rpm")):
        waits = []
        if "tpm" in head and head["tpm"] < pol.target:
            allowed = int(lim.tpm * (1 - pol.target)) - total
            w = tracker.seconds_until(provider, model, 0, allowed, group=group) if allowed > 0 else None
            waits.append(w)
        if "rpm" in head and head["rpm"] < pol.target:
            allowed_n = int(math.floor(lim.rpm * (1 - pol.target))) - 1       # requests allowed to remain in the window
            waits.append(tracker.seconds_until_requests(provider, model, allowed_n, group=group))
        if waits and all(w is not None for w in waits):
            wait = max(waits)
            if wait <= pol.max_wait:
                p.wait_s = wait
    return p
