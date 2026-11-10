"""The configuration schema: defaults, section layout and the field specs the Configuration page is built from.

Defaults reproduce the application's behaviour from before the Configuration tab existed (a test pins this)."""
import copy

from app.config.limit_defaults import GEMINI_LIMITS

CONTENT_TYPES = ["Blog", "Homepage", "CTA Page"]
LENGTH_LEVELS = ["Short", "Standard", "In-depth"]
FRAMEWORKS = ["AIDA", "PAS", "BAB", "BLUF"]
MODES = ["New Content", "Revamp"]
PROVIDER_MODES = ["auto", "live", "gemini_only", "groq_only", "mock"]
STRATEGY_PREDICT = ["ai", "always_ai", "heuristic"]   # ai = heuristic-first (model only when the request is ambiguous)
GROUNDING_CAPABILITY = ["auto", "yes", "no"]
STAGE_MODELS_ENV = ["ignore", "apply"]
RULES_SCOPING = ["all", "groq", "off"]
EFFORTS = ["", "low", "medium", "high"]

# A copy of strategy/length.TARGETS on purpose (importing it here would create an import cycle); a test keeps them equal.
DEFAULT_TARGETS = {
    "Blog":     {"Short": 450, "Standard": 900, "In-depth": 1500},
    "Homepage": {"Short": 350, "Standard": 600, "In-depth": 900},
    "CTA Page": {"Short": 250, "Standard": 450, "In-depth": 700},
}

# QA checks a person may switch off. NOT listed here, so never switchable: the evidence gate (claims outside VERIFIED
# CLAIMS), mandatory removals, times without a verified source, and the AI audit itself.
QA_CHECKS = {
    "layout": "Revamp section layout (headings kept, in order)",
    "framework_stages": "Framework stage markers (AIDA / PAS / BAB / BLUF)",
    "length_gate": "Minimum length (short drafts cannot pass)",
    "service_promotion": "Unverified service promotion",
    "scope_leak": "Brand content leaking into unrelated topics",
    "process_wording": "Process wording in headings (\"based on verified claims\")",
    "flagged_removal": "Automatically delete sentences QA flagged as unsupported",
    "reference_copy": "Reference website: no copied wording (12+ words in a row)",
    "reference_style": "Reference website: warn when the draft drifts from the site's voice",
}

SECTIONS = [
    ("rules", "Content Rules", "📜"),
    ("research", "Research & Evidence", "🔎"),
    ("generation", "Generation Settings", "✍️"),
    ("qa", "QA & Validation", "🛡️"),
    ("system", "System Settings", "⚙️"),
    ("maintenance", "Maintenance", "🧰"),
]

STAGES = ["strategy", "research", "generate", "qa"]


def default_config():
    return copy.deepcopy({
        "rules": {
            "master": None, "canonical": None, "brand": None,      # None = use the file in data/rules
            "mandatory_removals": [],
            "restricted_phrases": [],
            "writing_constraints": "",
        },
        "research": {
            "words_per_claim": 75,
            "source_check": True,
            "gemini_grounding": True,
            "gemini_grounding_capability": "auto",   # auto: not assumed, tried once per key/model and remembered; yes: confirmed; no: never
            "grounding_retry_hours": 24,
            "groq_browser_search": True,
            "require_source_url": False,
            "trusted_facts": [],
        },
        "reference": {
            "enabled": True,
            "max_pages": 3,
            "cache_hours": 24,
            "timeout_seconds": 10,
            "max_chars": 8000,
            "respect_robots": True,
            "allowed_domains": [],
            "blocked_domains": [],
        },
        "generation": {
            "default_length": "Standard",
            "length_targets": DEFAULT_TARGETS,
            "enabled_frameworks": list(FRAMEWORKS),
            "default_framework": "Auto",
            "strategy_predict": "ai",
        },
        "provider": {
            "mode": "auto",
            "gemini_model": "gemini-3.5-flash",
            "gemini_fallback_model": "",
            "gemini_stage_models": {},       # e.g. {"qa": "<model>"}: only models you have confirmed in AI Studio
            "gemini_model_pool": [],         # extra Gemini models tried in order when one is limited (each has its own quota)
            "gemini_stage_models_env": "ignore",   # ignore (as always) | apply: use GEMINI_MODEL_<STAGE> variables found in the environment
            # Which Google project / Groq organisation each key belongs to: [{"provider": "gemini", "key": "<fingerprint>", "group": "project-a"}]
            # (fingerprints are shown in Maintenance). Unlisted keys are 'unverified' and assumed to share one quota.
            "key_groups": [],
            "groq_model": "openai/gpt-oss-120b",
            "groq_research_model": "",
            "groq_reasoning_effort": "low",
            "groq_tpm": 8000,
            "groq_revision_tpm": 6500,
            "gemini_min_interval": 6,
            "key_backup": True,
            "key_cooldown_seconds": 90,
        },
        "budget": {
            "target_headroom": 0.25,         # preferred share of every limit kept free
            "floor_headroom": 0.20,          # below this a request is not sent
            "hard_ceiling": 0.80,            # no request ever uses more than this share of a limit
            "tool_overhead_provisional": 1000,   # tokens reserved for search-tool results until real ones are measured
            "max_wait_seconds": 75,          # longest we wait for the per-minute window instead of routing elsewhere
            "tokens_per_word": 2.0,
            "rules_scoping": "all",          # all | groq | off  (see data/rules/profiles.json)
            "groq_context": 131072, "groq_tpd": 200000, "groq_rpm": 30, "groq_rpd": 1000,
            "gemini_context": 1048576,
            "gemini_min_output_tokens": 8192,    # Gemini counts hidden thinking inside the output limit: this minimum is reserved up front
            "unknown_limits": "conservative",    # a model/project with no entry below is assumed no better than the strictest entry
            # Gemini limits PER PROJECT AND MODEL. group "unverified" = every key whose project is not declared/observed (they are
            # assumed to share one quota); a declared project label gets its own entry; "*" would apply to every project.
            "gemini_limits": copy.deepcopy(GEMINI_LIMITS),
        },
        "qa": {
            "checks": {k: True for k in QA_CHECKS},
            "min_length_ratio": 0.8,
            "max_revisions": 2,
        },
        "system": {
            "pipeline_cache": True,
            "cache_dir": ".cache",
            "lean_mode": False,
            "default_mode": "New Content",
            "stage_instructions": {s: "" for s in STAGES},
        },
    })


def _b(v):
    return str(v).strip().lower() in ("1", "true", "on", "yes")


# env var -> (config path, converter). Used ONLY to seed version 1 from the current .env.
ENV_MAP = {
    "MAX_REVISIONS": ("qa.max_revisions", int),
    "PIPELINE_LEAN": ("system.lean_mode", _b),
    "PIPELINE_CACHE": ("system.pipeline_cache", _b),
    "CACHE_DIR": ("system.cache_dir", str),
    "SOURCE_CHECK": ("research.source_check", _b),
    "GEMINI_GROUNDING": ("research.gemini_grounding", lambda v: str(v).strip().lower() != "off"),
    "GROQ_BROWSER_SEARCH": ("research.groq_browser_search", _b),
    "STRATEGY_PREDICT": ("generation.strategy_predict", lambda v: str(v).strip().lower() if str(v).strip().lower() in ("heuristic", "always_ai") else "ai"),
    "GEMINI_GROUNDING_CAPABILITY": ("research.gemini_grounding_capability", lambda v: str(v).strip().lower() if str(v).strip().lower() in ("auto", "yes", "no") else "auto"),
    "RULES_SCOPING": ("budget.rules_scoping", lambda v: str(v).strip().lower() if str(v).strip().lower() in ("all", "groq", "off") else "all"),
    "GEMINI_MODEL_POOL": ("provider.gemini_model_pool", lambda v: [m.strip() for m in str(v).split(",") if m.strip()]),
    "GEMINI_STAGE_MODELS_ENV": ("provider.gemini_stage_models_env", lambda v: "apply" if str(v).strip().lower() in ("apply", "on", "true", "1", "yes") else "ignore"),
    "MODE": ("provider.mode", lambda v: str(v).strip().lower()),
    "GEMINI_MODEL": ("provider.gemini_model", str),
    "GEMINI_FALLBACK_MODEL": ("provider.gemini_fallback_model", lambda v: str(v).strip()),
    "GROQ_MODEL": ("provider.groq_model", str),
    "GROQ_RESEARCH_MODEL": ("provider.groq_research_model", lambda v: str(v).strip()),
    "GROQ_REASONING_EFFORT": ("provider.groq_reasoning_effort", lambda v: str(v).strip()),
    "GROQ_TPM": ("provider.groq_tpm", int),
    "GROQ_REVISION_TPM": ("provider.groq_revision_tpm", int),
    "GEMINI_MIN_INTERVAL": ("provider.gemini_min_interval", float),
}


def set_path(d, path, value):
    cur = d
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def get_path(d, path, default=None):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def config_from_env(env):
    """Built-in defaults overlaid with whatever the current .env / environment sets (secrets are never read here)."""
    cfg = default_config()
    for name, (path, conv) in ENV_MAP.items():
        raw = env.get(name)
        if raw is None:
            continue
        try:
            set_path(cfg, path, conv(raw))
        except (TypeError, ValueError):
            pass            # a malformed env value falls back to the default instead of breaking start-up
    return cfg


def merge_defaults(cfg):
    """Fill any missing keys from the defaults, so versions saved by an older release still load."""
    base = default_config()

    def merge(b, c):
        for k, v in c.items():
            if isinstance(v, dict) and isinstance(b.get(k), dict):
                merge(b[k], v)
            else:
                b[k] = v
    merge(base, cfg if isinstance(cfg, dict) else {})
    return base


def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        p = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, p + "."))
        else:
            out[p] = v
    return out


def diff(old, new):
    """[(path, old_value, new_value)] for every setting that differs."""
    a, b = flatten(old or {}), flatten(new or {})
    return [(k, a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
