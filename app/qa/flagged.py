"""Code-side helpers for QA findings: remove the sentences QA flagged as unsupported, and catch process wording.

Why: the revision step is a model call and it sometimes leaves a flagged sentence in. When QA quotes the exact
sentence and says it is unverified, code can delete it deterministically instead of hoping the model does."""
import re

_QUOTE = re.compile(r'[“"‘]([^”"’]{25,}?)[”"’]')
_WHY = re.compile(r"unverified|unsupported|no (?:authoritative )?(?:source|evidence)|not (?:in|supported)", re.I)
_PROCESS_HEADING = re.compile(r"\b(?:based on (?:the )?(?:verified|available)|verified (?:information|claims|facts)|"
                              r"according to the (?:blueprint|evidence))\b", re.I)


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(s).lower())).strip()


def flagged_quotes(issues):
    """Long quoted passages that QA called unverified/unsupported (short quotes like 'traditional' are ignored)."""
    out = []
    for issue in issues or []:
        if not _WHY.search(str(issue)):
            continue
        for q in _QUOTE.findall(str(issue)):
            q = q.strip().rstrip(".")
            if len(q.split()) >= 5 and q not in out:
                out.append(q)
    return out


def remove_flagged_sentences(draft, issues):
    """Returns (new_draft, removed_sentences, resolved_issue_texts). Headings and hidden stage markers are never touched."""
    quotes = [(_norm(q), q) for q in flagged_quotes(issues)]
    if not quotes:
        return draft, [], []
    removed, new_lines = [], []
    for line in str(draft).split("\n"):
        if line.lstrip().startswith("#") or line.strip().startswith("<!--") or not line.strip():
            new_lines.append(line)
            continue
        if not any(nq and nq in _norm(line) for nq, _ in quotes):
            new_lines.append(line)
            continue
        m = re.match(r"^(\s*(?:[-*]|\d+[.)])\s+)?(.*)$", line)
        prefix, body = m.group(1) or "", m.group(2)
        kept = []
        for part in re.split(r"(?<=[.!?])\s+", body):
            pn = _norm(part)
            hit = any(nq and nq in pn for nq, _ in quotes) or \
                any(len(pn.split()) >= 5 and pn in nq for nq, _ in quotes)
            if hit:
                removed.append(part.strip())
            else:
                kept.append(part)
        if kept:
            new_lines.append(prefix + " ".join(kept))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(new_lines))
    resolved = [i for i in issues if any(q in str(i) for _, q in quotes) and any(
        _norm(r) and (_norm(r) in _norm(q) or _norm(q) in _norm(r)) for _, q in quotes for r in removed)]
    return text, removed, resolved


def process_wording_issues(draft):
    """Headings that expose how the page was made (for example 'FAQ (Based on Verified Information)')."""
    out = []
    for line in str(draft or "").split("\n"):
        if line.lstrip().startswith("#") and _PROCESS_HEADING.search(line):
            out.append(f"Heading uses process wording: '{line.strip('# ').strip()}'. Use a plain keyword heading.")
    return out
