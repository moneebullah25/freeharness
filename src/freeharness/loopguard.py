"""
Loop guard: spot a model that keeps running the SAME tool call and getting the SAME answer.

ON by default (`--no-loop-guard` or FREEHARNESS_LOOP_GUARD=0 turns it off). The root cause of the loops seen
so far was a model that no longer had the data it had already read (fixed: sticky chats, bounded fresh-chat
history); the guard only catches what is left, a model that has the data and still repeats itself.

`detect_repeat` is a small pure function; `Watch` feeds it from dsh session events (tool/call + tool/result).
The harness only ever REPORTS the repeat (and, in the oss engine, ends the turn and asks for a different
next action); it never decides or writes the action itself.
"""
from __future__ import annotations

import hashlib
import json
import os

WINDOW = 8                 # look at the last this-many calls ...
THRESHOLD = 3              # ... and flag one that occurred this many times with the same result


def enabled(flag: bool = True) -> bool:
    """On unless `flag` is False (`--no-loop-guard`) or FREEHARNESS_LOOP_GUARD=0/false/no/off."""
    if os.environ.get("FREEHARNESS_LOOP_GUARD", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool(flag)


def _args_key(args) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args.strip()
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


def call_key(name, args, result) -> tuple:
    """Identity of one call: tool name, canonical arguments, digest of the (stripped) result text."""
    return (str(name), _args_key(args), hashlib.sha256(str(result or "").strip().encode()).hexdigest())


def detect_repeat(calls, window: int = WINDOW, threshold: int = THRESHOLD) -> dict | None:
    """`calls` = [(name, args, result_text), ...] oldest first. Returns a dict describing the repeated call when
    the MOST RECENT call (same name, same arguments, same result) occurs at least `threshold` times among the
    last `window` calls; otherwise None. Only the newest call can trigger it, so a loop that already ended
    is never reported again, and calls whose results differ (a file that changed, a counter) never count."""
    recent = list(calls)[-window:]
    if len(recent) < threshold:
        return None
    last = recent[-1]
    key = call_key(*last)
    count = sum(1 for c in recent if call_key(*c) == key)
    if count < threshold:
        return None
    return {"name": last[0], "args": last[1], "result": str(last[2] or ""), "count": count, "window": len(recent)}


class Watch:
    """Collects (name, args, result) from dsh session events and reports a repeat as soon as it appears."""

    def __init__(self, window: int = WINDOW, threshold: int = THRESHOLD):
        self.window, self.threshold = window, threshold
        self.pending: dict = {}                 # callId -> (name, arguments)
        self.calls: list = []

    def feed(self, event: dict) -> dict | None:
        t, d = event.get("type"), event.get("data") or {}
        if t == "tool/call":
            self.pending[d.get("callId")] = (d.get("name"), d.get("arguments"))
        elif t == "tool/result":
            m = d.get("message") or {}
            name, args = self.pending.pop(m.get("toolCallId"), (None, None))
            if name is None:
                return None
            content = m.get("content")
            text = content if isinstance(content, str) else "".join(
                b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
            self.calls.append((name, args, text))
            return detect_repeat(self.calls, self.window, self.threshold)
        return None

    def reset(self) -> None:
        self.pending.clear()
        self.calls.clear()


def _clip(s, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe(rep: dict) -> str:
    return (f"{rep['name']} {_clip(_args_key(rep['args']), 160)} returned the same output "
            f"{rep['count']} times in the last {rep['window']} calls")


def repair_text(rep: dict) -> str:
    """The text of a repair turn. It states the facts and asks for the next DIFFERENT action; it never says
    which action that should be."""
    return (f"The harness noticed a loop: you called {rep['name']} with arguments {_clip(_args_key(rep['args']), 300)} "
            f"{rep['count']} times in your last {rep['window']} calls and it returned exactly the same output every "
            f"time:\n{_clip(rep['result'], 400)}\n"
            "Repeating that call will not produce anything new. Do not call it again with these arguments. Use what "
            "that output already tells you and take the next DIFFERENT action (another tool, or different arguments), "
            "then carry on with the task.")
