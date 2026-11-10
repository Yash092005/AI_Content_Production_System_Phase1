"""Content Development fields. Every field is a CONTROLLED list: the AI may only pick from these."""
import re

CONTENT_TYPES = ["Blog", "Homepage", "CTA Page"]
MARKET_TYPES = ["B2C", "B2B"]
AUDIENCES = ["First-time visitor", "Existing customer", "Decision maker"]
STAGES = ["Awareness", "Consideration", "Decision", "Existing Customer"]
INTENTS = ["Informational", "Commercial", "Transactional", "Navigational"]
OBJECTIVES = ["Educate", "Build awareness", "Generate leads", "Drive conversion", "Build trust", "Encourage action"]
FRAMEWORK_CODES = ["AIDA", "PAS", "BAB", "BLUF"]

OPTIONS = {
    "content_type": CONTENT_TYPES,
    "market_type": MARKET_TYPES,
    "audience": AUDIENCES,
    "relationship_stage": STAGES,
    "user_intent": INTENTS,
    "objective": OBJECTIVES,
}
LABELS = {
    "content_type": "Content Type",
    "market_type": "Market Type",
    "audience": "Audience",
    "relationship_stage": "Relationship Stage",
    "user_intent": "User Intent",
    "jtbd": "Jobs To Be Done",
    "objective": "Content Objective",
    "framework": "Framework",
}
DEFAULTS = {
    "content_type": "Blog",
    "market_type": "B2C",
    "audience": "First-time visitor",
    "relationship_stage": "Awareness",
    "user_intent": "Informational",
    "jtbd": "I want to understand this topic",
    "objective": "Educate",
}
# Types the MVP does not support yet -> closest supported type.
CONTENT_TYPE_ALIASES = {
    "blog post": "Blog", "article": "Blog", "blogpost": "Blog",
    "home page": "Homepage", "homepage": "Homepage",
    "cta": "CTA Page", "call to action": "CTA Page", "call to action page": "CTA Page",
    "landing page": "CTA Page", "landing": "CTA Page", "product page": "CTA Page", "service page": "CTA Page",
}
JTBD_MAX = 200


def _key(v):
    return re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()


def match_option(field, value):
    """Return the canonical option for `value`, or None when it is not allowed."""
    k = _key(value)
    for opt in OPTIONS[field]:
        if _key(opt) == k:
            return opt
    return None


def normalize(raw, base=None):
    """Snap a raw dict onto the controlled lists. Returns a dict with all seven fields plus `notes`
    (what was corrected). Anything invalid falls back to `base` (if given) or the default."""
    raw = raw if isinstance(raw, dict) else {}
    base = base or DEFAULTS
    out, notes = {}, []
    for field in OPTIONS:
        v = raw.get(field)
        hit = match_option(field, v)
        if hit is None and field == "content_type" and v:
            alias = CONTENT_TYPE_ALIASES.get(_key(v))
            if alias:
                notes.append(f"{v} is not supported yet; using {alias}.")
                hit = alias
        if hit is None:
            hit = base.get(field, DEFAULTS[field])
            if v not in (None, ""):
                notes.append(f"{LABELS[field]} '{v}' is not an allowed value; using {hit}.")
        out[field] = hit
    j = re.sub(r"\s+", " ", str(raw.get("jtbd") or "")).strip()[:JTBD_MAX]
    out["jtbd"] = j or base.get("jtbd", DEFAULTS["jtbd"])
    out["notes"] = notes
    return out
