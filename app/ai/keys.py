"""Which API key serves which PURPOSE.

Why: every stage used the same one key, so research, writing and QA all drained one quota. Each purpose can now have its own
key (GEMINI_API_KEY_RESEARCH, GROQ_API_KEY_QA, ...). A purpose without its own key uses the shared key (GEMINI_API_KEY /
GROQ_API_KEY), so nothing changes until dedicated keys are added.

IMPORTANT, from the providers' documentation: Gemini limits are applied per PROJECT and Groq limits per ORGANISATION, not per
key. Several keys from the same Google project or the same Groq organisation share ONE quota and add no capacity.

Keys are secrets: they live only in the environment, are never written to the configuration, and are only ever shown as a
short fingerprint that cannot be turned back into the key."""
import hashlib
import os
import re

PURPOSES = ("context", "research", "generation", "qa")
PURPOSE_LABELS = {
    "context": "Context (strategy and reference website)",
    "research": "Research (facts and blueprint)",
    "generation": "Generation (writer and revisions)",
    "qa": "QA (the audit)",
}
PROVIDERS = ("gemini", "groq")
DEFAULT_VAR = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}
STAGE_PURPOSE = {
    "strategy": "context", "reference": "context",
    "research": "research", "research_blueprint": "research", "blueprint": "research", "extract": "research",
    "generate": "generation", "revise": "generation",
    "qa": "qa",
}
# An optional fifth purpose: the strategy prediction call. With GEMINI_API_KEY_STRATEGY / GROQ_API_KEY_STRATEGY set it uses them
# (plus the shared key as a backup); without them it simply uses the Context client, exactly as before.
OPTIONAL_PURPOSES = ("strategy",)

# Per-stage Gemini model variables that exist in some .env files. They are PRESERVED and shown, but only applied when
# Configuration -> "Per-stage Gemini model variables" is set to apply (the deployed code has always ignored them).
STAGE_MODEL_ENV = {
    "research": ("GEMINI_MODEL_RESEARCH",), "research_blueprint": ("GEMINI_MODEL_RESEARCH",),
    "blueprint": ("GEMINI_MODEL_RESEARCH",), "extract": ("GEMINI_MODEL_RESEARCH",),
    "qa": ("GEMINI_MODEL_QA",),
    "generate": ("GEMINI_MODEL_GENERATION", "GEMINI_MODEL_GENERATE"),
    "revise": ("GEMINI_MODEL_GENERATION", "GEMINI_MODEL_GENERATE"),
    "strategy": ("GEMINI_MODEL_STRATEGY",),
}
QUOTA_WORDS = ("429", "resource_exhausted", "quota", "rate limit", "rate_limit", "too many requests")


def var_name(provider, purpose):
    return f"{DEFAULT_VAR[provider]}_{purpose.upper()}"


def split_keys(raw):
    """'k1, k2;k3' -> ['k1', 'k2', 'k3'] (order kept, blanks and duplicates dropped)."""
    out = []
    for k in re.split(r"[,;\s]+", str(raw or "")):
        k = k.strip().strip("\"'")
        if k and k not in out:
            out.append(k)
    return out


def fingerprint(key):
    """A short id for telling keys apart in the UI. One-way: it cannot be turned back into the key."""
    return hashlib.sha256(str(key).encode()).hexdigest()[:6]


def dedicated_keys(provider, purpose, env=None):
    env = os.environ if env is None else env
    keys = split_keys(env.get(var_name(provider, purpose)))
    if not keys and purpose == "generation":
        keys = split_keys(env.get(f"{DEFAULT_VAR[provider]}_GENERATE"))     # the older spelling, used only when the standard one is empty
    return keys


def default_keys(provider, env=None):
    env = os.environ if env is None else env
    return split_keys(env.get(DEFAULT_VAR[provider]))


def keys_for(provider, purpose, env=None, backup=True):
    """(ordered keys, source) for one provider and purpose.
    source is 'dedicated' (own key, plus the shared key as a backup when `backup`), 'default' (the shared key),
    'borrowed' (no key for this purpose and no shared key, so another purpose's key is used) or 'missing'."""
    env = os.environ if env is None else env
    own, shared = dedicated_keys(provider, purpose, env), default_keys(provider, env)
    if own:
        keys = own + ([k for k in shared if k not in own] if backup else [])
        return keys, "dedicated"
    if shared:
        return shared, "default"
    borrowed = []
    for other in PURPOSES:
        for k in dedicated_keys(provider, other, env):
            if k not in borrowed:
                borrowed.append(k)
    return borrowed, ("borrowed" if borrowed else "missing")


def any_key(provider, env=None):
    """True if ANY key for this provider is set, shared or dedicated."""
    env = os.environ if env is None else env
    return bool(default_keys(provider, env)) or any(dedicated_keys(provider, p, env) for p in PURPOSES)


def describe(env=None, backup=True):
    """Rows for the screen, plus warnings. Never contains a key, only fingerprints."""
    env = os.environ if env is None else env
    rows, owners = [], {}
    for purpose in PURPOSES:
        row = {"purpose": purpose, "label": PURPOSE_LABELS[purpose]}
        for provider in PROVIDERS:
            keys, source = keys_for(provider, purpose, env, backup)
            row[provider] = {"source": source, "ids": [fingerprint(k) for k in keys], "count": len(keys),
                             "dedicated": len(dedicated_keys(provider, purpose, env)) if source == "dedicated" else 0}
            if source == "dedicated":
                for k in dedicated_keys(provider, purpose, env):
                    owners.setdefault((provider, k), []).append(purpose)
        rows.append(row)
    warnings = []
    for (provider, k), purposes in owners.items():
        if len(purposes) > 1:
            warnings.append(f"The same {provider.title()} key (id {fingerprint(k)}) is used for {', '.join(purposes)}, so those purposes share one quota.")
    for provider in PROVIDERS:
        shared = default_keys(provider, env)
        ded = {k for p in PURPOSES for k in dedicated_keys(provider, p, env)}
        if shared and set(shared) & ded:
            warnings.append(f"A dedicated {provider.title()} key is the same as the shared key (id {fingerprint(sorted(set(shared) & ded)[0])}), so it adds no capacity.")
    return rows, warnings


def is_quota_error(e):
    """A limit on THIS key (rate limit or quota). Our own request-size guard is not one: another key would not help."""
    text = f"{getattr(e, 'status', '')} {e}".lower()
    if "configured groq limit" in text:
        return False
    return getattr(e, "status", None) == 429 or any(w in text for w in QUOTA_WORDS)


def stage_model_from_env(stage, env=None):
    """The model named for this stage by GEMINI_MODEL_<STAGE>, or '' (not set, or the setting says to ignore them)."""
    from app.config import runtime
    if str(runtime.get("provider.gemini_stage_models_env", None, "ignore")).strip().lower() != "apply":
        return ""
    env = os.environ if env is None else env
    for name in STAGE_MODEL_ENV.get(stage, ()):
        v = str(env.get(name, "") or "").strip()
        if v:
            return v
    return ""


def stage_model_env_found(env=None):
    """[(variable, model)] for the per-stage model variables present in the environment (for the screen)."""
    env = os.environ if env is None else env
    names = sorted({n for names in STAGE_MODEL_ENV.values() for n in names})
    return [(n, str(env.get(n)).strip()) for n in names if str(env.get(n, "") or "").strip()]
