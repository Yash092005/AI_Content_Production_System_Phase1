"""Small HTML builders and result-state logic for the Streamlit UI (no Streamlit imports: easy to test)."""
import html

from app.strategy import FRAMEWORKS, LABELS
from app.strategy.length import count_words

STEPS = [
    ("strategy", "Strategy", "Pick framework"),
    ("rules", "Rules", "Load constraints"),
    ("research", "Research", "Verify claims"),
    ("generate", "Generate", "Build content"),
    ("qa", "QA", "Validate output"),
]
STAGE_TO_STEP = {
    "strategy": "strategy", "reference": "strategy", "rules": "rules", "research": "research", "research_blueprint": "research", "blueprint": "research",
    "generate": "generate", "qa": "qa", "revise": "qa",
}
ACTIVE_TEXT = {
    "strategy": "Choosing framework…", "reference": "Studying the website…", "rules": "Loading rules…", "research": "Researching & verifying…", "generate": "Writing draft…",
    "qa": "Auditing draft…", "revise": "Revising to fix QA…",
}


def new_states():
    return {k: {"state": "idle", "text": desc} for k, _, desc in STEPS}


def apply_stage(states, stage, state):
    """Update the stepper state dict from an orchestrator on_stage(stage, state) event."""
    key = STAGE_TO_STEP.get(stage)
    if not key:
        return states
    if state == "start":
        states[key] = {"state": "active", "text": ACTIVE_TEXT.get(stage, "Working…")}
    elif state == "done":
        # QA stays "active" until the pipeline finishes: a revise round may follow.
        keep = stage == "qa"
        states[key] = {"state": "active" if keep else "done", "text": "Verdict received…" if keep else "Done"}
    elif state == "error":
        states[key] = {"state": "error", "text": "Failed"}
    return states


def finish_states(states, ok=True):
    for v in states.values():
        if v["state"] == "active":
            v.update(state="done" if ok else "error", text="Done" if ok else "Failed")
    return states


def stepper_html(states):
    parts = []
    for i, (key, title, _) in enumerate(STEPS, 1):
        s = states[key]
        icon = {"done": "✓", "error": "✕"}.get(s["state"], str(i))
        parts.append(
            f'<div class="ps {s["state"]}"><div class="dot">{icon}</div>'
            f'<div><b>{title}</b><span>{html.escape(s["text"])}</span></div></div>'
        )
    return '<div class="pipe">' + "".join(parts) + "</div>"


def result_state(result):
    """Single source of truth for the banner: it can never say 'passed' when QA said FAIL."""
    status = result.get("status", "UNKNOWN")
    qa = result.get("qa") or {}
    qa_status = qa.get("status", "NOT RUN")
    if status == "NEEDS_INPUT":
        return "warn", "⚠️", "More information needed", "Answer the questions below, then run again."
    if status == "BLUEPRINT_FAILED":
        return "fail", "🧩", "Research could not be parsed", "Nothing was written. Re-running reuses finished steps."
    if status == "PAUSED":
        return "warn", "⏸️", "Paused: providers at capacity", "Finished steps are kept. Press Run again to continue from here."
    if status == "PARTIAL":
        return "fail", "⛔", "Pipeline stopped early", friendly_error(qa.get("error"))["summary"]
    if status == "MOCK_RUN":
        return "warn", "🧪", "Mock run", "Placeholder output only. Add an API key for live content."
    if qa_status == "PASS" and status == "COMPLETE":
        return "pass", "✅", "Ready to ship", "Draft passed deterministic checks and the QA audit."
    n = len(qa.get("issues") or [])
    return "fail", "🔍", "Needs review", f"QA flagged {n} issue{'s' if n != 1 else ''}. See the QA tab for exactly what to fix."


def verdict_html(result):
    kind, icon, title, sub = result_state(result)
    return (f'<div class="verdict {kind}"><div class="ic">{icon}</div>'
            f'<div><h2>{html.escape(title)}</h2><p>{html.escape(str(sub))}</p></div></div>')


def stats_html(result):
    qa = result.get("qa") or {}
    rules = result.get("rules") or {}
    loaded = sum(1 for v in rules.values() if v == "LOADED")
    words = count_words(result.get("draft"))
    target = (result.get("length") or {}).get("target")
    items = [
        ("QA", qa.get("status", "—")), ("Revisions", result.get("revisions", 0)),
        ("Rules", f"{loaded}/{len(rules)}" if rules else "0"),
        ("Fallbacks", len(result.get("fallbacks") or [])), ("Words", f"{words} / ~{target}" if target else words),
        ("Read time", f"{max(1, round(words / 200))} min" if words else "—"),
    ]
    return '<div class="stats">' + "".join(
        f'<div class="stat"><small>{k}</small><b>{html.escape(str(v))}</b></div>' for k, v in items) + "</div>"


def issues_html(issues):
    return "".join(
        f'<div class="issue"><div class="no">{i}</div><div>{html.escape(str(x))}</div></div>'
        for i, x in enumerate(issues, 1))


def checks_html(checks):
    return "".join(
        f'<span class="check {"y" if ok else "n"}">{"✓" if ok else "✕"} {html.escape(k.replace("_", " "))}</span>'
        for k, ok in (checks or {}).items())


_SOURCE_LABEL = {"ai": "🤖 Predicted by AI", "heuristic": "🔤 Predicted from keywords", "user": "✏️ Set by you"}


def framework_badge_html(strategy):
    """The selected framework + WHY, shown on the output screen and in the strategy panel."""
    if not strategy or strategy.get("framework") not in FRAMEWORKS:
        return ""
    fw = strategy["framework"]
    src = "Chosen by you" if strategy.get("framework_source") == "user" else "Chosen by the router"
    return (f'<div class="fwbar"><div class="fwbadge"><small>FRAMEWORK</small><b>{html.escape(fw)}</b></div>'
            f'<div><strong>{html.escape(FRAMEWORKS[fw]["expansion"])}</strong>'
            f'<span class="fwsrc">{src}</span>'
            f'<p>{html.escape(str(strategy.get("framework_reason", "")))}</p></div></div>')


def strategy_table_html(strategy):
    if not strategy:
        return ""
    rows = [(LABELS[k], strategy.get(k, "")) for k in
            ("content_type", "market_type", "audience", "relationship_stage", "user_intent", "jtbd", "objective", "framework")]
    rows.append(("Selection reason", strategy.get("framework_reason", "")))
    body = "".join(f'<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>' for k, v in rows)
    src = _SOURCE_LABEL.get(strategy.get("source"), "")
    return f'<div class="muted" style="margin-bottom:.5rem">{src}</div><table class="stbl">{body}</table>'


def warnings_html(warnings):
    return "".join(f'<div class="warn-box">⚠️ {html.escape(str(w))}</div>' for w in (warnings or []))


def friendly_error(msg):
    """Turn a raw provider error into a short summary plus what to do about it."""
    m = str(msg or "")
    low = m.lower()
    busy = "503" in m or "unavailable" in low or "high demand" in low
    budget = "tokens/minute" in low or "configured groq limit" in low
    quota = "429" in m or "quota" in low or "rate limit" in low
    tips = []
    if busy and budget:
        summary = "Gemini is busy right now, and the backup (Groq) could not take a request this large."
    elif busy:
        summary = "Gemini is busy right now. This is usually temporary."
    elif budget:
        summary = "The request is larger than your Groq per-minute token budget."
    elif quota:
        summary = "A provider rate limit or quota was reached."
    else:
        summary = (m[:200] + "…") if len(m) > 200 else (m or "A pipeline step failed.")
    if busy or quota:
        tips.append("Wait a minute and click Run again. Finished steps such as research are reused, so only the failed step repeats.")
    if busy:
        tips.append("Set GEMINI_FALLBACK_MODEL in .env as a second Gemini model (run scripts/list_models.py to see your models).")
    if budget:
        tips.append("Choose a shorter content length. Groq's backup budget fits Short or Standard, not In-depth.")
        tips.append("Or raise GROQ_TPM in .env, but only if your Groq plan allows more than 8000 tokens per minute.")
    return {"summary": summary, "tips": tips, "raw": m}


def error_help_html(msg):
    e = friendly_error(msg)
    if not e["tips"]:
        return ""
    items = "".join(f"<li>{html.escape(t)}</li>" for t in e["tips"])
    return f'<div class="warn-box"><b>What to do</b><ul style="margin:.4rem 0 0 1.1rem">{items}</ul></div>'


# ---------------------------------------------------------------------------
# Page shell: header, hero, how-it-works, empty state, FAQ, footer
# ---------------------------------------------------------------------------
from app.ui.brand import LOGO_SVG  # noqa: E402
from app.version import __version__  # noqa: E402


def anchor(name):
    return f'<span id="{html.escape(name)}" class="anchor"></span>'


def status_pill_html(status):
    state = status.get("state", "demo")
    return (f'<span class="spill {state}" title="{html.escape(status.get("detail", ""))}">'
            f'<i></i>{html.escape(status.get("label", ""))}</span>')


def config_pill_html(version):
    if not version:
        return ""
    return f'<span class="spill live" title="The main page runs on this configuration version"><i></i>Config v{int(version)}</span>'


def nav_html(status, config_version=None):
    return (f'<header class="topnav"><a class="brand" href="#top">{LOGO_SVG}<span>Content Compiler</span></a>'
            '<nav class="navlinks"><a href="#studio">Studio</a><a href="#how">How it works</a><a href="#faq">FAQ</a></nav>'
            f'<span style="display:flex;gap:.5rem;align-items:center">{config_pill_html(config_version)}'
            f'{status_pill_html(status)}</span></header>')


def hero_html():
    return """
<section class="hero2">
  <div class="hero-copy">
    <div class="eyebrow">AI CONTENT PRODUCTION SYSTEM</div>
    <h1>Write content you can<br>actually stand behind.</h1>
    <p>Turn a brief or an existing page into publish-ready content. Every claim is checked against verified research,
    and automated QA decides what ships.</p>
    <div class="cta-row"><a class="btn-primary" href="#studio">Start writing →</a><a class="btn-ghost" href="#how">How it works</a></div>
    <div class="trust"><div><b>4</b><span>pipeline stages</span></div><div><b>100%</b><span>claims checked</span></div>
    <div><b>PASS / FAIL</b><span>decided by QA</span></div></div>
  </div>
  <div class="hero-card" aria-label="Sample result">
    <div class="hc-top"><span class="hc-tag">SAMPLE RESULT</span><span class="hc-dots"><i></i><i></i><i></i></span></div>
    <div class="hc-title">Navratri 2026 in India</div>
    <div class="hc-sub">Blog · BLUF framework · 912 words</div>
    <div class="hc-ok"><span>✅</span><div><b>Ready to ship</b><small>Passed deterministic checks and the QA audit</small></div></div>
    <div class="hc-rows"><div><small>Claims</small><b>18</b></div><div><small>Revisions</small><b>0</b></div><div><small>QA</small><b>PASS</b></div></div>
    <div class="hc-bars"><i style="width:92%"></i><i style="width:78%"></i><i style="width:85%"></i><i style="width:60%"></i></div>
  </div>
</section>"""


HOW_STEPS = [
    ("01", "Strategy", "The AI predicts audience, intent and objective. A rules table picks AIDA, PAS, BAB or BLUF and tells you why. You can edit everything."),
    ("02", "Evidence", "Research gathers claims and checks cited sources. Anything that cannot be verified is blocked from the draft."),
    ("03", "Write", "The writer uses only verified claims, follows the chosen framework and aims for the length you pick."),
    ("04", "QA", "Code checks and an AI audit decide PASS or FAIL. Failures list exactly what to fix, and drafts are revised automatically."),
]


def how_it_works_html():
    cards = "".join(f'<div class="how-card"><span class="how-n">{n}</span><h4>{html.escape(t)}</h4><p>{html.escape(d)}</p></div>'
                    for n, t, d in HOW_STEPS)
    return (f'{anchor("how")}<div class="sec-head"><div class="eyebrow">HOW IT WORKS</div>'
            '<h2>From brief to checked draft in four steps</h2>'
            '<p>Strategy decides how to write. Evidence decides what can be said. QA decides whether it ships.</p></div>'
            f'<div class="how-grid">{cards}</div>')


def empty_state_html():
    tiles = [("🌐", "Web preview", "Your draft as a real article page, ready to review."),
             ("📝", "Clean draft", "Markdown with no hidden markers. Copy it in one click."),
             ("🛡️", "QA report", "Every issue listed. Nothing is hidden behind a green tick.")]
    items = "".join(f'<div class="empty-tile"><span>{i}</span><b>{html.escape(t)}</b><small>{html.escape(d)}</small></div>'
                    for i, t, d in tiles)
    return ('<div class="empty"><div class="empty-ic">✨</div><h3>Your content will appear here</h3>'
            '<p>Describe what you need above and run the pipeline. Here is what you will get:</p>'
            f'<div class="empty-grid">{items}</div></div>')


FAQS = [
    ("Why did QA fail my draft?",
     "QA fails a draft that contains a claim outside the verified research, a banned phrase, a mandatory removal, an unverified "
     "service promotion, or broken framework stages. The QA report lists every issue. Add trusted facts under Advanced inputs "
     "and run again with Fresh run."),
    ("Why is my content shorter than I expected?",
     "Length comes from verified facts, never padding. Choose Standard or In-depth and add facts under Advanced inputs. If research "
     "finds little verified material, the draft stays short and QA shows a warning instead of inventing facts."),
    ("What does Fresh run do?",
     "Model answers are cached to save API quota, so repeating the same request returns the same result. Fresh run skips that cache "
     "and generates again."),
    ("Which AI providers does it use?",
     "Gemini first, with Groq as an automatic backup when both keys are set. With no keys the app runs in a demo mode that returns "
     "placeholder text."),
    ("Is my data stored?",
     "There is no database. Model responses are cached as files in the server's .cache folder to save quota, and your requests are "
     "sent to Gemini and Groq. Do not paste passwords or private customer data."),
    ("Can I publish the output directly?",
     "Treat it as a draft for human review. The web preview shows layout only and publishes nothing. Download the HTML page or the "
     "Markdown when you are ready."),
]


def footer_html(status):
    return f"""
<div class="foot" role="contentinfo">
  <div class="foot-grid">
    <div><a class="brand" href="#top">{LOGO_SVG}<span>Content Compiler</span></a>
      <p>Evidence-first content production. Strategy, research, writing and QA in one pipeline.</p></div>
    <div><h5>Pipeline</h5><span>Strategy</span><span>Evidence</span><span>Write</span><span>QA</span></div>
    <div><h5>Good to know</h5><span>Requests are processed by Gemini and Groq.</span><span>Responses are cached on the server.</span>
      <span>Review every draft before publishing.</span></div>
  </div>
  <div class="foot-bar"><span>© 2026 Content Compiler · v{__version__}</span>{status_pill_html(status)}</div>
</div>"""


def login_html():
    return (f'<div class="login-head">{LOGO_SVG}<h2>Content Compiler</h2>'
            '<p>This workspace is protected. Enter the access password to continue.</p></div>')


def section_head_html(n, title, hint=""):
    h = f"<small>{html.escape(hint)}</small>" if hint else ""
    return f'<div class="sec"><span class="n">{html.escape(str(n))}</span><h3>{html.escape(title)} {h}</h3></div>'


def _chips(items, cls="chip"):
    return "".join(f'<span class="{cls}">{html.escape(str(x))}</span>' for x in items or [])


_POV = {"we_you": "We + you", "you": "You (the reader)", "we": "We (the brand)", "third_person": "Third person", "mixed": "Mixed"}


def reference_html(ref):
    """What the system learned from the reference website. Every value is escaped: it came from a website."""
    if not ref:
        return ""
    if ref.get("error"):
        return f'<div class="warn-box">⚠️ {html.escape(str(ref["error"]))}</div>'
    p, st = ref["profile"], ref["stats"]
    src = "Summarised by the AI" if ref.get("source") == "ai" else "Measured from the text (no AI summary)"
    cached = " · remembered from an earlier run" if ref.get("cached") else ""
    pages = "".join(f'<li>{html.escape(pg.get("title") or pg["url"])}</li>' for pg in ref.get("pages", []))
    rules = "".join(f"<li>{html.escape(r)}</li>" for r in p.get("content_rules", []) + p.get("do", []))
    avoid = f'<div class="refrow"><b>Avoids</b><span>{_chips(p.get("terms_to_avoid"), "chip warn")}</span></div>' if p.get("terms_to_avoid") else ""
    notes = "".join(f'<div class="muted">ℹ️ {html.escape(w)}</div>' for w in ref.get("warnings", []))
    return (
        f'<div class="refcard"><div class="refhead"><b>{html.escape(ref.get("host", ""))}</b>'
        f'<span class="muted">{src}{cached} · studied {html.escape(str(ref.get("fetched_at", "")))}</span></div>'
        f'<div class="refrow"><b>Tone</b><span>{_chips(p.get("tone"))}</span></div>'
        f'<div class="refrow"><b>Voice</b><span>{_chips([p.get("formality", "").title(), _POV.get(p.get("point_of_view"), ""), p.get("language", "")])}</span></div>'
        f'<div class="refrow"><b>Sentences</b><span>{html.escape(p.get("sentence_length", ""))}, about {st["avg_sentence_words"]:.0f} words</span></div>'
        f'<div class="refrow"><b>Uses</b><span>{_chips(p.get("signature_terms"))}</span></div>{avoid}'
        f'<div class="refrow"><b>Headings</b><span>{html.escape(p.get("heading_style") or "-")}</span></div>'
        f'<div class="refrow"><b>Calls to action</b><span>{_chips(p.get("cta_examples")) or "-"}</span></div>'
        f'<details><summary>Rules it follows</summary><ul>{rules or "<li>None found</li>"}</ul></details>'
        f'<details><summary>Pages studied ({len(ref.get("pages", []))})</summary><ul>{pages}</ul></details>{notes}'
        f'<div class="muted" style="margin-top:.4rem">This shapes HOW the content is written. Facts still come only from verified research.</div></div>')

