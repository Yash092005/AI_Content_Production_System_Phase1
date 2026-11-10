"""Tiny durable job store for long-running Streamlit pipeline runs."""

import json
import os
import time
import uuid
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "config" / "jobs"

def _base_dir():
    return Path(os.getenv("CONFIG_DIR") or DEFAULT_DIR)

def _path(run_id):
    return _base_dir() / f"{run_id}.json"

def new_id():
    return uuid.uuid4().hex

def save(run_id, data):
    path = _path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": time.time(), **data}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)

def load(run_id):
    path = _path(run_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

def start(run_id, meta=None):
    save(run_id, {"status": "QUEUED", "states": {}, "result": None, "meta": meta or {}, "error": None})

def stage(run_id, states):
    save(run_id, {"status": "RUNNING", "states": states})

def complete(run_id, result, states):
    save(run_id, {"status": "COMPLETE", "states": states, "result": result, "error": None})

def fail(run_id, error, states=None):
    save(run_id, {"status": "FAILED", "states": states or {}, "error": str(error)})
