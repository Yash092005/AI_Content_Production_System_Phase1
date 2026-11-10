import re

STUB_MARKER = "not connected yet"


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s.lower())).strip()


def find_removal_violations(text, removals):
    """Verbatim recurrence only. Paraphrase/implication is judged by the QA model."""
    t = _norm(text or "")
    return [r for r in (removals or []) if r.strip() and _norm(r) in t]


def validate(draft: str, rules: dict, removals=None):
    banned = find_removal_violations(draft, removals)
    checks = {
        "draft_present": bool(draft and draft.strip()),
        "draft_is_real": bool(draft) and STUB_MARKER not in draft.lower(),
        "all_rules_loaded": all(bool(v.strip()) for v in rules.values()),
        "no_banned_phrases": not banned,
    }
    issues = [name for name, ok in checks.items() if not ok]
    return {"status": "PASS" if not issues else "FAIL", "checks": checks,
            "issues": issues, "banned_phrases_found": banned}
