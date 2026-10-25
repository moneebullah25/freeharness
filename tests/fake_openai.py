#!/usr/bin/env python3
"""Stub OpenAI-compatible endpoint standing in for the shim in tests. Logs every request body."""
import json
import os
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path(os.environ.get("FAKE_OPENAI_LOG", tempfile.gettempdir())) / "freeharness-fake-requests.jsonl"
PORT = int(os.environ.get("FAKE_PORT") or os.environ.get("FAKE_OPENAI_PORT") or "8766")
# FAKE_SCRIPT is either a path to a JSON file (steps picked by how many assistant messages the
# history holds, so a script survives runtime restarts) or an inline JSON list (steps picked by
# request count, the last repeats; {"sleep": 5, "then": {...}} delays a reply):
#   {"text": "..."} | {"tool": "bash", "args": {...}}
_RAW = os.environ.get("FAKE_SCRIPT", "")
SCRIPT = json.loads(_RAW) if _RAW.lstrip().startswith("[") else None
_N = [0]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("fake: " + fmt % a + "\n")

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json({"object": "list", "data": [{"id": "scripted", "object": "model"}]})
        self._json({"ok": True})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        with LOG.open("a") as f:
            f.write(json.dumps(body) + "\n")
        tools = [t["function"]["name"] for t in body.get("tools", [])]
        sys.stderr.write(f"fake: {len(body.get('messages', []))} msgs, {len(tools)} tools, stream={body.get('stream')}\n")
        time.sleep(float(os.environ.get("STUB_DELAY", "0")))   # lets a test abandon a turn mid-flight
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta, finish=None, usage=None):
            c = {"id": "cmpl-1", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage:
                c["usage"] = usage
            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
            self.wfile.flush()

        chunk({"role": "assistant", "content": ""})
        msgs = body.get("messages", [])
        if SCRIPT is not None:
            step = SCRIPT[min(_N[0], len(SCRIPT) - 1)]
            _N[0] += 1
            if step.get("sleep"):
                time.sleep(step["sleep"])
                step = step.get("then", {"text": "late"})
            if "tool" in step:
                chunk({"tool_calls": [{"index": 0, "id": f"call_{_N[0]}", "type": "function",
                                       "function": {"name": step["tool"], "arguments": json.dumps(step.get("args", {}))}}]})
                chunk({}, "tool_calls")
            else:
                chunk({"content": step.get("text", "ok")})
                chunk({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        got_tool = any(m["role"] == "tool" for m in msgs)
        script = os.environ.get("FAKE_SCRIPT")
        if script and Path(script).exists():
            # Scripted mode: the file is re-read per request, a JSON list of steps
            # {"tool": name, "args": {...}} | {"text": "..."}. The step served is the
            # number of assistant messages already in the history, so a script keeps
            # its place across turns and across a runtime restart + session/resume.
            steps = json.loads(Path(script).read_text())
            idx = sum(1 for m in msgs if m["role"] == "assistant")
            step = steps[idx] if idx < len(steps) else {"text": "done"}
            real = next((t for t in tools if step.get("tool") and (t == step["tool"] or t.endswith("_" + step["tool"]))), None)
            if step.get("tool") and real:
                chunk({"tool_calls": [{"index": 0, "id": f"call_{idx}", "type": "function",
                                       "function": {"name": real, "arguments": json.dumps(step.get("args", {}))}}]})
                chunk({}, "tool_calls")
            else:
                chunk({"content": step.get("text") or f"(scripted tool {step.get('tool')} not offered: {tools})"})
                chunk({}, "stop")
        elif tools and "read" in tools and not got_tool:
            # split the arguments across chunks the way real servers do
            chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                   "function": {"name": "read", "arguments": ""}}]})
            for frag in ['{"file_path": ', '"hello.txt"}']:
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": frag}}]})
            chunk({}, "tool_calls")
        else:
            seen = next((m["content"] for m in reversed(msgs) if m["role"] == "tool"), "")
            seen = seen if isinstance(seen, str) else json.dumps(seen)
            for w in ["The file says: ", seen[:80]]:
                chunk({"content": w})
            chunk({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    LOG.unlink(missing_ok=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
