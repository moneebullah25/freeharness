#!/usr/bin/env python3
"""Scripted OpenAI-compatible stub on a configurable port (so it never collides with a live shim).

Usage: scripted_openai.py PORT SCRIPT.json LOG.jsonl
SCRIPT is a list; request N gets SCRIPT[N] (the last entry repeats):
  {"calls": [{"name": "bash", "args": {...}}, ...]}  -> tool calls
  {"text": "done"}                                   -> final text
Every request body is appended to LOG.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, SCRIPT, LOG = int(sys.argv[1]), json.load(open(sys.argv[2])), sys.argv[3]
COUNT = [0]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps({"object": "list", "data": [{"id": "scripted", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        with open(LOG, "a") as f:
            f.write(json.dumps(body) + "\n")
        step = SCRIPT[min(COUNT[0], len(SCRIPT) - 1)]
        COUNT[0] += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta, finish=None):
            c = {"id": "c", "object": "chat.completion.chunk", "created": int(time.time()), "model": "scripted",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
            self.wfile.flush()

        chunk({"role": "assistant", "content": ""})
        if "calls" in step:
            for i, c in enumerate(step["calls"]):
                chunk({"tool_calls": [{"index": i, "id": f"call_{COUNT[0]}_{i}", "type": "function",
                                       "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}]})
            chunk({}, "tool_calls")
        else:
            chunk({"content": step.get("text", "done")})
            chunk({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
