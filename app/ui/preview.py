"""Turns a markdown draft into a standalone, safe, web-style article page."""
import html
import re

from app.strategy.length import count_words

try:
    import markdown as _md
except ImportError:  # pragma: no cover - the app still works, with a simpler renderer
    _md = None

_LEADING = re.compile(
    r"^\s*(?:please\s+)?(?:create|write|generate|make|draft)\s+(?:me\s+)?(?:an?\s+)?"
    r"(?:(?:website|web|seo)\s+)?(?:blog\s+post|blog|article|page|post)?\s*(?:on|about|for)?\s*",
    re.I,
)


def guess_title(request, fallback="Untitled draft"):
    t = _LEADING.sub("", str(request or "")).strip().rstrip(".")
    if not t:
        return fallback
    return t[:1].upper() + t[1:]


def sanitize(fragment):
    """Model output is untrusted: drop active content. A CSP in the page is the second layer."""
    f = re.sub(r"(?is)<(script|style|iframe|object|embed|link|meta|form)\b.*?(?:</\1>|/>|>)", "", fragment)
    f = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", f)
    f = re.sub(r"(?i)(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", r"\1=\2#\2", f)
    f = re.sub(r"(?i)\]\(\s*javascript:[^)]+\)", "](#)", f)
    return f


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "section"


def _fallback_render(text):
    out, toc, list_type = [], [], None
    for line in str(text).splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        ul = re.match(r"^\s*[-*+]\s+(.*)$", line)
        ol = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        current = "ul" if ul else ("ol" if ol else None)
        if list_type and current != list_type:
            out.append(f"</{list_type}>")
            list_type = None
        if m:
            lvl, name = len(m.group(1)), m.group(2).strip()
            sid = _slug(name)
            toc.append({"level": lvl, "id": sid, "name": name})
            out.append(f'<h{lvl} id="{sid}">{html.escape(name)}</h{lvl}>')
        elif current:
            if not list_type:
                out.append(f"<{current}>")
                list_type = current
            value = ul.group(1) if ul else ol.group(1)
            out.append(f"<li>{html.escape(value)}</li>")
        elif line.strip():
            out.append(f"<p>{html.escape(line.strip())}</p>")
    if list_type:
        out.append(f"</{list_type}>")
    return "\n".join(out), toc


_LIST_LINE = re.compile(r"^\s*(?:[*+-]|\d+[.)])\s+\S")


def fix_lists(text):
    """Models often put a bullet list right after a sentence with no blank line. Markdown then folds the
    bullets into the paragraph (literal '*' characters). Insert the missing blank line before a list starts."""
    out, prev_blank, prev_list, fenced = [], True, False, False
    for line in str(text or "").splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
        is_list = (not fenced) and bool(_LIST_LINE.match(line))
        if is_list and not prev_blank and not prev_list:
            out.append("")
        out.append(line)
        prev_blank, prev_list = not line.strip(), is_list
    return "\n".join(out)


def render_markdown(text):
    """Return (body_html, toc) where toc = [{level, id, name}, ...] for headings up to H2."""
    safe_text = sanitize(str(text or ""))
    if _md is None:
        safe_text = fix_lists(safe_text)
        body, toc = _fallback_render(safe_text)
    else:
        m = _md.Markdown(extensions=["extra", "toc", "sane_lists"])
        body = m.convert(fix_lists(safe_text))
        toc = []

        def walk(tokens):
            for t in tokens:
                toc.append({"level": t["level"], "id": t["id"], "name": t["name"]})
                walk(t.get("children", []))

        walk(m.toc_tokens)
    # Include H3 topic sections as well as H1/H2 so the preview TOC reflects the actual article structure.
    toc = [t for t in toc if t["level"] <= 3]
    return sanitize(body), toc


PAGE_CSS = """
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:#f6f7fb;color:#1c2033;font:17px/1.75 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.bar{display:flex;align-items:center;gap:10px;padding:10px 18px;background:#e9ebf3;border-bottom:1px solid #d9dcea}
.dots{display:flex;gap:6px}.dots i{width:11px;height:11px;border-radius:50%;background:#c6cadb;display:block}
.dots i:nth-child(1){background:#ff6058}.dots i:nth-child(2){background:#ffbd2e}.dots i:nth-child(3){background:#28c941}
.url{flex:1;max-width:560px;margin:0 auto;background:#fff;border:1px solid #d9dcea;border-radius:999px;padding:5px 14px;font-size:13px;color:#667;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nav{display:flex;justify-content:space-between;align-items:center;padding:16px 6vw;background:#fff;border-bottom:1px solid #eceef6}
.logo{font-weight:800;letter-spacing:-.03em;font-size:19px;color:#5b21b6}.nav span{font-size:14px;color:#6b7191;margin-left:22px}
.hero{padding:64px 6vw 56px;color:#fff;background:radial-gradient(circle at 85% 10%,rgba(6,182,212,.55),transparent 40%),linear-gradient(135deg,#5b21b6,#2e1065 70%)}
.hero .tag{display:inline-block;font-size:12px;font-weight:700;letter-spacing:.1em;padding:5px 12px;border-radius:99px;background:rgba(255,255,255,.16);margin-bottom:16px}
.hero h1{margin:0;font-size:clamp(30px,5vw,52px);line-height:1.1;letter-spacing:-.04em;font-weight:800;max-width:900px}
.hero p{margin:14px 0 0;opacity:.8;font-size:15px}
.wrap{display:flex;gap:44px;max-width:1120px;margin:0 auto;padding:44px 6vw 70px}
.toc{width:230px;flex:none;position:sticky;top:20px;align-self:flex-start;background:#fff;border:1px solid #eceef6;border-radius:16px;padding:18px}
.toc b{display:block;font-size:12px;letter-spacing:.1em;color:#7a7fa0;margin-bottom:10px}
.toc a{display:block;padding:6px 0;font-size:14px;line-height:1.35;color:#3b3f5c;text-decoration:none;border-bottom:1px solid #f1f2f8}
.toc a:hover{color:#6d28d9}.toc a[data-level="3"]{padding-left:12px;font-size:13px;color:#5f6480}
article{flex:1;min-width:0;background:#fff;border:1px solid #eceef6;border-radius:20px;padding:40px 46px;box-shadow:0 10px 40px rgba(30,30,80,.05)}
article h1,article h2{font-size:28px;line-height:1.25;letter-spacing:-.025em;margin:2.1em 0 .5em;font-weight:800;color:#14172b}
article>h1:first-child,article>h2:first-child{margin-top:0}
article h3{font-size:21px;margin:1.6em 0 .4em;font-weight:700}
article p{margin:0 0 1.1em;color:#2d3250}article li{margin:.35em 0}
article a{color:#6d28d9}article blockquote{margin:1.4em 0;padding:.6em 1.2em;border-left:4px solid #8b5cf6;background:#f5f1ff;border-radius:0 12px 12px 0}
article table{border-collapse:collapse;width:100%;margin:1.2em 0;font-size:15px}article th,article td{border:1px solid #e4e6f1;padding:9px 12px;text-align:left}article th{background:#f5f6fb}
footer{text-align:center;color:#8a8fae;font-size:13px;padding:0 0 36px}
@media(max-width:820px){.toc{display:none}.wrap{padding:24px 14px}article{padding:24px 20px}}
"""


def build_page(title, markdown_text, reading_wpm=200):
    body, toc = render_markdown(markdown_text)
    words = count_words(markdown_text)
    mins = max(1, round(words / reading_wpm))
    toc_html = "".join(f'<a data-level="{int(t["level"])}" href="#{html.escape(t["id"])}">{html.escape(t["name"])}</a>' for t in toc)
    toc_block = f'<aside class="toc"><b>ON THIS PAGE</b>{toc_html}</aside>' if toc else ""
    t = html.escape(title)
    slug = _slug(title)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src https: data:; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com">
<title>{t}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap">
<style>{PAGE_CSS}</style></head><body>
<div class="bar"><div class="dots"><i></i><i></i><i></i></div><div class="url">https://yoursite.com/blog/{slug}</div></div>
<div class="nav"><div class="logo">YourSite</div><div><span>Festivals</span><span>Guides</span><span>About</span></div></div>
<header class="hero"><div class="tag">DRAFT PREVIEW</div><h1>{t}</h1><p>{mins} min read &middot; {words} words</p></header>
<div class="wrap">{toc_block}<article>{body}</article></div>
<footer>Preview generated by Content Compiler &middot; not published</footer>
</body></html>"""
