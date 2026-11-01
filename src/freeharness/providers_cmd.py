"""`freeharness providers status`: per provider, new chats used this hour of the cap, cooldown left, last error class,
whether it recently Stalled, and its tool-call record. Reads chats.json, limits.json and provider_health.json."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import paths
from .backends import health as health_mod
from .backends.providers import PROVIDERS

STALL_RECENT = 3600          # seconds: a Stall this recent counts as "recently"
NEVER_USE = ("deepseek",)    # blocked: never listed


def _read(path) -> dict:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def collect(state_dir, now, cap: int) -> list[dict]:
    chats, limits = _read(Path(state_dir) / "chats.json"), _read(Path(state_dir) / "limits.json")
    health = _read(Path(state_dir) / "provider_health.json")
    rows = []
    for key in PROVIDERS:
        if key in NEVER_USE:
            continue
        h = health.get(key) or {}
        used = sum(1 for t in chats.get(key, []) if isinstance(t, (int, float)) and 0 <= now - t < 3600)
        stalled_at = h.get("last_stalled_at") or 0
        rows.append({
            "provider": key, "chats_used": used, "chat_cap": cap,
            "cooldown_s": max(0, round(h.get("cooldown_until", 0) - now)),
            "last_error_class": h.get("last_error_class") or "-",
            "stalled_recently": bool(stalled_at) and now - stalled_at < STALL_RECENT,
            "stalled_count": h.get("stalled_count", 0),
            "tool_replies": h.get("tool_replies", 0),
            "nudged_or_stalled": h.get("nudged_replies", 0) + h.get("stalled_replies", 0),
            "score": round(health_mod.score_of(h), 2),
            "known_good_chars": (limits.get(key) or {}).get("known_good_chars", 0)})
    return rows


def render(rows: list[dict]) -> str:
    lines = [f"{'provider':<11}{'chats/h':<9}{'cooldown':<10}{'last error':<16}{'stalled':<9}{'tool calls':<11}nudged/stalled"]
    for r in rows:
        lines.append(f"{r['provider']:<11}{str(r['chats_used']) + '/' + str(r['chat_cap']):<9}{str(r['cooldown_s']) + 's':<10}"
                     f"{r['last_error_class']:<16}{'yes' if r['stalled_recently'] else 'no':<9}{r['tool_replies']:<11}"
                     f"{r['nudged_or_stalled']}")
    return "\n".join(lines)


def main(argv=None, state_dir=None, now=None, out=print) -> int:
    argv = list(argv or [])
    if not argv or argv[0] != "status":
        print("usage: freeharness providers status", file=sys.stderr)
        return 2
    from .backends.shim import MAX_NEW_CHATS_PER_HOUR
    out(render(collect(state_dir or paths.state_dir(), time.time() if now is None else now, MAX_NEW_CHATS_PER_HOUR)))
    return 0
