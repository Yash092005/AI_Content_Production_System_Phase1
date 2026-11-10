"""The Reference Website layer: study a site, then check drafts against it. Never raises: a failure is reported, not thrown,
because this layer is optional and must never break a run."""
import hashlib
import json
import re
import time
from pathlib import Path

from app.config import runtime
from app.reference.extract import page_text
from app.reference.fetch import FetchError, fetch_site
from app.reference.profile import extract_json, normalize_profile, profile_prompt, style_block, style_block_compact
from app.reference.safety import UnsafeUrl, validate_url
from app.reference.stats import heuristic_profile, style_stats, words_of

SHINGLE_WORDS = 12          # this many words in a row copied from the site counts as copying
MAX_SHINGLE_TEXT = 60000


def _setting(path, default):
    return runtime.get(path, None, default)


def shingles(text, n=SHINGLE_WORDS):
    """Fingerprints of every run of n words. Only the fingerprints are stored, never the website's text."""
    ws = words_of(text)
    return {hashlib.blake2b(" ".join(ws[i:i + n]).encode(), digest_size=6).hexdigest() for i in range(max(0, len(ws) - n + 1))}


def copied_runs(draft, ref_shingles, n=SHINGLE_WORDS, limit=3):
    """Passages of the draft that repeat n or more consecutive words from the site: [(first words, words in the run)]."""
    ws = words_of(draft)
    hit = [hashlib.blake2b(" ".join(ws[i:i + n]).encode(), digest_size=6).hexdigest() in ref_shingles for i in range(max(0, len(ws) - n + 1))]
    runs, i = [], 0
    while i < len(hit):
        if hit[i]:
            j = i
            while j + 1 < len(hit) and hit[j + 1]:
                j += 1
            runs.append((" ".join(ws[i:i + 14]), j - i + n))
            i = j + 1
        else:
            i += 1
    return runs[:limit]


def _sample(pages, max_chars):
    """Text for the model: every page contributes, none can crowd the others out."""
    texts = [page_text(p) for p in pages]
    share = max(500, max_chars // max(1, len(texts)))
    out = []
    for p, t in zip(pages, texts):
        head = f"[{p['title'] or p['url']}]\n" if p["title"] else ""
        out.append(head + t[:share])
    return "\n\n".join(out)[:max_chars]


def _cache_path(cache_dir, norm_url):
    return Path(cache_dir) / f"reference_{hashlib.sha1(norm_url.encode()).hexdigest()[:20]}.json"


def analyse_reference(url, client=None, fetcher=None, cache_dir=None, cache_on=None, now=None, **fetch_kwargs):
    """Study `url`. Returns a dict that always has "url" and either "error" (a sentence for the user) or the full analysis."""
    now = now or time.time()
    out = {"url": str(url or "").strip(), "error": None, "warnings": []}
    if not runtime.get_bool("reference.enabled", None, True):
        out["error"] = "The reference website layer is switched off in the system configuration."
        return out
    opts = {"allowed_domains": _setting("reference.allowed_domains", []), "blocked_domains": _setting("reference.blocked_domains", [])}
    for k in ("allow_private", "resolver", "session"):
        if k in fetch_kwargs:
            opts[k] = fetch_kwargs.pop(k) if k != "session" else None
    try:
        norm = validate_url(url, **{k: v for k, v in opts.items() if k in ("allowed_domains", "blocked_domains", "allow_private", "resolver")})
    except UnsafeUrl as e:
        out["error"] = str(e)
        return out
    out["url"] = norm
    cache_on = runtime.get_bool("system.pipeline_cache", "PIPELINE_CACHE", True) if cache_on is None else cache_on
    cache_dir = cache_dir or runtime.get("system.cache_dir", "CACHE_DIR", ".cache")
    hours = float(_setting("reference.cache_hours", 24))
    cfile = _cache_path(cache_dir, norm)
    if cache_on and hours > 0 and cfile.exists():
        try:
            cached = json.loads(cfile.read_text(encoding="utf-8"))
            if now - cached.get("fetched_epoch", 0) < hours * 3600:
                cached["cached"] = True
                return cached
        except (OSError, ValueError):
            pass
    fetcher = fetcher or fetch_site
    try:
        pages, warns = fetcher(norm, max_pages=int(_setting("reference.max_pages", 3)), timeout=int(_setting("reference.timeout_seconds", 10)),
                               respect_robots=bool(_setting("reference.respect_robots", True)),
                               allowed_domains=opts["allowed_domains"], blocked_domains=opts["blocked_domains"], **fetch_kwargs)
    except (UnsafeUrl, FetchError) as e:
        out["error"] = str(e)
        return out
    except Exception as e:  # an unexpected failure is still only a warning for an optional layer
        out["error"] = f"The website could not be studied ({type(e).__name__})."
        return out
    text = "\n\n".join(page_text(p) for p in pages)
    if len(words_of(text)) < 80:
        out["error"] = "The website has too little readable text to learn its style from (it may load its content with scripts)."
        return out
    heads = [h for p in pages for h in p["headings"]]
    ctas = [c for p in pages for c in p["ctas"]]
    paras = [x for p in pages for x in p["paragraphs"]]
    stats = style_stats(text, heads, ctas, paras, pages[0]["lang"])
    host = (re.sub(r"^https?://", "", norm).split("/")[0]).lower()
    profile, source, dropped = None, "heuristic", []
    if client is not None and getattr(client, "live", False):
        try:
            raw = extract_json(client.generate(profile_prompt(host, stats, _sample(pages, int(_setting("reference.max_chars", 8000)))),
                                               system=None, stage="reference"))
            if raw is not None:
                profile, dropped = normalize_profile(raw, stats)
                source = "ai"
            else:
                warns.append("The model's style summary could not be read, so a measured profile was used.")
        except Exception as e:
            warns.append(f"The model could not summarise the style ({str(e)[:80]}), so a measured profile was used.")
    if profile is None:
        profile, _ = normalize_profile(heuristic_profile(stats), stats)
    if dropped:
        warns.append(f"{len(dropped)} instruction-like text(s) found in the website were ignored.")
    out.update({"host": host, "pages": [{"url": p["url"], "title": p["title"]} for p in pages], "stats": stats, "profile": profile,
                "source": source, "warnings": list(warns), "cached": False, "fetched_epoch": now,
                "fetched_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
                "shingles": sorted(shingles(text[:MAX_SHINGLE_TEXT]))})
    if cache_on and hours > 0:
        try:
            cfile.parent.mkdir(parents=True, exist_ok=True)
            cfile.write_text(json.dumps(out), encoding="utf-8")
        except OSError:
            pass
    return out


def usable(ref):
    return bool(ref) and not ref.get("error") and "profile" in ref


def public_view(ref):
    """The analysis without the fingerprints, for the screen and the result."""
    return {k: v for k, v in (ref or {}).items() if k not in ("shingles", "fetched_epoch")}


def copy_issues(draft, ref):
    """FAIL-level issues: the draft repeats a run of words from the reference website."""
    if not usable(ref):
        return []
    return [f"Copied wording from the reference website ({r[1]} words in a row, starting “{r[0]}…”). "
            "Rewrite it in your own words: the site is a style guide, not text to reuse." for r in copied_runs(draft, set(ref.get("shingles", [])))]


def style_warnings(draft, ref):
    """Warnings (never a FAIL): the draft drifts away from the site's measured voice."""
    if not usable(ref):
        return []
    p, st = ref["profile"], ref["stats"]
    d = style_stats(re.sub(r"<!--.*?-->", "", draft or ""), [], [], [])
    out, host = [], ref["host"]
    if st["avg_sentence_words"] and d["sentences"] >= 5:
        ratio = d["avg_sentence_words"] / st["avg_sentence_words"]
        if ratio > 1.6 or ratio < 0.6:
            out.append(f"Sentences average {d['avg_sentence_words']:.0f} words, while {host} averages {st['avg_sentence_words']:.0f}.")
    if p["point_of_view"] in ("you", "we_you") and d["words"] >= 150 and d["you_per_100"] < 0.3:
        out.append(f"{host} speaks to the reader as “you”, but the draft rarely does.")
    if p["point_of_view"] == "third_person" and d["you_per_100"] > 2.0:
        out.append(f"{host} uses a neutral third-person voice, but the draft addresses the reader as “you” a lot.")
    low = (draft or "").lower()
    bad = [t for t in p["terms_to_avoid"] if re.search(r"\b" + re.escape(t.lower()) + r"\b", low)]
    if bad:
        out.append(f"The draft uses words {host} avoids: {', '.join(bad[:5])}.")
    terms = p["signature_terms"][:8]
    if len(terms) >= 3 and d["words"] >= 200 and not any(t.lower() in low for t in terms):
        out.append(f"None of {host}'s typical terms appear in the draft ({', '.join(terms[:4])}).")
    return out
