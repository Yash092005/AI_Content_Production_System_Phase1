"""AIDA / PAS / BAB / BLUF: writing STRUCTURE only. A framework never supplies or justifies facts."""
import re

FRAMEWORKS = {
    "AIDA": {
        "expansion": "Attention → Interest → Desire → Action",
        "stages": ["Attention", "Interest", "Desire", "Action"],
        "guidance": "Open with a hook grounded in a verified fact, build interest with relevant verified detail, "
                    "create desire from verified benefits only, and close with one clear action.",
    },
    "PAS": {
        "expansion": "Problem → Agitate → Solution",
        "stages": ["Problem", "Agitate", "Solution"],
        "guidance": "Name the problem the reader actually has, show why it matters using verified context only "
                    "(no invented stakes or statistics), then present the solution.",
    },
    "BAB": {
        "expansion": "Before → After → Bridge",
        "stages": ["Before", "After", "Bridge"],
        "guidance": "Describe the reader's situation today, the improved situation, then bridge the two. "
                    "The 'after' may only promise outcomes that are verified.",
    },
    "BLUF": {
        "expansion": "Bottom Line Up Front",
        "stages": ["Bottom Line", "Context", "Details"],
        "guidance": "Give the answer or key takeaway in the first lines, then context, then supporting detail. "
                    "The bottom line must itself be a verified claim.",
    },
}
MARKER_RE = re.compile(r"<!--\s*stage:\s*([A-Za-z ]+?)\s*-->")


def stage_marker(stage):
    return f"<!-- stage: {stage} -->"


def framework_block(strategy, mode):
    """Prompt block that tells the writer HOW to communicate. Evidence rules are repeated on purpose."""
    if not strategy or strategy.get("framework") not in FRAMEWORKS:
        return ""
    fw = strategy["framework"]
    spec = FRAMEWORKS[fw]
    lines = [
        "\nCONTENT STRATEGY (HOW to write. It is NOT evidence.)",
        f"- {strategy['content_type']} | {strategy['market_type']} | {strategy['audience']} | stage: {strategy['relationship_stage']} "
        f"| intent: {strategy['user_intent']} | objective: {strategy['objective']}",
        f"- Job to be done: {strategy['jtbd']}",
        f"- Framework: {fw} ({spec['expansion']}). {spec['guidance']}",
        "- Persuasion must never add claims, numbers, superlatives, guarantees, timings or service promises beyond "
        "VERIFIED CLAIMS. If a stage lacks evidence, keep it short and neutral.",
    ]
    if mode == "Revamp":
        lines.append("- REVAMP: the locked section layout wins. Use the framework only for tone and the order of ideas "
                     "INSIDE each section. Do not add, rename or reorder sections. Do not add stage markers.")
    else:
        lines.append("- Follow the stage order. Put each marker on its own line before the part that delivers that stage: "
                     + " ".join(stage_marker(s) for s in spec["stages"]) + ". Keep headings natural.")
    return "\n".join(lines) + "\n"


def framework_block_compact(strategy, mode):
    """Minimal version for prompts that are over the token budget. Keeps the rules that matter: the evidence boundary
    and (New Content) the stage markers, so a trimmed revision does not silently drop the framework structure."""
    if not strategy or strategy.get("framework") not in FRAMEWORKS:
        return ""
    fw = strategy["framework"]
    spec = FRAMEWORKS[fw]
    if mode == "Revamp":
        return f"\nSTRATEGY: {fw}. The locked layout wins: no new or renamed sections, no stage markers. Persuasion only from VERIFIED CLAIMS.\n"
    return (f"\nSTRATEGY: {fw} ({spec['expansion']}). Keep these markers on their own lines, in order: "
            + " ".join(stage_marker(x) for x in spec["stages"]) + ". Persuasion only from VERIFIED CLAIMS.\n")


def research_note(strategy):
    if not strategy or strategy.get("framework") not in FRAMEWORKS:
        return ""
    return (f"\nContent strategy: {strategy['content_type']} / {strategy['user_intent']} intent / "
            f"{strategy['objective']} objective, written with {strategy['framework']}. Prefer evidence that supports "
            f"each stage ({', '.join(FRAMEWORKS[strategy['framework']]['stages'])}). The framework is structure, never evidence.\n")


def found_stages(draft):
    return [m.strip() for m in MARKER_RE.findall(str(draft or ""))]


_LABEL_RE = re.compile(r"(?m)^[ \t]*(?:\*\*|__)?([A-Za-z ]{3,20}?)(?:\*\*|__)?\s*:\s*(?:\*\*|__)?[ \t]*")


def labels_to_markers(draft):
    """If the writer printed framework stage names as visible labels ('**Bottom Line:** ...') instead of hidden
    comments, convert them to hidden markers. Needs at least 2 stages of one framework, so ordinary
    sentences that merely start with a word like 'Action:' are left alone."""
    text = str(draft or "")
    if MARKER_RE.search(text):
        return text
    names = {m.group(1).strip().lower() for m in _LABEL_RE.finditer(text)}
    best = None
    for fw, spec in FRAMEWORKS.items():
        hits = [st for st in spec["stages"] if st.lower() in names]
        if len(hits) >= 2 and (best is None or len(hits) > len(best[1])):
            best = (fw, hits)
    if not best:
        return text
    allowed = {st.lower(): st for st in FRAMEWORKS[best[0]]["stages"]}

    def repl(m):
        stage = allowed.get(m.group(1).strip().lower())
        return stage_marker(stage) + "\n" if stage else m.group(0)
    return _LABEL_RE.sub(repl, text)


def strip_stage_markers(draft):
    out = MARKER_RE.sub("", str(draft or ""))
    return re.sub(r"\n{3,}", "\n\n", out).strip() + "\n" if out.strip() else ""


def framework_check(draft, strategy, mode):
    """Light structure check. Returns (issues, warnings).
    - no markers at all  -> warning (the writer may have ignored the comment convention)
    - partial / wrong order -> issue (the writer tried and broke the structure)
    Revamp is never checked: its layout is locked."""
    if mode == "Revamp" or not strategy or strategy.get("framework") not in FRAMEWORKS:
        return [], []
    fw = strategy["framework"]
    expected = FRAMEWORKS[fw]["stages"]
    found = found_stages(draft)
    if not found:
        return [], [f"No {fw} stage markers found, so the {fw} structure could not be checked automatically."]
    missing = [s for s in expected if s not in found]
    issues = []
    if missing:
        issues.append(f"Framework {fw}: missing stage(s) {', '.join(missing)}. Add "
                      + " ".join(stage_marker(s) for s in missing) + " before the part that delivers each stage.")
    else:
        order = [found.index(s) for s in expected]
        if order != sorted(order):
            issues.append(f"Framework {fw}: stages are out of order. Required order: {' → '.join(expected)}.")
    return issues, []
