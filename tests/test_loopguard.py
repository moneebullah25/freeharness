"""Loop guard: the pure detector, the event watcher, the oss engine's repair turn, the interactive warning, and a
real-dsh check that a turn really ends. OFF by default everywhere."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from freeharness import loopguard, oss_cmd
from freeharness.client import rpc
from freeharness.client.rpc import TurnResult

from test_oss_flow import StubRuntime, stub  # noqa: F401, F811  (fixture)

HERE = Path(__file__).resolve().parent


def c(name="bash", args=None, result="clean"):
    return (name, args if args is not None else {"command": "git status"}, result)


# ------------------------------------------------------------------ the pure function

def test_three_identical_calls_with_identical_results_in_the_last_eight_are_a_loop():
    rep = loopguard.detect_repeat([c(), c(), c()])
    assert rep["name"] == "bash" and rep["count"] == 3 and rep["window"] == 3
    assert rep["result"] == "clean"


def test_two_repeats_are_not_a_loop():
    assert loopguard.detect_repeat([c(), c()]) is None
    assert loopguard.detect_repeat([c(), c(), c("read", {"file_path": "a"}, "x")]) is None


def test_same_call_with_a_different_result_never_counts():
    calls = [c(result="a"), c(result="b"), c(result="c"), c(result="d")]
    assert loopguard.detect_repeat(calls) is None


def test_same_result_for_different_arguments_never_counts():
    calls = [c(args={"command": f"ls {i}"}) for i in range(5)]
    assert loopguard.detect_repeat(calls) is None


def test_the_window_is_the_last_eight_calls_and_only_the_newest_call_triggers():
    old = [c(), c(), c()]                                         # a loop that has ended ...
    fillers = [c("read", {"file_path": f"f{i}"}, f"r{i}") for i in range(8)]
    assert loopguard.detect_repeat(old + fillers) is None         # ... is outside the window and not the newest call
    spread = [c(), c("read", {"file_path": "a"}, "1"), c(), c("read", {"file_path": "b"}, "2"), c()]
    assert loopguard.detect_repeat(spread)["count"] == 3          # not consecutive: still 3 of the last 8
    assert loopguard.detect_repeat([c()] * 3 + [c("read", {"file_path": "z"}, "9")]) is None   # newest call is another call
    assert loopguard.detect_repeat([c()] * 10)["window"] == 8 and loopguard.detect_repeat([c()] * 10)["count"] == 8


def test_argument_order_and_json_string_arguments_are_normalised():
    a = ("bash", {"command": "ls", "workdir": "/w"}, "o")
    b = ("bash", json.dumps({"workdir": "/w", "command": "ls"}), "o")
    assert loopguard.detect_repeat([a, b, a])["count"] == 3
    assert loopguard.detect_repeat([("bash", {"c": 1}, " o\n"), ("bash", {"c": 1}, "o"), ("bash", {"c": 1}, "o ")])["count"] == 3


def test_thresholds_are_parameters():
    assert loopguard.detect_repeat([c(), c()], threshold=2)["count"] == 2
    assert loopguard.detect_repeat([c(), c(), c()], threshold=4) is None


def test_off_unless_asked(monkeypatch):
    monkeypatch.delenv("FREEHARNESS_LOOP_GUARD", raising=False)
    assert loopguard.enabled() is False and loopguard.enabled(True) is True
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("FREEHARNESS_LOOP_GUARD", v)
        assert loopguard.enabled() is True
    monkeypatch.setenv("FREEHARNESS_LOOP_GUARD", "0")
    assert loopguard.enabled() is False


def call_events(n, name="bash", args=None, text="clean", prefix="c"):
    out = []
    for i in range(n):
        cid = f"{prefix}{i}"
        out.append({"type": "tool/call", "data": {"callId": cid, "name": name, "arguments": json.dumps(args or {"command": "git status"})}})
        out.append({"type": "tool/result", "data": {"message": {"toolCallId": cid, "content": [{"type": "text", "text": text}]}}})
    return out


def test_watch_reports_on_the_third_identical_result_and_resets():
    w = loopguard.Watch()
    got = [w.feed(e) for e in call_events(3)]
    assert got[:4] == [None, None, None, None] and got[5]["count"] == 3
    w.reset()
    assert [x for x in (w.feed(e) for e in call_events(2)) if x] == []


def test_repair_text_quotes_the_call_and_never_names_the_next_action():
    rep = loopguard.detect_repeat([c(result="nothing to commit, working tree clean")] * 3)
    text = loopguard.repair_text(rep)
    assert "bash" in text and "git status" in text and "nothing to commit, working tree clean" in text
    assert "3 times" in text and "same output" in text and "different action" in text.lower()
    assert "git status" in loopguard.describe(rep)


# ------------------------------------------------------------ the oss stage engine

class LoopRuntime(StubRuntime):
    """The first turn loops (the guard fires, as the real Runtime.run_turn would); later turns complete."""

    def run_turn(self, sid, text, on_event=None, timeout=0, max_steps=None, stop_when=None):
        StubRuntime.log.append(("turn", self.model, sid, max_steps, text, stop_when is not None))
        looped = sum(1 for e in StubRuntime.log if e[0] == "turn") == 1
        if stop_when and looped:
            for ev in call_events(4):
                why = stop_when(ev)
                if why:
                    return TurnResult(False, {"kind": "loop_guard", **why}, "", 4, [], 0, 1.0, [])
        return TurnResult(True, {"kind": "completed"}, "done", 3, [], 0, 1.0, [])


def test_guard_is_off_by_default_in_the_oss_engine(stub, monkeypatch):  # noqa: F811
    monkeypatch.delenv("FREEHARNESS_LOOP_GUARD", raising=False)
    monkeypatch.setattr(oss_cmd.rpc, "Runtime", LoopRuntime)
    oss_cmd.run("up/proj", "src", ["auto"], False, 0, 60.0, no_shim=True, root=stub / "runs", say=lambda s: None, repair=0)
    turns = [e for e in StubRuntime.log if e[0] == "turn"]
    assert turns and all(t[5] is False for t in turns), "no stop_when handed to the runtime"


def test_guard_ends_the_turn_and_sends_a_repair_turn_that_quotes_the_call(stub, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FREEHARNESS_LOOP_GUARD", "1")
    monkeypatch.setattr(oss_cmd.rpc, "Runtime", LoopRuntime)
    said = []
    code, rd = oss_cmd.run("up/proj", "src", ["auto"], False, 0, 60.0, no_shim=True, root=stub / "runs", say=said.append, repair=0)
    turns = [e for e in StubRuntime.log if e[0] == "turn"]
    assert turns[0][5] is True and "OSS contribution, stage 1 of 6" in turns[0][4]
    second = turns[1][4]
    assert second.startswith("The harness noticed a loop") and "git status" in second and "same output" in second
    assert "different" in second and "STAGE DONE" not in second.split("different")[0]
    assert turns[1][1] == turns[0][1] and turns[1][2] == turns[0][2], "same model, same session"
    assert any("loop guard" in s for s in said)
    s = json.loads(rd.state.read_text())
    assert s["stages"]["setup"]["repairs"] == 0, "a loop repair is not an acceptance repair"


class AlwaysLoops(LoopRuntime):
    def run_turn(self, sid, text, on_event=None, timeout=0, max_steps=None, stop_when=None):
        StubRuntime.log.append(("turn", self.model, sid, max_steps, text, stop_when is not None))
        for ev in call_events(4):
            why = stop_when(ev) if stop_when else None
            if why:
                return TurnResult(False, {"kind": "loop_guard", **why}, "", 4, [], 0, 1.0, [])
        return TurnResult(True, {"kind": "completed"}, "done", 3, [], 0, 1.0, [])


def test_a_persistent_loop_is_repaired_at_most_twice_per_turn(stub, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FREEHARNESS_LOOP_GUARD", "1")
    monkeypatch.setattr(oss_cmd.rpc, "Runtime", AlwaysLoops)
    oss_cmd.run("up/proj", "src", ["auto"], False, 0, 60.0, no_shim=True, root=stub / "runs", say=lambda s: None, repair=0)
    first_stage = []
    for e in StubRuntime.log:
        if e[0] == "turn":
            first_stage.append(e)
            if len(first_stage) == 3:
                break
    assert [t[4].startswith("The harness noticed a loop") for t in first_stage] == [False, True, True]
    assert first_stage[1][4].startswith("The harness noticed a loop")


# ------------------------------------------------------------ the interactive client

def test_client_flag_and_help_exist():
    from freeharness.client import cli
    src = Path(cli.__file__).read_text()
    assert "--loop-guard" in src and "loopguard.Watch()" in src


# ------------------------------------------------------------ real dsh: the turn really ends

try:
    rpc.find_node_dir()
    HAVE = rpc.DSH_BIN.exists()
except Exception:  # noqa: BLE001
    HAVE = False


@pytest.mark.skipif(not HAVE, reason="node / dsh harness not installed")
def test_real_dsh_turn_ends_when_the_same_read_repeats(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = tmp_path / "log"
    log.mkdir()
    script = [{"tool": "read", "args": {"file_path": "a.txt"}}] * 12 + [{"text": "done"}]
    p = subprocess.Popen([sys.executable, str(HERE / "fake_openai.py")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=dict(os.environ, FAKE_OPENAI_PORT=str(port), FAKE_OPENAI_LOG=str(log), FAKE_SCRIPT=json.dumps(script)))
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        patch = tmp_path / "patch.yml"
        patch.write_text(rpc.PATCH.read_text().replace("127.0.0.1:8766", f"127.0.0.1:{port}"))
        home, proj = tmp_path / "home", tmp_path / "proj"
        home.mkdir()
        proj.mkdir()
        (proj / "a.txt").write_text("same content\n")
        rt = rpc.Runtime(proj, home=home, patch=patch, overlay_files=[])
        try:
            rt.start()
            r = rt.run_turn(f"lg-{uuid.uuid4().hex[:8]}", "read a.txt", timeout=120, stop_when=loopguard.Watch().feed)
        finally:
            rt.close()
        assert not r.ok and r.reason["kind"] == "loop_guard" and r.reason["count"] == 3 and r.reason["name"] == "read"
        assert r.steps <= 4, "the turn ended at the third identical result, not after all 12 calls"
    finally:
        p.terminate()
        p.wait(5)
