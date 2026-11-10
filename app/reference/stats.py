"""Measure how a site writes, in plain code (no AI), and derive a fallback style profile from the numbers."""
import re
from collections import Counter

WORD = re.compile(r"[A-Za-z\u00C0-\u024F\u0900-\u097F']+")
SENT_SPLIT = re.compile(r"(?<=[.!?\u0964])\s+")
STOP = set("""the and for that with this from are was were will have has had not but you your yours our ours they their them its
into about over under than then them there these those what when where which while who whom whose why how can could would should
may might must shall also just very more most such each other some any all one two three out off per via get got make made""".split())
YOU = {"you", "your", "yours", "yourself", "you're", "you'll"}
WE = {"we", "our", "ours", "us", "we're", "we'll", "ourselves"}


def words_of(text):
    return [w.lower().strip("'") for w in WORD.findall(text or "")]


def detect_language(text, lang_attr=""):
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if "\u0900" <= c <= "\u097f") / len(letters) > 0.3:
        return "Hindi"
    return {"hi": "Hindi", "en": "English"}.get((lang_attr or "").lower()[:2], "English")


def style_stats(text, headings=(), ctas=(), paragraphs=(), lang_attr=""):
    ws = words_of(text)
    n = max(1, len(ws))
    sents = [s for s in SENT_SPLIT.split(text or "") if len(words_of(s)) >= 3]
    ns = max(1, len(sents))
    content = [w for w in ws if len(w) >= 4 and w not in STOP]
    top_terms = [w for w, c in Counter(content).most_common(14) if c >= 2][:12]
    bigrams = Counter(f"{a} {b}" for a, b in zip(ws, ws[1:]) if a not in STOP and b not in STOP and len(a) > 2 and len(b) > 2)
    top_bigrams = [b for b, c in bigrams.most_common(10) if c >= 2][:8]
    q_heads = sum(1 for h in headings if h.strip().endswith("?"))
    return {
        "words": len(ws), "sentences": len(sents),
        "avg_sentence_words": round(len(ws) / ns, 1),
        "avg_paragraph_words": round(sum(len(words_of(p)) for p in paragraphs) / max(1, len(paragraphs)), 1) if paragraphs else 0,
        "you_per_100": round(100 * sum(1 for w in ws if w in YOU) / n, 2),
        "we_per_100": round(100 * sum(1 for w in ws if w in WE) / n, 2),
        "exclaim_per_100_sentences": round(100 * (text or "").count("!") / ns, 1),
        "question_per_100_sentences": round(100 * (text or "").count("?") / ns, 1),
        "contractions_per_100": round(100 * len(re.findall(r"\b\w+'(?:t|re|ll|ve|d|m)\b", text or "")) / n, 2),
        "headings": len(headings),
        "avg_heading_words": round(sum(len(words_of(h)) for h in headings) / max(1, len(headings)), 1) if headings else 0,
        "question_headings": q_heads,
        "top_terms": top_terms, "top_phrases": top_bigrams,
        "cta_examples": list(ctas)[:6],
        "language": detect_language(text, lang_attr),
    }


def length_label(avg):
    return "short" if avg <= 14 else "medium" if avg <= 22 else "long"


def heuristic_profile(stats):
    """A usable profile from the numbers alone. Used with no API key, or when the model's answer cannot be used."""
    you, we = stats["you_per_100"], stats["we_per_100"]
    pov = "we_you" if you >= 1.0 and we >= 0.5 else "you" if you >= 1.0 else "we" if we >= 0.8 else "third_person"
    casual = stats["contractions_per_100"] > 1.0 or stats["exclaim_per_100_sentences"] > 15
    formal = stats["avg_sentence_words"] > 22 and you < 1.0 and not casual
    formality = "casual" if casual else "formal" if formal else "neutral"
    tone = []
    if stats["exclaim_per_100_sentences"] > 15:
        tone.append("enthusiastic")
    if you >= 1.0:
        tone.append("direct and personal")
    if we >= 0.8:
        tone.append("first-person (brand voice)")
    if formality == "formal":
        tone.append("formal")
    if not tone:
        tone.append("informative")
    h = stats["avg_heading_words"]
    heading_style = (f"Headings of about {h:.0f} words" + (", often phrased as questions" if stats["headings"] and
                     stats["question_headings"] / stats["headings"] > 0.3 else "")) if stats["headings"] else ""
    ctas = stats["cta_examples"]
    return {
        "tone": tone[:4], "formality": formality, "point_of_view": pov, "sentence_length": length_label(stats["avg_sentence_words"]),
        "language": stats["language"],
        "signature_terms": (stats["top_phrases"][:5] + [t for t in stats["top_terms"] if t not in " ".join(stats["top_phrases"][:5])])[:10],
        "terms_to_avoid": [],
        "heading_style": heading_style,
        "cta_style": "Short action phrases" if ctas else "", "cta_examples": ctas[:5],
        "structure": f"About {stats['avg_paragraph_words']:.0f} words per paragraph" if stats["avg_paragraph_words"] else "",
        "do": [f"Keep sentences around {stats['avg_sentence_words']:.0f} words on average"]
              + (["Address the reader directly as 'you'"] if pov in ("you", "we_you") else [])
              + (["Speak as 'we' for the brand"] if pov in ("we", "we_you") else []),
        "dont": (["Avoid exclamation marks"] if stats["exclaim_per_100_sentences"] < 3 else []),
        "content_rules": [],
    }
