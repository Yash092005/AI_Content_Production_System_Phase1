"""Checks the sources the model cites, so made-up URLs and unconfirmed times do not count as verified."""
import re
import requests
from html.parser import HTMLParser

TIME_TOKEN = re.compile(r"\b(\d{1,2})\s*[:.]\s*(\d{2})\b")
UA = {"User-Agent": "Mozilla/5.0 (compatible; content-tool-source-check/0.2)"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _page_text(raw):
    raw = str(raw or "")
    if not raw:
        return ""
    try:
        parser = _TextExtractor()
        parser.feed(raw)
        raw = " ".join(parser.parts)
    except Exception:
        pass
    return re.sub(r"\s+", " ", raw).strip()


def _norm_text(value):
    value = str(value or "").lower().replace(
        "’", "'").replace("“", '"').replace("”", '"')
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _excerpt_found(excerpt, page_text):
    excerpt = str(excerpt or "").strip()
    page_text = str(page_text or "").strip()

    if not excerpt or not page_text:
        return False

    # Support abbreviated excerpts using "..." or "…".
    parts = re.split(r"\.{3}|…", excerpt)

    page_n = _norm_text(page_text)
    page_words = page_n.split()

    valid_parts = []
    for part in parts:
        part_n = _norm_text(part)
        words = part_n.split()
        if len(words) >= 5:
            valid_parts.append(words)

    if not valid_parts:
        return False

    # Every meaningful excerpt segment must occur on the cited page
    # in the same order. Text between segments may be omitted with "...".
    position = 0

    for segment in valid_parts:
        found = False

        for i in range(position, len(page_words) - len(segment) + 1):
            if page_words[i:i + len(segment)] == segment:
                position = i + len(segment)
                found = True
                break

        if not found:
            return False

    return True


def _fetch(url):
    """Returns (status_code or None, page_text).

    Uses a reader fallback when the source blocks direct HTTP fetching.
    Strict evidence verification is still performed against the fetched text.
    """
    try:
        r = requests.get(
            url,
            timeout=8,
            headers=UA,
            allow_redirects=True,
        )

        content_type = r.headers.get("content-type", "")
        text = r.text[:400000] if (
            "text" in content_type or "html" in content_type
        ) else ""

        # Normal direct fetch succeeded.
        if r.status_code < 400 and text:
            return r.status_code, _page_text(text)

        # Some sites (e.g. Cloudflare-protected pages) block direct requests.
        # Use the Jina reader only as a retrieval fallback; evidence is still
        # validated against the returned page text by _excerpt_found().
        if r.status_code in (401, 403, 429):
            reader_url = f"https://r.jina.ai/{url}"
            rr = requests.get(
                reader_url,
                timeout=15,
                headers=UA,
                allow_redirects=True,
            )

            if rr.status_code == 200 and rr.text:
                return 200, _page_text(rr.text[:400000])

        return r.status_code, _page_text(text)

    except requests.exceptions.RequestException:
        return None, ""


def _time_found(claim, text):
    tokens = TIME_TOKEN.findall(claim)
    return all(re.search(rf"\b{h}\s*[:.]\s*{m}\b", text) for h, m in tokens)


def check_sources(blueprint, fetch=None):
    """Validate cited evidence before it enters the generation boundary.

    In strict evidence mode a claim must include an exact supporting excerpt from
    the cited page. A reachable URL alone is not treated as proof. Legacy/test
    blueprints without ``evidence_excerpt`` keep the previous URL/time behavior
    unless ``evidence_mode`` is explicitly ``strict``.
    """
    fetch = fetch or _fetch
    claims = blueprint.get("verified_claims")
    if not isinstance(claims, list):
        return blueprint
    strict = str(blueprint.get("evidence_mode", "legacy")).lower() == "strict"
    kept, dropped, cache = [], [], {}
    for item in claims:
        if not isinstance(item, dict):
            item = {"claim": str(item), "source_url": ""}
        url = (item.get("source_url") or "").split("#")[0]
        claim = item.get("claim", "")

        # Requester-provided facts are explicitly trusted by the user and do not
        # need a web citation. Keep them inside the evidence boundary.
        if str(url).lower() == "provided by requester":
            kept.append({**item, "evidence_verified": True,
                        "source_checked": False})
            continue

        if not url.startswith("http"):
            dropped.append({"claim": claim, "source_url": url,
                           "reason": "no usable source URL"})
            continue
        if url not in cache:
            cache[url] = fetch(url)
        status, text = cache[url]
        if status in (404, 410):
            dropped.append({"claim": claim, "source_url": url,
                           "reason": f"source URL does not exist (HTTP {status})"})
            continue
        if TIME_TOKEN.search(str(claim)) and not _time_found(str(claim), text):
            dropped.append({"claim": claim, "source_url": url,
                           "reason": "clock time not found on the cited page"})
            continue
        if strict:
            excerpt = str(item.get("evidence_excerpt", "")).strip()
            if not excerpt:
                dropped.append({"claim": claim, "source_url": url,
                               "reason": "no supporting evidence excerpt supplied"})
                continue
            if status is None or not text:
                dropped.append({"claim": claim, "source_url": url,
                               "reason": "cited page could not be fetched for evidence verification"})
                continue
            if not _excerpt_found(excerpt, text):
                dropped.append({"claim": claim, "source_url": url,
                               "reason": "supporting evidence excerpt not found on cited page"})
                continue
            kept.append({**item, "evidence_verified": True,
                        "source_checked": True})
            continue
        kept.append({**item, "source_checked": status is not None})
    blueprint["verified_claims"] = kept
    if dropped:
        blueprint["dropped_claims"] = dropped
        if isinstance(blueprint.get("claims_to_verify"), list):
            blueprint["claims_to_verify"] += [d["claim"] for d in dropped]
    return blueprint
