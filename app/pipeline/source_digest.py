"""Revamp source pages: a verified, verbatim fact digest for the small-limit search route.

When the full source page does not fit the preferred research route with the required headroom, a large-context model (Gemini)
lists the factual statements of the page. The digest is then BUILT FROM THE PAGE ITSELF, never from the model's wording:

  * every unit of the page (heading, list line, sentence) is either kept verbatim or shown to carry no checkable fact;
  * a unit is kept when the model listed it OR when a deterministic check finds anything fact-bearing in it: numbers, dates,
    times, prices, URLs, e-mail addresses, names (capitalised words inside a sentence), rules and obligations ("must",
    "not permitted", "only"), availability and contact words, units and distances;
  * lines the model returned that are not found in the page are dropped (they would be invented text);
  * headings keep the page's structure.

The digest is accepted only if the model's list was trustworthy (most lines really are in the page) and the digest is clearly smaller
than the page. Otherwise the caller must use the original page (or pause): an unverifiable digest is never treated as complete.
Limit, stated plainly: a sentence that has none of the signals above is treated as descriptive text; the signals are deliberately wide."""
import re
from dataclasses import dataclass, field

EXTRACT_INSTRUCTION = (
    "List every factual statement in the SOURCE PAGE below, exactly as written (verbatim, never reworded or shortened), "
    "one per line, in page order, each as 'Heading :: sentence' using the heading it sits under. A factual statement is any "
    "sentence with a name, number, time, date, price, distance, rule, instruction, requirement, prohibition, contact detail, "
    "claim or promise. Skip only sentences that are purely descriptive. Return plain text only, no commentary.\n\nSOURCE PAGE:\n")

MIN_VALID_FRACTION = 0.8      # share of the model's lines that must be found verbatim in the page
MAX_RATIO = 0.85              # the digest must be at most this share of the page, or it is not worth using

DIGIT = re.compile(r"\d")
URL = re.compile(r"(https?://|www\.)\S+|\b[\w-]+\.(com|org|in|net|gov|edu|co|io)\b", re.I)
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
CURRENCY = re.compile(r"[₹$€£]|\b(rs\.?|inr|usd|rupees?|dollars?)\b", re.I)
WORDS = re.compile(
    r"\b(am|pm|noon|midnight|daily|weekly|monthly|yearly|annual|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"km|kms|kilomet\w+|metres?|meters?|miles?|minutes?|hours?|days?|weeks?|months?|years?|"
    r"must|shall|should|required?|requires|requirement|mandatory|compulsory|necessary|essential|"
    r"not\s+(allowed|permitted|available|open|accepted)|prohibited|forbidden|banned|restricted|strictly|only|never|always|"
    r"no\s+(entry|photography|mobile|phones?|footwear|bags?)|closed?|closes|opens?|open|"
    r"free|fees?|charges?|costs?|prices?|tickets?|passes?|book(ing|ings|ed)?|reservations?|donations?|discounts?|"
    r"address|located|location|phone|call|email|contact|whatsapp|helpline|"
    r"dress\s+code|timings?|schedule|deadline|valid|expire\w*|"
    r"first|second|third|fourth|fifth|largest|oldest|only|unique)\b", re.I)


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "").strip(" -•*\t")).casefold()


def has_fact_signal(text):
    """True when the unit holds anything that could be a checkable fact (deliberately wide: a wrong 'yes' only keeps text)."""
    if DIGIT.search(text) or URL.search(text) or EMAIL.search(text) or CURRENCY.search(text) or WORDS.search(text):
        return True
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", text)
    return any(w[0].isupper() and w not in ("I", "A", "An", "The") for w in words[1:])      # a name inside the sentence


def split_units(source):
    """The page as ordered (text, is_heading) units. A heading / list line is a short line without closing punctuation."""
    units = []
    for line in str(source or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) <= 80 and not re.search(r"[.!?;:]$", line):
            units.append((line, True))
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = sentence.strip()
            if sentence:
                units.append((sentence, False))
    return units


def extraction_prompt(source):
    return EXTRACT_INSTRUCTION + source


@dataclass
class DigestResult:
    ok: bool
    text: str
    report: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)


def _model_lines(extraction):
    lines = []
    for raw in str(extraction or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        part = raw.split("::", 1)[1] if "::" in raw else raw
        part = _norm(part)
        if len(part) >= 3:
            lines.append(part)
    return lines


def build_digest(source, extraction):
    """-> DigestResult. `text` is made only of the page's own units, in page order."""
    source = str(source or "")
    norm_page = _norm(source)
    model = _model_lines(extraction)
    valid = [m for m in model if m in norm_page]
    blob = " | ".join(valid)
    kept, by_signal, by_model_only, skipped = [], 0, 0, 0
    for text, heading in split_units(source):
        n = _norm(text)
        signal = heading or has_fact_signal(text)
        listed = bool(valid) and (n in valid or (len(n) >= 12 and n in blob))
        if signal or listed:
            kept.append(text)
            by_signal += 1 if signal else 0
            by_model_only += 1 if (listed and not signal) else 0
        else:
            skipped += 1
    digest = "\n".join(kept)
    report = {"page_chars": len(source), "digest_chars": len(digest), "units": len(kept) + skipped, "kept": len(kept),
              "kept_by_check": by_signal, "kept_only_because_model_listed_it": by_model_only,
              "descriptive_units_left_out": skipped, "model_lines": len(model), "model_lines_found_in_page": len(valid),
              "model_lines_dropped_not_in_page": len(model) - len(valid)}
    reasons = []
    if not model or not valid:
        reasons.append("the model's list contained no line that is in the page")
    elif len(valid) / len(model) < MIN_VALID_FRACTION:
        reasons.append(f"only {len(valid)} of {len(model)} listed lines are in the page, so the list cannot be trusted")
    if not kept:
        reasons.append("nothing was kept")
    if source and len(digest) > MAX_RATIO * len(source):
        reasons.append(f"the digest ({len(digest)} chars) is not clearly smaller than the page ({len(source)} chars)")
    return DigestResult(not reasons, digest, report, reasons)


def digest_block(digest):
    return ("\nSOURCE PAGE FACTS (the existing page's own sentences, verbatim and in page order, minus purely descriptive text; "
            f"the system keeps the full page and its structure):\n{digest}\n")
