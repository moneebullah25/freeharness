"""
Real DeepSeek Harness + scripted model + fake GitHub (free port, never 8766, no real network): the gh_* tools are
offered through the overlay row, the token and the confirmation socket reach the MCP server through dsh's
env scrub by NAME, and a live write is only done after the (scripted) human approves. Skipped without the harness.
"""
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import freeharness
from freeharness import github_cmd, paths, sessions
from freeharness.client import approval, rpc
from freeharness.client.rpc import Runtime

from fake_github import FakeGitHub

pytestmark = pytest.mark.skipif(not rpc.DSH_BIN.exists(), reason="DeepSeek Harness not installed (run `freeharness setup`)")

TOKEN = "ghp_DSHCANARYDSHCANARYDSHCANARY99"
REQUESTS: list = []
EXPECTED = ["gh_repo_info", "gh_search", "gh_get_file", "gh_list_pr_files", "gh_pr_threads", "gh_fork", "gh_clone",
            "gh_push", "gh_issue_create", "gh_pr_create", "gh_pr_reply", "gh_pr_resolve_thread"]


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
        last = msgs[-1] if msgs else {}
        tail = []
        for m in reversed(msgs):
            if m["role"] != "user":
                break
            tail.append(_text(m["content"]))
        user = "\n".join(reversed(tail)).split("<system-reminder>")[0].strip()
        if last.get("role") == "tool":
            chunk({"role": "assistant", "content": ""})
            chunk({"content": "tool said: " + _text(last["content"])[:600]})
            chunk({}, "stop")
        elif user.startswith("CALL "):
            suffix, _, args = user[5:].partition(" ")
            name = next((t for t in tools if t.endswith("__" + suffix)), None)
            assert name, f"tool {suffix} missing from {tools}"
            chunk({"role": "assistant", "content": ""})
            chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                   "function": {"name": name, "arguments": args or "{}"}}]})
            chunk({}, "tool_calls")
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
    monkeypatch.setenv("PYTHONPATH", str(Path(freeharness.__file__).resolve().parent.parent))
    fake = FakeGitHub().start()
    fake.add_repo("up/proj", files={"README.md": "hi"})
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("FH_GH_API", fake.url)
    monkeypatch.setenv("FH_GH_STATE", str(tmp_path / "ghstate"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    port = srv.server_address[1]
    assert port != 8766
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    patch = tmp_path / "patch.yml"
    patch.write_text(rpc.PATCH.read_text().replace("127.0.0.1:8766", f"127.0.0.1:{port}"))
    ws = tmp_path / "ws"
    ws.mkdir()
    REQUESTS.clear()
    runtimes = []

    def make(approver=None):
        rt = Runtime(ws, home=paths.dsh_home(), patch=patch, approver=approver)
        runtimes.append(rt)
        return rt
    yield ws, make, fake
    for rt in runtimes:
        rt.close()
    srv.shutdown()
    fake.stop()


def turn(rt, ws, text):
    sid = f"test-gh-{uuid.uuid4().hex[:8]}"
    sessions.set_active(ws, sid)
    return rt.run_turn(sid, text, timeout=120)


def prompts() -> str:
    return "\n".join(_text(m.get("content")) for r in REQUESTS for m in r.get("messages", []))


def test_gh_tools_are_offered_and_the_token_reaches_the_server_but_never_the_model(env):
    ws, make, fake = env
    github_cmd.enable()
    rt = make()
    rt.start()
    r = turn(rt, ws, "CALL gh_repo_info " + json.dumps({"repo": "up/proj"}))
    assert r.ok and r.tool_errors == 0, (r.reason, r.final_text, rt.diagnose(""))
    names = {t["function"]["name"] for t in REQUESTS[-1]["tools"]}
    gh = sorted(n for n in names if n.startswith("mcp__gh__"))
    assert gh == sorted("mcp__gh__" + n for n in EXPECTED), names
    assert not [n for n in names if "resource" in n]
    assert "default=main" in r.final_text
    assert f"Bearer {TOKEN}" in fake.auth, "dsh must pass GITHUB_TOKEN to the server by name"
    assert TOKEN not in prompts() and "GITHUB_TOKEN" not in prompts().replace("GitHub contribution", "")
    assert github_cmd.INSTRUCTIONS.split("\n")[1] in prompts(), "the AGENTS.md block reaches the model"
    assert fake.writes == []


def test_live_write_happens_only_after_the_human_approves_over_the_socket(env):
    ws, make, fake = env
    github_cmd.enable(live=True)
    deny = approval.FixedAnswer("rejected", confirm="denied")
    rt = make(deny)
    rt.start()
    r = turn(rt, ws, "CALL gh_fork " + json.dumps({"repo": "up/proj"}))
    assert r.ok and "did not approve" in r.final_text, (r.final_text, rt.diagnose(""))
    assert fake.writes == []
    assert [x["request"]["type"] for x in deny.log] == ["confirm"] and "FORK up/proj" in deny.log[0]["request"]["text"]
    rt.close()

    ok = approval.FixedAnswer("rejected", confirm="confirmed")
    rt2 = make(ok)
    rt2.start()
    r = turn(rt2, ws, "CALL gh_fork " + json.dumps({"repo": "up/proj"}))
    assert r.ok and "forked: me/proj" in r.final_text, (r.final_text, rt2.diagnose(""))
    assert [w[1] for w in fake.writes] == ["/repos/up/proj/forks"]
    rt2.close()


def test_no_approver_means_no_write_even_when_live(env):
    """`-p` and tests run without an answerer: the server has no socket and refuses."""
    ws, make, fake = env
    github_cmd.enable(live=True)
    rt = make(None)
    rt.start()
    r = turn(rt, ws, "CALL gh_fork " + json.dumps({"repo": "up/proj"}))
    assert r.ok and "no human confirmation channel" in r.final_text, (r.final_text, rt.diagnose(""))
    assert fake.writes == []
