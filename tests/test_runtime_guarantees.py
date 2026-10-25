"""
Runtime guarantees: what DSH owns and what the client adds, proven offline.

Real `dsh --profile sdk` against tests/fake_openai.py with a scripted reply per
model request. No shim, no browser, no network beyond loopback. The stub listens
on a private free port (a patched copy of dsh.patch.yml points at it) and every
test runs in its own DSH home, so a live shim on :8766 is never touched.
Skipped when the pinned harness or a suitable Node is missing.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from freeharness.client import rpc
from freeharness.client.rpc import Runtime, RuntimeDied

HERE = Path(__file__).resolve().parent


def _can_run() -> bool:
    if not rpc.DSH_BIN.exists():
        return False
    try:
        rpc.find_node_dir()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(not _can_run(), reason="DeepSeek Harness / Node >= 24.9 not installed")

READ = {"tool": "read", "args": {"file_path": "hello.txt"}}
BASH_OK = {"tool": "bash", "args": {"command": "echo recovered > out.txt", "description": "write out.txt"}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    """A scripted stub model + a patch pointing dsh at it + an isolated workspace."""

    def __init__(self, script):
        self.port = free_port()
        self.tmp = Path(tempfile.mkdtemp(prefix="fh-guarantee-"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        (self.ws / "hello.txt").write_text("the secret word is pineapple\n")
        self.log = self.tmp / "log"
        self.log.mkdir()
        self.patch = self.tmp / "dsh.patch.yml"
        self.patch.write_text(rpc.PATCH.read_text().replace("127.0.0.1:8766", f"127.0.0.1:{self.port}"))
        self.script = script

    def requests(self) -> list:
        f = self.log / "freeharness-fake-requests.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def __enter__(self):
        env = dict(os.environ, FAKE_PORT=str(self.port), FAKE_SCRIPT=json.dumps(self.script),
                   FAKE_OPENAI_LOG=str(self.log))
        self.stub = subprocess.Popen([sys.executable, str(HERE / "fake_openai.py")], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/models", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("stub did not start")
        self.rt = Runtime(self.ws, home=self.tmp / "dsh-home", patch=self.patch)
        self.rt.start()
        return self

    def __exit__(self, *a):
        self.rt.close()
        self.stub.terminate()
        self.stub.wait(5)


def sid(tag="g"):
    return f"test-{tag}-{uuid.uuid4().hex[:8]}"


def results(turn):
    return [e["data"]["message"] for e in turn.events if e["type"] == "tool/result"]


def text_of(msg) -> str:
    return "".join(b.get("text", "") for b in msg.get("content", []) if isinstance(b, dict))


# --------------------------------------------------------------- step limit

def test_step_limit_abandons_the_turn_and_the_session_survives():
    """dsh has no step limit; the client enforces one, abandons, and resumes."""
    with Env([READ, READ, READ, READ, {"text": "finished"}]) as e:
        s = sid("steps")
        r = e.rt.run_turn(s, "loop", max_steps=3, timeout=60)
        assert not r.ok and r.reason == {"kind": "max_steps", "limit": 3}, r.reason
        assert r.steps == 3 and len(r.tool_calls) <= 3
        again = e.rt.run_turn(s, "now stop", max_steps=3, timeout=60)     # same session, fresh runtime
        assert again.ok and again.final_text == "finished", again.reason


def test_step_limit_does_not_trip_a_turn_that_fits():
    with Env([READ, {"text": "ok"}]) as e:
        r = e.rt.run_turn(sid(), "go", max_steps=2, timeout=60)
        assert r.ok and r.steps == 2


# ----------------------------------------------------------- tool-error recovery

def test_tool_error_is_shown_to_the_model_which_retries():
    bad = {"tool": "bash", "args": {"command": "echo nope"}}                 # missing `description`
    with Env([bad, BASH_OK, {"text": "all good"}]) as e:
        r = e.rt.run_turn(sid(), "write out.txt", timeout=60)
        assert r.ok and r.tool_errors == 1 and len(r.tool_calls) == 2, (r.reason, r.tool_errors)
        assert (e.ws / "out.txt").read_text().strip() == "recovered"
        second = e.requests()[1]["messages"]                                  # what the model saw next
        seen = [m["content"] for m in second if m["role"] == "tool"]
        assert any("invalid arguments" in (c if isinstance(c, str) else json.dumps(c)) for c in seen), seen


# ------------------------------------------------------------------- timeouts

def test_hung_model_reply_hits_the_client_timeout_and_abandon_recovers():
    with Env([{"sleep": 40, "then": {"text": "too late"}}, {"text": "back"}]) as e:
        s = sid("hung")
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            e.rt.run_turn(s, "go", timeout=8)
        assert time.monotonic() - t0 < 25
        assert len(e.requests()) == 1, "the model request must have been in flight when the client gave up"
        e.rt.abandon(s)
        r = e.rt.run_turn(s, "again", timeout=60)
        assert r.ok and r.final_text == "back", r.reason


# ------------------------------------------------------------- runtime crash

def kill_runtime_when(env, ready, then_wait=1.0):
    """SIGKILL dsh once `ready()` holds (so the kill never races the turn's start)."""
    def go():
        deadline = time.monotonic() + 30
        while not ready() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(then_wait)
        env.rt._proc.kill()
    threading.Thread(target=go, daemon=True).start()


def test_runtime_killed_mid_model_call_then_resumed():
    with Env([{"sleep": 40, "then": {"text": "x"}}, {"text": "after crash"}]) as e:
        s = sid("crash")
        kill_runtime_when(e, lambda: len(e.requests()) >= 1)
        with pytest.raises(RuntimeDied):
            e.rt.run_turn(s, "go", timeout=60)
        e.rt.abandon(s)
        r = e.rt.run_turn(s, "continue", timeout=60)
        assert r.ok and r.final_text == "after crash", r.reason


def test_runtime_killed_mid_tool_then_resumed_and_model_is_told():
    slow = {"tool": "bash", "args": {"command": "sleep 60", "description": "sleep"}}
    with Env([slow, {"text": "recovered"}]) as e:
        s = sid("tool")
        kill_runtime_when(e, lambda: len(e.requests()) >= 1, then_wait=4.0)   # tool call is in flight
        with pytest.raises(RuntimeDied):
            e.rt.run_turn(s, "go", timeout=60)
        e.rt.abandon(s)
        r = e.rt.run_turn(s, "continue", timeout=60)
        assert r.ok, r.reason
        # the interrupted call got an explicit 'interrupted, do not retry blindly' result, not silence: the history stays paired
        history = e.requests()[-1]["messages"]
        tool_msgs = [m for m in history if m["role"] == "tool"]
        assert tool_msgs and any("interrupted" in json.dumps(m["content"]) for m in tool_msgs), history[-3:]


# ------------------------------------------- approval / trust model / confinement

def test_approval_never_hangs_and_writes_outside_the_workspace_are_denied():
    probe = Path.home() / f".freeharness-probe-{uuid.uuid4().hex[:8]}"
    escalate = {"tool": "bash", "args": {"command": f"touch {probe}.b", "description": "t",
                                         "sandbox_permissions": "danger-full-access", "justification": "x"}}
    write = {"tool": "write", "args": {"file_path": str(probe) + ".w", "content": "x"}}
    old = os.environ.get("DSH_PERMISSION_MODE")
    os.environ["DSH_PERMISSION_MODE"] = "danger-full-access"      # an inherited widening must be ignored
    try:
        with Env([write, escalate, {"text": "stopped"}]) as e:
            t0 = time.monotonic()
            r = e.rt.run_turn(sid("perm"), "go", timeout=60)
            assert r.ok and r.tool_errors == 2, (r.reason, r.tool_errors)
            assert time.monotonic() - t0 < 30, "approval must not wait on anyone"
            msgs = [text_of(m) for m in results(r)]
            assert "sandbox" in msgs[0] and "rejected" in msgs[1], msgs
        assert not Path(str(probe) + ".w").exists() and not Path(str(probe) + ".b").exists()
    finally:
        if old is None:
            os.environ.pop("DSH_PERMISSION_MODE", None)
        else:
            os.environ["DSH_PERMISSION_MODE"] = old
        for suffix in (".w", ".b"):
            Path(str(probe) + suffix).unlink(missing_ok=True)


def test_write_and_edit_inside_the_workspace_need_no_approval():
    w = {"tool": "write", "args": {"file_path": "new.txt", "content": "x"}}
    ed = {"tool": "edit", "args": {"file_path": "new.txt", "old_string": "x", "new_string": "y"}}
    with Env([w, ed, {"text": "ok"}]) as e:
        r = e.rt.run_turn(sid(), "go", timeout=60)
        assert r.ok and r.tool_errors == 0
        assert (e.ws / "new.txt").read_text() == "y"


# ----------------------------------------------------------- -p exit codes

def run_cli(script, *args, timeout=180):
    """`freeharness -p` in a subprocess with a relocated home that reuses the real harness install."""
    env_ = Env(script)
    home = env_.tmp / "fh-home"
    (home / "data").mkdir(parents=True)
    (home / "data" / "harness").symlink_to(rpc.paths.harness_dir())
    ov = home / "config" / "overlay.d"
    ov.mkdir(parents=True)
    (ov / "port.yml").write_text(env_.patch.read_text())   # same patch, port rewritten: overlays replace provider config
    env = dict(os.environ, FREEHARNESS_HOME=str(home), FAKE_PORT=str(env_.port),
               FAKE_SCRIPT=json.dumps(script), FAKE_OPENAI_LOG=str(env_.log))
    stub = subprocess.Popen([sys.executable, str(HERE / "fake_openai.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{env_.port}/v1/models", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        cwd = args[-1] if args and args[-1] == "__missing__" else None
        argv = [sys.executable, "-m", "freeharness", "--no-shim",
                "--cwd", str(env_.tmp / "nope") if cwd else str(env_.ws),
                *[a for a in args if a != "__missing__"], "-p", "go"]
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
    finally:
        stub.terminate()
        stub.wait(5)


def test_cli_p_exit_0_on_completed_turn():
    p = run_cli([{"text": "hi"}])
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-400:])


def test_cli_p_exit_1_on_step_limit():
    p = run_cli([READ], "--max-steps", "2")
    assert p.returncode == 1 and "max_steps" in p.stdout, (p.returncode, p.stdout[-400:], p.stderr[-400:])


def test_cli_p_exit_1_on_turn_timeout():
    p = run_cli([{"sleep": 40, "then": {"text": "late"}}], "--turn-timeout", "3")
    assert p.returncode == 1 and "runtime problem" in p.stdout, (p.returncode, p.stdout[-400:], p.stderr[-400:])


def test_cli_p_exit_2_when_the_workspace_does_not_exist():
    p = run_cli([{"text": "hi"}], "__missing__")
    assert p.returncode == 2, (p.returncode, p.stdout[-400:], p.stderr[-400:])
    assert "Traceback" not in p.stderr, p.stderr[-400:]
