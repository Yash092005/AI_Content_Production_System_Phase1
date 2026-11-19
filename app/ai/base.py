import json
import time
import requests

RETRY_STATUS = {429, 500, 502, 503, 504}


class ProvidersUnavailable(RuntimeError):
    """No provider can take this request safely right now (limits, cooldowns or no candidate with enough headroom).
    Raised BEFORE a risky call is made, so the pipeline can pause and resume instead of crashing."""

    def __init__(self, stage, reasons, retry_after=None):
        self.stage, self.reasons, self.retry_after = stage, list(reasons or []), retry_after
        wait = f" Try again in about {int(retry_after)}s." if retry_after else ""
        super().__init__(f"No provider can safely run '{stage}' right now: " + "; ".join(self.reasons) + "." + wait)


class CapabilityUnavailable(RuntimeError):
    """The provider or project does not offer a capability (for example Gemini Google Search grounding on the free tier)."""

    def __init__(self, provider, capability, detail=""):
        self.provider, self.capability = provider, capability
        super().__init__(f"{provider} does not offer {capability} for this key/project. {detail}".strip())


class OutputTruncated(RuntimeError):
    """The reply hit the output cap, so it is incomplete. Never treated as a usable answer."""


def _retry_after(headers):
    try:
        return float(str((headers or {}).get("retry-after", "")).strip().rstrip("s"))
    except ValueError:
        return None


class ApiError(RuntimeError):
    def __init__(self, status, body, headers=None):
        self.status, self.body = status, body
        self.retry_after = _retry_after(headers)
        self.quota_metric = self.quota_id = self.retry_delay = ""
        try:  # Google puts the exact limit that was hit inside error.details
            for d in json.loads(body).get("error", {}).get("details", []):
                for v in d.get("violations", []):
                    self.quota_metric = self.quota_metric or v.get("quotaMetric", "")
                    self.quota_id = self.quota_id or v.get("quotaId", "")
                self.retry_delay = self.retry_delay or d.get("retryDelay", "")
        except Exception:
            pass
        hint = ""
        if self.quota_metric or self.quota_id:
            hint = (f" [LIMIT HIT: metric={self.quota_metric or '?'}; id={self.quota_id or '?'}; "
                    f"retry after={self.retry_delay or 'unknown'}]")
        super().__init__(f"HTTP {status}{hint}: {body[:600]}")

    @property
    def is_grounding_quota(self):
        return "ground" in (self.quota_metric + self.quota_id).lower() or \
               "search" in (self.quota_metric + self.quota_id).lower()


KEEP_HEADERS = ("x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens", "x-ratelimit-remaining-requests",
                "x-ratelimit-reset-requests", "x-ratelimit-limit-tokens", "retry-after")


def _pick_headers(r):
    h = getattr(r, "headers", None) or {}
    try:
        return {k: str(h.get(k)) for k in KEEP_HEADERS if h.get(k) is not None}
    except Exception:
        return {}


DAILY_MARKERS = ("perday", "per day", "(tpd)", "(rpd)")
MAX_RETRY_WAIT = 120.0          # a provider asking for longer than this is not waited for inside one request


def _server_wait(headers, body):
    """The wait the provider asked for: the Retry-After header, else Google's RetryInfo.retryDelay ('35s'). None if absent."""
    ra = _retry_after(headers)
    if ra:
        return ra
    try:
        for d in json.loads(body).get("error", {}).get("details", []):
            if d.get("retryDelay"):
                return float(str(d["retryDelay"]).strip().rstrip("s"))
    except Exception:
        pass
    return None


def post_json(url, payload, headers, timeout=120, retries=4, sleep=time.sleep, meta=None):
    """POST with bounded backoff on 429/5xx. When the provider says how long to wait (Retry-After / retryDelay) that wait is
    honoured, longer or shorter than our own backoff (15s, 30s, 45s, 60s); a wait above MAX_RETRY_WAIT, or a DAILY limit
    (Gemini or Groq), is not retried at all. Errors include the provider's message. When `meta` (a dict) is given it
    receives the status and the rate-limit headers (never the request)."""
    for attempt in range(retries + 1):
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        picked = _pick_headers(r)
        if meta is not None:
            meta["status"], meta["headers"] = r.status_code, picked
        daily = r.status_code == 429 and any(m in r.text.lower() for m in DAILY_MARKERS)  # retrying only wastes requests
        if r.status_code in RETRY_STATUS and attempt < retries and not daily:
            asked = _server_wait(picked, r.text) if r.status_code == 429 else _retry_after(picked)
            delay = asked if asked else 15 * (attempt + 1)
            if delay <= MAX_RETRY_WAIT:
                sleep(delay + (1.0 if asked else 0.0))      # +1 s so we arrive after the window reopens, not at its edge
                continue
        if r.status_code >= 400:
            raise ApiError(r.status_code, r.text, picked)
        return r.json()
