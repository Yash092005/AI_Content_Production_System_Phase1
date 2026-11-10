"""Maintenance helpers (no Streamlit): provider health, cache info/clear, and a live provider test."""
import os
import time
from pathlib import Path

from app.config import schema as S

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_SUFFIXES = (".txt", ".json")


def provider_health(cfg, env=None):
    """Rows describing each provider as the CONFIGURATION would use it. Secrets are only ever reported as set / not set."""
    env = os.environ if env is None else env
    p = cfg.get("provider", {})
    mode = p.get("mode", "auto")
    from app.ai.keys import any_key
    g, r = any_key("gemini", env), any_key("groq", env)
    rows = [
        {"provider": "Gemini", "key": g, "model": p.get("gemini_model"), "fallback_model": p.get("gemini_fallback_model") or "-",
         "used": mode in ("auto", "live", "gemini_only") and g},
        {"provider": "Groq", "key": r, "model": p.get("groq_model"), "fallback_model": p.get("groq_research_model") or "-",
         "used": mode in ("auto", "live", "groq_only") and r},
    ]
    if mode == "mock" or not (g or r):
        summary = "Demo mode: the mock model returns placeholder text."
    else:
        summary = "Live: " + " + ".join(x["provider"] for x in rows if x["used"]) or "No provider is usable with this mode and these keys."
    return rows, summary


def resolve_cache_dir(cache_dir, root=None):
    """The cache folder as an absolute path, only if it stays inside the project (never an arbitrary location)."""
    root = Path(root or PROJECT_ROOT).resolve()
    path = (root / str(cache_dir or ".cache")).resolve()
    if root != path and root not in path.parents:
        raise ValueError("The cache folder must be inside the project.")
    return path


def cache_info(cache_dir, root=None):
    path = resolve_cache_dir(cache_dir, root)
    files = [f for f in path.glob("*") if f.is_file() and f.suffix in CACHE_SUFFIXES] if path.exists() else []
    return {"path": str(path), "files": len(files), "bytes": sum(f.stat().st_size for f in files)}


def clear_cache(cache_dir, root=None):
    """Delete saved model answers (only .txt/.json files directly inside the cache folder). Returns how many."""
    path = resolve_cache_dir(cache_dir, root)
    n = 0
    if path.exists():
        for f in path.glob("*"):
            if f.is_file() and f.suffix in CACHE_SUFFIXES:
                f.unlink()
                n += 1
    return n


def test_providers(settings, clients=None):
    """Send a tiny request to each configured client. Uses a few tokens: only call it on a button press."""
    from app.ai import get_clients
    from app.config import runtime
    out = []
    with runtime.use_settings(settings):
        try:
            clients = clients or get_clients()
        except Exception as e:
            return [{"client": "setup", "ok": False, "detail": str(e)[:200], "seconds": 0.0}]
        done = {}                                   # several purposes share one client: test it once, not once per name
        for name, c in clients.items():
            if id(c) in done:
                out.append({**done[id(c)], "client": name})
                continue
            t0 = time.time()
            try:
                if not getattr(c, "live", False):
                    row = {"client": name, "ok": True, "detail": "Mock client (no API call).", "seconds": 0.0}
                else:
                    reply = c.generate("Reply with the single word OK.", system=None, stage="strategy")
                    row = {"client": name, "ok": True, "detail": (reply or "").strip()[:60] or "(empty reply)",
                           "seconds": round(time.time() - t0, 1)}
            except Exception as e:
                row = {"client": name, "ok": False, "detail": str(e).replace("\n", " ")[:220],
                       "seconds": round(time.time() - t0, 1)}
            done[id(c)] = row
            out.append(row)
    return out


def suggested_restricted(brand_text):
    """Quoted phrases from the brand rules' 'Restricted ...' line(s), offered as a one-click starting list."""
    import re
    found = []
    for line in str(brand_text or "").splitlines():
        if "restricted" in line.lower():
            found += re.findall(r'"([^"]{3,80})"', line)
    seen, out = set(), []
    for p in found:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def key_overview(cfg, env=None):
    """(rows, warnings) describing which key serves which purpose. Only short fingerprints, never a key."""
    from app.ai.keys import describe
    return describe(os.environ if env is None else env, bool(cfg.get("provider", {}).get("key_backup", True)))


def key_text(cell):
    """One cell of the 'API keys by purpose' table in plain words."""
    src, n, ded = cell["source"], cell["count"], cell["dedicated"]
    ids = ", ".join(cell["ids"][:3])
    if src == "dedicated":
        backup = f" + shared backup" if n > ded else ""
        return f"{ded} own key{'s' if ded != 1 else ''}{backup} ({ids})"
    if src == "default":
        return f"shared key ({ids})" if n == 1 else f"{n} shared keys ({ids})"
    if src == "borrowed":
        return f"borrowing another purpose's key ({ids})"
    return "missing"


def test_keys(cfg, env=None, makers=None):
    """Send one tiny request with EACH distinct key (not through the rotation), so a person can see which keys work and
    which are at their limit. Costs a few tokens per key."""
    from app.ai import keys as K
    env = os.environ if env is None else env
    if makers is None:
        from app.ai.gemini_client import GeminiClient
        from app.ai.groq_client import GroqClient
        makers = {"gemini": GeminiClient, "groq": GroqClient}
    mode = cfg.get("provider", {}).get("mode", "auto")
    if mode == "mock":
        return [{"provider": "-", "key_id": "-", "purposes": "-", "ok": True, "detail": "Mock mode: nothing to test.", "seconds": 0.0}]
    wanted = {"gemini_only": ("gemini",), "groq_only": ("groq",)}.get(mode, K.PROVIDERS)
    backup = bool(cfg.get("provider", {}).get("key_backup", True))
    seen = {}
    for provider in wanted:
        for purpose in K.PURPOSES:
            for key in K.keys_for(provider, purpose, env, backup)[0]:
                seen.setdefault((provider, key), []).append(purpose)
    out = []
    for (provider, key), purposes in seen.items():
        t0 = time.time()
        row = {"provider": provider.title(), "key_id": K.fingerprint(key), "purposes": ", ".join(purposes)}
        try:
            reply = makers[provider](api_key=key).generate("Reply with the single word OK.", system=None, stage="strategy")
            out.append({**row, "ok": True, "detail": (reply or "").strip()[:40] or "(empty reply)", "seconds": round(time.time() - t0, 1)})
        except Exception as e:
            limited = K.is_quota_error(e)
            out.append({**row, "ok": False, "detail": ("At its limit right now. " if limited else "") + str(e).replace("\n", " ")[:200],
                        "seconds": round(time.time() - t0, 1)})
    return out or [{"provider": "-", "key_id": "-", "purposes": "-", "ok": False, "detail": "No API keys are set.", "seconds": 0.0}]

