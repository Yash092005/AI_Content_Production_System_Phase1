"""AI predicts, user overrides. Prediction never blocks the pipeline: any failure falls back to keywords."""
import hashlib
import json
import os
import re
from pathlib import Path

from app.config import runtime
from app.strategy.schema import OPTIONS, DEFAULTS, normalize, FRAMEWORK_CODES, CONTENT_TYPE_ALIASES
from app.strategy.router import route


def _has(text, *words):
    return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)


def heuristic_predict(request, mode="New Content"):
    """Deterministic keyword prediction. Used with no API key, on provider errors, or STRATEGY_PREDICT=heuristic."""
    t = str(request or "").lower()
    notes = []
    landing = next((k for k in ("landing page", "product page", "service page") if k in t), None)
    if _has(t, "homepage", "home page"):
        ctype = "Homepage"
    elif _has(t, "cta", "call to action", "landing page", "product page", "service page", "sign up", "signup"):
        ctype = "CTA Page"
        if landing:
            notes.append(f"{landing.title()} is not supported yet; using CTA Page.")
    elif mode == "Revamp":
        ctype = "CTA Page" if _has(t, "darshan", "book", "booking", "service") else "Homepage"
    else:
        ctype = "Blog"
    audience = "First-time visitor"
    if _has(t, "existing customer", "existing customers", "our customers", "returning", "current customers"):
        audience = "Existing customer"
    elif _has(t, "decision maker", "decision makers", "cto", "ceo", "manager", "managers", "buyers", "procurement"):
        audience = "Decision maker"
    market = "B2B" if _has(t, "b2b", "enterprise", "businesses", "business", "saas", "companies", "teams") else "B2C"
    intent = {"Blog": "Informational", "Homepage": "Navigational", "CTA Page": "Transactional"}[ctype]
    if _has(t, "compare", "comparison", "vs", "versus", "best", "alternatives", "pricing"):
        intent = "Commercial"
    problem = _has(t, "how to", "fix", "solve", "problem", "avoid", "overcome", "struggling")
    stage = {"Blog": "Awareness", "Homepage": "Awareness", "CTA Page": "Decision"}[ctype]
    if audience == "Existing customer":
        stage = "Existing Customer"
    elif intent == "Commercial" or _has(t, "compare", "consider"):
        stage = "Consideration"
    objective = {"Blog": "Educate", "Homepage": "Build awareness", "CTA Page": "Drive conversion"}[ctype]
    if _has(t, "leads", "lead generation"):
        objective = "Generate leads"
    elif _has(t, "trust", "credibility"):
        objective = "Build trust"
    if landing:  # commercial pages: the visitor is still evaluating (matches the Content Development brief)
        intent, stage, objective = "Commercial", "Consideration", "Generate leads"
    if problem:
        jtbd = "I want to solve a particular problem"
    elif landing:
        jtbd = "I want to evaluate the solution"
    else:
        jtbd = {"Informational": "I want to understand this topic", "Commercial": "I want to compare options",
                "Transactional": "I want to take the next step", "Navigational": "I want to find the right place on this site"}[intent]
    out = normalize({"content_type": ctype, "market_type": market, "audience": audience, "relationship_stage": stage,
                     "user_intent": intent, "jtbd": jtbd, "objective": objective})
    out["notes"] = notes + out["notes"]
    return out


def _stage_note():
    t = str(runtime.get("system.stage_instructions.strategy", None, "") or "").strip()
    return f"\nADDITIONAL INSTRUCTIONS (system configuration): {t}" if t else ""


def _prompt(request, mode):
    lists = "\n".join(f"- {k}: {' | '.join(v)}" for k, v in OPTIONS.items())
    return (
        "Classify this content request for a content strategy. Return ONLY a JSON object, no prose, no markdown fences.\n"
        "Choose every value EXACTLY from its list:\n" + lists + "\n"
        "- jtbd: one short sentence in the reader's voice, starting with 'I want to'\n"
        "Supported content types are only Blog, Homepage and CTA Page. If the request asks for another type "
        "(landing page, product page, service page) choose the closest supported one.\n"
        f"Mode: {mode}\nRequest: {request}" + _stage_note()
    )


def _extract_json(text):
    t = re.sub(r"```(?:json)?", "", str(text or "")).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        v = json.loads(t[a:b + 1])
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def _ai_predict(request, mode, client):
    prompt = _prompt(request, mode)
    cache_on = runtime.get_bool("system.pipeline_cache", "PIPELINE_CACHE", True)
    cached = Path(runtime.get("system.cache_dir", "CACHE_DIR", ".cache")) / f"strategy_{hashlib.sha256(prompt.encode()).hexdigest()[:24]}.txt"
    if cache_on and cached.exists():
        raw = cached.read_text(encoding="utf-8")
    else:
        raw = client.generate(prompt, system=None, stage="strategy")
        if cache_on:  # like every other stage: a rerun must not re-spend quota, even after a bad answer
            cached.parent.mkdir(exist_ok=True)
            cached.write_text(raw, encoding="utf-8")
    data = _extract_json(raw)
    if data is None:
        raise ValueError("strategy prediction was not valid JSON")
    return normalize(data, base=heuristic_predict(request, mode))


EXPLICIT_TYPE_WORDS = ("blog", "article", "post", "guide", "homepage", "home page", "cta", "call to action",
                       "landing page", "product page", "service page", "sign up", "signup")


def heuristic_confident(request, mode="New Content"):
    """True when the keyword prediction is trustworthy, so no model call is needed. A Revamp takes its type from the existing
    page (a model would only see the same request text); a new piece is confident when the request names its type."""
    if mode == "Revamp":
        return True
    return _has(str(request or "").lower(), *EXPLICIT_TYPE_WORDS)


def predict_fields(request, mode="New Content", client=None):
    """Return (fields, source) where source is 'ai' or 'heuristic'.

    'ai' (default) is heuristic-first: the model is asked only when the request is ambiguous. 'always_ai' asks every time
    (the previous behaviour). 'heuristic' never asks."""
    setting = str(runtime.get("generation.strategy_predict", "STRATEGY_PREDICT", "ai")).strip().lower()
    want_ai = setting == "always_ai" or (setting not in ("heuristic", "always_ai") and not heuristic_confident(request, mode))
    if want_ai and client is not None and getattr(client, "live", False):
        try:
            return _ai_predict(request, mode, client), "ai"
        except Exception as e:  # provider limits, bad JSON, anything: never block the pipeline
            fields = heuristic_predict(request, mode)
            fields["notes"].append(f"AI prediction failed ({str(e)[:120]}); used keyword prediction.")
            return fields, "heuristic"
    return heuristic_predict(request, mode), "heuristic"


def finalize(fields, source, framework=None, framework_source="router"):
    """Attach the framework (router choice unless the user picked one) and its reason."""
    s = {k: fields[k] for k in OPTIONS}
    s["jtbd"] = fields["jtbd"]
    routed = route(s)
    enabled = runtime.get("generation.enabled_frameworks", None, FRAMEWORK_CODES) or FRAMEWORK_CODES
    default_fw = runtime.get("generation.default_framework", None, "Auto")
    if framework in FRAMEWORK_CODES and framework_source == "user":
        s.update(framework=framework, framework_source="user", rule="user_override",
                 framework_reason=f"Selected manually. The router would have chosen {routed['framework']}: {routed['reason']}")
    elif default_fw in FRAMEWORK_CODES and default_fw in enabled:
        s.update(framework=default_fw, framework_source="router", rule="configured_default",
                 framework_reason=f"{default_fw} is the default framework in the system configuration. "
                                  f"The router would have chosen {routed['framework']}: {routed['reason']}")
    elif routed["framework"] not in enabled:
        fw = next((f for f in FRAMEWORK_CODES if f in enabled), routed["framework"])
        s.update(framework=fw, framework_source="router", rule="enabled_fallback",
                 framework_reason=f"{routed['framework']} is switched off in the system configuration, so {fw} is used. {routed['reason']}")
    else:
        s.update(framework=routed["framework"], framework_source="router", rule=routed["rule"],
                 framework_reason=routed["reason"])
    s["source"] = source
    s["notes"] = list(fields.get("notes", []))
    return s


def develop_strategy(request, mode="New Content", client=None, user_strategy=None):
    """Content Development layer. If the UI already has (edited) fields, those win; otherwise predict."""
    if isinstance(user_strategy, dict) and any(k in user_strategy for k in OPTIONS):
        fields = normalize(user_strategy)
        return finalize(fields, user_strategy.get("source") or "user",
                        framework=user_strategy.get("framework"),
                        framework_source=user_strategy.get("framework_source", "router"))
    fields, source = predict_fields(request, mode, client)
    return finalize(fields, source)
