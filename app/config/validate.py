"""Validate a configuration BEFORE it can be applied. Returns (errors, warnings).
Errors block Apply. Warnings are shown but allowed."""
import os
import re
import ntpath
import posixpath

from app.config import schema as S

RULE_KEYS = ("master", "canonical", "brand")
MAX_RULE_CHARS = 30000
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
MAX_LIST_ITEMS = 100
MAX_ITEM_CHARS = 300
TRUSTED_FACTS_MAX = 25            # the pipeline caps requester facts at 25 (token budget)
OUTPUT_RESERVE = 1500             # tokens the Groq client reserves for the reply


class ConfigError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def effective_rules(cfg, rule_dir=None):
    """{key: text} the pipeline would use: the data/rules file, unless the configuration overrides it."""
    from app.rules.loader import load_rules
    return load_rules(rule_dir, overrides=cfg.get("rules", {}))


def rules_tokens(cfg, rule_dir=None):
    rules = effective_rules(cfg, rule_dir)
    return int(sum(len(v) for v in rules.values()) / 4)


def _num(cfg, path, lo, hi, errors, label, integer=True):
    v = S.get_path(cfg, path)
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and (not integer or float(v).is_integer())
    if not ok or v < lo or v > hi:
        errors.append(f"{label} must be a {'whole ' if integer else ''}number between {lo} and {hi}.")
        return None
    return v


def _str_list(cfg, path, label, errors, limit=MAX_LIST_ITEMS):
    v = S.get_path(cfg, path)
    if not isinstance(v, list):
        errors.append(f"{label} must be a list.")
        return
    if len(v) > limit:
        errors.append(f"{label} has {len(v)} items; the maximum is {limit}.")
    for i, x in enumerate(v, 1):
        if not isinstance(x, str) or not x.strip():
            errors.append(f"{label}: item {i} is empty.")
        elif len(x) > MAX_ITEM_CHARS:
            errors.append(f"{label}: item {i} is longer than {MAX_ITEM_CHARS} characters.")


def validate(cfg, env=None, rule_dir=None):
    env = os.environ if env is None else env
    errors, warnings = [], []
    g = lambda p, d=None: S.get_path(cfg, p, d)

    # ---- rules ----
    for k in RULE_KEYS:
        v = g(f"rules.{k}")
        if v is not None and (not isinstance(v, str) or not v.strip()):
            errors.append(f"The {k} rule document override is empty. Clear it to use the file in data/rules, or write the rules.")
        elif isinstance(v, str) and len(v) > MAX_RULE_CHARS:
            errors.append(f"The {k} rule document is longer than {MAX_RULE_CHARS} characters.")
    try:
        rules = effective_rules(cfg, rule_dir)
        missing = [k for k, v in rules.items() if not v.strip()]
        if missing:
            errors.append("No rule text for: " + ", ".join(missing) + ". Add the file in data/rules or write the rules here.")
    except Exception as e:  # pragma: no cover
        errors.append(f"Could not load the rule documents: {e}")
    _str_list(cfg, "rules.mandatory_removals", "Mandatory removals", errors)
    _str_list(cfg, "rules.restricted_phrases", "Restricted phrases", errors)
    wc = g("rules.writing_constraints", "")
    if not isinstance(wc, str) or len(wc) > 3000:
        errors.append("Writing constraints must be text of at most 3000 characters.")

    # ---- research ----
    _num(cfg, "research.words_per_claim", 30, 200, errors, "Words per verified claim")
    _str_list(cfg, "research.trusted_facts", "Trusted facts", errors, TRUSTED_FACTS_MAX)
    for k in ("source_check", "gemini_grounding", "groq_browser_search", "require_source_url"):
        if not isinstance(g(f"research.{k}"), bool):
            errors.append(f"research.{k} must be on or off.")
    if g("research.source_check") is False:
        warnings.append("Source check is off: cited URLs and evidence excerpts will not be verified.")

    # ---- reference website layer ----
    for k in ("enabled", "respect_robots"):
        if not isinstance(g(f"reference.{k}"), bool):
            errors.append(f"reference.{k} must be on or off.")
    _num(cfg, "reference.max_pages", 1, 5, errors, "Reference website pages to read")
    _num(cfg, "reference.cache_hours", 0, 720, errors, "Reference website cache (hours)")
    _num(cfg, "reference.timeout_seconds", 3, 30, errors, "Reference website timeout (seconds)")
    _num(cfg, "reference.max_chars", 2000, 20000, errors, "Reference website sample size (characters)")
    for key, label in (("allowed_domains", "Allowed reference domains"), ("blocked_domains", "Blocked reference domains")):
        _str_list(cfg, f"reference.{key}", label, errors, 50)
        for d in g(f"reference.{key}", []) or []:
            if isinstance(d, str) and d.strip() and not HOSTNAME.match(d.strip().lower().lstrip(".")):
                errors.append(f"{label}: '{d}' is not a website name such as example.com.")
    allowed, blocked = g("reference.allowed_domains", []) or [], g("reference.blocked_domains", []) or []
    both = {str(a).strip().lower() for a in allowed} & {str(b).strip().lower() for b in blocked}
    if both:
        errors.append("The same website is both allowed and blocked: " + ", ".join(sorted(both)) + ".")
    if g("reference.respect_robots") is False:
        warnings.append("robots.txt is ignored: the reference website's own rules about automated access will not be followed.")

    # ---- generation ----
    if g("generation.default_length") not in S.LENGTH_LEVELS:
        errors.append("Default length must be Short, Standard or In-depth.")
    targets = g("generation.length_targets", {})
    for ct in S.CONTENT_TYPES:
        row = targets.get(ct) if isinstance(targets, dict) else None
        vals = [row.get(l) if isinstance(row, dict) else None for l in S.LENGTH_LEVELS]
        if not all(isinstance(v, int) and not isinstance(v, bool) and 50 <= v <= 5000 for v in vals):
            errors.append(f"{ct}: each length target must be a whole number of words between 50 and 5000.")
        elif not (vals[0] < vals[1] < vals[2]):
            errors.append(f"{ct}: targets must grow, Short < Standard < In-depth.")
    enabled = g("generation.enabled_frameworks", [])
    if not isinstance(enabled, list) or not enabled or any(f not in S.FRAMEWORKS for f in enabled):
        errors.append("Enable at least one framework (AIDA, PAS, BAB, BLUF).")
        enabled = []
    df = g("generation.default_framework")
    if df != "Auto" and (df not in S.FRAMEWORKS or (enabled and df not in enabled)):
        errors.append("Default framework must be Auto or one of the enabled frameworks.")
    if g("generation.strategy_predict") not in S.STRATEGY_PREDICT:
        errors.append("Strategy prediction must be ai, always_ai or heuristic.")

    # ---- budget / capabilities ----
    if g("budget.rules_scoping", "all") not in S.RULES_SCOPING:
        errors.append("Rule scoping must be all, groq or off.")
    if g("research.gemini_grounding_capability", "auto") not in S.GROUNDING_CAPABILITY:
        errors.append("Gemini grounding capability must be auto, yes or no.")
    tgt = _num(cfg, "budget.target_headroom", 0.05, 0.9, errors, "Target headroom", integer=False)
    flr = _num(cfg, "budget.floor_headroom", 0.05, 0.9, errors, "Floor headroom", integer=False)
    ceil = _num(cfg, "budget.hard_ceiling", 0.5, 0.95, errors, "Hard ceiling", integer=False)
    if tgt and flr and flr > tgt:
        errors.append("Floor headroom cannot be larger than the target headroom.")
    if ceil and flr and flr < round(1 - ceil, 4):
        errors.append("Floor headroom must be at least 1 minus the hard ceiling (the ceiling is also a limit on headroom).")
    if g("provider.gemini_stage_models_env", "ignore") not in S.STAGE_MODELS_ENV:
        errors.append("Per-stage Gemini model variables must be ignore or apply.")
    limits = g("budget.gemini_limits", [])
    if isinstance(limits, str):
        errors.append("Gemini limits are not valid JSON.")
    elif not isinstance(limits, list):
        errors.append("Gemini limits must be a list of entries.")
    else:
        for i, e in enumerate(limits, 1):
            ok = isinstance(e, dict) and str(e.get("model", "")).strip()
            nums = [e.get(k, 0) for k in ("rpm", "tpm", "rpd")] if isinstance(e, dict) else []
            if not ok or any(isinstance(n, bool) or not isinstance(n, (int, float)) or n < 0 for n in nums):
                errors.append(f"Gemini limit entry {i} needs a model and whole, non-negative rpm / tpm / rpd numbers.")
    groups = g("provider.key_groups", [])
    if isinstance(groups, str):
        errors.append("Key groups are not valid JSON.")
    elif not isinstance(groups, list) or any(
            not (isinstance(r, dict) and r.get("provider") in ("gemini", "groq") and str(r.get("key", "")).strip()
                 and str(r.get("group", "")).strip()) for r in groups):
        errors.append("Key groups must be a list of entries with provider (gemini or groq), key (fingerprint) and group.")
    stage_models = g("provider.gemini_stage_models", {})
    if not isinstance(stage_models, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in stage_models.items()):
        errors.append("Gemini per-stage models must be a mapping of stage name to model name.")
    pool = g("provider.gemini_model_pool", [])
    if not isinstance(pool, list) or any(not isinstance(m, str) for m in pool):
        errors.append("Gemini model pool must be a list of model names.")

    # ---- provider ----
    if g("provider.mode") not in S.PROVIDER_MODES:
        errors.append("Provider mode must be one of: " + ", ".join(S.PROVIDER_MODES) + ".")
    if g("provider.groq_reasoning_effort") not in S.EFFORTS:
        errors.append("Groq reasoning effort must be empty, low, medium or high.")
    for k in ("gemini_model", "groq_model"):
        if not str(g(f"provider.{k}", "")).strip():
            errors.append(f"{k.replace('_', ' ').capitalize()} cannot be empty.")
    tpm = _num(cfg, "provider.groq_tpm", 1000, 1_000_000, errors, "Groq tokens per minute")
    rtpm = _num(cfg, "provider.groq_revision_tpm", 1000, 1_000_000, errors, "Groq revision tokens per minute")
    if tpm and rtpm and rtpm > tpm:
        errors.append("Groq revision budget cannot be larger than the Groq tokens-per-minute limit.")
    _num(cfg, "provider.gemini_min_interval", 0, 60, errors, "Gemini minimum interval (seconds)", integer=False)
    _num(cfg, "provider.key_cooldown_seconds", 10, 900, errors, "Seconds a limited API key rests")
    if not isinstance(g("provider.key_backup"), bool):
        errors.append("provider.key_backup must be on or off.")
    from app.ai.keys import describe
    for w in describe(env, bool(g("provider.key_backup", True)))[1]:
        warnings.append(w)
    mode = g("provider.mode")
    if mode in ("gemini_only", "live") and not env.get("GEMINI_API_KEY"):
        warnings.append("Provider mode needs GEMINI_API_KEY, which is not set in the environment.")
    if mode in ("groq_only", "live") and not env.get("GROQ_API_KEY"):
        warnings.append("Provider mode needs GROQ_API_KEY, which is not set in the environment.")

    # ---- qa ----
    _num(cfg, "qa.max_revisions", 0, 5, errors, "Maximum revisions")
    _num(cfg, "qa.min_length_ratio", 0.5, 1.0, errors, "Minimum length ratio", integer=False)
    checks = g("qa.checks", {})
    for k in S.QA_CHECKS:
        if not isinstance(checks.get(k), bool):
            errors.append(f"QA check '{k}' must be on or off.")
        elif checks[k] is False:
            warnings.append(f"QA check switched off: {S.QA_CHECKS[k]}.")

    # ---- system ----
    cd = str(g("system.cache_dir", "")).strip()
    # Treat both Windows and POSIX absolute paths as absolute regardless of the
    # platform running the validator. This keeps config validation portable and
    # prevents values such as /tmp/x from slipping through on Windows.
    normalized_cd = cd.replace("\\", "/")
    if (not cd or os.path.isabs(cd) or ntpath.isabs(cd) or posixpath.isabs(normalized_cd)
            or ".." in normalized_cd.split("/")):
        errors.append("Cache folder must be a relative path inside the project, without '..'.")
    if g("system.default_mode") not in S.MODES:
        errors.append("Default mode must be New Content or Revamp.")
    for s in S.STAGES:
        v = g(f"system.stage_instructions.{s}", "")
        if not isinstance(v, str) or len(v) > 2000:
            errors.append(f"The {s} instruction must be text of at most 2000 characters.")
    for k in ("pipeline_cache", "lean_mode"):
        if not isinstance(g(f"system.{k}"), bool):
            errors.append(f"system.{k} must be on or off.")

    # ---- token budget (this is what actually breaks runs on Groq's free tier) ----
    # Groq only ever receives the research excerpt when rules are scoped, so that is what must fit; unscoped it is everything.
    if tpm and not [e for e in errors if "rule" in e.lower()]:
        what, rule_tokens = "The rule documents", rules_tokens(cfg, rule_dir)
        if g("budget.rules_scoping", "all") != "off":
            from app.rules.loader import build_stage_system_prompt
            try:
                rule_tokens = int(len(build_stage_system_prompt(effective_rules(cfg, rule_dir), None, "research", scoped=True)) / 4)
                what = "The research excerpt of the rule documents"
            except ValueError:
                pass
        used = rule_tokens + OUTPUT_RESERVE
        share = used / tpm
        if share >= 0.97:
            errors.append(f"{what} alone (about {used} tokens with the reply reserve) fill the whole {tpm} token budget. "
                          "Shorten them or raise the Groq limit.")
        elif share >= 0.6:
            warnings.append(f"{what} use about {int(share * 100)}% of the {tpm} token budget, leaving little room for "
                            "research and drafts on Groq.")
    return errors, warnings
