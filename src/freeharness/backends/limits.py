"""
Empirical prompt-size limits, learned per provider from what the SITE actually did.

Nothing here is a guess about a site: a provider starts with NO assumed ceiling (`NO_LIMIT`, a safety value
far above any real chat box) and the ledger only records observations:

  known_good_chars   the largest single message that got a real answer.
  suspect_max_chars  set ONLY by a failure carrying a length-looking signal (the site's own "too long"
                     notice, a length/context error, a provider-specific size error) on a FRESH chat at a
                     size larger than known_good_chars. One failure is a suspicion, not a ceiling: the
                     failure may have had another cause.
  confirmed          True once a smaller message was answered after the failure (failure at N, then a
                     success below N): that is the repeat that turns the suspicion into a ceiling.

`ceiling()` is the learned limit with a safety margin and exists only when confirmed. `suspect_max_chars`
alone is used just to make the immediate retry smaller (`probe_budget`). Persisted to
<state>/limits.json (owner-only) so the next run does not rediscover it; shown in /health and `doctor`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

NO_LIMIT = 2_000_000          # chars. "No assumed limit": a safety value, never a measured one.
MARGIN = 0.8                  # a confirmed ceiling is this fraction of the failing size
PROBE = 0.5                   # right after a failure, retry at this fraction of the failing size


class Limits:
    def __init__(self, path: Path | str | None = None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.data: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self):
        if not self.path:
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            for key, rec in raw.items():
                if isinstance(rec, dict):
                    self.data[str(key)] = {
                        "known_good_chars": int(rec.get("known_good_chars") or 0),
                        "suspect_max_chars": int(rec["suspect_max_chars"]) if rec.get("suspect_max_chars") else None,
                        "confirmed": bool(rec.get("confirmed")), "updated": rec.get("updated", 0)}

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

    def _rec(self, key: str) -> dict:
        return self.data.setdefault(key, {"known_good_chars": 0, "suspect_max_chars": None,
                                          "confirmed": False, "updated": 0})

    # -- observations --------------------------------------------------------

    def good(self, key: str, n: int) -> None:
        """A message of n chars got a real answer."""
        with self._lock:
            r = self._rec(key)
            changed = False
            if n > r["known_good_chars"]:
                r["known_good_chars"], changed = n, True
            sus = r["suspect_max_chars"]
            if sus:
                if n >= sus:                       # it worked at a size that "failed": the suspicion was wrong
                    r["suspect_max_chars"], r["confirmed"], changed = None, False, True
                elif not r["confirmed"]:           # failure at N, then success below N: confirmed
                    r["confirmed"], changed = True, True
            if changed:
                r["updated"] = int(self.clock())
                self._save()

    def failed_length(self, key: str, n: int) -> bool:
        """A FRESH chat failed at n chars with a length-looking signal. Returns True if that was recorded
        (it must be larger than anything known to work, otherwise the size was not the problem)."""
        with self._lock:
            r = self._rec(key)
            if n <= r["known_good_chars"]:
                return False
            sus = r["suspect_max_chars"]
            if sus is None or n < sus:
                r["suspect_max_chars"], r["confirmed"] = n, False
            r["updated"] = int(self.clock())
            self._save()
            return True

    # -- what the rest of the shim may use -----------------------------------

    def ceiling(self, key: str) -> int | None:
        """The learned per-message limit, with margin; only once confirmed by a repeat."""
        r = self.data.get(key)
        if not r or not r["suspect_max_chars"] or not r["confirmed"]:
            return None
        return max(int(r["suspect_max_chars"] * MARGIN), r["known_good_chars"])

    def probe_budget(self, key: str) -> int | None:
        """Size to retry at after a length-looking failure (confirmed or not)."""
        r = self.data.get(key)
        if not r or not r["suspect_max_chars"]:
            return None
        return max(int(r["suspect_max_chars"] * PROBE), 1)

    def snapshot(self, key: str) -> dict:
        r = self.data.get(key) or {}
        return {"known_good_chars": r.get("known_good_chars", 0),
                "suspect_max_chars": r.get("suspect_max_chars"),
                "confirmed": bool(r.get("confirmed")), "ceiling_chars": self.ceiling(key)}

    def snapshot_all(self, keys) -> dict:
        return {k: self.snapshot(k) for k in keys}


def default_path() -> Path:
    from .. import paths
    return paths.state_dir() / "limits.json"
