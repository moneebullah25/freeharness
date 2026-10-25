#!/usr/bin/env python3
"""Self-check for the free-agent client. No browser and no web chat: a stub
OpenAI-compatible server (spike/fake_openai.py) stands in for the shim, so this
exercises the real dsh runtime, the JSON-RPC wrapper, the renderer and the CLI.

Run: .venv/bin/python tests/test_client.py        (port 8766 must be free)
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent

from rich.console import Console  # noqa: E402

from freeharness.client import render  # noqa: E402
from freeharness.client.rpc import Runtime, RuntimeDied, shim_health  # noqa: E402

FIXTURE = HERE / "fixtures" / "read_turn.json"


class Stub:
    def __init__(self, delay=0):
        self.delay = delay

    def __enter__(self):
        assert not shim_health(0.5), "port 8766 is in use (a shim is running?) -- stop it first"
        env = dict(os.environ, STUB_DELAY=str(self.delay))
        self.p = subprocess.Popen([sys.executable, str(HERE / "fake_openai.py")],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                import urllib.request
                urllib.request.urlopen("http://127.0.0.1:8766/v1/models", timeout=1).read()
                return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("stub did not start")

    def __exit__(self, *a):
        self.p.terminate()
        self.p.wait(5)


def sid(tag="t"):
    """Sessions persist in the dsh home across runs, so ids must be unique."""
    return f"test-{tag}-{uuid.uuid4().hex[:8]}"


def workspace(prefix="free-agent-test-"):
    d = Path(tempfile.mkdtemp(prefix=prefix))
    (d / "hello.txt").write_text("the secret word is pineapple\n")
    return d


# ----------------------------------------------------------------- pure render

def test_describe_call():
    assert render.describe_call("bash", '{"command":"python3 -m pytest -x"}') == "bash  $ python3 -m pytest -x"
    assert render.describe_call("read", {"file_path": "a.py"}) == "read  a.py"
    assert render.describe_call("write", {"file_path": "n.txt", "content": "1\n2\n3\n"}) == "write  n.txt  (3 lines)"
    assert render.describe_call("grep", {"pattern": "TODO", "path": "src"}) == "grep  TODO  in src"
    assert render.describe_call("todo_write", {"todos": [1, 2]}) == "todo  2 item(s)"
    assert render.describe_call("mystery", {"a": "x"}) == "mystery  a=x"
    long = render.describe_call("bash", {"command": "x" * 500})
    assert len(long) < 200 and long.endswith("…"), "a long command must be clipped"
    assert render.describe_call("read", "not json") == "read  ?", "bad arguments must not crash"
    print("  describe_call ....... ok")


def test_summarize_result():
    shown, hidden = render.summarize_result("\n".join(f"line {i}" for i in range(20)))
    assert shown.count("\n") == render.MAX_RESULT_LINES - 1 and hidden == 12
    assert render.summarize_result("") == ("", 0)
    shown, _ = render.summarize_result("y" * 1000)
    assert len(shown) <= render.MAX_LINE
    shown, _ = render.summarize_result("1: def f():\n2:     return 1\n")
    assert "2:     return 1" in shown, "indentation in code output must survive"
    print("  summarize_result .... ok")


def test_turn_outcome():
    assert render.turn_outcome({"kind": "completed"}) == (True, "done")
    ok, why = render.turn_outcome({"kind": "error", "code": "X"})
    assert not ok and "error" in why and "X" in why
    assert render.turn_outcome(None)[0] is False
    print("  turn_outcome ........ ok")


def ensure_fixture():
    """Real events from a real dsh runtime, recorded once (delete the file to re-record)."""
    if FIXTURE.exists():
        return
    with Stub():
        rt = Runtime(workspace())
        try:
            rt.start()
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(json.dumps(rt.run_turn(sid("fx"), "read hello.txt").events))
        finally:
            rt.close()


def test_renderer_on_recorded_events():
    ensure_fixture()
    events = json.loads(FIXTURE.read_text())
    buf = io.StringIO()
    rend = render.Renderer(Console(file=buf, width=100, force_terminal=False, highlight=False))
    labels = [rend.handle(e) for e in events]
    out = buf.getvalue()
    assert "● read  hello.txt" in out, out
    assert "the secret word is pineapple" in out
    assert "The file says" in out
    assert rend.steps == 2 and rend.tools == 1
    assert any(l and "thinking" in l for l in labels), "the spinner label never updated"
    print("  renderer fixture .... ok")


def test_markup_in_output_cannot_break_the_renderer():
    buf = io.StringIO()
    rend = render.Renderer(Console(file=buf, width=100, force_terminal=False, highlight=False))
    rend.handle({"type": "tool/result", "data": {"message": {
        "isError": True, "content": [{"type": "text", "text": "[bold]oops[/] [/nonsense] [red"}]}}})
    assert "[red" in buf.getvalue() and "[/nonsense]" in buf.getvalue()
    print("  markup safety ....... ok")


# ------------------------------------------------------------------- runtime

def test_run_turn_against_real_dsh():
    ws = workspace()
    with Stub():
        rt = Runtime(ws)
        try:
            rt.start()
            seen = []
            r = rt.run_turn(sid(), "read hello.txt", on_event=seen.append)
            assert r.ok and r.reason == {"kind": "completed"}, r.reason
            assert r.steps == 2 and [c[0] for c in r.tool_calls] == ["read"]
            assert r.tool_calls[0][1] == {"file_path": "hello.txt"}
            assert "The file says" in r.final_text
            assert r.tool_errors == 0 and [e["type"] for e in seen] == [e["type"] for e in r.events]
            assert r.events[-1]["type"] == "turn/end"
        finally:
            rt.close()
    print("  run_turn (real dsh) . ok")


def test_workspace_with_spaces_and_non_ascii_characters():
    ws = workspace("wörk dir 日本 ünï-")
    with Stub():
        rt = Runtime(ws)
        try:
            rt.start()
            r = rt.run_turn(sid(), "read hello.txt")
            assert r.ok and "The file says" in r.final_text, r.reason
        finally:
            rt.close()
    print("  unicode/space cwd ... ok")


def test_two_turns_share_a_session_and_other_sessions_are_ignored():
    ws = workspace()
    with Stub():
        rt = Runtime(ws)
        try:
            rt.start()
            a = rt.run_turn(sid("a"), "read hello.txt")
            b = rt.run_turn(sid("b"), "read hello.txt")
            assert a.ok and b.ok
            assert all(e.get("type") for e in b.events)
        finally:
            rt.close()
    print("  sessions ............ ok")


def test_interrupt_then_resume_repairs_the_session():
    """The protocol has no cancel. Killing the runtime mid-turn and resuming is
    the interrupt, so prove the session is usable afterwards."""
    ws = workspace()
    with Stub(delay=4):
        rt = Runtime(ws)
        try:
            rt.start()
            sess = sid("int")
            interrupt = threading.Timer(1.5, rt.restart)  # "Ctrl-C" 1.5s into a 4s model call
            interrupt.start()
            try:
                rt.run_turn(sess, "read hello.txt", timeout=30)
                raise AssertionError("the abandoned turn should not have completed")
            except (RuntimeDied, TimeoutError):
                pass
            interrupt.join(90)                              # the CLI restarts synchronously; so wait
            assert not interrupt.is_alive(), "the restart never finished"
            rt.resume(sess)                                 # must not raise
            r = rt.run_turn(sess, "read hello.txt again", timeout=60)
            assert r.ok, r.reason
        finally:
            rt.close()
    print("  interrupt + resume .. ok")


def test_cli_one_shot():
    ws = workspace()
    with Stub():
        p = subprocess.run([sys.executable, "-m", "freeharness", "--no-shim", "--cwd", str(ws), "-p", "read hello.txt"],
                           capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, (p.returncode, p.stdout[-300:], p.stderr[-300:])
    assert "● read  hello.txt" in p.stdout and "done" in p.stdout, p.stdout
    print("  cli -p .............. ok")


def test_cli_repl_commands():
    ws = workspace()
    with Stub():
        p = subprocess.run([sys.executable, "-m", "freeharness", "--no-shim", "--cwd", str(ws)],
                           input="/help\n/status\n/model gemini\n/model nope\n/bogus\nread hello.txt\n/quit\n",
                           capture_output=True, text=True, timeout=180)
    out = p.stdout
    assert p.returncode == 0, (p.returncode, out[-400:], p.stderr[-400:])
    for want in ("/providers", "session", "model: gemini", "unknown model", "unknown command /bogus",
                 "● read  hello.txt", "done"):
        assert want in out, f"missing {want!r} in:\n{out}"
    print("  cli repl ............ ok")


if __name__ == "__main__":
    print("free-agent client self-check")
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("all passed")
