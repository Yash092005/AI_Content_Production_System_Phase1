import os

from app.ai.router import Router
from app.ai.gemini_client import GeminiClient
from app.ai.groq_client import GroqClient
from app.ai.keys import PURPOSES, dedicated_keys, keys_for
from app.ai.mock_client import MockClient
from app.ai.pool import KeyPool
from app.config import runtime
from app.metrics import usage as usage_log


def get_clients(mode=None, env=None):
    """Return one client per PURPOSE (context, research, generation, qa), plus the legacy "gemini" and "reasoning" entries.

    Each purpose uses its own API key when one is set (GEMINI_API_KEY_RESEARCH ...), otherwise the shared key
    (GEMINI_API_KEY / GROQ_API_KEY), so with only the shared keys every purpose behaves exactly as before: purposes that share a
    key share ONE client object, which keeps the pacing between calls.

    Gemini is the primary provider and Groq the fallback. With several keys for one purpose, a key that hits its limit is skipped
    for a while and the next key is used before falling back to the other provider.
    Both providers available: the Router plans every request (tokens, limits, capabilities) and picks the route; Gemini for
    reading/writing/auditing, Groq's browser_search for web research unless Gemini grounding is confirmed. One provider: used
    directly. No keys: mock clients.
    """
    env = os.environ if env is None else env
    usage_log.configure_from_env()
    mode = str(mode or runtime.get("provider.mode", "MODE", "auto")).lower()
    if mode == "mock":
        return _mock()
    backup = runtime.get_bool("provider.key_backup", None, True)
    cooldown = float(runtime.get("provider.key_cooldown_seconds", None, 90))
    makers = {"gemini": GeminiClient, "groq": GroqClient}
    singles, pools, fallbacks = {}, {}, {}

    def single(provider, key):
        if (provider, key) not in singles:
            singles[(provider, key)] = makers[provider](api_key=key)
        return singles[(provider, key)]

    def side(provider, purpose):
        keys, _ = keys_for(provider, purpose, env, backup)
        if not keys:
            return None
        clients = [single(provider, k) for k in keys]
        if len(clients) == 1:
            return clients[0]
        if (provider, tuple(keys)) not in pools:
            pools[(provider, tuple(keys))] = KeyPool(provider, clients, cooldown)
        return pools[(provider, tuple(keys))]

    def for_purpose(purpose):
        g, q = side("gemini", purpose), side("groq", purpose)
        if mode == "gemini_only":
            if g is None:
                raise RuntimeError("MODE=gemini_only but GEMINI_API_KEY is missing")
            return g
        if mode == "groq_only":
            if q is None:
                raise RuntimeError("MODE=groq_only but GROQ_API_KEY is missing")
            return q
        if mode == "live":
            if g is None:
                raise RuntimeError("GEMINI_API_KEY is missing")
            if q is None:
                raise RuntimeError("GROQ_API_KEY is missing")
        if g is not None and q is not None:
            key = (id(g), id(q))
            if key not in fallbacks:
                fallbacks[key] = Router(g, q)            # routed calls carry planned=True, so they use 0 retries; direct calls keep theirs
                for client in (g, q):
                    if isinstance(client, KeyPool):
                        client.events = fallbacks[key].events          # pool messages appear with the fallback events
            return fallbacks[key]
        return g or q                                                   # one provider only, or None when there are no keys

    per = {purpose: for_purpose(purpose) for purpose in PURPOSES}
    if any(c is None for c in per.values()):
        return _mock()
    # Optional strategy purpose: its own keys only when GEMINI_API_KEY_STRATEGY / GROQ_API_KEY_STRATEGY are set.
    own_strategy = any(dedicated_keys(p, "strategy", env) for p in ("gemini", "groq"))
    per["strategy"] = (for_purpose("strategy") or per["context"]) if own_strategy else per["context"]
    return {"gemini": per["generation"], "reasoning": per["generation"], **per}


def _mock():
    mock = MockClient("mock")
    return {"gemini": mock, "reasoning": mock, **{p: mock for p in (*PURPOSES, "strategy")}}
