"""Framework Router: a plain rules table (not an AI call) so the choice is repeatable and testable."""
import re

CONVERSION_OBJECTIVES = {"Drive conversion", "Encourage action", "Generate leads"}
EDUCATE_OBJECTIVES = {"Educate", "Build awareness"}
TRUST_OBJECTIVES = {"Build trust", "Build awareness"}
_PROBLEM = re.compile(r"\b(solve|problem|fix|struggl\w*|avoid|stop|overcome|issue)\b", re.I)
_OUTCOME = re.compile(r"\b(transform\w*|achieve|improve|become|outcome|grow|better|upgrade)\b", re.I)


def _problem_job(s):
    return bool(_PROBLEM.search(s.get("jtbd", "")))


RULES = [
    ("cta_problem", lambda s: s["content_type"] == "CTA Page" and _problem_job(s), "PAS",
     "The page converts by naming a problem the audience feels, showing why it matters, then offering the solution."),
    ("cta_conversion", lambda s: s["content_type"] == "CTA Page"
     and (s["user_intent"] == "Transactional" or s["objective"] in CONVERSION_OBJECTIVES), "AIDA",
     "The content is conversion-focused and needs a clear progression from attention to action."),
    ("blog_problem", lambda s: s["content_type"] == "Blog" and _problem_job(s), "PAS",
     "The reader wants to solve a problem, so the article names it, shows why it matters, then resolves it."),
    ("blog_informational", lambda s: s["content_type"] == "Blog"
     and (s["user_intent"] == "Informational" or s["objective"] in EDUCATE_OBJECTIVES), "BLUF",
     "The reader wants an answer, so the bottom line comes first and the supporting detail follows."),
    ("homepage_outcome", lambda s: s["content_type"] == "Homepage" and bool(_OUTCOME.search(s.get("jtbd", ""))), "BAB",
     "The homepage sells an outcome, so it contrasts the situation before and after and bridges the two."),
    ("homepage_awareness", lambda s: s["content_type"] == "Homepage"
     and (s["relationship_stage"] in {"Awareness", "Consideration"} or s["objective"] in TRUST_OBJECTIVES), "AIDA",
     "The homepage introduces the brand to people still forming an opinion, so it earns attention, builds interest and desire, then points to one action."),
]


def route(strategy):
    """Return {framework, reason, rule}. First matching rule wins; otherwise a documented default."""
    for name, test, fw, reason in RULES:
        if test(strategy):
            return {"framework": fw, "reason": reason, "rule": name}
    if strategy.get("user_intent") == "Informational":
        return {"framework": "BLUF", "rule": "default_informational",
                "reason": "No specific rule matched. Informational intent defaults to answer-first writing."}
    return {"framework": "AIDA", "rule": "default",
            "reason": "No specific rule matched. AIDA is the general-purpose default for non-informational content."}
