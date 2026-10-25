"""
A plain-text record of every conversation, written as it happens.

The harness already keeps each session (compressed, machine-readable) and lets a task continue on
another provider from it. This is the human-readable copy: your prompt, the assistant's text, each
tool call and its result, one markdown file per session, in
`<data>/transcripts/<session id>.md` (owner-only). It is the file to open if you want to read, grep,
or paste a conversation into any chat by hand.

It contains what the agent saw (file contents, command output), so treat it like the session log:
it never leaves this machine unless you copy it. Each tool result is cut at MAX_RESULT characters.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import paths
from .client.render import describe_call, extract_text, parse_args

MAX_RESULT = 20_000


def transcript_dir() -> Path:
    return paths.data_dir() / "transcripts"


class Transcript:
    def __init__(self, session_id: str, cwd: str, directory: Path | None = None):
        d = paths.private_dir(directory or transcript_dir())
        self.path = d / f"{session_id}.md"
        new = not self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._f = os.fdopen(fd, "a", encoding="utf-8")
        if new:
            self._w(f"# Session {session_id}\n\nworkspace: `{cwd}`  \nstarted: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    def _w(self, text: str):
        self._f.write(text + "\n")
        self._f.flush()

    def user(self, text: str):
        self._w(f"\n## You  ({time.strftime('%H:%M:%S')})\n\n{text}\n")

    def event(self, ev: dict):
        t, d = ev.get("type", ""), ev.get("data") or {}
        if t == "assistant/message":
            text = extract_text((d.get("message") or {}).get("content")).strip()
            if text:
                self._w(f"\n### Assistant\n\n{text}\n")
        elif t == "tool/call":
            args = parse_args(d.get("arguments"))
            self._w(f"\n**tool call:** `{describe_call(d.get('name', '?'), args)}`\n\n```json\n"
                    f"{json.dumps(args, ensure_ascii=False, indent=1)[:MAX_RESULT]}\n```\n")
        elif t == "tool/result":
            m = d.get("message") or {}
            body = extract_text(m.get("content")).strip()
            cut = f"\n[... {len(body) - MAX_RESULT} more characters not saved]" if len(body) > MAX_RESULT else ""
            self._w(f"\n**tool result{' (error)' if m.get('isError') else ''}:**\n\n```text\n{body[:MAX_RESULT]}{cut}\n```\n")
        elif t == "turn/end":
            kind = (d.get("reason") or {}).get("kind", "?")
            self._w(f"\n_turn ended: {kind}_\n")

    def close(self):
        try:
            self._f.close()
        except OSError:
            pass


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    d = transcript_dir()
    files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
    if not args:
        if not files:
            print(f"no transcripts yet (they are written to {d})")
        for p in files[:20]:
            print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))}  {p.stem}  {p.stat().st_size:,} bytes")
        return 0
    name = args[0]
    match = [p for p in files if p.stem == name] or [p for p in files if p.stem.startswith(name)]
    if len(match) != 1:
        print(f"{'no' if not match else 'several'} transcript matches {name!r}", file=sys.stderr)
        return 1
    print(match[0].read_text(encoding="utf-8"))
    return 0
