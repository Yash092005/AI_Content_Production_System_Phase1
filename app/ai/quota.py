"""Quota groups: Gemini limits are applied per Google PROJECT (and model), Groq limits per ORGANISATION (and model). Several
keys can share one quota, or each have their own; nothing in a key tells us which.

Every key therefore belongs to a quota group:

  declared   you said so: Configuration -> key groups (or GEMINI_KEY_GROUPS / GROQ_KEY_GROUPS="fingerprint=label,...")
  observed   a provider error named the project number / organisation id, or two keys were seen to fail and succeed
             independently within one request
  unverified nothing known. Unverified keys of one provider are treated as ONE shared quota: capacity is never assumed.

Usage, limits and (for verified groups) cooldowns are tracked per (provider, group, model). Keys are never stored here, only the
short one-way fingerprint that the screen already shows."""
import json
import os
import re
import threading

from app.config import runtime

UNVERIFIED = "unverified"
_PROJECT = re.compile(r"projects/(\d{6,})")
_ORG = re.compile(r"organi[sz]ation\s+[`'\"]?(org_[A-Za-z0-9]+)")


def _parse_env_groups(raw):
    out = {}
    for part in re.split(r"[,;\n]+", str(raw or "")):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def declared_groups(provider, env=None):
    """{fingerprint: label} from the configuration and the environment (the environment wins when both name a key)."""
    env = os.environ if env is None else env
    out = {}
    for row in runtime.get("provider.key_groups", None, []) or []:
        if isinstance(row, dict) and str(row.get("provider", "")).lower() == provider and row.get("key") and row.get("group"):
            out[str(row["key"]).strip()] = str(row["group"]).strip()
    out.update(_parse_env_groups(env.get(f"{provider.upper()}_KEY_GROUPS")))
    return out


class Groups:
    def __init__(self):
        self._lock = threading.Lock()
        self._observed = {}     # (provider, key_id) -> (label, evidence)
        self.conflicts = []

    def reset(self):
        with self._lock:
            self._observed.clear()
            self.conflicts.clear()

    def resolve(self, provider, key_id, env=None):
        """-> (label, status). status: declared | observed | unverified."""
        declared = declared_groups(provider, env).get(key_id)
        with self._lock:
            seen = self._observed.get((provider, key_id))
        if declared:
            if seen and seen[0] != declared and not seen[0].startswith("key-"):
                note = f"{provider} key {key_id}: declared group '{declared}' but the provider reported '{seen[0]}'"
                if note not in self.conflicts:
                    self.conflicts.append(note)
            return f"{provider}:{declared}", "declared"
        if seen:
            return f"{provider}:{seen[0]}", "observed"
        return f"{provider}:{UNVERIFIED}", "unverified"

    def label(self, provider, key_id):
        return self.resolve(provider, key_id)[0]

    def verified(self, provider, key_id):
        return self.resolve(provider, key_id)[1] != "unverified"

    def cooldown_scope(self, provider, key_id):
        """A limit error rests the whole group when the group is known; for unverified keys only that key rests, so key
        rotation keeps working (they may or may not share a quota, and trying the next key is what tells us)."""
        label, status = self.resolve(provider, key_id)
        return label if status != "unverified" else f"{label}#key:{key_id}"

    def observe(self, provider, key_id, label, evidence=""):
        with self._lock:
            self._observed[(provider, key_id)] = (str(label), evidence)

    def observe_from_error(self, provider, key_id, exc):
        """Learn the project / organisation from a provider error message, when it names one."""
        text = f"{exc} {getattr(exc, 'body', '')}"
        if provider == "gemini":
            m = _PROJECT.search(text)
            if m:
                self.observe(provider, key_id, f"project-{m.group(1)}", "named in a Google error")
                return f"project-{m.group(1)}"
        elif provider == "groq":
            m = _ORG.search(text)
            if m:
                self.observe(provider, key_id, m.group(1), "named in a Groq error")
                return m.group(1)
        return None

    def separate(self, provider, key_a, key_b, evidence="one key was limited while another succeeded"):
        """Two unverified keys behaved independently: give each its own group."""
        for k in (key_a, key_b):
            if self.resolve(provider, k)[1] == "unverified":
                self.observe(provider, k, f"key-{k}", evidence)

    def snapshot(self, provider, key_ids, env=None):
        rows = []
        for k in key_ids:
            label, status = self.resolve(provider, k, env)
            with self._lock:
                ev = self._observed.get((provider, k), ("", ""))[1]
            rows.append({"provider": provider, "key": k, "group": label.split(":", 1)[1], "status": status, "evidence": ev})
        return rows


GROUPS = Groups()
