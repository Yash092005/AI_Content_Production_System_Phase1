"""Fetch a reference site politely and safely: robots.txt, size/time limits, manual redirects, same-site pages only."""
import re
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from app.reference.extract import extract_page
from app.reference.safety import UnsafeUrl, make_session, validate_url

USER_AGENT = "ContentCompilerBot/1.2 (writing-style analysis; honours robots.txt)"
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 4
PREFER = re.compile(r"about|story|blog|news|services?|faq|offer|why|how|learn|guide|press", re.I)
AVOID = re.compile(r"login|log-in|signin|sign-in|cart|checkout|account|admin|wp-|privacy|terms|cookie|unsubscribe|\.(pdf|jpe?g|png|gif|svg|zip|mp4|css|js|xml)(\?|$)", re.I)


class FetchError(RuntimeError):
    """A page could not be read. The message is written for the person who typed the URL."""


def _get(session, url, timeout, opts, headers=None):
    """GET with redirects followed by hand: every hop is validated again. Returns (final_url, response_text)."""
    for _ in range(MAX_REDIRECTS + 1):
        url = validate_url(url, **opts)
        try:
            resp = session.get(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml", **(headers or {})},
                               timeout=(min(5, timeout), timeout), allow_redirects=False, stream=True)
        except UnsafeUrl:
            raise
        except requests.exceptions.Timeout:
            raise FetchError("The website took too long to respond.")
        except requests.exceptions.SSLError:
            raise FetchError("The website's security certificate could not be verified.")
        except requests.exceptions.RequestException:
            raise FetchError("The website could not be reached.")
        try:
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                url = urljoin(url, resp.headers["Location"])
                continue
            if resp.status_code != 200:
                raise FetchError(f"The website answered with an error ({resp.status_code}).")
            ctype = resp.headers.get("Content-Type", "").lower()
            if "html" not in ctype and "text/plain" not in ctype:
                raise FetchError("That address is not a web page.")
            body, size = [], 0
            for chunk in resp.iter_content(chunk_size=16384):
                size += len(chunk)
                body.append(chunk)
                if size >= MAX_BYTES:
                    break                         # a huge page is cut here, not downloaded to the end
            raw = b"".join(body)[:MAX_BYTES]
            enc = resp.encoding or "utf-8"
            try:
                return url, raw.decode(enc, errors="replace")
            except LookupError:
                return url, raw.decode("utf-8", errors="replace")
        finally:
            resp.close()
    raise FetchError("The website redirected too many times.")


def _robots(session, url, timeout, opts):
    """RobotFileParser for the site, or None when robots.txt is missing or unreadable (the standard: then allow)."""
    parts = urlsplit(url)
    try:
        _, text = _get(session, f"{parts.scheme}://{parts.netloc}/robots.txt", timeout, opts)
    except (FetchError, UnsafeUrl):
        return None
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    return rp


def _same_site(a, b):
    ha, hb = (urlsplit(a).hostname or "").lower(), (urlsplit(b).hostname or "").lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h
    return strip(ha) == strip(hb)


def pick_more_pages(first, limit):
    """Up to `limit` other pages of the same site, preferring about/blog/services-style pages."""
    scored, seen = [], {first["url"].rstrip("/")}
    for link, text in first["links"]:
        clean = link.split("#")[0]
        key = clean.rstrip("/")
        if key in seen or not _same_site(first["url"], clean) or AVOID.search(clean):
            continue
        if urlsplit(clean).scheme not in ("http", "https"):
            continue
        seen.add(key)
        scored.append((0 if PREFER.search(clean + " " + text) else 1, len(clean), clean))
    scored.sort()
    return [c for _, _, c in scored[:limit]]


def fetch_site(url, max_pages=3, timeout=10, respect_robots=True, allowed_domains=(), blocked_domains=(),
               session=None, allow_private=False, resolver=None):
    """Read a site's pages. Returns (pages, warnings). Raises UnsafeUrl / FetchError when the FIRST page cannot be read.
    Later pages that fail are skipped with a warning."""
    opts = {"allowed_domains": allowed_domains, "blocked_domains": blocked_domains, "allow_private": allow_private}
    if resolver is not None:
        opts["resolver"] = resolver
    session = session or make_session(allow_private=allow_private)
    warnings, pages = [], []
    start = validate_url(url, **opts)
    rp = _robots(session, start, timeout, opts) if respect_robots else None
    if rp is not None and not rp.can_fetch(USER_AGENT, start):
        raise FetchError("The website's robots.txt asks automated tools not to read this page, so it was not studied.")
    final, html = _get(session, start, timeout, opts)
    first = extract_page(html, final)
    pages.append(first)
    for extra in pick_more_pages(first, max(0, int(max_pages) - 1)):
        if rp is not None and not rp.can_fetch(USER_AGENT, extra):
            warnings.append(f"Skipped {extra}: not allowed by robots.txt.")
            continue
        try:
            time.sleep(0.4)                     # be gentle with the site
            f2, h2 = _get(session, extra, timeout, opts)
            if _same_site(first["url"], f2):    # a redirect to another site is not part of this site
                pages.append(extract_page(h2, f2))
        except (FetchError, UnsafeUrl) as e:
            warnings.append(f"Skipped {extra}: {e}")
    return pages, warnings
