"""
A stdio MCP server giving the agent long-term memory and a per-session scratchpad:
memory_save, memory_recall, memory_forget, scratch_get, scratch_set. Run by DeepSeek
Harness (tool names mcp__memory__*); see docs/SESSIONS_AND_MEMORY.md.

The working directory (for project-scoped facts) comes from FREEHARNESS_CWD, else the
process cwd. The session (for the scratchpad) comes from FREEHARNESS_SESSION, else the
"active" pointer the freeharness client writes for that cwd, else "default".

Newline-delimited JSON-RPC on stdin/stdout; nothing else is written to stdout.

    python -m freeharness.mcp.memory
"""
from __future__ import annotations

import json
import os
import sys

from .. import memory, sessions

MAX_OUT = 4000

TOOLS = [
    {"name": "memory_save",
     "description": "Save a durable fact the user told you (a convention, preference, project fact or pointer) so future "
                    "sessions know it. Call it when the user states a rule like 'always/never/from now on' or a lasting fact. "
                    "Saving something already known updates it. Do not save secrets or one-off task details.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "one self-contained sentence"},
         "type": {"type": "string", "enum": list(memory.TYPES), "description": "feedback = conventions/corrections (default)"},
         "scope": {"type": "string", "enum": ["global", "project"], "description": "project = only this working directory"},
         "pinned": {"type": "boolean", "description": "always show at session start (default: true for feedback and user)"}},
         "required": ["text"]}},
    {"name": "memory_recall",
     "description": "Search saved memory (facts and conventions). Call at the START of a task with 2-4 keywords about it.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "type": {"type": "string", "enum": list(memory.TYPES)},
         "limit": {"type": "integer", "description": "1-20 (default 8)"}},
         "required": ["query"]}},
    {"name": "memory_forget",
     "description": "Delete a saved fact by id (from memory_recall) or by a query that matches exactly one fact.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "query": {"type": "string"}}}},
    {"name": "scratch_get",
     "description": "Read this session's scratchpad (current task, what is ready for testing, next steps). One key or all.",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}}},
    {"name": "scratch_set",
     "description": "Write one scratchpad entry for this session so you can continue after a restart. Empty value deletes it. "
                    "Keep it short; update it when the task or the next steps change.",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                     "required": ["key", "value"]}},
]


class Context:
    def __init__(self, store: memory.MemoryStore, cwd: str | None = None, session: str | None = None):
        self.store = store
        self.cwd = cwd or os.environ.get("FREEHARNESS_CWD") or os.getcwd()
        self._session = session or os.environ.get("FREEHARNESS_SESSION")

    @property
    def session(self) -> str:
        return self._session or sessions.active_for(self.cwd) or "default"


def _s(args: dict, key: str, default: str = "") -> str:
    v = args.get(key, default)
    if v is not None and not isinstance(v, (str, int, float, bool)):
        raise TypeError(f"{key} must be a string")
    return "" if v is None else str(v)


def call_tool(ctx: Context, name: str, args: dict) -> str:
    if name == "memory_save":
        scope = _s(args, "scope", "global")
        pin = args.get("pinned")
        f = ctx.store.save(_s(args, "text"), _s(args, "type", "feedback"), scope, ctx.cwd, ctx.session,
                           None if pin is None else bool(pin))
        return f"{f['action']}: {memory.format_fact(f)}"
    if name == "memory_recall":
        hits = ctx.store.search(_s(args, "query"), ctx.cwd, _s(args, "type") or None,
                                max(1, min(20, int(args.get("limit", 8)))))
        out = "\n".join(memory.format_fact(f) for f in hits) or "No saved memory matches."
        return out[:MAX_OUT]
    if name == "memory_forget":
        ref = args.get("id") if args.get("id") is not None else _s(args, "query")
        if ref in ("", None):
            raise ValueError("give an id or a query")
        gone = ctx.store.forget(ref, ctx.cwd)
        return "forgot: " + "; ".join(memory.format_fact(f) for f in gone) if gone else "Nothing matched; nothing forgotten."
    if name == "scratch_get":
        key = _s(args, "key")
        if key:
            v = sessions.scratch_get(ctx.session, key)
            return v if v is not None else f"(no scratch key {key!r})"
        return sessions.render_scratch(ctx.session, MAX_OUT) or "(scratchpad is empty)"
    if name == "scratch_set":
        d = sessions.scratch_set(ctx.session, _s(args, "key"), _s(args, "value"))
        return f"ok ({len(d)} key(s) in this session's scratchpad)"
    raise KeyError(name)


def handle(ctx: Context, msg: dict) -> dict | None:
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "freeharness-memory", "version": "0.1.0"},
                   "instructions": "Long-term memory and session scratchpad. Recall at task start; save stated conventions."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS}:
            return err(-32602, f"unknown tool {name!r}")
        try:
            text = call_tool(ctx, name, args)
        except (ValueError, TypeError, AttributeError) as e:
            return ok({"content": [{"type": "text", "text": f"bad arguments: {e}"}], "isError": True})
        return ok({"content": [{"type": "text", "text": text}]})
    return err(-32601, f"method not found: {method}")


def main(argv=None) -> int:
    ctx = Context(memory.MemoryStore(), None)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(ctx, msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
