"""Capability-, quota-group- and budget-aware routing (replaces the fixed Gemini -> Groq fallback).

Every request is planned on each KEY that could serve it (keys in the order the key pool defines: the purpose's own key first,
then the shared backup; a key that hit its limit rests, exactly as before). The plan compares input + output cap + tool overhead
with every limit of that key's QUOTA GROUP (Google project / Groq organisation, see app.ai.quota) and the request goes to the
first key with enough headroom: 25% preferred, 20% as a floor, never above the 80% ceiling. A failure is classified, the key or
(for a verified group) the whole group rests, and the next key is tried. If nothing can take the request safely it raises
ProvidersUnavailable BEFORE a risky call, so the pipeline can pause and resume.

Search stages: Google Search grounding on Gemini is used only when it is confirmed available (or explicitly configured);
otherwise research goes to Groq's browser_search. An unsupported Gemini grounding request is never repeated."""
import time
from dataclasses import dataclass

from app.ai import budget
from app.ai.base import ProvidersUnavailable
from app.ai.fallback import EmptyResponseError
from app.ai.gemini_client import SEARCH_STAGES, UNGROUNDED_NOTE
from app.ai.health import HEALTH, grounding_policy
from app.ai.pool import KeyPool
from app.ai.quota import GROUPS
from app.metrics.usage import LOG


@dataclass
class Candidate:
    provider: str
    client: object       # ONE key's client
    model: str
    mode: str            # 'plain' | 'search' (Groq browser_search) | 'grounded' (Gemini) | 'degraded' (no search, flagged)
    probe: bool = False  # grounded route whose availability is not confirmed yet (tried once, then remembered)
    pool: object = None  # the key pool this key belongs to (None when the provider has a single key)
    key: str = "?"
    group: str = ""

    @property
    def search(self):
        return self.mode in ("search", "grounded")

    @property
    def label(self):
        return f"{self.provider}/{self.model} key {self.key} ({self.mode})"


def _members(client):
    return list(getattr(client, "clients", None) or [client])


class Router:
    name = "router"
    live = True

    def __init__(self, gemini, groq, health=None, tracker=None, usage=None, sleep=time.sleep, groups=None):
        self.gemini, self.groq = gemini, groq
        self.health = health or HEALTH
        self.tracker = tracker or budget.TRACKER
        self.usage = usage or LOG
        self.groups = groups or GROUPS
        self.sleep = sleep
        self.events = []

    # keep the old client surface -------------------------------------------------------------------------------
    @property
    def primary(self):
        return self.gemini

    @property
    def secondary(self):
        return self.groq

    @property
    def retries(self):
        """Routed calls never retry inside a client (every attempt is sent with planned=True, which forces 0): a limited key is
        routed around, not waited for. The clients' own `retries` setting is NOT changed, so direct calls keep theirs."""
        return 0

    def available(self):
        return True

    # ---- candidates --------------------------------------------------------------------------------------------
    def grounding_overall(self, stage="research_blueprint"):
        """'off' | 'available' | 'unknown' | 'unavailable' for Gemini grounding across all keys (never assumed)."""
        if grounding_policy() == "no":
            return "off"
        states = []
        for c in _members(self.gemini):
            chain = c.model_chain(stage) if hasattr(c, "model_chain") else [getattr(c, "model", "")]
            for m in chain[:1]:
                states.append(self.health.grounding_state(m, getattr(c, "key_id", "?")))
        if "available" in states or (grounding_policy() == "yes" and "unavailable" not in states):
            return "available"
        return "unknown" if "unknown" in states else "unavailable"

    def _keys(self, provider):
        side = self.gemini if provider == "gemini" else self.groq
        if isinstance(side, KeyPool):
            return side, list(side.members())
        return None, [side]

    def _expand(self, provider, stage, mode, probe=False, first_model_only=False):
        """One candidate per (model, key). Models are the outer loop: the preferred model is tried on every key before the next
        model (each model has its own quota). Keys keep the pool's order."""
        pool, keys = self._keys(provider)
        chains = []
        for k in keys:
            if provider == "gemini":
                chain = k.model_chain(stage) if hasattr(k, "model_chain") else [getattr(k, "model", "")]
            else:
                chain = [k.model_for(stage) if hasattr(k, "model_for") else getattr(k, "model", "")]
            chains.append(chain[:1] if first_model_only else chain)
        out = []
        for i in range(max((len(c) for c in chains), default=0)):
            for k, chain in zip(keys, chains):
                if i < len(chain):
                    key_id = getattr(k, "key_id", "?")
                    out.append(Candidate(provider, k, chain[i], mode, probe=probe, pool=pool, key=key_id,
                                         group=self.groups.label(provider, key_id)))
        return out

    def candidates(self, stage):
        if stage not in SEARCH_STAGES:
            return self._expand("gemini", stage, "plain") + self._expand("groq", stage, "plain")
        state = self.grounding_overall(stage)
        groq_search = bool(getattr(self.groq, "grounded_research", False))
        out = []
        if state == "available":
            out += self._expand("gemini", stage, "grounded", first_model_only=True)
        if groq_search:
            out += self._expand("groq", stage, "search")
        if state == "unknown":                   # one probe per key and model, remembered either way; never before a confirmed route
            out += self._expand("gemini", stage, "grounded", probe=True, first_model_only=True)
        # degraded routes (no search): last resort, always flagged UNVERIFIED
        out += self._expand("gemini", stage, "degraded", first_model_only=True)
        if not groq_search:
            out += self._expand("groq", stage, "degraded")
        return out

    def _scope(self, cand):
        return self.groups.cooldown_scope(cand.provider, cand.key)

    def _rest(self, cand):
        """Seconds this key (or its verified quota group) still rests (0 = usable)."""
        rest = self.health.cooling(cand.provider, cand.model, self._scope(cand))
        if cand.pool is not None:
            rest = max(rest, cand.pool.key_rest(cand.client))
        return rest

    @staticmethod
    def _sys(cand, system, groq_system):
        """Rules text for this route: Groq may get a narrower excerpt (its limits are tight), Gemini keeps `system`."""
        return groq_system if (cand.provider == "groq" and groq_system) else system

    def _plan(self, cand, stage, system, prompt, words, cap=None, groq_system=None):
        return budget.plan(cand.provider, cand.model, stage, self._sys(cand, system, groq_system), prompt, out_cap=cap,
                           search=cand.search, target_words=words, tracker=self.tracker, usage=self.usage, group=cand.group)

    # ---- preview (used by the pipeline to decide, before any call, whether a prompt fits its preferred route) ------
    def preview(self, stage, system, prompt, target_words=None, groq_system=None):
        pol = budget.policy()
        for cand in self.candidates(stage):
            if cand.mode == "degraded" or self._rest(cand) > 0:
                continue
            p = self._plan(cand, stage, system, prompt, target_words, groq_system=groq_system)
            return {"fits": p.fits(pol.target), "candidate": cand.label, "plan": p}
        return {"fits": False, "candidate": None, "plan": None}

    # ---- generate ----------------------------------------------------------------------------------------------
    def generate(self, prompt, system=None, stage=None, target_words=None, alt_prompts=None, groq_system=None, **ignored):
        pol = budget.policy()
        variants = [prompt] + [a for a in (alt_prompts or []) if a and a != prompt]
        reasons, shortfalls, failed, quota_fails = [], [], set(), []
        # Pass 1: proper routes only (a search stage keeps its search). We wait for a busy per-minute window rather than give
        # up web evidence. Pass 2 adds unconfirmed grounding probes. Pass 3: degraded, flagged routes.
        for allow_probe, allow_degraded in ((False, False), (True, False), (True, True)):
            waited = False
            while True:
                for text in variants:
                    for cand, plan in self._eligible(stage, system, text, target_words, pol, shortfalls, groq_system,
                                                     allow_probe, allow_degraded, failed):
                        if cand.label in failed or self._rest(cand) > 0:
                            continue          # an earlier failure in this very request may have rested this key's whole group
                        out = self._attempt(cand, plan, text, self._sys(cand, system, groq_system), stage, reasons, quota_fails)
                        if out is not None:
                            return out
                        failed.add(cand.label)
                wait = min((p.wait_s for p in shortfalls if p.wait_s), default=0)
                if wait and not waited and not allow_probe and wait <= pol.max_wait:
                    waited = True
                    self.events.append(f"waiting {int(wait)}s for the per-minute window before '{stage}'")
                    self.sleep(wait)
                    shortfalls.clear()
                    continue
                break
        raise ProvidersUnavailable(stage, reasons or self._shortfall_reasons(shortfalls), self._retry_after(stage))

    def _eligible(self, stage, system, text, words, pol, shortfalls, groq_system=None, allow_probe=True,
                  allow_degraded=False, failed=()):
        """Keys in chain order with headroom >= target, then those with headroom >= floor. Nothing below the floor is ever
        returned. Unconfirmed grounding probes and degraded (no-search) routes are included only when allowed."""
        planned = []
        for cand in self.candidates(stage):
            if cand.label in failed or self._rest(cand) > 0:
                continue
            if (cand.probe and not allow_probe) or (cand.mode == "degraded" and not allow_degraded):
                continue
            plan = self._plan(cand, stage, system, text, words, groq_system=groq_system)
            planned.append((cand, plan))
            if not plan.fits(pol.floor) and cand.mode != "degraded":
                shortfalls.append(plan)
        return [(c, p) for c, p in planned if p.fits(pol.target)] + \
               [(c, p) for c, p in planned if not p.fits(pol.target) and p.fits(pol.floor)]

    def _call(self, cand, text, system, stage, opts):
        if cand.pool is not None:
            return cand.pool.call_member(cand.client, text, system=system, stage=stage, **opts)
        return cand.client.generate(text, system=system, stage=stage, **opts)

    def _attempt(self, cand, plan, text, system, stage, reasons, quota_fails):
        opts = {"max_tokens": plan.out_cap, "planned": True, "model": cand.model}
        if cand.provider == "gemini" and stage in SEARCH_STAGES:
            opts["grounded"] = cand.mode == "grounded"
        try:
            out = self._call(cand, text, system, stage, opts)
            if not out or not str(out).strip():
                raise EmptyResponseError(f"{cand.provider} returned an empty reply")
        except Exception as e:
            info = self.health.note_error(cand.provider, cand.model, e, self._scope(cand))
            if info["kind"] == "client":          # bad request / key problem: another provider would not fix it
                raise
            if info["kind"] in ("rpm", "daily") and cand.mode != "grounded":
                quota_fails.append(cand)
            reasons.append(f"{cand.label}: {info['kind']}")
            msg = f"{cand.label} failed at '{stage}' ({info['kind']}); trying the next route"
            print(f"[router] {msg}", flush=True)
            self.events.append(msg)
            return None
        self.health.note_success(cand.provider, cand.model, self._scope(cand))
        for earlier in quota_fails:                # this key worked right after another one was limited: they do not share a quota
            if earlier.provider == cand.provider and earlier.model == cand.model and earlier.key != cand.key:
                self.groups.separate(cand.provider, earlier.key, cand.key)
                self.events.append(f"{cand.provider} keys {earlier.key} and {cand.key} behaved independently: tracked as separate quota groups")
        quota_fails.clear()
        if cand.mode == "degraded" and cand.provider == "groq" and stage in SEARCH_STAGES:
            out += UNGROUNDED_NOTE
        if cand.mode == "degraded":
            self.events.append(f"{stage}: served by {cand.label} WITHOUT web search (flagged UNVERIFIED)")
        return out

    def _shortfall_reasons(self, plans):
        if not plans:
            return ["no provider is available (all resting or not configured)"]
        best = max(plans, key=lambda p: p.min_headroom)
        return [f"the request does not fit any provider with the required headroom (best: {best.describe()})"]

    def _retry_after(self, stage):
        rests = [self._rest(c) for c in self.candidates(stage)]
        rests = [r for r in rests if r > 0]
        return min(rests) if rests else None
