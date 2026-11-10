"""Where the quota goes: one record per model call (real usage when the provider reports it), plus the calibration the
budget planner uses (measured characters per token, output size, tool/search overhead).

Never stores prompts, replies or API keys. `key_id` is the short one-way fingerprint already shown in the UI."""
import json
import os
import re
import statistics
import threading
import time
from collections import deque

CHARS_PER_TOKEN = 4.0          # used until enough calls have been measured
MIN_SAMPLES = 5                # measured values replace provisional ones only after this many calls
_SECRET = re.compile(r"(AIza[0-9A-Za-z_\-]{20,}|gsk_[0-9A-Za-z]{16,}|sk-[0-9A-Za-z_\-]{16,}|Bearer\s+\S+)")
FIELDS = ("provider", "model", "stage", "key_id", "group", "ok", "error", "est_in", "chars_in", "out_cap", "tool_reserve", "search",
          "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "thinking_tokens", "duration_s",
          "remaining_tokens", "note")


def redact(text, limit=160):
    return _SECRET.sub("[redacted]", str(text or ""))[:limit]


def _q90(values):
    values = sorted(values)
    if not values:
        return None
    return values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]


class UsageLog:
    def __init__(self, maxlen=2000, clock=time.time):
        self._rows = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.clock = clock
        self.path = None

    def configure(self, path=None):
        """Optional JSON-lines file (best effort: on a host with a temporary disk it simply starts empty after a restart)."""
        self.path = str(path) if path else None

    def reset(self):
        with self._lock:
            self._rows.clear()

    def record(self, **kw):
        row = {k: kw.get(k) for k in FIELDS if kw.get(k) is not None}
        row["ts"] = round(self.clock(), 3)
        if "error" in row:
            row["error"] = redact(row["error"])
        if "note" in row:
            row["note"] = redact(row["note"])
        prompt, comp = row.get("prompt_tokens"), row.get("completion_tokens")
        if row.get("total_tokens") is None and prompt is not None and comp is not None:
            row["total_tokens"] = prompt + comp
        with self._lock:
            self._rows.append(row)
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
            except OSError:
                pass
        return row

    def rows(self, n=None):
        with self._lock:
            rows = list(self._rows)
        return rows[-n:] if n else rows

    # ---- calibration -------------------------------------------------------------------------------------------
    def _ok(self, provider=None, stage=None):
        return [r for r in self.rows() if r.get("ok") and (provider is None or r.get("provider") == provider)
                and (stage is None or r.get("stage") == stage)]

    def chars_per_token(self, provider):
        """Measured characters per input token for this provider (plain calls only: search adds hidden tokens)."""
        ratios = [r["chars_in"] / r["prompt_tokens"] for r in self._ok(provider)
                  if not r.get("search") and r.get("prompt_tokens") and r.get("chars_in")]
        return statistics.median(ratios) if len(ratios) >= MIN_SAMPLES else None

    def completion_p90(self, provider, stage):
        vals = [r["completion_tokens"] for r in self._ok(provider, stage) if r.get("completion_tokens")]
        return _q90(vals) if len(vals) >= MIN_SAMPLES else None

    def tool_overhead_p90(self, provider, stage):
        """Input tokens a search call used beyond what our own text accounts for (the search tool's results)."""
        cpt = self.chars_per_token(provider) or CHARS_PER_TOKEN
        vals = []
        for r in self._ok(provider, stage):
            if r.get("search") and r.get("prompt_tokens") and r.get("chars_in"):
                vals.append(max(0, r["prompt_tokens"] - int(r["chars_in"] / cpt)))
        return _q90(vals) if len(vals) >= MIN_SAMPLES else None

    # ---- reporting ---------------------------------------------------------------------------------------------
    @staticmethod
    def _tokens(r):
        return r.get("total_tokens") or ((r.get("est_in") or 0) + (r.get("out_cap") or 0))

    def summary(self):
        rows = self.rows()
        total = sum(self._tokens(r) for r in rows) or 0

        def group(field):
            out = {}
            for r in rows:
                g = out.setdefault(r.get(field) or "?", {"calls": 0, "failed": 0, "tokens": 0, "cached": 0})
                g["calls"] += 1
                g["failed"] += 0 if r.get("ok") else 1
                g["tokens"] += self._tokens(r)
                g["cached"] += r.get("cached_tokens") or 0
            for g in out.values():
                g["share"] = round(100 * g["tokens"] / total, 1) if total else 0.0
            return out
        return {"calls": len(rows), "tokens": total, "by_provider": group("provider"), "by_stage": group("stage"),
                "by_model": group("model")}


LOG = UsageLog()


def configure_from_env():
    """Called once at start-up: write the JSON-lines log next to the cache unless USAGE_LOG=off."""
    if str(os.getenv("USAGE_LOG", "on")).lower() in ("off", "0", "false", "no"):
        LOG.configure(None)
        return
    base = os.getenv("CACHE_DIR", ".cache")
    try:
        os.makedirs(base, exist_ok=True)
        LOG.configure(os.path.join(base, "usage.jsonl"))
    except OSError:
        LOG.configure(None)
