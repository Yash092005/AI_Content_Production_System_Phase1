import requests

RETRYABLE = {429, 500, 502, 503, 504}
UNGROUNDED_NOTE = "\n[NOTE: web search unavailable; research is ungrounded, treat all facts as UNVERIFIED]"
RESEARCH_STAGES = ("research", "research_blueprint")


class EmptyResponseError(RuntimeError):
    """Provider returned HTTP success but no usable model content."""


def should_fallback(e):
    """Fall back on rate limits, overload and network failures. NOT on bad keys or bad requests (400/401/403/404)."""
    status = getattr(e, "status", None)
    if isinstance(e, EmptyResponseError):
        return True
    if status is not None:
        return status in RETRYABLE
    return isinstance(e, requests.exceptions.RequestException)


class FallbackClient:
    """Tries the primary provider; if it is rate-limited or overloaded, uses the secondary."""
    live = True

    def __init__(self, primary, secondary):
        self.primary, self.secondary = primary, secondary
        self.name = f"{primary.name}->{secondary.name}"
        self.events = []

    def available(self):
        return True

    def generate(self, prompt, system=None, stage=None):
        try:
            out = self.primary.generate(prompt, system=system, stage=stage)
            if not str(out or "").strip():
                raise EmptyResponseError(f"{self.primary.name} returned an empty model response")
            return out
        except Exception as e:
            if not should_fallback(e):
                raise
            msg = f"{self.primary.name} failed at '{stage}' ({str(e)[:160]}); using {self.secondary.name}"
            print(f"[fallback] {msg}", flush=True)
            self.events.append(msg)
            try:
                out = self.secondary.generate(prompt, system=system, stage=stage)
                if not str(out or "").strip():
                    raise EmptyResponseError(f"{self.secondary.name} returned an empty model response")
            except Exception as e2:
                raise RuntimeError(f"Both providers failed. {self.primary.name}: {e} | "
                                   f"{self.secondary.name}: {e2}") from e2
            if stage in RESEARCH_STAGES and not getattr(self.secondary, "grounded_research", False):
                out += UNGROUNDED_NOTE  # the backup has no web search, so facts stay unverified
            return out
