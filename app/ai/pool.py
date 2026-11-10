"""Try several keys of one provider in order. A key that hits its rate limit or quota rests for a while, so the next
calls go straight to a key that works instead of waiting for the limited one to fail again."""
import threading
import time

from app.ai.base import CapabilityUnavailable
from app.ai.keys import is_quota_error

_COOLING = {}                      # (provider, key id) -> time until which the key is skipped
_LOCK = threading.Lock()


def reset_cooling():
    with _LOCK:
        _COOLING.clear()


def _cooling_until(provider, key_id):
    with _LOCK:
        return _COOLING.get((provider, key_id), 0.0)


class KeyPool:
    live = True

    def __init__(self, provider, clients, cooldown=90.0, clock=time.monotonic):
        self.provider, self.clients, self.cooldown, self.clock = provider, list(clients), float(cooldown), clock
        self.name = self.clients[0].name
        self.events = []

    def available(self):
        return True

    @property
    def grounded_research(self):
        return getattr(self.clients[0], "grounded_research", False)

    @property
    def retries(self):
        return getattr(self.clients[0], "retries", None)

    @retries.setter
    def retries(self, value):
        for c in self.clients:
            c.retries = value

    def model_for(self, stage=None):
        return self.clients[0].model_for(stage) if hasattr(self.clients[0], "model_for") else getattr(self.clients[0], "model", "")

    def model_chain(self, stage=None):
        c = self.clients[0]
        return c.model_chain(stage) if hasattr(c, "model_chain") else [self.model_for(stage)]

    def rest_seconds(self):
        """0 when at least one key is ready; otherwise how long until the first key is ready again."""
        now = self.clock()
        left = [max(0.0, _cooling_until(self.provider, c.key_id) - now) for c in self.clients]
        return 0.0 if any(v == 0 for v in left) else min(left)

    def members(self):
        """The keys in the order generate() would try them: ready keys in their configured order, resting keys last."""
        return self._order()

    def key_rest(self, client):
        """Seconds this key still rests after hitting its limit (0 = ready)."""
        return max(0.0, _cooling_until(self.provider, client.key_id) - self.clock())

    def call_member(self, c, prompt, system=None, stage=None, **opts):
        """Run ONE key of the pool with the same cooldown bookkeeping generate() does (used by the router, which chooses the key)."""
        try:
            return c.generate(prompt, system=system, stage=stage, **opts)
        except CapabilityUnavailable:
            raise
        except Exception as e:
            if is_quota_error(e):
                with _LOCK:
                    _COOLING[(self.provider, c.key_id)] = self.clock() + self.cooldown
                self.events.append(f"{self.provider} key {c.key_id} hit its limit at '{stage}'")
            raise

    def _order(self):
        now = self.clock()
        ready = [c for c in self.clients if _cooling_until(self.provider, c.key_id) <= now]
        resting = sorted((c for c in self.clients if c not in ready), key=lambda c: _cooling_until(self.provider, c.key_id))
        return ready + resting                         # if every key is resting, try the one that rests the shortest first

    def generate(self, prompt, system=None, stage=None, **opts):
        last = None
        for i, c in enumerate(self._order()):
            try:
                return c.generate(prompt, system=system, stage=stage, **opts)
            except CapabilityUnavailable as e:
                last = e                               # capabilities belong to a key/project: another key may offer it
                continue
            except Exception as e:
                if not is_quota_error(e):
                    raise                              # overload, bad request...: another key would not help
                with _LOCK:
                    _COOLING[(self.provider, c.key_id)] = self.clock() + self.cooldown
                msg = f"{self.provider} key {c.key_id} hit its limit at '{stage}'"
                last = e
                if i + 1 < len(self.clients):
                    msg += "; trying the next key"
                    print(f"[keys] {msg}", flush=True)
                self.events.append(msg)
        raise last
