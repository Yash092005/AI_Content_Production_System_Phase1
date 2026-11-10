"""Versioned configuration on disk: data/config/versions/vNNNN.json, active.json (pointer), draft.json.

Every Apply creates a NEW numbered version (history is append-only), and a rollback is also a new version, so
nothing is ever overwritten or lost. Writes are atomic (temp file + replace)."""
import json
import os
import time
from pathlib import Path

from app.config import schema as S
from app.config.settings import Settings
from app.config.validate import ConfigError, validate

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "config"


def base_dir(path=None):
    return Path(path or os.getenv("CONFIG_DIR") or DEFAULT_DIR)


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _vpath(base, n):
    return base / "versions" / f"v{int(n):04d}.json"


def list_versions(path=None):
    """Newest first: [{version, created_at, author, note}] (no config bodies, so it stays light)."""
    base = base_dir(path)
    out = []
    for p in sorted((base / "versions").glob("v*.json")) if (base / "versions").exists() else []:
        try:
            d = _read(p)
            out.append({k: d.get(k) for k in ("version", "created_at", "author", "note")})
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda m: m["version"], reverse=True)


def get_version(n, path=None):
    p = _vpath(base_dir(path), n)
    if not p.exists():
        raise KeyError(f"Version {n} does not exist")
    return _read(p)


def active_version(path=None):
    base = base_dir(path)
    f = base / "active.json"
    if f.exists():
        try:
            return int(_read(f)["version"])
        except (OSError, ValueError, KeyError):
            pass
    vs = list_versions(path)
    return vs[0]["version"] if vs else None


def apply(cfg, note="", author="admin", path=None, env=None, rule_dir=None, validate_first=True):
    """Validate, then store as the next version and make it active. Returns the new version number."""
    cfg = S.merge_defaults(cfg)
    if validate_first:
        errors, _ = validate(cfg, env=env, rule_dir=rule_dir)
        if errors:
            raise ConfigError(errors)
    base = base_dir(path)
    vs = list_versions(path)
    n = (vs[0]["version"] + 1) if vs else 1
    _write(_vpath(base, n), {"version": n, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "author": author,
                             "note": (note or "").strip()[:300], "config": cfg})
    _write(base / "active.json", {"version": n})
    return n


def ensure_initialized(env=None, path=None):
    """First start: version 1 is a snapshot of the current .env and built-in defaults, so nothing changes until you edit."""
    if active_version(path) is not None:
        return active_version(path)
    env = os.environ if env is None else env
    return apply(S.config_from_env(env), "Initial configuration (imported from .env and built-in defaults)", "system",
                 path=path, env=env, validate_first=False)


def load_active(path=None, env=None):
    """(Settings, meta) for the active version. Never raises: a damaged store falls back to env-seeded defaults."""
    try:
        ensure_initialized(env=env, path=path)
        n = active_version(path)
        d = get_version(n, path)
        return Settings(d["config"], version=n), {k: d.get(k) for k in ("version", "created_at", "author", "note")}
    except Exception:
        env = os.environ if env is None else env
        return Settings(S.config_from_env(env), version=None), {"version": None, "created_at": None, "author": None,
                                                                  "note": "Configuration store unavailable; using defaults from .env"}


def rollback(n, author="admin", path=None, env=None, rule_dir=None):
    old = get_version(n, path)
    return apply(old["config"], f"Rollback to v{n}", author, path=path, env=env, rule_dir=rule_dir)


def changes_vs_previous(n, path=None):
    """What changed in version n compared with n-1: [(path, old, new)]."""
    cur = get_version(n, path)["config"]
    prev = {}
    p = _vpath(base_dir(path), n - 1)
    if p.exists():
        prev = _read(p)["config"]
    return S.diff(prev, cur) if prev else []


# ---- draft (unapplied edits survive a page reload) ----
def save_draft(cfg, path=None):
    _write(base_dir(path) / "draft.json", S.merge_defaults(cfg))


def load_draft(path=None):
    f = base_dir(path) / "draft.json"
    try:
        return S.merge_defaults(_read(f)) if f.exists() else None
    except (OSError, ValueError):
        return None


def clear_draft(path=None):
    f = base_dir(path) / "draft.json"
    if f.exists():
        f.unlink()


# ---- backup / restore ----
def export_json(cfg, version=None):
    return json.dumps({"app": "content-compiler-config", "exported_version": version, "config": S.merge_defaults(cfg)},
                      indent=2, ensure_ascii=False)


def import_json(text):
    """Parse an exported file into a full config dict. Raises ConfigError for anything that is not one."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ConfigError([f"Not valid JSON: {e}"])
    cfg = data.get("config") if isinstance(data, dict) and "config" in data else data
    if not isinstance(cfg, dict) or not (set(cfg) & set(S.default_config())):
        raise ConfigError(["This file does not look like an exported configuration."])
    return S.merge_defaults(cfg)
