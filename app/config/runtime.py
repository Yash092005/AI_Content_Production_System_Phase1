"""Run-scoped settings, visible to the whole pipeline without passing them through every function.

Why a ContextVar and not os.environ: each Streamlit session runs in its own thread, so one person's run (or one
person's "Fresh run") can never leak into another's. With NO settings active every lookup falls back to the
environment variable / default, which is exactly how the app behaved before this module existed."""
import contextlib
import contextvars
import os

_current = contextvars.ContextVar("content_settings", default=None)


@contextlib.contextmanager
def use_settings(settings):
    """Activate `settings` for the current execution context. `None` is allowed and means "no settings"."""
    token = _current.set(settings)
    try:
        yield settings
    finally:
        _current.reset(token)


def current():
    return _current.get()


def as_bool(v):
    return str(v).strip().lower() in ("1", "true", "on", "yes")


def get(path, env=None, default=None, cast=None):
    """Value for a dotted config `path`.
    Active settings always win. With none active: the env var `env` if set, else `default`."""
    s = _current.get()
    if s is not None:
        val = s.get(path, default)
        return cast(val) if cast and val is not None else val
    if env:
        raw = os.getenv(env)
        if raw is not None:
            return cast(raw) if cast else raw
    return default


def get_bool(path, env=None, default=False):
    s = _current.get()
    if s is not None:
        return bool(s.get(path, default))
    raw = os.getenv(env) if env else None
    return as_bool(raw) if raw is not None else default
