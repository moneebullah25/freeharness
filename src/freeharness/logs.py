"""
Diagnostic logging that is safe to share: rotated, size-bounded, and it never
records prompts, replies, file contents or tokens (callers log sizes and
outcomes only).
"""
from __future__ import annotations

import logging
import logging.handlers
import platform
import sys

from . import __version__, paths

_MAX_BYTES = 1_000_000
_BACKUPS = 3


def get_logger(name: str = "freeharness") -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    try:
        d = paths.private_dir(paths.log_dir())
        h = logging.handlers.RotatingFileHandler(d / "freeharness.log", maxBytes=_MAX_BYTES,
                                                 backupCount=_BACKUPS, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(h)
    except OSError:                       # read-only home: log to stderr instead of failing
        log.addHandler(logging.StreamHandler(sys.stderr))
    log.propagate = False
    return log


def environment() -> dict:
    """What to attach to a bug report. No paths under $HOME, no secrets."""
    return {"freeharness": __version__, "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}", "arch": platform.machine()}
