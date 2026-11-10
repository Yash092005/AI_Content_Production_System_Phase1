"""Gemini limits that have been verified (shared by the schema default and the planner's fallback). No imports on purpose."""

# Per Google PROJECT and MODEL. Verified in Google AI Studio (free tier) for gemini-3.6-flash: 5 requests/minute,
# 250,000 tokens/minute, 20 requests/day. group 'unverified' = keys whose project is not declared or observed.
GEMINI_LIMITS = [
    {"model": "gemini-3.6-flash", "group": "unverified", "rpm": 5, "tpm": 250000, "rpd": 20,
     "source": "Google AI Studio, free tier (operator screenshot)"},
]
