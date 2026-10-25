"""
Turn session-log events into terminal output. The pure helpers (describe_call,
summarize_result, extract_text) are what the tests pin down; `Renderer` only
wires them to a rich Console.

Event shapes were captured from a live runtime (spike/fixtures/read_turn.json):
  assistant/message  data.message.content = [{type:"text"|"tool-call"|..., ...}]
  tool/call          data.{callId,name,arguments(JSON string)}
  tool/result        data.message.{toolCallId,isError,content:[{type:"text",text}]}
  turn/end           data.reason.kind  ("completed" when it went well)
"""
from __future__ import annotations

import json

MAX_RESULT_LINES = 8
MAX_LINE = 160


def parse_args(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        out = json.loads(arguments or "{}")
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}


def _clip(text: str, n: int = MAX_LINE) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def describe_call(name: str, arguments) -> str:
    """One line: the call as a person would say it."""
    a = parse_args(arguments)
    if name == "bash":
        return f"bash  $ {_clip(a.get('command', ''))}"
    if name in ("read", "write", "edit"):
        extra = ""
        if name == "write":
            extra = f"  ({len(str(a.get('content', '')).splitlines())} lines)"
        return f"{name}  {a.get('file_path', '?')}{extra}"
    if name in ("glob", "grep"):
        where = a.get("path") or a.get("glob") or ""
        return f"{name}  {_clip(a.get('pattern', ''))}" + (f"  in {where}" if where else "")
    if name == "todo_write":
        return f"todo  {len(a.get('todos') or [])} item(s)"
    flat = ", ".join(f"{k}={_clip(v, 40)}" for k, v in a.items())
    return f"{name}  {flat}"


def extract_text(blocks) -> str:
    return "".join(b.get("text", "") for b in blocks or [] if b.get("type") == "text")


def summarize_result(text: str, max_lines: int = MAX_RESULT_LINES) -> tuple[str, int]:
    """(shown, hidden_line_count): the head of a tool result, clipped per line."""
    lines = text.strip("\n").splitlines()
    # NOT _clip: that collapses whitespace, which would flatten indented code.
    shown = [ln if len(ln) <= MAX_LINE else ln[: MAX_LINE - 1] + "…" for ln in lines[:max_lines]]
    return "\n".join(shown), max(0, len(lines) - max_lines)


def turn_outcome(reason: dict) -> tuple[bool, str]:
    kind = (reason or {}).get("kind", "?")
    if kind == "completed":
        return True, "done"
    detail = {k: v for k, v in (reason or {}).items() if k != "kind"}
    return False, f"{kind}" + (f": {_clip(json.dumps(detail), 200)}" if detail else "")


class Renderer:
    """Feeds on `session.event` params. Holds just enough state to pair results."""

    def __init__(self, console):
        self.c = console
        self.calls: dict[str, str] = {}
        self.steps = 0
        self.tools = 0
        self.last_text = ""

    def handle(self, event: dict) -> str | None:
        """Print one event. Returns a short status label for the live spinner."""
        t, d = event.get("type", ""), event.get("data") or {}
        if t == "step/start":
            self.steps += 1
            return f"model is thinking (step {self.steps})"
        if t == "assistant/message":
            blocks = d.get("message", {}).get("content", [])
            text = extract_text(blocks).strip()
            if text:
                from rich.markdown import Markdown
                self.last_text = text
                self.c.print(Markdown(text))
            return "running tools"
        if t == "tool/call":
            self.tools += 1
            line = describe_call(d.get("name", "?"), d.get("arguments"))
            self.calls[d.get("callId", "")] = d.get("name", "?")
            self.c.print(f"[bold cyan]●[/] {_esc(line)}")
            return f"running {d.get('name', 'tool')}"
        if t == "tool/result":
            m = d.get("message", {})
            body = extract_text(m.get("content"))
            shown, hidden = summarize_result(body)
            style = "red" if m.get("isError") else "dim"
            if shown:
                self.c.print(_indent(_esc(shown), style))
            if hidden:
                self.c.print(f"  [dim]… +{hidden} more line(s)[/]")
            return f"model is thinking (step {self.steps + 1})"
        if t == "turn/end":
            return None
        if any(k in t for k in ("error", "retry")) and t not in ("tool/result",):
            self.c.print(f"[yellow]! {_esc(t)}[/] [dim]{_esc(_clip(json.dumps(d), 220))}[/]")
        return None


def _esc(text: str) -> str:
    from rich.markup import escape
    return escape(text)


def _indent(text: str, style: str) -> str:
    return "\n".join(f"  [{style}]│ {ln}[/]" for ln in text.splitlines())
