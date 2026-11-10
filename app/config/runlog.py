"""A small append-only run log (data/config/runs.jsonl) so the Maintenance tab can show recent runs and errors."""
import json
import time

from app.config.store import base_dir

MAX_KEEP = 300


def append(entry, path=None):
    f = base_dir(path) / "runs.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:  # keep the file small
        lines = f.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_KEEP:
            f.write_text("\n".join(lines[-MAX_KEEP:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def recent(limit=25, path=None):
    f = base_dir(path) / "runs.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(out))
