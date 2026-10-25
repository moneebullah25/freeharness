"""
Real DeepSeek Harness, scripted model, free port (never 8766), temp FREEHARNESS_HOME. No browser, no network.

Proves end to end: the memory MCP server is loaded by dsh through the overlay row; a fact saved in one
session is recalled in a brand-new session after the runtime was restarted; pinned conventions arrive in
the model's prompt via the AGENTS.md block; the scratchpad is written under the right session; and dsh's own
session files restore the whole conversation on resume. Skipped when the harness is not installed.
"""
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import freeharness
from freeharness import memory, memory_cmd, paths, sessions
from freeharness.client import rpc
from freeharness.client.rpc import Runtime

pytestmark = pytest.mark.skipif(not rpc.DSH_BIN.exists(), reason="DeepSeek Harness not installed (run `freeharness setup`)")

REQUESTS: list = []


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


class Scripted(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps({"object": "list", "data": [{"id": "auto", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        REQUESTS.append(body)
        msgs, tools = body.get("messages", []), [t["function"]["name"] for t in body.get("tools", [])]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta, finish=None):
            c = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "auto",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
            self.wfile.flush()

        def call(suffix, args):
            name = next((t for t in tools if t.endswith(suffix)), None)
            assert name, f"tool ...{suffix} missing from {tools}"
            chunk({"role": "assistant", "content": ""})
            chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                   "function": {"name": name, "arguments": json.dumps(args)}}]})
            chunk({}, "tool_calls")

        last = msgs[-1] if msgs else {}
        # the current prompt = every user message after the last assistant/tool message (dsh appends context notes)
        tail = []
        for m in reversed(msgs):
            if m["role"] != "user":
                break
            tail.append(_text(m["content"]))
        user = "\n".join(reversed(tail)).split("<system-reminder>")[0].strip()
        if last.get("role") == "tool":
            chunk({"role": "assistant", "content": ""})
            chunk({"content": "tool said: " + _text(last["content"])[:300]})
            chunk({}, "stop")
        elif "SAVE:" in user:
            call("memory_save", {"text": user.split("SAVE:", 1)[1].strip(), "type": "feedback"})
        elif "RECALL:" in user:
            call("memory_recall", {"query": user.split("RECALL:", 1)[1].strip()})
        elif "SCRATCH:" in user:
            call("scratch_set", {"key": "current_task", "value": user.split("SCRATCH:", 1)[1].strip()})
        else:
            chunk({"role": "assistant", "content": ""})
            chunk({"content": "plain reply"})
            chunk({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "fh"))
    # the MCP child must import THIS checkout, not some other installed copy
    monkeypatch.setenv("PYTHONPATH", str(Path(freeharness.__file__).resolve().parent.parent))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)          # port 0: the OS picks a free one
    port = srv.server_address[1]
    assert port != 8766
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    patch = tmp_path / "patch.yml"
    patch.write_text(rpc.PATCH.read_text().replace("127.0.0.1:8766", f"127.0.0.1:{port}"))
    ws = tmp_path / "ws"
    ws.mkdir()
    REQUESTS.clear()
    memory_cmd.enable(ws)
    runtimes = []

    def make():
        rt = Runtime(ws, home=paths.dsh_home(), patch=patch)
        runtimes.append(rt)
        return rt
    yield ws, make
    for rt in runtimes:
        rt.close()
    srv.shutdown()


def sid(tag):
    return f"test-{tag}-{uuid.uuid4().hex[:8]}"


def all_prompt_text(requests) -> str:
    return "\n".join(_text(m.get("content")) for r in requests for m in r.get("messages", []))


def test_save_in_one_session_recall_in_a_new_one_after_restart_and_resume_restores_history(env):
    ws, make = env
    fact = "Commit subjects are one line with no trailers"
    s1, s2 = sid("a"), sid("b")

    # --- day 1: save a fact and write the scratchpad -------------------------------------------
    rt = make()
    rt.start()
    sessions.set_active(ws, s1)                                    # what the client does before a turn
    r = rt.run_turn(s1, "SAVE: " + fact, timeout=120)
    assert r.ok and r.tool_calls and r.tool_calls[0][0].endswith("memory_save"), (r.reason, r.final_text, r.tool_calls, [t["function"]["name"] for t in REQUESTS[-1]["tools"]], rt.diagnose(""))
    assert r.tool_errors == 0 and "added" in r.final_text, r.final_text
    r = rt.run_turn(s1, "SCRATCH: finish the parser", timeout=120)
    assert r.ok and r.tool_errors == 0
    names = {t["function"]["name"] for t in REQUESTS[-1]["tools"]}
    memory_tools = sorted(n for n in names if "memory__" in n)
    assert memory_tools == sorted("mcp__memory__" + n for n in
                                  ("memory_save", "memory_recall", "memory_forget", "scratch_get", "scratch_set")), names
    store = memory.MemoryStore()
    saved = store.list(ws)
    assert [f["text"] for f in saved] == [fact] and saved[0]["source_session"] == s1
    store.close()
    assert sessions.scratch_get(s1, "current_task") == "finish the parser"
    rt.close()                                                     # "stop today"

    # --- day 2: a fresh runtime, a brand-new session ---------------------------------------------
    memory_cmd.refresh(ws)                                         # the client does this at startup
    REQUESTS.clear()
    rt2 = make()
    rt2.start()
    sessions.set_active(ws, s2)
    r = rt2.run_turn(s2, "RECALL: commit subjects", timeout=120)
    assert r.ok and r.tool_errors == 0
    assert fact in r.final_text, "memory_recall must return the fact saved in the other session"
    assert fact in all_prompt_text(REQUESTS[:1]), "the pinned convention must be in the first prompt via AGENTS.md"
    assert sessions.scratch_get(s2, "current_task") is None, "scratchpads are per session"
    rt2.close()

    # --- resume next day: dsh's own session files restore the conversation ------------------------
    REQUESTS.clear()
    rt3 = make()
    rt3.start()
    rt3.resume(s1)
    r = rt3.run_turn(s1, "hello again", timeout=120)
    assert r.ok and r.final_text == "plain reply"
    text = all_prompt_text(REQUESTS)
    assert "SAVE: " + fact in text and "SCRATCH: finish the parser" in text, "earlier turns must be replayed on resume"
    assert any(m.get("role") == "tool" for m in REQUESTS[0]["messages"]), "tool history must be replayed too"


def test_vault_and_memory_enabled_together_both_servers_load(env, tmp_path):
    """Both rows disable `mcp-resources`; dsh must tolerate that and load both servers."""
    from freeharness import vault_cmd
    ws, make = env
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\nhello\n")
    vault_cmd.connect(notes)
    assert (vault_cmd.overlay_dir() / "vault.yml").exists() and (vault_cmd.overlay_dir() / "memory.yml").exists()
    rt = make()
    rt.start()
    s = sid("both")
    sessions.set_active(ws, s)
    r = rt.run_turn(s, "hello", timeout=120)
    assert r.ok, (r.reason, rt.diagnose(""))
    names = {t["function"]["name"] for t in REQUESTS[-1]["tools"]}
    assert {"mcp__vault__vault_search", "mcp__vault__vault_read", "mcp__memory__memory_save"} <= names, names
    assert not [n for n in names if "resource" in n], names
    assert len([n for n in names if n.startswith("mcp__")]) == 7
