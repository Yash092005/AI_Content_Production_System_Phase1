"""Content length control. Length must come from DEPTH of verified material, never from padding or invented facts."""
import re

from app.config import runtime
from app.strategy.frameworks import strip_stage_markers, labels_to_markers

# Minimum words of body text required per Revamp section before QA passes.
# One or two verification-pending sentences can hit ~30 words, so sections
# that are nothing but stubs will fall below this threshold.
REVAMP_SECTION_MIN_WORDS = 30

LENGTH_LEVELS = ["Short", "Standard", "In-depth"]
DEFAULT_LENGTH = "Standard"
# Target words per content type and level (a Blog earns more words than a CTA page).
TARGETS = {
    "Blog":     {"Short": 450, "Standard": 900, "In-depth": 1500},
    "Homepage": {"Short": 350, "Standard": 600, "In-depth": 900},
    "CTA Page": {"Short": 250, "Standard": 450, "In-depth": 700},
}
LOW_RATIO = 0.8     # below 80% of target is materially below the requested length


def count_words(text):
    """The ONE word counter: ignores hidden stage markers and markdown symbols, so every screen shows the same number."""
    clean = strip_stage_markers(labels_to_markers(text))
    return len([t for t in clean.split() if re.search(r"\w", t)])


def _targets():
    """Length targets from the active configuration, else the built-in table."""
    t = runtime.get("generation.length_targets")
    return t if isinstance(t, dict) and t else TARGETS


def min_ratio():
    """Below this share of the target a draft is materially short (configurable under QA & Validation)."""
    return float(runtime.get("qa.min_length_ratio", None, LOW_RATIO))


def words_per_claim():
    """About how many words of prose one verified claim supports (configurable under Research & Evidence)."""
    return max(1, int(runtime.get("research.words_per_claim", None, 75)))


def recommended_claims(target):
    return max(8, round(target / words_per_claim()))


def target_words(level, content_type):
    level = level if level in LENGTH_LEVELS else DEFAULT_LENGTH
    targets = _targets()
    row = targets.get(content_type) or targets.get("Blog") or TARGETS["Blog"]
    return int(row.get(level, TARGETS["Blog"][level]))


def _lo_hi(target):
    return int(target * min_ratio()), int(target * 1.25)


def length_block(level, content_type, mode):
    """Generation/revision prompt block. Revamp keeps the source layout, so it gets no length instruction."""
    if mode == "Revamp" or level not in LENGTH_LEVELS:
        return ""
    t = target_words(level, content_type)
    lo, hi = _lo_hi(t)
    return (f"\nLENGTH: about {t} words (range {lo}-{hi}). Develop each verified claim fully: context, what it means for "
            "the reader, practical points, and a short FAQ from verified claims only. Do not pad, repeat or add any "
            f"unverified fact. If VERIFIED CLAIMS cannot support {lo} words, write what they support and stop.\n")


def length_block_compact(level, content_type, mode):
    if mode == "Revamp" or level not in LENGTH_LEVELS:
        return ""
    return f"\nLENGTH: about {target_words(level, content_type)} words. Never pad or add unverified facts.\n"


def research_depth_note(level, content_type, mode):
    if mode == "Revamp" or level not in LENGTH_LEVELS:
        return ""
    t = target_words(level, content_type)
    claims = recommended_claims(t)  # words of prose per verified claim is configurable; more claims cost tokens on small budgets
    sections = max(4, round(t / 150))
    return (f"\nDEPTH: aim for at least {claims} distinct, source-backed verified claims spread across about {sections} "
            f"sections, so the content can reach about {t} words. Prefer breadth of verified facts (dates, meaning, history, "
            "regional practice, practical guidance) over longer wording per fact.\n")



def length_gate_issues(draft, level, content_type, mode, evidence_status="not_applicable"):
    """Return a deterministic QA issue when a requested length is materially missed.

    A materially short New Content draft must never reach PASS/Ready to ship.
    This remains evidence-aware: when evidence is thin, the failure asks for
    deeper research/trusted facts rather than encouraging filler or invention.
    Revamp is excluded because it preserves the source page's existing length/layout.
    """
    if mode == "Revamp" or level not in LENGTH_LEVELS:
        return []
    target = target_words(level, content_type)
    words = count_words(draft)
    minimum = int(target * min_ratio())
    if words < minimum:
        if evidence_status == "sufficient":
            message = (
                f"Length requirement not met: draft is {words} words; minimum acceptable length is about {minimum} words "
                f"for the {level} target of ~{target}. Expand only with VERIFIED CLAIMS; do not add unsupported facts or filler."
            )
        elif evidence_status == "thin":
            message = (
                f"Length requirement not met: draft is {words} words; minimum acceptable length is about {minimum} words "
                f"for the {level} target of ~{target}, and the verified evidence set is still too thin. "
                "Run a deeper research pass or add trusted facts; do not pad or invent facts."
            )
        else:
            message = (
                f"Length requirement not met: draft is {words} words; minimum acceptable length is about {minimum} words "
                f"for the {level} target of ~{target}. Resolve evidence depth before publishing; do not pad or invent facts."
            )
        return [message]
    return []


def revamp_section_min_words():
    """Minimum body-word count per section in a Revamp draft (configurable under QA & Validation)."""
    return max(10, int(runtime.get("qa.revamp_section_min_words", None, REVAMP_SECTION_MIN_WORDS)))


def revamp_coverage_issues(draft, blueprint_sections, mode):
    """Deterministic QA: each section in a Revamp output must have meaningful body content.

    A section that contains only a verification-pending stub (e.g. \"Details require confirmation\") is
    acceptable only as a last resort — it must still have at least revamp_section_min_words() words of
    body text to count as substantially present.

    The reference website must never be the reason a section is underwritten: this check fires
    regardless of reference-site style.
    """
    if mode != "Revamp" or not draft or not blueprint_sections:
        return []
    sections = [str(x).strip() for x in blueprint_sections if str(x).strip()]
    if not sections or any(x.lower().startswith("[mock") for x in sections):
        return []

    min_words = revamp_section_min_words()
    # Split draft into per-section chunks by finding heading boundaries.
    # A heading is a markdown # line whose normalised text matches the section name.
    norm_draft = re.sub(r"[ \t]+", " ", draft)
    heading_re = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
    heading_matches = list(heading_re.finditer(norm_draft))

    def _norm(s):
        return re.sub(r"[^a-z0-9\s]", " ", str(s).lower()).strip()

    # Map each section to its body text by finding the heading boundary
    thin_sections = []
    for section in sections:
        norm_section = _norm(section)
        # Find which heading match corresponds to this section
        section_body = ""
        for i, m in enumerate(heading_matches):
            if norm_section in _norm(m.group(1)):
                # Body is text from after this heading to start of next heading (or end of draft)
                body_start = m.end()
                body_end = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(norm_draft)
                section_body = norm_draft[body_start:body_end]
                break
        body_words = len([w for w in section_body.split() if re.search(r"\w", w)])
        if section_body and body_words < min_words:
            thin_sections.append(f"{section!r} ({body_words} words — needs at least {min_words})")

    if thin_sections:
        return [f"revamp section content too thin — write more from VERIFIED CLAIMS, do not pad or invent: "
                + "; ".join(thin_sections)]
    return []


def revamp_coverage_block(blueprint_sections, mode):
    """Generation prompt instruction: each Revamp section must have substantial body text.

    This is separate from the section-existence contract (which just checks heading presence).
    It ensures a concise reference-website style does not cause the writer to stub out sections.
    """
    if mode != "Revamp" or not blueprint_sections:
        return ""
    min_words = revamp_section_min_words()
    return (
        f"\nREVAMP CONTENT COVERAGE (NON-NEGOTIABLE):\n"
        f"Every section listed in the REVAMP SECTION CONTRACT above must contain at least {min_words} words of "
        "meaningful body text — not just a verification-pending stub. "
        "Use VERIFIED CLAIMS to fill each section. Where a section has no verified claims available, write "
        "a neutral sentence explaining that details are pending verification and supplement with any general, "
        "non-factual context that is appropriate. "
        "The reference website’s concise style applies to HOW each sentence is worded, "
        "NEVER to how much content each section contains. "
        "Do not collapse, merge, or stub out sections because the reference site is brief.\n"
    )

def length_info(draft, level, content_type, mode):
    """(info dict for the result, warnings list)."""
    if mode == "Revamp" or level not in LENGTH_LEVELS:
        return None, []
    t = target_words(level, content_type)
    words = count_words(draft)
    info = {"level": level, "target": t, "words": words}
    warns = []
    if words and words < int(t * min_ratio()):
        warns.append(f"Draft is {words} words, below the ~{t} word target for {level}. Research found limited verified "
                     "material. Add trusted facts under Advanced inputs, or run again with Fresh run for a deeper search.")
    return info, warns
