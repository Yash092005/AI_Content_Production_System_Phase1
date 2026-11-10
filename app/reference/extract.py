"""Turn raw HTML into the pieces that show how a site writes. Standard library only; tolerant of broken HTML."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

SKIP = {"script", "style", "noscript", "svg", "iframe", "template", "canvas", "head"}
BLOCK = {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "td", "th", "dd", "dt", "figcaption", "br"}
MAX_LIST = 60
MAX_ELEMENT_CHARS = 4000          # one giant element (a page cut mid-paragraph) cannot flood the sample
NOT_A_CTA = re.compile(r"^(sign|log)[\s-]?in$|^log[\s-]?out$|^login$|^cart$|^menu$|^search$|^skip to|^(my )?account$", re.I)


def _clean(t):
    return re.sub(r"\s+", " ", t or "").strip()


class _Page(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.title = ""
        self.description = ""
        self.lang = ""
        self.headings, self.paragraphs, self.list_items, self.ctas, self.links = [], [], [], [], []
        self._skip = 0
        self._stack = []          # (tag, buffer)
        self._in_title = False
        self._a_href = None
        self._in_nav = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = _clean(a.get("lang", ""))[:10]
        if tag == "title":
            self._in_title = True
        if tag == "meta" and (a.get("name") or "").lower() == "description":
            self.description = _clean(a.get("content", ""))[:300]
        if tag in SKIP and tag != "head":
            self._skip += 1
            return
        if self._skip:
            return
        if tag in ("nav", "header", "footer"):
            self._in_nav += 1
        if tag == "a":
            self._a_href = a.get("href")
        if tag in ("p", "li", "h1", "h2", "h3", "h4", "a", "button"):
            self._stack.append([tag, []])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIP and tag != "head":
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("nav", "header", "footer"):
            self._in_nav = max(0, self._in_nav - 1)
        if self._stack and self._stack[-1][0] == tag:
            self._finish()

    def _finish(self):
        """Close the innermost open element and file its text."""
        if True:
            tag, buf = self._stack.pop()
            text = _clean(" ".join(buf))[:MAX_ELEMENT_CHARS]
            if tag == "a":
                href, self._a_href = self._a_href, None
                if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                    self.links.append((urljoin(self.base, href), text))
                if text and 2 <= len(text.split()) <= 6 and not self._in_nav and not NOT_A_CTA.match(text):
                    self.ctas.append(text)
            elif tag == "button":
                if text and len(text.split()) <= 6 and not NOT_A_CTA.match(text):
                    self.ctas.append(text)
            elif tag in ("h1", "h2", "h3", "h4"):
                if text:
                    self.headings.append(text)
            elif tag == "li":
                if text and len(text.split()) >= 3 and not self._in_nav:
                    self.list_items.append(text)
            elif tag == "p":
                if len(text.split()) >= 5:
                    self.paragraphs.append(text)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        for entry in self._stack:
            entry[1].append(data)


def extract_page(html_text, url):
    """{url, title, description, lang, headings, paragraphs, list_items, ctas, links} for one page (lists are capped)."""
    p = _Page(url)
    try:
        p.feed(html_text or "")
        p.close()
    except Exception:  # broken markup must never stop the run: use what was read so far
        pass
    try:
        while p._stack:                    # a page cut off mid-element: keep the text read so far
            p._finish()
    except Exception:
        pass
    seen, ctas = set(), []
    for c in p.ctas:
        if c.lower() not in seen:
            seen.add(c.lower())
            ctas.append(c)
    return {"url": url, "title": _clean(p.title)[:200], "description": p.description, "lang": p.lang,
            "headings": p.headings[:MAX_LIST], "paragraphs": p.paragraphs[:MAX_LIST * 2],
            "list_items": p.list_items[:MAX_LIST], "ctas": ctas[:20], "links": p.links[:200]}


def page_text(page):
    """The readable text of a page in reading order (headings, then paragraphs, then list items)."""
    return "\n".join(page["headings"] + page["paragraphs"] + page["list_items"])
