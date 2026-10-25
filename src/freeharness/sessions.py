"""
Session index and per-session scratchpad. Stdlib only, owner-only files, atomic writes.

The harness keeps each conversation (dsh-home/sessions/<id>) and `transcript.py` keeps the
readable copy. This adds what neither has: an index (id, cwd, title from the first user
message, model, created, last_used, turns) so `freeharness --continue` can find "the latest
session for this folder" after a restart, and a small key/value scratchpad per session
(current task, what is ready to test, next steps) that is shown and re-injected on resume.

    <data>/sessions-index/index.json         {"sessions": {id: {...}}}
    <data>/sessions-index/scratch/<id>.json  {"key": "value", ...}
    <data>/sessions-index/active.json        {cwd: session id}  (so the MCP server knows the session)
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import paths

MAX_TITLE = 80
MAX_VALUE = 2000
MAX_KEYS = 40


def index_dir() -> Path:
    return paths.data_dir() / "sessions-index"


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` via a 0600 temp file in the same directory + rename."""
    paths.private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def make_title(text: str) -> str:
    one = re.sub(r"\s+", " ", text).strip()
    return one if len(one) <= MAX_TITLE else one[:MAX_TITLE - 1] + "…"


def _norm_cwd(cwd) -> str:
    return str(Path(cwd).expanduser().resolve())


def _all() -> dict:
    data = _load(index_dir() / "index.json", {})
    s = data.get("sessions") if isinstance(data, dict) else None
    return s if isinstance(s, dict) else {}


def _save(sessions: dict) -> None:
    atomic_write(index_dir() / "index.json", json.dumps({"sessions": sessions}, ensure_ascii=False, indent=1))


def record_turn(sid: str, cwd, first_message: str = "", model: str = "") -> dict:
    """Create or update the entry for `sid`; call after each turn. Title is set once."""
    sessions, now = _all(), time.time()
    e = sessions.get(sid) or {"id": sid, "cwd": _norm_cwd(cwd), "title": "", "model": model,
                              "created": now, "last_used": now, "turns": 0}
    if not e["title"] and first_message:
        e["title"] = make_title(first_message)
    if model:
        e["model"] = model
    e["last_used"], e["turns"] = now, e["turns"] + 1
    sessions[sid] = e
    _save(sessions)
    return e


def touch(sid: str, cwd, model: str = "") -> dict:
    """Register `sid` without counting a turn (e.g. `--resume` of a session from before the index)."""
    sessions = _all()
    if sid not in sessions:
        now = time.time()
        sessions[sid] = {"id": sid, "cwd": _norm_cwd(cwd), "title": "", "model": model,
                         "created": now, "last_used": now, "turns": 0}
        _save(sessions)
    return sessions[sid]


def get(sid: str) -> dict | None:
    return _all().get(sid)


def list_sessions(cwd=None, limit: int | None = None) -> list[dict]:
    items = sorted(_all().values(), key=lambda e: e.get("last_used", 0), reverse=True)
    if cwd is not None:
        c = _norm_cwd(cwd)
        items = [e for e in items if e.get("cwd") == c]
    return items[:limit] if limit else items


def latest_for_cwd(cwd, existing=None) -> str | None:
    """Most recently used session for `cwd` that has at least one turn.
    `existing` optionally filters to ids the harness still has on disk."""
    for e in list_sessions(cwd):
        if e.get("turns", 0) > 0 and (existing is None or e["id"] in existing):
            return e["id"]
    return None


def age(ts: float, now: float | None = None) -> str:
    d = max(0, int((now or time.time()) - ts))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{d // n}{unit} ago"
    return "just now"


def format_list(items: list[dict]) -> list[str]:
    return [f"{e['id']}  {age(e.get('last_used', 0)):>8}  {e.get('turns', 0):>3} turns  "
            f"{e.get('title') or '(no title)'}" for e in items]


# ------------------------------------------------------------------ active pointer

def set_active(cwd, sid: str) -> None:
    a = _load(index_dir() / "active.json", {})
    a = a if isinstance(a, dict) else {}
    a[_norm_cwd(cwd)] = sid
    atomic_write(index_dir() / "active.json", json.dumps(a))


def active_for(cwd) -> str | None:
    a = _load(index_dir() / "active.json", {})
    return a.get(_norm_cwd(cwd)) if isinstance(a, dict) else None


# ------------------------------------------------------------------ scratchpad

def _scratch_path(sid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", sid) or sid.startswith("."):
        raise ValueError("bad session id")
    return index_dir() / "scratch" / f"{sid}.json"


def scratch_all(sid: str) -> dict:
    d = _load(_scratch_path(sid), {})
    return d if isinstance(d, dict) else {}


def scratch_get(sid: str, key: str | None = None):
    d = scratch_all(sid)
    return d if key is None else d.get(key)


def scratch_set(sid: str, key: str, value: str | None) -> dict:
    """Set `key`; value None or "" deletes it."""
    key = str(key).strip()
    if not re.fullmatch(r"[A-Za-z0-9_. -]{1,40}", key):
        raise ValueError("key must be 1-40 chars of letters, digits, space, _ . -")
    d = scratch_all(sid)
    if value is None or str(value) == "":
        d.pop(key, None)
    else:
        if key not in d and len(d) >= MAX_KEYS:
            raise ValueError(f"scratchpad is full ({MAX_KEYS} keys)")
        d[key] = str(value)[:MAX_VALUE]
    atomic_write(_scratch_path(sid), json.dumps(d, ensure_ascii=False, indent=1))
    return d


def render_scratch(sid: str, limit: int = 1500) -> str:
    d = scratch_all(sid)
    if not d:
        return ""
    out = "\n".join(f"- {k}: {v}" for k, v in d.items())
    return out if len(out) <= limit else out[:limit - 1] + "…"


def main(argv=None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    cwd = None if "--all" in args else Path.cwd()
    items = list_sessions(cwd, 30)
    if not items:
        print("no sessions yet" + ("" if cwd is None else " for this folder (use --all for every folder)"))
    for line in format_list(items):
        print(line)
    return 0
