"""
Per-provider health, kept by the shim in <state>/provider_health.json (atomic writes, owner-only) and read by
`freeharness providers status`: the last error class, how often a provider Stalled, when it last answered, when its
cooldown ends, and a short record of whether its replies to a tool-offering request held a tool call.

Outcomes of those replies: "t" = a tool call at once, "n" = needed the one nudge, "s" = Stalled after it. `score()` is
the share of "t" in the recent record, used to prefer a provider on failover when the order is `auto`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

RECENT = 20                   # outcomes remembered per provider
UNKNOWN_SCORE = 0.5           # a provider with no record is neither preferred nor avoided
_COUNTER = {"t": "tool_replies", "n": "nudged_replies", "s": "stalled_replies"}


def default_path() -> Path:
    from .. import paths
    return paths.state_dir() / "provider_health.json"


def load(path) -> dict:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def score_of(rec: dict) -> float:
    recent = [o for o in (rec or {}).get("recent", []) if o in _COUNTER]
    return recent.count("t") / len(recent) if recent else UNKNOWN_SCORE


class Health:
    def __init__(self, path=None, wall=time.time):
        self.path = Path(path) if path else None
        self.wall = wall
        self.data: dict[str, dict] = load(self.path) if self.path else {}
        self._lock = threading.Lock()

    def _rec(self, key: str) -> dict:
        return self.data.setdefault(key, {})

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            pass                        # a read-only state dir must never break a request

    def error(self, key: str, cls: str, cooldown_s: float = 0) -> None:
        with self._lock:
            r, now = self._rec(key), self.wall()
            r.update(last_error_class=cls, last_error_at=now, cooldown_until=now + cooldown_s)
            if cls == "stalled":
                r["stalled_count"] = r.get("stalled_count", 0) + 1
                r["last_stalled_at"] = now
            self._save()

    def success(self, key: str) -> None:
        with self._lock:
            self._rec(key)["last_success"] = self.wall()
            self._save()

    def outcome(self, key: str, code: str) -> None:
        """A reply to a tool-offering request: "t" tool call, "n" nudged then fine, "s" Stalled."""
        with self._lock:
            r = self._rec(key)
            r["recent"] = (list(r.get("recent", [])) + [code])[-RECENT:]
            r[_COUNTER[code]] = r.get(_COUNTER[code], 0) + 1
            self._save()

    def score(self, key: str) -> float:
        return score_of(self.data.get(key, {}))
