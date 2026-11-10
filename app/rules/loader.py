import json
import re
from pathlib import Path

# Absolute path, so it works no matter where the app is launched from
RULE_DIR = Path(__file__).resolve().parents[2] / "data" / "rules"

EXPECTED_FILES = {
    "master": "master_instructions.txt",
    "canonical": "canonical_specification.txt",
    "brand": "brand_constitution.txt",
}


def load_rules(rule_dir=None, overrides=None):
    """Return {key: full text}. Missing file -> empty string (see rules_status).

    A rule document edited in the Configuration tab replaces the file's text. `overrides` can be passed explicitly
    (validation does); otherwise the settings active for this run are used, and with none active the files are used
    exactly as before."""
    base = Path(rule_dir) if rule_dir else RULE_DIR
    rules = {}
    for key, filename in EXPECTED_FILES.items():
        path = base / filename
        rules[key] = path.read_text(encoding="utf-8") if path.exists() else ""
    if overrides is None:
        from app.config import runtime
        active = runtime.current()
        overrides = active.get("rules", {}) if active is not None else {}
    for key in rules:
        text = (overrides or {}).get(key)
        if isinstance(text, str) and text.strip():
            rules[key] = text
    return rules


def rules_status(rules):
    return {k: ("LOADED" if v.strip() else "MISSING") for k, v in rules.items()}


def all_rules_loaded(rules):
    return all(v.strip() for v in rules.values())


def build_prompt(rules, request):
    """Inject the FULL governing documents into the prompt (no model-side retrieval)."""
    if not all_rules_loaded(rules):
        missing = [k for k, v in rules.items() if not v.strip()]
        raise ValueError(f"Cannot build prompt, missing rule documents: {missing}")
    return (
        "=== MASTER INSTRUCTIONS ===\n" + rules["master"]
        + "\n\n=== CANONICAL SPECIFICATION ===\n" + rules["canonical"]
        + "\n\n=== BRAND CONSTITUTION ===\n" + rules["brand"]
        + "\n\n=== USER REQUEST ===\n" + request
    )


def build_system_prompt(rules, task_constraints=None):
    """Governing documents as the system prompt (full text, every call)."""
    if not all_rules_loaded(rules):
        missing = [k for k, v in rules.items() if not v.strip()]
        raise ValueError(f"Cannot build prompt, missing rule documents: {missing}")
    parts = [
        "You are the execution engine of the AI content system. The documents below are binding and complete.",
        "=== MASTER INSTRUCTIONS ===\n" + rules["master"],
        "=== CANONICAL SPECIFICATION ===\n" + rules["canonical"],
        "=== BRAND CONSTITUTION ===\n" + rules["brand"],
    ]
    if task_constraints:
        parts.append("=== TASK-SPECIFIC CONSTRAINTS ===\n" + task_constraints)
    return "\n\n".join(parts)


# ---- stage-scoped rules ------------------------------------------------------------------------------------------
PROFILES_FILE = RULE_DIR / "profiles.json"
STAGE_PROFILE = {"research": "research", "research_blueprint": "research_blueprint", "blueprint": "blueprint",
                 "generate": "generate", "qa": "qa"}
_SECTION = re.compile(r"(?m)^(?=\d+\. [A-Z])")


def rules_scoping_mode():
    """'all' (each stage gets only its sections), 'groq' (only calls that go to Groq are scoped; Gemini keeps the full
    documents), 'off' (every call gets the full documents, as before)."""
    from app.config import runtime
    mode = str(runtime.get("budget.rules_scoping", "RULES_SCOPING", "all")).strip().lower()
    return mode if mode in ("all", "groq", "off") else "all"


def load_profiles(path=None):
    try:
        data = json.loads(Path(path or PROFILES_FILE).read_text(encoding="utf-8"))
        return data.get("profiles", {})
    except (OSError, ValueError):
        return {}


def split_sections(text):
    """-> (preamble, {'1': text, ...}) for a rule document whose sections start with 'N. TITLE'."""
    parts = _SECTION.split(text or "")
    sections = {}
    for part in parts[1:]:
        m = re.match(r"(\d+)\. ", part)
        if m:
            sections[m.group(1)] = part.rstrip()
    return parts[0].rstrip(), sections


def scope_rules(rules, stage, profiles=None):
    """-> (rules for this stage, notes). A document that cannot be scoped safely (no numbered sections, or a section named
    in the profile is missing) is passed in FULL and noted, so scoping can never silently remove a rule."""
    name = STAGE_PROFILE.get(stage)
    profile = (profiles if profiles is not None else load_profiles()).get(name) if name else None
    if not profile:
        return dict(rules), []
    scoped, notes = {}, []
    for key, text in rules.items():
        want = profile.get(key)
        if want is None:
            scoped[key] = text
            continue
        pre, secs = split_sections(text)
        missing = [w for w in want if w != "preamble" and w not in secs]
        if not secs or missing:
            notes.append(f"{key}: section(s) {missing or 'all'} not found; sent in full for '{stage}'")
            scoped[key] = text
            continue
        pieces = ([pre] if "preamble" in want and pre else []) + [secs[w] for w in want if w != "preamble"]
        scoped[key] = "\n\n".join(pieces)
    return scoped, notes


def build_stage_system_prompt(rules, task_constraints=None, stage=None, scoped=True, profiles=None):
    """System prompt for one stage. With scoped=False (or a stage without a profile) this is exactly build_system_prompt."""
    if not scoped or not STAGE_PROFILE.get(stage):
        return build_system_prompt(rules, task_constraints)
    if not all_rules_loaded(rules):
        missing = [k for k, v in rules.items() if not v.strip()]
        raise ValueError(f"Cannot build prompt, missing rule documents: {missing}")
    sc, _ = scope_rules(rules, stage, profiles)
    if sc == rules:
        return build_system_prompt(rules, task_constraints)
    parts = [
        f"You are the execution engine of the AI content system. The excerpts below are the binding rules for this stage "
        f"('{stage}') of the pipeline; the other stages apply the remaining sections of the same documents.",
        "=== MASTER INSTRUCTIONS (excerpts) ===\n" + sc["master"],
        "=== CANONICAL SPECIFICATION (excerpts) ===\n" + sc["canonical"],
        "=== BRAND CONSTITUTION (excerpts) ===\n" + sc["brand"],
    ]
    if task_constraints:
        parts.append("=== TASK-SPECIFIC CONSTRAINTS ===\n" + task_constraints)
    return "\n\n".join(parts)
