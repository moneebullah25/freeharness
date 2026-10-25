"""
An in-process OpenAI-compatible endpoint that plays a fixed script of tool calls,
on a free port (never 8766). The model is a stand-in for "whatever the web chat
says": the security tests prove the enforcement layer, so the model may be obedient,
malicious or prompt-injected and the outcome must not change.

    with ScriptedModel([("bash", {"command": "cat /etc/hostname"})]) as m:
        ... point dsh at m.url ...

Step N of the script is emitted when the request holds N tool results; after the
script is exhausted the model answers "scripted model finished". `requests` keeps
every request body so a test can check what dsh sent (e.g. the tool results).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ScriptedModel:
    def __init__(self, script):
        self.script = list(script)          # [(tool_name, arguments-dict)]
        self.requests: list[dict] = []
        outer = self

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
                outer.requests.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()

                def chunk(delta, finish=None):
                    c = {"id": "cmpl-1", "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                    self.wfile.flush()

                done = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
                names = {t["function"]["name"] for t in body.get("tools", [])}
                chunk({"role": "assistant", "content": ""})
                if done < len(outer.script) and outer.script[done][0] in names:
                    name, args = outer.script[done]
                    chunk({"tool_calls": [{"index": 0, "id": f"call_{done}", "type": "function",
                                           "function": {"name": name, "arguments": json.dumps(args)}}]})
                    chunk({}, "tool_calls")
                else:
                    chunk({"content": "scripted model finished"})
                    chunk({}, "stop")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def tool_results(self) -> list[str]:
        """Text of every tool result in the last request, in order."""
        out = []
        for m in (self.requests[-1].get("messages", []) if self.requests else []):
            if m.get("role") == "tool":
                c = m.get("content")
                out.append(c if isinstance(c, str) else json.dumps(c))
        return out
