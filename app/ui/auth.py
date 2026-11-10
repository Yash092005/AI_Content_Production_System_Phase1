"""Optional shared-password gate. If APP_PASSWORD is not set the app is open (fine for localhost).
When deployed on a public URL, set it: otherwise anyone with the link can spend your API quota."""
import hmac
import os

MAX_ATTEMPTS = 5


def configured_password(env=None, secrets=None, name="APP_PASSWORD"):
    """The password called `name` (APP_PASSWORD for the app, CONFIG_PASSWORD for the Configuration page) from the
    environment, or from Streamlit secrets. Empty/blank means 'no gate'."""
    env = os.environ if env is None else env
    pw = (env.get(name) or "").strip()
    if not pw and secrets is not None:
        try:
            pw = str(secrets.get(name, "") or "").strip()
        except Exception:
            pw = ""
    return pw or None


def verify(supplied, expected):
    """Constant-time comparison so timing cannot be used to guess the password."""
    if not expected:
        return True
    return hmac.compare_digest(str(supplied or "").encode("utf-8"), str(expected).encode("utf-8"))


def provider_status(env=None):
    """What the header badge shows. Mirrors the provider choice in app.ai.get_clients without calling any API."""
    env = os.environ if env is None else env
    mode = (env.get("MODE") or "auto").lower()
    from app.ai.keys import any_key
    g, r = any_key("gemini", env), any_key("groq", env)
    if mode == "mock":
        return {"state": "demo", "label": "Demo mode", "detail": "Mock AI. Output is placeholder text."}
    if mode == "gemini_only":
        return ({"state": "live", "label": "Live · Gemini", "detail": "Gemini only."} if g else
                {"state": "error", "label": "Setup needed", "detail": "MODE=gemini_only but GEMINI_API_KEY is missing."})
    if mode == "groq_only":
        return ({"state": "live", "label": "Live · Groq", "detail": "Groq only."} if r else
                {"state": "error", "label": "Setup needed", "detail": "MODE=groq_only but GROQ_API_KEY is missing."})
    if mode == "live":
        return ({"state": "live", "label": "Live · Gemini + Groq", "detail": "Gemini first, Groq as backup."} if g and r else
                {"state": "error", "label": "Setup needed", "detail": "MODE=live needs both GEMINI_API_KEY and GROQ_API_KEY."})
    if g and r:
        return {"state": "live", "label": "Live · Gemini + Groq", "detail": "Gemini first, Groq as backup."}
    if g:
        return {"state": "live", "label": "Live · Gemini", "detail": "Gemini only."}
    if r:
        return {"state": "live", "label": "Live · Groq", "detail": "Groq only."}
    return {"state": "demo", "label": "Demo mode", "detail": "No API keys found. Output is placeholder text."}
