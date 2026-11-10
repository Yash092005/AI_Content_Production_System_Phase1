import base64
import inspect
import threading
import time

import streamlit as st
import streamlit.components.v1 as components

from app.ai import get_clients
from app.config import jobs, runlog, runtime, store
from app.reference.engine import analyse_reference, public_view as ref_public, usable as ref_usable
from app.pipeline.orchestrator import run_pipeline
from app.strategy import (LENGTH_LEVELS, DEFAULT_LENGTH, TARGETS, OPTIONS, LABELS, DEFAULTS, FRAMEWORK_CODES,
                          normalize, predict_fields, finalize, strip_stage_markers, labels_to_markers)
from app.ui import components as ui
from app.ui import config_page
from app.ui.auth import MAX_ATTEMPTS, configured_password, provider_status, verify
from app.ui.brand import favicon
from app.ui.preview import build_page, guess_title
from app.ui.theme import CSS
from app.version import __version__

st.set_page_config(page_title="Content Compiler | Evidence-first AI content", page_icon=favicon(), layout="wide",
                   initial_sidebar_state="auto", menu_items={"About": f"Content Compiler v{__version__}"})
st.markdown(CSS, unsafe_allow_html=True)

ss = st.session_state
status = provider_status()

# ---------------------------------------------------------------------------
# Optional access gate. Set APP_PASSWORD (env or Streamlit secrets) before putting this on a public URL.
# ---------------------------------------------------------------------------
_password = configured_password(secrets=st.secrets)
if _password and not ss.get("authed"):
    st.markdown('<span id="top" class="anchor"></span>', unsafe_allow_html=True)
    with st.container(key="login_card"):
        st.markdown(ui.login_html(), unsafe_allow_html=True)
        with st.form("login_form"):
            supplied = st.text_input("Access password", type="password", placeholder="Enter password")
            submitted = st.form_submit_button("Unlock", use_container_width=True)
        if submitted:
            if ss.get("auth_fail", 0) >= MAX_ATTEMPTS:
                st.error("Too many attempts. Reload the page to try again.")
            elif verify(supplied, _password):
                # Keep this transition in the same Streamlit run. A forced rerun here can
                # leave AppTest with a stale widget node after the login form disappears,
                # which breaks the next interaction with the View radio.
                ss["authed"] = True
            else:
                ss["auth_fail"] = ss.get("auth_fail", 0) + 1
                time.sleep(1)  # slows guessing
                st.error("Incorrect password.")
    st.stop()

# The Studio always runs on the ACTIVE configuration version (see the Configuration tab).
_settings, _cfg_meta = store.load_active()
_admin_pw = configured_password(secrets=st.secrets, name="CONFIG_PASSWORD")

EXAMPLES = {
    "Revamp": ["Revamp the Jagannath page. Keep the layout, update outdated information and remove unsupported claims."],
    "New Content": [
        "Create a website blog on Ganesh Visarjan 2026 in India",
        "Create a website blog on Navratri 2026 in India",
        "Create a website blog on Diwali 2026 in India",
    ],
}
MODE_HELP = {
    "New Content": "Research a topic and create an original page from scratch.",
    "Revamp": "Keep the existing page structure while updating and validating its content.",
}

ss.setdefault("request", "")
ss.setdefault("result", None)
ss.setdefault("states", ui.new_states())
ss.setdefault("history", [])
ss.setdefault("run_meta", {})
ss.setdefault("active_run_id", None)
ss.setdefault("reference_state", None)   # set by "Analyse website": {input, ref}
ss.setdefault("strategy_state", None)   # set by "Analyse request": {for, mode, source, fields, notes}
FW_AUTO = "Auto (router)"
SK = {f: f"st_{f}" for f in OPTIONS}
for _f in OPTIONS:
    ss.setdefault(SK[_f], DEFAULTS[_f])
ss.setdefault("st_jtbd", DEFAULTS["jtbd"])
ss.setdefault("st_framework", FW_AUTO)
ss.setdefault("length", _settings.get("generation.default_length", DEFAULT_LENGTH))
ss.setdefault("content_mode", _settings.get("system.default_mode", "New Content"))
ss.setdefault("view", "🎨 Studio")


def edited_values():
    vals = {f: ss[SK[f]] for f in OPTIONS}
    vals["jtbd"] = ss["st_jtbd"]
    return vals


def current_strategy():
    """The strategy as the user sees it right now (predicted values + their edits), or None if not analysed."""
    state = ss.get("strategy_state")
    if not state:
        return None
    vals = edited_values()
    fw = ss["st_framework"]
    user_fw = fw != FW_AUTO
    changed = user_fw or any(vals[k] != state["fields"][k] for k in vals)
    with runtime.use_settings(_settings):
        return finalize(normalize(vals), "user" if changed else state["source"],
                        framework=fw if user_fw else None, framework_source="user" if user_fw else "router")


def section(n, title, hint=""):
    st.markdown(ui.section_head_html(n, title, hint), unsafe_allow_html=True)


def show_page(page, height=820):
    """Render the (untrusted, model-written) preview page in an isolated iframe.
    A data: URL gives the iframe an opaque origin, so it cannot touch the Streamlit app."""
    if hasattr(st, "iframe"):
        st.iframe("data:text/html;base64," + base64.b64encode(page.encode("utf-8")).decode("ascii"), height=height)
    else:  # older Streamlit
        components.html(page, height=height, scrolling=True)


def show_code(text, height=540):
    """Code block with a built-in copy button (so what you copy is exactly what ships)."""
    params = inspect.signature(st.code).parameters
    kw = {k: v for k, v in {"wrap_lines": True, "height": height}.items() if k in params}
    st.code(text, language="markdown", **kw)


def retry_shorter(level):
    ss["length"] = level
    ss["autorun"] = True


def use_example(text):
    ss["request"] = text


def sign_out():
    ss["authed"] = False


# ---------------------------------------------------------------------------
# Header + hero
# ---------------------------------------------------------------------------
st.markdown('<span id="top" class="anchor"></span>' + ui.nav_html(status, _settings.version), unsafe_allow_html=True)
with st.container(key="view_switch"):
    view = st.radio("View", ["🎨 Studio", "⚙️ Configuration"], horizontal=True, label_visibility="collapsed")
if view.endswith("Configuration"):
    config_page.render(ss, admin_password=_admin_pw, app_password_set=bool(_password), verify=verify)
    st.markdown(ui.footer_html(status), unsafe_allow_html=True)
    st.stop()
st.markdown(ui.hero_html(), unsafe_allow_html=True)
if status["state"] == "error":
    st.error(status["detail"])

# ---------------------------------------------------------------------------
# 01  Studio: define the task
# ---------------------------------------------------------------------------
st.markdown(ui.anchor("studio"), unsafe_allow_html=True)
with st.container(key="studio_card"):
    section("01", "Define the content task")
    mode = st.radio("Content mode", ["New Content", "Revamp"], horizontal=True, key="content_mode")
    st.caption(MODE_HELP[mode])
    placeholder = ("Example: Revamp the Jagannath page. Keep the layout, update outdated information and remove unsupported claims."
                   if mode == "Revamp" else "Example: Create a website blog on Navratri 2026 in India.")
    request = st.text_area("Content request", key="request", placeholder=placeholder, height=120, label_visibility="collapsed")

    st.caption("Try an example")
    ex = EXAMPLES[mode]
    for col, text in zip(st.columns(len(ex)), ex):
        col.button(text if len(text) < 60 else text[:57] + "…", key=f"ex_{hash(text)}", on_click=use_example, args=(text,),
                   use_container_width=True)

    if mode == "New Content":
        st.radio("Content length", LENGTH_LEVELS, key="length", horizontal=True,
                 help="How much to write. Longer content needs more verified facts, so research is asked to find more.")
        _t = " · ".join(f"{c} ≈ {TARGETS[c][ss['length']]} words" for c in TARGETS)
        st.caption(f"Target for {ss['length']}: {_t}. Length comes from verified facts, never padding.")
        source_text = ""
    else:
        st.markdown("**Existing page** · paste the current content")
        source_text = st.text_area("Existing page content", placeholder="Paste the current page content here…", height=240,
                                   label_visibility="collapsed")

    with st.expander("Advanced inputs", expanded=False):
        extra_text = st.text_area("Facts you vouch for (optional, one per line)",
                                  placeholder="Rangoli is a decorative pattern made on floors during Diwali.\nDiyas are oil lamps lit to represent light over darkness.",
                                  height=110,
                                  help="These are treated as verified: the writer may use them and QA will not flag them. You are responsible for their accuracy.")
        removals_text = st.text_area("Mandatory removals (one per line)",
                                     placeholder="100% guaranteed\nUnsupported waiting-time promise\nUnrelated testimonials", height=110)
        _n_rm = len(_settings.get("rules.mandatory_removals", [])) + len(_settings.get("rules.restricted_phrases", []))
        _n_tf = len(_settings.get("research.trusted_facts", []))
        if _n_rm or _n_tf:
            st.caption(f"The system configuration (v{_settings.version}) also applies {_n_rm} default removal/restricted phrase(s) "
                       f"and {_n_tf} trusted fact(s). What you type here is added to them.")
        fresh = st.toggle("Fresh run (skip saved cache)", value=False,
                          help="Re-running the same request normally reuses saved model output, which repeats the same QA result. "
                               "Turn this on to generate again.")
    ref_on = bool(_settings.get("reference.enabled", True))
    with st.expander("Reference website (optional)", expanded=bool(ss.get("reference_url"))):
        if not ref_on:
            st.info("The reference website layer is switched off in the system configuration.")
        st.caption("Give the website you are writing for. The system studies how it writes (tone, wording and rules) and writes in a "
                   "similar voice. It learns HOW the site writes, never WHAT is true: facts still come only from verified research. "
                   "Leave this empty to skip.")
        ref_url = st.text_input("Website address", key="reference_url", placeholder="https://www.example.com", disabled=not ref_on)
        ra, rb = st.columns([1, 3])
        analyse_ref = ra.button("🔎  Analyse website", use_container_width=True, disabled=not ref_on)
        rb.markdown('<div class="muted" style="padding-top:.55rem">Optional. Skip it and the site is studied automatically when you run.</div>',
                    unsafe_allow_html=True)
        if analyse_ref:
            if not ref_url.strip():
                st.warning("Enter a website address first.")
            else:
                with runtime.use_settings(_settings):
                    try:
                        _rclient = (lambda c: c.get("context") or c.get("reasoning"))(get_clients())
                    except Exception:
                        _rclient = None
                    with st.spinner("Reading the website…"):
                        ss["reference_state"] = {"input": ref_url.strip(), "ref": analyse_reference(ref_url.strip(), client=_rclient, cache_on=False)}
        _rs = ss.get("reference_state")
        if _rs and ref_url.strip():
            if _rs["input"] != ref_url.strip():
                st.caption("This analysis is for a different address. Click “Analyse website” to refresh it.")
            else:
                st.markdown(ui.reference_html(ref_public(_rs["ref"])), unsafe_allow_html=True)
removals = [l.strip() for l in removals_text.splitlines() if l.strip()]

# ---------------------------------------------------------------------------
# 02  Content strategy (Content Development layer)
# ---------------------------------------------------------------------------
with st.container(key="strategy_card"):
    section("02", "Content strategy", "AI predicts · you decide")
    a1, a2 = st.columns([1, 3])
    analyse = a1.button("🧭  Analyse request", use_container_width=True,
                        help="Predicts content type, audience, intent, objective and the best writing framework. You can edit everything.")
    a2.markdown('<div class="muted" style="padding-top:.55rem">Optional. Skip it and the pipeline predicts these automatically. '
                'Edit anything below and your values win.</div>', unsafe_allow_html=True)
    if analyse:
        if not request.strip():
            st.warning("Enter a content request first.")
        else:
            with runtime.use_settings(_settings):
                try:
                    _client = (lambda c: c.get("strategy") or c.get("context") or c.get("reasoning"))(get_clients())
                except Exception:
                    _client = None
                with st.spinner("Analysing your request…"):
                    fields, source = predict_fields(request, mode, _client)
            for f in OPTIONS:
                ss[SK[f]] = fields[f]
            ss["st_jtbd"] = fields["jtbd"]
            ss["st_framework"] = FW_AUTO
            ss["strategy_state"] = {"for": request, "mode": mode, "source": source, "notes": fields["notes"],
                                    "fields": {**{f: fields[f] for f in OPTIONS}, "jtbd": fields["jtbd"]}}

    state = ss["strategy_state"]
    if state:
        if state["for"] != request or state["mode"] != mode:
            st.warning("This strategy was analysed for a different request. Click “Analyse request” to refresh it.")
        for n in state["notes"]:
            st.caption("ℹ️ " + n)
        c1, c2, c3 = st.columns(3)
        c1.selectbox(LABELS["content_type"], OPTIONS["content_type"], key=SK["content_type"])
        c2.selectbox(LABELS["market_type"], OPTIONS["market_type"], key=SK["market_type"])
        c3.selectbox(LABELS["audience"], OPTIONS["audience"], key=SK["audience"])
        c4, c5, c6 = st.columns(3)
        c4.selectbox(LABELS["relationship_stage"], OPTIONS["relationship_stage"], key=SK["relationship_stage"])
        c5.selectbox(LABELS["user_intent"], OPTIONS["user_intent"], key=SK["user_intent"])
        c6.selectbox(LABELS["objective"], OPTIONS["objective"], key=SK["objective"])
        j1, j2 = st.columns([3, 1])
        j1.text_input(LABELS["jtbd"], key="st_jtbd", max_chars=200)
        j2.selectbox(LABELS["framework"], [FW_AUTO] + [f for f in FRAMEWORK_CODES if f in _settings.get("generation.enabled_frameworks", FRAMEWORK_CODES)], key="st_framework",
                     help="Auto lets the router choose from your fields. Pick one to override it.")
        st.markdown(ui.framework_badge_html(current_strategy()), unsafe_allow_html=True)
        if mode == "Revamp":
            st.caption("Revamp keeps the locked page structure, so the framework only guides tone and the order of ideas inside each section.")
    else:
        st.markdown('<div class="card muted">🧭 <b>Not analysed yet.</b> Click “Analyse request” to review the predicted strategy, '
                    'or just run the pipeline and it will be predicted automatically.</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# 03  Run
# ---------------------------------------------------------------------------
with st.container(key="run_card"):
    section("03", "Compile your content")
    stepper_slot = st.empty()
    stepper_slot.markdown(ui.stepper_html(ss["states"]), unsafe_allow_html=True)
    run = st.button("▶  Run content pipeline", type="primary", use_container_width=True)
    if ss.pop("autorun", False):
        run = True

    if run:
        if not request.strip():
            st.warning("Please enter a content request first.")
        elif mode == "Revamp" and not source_text.strip():
            st.warning("Revamp mode needs the existing page content.")
        else:
            task_constraints = (("REQUESTER-PROVIDED INFORMATION (authoritative for Naman service details):\n" + extra_text.strip())
                                if extra_text.strip() else None)
            states = ui.new_states()
            ss["states"] = states

            run_settings = _settings.with_overrides(**{"system.pipeline_cache": False}) if fresh else _settings
            _ref_arg, _ref_url_arg = None, None
            if ref_on and (ss.get("reference_url") or "").strip():
                _rs = ss.get("reference_state")
                if _rs and _rs["input"] == ss["reference_url"].strip() and ref_usable(_rs["ref"]) and not fresh:
                    _ref_arg = _rs["ref"]
                else:
                    _ref_url_arg = ss["reference_url"].strip()

            run_id = jobs.new_id()
            run_meta = {"mode": mode, "request": request}
            jobs.start(run_id, run_meta)
            ss["active_run_id"] = run_id
            ss["run_meta"] = run_meta
            ss["result"] = None

            extra_facts = [l.strip() for l in extra_text.splitlines() if l.strip()]
            stage_cache = ss.setdefault("stage_cache", {})       # finished steps survive a pause (and a server restart)
            if len(stage_cache) > 200:
                stage_cache.clear()
            strategy_value = current_strategy()
            length_value = ss["length"] if mode == "New Content" else None

            def worker():
                worker_states = ui.new_states()

                def on_stage(stage, state):
                    ui.apply_stage(worker_states, stage, state)
                    jobs.stage(run_id, worker_states)

                try:
                    result = run_pipeline(
                        request=request, mode=mode, mandatory_removals=removals, source_text=source_text,
                        task_constraints=task_constraints, on_stage=on_stage, extra_facts=extra_facts,
                        strategy=strategy_value, length=length_value,
                        settings=run_settings, reference_url=_ref_url_arg, reference=_ref_arg, stage_cache=stage_cache
                    )
                    ui.finish_states(worker_states, ok=result.get("status") not in {"PARTIAL", "BLUEPRINT_FAILED", "PAUSED"})
                    jobs.complete(run_id, result, worker_states)

                    try:
                        _qa = result.get("qa") or {}
                        kind, _, label, _ = ui.result_state(result)
                        runlog.append({
                            "config_version": result.get("config_version"), "mode": mode, "request": request[:90],
                            "result": label, "qa": _qa.get("status"), "issues": len(_qa.get("issues") or []),
                            "revisions": result.get("revisions", 0), "error": (_qa.get("error") or "")[:160],
                            "reference": ((result.get("reference") or {}).get("host") or "") if (result.get("reference") or {}).get("used") else ""
                        })
                    except Exception:
                        pass
                except Exception as e:
                    ui.finish_states(worker_states, ok=False)
                    result = {
                        "status": "PARTIAL", "live": False, "rules": {}, "blueprint": {}, "draft": "",
                        "revisions": 0, "fallbacks": [],
                        "qa": {"status": "FAIL", "issues": [f"Pipeline error: {e}"], "error": str(e), "evaluated": False}
                    }
                    jobs.fail(run_id, str(e), worker_states)

            threading.Thread(target=worker, daemon=True).start()
            st.info("Pipeline started. This page will update automatically when each stage completes.")

# ---------------------------------------------------------------------------
# 04 / 05  Results (persisted in session state: survive downloads and widget clicks)
# ---------------------------------------------------------------------------
st.markdown(ui.anchor("results"), unsafe_allow_html=True)


@st.fragment(run_every="2s")
def render_pipeline_results():
    active_run_id = ss.get("active_run_id")
    if active_run_id:
        job = jobs.load(active_run_id)
        if job:
            latest_states = job.get("states") or {}
            if latest_states:
                ss["states"] = latest_states
            if job.get("status") == "COMPLETE":
                ss["result"] = job.get("result")
                ss["run_meta"] = job.get("meta") or ss.get("run_meta", {})
                if ss.get("result") is not None and not ss.get("completed_run_logged") == active_run_id:
                    result = ss["result"]
                    kind, _, label, _ = ui.result_state(result)
                    ss["history"].append({
                        "kind": kind, "label": label,
                        "mode": ss["run_meta"].get("mode", mode),
                        "title": ss["run_meta"].get("request", "")[:42] + ("…" if len(ss["run_meta"].get("request", "")) > 42 else "")
                    })
                    ss["completed_run_logged"] = active_run_id
                    st.toast(label, icon={"pass": "✅", "warn": "⚠️"}.get(kind, "🔍"))
                ss["active_run_id"] = None
            elif job.get("status") == "FAILED":
                st.error("Pipeline failed: " + str(job.get("error") or "Unknown error"))
                return

    result = ss["result"]
    if not result:
        st.markdown(ui.empty_state_html(), unsafe_allow_html=True)
    else:
        meta = ss["run_meta"]
        qa = result.get("qa") or {}
        with st.container(key="result_card"):
            section("04", "Run result")
            st.markdown(ui.verdict_html(result), unsafe_allow_html=True)
            if result.get("config_version"):
                st.caption(f"Produced with system configuration v{result['config_version']}.")
            _rf = result.get("reference")
            if _rf and _rf.get("used"):
                st.caption(f"Written in the style of {_rf['host']}.")
            elif _rf:
                st.warning(f"The reference website was not used: {_rf.get('error') or 'it could not be studied'}. The content was written without it.")
            if result.get("status") == "PARTIAL":
                err = (qa or {}).get("error") or " ".join((qa or {}).get("issues") or [])
                st.markdown(ui.error_help_html(err), unsafe_allow_html=True)
                if mode == "New Content" and ss["length"] != LENGTH_LEVELS[0]:
                    lower = LENGTH_LEVELS[LENGTH_LEVELS.index(ss["length"]) - 1]
                    st.button(f"↻  Retry with {lower} length", on_click=retry_shorter, args=(lower,))
                with st.expander("Technical details"):
                    st.code(err or "No details", language=None)
            if result.get("status") == "PAUSED":
                _p = result.get("paused") or {}
                _wait = f" Providers should be ready again in about {int(_p['retry_after'])} seconds." if _p.get("retry_after") else ""
                st.info(f"Paused at the '{_p.get('stage')}' step because no AI provider can safely take it right now. "
                        f"Everything finished so far is saved: press Run again to continue from here.{_wait}")
                with st.expander("Technical details"):
                    st.code(_p.get("message") or "No details", language=None)
            st.markdown(ui.stats_html(result), unsafe_allow_html=True)
            st.markdown(ui.framework_badge_html(result.get("strategy")), unsafe_allow_html=True)

            if result.get("status") == "NEEDS_INPUT":
                st.markdown("#### Information needed")
                for q in result.get("questions", []):
                    st.write("• " + str(q))
            if not result.get("live", True) and result.get("status") not in {"PARTIAL", "PAUSED"}:
                st.warning("Mock/offline run: output is placeholder content, not live AI output.")
            if result.get("fallbacks"):
                with st.expander(f"Provider / fallback events ({len(result['fallbacks'])})"):
                    for note in result["fallbacks"]:
                        st.caption("↳ " + str(note))

            section("05", "Output & validation")
            _labels = ["🌐 Web preview", "📝 Draft", "🧭 Strategy", "🛡️ QA report", "🧩 Blueprint", "📚 Rules"]
            if (result.get("reference") or {}).get("used"):
                _labels.insert(3, "🔗 Reference")
            _tabs = dict(zip(_labels, st.tabs(_labels)))
            t_prev, t_draft, t_strat, t_qa, t_bp, t_rules = (_tabs[k] for k in ("🌐 Web preview", "📝 Draft", "🧭 Strategy",
                                                                              "🛡️ QA report", "🧩 Blueprint", "📚 Rules"))
            t_ref = _tabs.get("🔗 Reference")
            draft = result.get("draft", "")
            clean_draft = strip_stage_markers(labels_to_markers(draft))   # framework stage markers are hidden comments: never ship them
            title = guess_title(meta.get("request"))

            with t_prev:
                if draft:
                    page = build_page(title, clean_draft)
                    d1, d2, _ = st.columns([1, 1, 3])
                    d1.download_button("⬇ HTML page", page, file_name="content-preview.html", mime="text/html", use_container_width=True)
                    d2.download_button("⬇ Markdown", clean_draft, file_name="content-draft.md", mime="text/markdown", use_container_width=True)
                    show_page(page)
                else:
                    st.info("No draft was generated.")

            with t_draft:
                if draft:
                    st.caption("Clean markdown, ready to copy. Use the copy icon at the top right of the box.")
                    show_code(clean_draft)
                    if clean_draft != draft:
                        with st.expander("Show with framework stage markers"):
                            st.caption("The hidden <!-- stage: … --> comments mark the framework stages QA checks. Previews and downloads remove them.")
                            show_code(draft, height=360)
                else:
                    st.info("No draft was generated.")

            with t_strat:
                if result.get("strategy"):
                    st.markdown(ui.framework_badge_html(result["strategy"]), unsafe_allow_html=True)
                    st.markdown(ui.strategy_table_html(result["strategy"]), unsafe_allow_html=True)
                    for n in result["strategy"].get("notes", []):
                        st.caption("ℹ️ " + n)
                else:
                    st.info("No strategy was recorded for this run.")

            if t_ref is not None:
                with t_ref:
                    st.markdown(ui.reference_html(result["reference"]), unsafe_allow_html=True)

            with t_qa:
                if not qa.get("evaluated", True):
                    st.warning("QA was not evaluated; this result should not be treated as a real PASS/FAIL.")
                issues = qa.get("issues") or []
                st.markdown(ui.checks_html(qa.get("checks")), unsafe_allow_html=True)
                st.markdown(ui.warnings_html(qa.get("warnings")), unsafe_allow_html=True)
                st.write("")
                if qa.get("status") == "PASS" and qa.get("warnings"):
                    st.markdown('<div class="warn-box">⚠️ QA passed the correctness checks, but the result has warnings that should be reviewed before publishing.</div>', unsafe_allow_html=True)
                elif qa.get("status") == "PASS":
                    st.markdown('<div class="ok-box">✅ No issues. The draft passed every deterministic check and the QA audit.</div>', unsafe_allow_html=True)
                elif issues:
                    st.markdown(f"**What QA wants fixed** ({len(issues)})")
                    st.markdown(ui.issues_html(issues), unsafe_allow_html=True)
                    st.caption("Tip: turn on “Fresh run” under Advanced inputs and run again, or put facts you trust there.")
                else:
                    st.error("QA failed but returned no detail. See the auditor note below.")
                if qa.get("auto_removed"):
                    st.info(qa.get("note", "Code removed sentences QA flagged."))
                    with st.expander(f"Sentences removed by code ({len(qa['auto_removed'])})"):
                        for sentence in qa["auto_removed"]:
                            st.markdown("- " + sentence)
                if qa.get("model_verdict"):
                    with st.expander("Auditor note (raw model verdict)"):
                        st.text(qa["model_verdict"])
                with st.expander("Raw QA JSON"):
                    st.json(qa)

            with t_bp:
                st.json(result.get("blueprint", {}))
            with t_rules:
                st.json(result.get("rules", {}))


render_pipeline_results()

# ---------------------------------------------------------------------------
# How it works, FAQ, footer
# ---------------------------------------------------------------------------
st.markdown(ui.how_it_works_html(), unsafe_allow_html=True)
st.markdown(ui.anchor("faq") + '<div class="sec-head"><div class="eyebrow">FAQ</div><h2>Good questions</h2></div>', unsafe_allow_html=True)
with st.container(key="faq_card"):
    for q, a in ui.FAQS:
        with st.expander(q):
            st.write(a)
st.markdown(ui.footer_html(status), unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Sidebar (rendered last so "Recent runs" includes the run that just finished)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Workspace")
    st.markdown(f'<div class="sidecard"><b>AI PROVIDER</b>{ui.status_pill_html(status)}<small>{status["detail"]}</small></div>',
                unsafe_allow_html=True)
    if ss["history"]:
        st.markdown("**Recent runs**")
        colors = {"pass": "#22c55e", "fail": "#ef4444", "warn": "#f59e0b"}
        for h in reversed(ss["history"][-5:]):
            st.markdown(f'<div class="hist"><i style="background:{colors[h["kind"]]}"></i><div>{h["title"]}<br>'
                        f'<span class="muted">{h["mode"]} · {h["label"]}</span></div></div>', unsafe_allow_html=True)
        st.write("")
    st.markdown("**Pipeline principles**")
    st.caption("🔒 Locked structure stays locked")
    st.caption("🔎 Verified evidence is the factual boundary")
    st.caption("🛡️ Unsupported claims must not ship")
    st.caption("✅ QA decides PASS / FAIL")
    if _password:
        st.divider()
        st.button("Sign out", on_click=sign_out, use_container_width=True)
