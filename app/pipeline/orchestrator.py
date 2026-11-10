from app.strategy import (develop_strategy, framework_block, framework_block_compact, length_block_compact, research_note, framework_check, length_block,
                          research_depth_note, length_info, length_gate_issues, recommended_claims, DEFAULT_LENGTH, layout_prompt, layout_spec,
                          revamp_coverage_issues, revamp_coverage_block)
from app.strategy.frameworks import labels_to_markers
from app.qa.flagged import flagged_quotes, remove_flagged_sentences, process_wording_issues
from app.reference.profile import style_block, style_block_compact
from app.reference.engine import analyse_reference, copy_issues, public_view, style_warnings, usable as usable_reference
from app.config import runtime
from app.research.sources import check_sources
from app.qa.validator import validate
from app.research.engine import research
from app.rules.loader import (load_rules, rules_status, all_rules_loaded, build_system_prompt, build_stage_system_prompt,
                              rules_scoping_mode)
from app.strategy.length import target_words as _target_words
from app.pipeline.source_digest import extraction_prompt, build_digest, digest_block
from app.ai.base import ProvidersUnavailable
from app.ai.keys import STAGE_PURPOSE
from app.ai import get_clients
import hashlib
import json
import os
from pathlib import Path
import re
import time
from dotenv import load_dotenv
load_dotenv()


# Gemini is the primary provider for all stages; Groq is the automatic fallback.
STAGE_MODEL = {
    "research": "gemini",
    "research_blueprint": "gemini",
    "blueprint": "reasoning",
    "generate": "gemini",
    "qa": "reasoning",
    "revise": "gemini",
    "extract": "gemini",
}


REVAMP_REMINDER = (
    "REVAMP TASK. Apply the revamp rules in the Master Instructions and Canonical Specification: the layout "
    "is preserved, but headings, copy and claims are re-evaluated, and the SOURCE PAGE is input to verify, "
    "not truth. Mandatory removals apply to meaning, not only wording."
)
TIME_PATTERN = re.compile(r"\b\d{1,2}[:.]\d{2}\s?(?:AM|PM|am|pm)\b")
GROUND_RULES = (
    " Use web search for current facts. Put every fact you could confirm in verified_claims as "
    "[{claim, source_url, evidence_excerpt}]. evidence_excerpt MUST be an exact supporting excerpt copied from the cited source, "
    "5-40 words, sufficient to support the claim. If you cannot provide an exact supporting excerpt from the source, mark the claim UNVERIFIED "
    "and do not put it in verified_claims. A reachable URL alone is never proof. Never assume the subject (temple, deity, city, brand) if the request and source "
    "do not state it: if the SUBJECT itself is unknown, list it in needs_input. needs_input must be an empty "
    "list in every other case. Missing details about Naman's own service at this temple (price, inclusions, "
    "booking URL, exact availability) are NOT blocking: write only the approved service definition from the "
    "Brand Constitution, state none of those details, and list them in required_information so the team can "
    "supply them later. Never invent them. Return valid JSON only: no comments, no text outside the JSON. "
    "Keep every string under 20 words and every legacy_dispositions reason under 12 words. Put sections, "
    "verified_claims and needs_input FIRST in the JSON."
)
SERVICE_SENTENCE_START = "Missing details about Naman's own service"
SERVICE_SENTENCE_END = "Never invent them."
NEUTRAL_SENTENCE = (
    "This is an informational topic that does not ask about any commercial service: do NOT list service, "
    "pricing, booking, VIP darshan or brand details in required_information or claims_to_verify."
)
SERVICE_REQUEST_TOKENS = ("naman darshan", "naman puja",
                          "guided vip", "vip darshan", "darshan assistance")


def _require_model_output(out, stage):
    """Reject successful HTTP responses that contain no usable content for writer stages."""
    if stage in {"generate", "revise"} and not str(out or "").strip():
        raise RuntimeError(f"{stage} returned an empty model response")
    return out


def _ground_rules(service_intent):
    """Service-gap instructions only apply when the task is actually about a Naman service."""
    if service_intent:
        return GROUND_RULES
    a = GROUND_RULES.find(SERVICE_SENTENCE_START)
    b = GROUND_RULES.find(SERVICE_SENTENCE_END, a)
    if a < 0 or b < 0:
        return GROUND_RULES
    return GROUND_RULES[:a] + NEUTRAL_SENTENCE + GROUND_RULES[b + len(SERVICE_SENTENCE_END):]


def _request_has_service_intent(request, mode):
    """Intent comes from what the USER asked, never from the model's own blueprint output."""
    return mode == "Revamp" or any(t in str(request or "").lower() for t in SERVICE_REQUEST_TOKENS)


def _verdict_issues(verdict, limit=8):
    """Turn the QA model's free-text FAIL verdict into a list of readable issues for the UI.
    A line ending in ':' that has indented bullets under it is a GROUP HEADING, not an issue: its children become
    issues prefixed with the heading, so the count matches what the person actually has to fix."""
    raw = [l.rstrip() for l in str(verdict or "").splitlines() if l.strip()]
    if not raw:
        return []
    m = re.match(
        r"^[^A-Za-z]*(?:PASS|FAIL)\b[\s:.\-\u2013\u2014*]*(.*)$", raw[0].strip(), re.I)
    rest = [m.group(1).strip()] if m and m.group(1).strip() else []
    body = raw[1:] if m else raw
    entries = [(0, r) for r in rest] + \
        [(len(l) - len(l.lstrip()), l.strip()) for l in body]
    bullet = re.compile(r"^(?:[*\-\u2022]|\d+[.)])\s+")
    has_bullets = any(bullet.match(t) for _, t in entries)
    out, header, header_indent, header_used = [], None, -1, True

    def add(text):
        if text and text not in out:
            out.append(text[:300])

    for indent, text in entries:
        if has_bullets and not bullet.match(text):
            continue
        t = re.sub(r"\*{1,2}", "", bullet.sub("", text)).strip()
        if not t:
            continue
        if header is not None and indent <= header_indent:
            if not header_used:
                add(header + ":")
            header = None
        if t.endswith(":") and len(t) <= 120:
            if header is not None and not header_used:
                add(header + ":")
            header, header_indent, header_used = t.rstrip(
                ":").strip(), indent, False
            continue
        if header is not None:
            header_used = True
            t = f"{header}: {t}"
        add(t)
    if header is not None and not header_used:
        add(header + ":")
    return out[:limit]


def FRAMEWORKS_STAGES(strategy):
    from app.strategy import FRAMEWORKS
    return FRAMEWORKS.get(strategy.get("framework"), {}).get("stages", [])


SOURCE_CAP = 12000  # characters of the existing page passed to the model


def _is_pass(verdict):
    """Tolerates formatting like '**PASS**' or 'PASS.' from the model."""
    return re.sub(r"^[^A-Za-z]+", "", verdict or "").upper().startswith("PASS")


def _text_items(value):
    if value is None:
        return []
    if isinstance(value, dict):
        return [json.dumps(value, ensure_ascii=False).lower()]
    if isinstance(value, list):
        return [json.dumps(x, ensure_ascii=False).lower() if isinstance(x, (dict, list)) else str(x).lower() for x in value]
    return [str(value).lower()]


SERVICE_TOKENS = (
    "naman darshan", "naman puja", "guided vip", "vip darshan",
    "vip darshan assistance", "service availability", "service scope",
    "booking url", "booking", "price", "pricing", "inclusion", "availability",
    "pickup", "locker", "senior care", "senior-friendly", "coordinator",
    "pandit", "platform", "service provider", "service providers",
    "confirmation", "cta", "guided darshan",
)


def _service_evidence_state(blueprint):
    """Brand-approved terminology is vocabulary, not proof of temple-specific availability."""
    reasons = []
    for label in ("required_information", "claims_to_verify"):
        for text in _text_items(blueprint.get(label, []) or []):
            if any(token in text for token in SERVICE_TOKENS):
                reasons.append(f"{label}: unresolved service information")
                break
    notes = str(blueprint.get("research_notes", "")).lower()
    gap_words = ("could not verify", "not verified", "needs input", "not confirmed",
                 "confirmation required", "returned 404")
    if any(w in notes for w in gap_words) and any(t in notes for t in SERVICE_TOKENS):
        reasons.append("research_notes: service verification gap")
    return bool(reasons), list(dict.fromkeys(reasons))


def _contains_negated_service(text):
    low = text.lower()
    patterns = (
        r"(not|no|cannot|can't|unable to|could not|do not|does not|isn't|is not)\s+(?:currently\s+)?(?:provide|offer|confirm|guarantee|verify|include|arrange|book|sell|facilitate)",
        r"(?:service|availability|booking|price|inclusions?)\s+(?:is|are)\s+(?:not|unverified|unknown|unconfirmed)",
        r"(?:could not|unable to)\s+verify",
        r"requires?\s+(?:current|direct|official)\s+confirmation",
    )
    return any(re.search(p, low) for p in patterns)


def _new_content_scope_issues(draft, service_intent):
    """Keep unrelated New Content topics free of leaked Naman/brand-service copy."""
    if service_intent or not draft:
        return []
    low = str(draft).lower()
    patterns = (
        r"\bnaman\s+darshan\b",
        r"\bnaman\s+puja\b",
        r"\bguided\s+vip\s+darshan\b",
        r"\bvip\s+darshan\s+assistance\b",
        r"\babout\s+naman\b",
        r"\bnaman\b.{0,120}\b(?:service|support|booking|customer support|cta)\b",
    )
    if any(re.search(pattern, low, re.I | re.S) for pattern in patterns):
        return ["unrelated Naman/brand service content leaked into New Content"]
    return []


def _service_promotion_issues(draft, unresolved_service):
    """Block positive temple-specific service promotion when availability is unresolved."""
    if not unresolved_service or not draft:
        return []

    sentences = re.split(r"(?<=[.!?])\s+", draft)
    positive_patterns = (
        r"\bnaman darshan\s+(?:provides|offers|arranges|facilitates|providing|offers you)\b",
        r"\bnaman\s+(?:provides|offers|arranges|facilitates)\b.{0,100}\b(?:vip|darshan|guide|booking|service)\b",
        r"\b(?:signature|standard|typical|general|core)\s+services?\s+(?:generally\s+)?include\b",
        r"\bservices?\s+(?:generally\s+)?include\b.{0,120}\b(?:guided vip|vip darshan|darshan assistance|booking|guide|coordinator)\b",
        r"\bguided vip darshan\b.{0,100}\b(?:book|available|provides|offers|arrange|select)\b",
        r"\bvip darshan assistance\b.{0,100}\b(?:available|provides|offers|book|arrange)\b",
        r"\b(?:assigned|dedicated|local)\s+(?:pandit|guide|coordinator|representative)\b",
        r"\b(?:select|choose)\s+(?:your\s+)?(?:date|slot|package)\b.{0,100}\b(?:confirmation|booking|book)\b",
        r"\bbooking\s+(?:form|process|portal|link|url|calendar)\b",
        r"\b(?:confirmation|confirmed)\s+(?:email|booking|slot)\b",
        r"\b(?:price|pricing|package|inclusions?)\s+(?:includes?|starts?|is|of)\b",
    )
    # Approved terminology may appear in a neutral verification note, but that
    # does not make the underlying service a verified fact.
    neutral_service_patterns = (
        r"\b(?:availability|details?|pricing|inclusions?|booking|service)\b.{0,180}"
        r"\b(?:confirm|confirmed|confirmation|unverified|not verified|not confirmed|requires?|pending)\b",
        r"\b(?:confirm|confirmed|confirmation|unverified|not verified|not confirmed|requires?|pending)\b.{0,180}"
        r"\b(?:availability|details?|pricing|inclusions?|booking|service)\b",
    )
    for sentence in sentences:
        if _contains_negated_service(sentence):
            continue
        low = sentence.lower()
        # A question heading such as "How can I book a Guided VIP Darshan?"
        # is not itself a service-availability claim. The following answer is
        # evaluated separately and must carry the verification-pending language.
        if ("guided vip darshan" in low or "vip darshan assistance" in low) and sentence.strip().endswith("?"):
            continue
        if "guided vip darshan" in low or "vip darshan assistance" in low:
            # If the whole sentence is explicitly verification-pending, it is
            # informational rather than a service-availability claim. Check this
            # before generic booking/price patterns below.
            if any(re.search(p, low, re.I | re.S) for p in neutral_service_patterns):
                continue
            return ["unverified temple-specific Naman service claim survives"]
        if any(re.search(p, sentence, re.I | re.S) for p in positive_patterns):
            return ["unverified temple-specific Naman service claim survives"]
    return []


def _revamp_layout_issues(draft, blueprint_sections, mode):
    """Keep the source section slots in order during Revamp."""
    if mode != "Revamp" or not draft:
        return []
    sections = [str(x).strip()
                for x in (blueprint_sections or []) if str(x).strip()]
    if not sections or any(x.lower().startswith("[mock") for x in sections):
        return []
    normalized_draft = re.sub(r"[^a-z0-9\s]", " ", draft.lower())
    normalized_draft = re.sub(r"\s+", " ", normalized_draft).strip()
    missing = []
    positions = []
    for section in sections:
        # Heading may be prefixed with numbering/markdown or lightly formatted.
        title = re.sub(r"[^a-z0-9\s]", " ", section.lower())
        title = re.sub(r"\s+", " ", title).strip()
        pos = normalized_draft.find(title) if title else -1
        if pos < 0:
            missing.append(section)
        else:
            positions.append((section, pos))
    issues = []
    if missing:
        issues.append(f"revamp layout section missing: {', '.join(missing)}")
    if not missing and any(positions[i][1] > positions[i + 1][1] for i in range(len(positions) - 1)):
        issues.append("revamp layout section order changed")
    return issues


def _source_revamp_sections(source_text):
    """Derive the Revamp section contract from the supplied page, not from the LLM.

    Revamp must preserve the existing page's structural slots. The model may rewrite
    content, but it must not invent a new section architecture.
    """
    text = str(source_text or "")
    low = text.lower()
    sections = []

    # Hero is a structural slot even when the source export has no literal
    # ``Hero`` heading. A page title + opening/CTA area is its hero slot.
    title_match = re.search(
        r"(?im)^\s*(?:\d+\s*)?(Jagannath Puri Temple Darshan)\s*$", text)
    if title_match:
        sections.append("Hero")

    patterns = [
        r"(?im)^\s*About Jagannath Puri Temple Darshan\s*$",
        r"(?im)^\s*Jagannath Puri Temple Darshan & Seva Timings\s*$",
        r"(?im)^\s*Types of Darshan at Jagannath Puri Temple\s*$",
        r"(?im)^\s*Why Devotees Trust Naman Darshan\??\s*$",
        r"(?im)^\s*What Naman Darshan Provides\s*$",
        r"(?im)^\s*How We Arrange Your Darshan\s*$",
        r"(?im)^\s*Jagannath Puri Temple Pilgrim Reviews\s*$",
        r"(?im)^\s*Jagannath Puri Temple Info & FAQs\s*$",
    ]
    canonical = [
        "About Jagannath Puri Temple Darshan",
        "Jagannath Puri Temple Darshan & Seva Timings",
        "Types of Darshan at Jagannath Puri Temple",
        "Why Devotees Trust Naman Darshan?",
        "What Naman Darshan Provides",
        "How We Arrange Your Darshan",
        "Jagannath Puri Temple Pilgrim Reviews",
        "Jagannath Puri Temple Info & FAQs",
    ]
    for pat, heading in zip(patterns, canonical):
        if re.search(pat, text):
            sections.append(heading)

    return sections


def _est_tokens(system, prompt):
    """Same estimate the Groq client uses (chars/4 plus 1500 reserved for the reply), so we trim BEFORE it refuses."""
    return int((len(system or "") + len(prompt)) / 4) + 1500


def _token_budget(stage=None):
    """Safe input+reserved-output budget used before a Groq call.

    Revision gets a smaller default budget because it is a fallback-heavy stage and
    must leave headroom for the provider's reserved completion tokens. The Groq
    client still enforces GROQ_TPM; this preflight keeps oversized revision prompts
    away from that hard limit.
    """
    base = int(int(runtime.get("provider.groq_tpm", "GROQ_TPM", 8000))
               * 0.97)   # 3% margin: estimates are approximate
    if stage == "revise":
        revision_cap = int(runtime.get(
            "provider.groq_revision_tpm", "GROQ_REVISION_TPM", 6500))
        return min(base, revision_cap)
    return base


def _json_fit(items, max_chars):
    """JSON list that keeps whole items only (never cuts a claim in half) until max_chars is reached."""
    out, size = [], 2
    for it in items or []:
        n = len(json.dumps(it, ensure_ascii=False)) + 1
        if size + n > max_chars:
            break
        out.append(it)
        size += n
    return json.dumps(out, ensure_ascii=False)


def _claims_fit(items, max_chars):
    """Compact verified evidence for revision: keep claim text, drop source URLs and metadata."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            claim = str(it.get("claim", "")).strip()
        else:
            claim = str(it).strip()
        if not claim:
            continue
        candidate = out + [{"claim": claim[:300]}]
        encoded = json.dumps(candidate, ensure_ascii=False)
        if len(encoded) > max_chars:
            break
        out = candidate
    return json.dumps(out, ensure_ascii=False)


def _fit(stage, system, builders, trims):
    """Pick the fullest prompt that fits the stage budget, otherwise fail before the provider call.

    Builders must be ordered from fullest to leanest. This is intentionally a hard
    preflight: sending an oversized final builder to Groq only guarantees a 400-like
    budget failure after Gemini fallback has already been attempted.
    """
    budget = _token_budget(stage)
    last_est = None
    for level, build in enumerate(builders):
        prompt = build()
        last_est = _est_tokens(system, prompt)
        if last_est <= budget:
            if level:
                print(
                    f"[pipeline] {stage}: prompt trimmed to fit the token budget (level {level})", flush=True)
                trims.append(
                    f"{stage}: trimmed to fit the token budget (level {level})")
            return prompt
    raise RuntimeError(
        f"{stage} prompt remains too large after compression: about {last_est} tokens "
        f"vs safe budget {budget}. Shorten the draft/context or raise the stage budget if the account permits it."
    )


class PlannedPrompt(str):
    """A prompt plus evidence-safe alternatives (instruction compaction only). The router tries the alternatives only when
    the full prompt does not fit any provider with the required headroom; evidence is never removed from them."""
    alternatives = ()


def _planned(full, alternatives):
    p = PlannedPrompt(full)
    p.alternatives = tuple(a for a in alternatives if a and a != full)
    return p


def _strip_internal_source_placeholders(text):
    """Remove internal evidence placeholders that must never reach published content."""
    text = str(text or "")
    patterns = [
        r"\s*\[(?:source_url|source\s*[_-]?\d+)\]",
        r"\s*\bsource_url\b",
        r"\s*\bevidence_excerpt\b",
    ]
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.I)
    return labels_to_markers(re.sub(r"[ \t]{2,}", " ", text))


def _compact(text, max_chars):
    text = str(text or "")
    return text if len(text) <= max_chars else text[:max_chars] + "\n[QA CONTEXT TRUNCATED]"


JSON_KEYS = ("sections", "required_information", "claims_to_verify", "verified_claims", "needs_input",
             "research_notes", "legacy_dispositions")


def _clean_json(t):
    """Removes // comments (outside strings) and trailing commas, which models sometimes add."""
    out, i, in_str, esc = [], 0, False, False
    while i < len(t):
        c = t[i]
        if in_str:
            out.append(c)
            esc = (not esc) if c == "\\" else False
            if c == '"' and not esc:
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == "/" and t[i:i + 2] == "//":
            while i < len(t) and t[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


def _parse_json(text):
    """Takes the JSON out of a model reply. Repairs comments/trailing commas; if the reply was cut off,
    recovers every key that is still complete instead of losing everything."""
    if "{" not in text:
        return {"raw": text}
    try:
        return json.loads(_clean_json(text[text.index("{"):text.rindex("}") + 1]))
    except Exception:
        pass
    cleaned, found = _clean_json(text[text.index("{"):]), {}
    for key in JSON_KEYS:
        m = re.search(rf'"{key}"\s*:\s*', cleaned)
        if m:
            try:
                found[key], _ = json.JSONDecoder(
                ).raw_decode(cleaned[m.end():])
            except Exception:
                pass
    if found:
        found["parse_warning"] = "Model JSON was malformed or cut off; recovered fields: " + \
            ", ".join(found)
        return found
    return {"raw": text}


def _normalize_new_content_sections(blueprint, content_type, level):
    """Keep AI-generated blueprints topic-specific while guaranteeing the minimum useful structure.

    This is intentionally small: it never replaces the model's section choices. It only adds missing
    closing/FAQ slots that make the requested content type usable and auditable.
    """
    if not isinstance(blueprint, dict) or not isinstance(blueprint.get("sections"), list):
        return blueprint
    sections = [str(x).strip()
                for x in blueprint.get("sections", []) if str(x).strip()]
    lower = {re.sub(r"[^a-z0-9]+", " ", x.lower()).strip() for x in sections}
    if content_type == "Blog":
        if not any("faq" in x or "frequently asked" in x for x in lower):
            sections.append("Frequently Asked Questions")
        if not any(x in lower for x in {"conclusion", "final thoughts", "key takeaways"}):
            sections.append("Conclusion")
    elif content_type == "Homepage":
        if not any("cta" in x or "get started" in x or "contact" in x for x in lower):
            sections.append("Get Started")
    elif content_type == "CTA Page":
        if not any("cta" in x or "get started" in x or "next step" in x for x in lower):
            sections.append("Next Steps")
    # Keep the contract within the level-specific budget.
    if content_type in {"Blog", "Homepage", "CTA Page"}:
        from app.strategy.layout import LAYOUTS
        spec = LAYOUTS[content_type]
        max_sections = spec["max_sections"].get(
            level, spec["max_sections"]["Standard"])
        sections = sections[:max_sections]
    blueprint["sections"] = sections
    return blueprint


def _run_pipeline_impl(request, mode, mandatory_removals=None, task_constraints=None,
                       rules_dir=None, clients=None, source_text=None, on_stage=None, strategy=None, length=DEFAULT_LENGTH, extra_facts=None,
                       reference_url=None, reference=None, stage_cache=None, progress=None):
    progress = progress if progress is not None else {}

    def notify(stage, state):
        if on_stage:
            try:
                on_stage(stage, state)
            except Exception:
                pass

    def qc(name):  # a QA check can be switched off in the Configuration tab (the evidence gate cannot)
        return runtime.get_bool(f"qa.checks.{name}", None, True)

    def merged(*lists):  # order kept, duplicates (case-insensitive) dropped
        seen, out = set(), []
        for lst in lists:
            for x in lst or []:
                x = str(x).strip()
                if x and x.lower() not in seen:
                    seen.add(x.lower())
                    out.append(x)
        return out

    # Per-run inputs STACK on top of the configured defaults. Restricted phrases are enforced like mandatory removals.
    removals = merged(mandatory_removals, runtime.get("rules.mandatory_removals", None, []),
                      runtime.get("rules.restricted_phrases", None, []))
    extra_facts = merged(runtime.get(
        "research.trusted_facts", None, []), extra_facts)
    writing_constraints = str(runtime.get(
        "rules.writing_constraints", None, "") or "").strip()
    if writing_constraints:
        task_constraints = "\n".join(x for x in (
            task_constraints, "WRITING CONSTRAINTS (system configuration):\n" + writing_constraints) if x)

    def stage_note(stage):
        t = str(runtime.get(
            f"system.stage_instructions.{stage}", None, "") or "").strip()
        return f"\nADDITIONAL INSTRUCTIONS FOR THIS STAGE (system configuration): {t}\n" if t else ""

    max_revisions = int(runtime.get("qa.max_revisions", "MAX_REVISIONS", 2))
    # fewer calls for free-tier quotas
    lean = runtime.get_bool("system.lean_mode", "PIPELINE_LEAN", False)
    rules = load_rules(rules_dir)
    status = rules_status(rules)

    if not all_rules_loaded(rules):
        return {"status": "RULES_MISSING", "live": False, "rules": status, "blueprint": {},
                "draft": "", "qa": {"status": "FAIL", "issues": ["all_rules_loaded"]}, "revisions": 0}

    source = (source_text or "").strip()[:SOURCE_CAP]
    if mode == "Revamp":
        task_constraints = "\n".join(x for x in (
            task_constraints, REVAMP_REMINDER) if x)
    def source_block(text):
        return f"\nSOURCE PAGE (existing content to revamp):\n{text}\n" if text else ""
    src_block = source_block(source)
    dispo = (" Also include legacy_dispositions: for every material claim or module in the SOURCE PAGE give "
             "{proposition, disposition (retain|verify_and_retain|rewrite|relocate|merge|remove|replace), reason}."
             if source else "")

    request_service = _request_has_service_intent(request, mode)
    clients = clients or get_clients()
    progress["_clients"] = clients
    live = all(c.live for c in clients.values())

    # CONTENT DEVELOPMENT -> FRAMEWORK SELECTION: strategy decides HOW to communicate (never what is true).
    notify("strategy", "start")
    try:
        strategy = develop_strategy(request, mode, client=clients.get(
            "strategy") or clients.get("context") or clients.get("reasoning"), user_strategy=strategy)
    except Exception as e:  # last-resort guard: strategy must never block a run
        print(f"[pipeline] strategy: FAILED -> {e}", flush=True)
        strategy = develop_strategy(request, mode, client=None)
    notify("strategy", "done")
    print(f"[pipeline] strategy: {strategy['content_type']} / {strategy['user_intent']} -> {strategy['framework']} "
          f"({strategy['source']})", flush=True)
    strat_note = research_note(strategy)
    strat_block = framework_block(strategy, mode)
    strat_c = framework_block_compact(strategy, mode)
    len_c = length_block_compact(length, strategy['content_type'], mode)
    trims = []
    len_block = length_block(length, strategy['content_type'], mode)
    strat_note += research_depth_note(length,
                                      strategy['content_type'], mode) + stage_note('research')
    strat_block += stage_note('generate')
    strat_c += stage_note('generate')

    # OPTIONAL reference-website layer. It learns HOW the site writes, never WHAT is true, and it fails soft:
    # if the site cannot be studied the run goes on without it and the result says why.
    ref_used, ref_info = None, None
    if reference is not None or str(reference_url or "").strip():
        notify("reference", "start")
        ref_info = reference if reference is not None else analyse_reference(
            reference_url, client=clients.get("context") or clients.get("reasoning"))
        notify("reference", "done")
        if usable_reference(ref_info):
            ref_used = ref_info
            strat_block += style_block(ref_used, mode)
            strat_c += style_block_compact(ref_used, mode)
            print(f"[pipeline] reference: studied {ref_used['host']} ({ref_used['source']}"
                  f"{', cached' if ref_used.get('cached') else ''})", flush=True)
        else:
            print(
                f"[pipeline] reference: not used -> {(ref_info or {}).get('error')}", flush=True)
    ref_fail_note = [f"The reference website was not used: {(ref_info or {}).get('error') or 'it could not be studied'}. "
                     "The content was written without it."] if ref_info is not None and ref_used is None else []
    notify("rules", "done")
    system = build_system_prompt(rules, task_constraints)      # the full documents
    scoping = rules_scoping_mode()
    _sys_cache = {}

    def system_for(stage):
        """Rules for a stage: its sections only ('all'), or the full documents ('groq'/'off': Gemini keeps everything)."""
        if scoping != "all":
            return system
        if stage not in _sys_cache:
            _sys_cache[stage] = build_stage_system_prompt(rules, task_constraints, stage, scoped=True)
        return _sys_cache[stage]

    def groq_system_for(stage):
        """A narrower excerpt used only when the call goes to Groq (tight limits); None = same text as system_for."""
        if scoping == "off":
            return None
        text = build_stage_system_prompt(rules, task_constraints, stage, scoped=True)
        return text if text != system_for(stage) else None

    cache_on = runtime.get_bool(
        "system.pipeline_cache", "PIPELINE_CACHE", True)
    cache_dir = Path(runtime.get("system.cache_dir", "CACHE_DIR", ".cache"))
    tw = _target_words(length, strategy["content_type"])

    def client_for(stage):
        # Each stage runs on its PURPOSE's client (own API key). Callers that pass only gemini/reasoning clients get the old routing.
        return clients.get(STAGE_PURPOSE.get(stage)) or clients[STAGE_MODEL[stage]]

    def call(stage, prompt, system_override=None, words=None):
        t0 = time.time()
        client = client_for(stage)
        effective_system = system_for(stage) if system_override is None else system_override
        # Cache real model outputs, so a rerun after a failure resumes instead of re-spending quota. A copy is kept in
        # memory too (stage_cache, owned by the caller), so resuming works even when the disk is wiped by a restart.
        key = hashlib.sha256(
            f"{stage}\n{effective_system}\n{prompt}".encode()).hexdigest()
        cached = cache_dir / f"{stage}_{key[:24]}.txt"
        if cache_on and client.live and stage_cache is not None and key in stage_cache:
            print(f"[pipeline] {stage}: reused saved result (no API call)", flush=True)
            notify(stage, "start")
            notify(stage, "done")
            return stage_cache[key]
        if cache_on and client.live and cached.exists():
            print(
                f"[pipeline] {stage}: reused saved result (no API call)", flush=True)
            notify(stage, "start")
            notify(stage, "done")
            return cached.read_text(encoding="utf-8")
        print(f"[pipeline] {stage}: started...", flush=True)
        notify(stage, "start")
        try:
            if hasattr(client, "preview"):      # routed client: it plans tokens/limits and picks the provider
                extra = {"target_words": words or (tw if stage in ("research", "research_blueprint", "blueprint", "generate", "revise") else None),
                         "alt_prompts": list(getattr(prompt, "alternatives", ()) or ())}
                if system_override is None:
                    extra["groq_system"] = groq_system_for(stage)
                out = client.generate(str(prompt), system=effective_system, stage=stage, **extra)
            else:
                out = client.generate(prompt, system=effective_system, stage=stage)
            # A provider can technically return HTTP 200 with an empty content
            # field. Never treat that as a successful generation/revision and
            # never cache an empty answer. The caller can then fail safely or
            # keep the previous draft during revision.
            _require_model_output(out, stage)
        except Exception as e:
            print(
                f"[pipeline] {stage}: FAILED after {time.time() - t0:.0f}s -> {e}", flush=True)
            notify(stage, "error")
            raise
        print(
            f"[pipeline] {stage}: done in {time.time() - t0:.0f}s", flush=True)
        notify(stage, "done")
        if cache_on and client.live:
            if stage_cache is not None:
                stage_cache[key] = out
            cache_dir.mkdir(exist_ok=True)
            cached.write_text(out, encoding="utf-8")
        return out

    def fit_prompt(stage, sys_, builders, trims, alts=(1,)):
        """Routed clients: the full prompt goes in, with evidence-safe alternatives, and the planner picks a provider that fits
        (evidence is never trimmed). Single-provider modes keep the earlier trimming ladder."""
        if hasattr(client_for(stage), "preview"):
            return _planned(builders[0](), [builders[i]() for i in alts if i < len(builders)])
        return _fit(stage, sys_, builders, trims)

    raw_text_holder = [""]
    digest_report = {}
    # placeholder provider; grounded research happens in the Gemini call below
    base = research(request)
    research_client = client_for("research")
    routed = hasattr(research_client, "preview")

    def merged_prompt(src_text):
        return (f"Mode: {mode}\nTask: {request}{strat_note}{layout_prompt(strategy['content_type'], length, mode)}{source_block(src_text)}\nFirst research what is needed (mark anything "
                "you cannot verify as UNVERIFIED), then return ONLY a JSON blueprint with keys "
                "sections, required_information, claims_to_verify, verified_claims, needs_input, research_notes."
                + _ground_rules(request_service) + dispo)

    def research_json_prompt(block):
        return (f"Mode: {mode}\nTask: {request}{strat_note}{layout_prompt(strategy['content_type'], length, mode)}{block}\nResearch what is needed "
                "(mark anything you cannot verify as UNVERIFIED), then return ONLY a JSON object with keys "
                "verified_claims, claims_to_verify, needs_input, research_notes." + _ground_rules(request_service))

    # One merged research+blueprint call is allowed only where its preferred route has the required headroom. On the search
    # route that is limited (Groq) it is split: research (small prompt, search) then blueprint (Gemini, full context).
    use_merged = lean
    if lean and routed:
        pv = research_client.preview("research_blueprint", system_for("research_blueprint"), merged_prompt(source), tw,
                                     groq_system=groq_system_for("research_blueprint"))
        use_merged = pv["fits"]
        head = f"{pv['plan'].min_headroom:.0%}" if pv.get("plan") else "n/a"
        print(f"[pipeline] research route: {'merged' if use_merged else 'split'} (preferred {pv.get('candidate')}, headroom {head})", flush=True)
    split = routed and not use_merged
    if use_merged:  # research + blueprint in ONE call
        raw = call("research_blueprint", merged_prompt(source))
        raw_text_holder[0] = raw
        blueprint = _parse_json(raw)
        notes = blueprint.get("research_notes", raw) if isinstance(
            blueprint, dict) else raw
    elif split:
        block = source_block(source)
        if source:
            pv = research_client.preview("research", system_for("research"), research_json_prompt(block), tw,
                                         groq_system=groq_system_for("research"))
            if not pv["fits"]:
                # The page is too large for the preferred (small-limit) search route: a large-context model lists every fact
                # of it verbatim and a deterministic check re-adds anything with a number it missed. No evidence is dropped.
                try:
                    res = build_digest(source, call("extract", extraction_prompt(source), system_override=""))
                    digest_report.update(res.report, accepted=res.ok, reasons=res.reasons)
                    if res.ok:
                        block = digest_block(res.text)
                        print(f"[pipeline] source page condensed to a verified verbatim digest ({len(source)} -> {len(res.text)} chars; "
                              f"{res.report['kept_by_check']} units kept by the fact check, "
                              f"{res.report['model_lines_dropped_not_in_page']} model line(s) dropped as not in the page)", flush=True)
                    else:
                        print(f"[pipeline] source digest rejected ({'; '.join(res.reasons)}): the full page is used instead", flush=True)
                except Exception as e:      # extraction is an optimisation: fall back to the page itself
                    print(f"[pipeline] extract: skipped -> {str(e)[:120]}", flush=True)
        research_raw = call("research", research_json_prompt(block))
        raw_text_holder[0] = research_raw
        research_json = _parse_json(research_raw)
        notes = research_json.get("research_notes", research_raw) if isinstance(research_json, dict) else research_raw
        blueprint = _parse_json(call("blueprint", (
            f"Mode: {mode}\nTask: {request}\n{layout_prompt(strategy['content_type'], length, mode)}\n"
            f"Research (JSON: verified claims, claims to verify, notes):\n{research_raw}\n{src_block}"
            "Return a JSON blueprint with keys sections, required_information, claims_to_verify, verified_claims, needs_input. "
            "Keep every verified_claims entry from the research exactly as given (claim, source_url, evidence_excerpt) "
            "and add none that the research does not contain."
            + _ground_rules(request_service) + dispo)))
        if isinstance(blueprint, dict) and isinstance(research_json, dict):
            for k in ("verified_claims", "claims_to_verify", "needs_input"):
                if not blueprint.get(k) and research_json.get(k):
                    blueprint[k] = research_json[k]      # never lose what the research found
    else:
        notes = call("research", f"Mode: {mode}\nTask: {request}{strat_note}{layout_prompt(strategy['content_type'], length, mode)}{src_block}\nResearch what is needed and list "
                     f"sources. Mark anything you cannot verify as UNVERIFIED.")
        blueprint = _parse_json(call("blueprint", f"Mode: {mode}\nTask: {request}\n{layout_prompt(strategy['content_type'], length, mode)}\nResearch:\n{notes}\n"
                                     "Return a JSON blueprint with keys sections, "
                                     "required_information, claims_to_verify, verified_claims, needs_input."
                                     + _ground_rules(request_service) + dispo))
    if not isinstance(blueprint, dict):
        blueprint = {"raw": blueprint}
    blueprint.update({"mode": mode, "request": request,
                     "research_notes": notes, "evidence_mode": "strict"})

    # In Revamp, the source page is authoritative for STRUCTURE only. Do not let
    # the research model invent/rename the section architecture. This is the
    # deterministic layout contract that the writer and QA must follow.
    if mode == "Revamp" and source:
        source_sections = _source_revamp_sections(source)
        if source_sections:
            blueprint["sections"] = source_sections

    if mode == "New Content":
        blueprint = _normalize_new_content_sections(
            blueprint, strategy["content_type"], length)

    if "[NOTE: web search unavailable" in (raw_text_holder[0] + str(notes)):
        blueprint["grounding"] = "UNGROUNDED: facts below are unverified"
    blueprint.setdefault("verified_claims", base.get("facts", []))
    progress["blueprint"] = blueprint

    if "sections" not in blueprint:  # reply was unreadable or cut off: writing now would produce a page with no evidence
        return {"status": "BLUEPRINT_FAILED", "live": live, "rules": status, "blueprint": blueprint,
                "draft": "", "revisions": 0, "fallbacks": [e for c in {id(c): c for c in clients.values()}.values()
                                                            for e in getattr(c, "events", [])],
                "qa": {"status": "NOT_RUN", "issues": ["blueprint_unreadable"], "evaluated": live}}

    if live and runtime.get_bool("research.source_check", "SOURCE_CHECK", True):
        print("[pipeline] checking cited sources and evidence excerpts...", flush=True)
        check_sources(blueprint)

    # Evidence sufficiency: a target length must never pressure the writer to
    # invent missing facts. If the verified evidence set is thin, do one targeted
    # expansion search before generation. The expansion returns only additional
    # evidence; the existing blueprint structure remains authoritative.
    evidence_sufficiency = {"status": "not_applicable"}
    if mode == "New Content" and length in ("Short", "Standard", "In-depth"):
        target = int(length_info(
            "", length, strategy["content_type"], mode)[0]["target"])
        recommended = recommended_claims(target)
        minimum = max(4, int(recommended * 0.6 + 0.999))
        current_count = len(blueprint.get("verified_claims", []) or [])
        evidence_sufficiency = {
            "status": "sufficient" if current_count >= minimum else "thin",
            "verified_claims": current_count,
            "recommended_claims": recommended,
            "minimum_claims": minimum,
            "target_words": target,
        }
        if live and current_count < minimum:
            expansion_prompt = (
                f"Evidence expansion for: {request}\n"
                f"Content type: {strategy['content_type']}. Target: about {target} words.\n"
                f"Current verified claims: {current_count}. We need additional source-backed evidence without repeating these claims.\n"
                f"Existing sections: {json.dumps(blueprint.get('sections', []), ensure_ascii=False)}\n"
                f"Existing verified claims: {json.dumps(blueprint.get('verified_claims', []), ensure_ascii=False)}\n"
                "Search authoritative sources for additional distinct facts that genuinely support the requested sections. "
                "Return ONLY JSON with keys verified_claims, claims_to_verify, research_notes. "
                "Each verified_claims item MUST contain claim, source_url and evidence_excerpt. "
                "evidence_excerpt must be an exact 5-40 word excerpt from the cited source. "
                "Do not guess or use model knowledge. If a claim cannot be supported by an exact source excerpt, mark it UNVERIFIED. "
                + _ground_rules(request_service)
            )
            try:
                expanded = _parse_json(
                    call("research_blueprint", expansion_prompt))
                print("[DEBUG] Expansion result:", json.dumps(
                    expanded, ensure_ascii=False, indent=2), flush=True)
                if isinstance(expanded, dict):
                    expanded["evidence_mode"] = "strict"
                    if live and runtime.get_bool("research.source_check", "SOURCE_CHECK", True):
                        check_sources(expanded)
                        print("[DEBUG] Verified after source check:", len(
                            expanded.get("verified_claims", [])), flush=True)
                        print("[DEBUG] Dropped claims:", json.dumps(expanded.get(
                            "dropped_claims", []), ensure_ascii=False, indent=2), flush=True)
                    existing = {
                        str(c.get("claim", c)).strip().lower()
                        for c in (blueprint.get("verified_claims", []) or [])
                    }
                    additions = []
                    for claim in expanded.get("verified_claims", []) or []:
                        key = str(claim.get("claim", claim)).strip().lower() if isinstance(
                            claim, dict) else str(claim).strip().lower()
                        if key and key not in existing:
                            additions.append(claim)
                            existing.add(key)
                    blueprint["verified_claims"] = list(
                        blueprint.get("verified_claims", []) or []) + additions
                    blueprint["claims_to_verify"] = list(dict.fromkeys(
                        list(blueprint.get("claims_to_verify", []) or []) +
                        list(expanded.get("claims_to_verify", []) or [])
                    ))
                    current_count = len(blueprint.get(
                        "verified_claims", []) or [])
                    evidence_sufficiency["verified_claims"] = current_count
                    evidence_sufficiency["status"] = "sufficient" if current_count >= minimum else "thin"
                    evidence_sufficiency["expansion_attempted"] = True
                    evidence_sufficiency["added_claims"] = len(additions)
            except Exception as e:
                evidence_sufficiency["expansion_attempted"] = True
                evidence_sufficiency["expansion_error"] = str(e)
    blueprint["evidence_sufficiency"] = evidence_sufficiency

    # In New Content mode, brand/commercial setup fields are optional. They must not
    # block an informational article: omit those elements rather than invent them.
    # Revamp remains strict because the existing page may depend on missing identity/
    # operating-model information. Any genuinely factual ambiguity still blocks.
    needs_input = blueprint.get("needs_input", []) or []
    deferred_input = []
    if mode == "New Content" and needs_input:
        optional_new_content = (
            "property selection", "target-audience", "target audience", "persona",
            "approved cta", "cta wording", "call-to-action", "destination url",
            "seo keyword", "seo keywords", "brand-approved seo", "disclaimer",
            "responsibility note"
        )
        blocking = []
        for item in needs_input:
            text = str(item).lower()
            if any(token in text for token in optional_new_content):
                deferred_input.append(item)
            else:
                blocking.append(item)
        if deferred_input:
            blueprint["deferred_input"] = deferred_input
            blueprint["needs_input"] = blocking
        needs_input = blocking

    if needs_input:  # genuinely mandatory input is missing: ask, don't guess
        return {"status": "NEEDS_INPUT", "live": live, "rules": status, "blueprint": blueprint,
                "draft": "", "questions": needs_input, "revisions": 0,
                "qa": {"status": "NOT_RUN", "issues": [], "evaluated": live}}

    if runtime.get_bool("research.require_source_url", None, False):
        before = blueprint.get("verified_claims", []) or []
        kept = [c for c in before if not isinstance(
            c, dict) or str(c.get("source_url", "")).strip()]
        if len(kept) != len(before):
            blueprint["dropped_without_source_url"] = len(before) - len(kept)
        blueprint["verified_claims"] = kept
    evidence = list(blueprint.get("verified_claims", []) or [])
    # Facts the requester vouches for count as verified evidence for BOTH the writer and QA.
    provided = [str(f).strip()[:300]
                for f in (extra_facts or []) if str(f).strip()][:25]
    if provided:
        have = {(c.get("claim", "") if isinstance(c, dict)
                 else str(c)).strip().lower() for c in evidence}
        evidence += [{"claim": f, "source_url": "provided by requester"}
                     for f in provided if f.lower() not in have]
        blueprint["verified_claims"] = evidence
        blueprint["provided_by_requester"] = provided
    unverified = blueprint.get("claims_to_verify", []) or []
    missing_info = blueprint.get("required_information", []) or []
    # Temple-specific Naman service validation is relevant only when the task
    # actually asks about Naman/temple services. Do not leak Revamp service
    # constraints into unrelated New Content topics such as festival blogs.
    service_intent = request_service
    unresolved_service, service_reasons = (
        _service_evidence_state(blueprint) if service_intent else (False, [])
    )
    revision_system = (
        "You are the revision component of a controlled AI content compiler. "
        "Return only the complete revised content. Use only VERIFIED CLAIMS supplied in the revision prompt as factual evidence. "
        "Do not add facts from memory, inference, or plausible knowledge. Remove unsupported claims rather than replacing them with new claims. "
        "Preserve the requested content type, selected framework, section structure, and useful supported content. "
        "Do not include internal source placeholders such as [source_url] or [source 1]."
    )

    generation_guard = (
        layout_prompt(strategy['content_type'], length, mode) +
        "\nGENERATION EVIDENCE GATE (NON-NEGOTIABLE):\n"
        "1. Treat ONLY the VERIFIED CLAIMS below as factual evidence.\n"
        "2. Do NOT use, paraphrase, infer, complete, or promote anything from claims_to_verify, research_notes, "
        "required_information, or your own knowledge into a factual statement.\n"
        "3. If a factual detail is not supported by VERIFIED CLAIMS, OMIT it. Do not replace it with a plausible detail.\n"
        "4. For rituals, traditions, beliefs, or cultural practices not supported by evidence, either omit the specific detail "
        "or explicitly frame only the general concept as a belief/tradition; never invent steps, offerings, locations, timing, "
        "or customary practices.\n"
        "5. If business/service information is missing, omit the service details rather than inventing price, inclusions, "
        "availability, booking method, coordinators, pickup, support channels, or operational steps.\n"
        "6. Never turn a required_information item into content unless its actual value is present in VERIFIED CLAIMS or approved brand rules.\n"
        "7. Preserve the requested content type and create genuinely new content, but factual novelty is NOT permission to invent facts.\n"
        "7a. Never output internal evidence/source placeholders such as [source_url], [source 1], [source 2], source_url, or evidence_excerpt.\n"
        + ("8. The Naman responsibility note: at most 2 sentences, once, after the service explanation; never in the hero, FAQs or final CTA.\n"
           if service_intent else
           "8. This is unrelated New Content. Do not add Naman Darshan, Naman services, brand-service descriptions, customer-support CTAs, booking language, or other commercial brand content unless the user request explicitly asks for it.\n")
        + "9. If official timings are not in VERIFIED CLAIMS, give no clock times.\n"
        + "10. In Revamp mode preserve every source section slot and their order. Do not remove, merge, rename, or replace source sections; rewrite their contents in place. Do not add layout notes to the published content. Output EVERY section from the Blueprint\"sections\" list as a visible heading, using the section name verbatim. If a section depends on unverified service details, keep the heading and use a short neutral verification-pending note instead of dropping the section.\n"
        "11. Never describe the service in wording that implies priority access, faster entry, reduced waiting or skipping queues.\n"
        "12. Approved brand terminology is vocabulary only. It does NOT prove temple-specific service availability.\n"
        + ("13. SERVICE AVAILABILITY IS UNVERIFIED for this page. Do not promote Guided VIP Darshan, VIP Darshan Assistance, "
           "Naman service availability, Pandit/guide/coordinator assignment, booking/confirmation, pricing, inclusions, "
           "pickup, locker, senior care, or service-specific CTA. You may include a single neutral note that current "
           "service details require confirmation, but do not turn that note into a sales claim.\n"
           if unresolved_service else "")
        + "\n"
        + ("EVIDENCE SUFFICIENCY: The verified evidence set is thinner than the requested depth. Do not pad; write only what is supported and keep the article concise.\n"
           if evidence_sufficiency.get("status") == "thin" and mode != "Revamp" else "")
        + f"VERIFIED CLAIMS (allowed evidence): {json.dumps(evidence, ensure_ascii=False)}\n"
        + f"UNVERIFIED CLAIMS (DO NOT USE AS FACTS): {json.dumps(unverified, ensure_ascii=False)}\n"
        + f"MISSING/OPTIONAL BUSINESS INFO (DO NOT INVENT): {json.dumps(missing_info, ensure_ascii=False)}\n"
    )
    # Generation must not resend bulky research notes, raw source-page dispositions, or parser metadata.
    # Those fields are useful for audit/debugging, but they consume Groq TPM and can crowd out the actual
    # evidence boundary.  Give the writer only the compact blueprint fields it needs to write safely.
    generation_blueprint = {
        "mode": mode,
        "layout_guidance": layout_spec(strategy['content_type'], length, mode),
        "request": request,
        "sections": blueprint.get("sections", []),
        "required_information": blueprint.get("required_information", []),
        "claims_to_verify": blueprint.get("claims_to_verify", []),
        # verified_claims are NOT repeated here: the evidence guard below already carries them once.
        # Sending them twice doubled the biggest part of the prompt and broke Groq's per-minute token budget.
        "needs_input": blueprint.get("needs_input", []),
        "deferred_input": blueprint.get("deferred_input", []),
    }
    generation_blueprint_text = _compact(json.dumps(
        generation_blueprint, ensure_ascii=False), 10000)
    section_heading_guard = ""
    if mode == "New Content":
        section_heading_guard = (
            "\nNEW CONTENT SECTION CONTRACT — REQUIRED.\n"
            "The final draft must visibly cover every Blueprint section below in this order. Use the exact section names as headings. "
            "Do not collapse multiple blueprint sections into one.\n"
            + "\n".join(f"{i+1}. {section}" for i,
                        section in enumerate(blueprint.get("sections", [])))
            + "\n"
        )
    if mode == "Revamp":
        section_heading_guard = (
            "\nREVAMP SECTION CONTRACT — REQUIRED.\n"
            "The final draft MUST contain every Blueprint section below as a visible heading, in exactly this order. "
            "Use each heading verbatim; do not rename, merge, replace, or omit any section.\n"
            + "\n".join(f"{i+1}. {section}" for i,
                        section in enumerate(blueprint.get("sections", [])))
            + "\n"
            "If a section cannot contain service details because evidence is unresolved, keep its heading and write a neutral, "
            "verification-pending sentence. Never delete the section.\n"
        )
    gen_prompt = (f"Mode: {mode}\nTask: {request}\nCompact Blueprint:\n{generation_blueprint_text}\n"
                  f"{strat_block}{len_block}{generation_guard}{section_heading_guard}"
                  f"{revamp_coverage_block(blueprint.get('sections', []), mode)}"
                  f"\nWrite the complete content now. Never include: {_compact(json.dumps(removals, ensure_ascii=False), 1800)}")

    # Generation is the largest prompt in the pipeline. Groq's configured TPM
    # includes both input and reserved output tokens, so a prompt that is only a
    # few hundred tokens over budget must be compressed automatically rather than
    # stopping the whole pipeline. Keep the evidence boundary intact while removing
    # redundant metadata/instructions in progressively leaner builders.
    compact_blueprint = json.dumps({
        "mode": mode,
        "layout_guidance": layout_spec(strategy['content_type'], length, mode),
        "request": request,
        "sections": blueprint.get("sections", []),
    }, ensure_ascii=False)
    evidence_compact = _claims_fit(evidence, 5200)
    unverified_compact = _compact(json.dumps(
        unverified, ensure_ascii=False), 1600)
    missing_compact = _compact(json.dumps(
        missing_info, ensure_ascii=False), 1000)
    guard_compact = (
        "\nEVIDENCE GATE: Use ONLY VERIFIED CLAIMS as factual evidence. "
        "Do not infer or use model knowledge, research notes, required information, or unverified claims. "
        "Do not invent service, pricing, booking, timings, rituals, practices, or operational details. "
        "Never output internal source placeholders. "
        + ("This is unrelated New Content: do not add Naman/brand-service content. " if mode ==
           "New Content" and not service_intent else "")
        + ("Service availability is unverified; do not promote Naman service details. " if unresolved_service else "")
        + ("Evidence is thin: do not pad; write only supported content. " if evidence_sufficiency.get("status") == "thin" else "")
        + f"\nVERIFIED CLAIMS: {evidence_compact}\nUNVERIFIED (DO NOT USE): {unverified_compact}\nMISSING INFO (DO NOT INVENT): {missing_compact}\n"
    )
    compact_sections = (
        "\nSECTION CONTRACT: cover every Blueprint section in this order, using exact names as headings.\n"
        + "\n".join(f"{i+1}. {section}" for i,
                    section in enumerate(blueprint.get("sections", [])))
        + "\n"
    )
    gen_prompt_compact = (
        f"Mode: {mode}\nTask: {request}\nBlueprint: {compact_blueprint}\n"
        f"{strat_c}{len_c}{guard_compact}{compact_sections}"
        f"Write the complete content now. Never include: {_compact(json.dumps(removals, ensure_ascii=False), 900)}"
    )
    # Final emergency builder: retain the full evidence claims but remove all
    # non-essential strategy/metadata text. This should be rare and prevents a
    # small TPM overage (like 7,960 vs 7,760) from aborting a valid run.
    emergency_guard = (
        "\nONLY VERIFIED CLAIMS ARE FACTS. Do not invent or infer. "
        "Do not output internal source placeholders.\n"
        f"VERIFIED CLAIMS: {evidence_compact}\n"
    )
    gen_prompt_emergency = (
        f"Mode: {mode}\nTask: {request}\n"
        f"SECTIONS: {json.dumps(blueprint.get('sections', []), ensure_ascii=False)}\n"
        f"{len_c}{emergency_guard}"
        "Write complete content using the sections above."
    )
    draft = call("generate", fit_prompt("generate", system, [
        lambda: gen_prompt,
        lambda: gen_prompt.replace(
            strat_block, strat_c).replace(len_block, len_c),
        lambda: gen_prompt_compact,
        lambda: gen_prompt_emergency,
    ], trims))
    draft = _strip_internal_source_placeholders(draft)
    revisions = 0
    partial = False
    while True:
        progress["draft"], progress["revisions"] = draft, revisions
        qa = validate(draft, rules, removals)
        # Service QA must never fire for unrelated New Content topics.
        deterministic_service_issues = (
            _service_promotion_issues(draft, unresolved_service)
            if service_intent and qc("service_promotion") else []
        )
        deterministic_new_content_scope_issues = _new_content_scope_issues(
            draft, service_intent
        ) if mode == "New Content" and qc("scope_leak") else []
        deterministic_layout_issues = _revamp_layout_issues(
            draft, blueprint.get("sections", []), mode
        ) if qc("layout") else []
        deterministic_revamp_coverage_issues = revamp_coverage_issues(
            draft, blueprint.get("sections", []), mode
        ) if mode == "Revamp" and qc("layout") else []
        framework_issues, framework_warnings = framework_check(
            draft, strategy, mode)
        if not qc("framework_stages"):
            framework_issues = []
        heading_issues = process_wording_issues(
            draft) if mode == "New Content" and qc("process_wording") else []
        reference_copy = copy_issues(
            draft, ref_used) if ref_used and qc("reference_copy") else []
        reference_warns = style_warnings(
            draft, ref_used) if ref_used and qc("reference_style") else []
        deterministic_issues = (
            heading_issues +
            deterministic_service_issues
            + deterministic_new_content_scope_issues
            + deterministic_layout_issues
            + deterministic_revamp_coverage_issues
            + framework_issues
            + reference_copy
        )
        length_meta, length_warnings = length_info(
            draft, length, strategy["content_type"], mode)
        length_issues = length_gate_issues(
            draft, length, strategy["content_type"], mode, evidence_sufficiency.get(
                "status", "not_applicable")
        ) if qc("length_gate") else []
        qa["warnings"] = framework_warnings + \
            length_warnings + reference_warns + ref_fail_note
        if length_issues:
            deterministic_issues.extend(
                x for x in length_issues if x not in deterministic_issues)
        if deterministic_issues:
            qa["issues"].extend(
                x for x in deterministic_issues if x not in qa["issues"])
            qa["status"] = "FAIL"
        if not blueprint.get("verified_claims") and TIME_PATTERN.search(draft):
            qa["issues"].append("times_stated_without_verified_source")
            qa["status"] = "FAIL"
        if deterministic_issues:
            qa["model_verdict"] = ""
            qa["evaluated"] = live
            if revisions >= max_revisions:
                qa["status"] = "FAIL"
                break
            try:
                def build_det_revise(level):
                    ev = json.dumps(evidence, ensure_ascii=False) if level < 2 else _json_fit(
                        evidence, 3500)
                    blocks = (strat_block +
                              len_block) if level == 0 else (strat_c + len_c)
                    text = (
                        "Fix ALL deterministic failures and return the FULL revised content.\n"
                        f"Failures: {json.dumps(deterministic_issues, ensure_ascii=False)}\n"
                        f"VERIFIED CLAIMS — the ONLY factual evidence allowed: {ev}\n"
                    )
                    if any("Length requirement not met" in str(issue) for issue in deterministic_issues):
                        text += (
                            "LENGTH REPAIR REQUIRED: The current draft is below the minimum acceptable length. "
                            f"Expand the revised content to at least {int(length_meta['target'] * 0.8)} words, "
                            f"aiming for about {length_meta['target']} words. "
                            "Use ONLY VERIFIED CLAIMS. Add useful context, explanations, practical details, "
                            "regional distinctions, and FAQ content supported by those verified claims. "
                            "Do not repeat content, pad the article, or introduce unsupported facts.\n"
                        )
                    if mode == "New Content" and not service_intent:
                        text += (
                            "NEW CONTENT SCOPE: remove all leaked Naman Darshan/brand-service content, including service descriptions, "
                            "customer-support CTAs, booking language, and Naman headings. Keep the article focused on the requested topic.\n"
                        )
                    if unresolved_service:
                        text += (
                            "Service availability is unverified. Remove every temple-specific Naman service promotion, "
                            "booking/confirmation flow, assigned guide/coordinator, pricing/inclusion claim and service CTA. "
                            "Keep service-related section headings but use a neutral verification-pending note.\n"
                        )
                    if mode == "Revamp":
                        text += (
                            "REVAMP LAYOUT CONTRACT: preserve EVERY required section heading below, in EXACTLY this order. "
                            "Do not rename, merge, omit, or replace them. Rewrite content inside the sections only.\n"
                            + "\n".join(f"{i+1}. {x}" for i, x in enumerate(blueprint.get("sections", [])))
                            + "\n"
                        )
                    return text + blocks + f"CURRENT DRAFT:\n{draft}"
                draft = call("revise", fit_prompt("revise", revision_system, [lambda: build_det_revise(0), lambda: build_det_revise(1),
                                                                        lambda: build_det_revise(2)], trims), system_override=revision_system)
            except Exception as e:
                qa["status"] = "FAIL"
                qa["error"] = f"Revision call failed after deterministic QA: {e}"
                partial = True
                break
            draft = _strip_internal_source_placeholders(draft)
            revisions += 1
            continue

        qa_prompt = (
            "Audit this draft against the governing documents. FAIL if any mandatory removal survives in meaning; "
            "any factual claim is outside VERIFIED CLAIMS; a service is promoted without verified temple-specific "
            "availability; brand terminology is broken; wording implies priority access, faster entry or reduced waiting; "
            "or unsupported timing/ritual/operational detail appears. Plain descriptors such as 'traditional' or 'ancient' need no attribution; "
            "ask for attribution only for beliefs, legends, ritual claims or promised outcomes. "
            + ("For unrelated New Content, also FAIL if Naman/brand-service content leaks into the article. "
               if mode == "New Content" and not service_intent else "")
            + (f"Also FAIL if the draft ignores the selected {strategy['framework']} framework "
               f"({' → '.join(FRAMEWORKS_STAGES(strategy))}) or if persuasion adds any claim not in VERIFIED CLAIMS. "
               if mode == "New Content" else "")
            + (
                f"Also FAIL if the draft is below the minimum acceptable length of about {int(length_meta['target'] * 0.7)} words "
                f"for the requested {length} target of ~{length_meta['target']} words, because the evidence set is sufficient. "
                "Expand only from VERIFIED CLAIMS; never pad or invent facts. "
                if length_meta and evidence_sufficiency.get("status") == "sufficient" and length_meta.get("words", 0) < int(length_meta["target"] * 0.7)
                else ""
            )
            + (
                "Also FAIL if this is a Revamp and any section contains only a brief verification-pending stub "
                "with fewer than 2 substantive sentences: each section must have meaningful body content. "
                "The reference website's brevity is NOT a reason for thin sections. "
                if mode == "Revamp"
                else ""
            )
            + "Reply PASS or FAIL on first line, then concise failures.\n"
            f"Mandatory removals: {json.dumps(removals, ensure_ascii=False)}\n"
            f"Verified facts: {json.dumps(evidence, ensure_ascii=False)}\n"
            f"Unverified claims: {json.dumps(unverified, ensure_ascii=False)}\n"
            f"Draft:\n{draft}"
        )
        qa_prompt += stage_note('qa')
        try:
            verdict = call("qa", qa_prompt).strip()
        except Exception as e:
            qa.update(
                status="FAIL", error=f"QA call failed: {e}", model_verdict="", evaluated=False)
            partial = True
            break
        qa["model_verdict"] = verdict
        if not _is_pass(verdict):
            qa["status"] = "FAIL"
            qa["issues"].extend(x for x in _verdict_issues(
                verdict) if x not in qa["issues"])
        if _is_pass(verdict) and qa["status"] == "PASS":
            break
        if revisions >= max_revisions:
            qa["status"] = "FAIL"
            break
        try:
            def build_qa_revise(level):
                v = verdict or json.dumps(qa.get("issues", []))
                flagged = flagged_quotes(qa.get("issues", []))
                delete_line = ("DELETE these sentences outright (QA found no evidence for them): "
                               + json.dumps(flagged, ensure_ascii=False) + "\n") if flagged else ""
                if level == 0:
                    blocks = strat_block + len_block
                    ev = json.dumps(evidence, ensure_ascii=False)
                    un = json.dumps(unverified, ensure_ascii=False)
                    mi = json.dumps(missing_info, ensure_ascii=False)
                    removals_text = json.dumps(removals, ensure_ascii=False)
                elif level == 1:
                    blocks = strat_c + len_c
                    v = _compact(v, 2200)
                    ev = _claims_fit(evidence, 5000)
                    un = _json_fit(unverified, 900)
                    mi = _json_fit(missing_info, 900)
                    removals_text = _compact(json.dumps(
                        removals, ensure_ascii=False), 1800)
                elif level == 2:
                    blocks = strat_c + len_c
                    v = _compact(v, 1500)
                    ev = _claims_fit(evidence, 3500)
                    un = _json_fit(unverified, 700)
                    mi = _json_fit(missing_info, 700)
                    removals_text = _compact(json.dumps(
                        removals, ensure_ascii=False), 1200)
                elif level == 3:
                    # Final compact context: keep the evidence boundary and the draft,
                    # but drop bulky strategy/length/rule metadata because the system
                    # prompt already carries the governing rules.
                    blocks = (
                        "Preserve the existing content type, selected framework, section structure, "
                        "and requested length where possible. Remove only unsupported material.\n"
                    )
                    v = _compact(v, 1200)
                    ev = _claims_fit(evidence, 2200)
                    un = _json_fit(unverified, 350)
                    mi = _json_fit(missing_info, 350)
                    removals_text = _compact(json.dumps(
                        removals, ensure_ascii=False), 900)
                else:
                    # Ultra-compact fallback for Groq: only the actionable QA failures,
                    # a small evidence boundary, and the complete current draft.
                    blocks = "Keep the existing structure/framework. Remove unsupported claims; do not add new facts.\n"
                    v = _compact(v, 700)
                    ev = _claims_fit(evidence, 500)
                    un = "[]"
                    mi = "[]"
                    removals_text = _compact(json.dumps(
                        removals, ensure_ascii=False), 350)

                return (
                    "Fix these failures and return the FULL revised content. Do not merely explain the fixes.\n"
                    f"Failures identified by QA: {v}\n"
                    f"{delete_line}"
                    f"Mandatory removals: {removals_text}\n"
                    f"VERIFIED CLAIMS — the ONLY factual evidence allowed: {ev}\n"
                    f"UNVERIFIED CLAIMS — MUST NOT be used: {un}\n"
                    f"MISSING BUSINESS INFO — MUST NOT be invented: {mi}\n"
                    "Remove unsupported factual/ritual/service details entirely. If service availability is unverified, do not promote "
                    "any temple-specific Naman service even if the Brand Constitution contains approved terminology. "
                    "If a section depends on missing information, rewrite it neutrally or remove the section. "
                    "Do not replace an unsupported detail with another plausible detail. Keep the content useful and original "
                    "while staying inside the evidence gate.\n"
                    f"{blocks}"
                    f"CURRENT DRAFT:\n{draft}"
                )
            # lvl=i binds the level NOW; a bare `i` would be late-bound and make every level identical.
            draft = call("revise", fit_prompt("revise", revision_system, [lambda lvl=i: build_qa_revise(
                lvl) for i in range(5)], trims, alts=()), system_override=revision_system)
            draft = _strip_internal_source_placeholders(draft)
        except Exception as e:  # show the un-revised draft and the QA findings
            qa["status"] = "FAIL"
            qa["error"] = f"Revision call failed, showing the draft BEFORE revision: {e}"
            partial = True
            break
        revisions += 1

    if qa.get("status") == "FAIL" and qa.get("issues") and qc("flagged_removal"):
        cleaned, removed, resolved = remove_flagged_sentences(
            draft, qa["issues"])
        if removed:  # the revision left QA-flagged unsupported sentences in: delete them in code
            draft = cleaned
            qa["auto_removed"] = removed
            qa["issues_resolved_by_code"] = resolved
            qa["issues"] = [i for i in qa["issues"] if i not in resolved]
            qa["note"] = ("Code removed " + str(len(removed)) + " sentence(s) QA flagged as unsupported. "
                          "The model did not re-audit the result; run again to re-check.")
    if qa.get("status") == "FAIL" and not qa.get("issues"):
        parsed = _verdict_issues(qa.get("model_verdict", ""), limit=8)
        qa["issues"] = parsed or [qa.get(
            "error") or "QA failed without a detailed reason. Open Auditor note for the raw verdict."]
    qa["evaluated"] = live  # False = mock run, the QA result is meaningless
    fallbacks = [e for c in {id(c): c for c in clients.values()}.values()
                 for e in getattr(c, "events", [])]
    return {"fallbacks": fallbacks, "status": "PARTIAL" if partial else ("COMPLETE" if live else "MOCK_RUN"), "live": live, "rules": status,
            "blueprint": blueprint, "draft": draft, "qa": qa, "revisions": revisions, "strategy": strategy, "budget_trims": trims, "source_digest": (digest_report or None),
            "reference": ({**public_view(ref_info), "used": ref_used is not None} if ref_info is not None else None),
            "length": length_info(draft, length, strategy["content_type"], mode)[0]}


def run_pipeline(request, mode, mandatory_removals=None, task_constraints=None,
                 rules_dir=None, clients=None, source_text=None, on_stage=None, strategy=None, length=DEFAULT_LENGTH,
                 extra_facts=None, settings=None, reference_url=None, reference=None, stage_cache=None):
    """Run the content pipeline.

    `settings` (app.config.settings.Settings) are the System Configuration to run with. They are active for this
    run only, in this thread only. With none given the pipeline behaves as before: environment variables and defaults.
    The result records `config_version` so any output can be traced to the configuration that produced it.

    `stage_cache` (a dict the caller keeps, e.g. in the browser session) holds finished stage outputs, so a run that
    paused because no provider could safely take a step resumes from there even if the server's disk was reset. If no
    provider can take a step the result has status PAUSED (with the work done so far), instead of an error."""
    active = settings if settings is not None else runtime.current()
    progress = {}
    with runtime.use_settings(active):
        try:
            result = _run_pipeline_impl(request, mode, mandatory_removals, task_constraints, rules_dir, clients, source_text,
                                        on_stage, strategy, length, extra_facts, reference_url, reference, stage_cache, progress)
        except ProvidersUnavailable as e:
            used = progress.get("_clients") or {}
            result = {"status": "PAUSED", "live": True, "rules": {}, "blueprint": progress.get("blueprint", {}),
                      "draft": progress.get("draft", ""), "revisions": progress.get("revisions", 0),
                      "fallbacks": [ev for c in {id(c): c for c in used.values()}.values() for ev in getattr(c, "events", [])],
                      "qa": {"status": "NOT_RUN", "issues": ["providers_at_capacity"], "evaluated": False},
                      "paused": {"stage": e.stage, "reasons": e.reasons, "retry_after": e.retry_after, "message": str(e)}}
    if isinstance(result, dict):
        result["config_version"] = getattr(
            active, "version", None) if active is not None else None
    return result
