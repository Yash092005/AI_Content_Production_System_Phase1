"""Content layout guidance: structure adapts to content type and strategy.

The layout is a blueprint contract, not a fixed article template. The model chooses
specific section names from the topic while preserving the required purpose/slots.
"""

LAYOUTS = {
    "Blog": {
        "purpose": "informational or editorial article",
        "slots": [
            "direct_answer", "context_or_background", "core_topic_details",
            "practical_or_reader_guidance", "faq", "conclusion"
        ],
        "guidance": (
            "Start with a direct answer or useful introduction, then give context/background, develop the core topic, "
            "include practical reader-facing guidance when the evidence supports it, answer likely FAQs, and close with a concise conclusion. "
            "Add or remove topic-specific sections as needed; do not use these slot names as visible headings."
        ),
        "min_sections": {"Short": 5, "Standard": 6, "In-depth": 8},
        "max_sections": {"Short": 8, "Standard": 10, "In-depth": 14},
    },
    "Homepage": {
        "purpose": "brand/product overview",
        "slots": ["hero_value", "value_proposition", "benefits", "proof_or_trust", "differentiators", "cta"],
        "guidance": (
            "Lead with the value proposition, explain key benefits, establish trust/proof only when verified, show meaningful differentiators, "
            "and finish with a clear CTA when an approved CTA exists. Do not force blog-style history or FAQs."
        ),
        "min_sections": {"Short": 4, "Standard": 5, "In-depth": 7},
        "max_sections": {"Short": 7, "Standard": 9, "In-depth": 11},
    },
    "CTA Page": {
        "purpose": "conversion-focused landing or action page",
        "slots": ["problem_or_need", "solution", "benefits", "proof_or_trust", "how_it_works", "cta"],
        "guidance": (
            "Lead with the user's problem or need, present the solution, explain benefits, add verified trust/proof, explain the next step or process, "
            "and end with the approved CTA. Do not invent testimonials, guarantees, prices, or outcomes."
        ),
        "min_sections": {"Short": 4, "Standard": 5, "In-depth": 7},
        "max_sections": {"Short": 7, "Standard": 9, "In-depth": 11},
    },
}


def layout_spec(content_type, level="Standard", mode="New Content"):
    if mode == "Revamp":
        return "REVAMP LAYOUT: preserve the source page's locked section layout exactly."
    spec = LAYOUTS.get(content_type, LAYOUTS["Blog"])
    level = level if level in spec["min_sections"] else "Standard"
    return (
        f"CONTENT LAYOUT ({content_type}; {level}; purpose: {spec['purpose']}):\n"
        f"- Use at least {spec['min_sections'][level]} and at most {spec['max_sections'][level]} meaningful sections.\n"
        f"- Required structural purposes: {', '.join(spec['slots'])}.\n"
        f"- {spec['guidance']}\n"
        "- Section names must be topic-specific and natural. Do not expose slot names, framework stage names, or internal process labels as headings.\n"
        "- The framework controls communication style/order of ideas; the layout controls what sections the page needs. Do not let the framework replace the content blueprint."
    )


def layout_prompt(content_type, level="Standard", mode="New Content"):
    return "\n" + layout_spec(content_type, level, mode) + "\n"
