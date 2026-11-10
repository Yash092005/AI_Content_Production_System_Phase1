"""The System Configuration screen. Everything on the main page that can be controlled without code lives here.

Design: every setting has a Streamlit widget keyed "cfg::<dotted.path>". The widgets ARE the draft. Pending changes,
validation and Apply are all computed from them, so what you see is exactly what will be applied."""
import html
import time

import json

import streamlit as st

from app.config import health, runlog, schema as S, store
from app.config.validate import ConfigError, OUTPUT_RESERVE, effective_rules, rules_tokens, validate
from app.rules.loader import load_rules


def K(path): return f"cfg::{path}"


RULE_LABELS = {"master": "Master Instructions",
               "canonical": "Canonical Specification", "brand": "Brand Constitution"}
LOCKED_CHECKS = [
    ("Evidence gate", "Claims outside the verified evidence are never allowed"),
    ("Mandatory removals", "Removal and restricted phrases are always enforced"),
    ("Unverified times", "Clock times need a verified source"),
    ("AI audit", "The QA model always reviews the draft"),
]
LIST_PATHS = {"rules.mandatory_removals", "rules.restricted_phrases", "research.trusted_facts",
              "reference.allowed_domains", "reference.blocked_domains"}
RULE_PATHS = {f"rules.{k}" for k in RULE_LABELS}
JSON_PATHS = {"budget.gemini_limits", "provider.key_groups"}          # lists of entries, edited as JSON text
FLOAT_PATHS = {"qa.min_length_ratio", "provider.gemini_min_interval", "budget.target_headroom", "budget.floor_headroom",
               "budget.hard_ceiling"}


def _file_rules():
    # the data/rules files only (ignores any active configuration)
    return load_rules(overrides={})


# ---------------------------------------------------------------------------
# config <-> widget state
# ---------------------------------------------------------------------------
def seed_widgets(ss, cfg):
    """Load a configuration into the widgets (only ever called before they are drawn)."""
    files = _file_rules()
    file_key = {"master": "master", "canonical": "canonical", "brand": "brand"}
    for path, value in S.flatten(S.merge_defaults(cfg)).items():
        if path in RULE_PATHS:
            value = value if isinstance(value, str) and value.strip(
            ) else files[file_key[path.split(".")[1]]]
        elif path in LIST_PATHS:
            value = "\n".join(value or [])
        elif path in JSON_PATHS:
            value = json.dumps(value if value is not None else [], indent=1)
        elif path in FLOAT_PATHS:
            value = float(value)
        ss[K(path)] = value


def collect(ss, base=None):
    """The configuration the widgets currently describe."""
    files = _file_rules()
    cfg = S.merge_defaults(base or S.default_config())
    for path in S.flatten(S.default_config()):
        if K(path) not in ss:
            continue
        v = ss[K(path)]
        if path in RULE_PATHS:
            v = None if (not str(v).strip() or str(v).strip() ==
                         files[path.split(".")[1]].strip()) else str(v)
        elif path in LIST_PATHS:
            v = [x.strip() for x in str(v).splitlines() if x.strip()]
        elif path in JSON_PATHS:
            try:
                v = json.loads(str(v) or "[]")
            except ValueError:
                v = str(v)            # kept as text so the validation can say it is not valid JSON
        elif path in FLOAT_PATHS:
            v = float(v)
        S.set_path(cfg, path, v)
    return cfg


def _baseline(ss):
    """What the widgets are seeded from: a requested config, else the saved draft, else the active version."""
    req = ss.pop("cfg_load", None)
    if isinstance(req, dict):
        return req
    return store.load_draft() or store.load_active()[0].to_dict()


def _request_load(ss, cfg=None):
    ss["cfg_load"] = cfg if cfg is not None else store.load_active()[
        0].to_dict()
    ss["cfg_needs_seed"] = True


def _pretty(v):
    if isinstance(v, list):
        return ", ".join(map(str, v)) if v else "(empty)"
    if v is None:
        return "(file)"
    if isinstance(v, str) and len(v) > 60:
        return v[:57] + "…"
    return "off" if v is False else "on" if v is True else str(v)


def _changes_html(changes, limit=14):
    if not changes:
        return '<div class="muted">No changes.</div>'
    rows = "".join(f'<div class="chg"><code>{html.escape(p)}</code> <span>{html.escape(_pretty(a))}</span> → '
                   f'<b>{html.escape(_pretty(b))}</b></div>' for p, a, b in changes[:limit])
    more = f'<div class="muted">…and {len(changes) - limit} more</div>' if len(
        changes) > limit else ""
    return rows + more


def _select(label, options, path, **kw):
    """selectbox whose current value is always valid (a hand-edited or older value is added instead of crashing)."""
    options = list(options)
    cur = st.session_state.get(K(path))
    if cur is not None and cur not in options:
        options.append(cur)
    return st.selectbox(label, options, key=K(path), **kw)


# ---------------------------------------------------------------------------
# tabs
# ---------------------------------------------------------------------------
def _tab_rules(ss, cfg_now):
    st.markdown("##### Rule documents")
    st.caption("These three documents are sent to the model with every request. Edit them here, and the next run follows "
               "them. Text that matches the file in data/rules is saved as 'use the file'.")
    files = _file_rules()
    for key, label in RULE_LABELS.items():
        path = f"rules.{key}"
        st.text_area(label, key=K(path), height=190)
        edited = cfg_now["rules"][key] is not None
        c1, c2 = st.columns([4, 1])
        c1.caption(f"{'✏️ Edited here (differs from the file)' if edited else '📄 Using data/rules file'} · "
                   f"{len(ss[K(path)]):,} characters · about {len(ss[K(path)]) // 4:,} tokens")
        if edited:
            c2.button("Use file", key=f"reset_{key}", on_click=lambda p=K(
                path), t=files[key]: ss.__setitem__(p, t))
    st.divider()
    st.markdown("##### Banned and restricted wording")
    l, r = st.columns(2)
    with l:
        st.text_area("Mandatory removals (one per line)", key=K("rules.mandatory_removals"), height=150,
                     help="Always removed from every draft. Per-run removals typed in the Studio are added to these.")
    with r:
        st.text_area("Restricted phrases (one per line)", key=K("rules.restricted_phrases"), height=150,
                     help="Checked in code on every draft, like a mandatory removal. Remove a phrase here if it is verified for a service.")
        suggested = health.suggested_restricted(files["brand"])
        if suggested:
            def add_suggested():
                have = [x.strip() for x in ss[K(
                    "rules.restricted_phrases")].splitlines() if x.strip()]
                ss[K("rules.restricted_phrases")] = "\n".join(
                    have + [p for p in suggested if p.lower() not in {h.lower() for h in have}])
            st.button(f"Add the {len(suggested)} phrases from the brand rules", key="add_suggested", on_click=add_suggested,
                      help="; ".join(suggested))
    st.text_area("Writing constraints (optional)", key=K("rules.writing_constraints"), height=90,
                 placeholder="For example: Use British spelling. Keep paragraphs under four sentences.",
                 help="Added to the model's instructions on every run.")


def _tab_research(ss, cfg_now):
    c1, c2 = st.columns(2)
    with c1:
        st.number_input("Words of prose per verified claim", 30, 200, step=5, key=K("research.words_per_claim"),
                        help="Research depth. Lower asks research for more claims per article (more tokens). Default 75.")
        st.toggle("Check cited sources", key=K("research.source_check"),
                  help="Visit cited URLs and drop made-up links and unconfirmed clock times.")
        st.toggle("Require a source link on every researched claim", key=K("research.require_source_url"),
                  help="Claims without a URL are dropped. Facts you type yourself are exempt.")
    with c2:
        st.toggle("Gemini web grounding", key=K("research.gemini_grounding"),
                  help="Let Gemini search the web while researching.")
        st.toggle("Groq browser search", key=K("research.groq_browser_search"),
                  help="Let Groq use its built-in browser search.")
    st.text_area("Trusted facts (one per line, up to 25)", key=K("research.trusted_facts"), height=150,
                 help="Treated as verified on EVERY run (the writer may use them and QA will not flag them). You are responsible "
                      "for their accuracy. Facts typed in the Studio are added to these.")
    st.divider()
    st.markdown("##### Reference website layer (optional)")
    st.caption("When a person gives a reference website in the Studio, the system studies how it writes and matches that voice. "
               "It learns HOW the site writes, never WHAT is true: facts still come only from verified research.")
    r1, r2 = st.columns(2)
    with r1:
        st.toggle("Allow the reference website layer",
                  key=K("reference.enabled"))
        st.toggle("Respect robots.txt", key=K("reference.respect_robots"),
                  help="Follow the site's own rules about automated access. Turning this off is not recommended.")
        st.number_input("Pages to read per site", 1, 5, step=1, key=K("reference.max_pages"),
                        help="The address given plus other pages of the same site, such as About or Blog.")
        st.number_input("Seconds to wait for each page", 3, 30,
                        step=1, key=K("reference.timeout_seconds"))
    with r2:
        st.number_input("Hours to remember a studied site", 0, 720, step=1, key=K("reference.cache_hours"),
                        help="0 studies the site again on every run.")
        st.number_input("Text sample sent to the model (characters)",
                        2000, 20000, step=500, key=K("reference.max_chars"))
    d1, d2 = st.columns(2)
    d1.text_area("Only allow these websites (one per line, optional)", key=K("reference.allowed_domains"), height=100,
                 placeholder="example.com", help="If any are listed, no other website can be used as a reference.")
    d2.text_area("Never allow these websites (one per line, optional)", key=K("reference.blocked_domains"), height=100,
                 placeholder="competitor.com")


def _tab_generation(ss, cfg_now):
    st.markdown("##### Content length")
    st.selectbox("Default length", S.LENGTH_LEVELS,
                 key=K("generation.default_length"))
    st.caption(
        "Target words per content type. They must grow from Short to In-depth.")
    for ct in S.CONTENT_TYPES:
        cols = st.columns([1.2, 1, 1, 1])
        cols[0].markdown(f"**{ct}**")
        for col, lv in zip(cols[1:], S.LENGTH_LEVELS):
            col.number_input(lv, 50, 5000, step=50, key=K(
                f"generation.length_targets.{ct}.{lv}"), label_visibility="visible")
    st.divider()
    st.markdown("##### Frameworks")
    c1, c2 = st.columns(2)
    c1.multiselect("Enabled frameworks", S.FRAMEWORKS, key=K("generation.enabled_frameworks"),
                   help="Only these are offered in the Studio and used by the router.")
    enabled = ss.get(K("generation.enabled_frameworks")) or S.FRAMEWORKS
    with c2:
        _select("Default framework", ["Auto"] + [f for f in S.FRAMEWORKS if f in enabled], "generation.default_framework",
                help="Auto lets the router choose per request. A fixed framework is used for every run unless the person overrides it.")
    st.selectbox("Strategy prediction", S.STRATEGY_PREDICT, key=K("generation.strategy_predict"),
                 help="ai asks the model only when the request is ambiguous (the keyword prediction is used otherwise). "
                      "always_ai asks every time. heuristic never asks.")
    with st.expander("Model and provider settings"):
        st.caption("API keys are never stored here. They stay in .env or your host's secrets. Give each purpose its own key "
                   "(GEMINI_API_KEY_RESEARCH, GROQ_API_KEY_QA ...). The Maintenance tab shows which key serves what.")
        a, b = st.columns(2)
        with a:
            st.selectbox("Provider mode", S.PROVIDER_MODES,
                         key=K("provider.mode"))
            st.text_input("Gemini model", key=K("provider.gemini_model"))
            st.text_input("Gemini fallback model (optional)",
                          key=K("provider.gemini_fallback_model"))
            st.number_input("Gemini minimum seconds between calls", 0.0,
                            60.0, step=1.0, key=K("provider.gemini_min_interval"))
            st.toggle("Use the shared key as a backup for each purpose's own key", key=K("provider.key_backup"),
                      help="When a purpose's own key hits its limit, try the shared key before falling back to the other provider.")
            st.number_input("Seconds a limited key rests", 10, 900, step=10, key=K("provider.key_cooldown_seconds"),
                            help="A key that hit its rate limit is skipped for this long, so the next calls go to a key that works.")
        with b:
            st.text_input("Groq model", key=K("provider.groq_model"))
            st.text_input("Groq research model (optional)",
                          key=K("provider.groq_research_model"))
            st.selectbox("Groq reasoning effort", S.EFFORTS, key=K("provider.groq_reasoning_effort"),
                         format_func=lambda v: v or "off")
            st.number_input("Groq tokens per minute", 1000,
                            1_000_000, step=500, key=K("provider.groq_tpm"))
            st.number_input("Groq revision budget (tokens)", 1000,
                            1_000_000, step=500, key=K("provider.groq_revision_tpm"))
        st.markdown("##### Request budget and routing")
        st.caption("Before every call the system estimates input + output + search overhead and keeps headroom below the limits of the "
                   "key's project / organisation.")
        c1, c2, c3 = st.columns(3)
        c1.number_input("Target headroom", 0.05, 0.9, step=0.05, key=K("budget.target_headroom"),
                        help="Preferred share of every limit kept free (0.25 = use at most 75%).")
        c2.number_input("Minimum headroom", 0.05, 0.9, step=0.05, key=K("budget.floor_headroom"),
                        help="A request with less headroom than this is not sent.")
        c3.number_input("Hard ceiling", 0.5, 0.95, step=0.05, key=K("budget.hard_ceiling"),
                        help="No request ever uses more than this share of a limit.")
        st.caption("Limits are per Google project AND model (Gemini) and per organisation AND model (Groq). Group **unverified** means "
                   "every key whose project is not declared or observed: those keys are assumed to share ONE quota. Gemini 3.6 "
                   "Flash (free tier: 5 requests/minute, 250,000 tokens/minute, 20 requests/day) is verified; any other model or "
                   "project is assumed to be no better than that until you enter its limits.")
        st.text_area("Gemini limits per project and model (JSON)", key=K("budget.gemini_limits"), height=170,
                     help='A list of {"model", "group", "rpm", "tpm", "rpd", "source"}. Use the project label you gave your keys below '
                          'as "group" ("unverified" for undeclared keys, "*" for every project).')
        st.text_area("Which project / organisation each key belongs to (JSON)", key=K("provider.key_groups"), height=110,
                     help='A list of {"provider": "gemini" or "groq", "key": "<fingerprint shown in Maintenance>", "group": "project-a"}. '
                          'Also possible with GEMINI_KEY_GROUPS / GROQ_KEY_GROUPS="fingerprint=label,...".')
        e1, e2 = st.columns(2)
        e1.selectbox("Gemini Google Search grounding", S.GROUNDING_CAPABILITY, key=K("research.gemini_grounding_capability"),
                     help="auto: never assumed; tried once per key and model, then remembered. yes: confirmed available for your "
                          "project. no: never use it (research then uses Groq's web search).")
        st.selectbox("Per-stage Gemini model variables (GEMINI_MODEL_QA, _RESEARCH, ...)", S.STAGE_MODELS_ENV,
                     key=K("provider.gemini_stage_models_env"),
                     help="ignore: exactly as the deployed application has always behaved (one model for every stage). apply: use the "
                          "GEMINI_MODEL_<STAGE> variables found in the environment. Their free-tier limits are NOT verified, so they are "
                          "treated conservatively; check output quality before applying.")
        e2.selectbox("Rule documents per stage", S.RULES_SCOPING, key=K("budget.rules_scoping"),
                     help="all: each stage gets only its sections (data/rules/profiles.json). groq: only calls that go to Groq "
                          "are scoped. off: every call gets the full documents.")


def _tab_qa(ss, cfg_now):
    c1, c2 = st.columns(2)
    c1.number_input("Maximum revisions", 0, 5, step=1, key=K("qa.max_revisions"),
                    help="How many times a failed draft is sent back for fixing before it is reported as failed.")
    c2.number_input("Minimum length (share of the target)", 0.5, 1.0, step=0.05, key=K("qa.min_length_ratio"),
                    help="Below this share of the target, a draft counts as too short.")
    st.markdown("##### Checks you can switch on or off")
    cols = st.columns(2)
    for i, (name, label) in enumerate(S.QA_CHECKS.items()):
        cols[i % 2].toggle(label, key=K(f"qa.checks.{name}"))
    st.markdown("##### Always on")
    st.markdown("".join(f'<span class="check y" title="{html.escape(d)}">🔒 {html.escape(n)}</span>' for n, d in LOCKED_CHECKS),
                unsafe_allow_html=True)
    st.caption(
        "These protect factual accuracy and cannot be switched off from here.")


def _tab_system(ss, cfg_now, meta):
    c1, c2 = st.columns(2)
    with c1:
        st.toggle("Save model answers (cache)", key=K("system.pipeline_cache"),
                  help="Repeating a request reuses saved answers and spends no API quota. Fresh run skips it for one run.")
        st.text_input("Cache folder", key=K("system.cache_dir"),
                      help="A folder inside the project.")
        st.toggle("Lean mode", key=K("system.lean_mode"),
                  help="Fewer model calls per run, for small free-tier quotas.")
    with c2:
        st.selectbox("Default mode on the main page",
                     S.MODES, key=K("system.default_mode"))
        st.markdown(f"**Active version:** v{meta.get('version') or '?'}  \n"
                    f"<span class='muted'>{html.escape(str(meta.get('created_at') or ''))} · {html.escape(str(meta.get('author') or ''))}</span>",
                    unsafe_allow_html=True)
    st.markdown("##### Extra instructions per stage")
    st.caption(
        "Standing instructions added to one stage's prompt on every run. Use this to improve behaviour without code.")
    labels = {"strategy": "Strategy", "research": "Research",
              "generate": "Generate (writer)", "qa": "QA audit"}
    a, b = st.columns(2)
    for i, s in enumerate(S.STAGES):
        (a if i % 2 == 0 else b).text_area(labels[s], key=K(f"system.stage_instructions.{s}"), height=110,
                                           placeholder="Optional. Leave empty for none.")


def _usage_panel(ss):
    """Where the quota goes (real usage when providers report it) and what the router currently knows. Read-only."""
    from app.ai.budget import TRACKER
    from app.ai.health import HEALTH
    from app.metrics.usage import LOG
    st.markdown("##### Usage and routing")
    snap = HEALTH.snapshot()
    if snap["cooldowns"]:
        st.dataframe([{"Provider": c["provider"], "Model": c["model"], "Resting": c["scope"], "Why": c["kind"], "Rests (s)": c["rests_s"]}
                      for c in snap["cooldowns"]], hide_index=True, use_container_width=True)
    else:
        st.caption("No provider is resting right now.")
    if snap["grounding"]:
        st.dataframe([{"Model": g["model"], "Key": g["key"], "Gemini web grounding": g["state"], "Remembered (h)": g["hours_left"]}
                      for g in snap["grounding"]], hide_index=True, use_container_width=True)
    else:
        st.caption("Gemini web grounding has not been tried yet (it is never assumed).")
    summary = LOG.summary()
    if summary["calls"]:
        st.caption(f"{summary['calls']} calls recorded since the last restart, about {summary['tokens']:,} tokens "
                   f"(real counts where the provider reports them).")
        a, b = st.columns(2)
        a.dataframe([{"Provider": k, "Calls": v["calls"], "Failed": v["failed"], "Tokens": v["tokens"], "Share %": v["share"],
                      "Cached": v["cached"]} for k, v in summary["by_provider"].items()], hide_index=True, use_container_width=True)
        b.dataframe([{"Stage": k, "Calls": v["calls"], "Failed": v["failed"], "Tokens": v["tokens"], "Share %": v["share"]}
                     for k, v in summary["by_stage"].items()], hide_index=True, use_container_width=True)
        with st.expander("Recent calls"):
            st.dataframe([{k: r.get(k) for k in ("provider", "model", "stage", "ok", "est_in", "prompt_tokens", "completion_tokens",
                                                "cached_tokens", "out_cap", "duration_s", "error")} for r in LOG.rows(25)][::-1],
                         hide_index=True, use_container_width=True)
        st.download_button("⬇ Export usage log (JSON)", __import__("json").dumps(LOG.rows(), indent=1),
                           file_name="usage-log.json", mime="application/json")
    else:
        st.caption("No calls recorded yet.")
    cal = {p: LOG.chars_per_token(p) for p in ("gemini", "groq")}
    st.caption("Calibration (replaces estimates after 5 measured calls): characters per token "
               + ", ".join(f"{p} {v:.2f}" if v else f"{p} not measured yet (using 4.0)" for p, v in cal.items())
               + ". Search overhead on Groq research: "
               + (f"{LOG.tool_overhead_p90('groq', 'research') or LOG.tool_overhead_p90('groq', 'research_blueprint')} tokens (p90)"
                  if (LOG.tool_overhead_p90('groq', 'research') or LOG.tool_overhead_p90('groq', 'research_blueprint')) is not None
                  else "not measured yet (reserving the provisional amount)."))
    if st.button("Forget provider memory", help="Clears cooldowns, the Gemini grounding memory and learned ceilings. "
                                                  "Use it after changing keys or billing."):
        HEALTH.reset()
        TRACKER.reset()
        st.success("Provider memory cleared.")


def _quota_panel(cfg_now):
    """Which Google project / Groq organisation each key is believed to belong to, and the limits applied to it."""
    import os
    from app.ai import budget
    from app.ai.keys import OPTIONAL_PURPOSES, PURPOSES, dedicated_keys, fingerprint, keys_for, stage_model_env_found
    from app.ai.quota import GROUPS
    st.markdown("##### Quota groups (which project / organisation each key counts against)")
    rows, per_provider = [], {"gemini": 0, "groq": 0}
    model = str(S.get_path(cfg_now, "provider.gemini_model") or "gemini-3.6-flash")
    for prov in ("gemini", "groq"):
        uses = {}
        for purpose in (*PURPOSES, *OPTIONAL_PURPOSES):
            if purpose in OPTIONAL_PURPOSES and not dedicated_keys(prov, purpose):
                continue
            for key in keys_for(prov, purpose)[0]:
                uses.setdefault(fingerprint(key), []).append(purpose)
        for snap in GROUPS.snapshot(prov, list(uses)):
            lim = budget.limits_for(prov, model if prov == "gemini" else str(S.get_path(cfg_now, "provider.groq_model") or ""),
                                    f"{prov}:{snap['group']}")
            text = ", ".join(f"{n} {v}" for n, v in (("RPM", lim.rpm), ("TPM", lim.tpm), ("RPD", lim.rpd)) if v) or "unknown (learned from limit errors)"
            rows.append({"Provider": prov, "Key": snap["key"], "Used for": ", ".join(uses[snap["key"]]), "Quota group": snap["group"],
                         "How known": snap["status"], "Limits applied": f"{text} [{lim.source or 'n/a'}]"})
            per_provider[prov] += 1 if snap["status"] == "unverified" else 0
    if rows:
        st.dataframe(rows, hide_index=True, use_container_width=True)
    for prov, n in per_provider.items():
        if n > 1:
            st.warning(f"{n} {prov.title()} keys are not verified to belong to different projects/organisations, so they are treated as "
                       f"ONE shared quota. Declare their groups above if they are separate, or let the system learn it from limit errors.")
    for note in GROUPS.conflicts:
        st.warning(note)
    found = stage_model_env_found()
    if found:
        applied = str(S.get_path(cfg_now, "provider.gemini_stage_models_env")) == "apply"
        st.caption("Per-stage model variables found in the environment: " + ", ".join(f"{k}={v}" for k, v in found)
                   + (" (applied)." if applied else " (preserved, not applied: the setting says ignore, as the deployed code always did)."))


def _tab_maintenance(ss, cfg_now, meta):
    st.markdown("##### Providers")
    rows, summary = health.provider_health(cfg_now)
    st.caption(summary)
    st.dataframe([{"Provider": r["provider"], "API key": "set" if r["key"] else "missing", "Model": r["model"],
                   "Second model": r["fallback_model"], "Used": "yes" if r["used"] else "no"} for r in rows],
                 hide_index=True, use_container_width=True)
    st.markdown("##### API keys by purpose")
    st.info("Keys only add capacity when they come from **different Google projects** (Gemini) or **different Groq organisations**. "
            "Gemini applies its limits per project and Groq per organisation, not per key, so several keys from one account share a single quota.")
    key_rows, key_warnings = health.key_overview(cfg_now)
    st.dataframe([{"Purpose": r["label"], "Gemini": health.key_text(r["gemini"]), "Groq": health.key_text(r["groq"])} for r in key_rows],
                 hide_index=True, use_container_width=True)
    for w in key_warnings:
        st.warning(w)
    st.caption("Set keys in .env or your host's secrets: GEMINI_API_KEY_CONTEXT, _RESEARCH, _GENERATION, _QA (and the same for GROQ_API_KEY). "
               "Separate several keys for one purpose with commas: when one hits its limit the next is used. The shared GEMINI_API_KEY / "
               "GROQ_API_KEY serves any purpose without its own key. Only short ids are shown here, never a key.")
    if st.button("Test each API key", help="Sends one tiny request with every distinct key. Uses a few tokens per key."):
        with st.spinner("Testing keys…"):
            ss["cfg_key_test"] = health.test_keys(cfg_now)
    for r in ss.get("cfg_key_test", []):
        line = f"{r['provider']} key {r['key_id']} ({r['purposes']}): {r['detail']} ({r['seconds']}s)"
        (st.success if r["ok"] else st.error)(line)
    st.divider()
    _quota_panel(cfg_now)
    st.divider()
    _usage_panel(ss)
    st.divider()
    st.markdown("##### Saved results (cache)")
    try:
        info = health.cache_info(cfg_now["system"]["cache_dir"])
        st.caption(
            f"{info['files']} saved answers · {info['bytes'] / 1024:.0f} KB · {info['path']}")
        ok = st.checkbox(
            "I understand the next runs will use API quota again", key="cfg_clear_ok")
        if st.button("Clear saved results", disabled=not ok):
            st.success(
                f"Deleted {health.clear_cache(cfg_now['system']['cache_dir'])} saved answers.")
    except ValueError as e:
        st.error(str(e))
    st.divider()
    st.markdown("##### Recent runs")
    runs = runlog.recent(15)
    if runs:
        st.dataframe([{"Time": r.get("time"), "Config": f"v{r['config_version']}" if r.get("config_version") else "-",
                       "Mode": r.get("mode"), "Request": r.get("request"), "Result": r.get("result"), "QA": r.get("qa"),
                       "Issues": r.get("issues"), "Reference": r.get("reference") or "", "Error": r.get("error") or ""} for r in runs],
                     hide_index=True, use_container_width=True)
    else:
        st.caption("No runs recorded yet.")
    st.divider()
    st.markdown("##### Backup and restore")
    st.download_button("⬇ Export this configuration", store.export_json(cfg_now, meta.get("version")),
                       file_name=f"content-compiler-config-v{meta.get('version')}.json", mime="application/json")
    up = st.file_uploader("Restore from an exported file",
                          type=["json"], key="cfg_upload")
    if up is not None and ss.get("cfg_upload_done") != up.file_id:
        try:
            ss["cfg_upload_done"] = up.file_id
            _request_load(ss, store.import_json(up.getvalue().decode("utf-8")))
            ss["cfg_notice"] = "Imported. Review the changes below, then click Apply."
            st.rerun()
        except (ConfigError, UnicodeDecodeError) as e:
            st.error(str(e))


# ---------------------------------------------------------------------------
# footer: pending changes, validation, save / apply, history
# ---------------------------------------------------------------------------
def _footer(ss, cfg_now, active_cfg, meta):
    errors, warnings = validate(cfg_now)
    changes = S.diff(active_cfg, cfg_now)
    st.divider()
    st.markdown("### Review and apply")
    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"**Pending changes** ({len(changes)})")
        st.markdown(_changes_html(changes), unsafe_allow_html=True)
    with right:
        tokens = rules_tokens(cfg_now) + OUTPUT_RESERVE
        budget = int(cfg_now["provider"]["groq_tpm"])
        share = min(tokens / budget, 1.0)
        st.markdown("**Rules size vs Groq budget**")
        st.progress(
            share, text=f"about {tokens:,} of {budget:,} tokens ({int(tokens / budget * 100)}%)")
    if errors:
        st.markdown("".join(f'<div class="issue"><div class="no">!</div><div>{html.escape(e)}</div></div>' for e in errors),
                    unsafe_allow_html=True)
    if warnings:
        st.markdown("".join(
            f'<div class="warn-box">⚠️ {html.escape(w)}</div>' for w in warnings), unsafe_allow_html=True)
    if not errors:
        st.markdown(
            '<div class="ok-box">✅ This configuration is valid.</div>', unsafe_allow_html=True)
    if ss.get("cfg_notice"):
        st.info(ss.pop("cfg_notice"))

    note = st.text_input(
        "What did you change and why? (saved with the version)", key="cfg_note", max_chars=300)
    b1, b2, b3, b4 = st.columns(4)
    if b1.button("✓ Validate", use_container_width=True):
        (st.toast("Configuration is valid", icon="✅") if not errors else st.toast(
            f"{len(errors)} problem(s) to fix", icon="⚠️"))
    if b2.button("Save draft", use_container_width=True):
        store.save_draft(cfg_now)
        st.toast("Draft saved. It is not applied yet.", icon="💾")
    b3.button("Discard changes", use_container_width=True,
              on_click=lambda: (store.clear_draft(), _request_load(ss)))
    if b4.button("Apply to main page", type="primary", use_container_width=True, disabled=bool(errors) or not changes):
        try:
            n = store.apply(cfg_now, note, "admin")
            store.clear_draft()
            # a widget cannot be changed after it is drawn: clear it on the next run
            ss["cfg_clear_note"] = True
            _request_load(ss)
            ss["cfg_notice"] = f"Applied as version v{n}. The main page uses it from the next run."
            st.rerun()
        except ConfigError as e:
            st.error("Could not apply: " + "; ".join(e.errors))
    if not changes and not errors:
        st.caption("Nothing to apply: the settings match the active version.")

    st.markdown("### Version history")
    versions = store.list_versions()
    for v in versions[:20]:
        n = v["version"]
        tag = " · **active**" if n == meta.get("version") else ""
        with st.expander(f"v{n}{tag} · {v['created_at']} · {v['author']} · {v['note'] or 'no note'}"):
            st.markdown(_changes_html(store.changes_vs_previous(n)) if n > 1 else '<div class="muted">First version.</div>',
                        unsafe_allow_html=True)
            if n != meta.get("version"):
                def do_rollback(n=n):
                    try:
                        new = store.rollback(n, "admin")
                        _request_load(ss)
                        ss["cfg_notice"] = f"Rolled back to the settings of v{n}. Saved as v{new}."
                    except ConfigError as e:
                        ss["cfg_notice"] = "Could not roll back: " + \
                            "; ".join(e.errors)
                st.button(f"Roll back to v{n}",
                          key=f"rb_{n}", on_click=do_rollback)


# ---------------------------------------------------------------------------
def render(ss, admin_password=None, app_password_set=False, verify=None):
    """Draw the Configuration screen. Returns nothing; all state lives in session_state."""
    st.markdown('<div class="sec-head" style="text-align:left;margin-top:.4rem"><div class="eyebrow">SYSTEM CONFIGURATION</div>'
                '<h2 style="margin:.2rem 0">Control and maintain the content system</h2>'
                '<p style="margin:0;max-width:720px">Change rules, limits and behaviour here. The main page uses the active '
                'version on its next run. No code changes or restarts needed.</p></div>', unsafe_allow_html=True)

    if admin_password and not ss.get("config_authed"):
        with st.container(key="login_card"):
            st.markdown('<div class="login-head"><h2>Admin access</h2><p>Enter the configuration password to continue.</p></div>',
                        unsafe_allow_html=True)
            with st.form("config_login"):
                pw = st.text_input("Configuration password", type="password")
                submitted = st.form_submit_button(
                    "Unlock", use_container_width=True)
                if submitted:
                    if verify(pw, admin_password):
                        # Continue rendering in the same Streamlit run instead of
                        # forcing a rerun while the form widget tree is still live.
                        # This also avoids a Streamlit AppTest SessionState race.
                        ss["config_authed"] = True
                    else:
                        time.sleep(1)
                        st.error("Incorrect password.")
        if not ss.get("config_authed"):
            return
    if not admin_password and app_password_set:
        st.warning(
            "Anyone with the app password can change these settings. Set CONFIG_PASSWORD to restrict this page.")

    settings, meta = store.load_active()
    active_cfg = settings.to_dict()
    if ss.pop("cfg_clear_note", False):
        ss["cfg_note"] = ""
    cfg_paths = list(S.flatten(S.default_config()).keys())
    needs_seed = ss.get("cfg_needs_seed", True) or any(
        K(path) not in ss for path in cfg_paths
    )

    if needs_seed:
        seed_widgets(ss, _baseline(ss))
        ss["cfg_needs_seed"] = False

    banner = (f'<div class="cfgbar"><b>Active version v{meta.get("version") or "?"}</b>'
              f'<span>{html.escape(str(meta.get("created_at") or ""))} · {html.escape(str(meta.get("author") or ""))} · '
              f'{html.escape(str(meta.get("note") or "no note"))}</span></div>')
    st.markdown(banner, unsafe_allow_html=True)
    if store.load_draft():
        st.caption(
            "📝 A saved draft was loaded. It is not active until you apply it.")

    cfg_now = collect(ss, active_cfg)
    tabs = st.tabs([f"{icon} {label}" for _, label, icon in S.SECTIONS])
    with tabs[0]:
        _tab_rules(ss, cfg_now)
    with tabs[1]:
        _tab_research(ss, cfg_now)
    with tabs[2]:
        _tab_generation(ss, cfg_now)
    with tabs[3]:
        _tab_qa(ss, cfg_now)
    with tabs[4]:
        _tab_system(ss, cfg_now, meta)
    with tabs[5]:
        _tab_maintenance(ss, cfg_now, meta)
    _footer(ss, collect(ss, active_cfg), active_cfg, meta)
