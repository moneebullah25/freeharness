"""Real dsh + tests/fake_openai.py: does dsh keep OLD tool results verbatim in later requests?

Hypothesis under test: `contextWindow: 16000` in data/dsh.patch.yml made dsh condense/prune older
history as pressure neared 16k tokens, so a model that read a file once lost its content and asked again.
This drives a long scripted session (one read of TARGET, then many reads of filler files) and inspects
the request BODIES the model endpoint receives. No browser, no live provider; skipped without node/dsh.
"""
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

from freeharness.client import rpc

HERE = Path(__file__).resolve().parent
try:
    rpc.find_node_dir()
    HAVE = rpc.DSH_BIN.exists()
except Exception:  # noqa: BLE001
    HAVE = False
pytestmark = pytest.mark.skipif(not HAVE, reason="node / dsh harness not installed")

MARK = "UNIQUE-MARK-ZEBRA-4417"
FILLERS = 30


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_session(tmp_path, window):
    """Run the scripted session with every model's contextWindow set to `window` (None = shipped patch
    unchanged). Returns the list of request bodies the fake model endpoint received."""
    port = free_port()
    log = tmp_path / "log"
    log.mkdir()
    script = [{"tool": "read", "args": {"file_path": "target.txt"}}]
    script += [{"tool": "read", "args": {"file_path": f"filler{i}.txt"}} for i in range(FILLERS)]
    script += [{"text": "all done"}]
    p = subprocess.Popen([sys.executable, str(HERE / "fake_openai.py")], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         env=dict(os.environ, FAKE_OPENAI_PORT=str(port), FAKE_OPENAI_LOG=str(log),
                                  FAKE_SCRIPT=json.dumps(script)))
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        text = rpc.PATCH.read_text().replace("127.0.0.1:8766", f"127.0.0.1:{port}")
        if window is not None:
            assert "&context_window 1000000" in text, "the patch must keep ONE named window constant"
            text = text.replace("&context_window 1000000", f"&context_window {window}")
        patch = tmp_path / "patch.yml"
        patch.write_text(text)
        home, proj = tmp_path / "home", tmp_path / "proj"
        home.mkdir()
        proj.mkdir()
        lines = [f"line {n} of the target file" + (f" {MARK}" if n == 20 else "") for n in range(1, 40)]
        (proj / "target.txt").write_text("\n".join(lines) + "\n")
        for i in range(FILLERS):
            (proj / f"filler{i}.txt").write_text("\n".join(f"filler{i} row {n} " + "x" * 80 for n in range(1, 36)) + "\n")
        rt = rpc.Runtime(proj, home=home, patch=patch, overlay_files=[])
        try:
            rt.start()
            r = rt.run_turn(f"win-{uuid.uuid4().hex[:8]}", "read target.txt, then every filler file", timeout=240)
        finally:
            rt.close()
        assert r.ok, r.reason
        return [json.loads(x) for x in (log / "freeharness-fake-requests.jsonl").read_text().splitlines()]
    finally:
        p.terminate()
        p.wait(5)


def _size(body):
    return len(json.dumps(body["messages"]))


def _tool_text(body):
    out = []
    for m in body["messages"]:
        if m.get("role") == "tool":
            c = m.get("content")
            out.append(c if isinstance(c, str) else json.dumps(c))
    return "\n".join(out)


def _held(reqs):
    return [MARK in _tool_text(b) for b in reqs]


def test_shipped_window_keeps_every_old_tool_result_verbatim(tmp_path):
    """The shipped patch (1,000,000 tokens): the file read in request 0 is in EVERY later request, and
    request sizes only grow (nothing condensed, nothing pruned, no extra summarisation requests)."""
    reqs = run_session(tmp_path, None)
    sizes = [_size(b) for b in reqs]
    assert len(reqs) == FILLERS + 2, "one request per script step: no summarisation request in between"
    assert all(_held(reqs)[1:]), "an old tool result vanished from a later request"
    assert sizes == sorted(sizes), "history shrank: something was condensed or pruned"
    print(f"\n  shipped window: {len(reqs)} requests, {sizes[0]} -> {sizes[-1]} chars, target kept in all")


def test_control_16000_never_condensed_in_this_setup(tmp_path):
    """The OLD value. Measured: dsh-compaction-basic's trigger is min(0.8*W, W - output - 65,536 headroom);
    for W=16000 that is negative, so dsh warns and skips proactive compaction: the old value did NOT make
    dsh drop old results. (The shim's own trimming at max_prompt was what blanked them.)"""
    reqs = run_session(tmp_path, 16000)
    assert all(_held(reqs)[1:]) and len(reqs) == FILLERS + 2


def test_control_80000_condenses_and_loses_the_old_result(tmp_path):
    """A window where dsh's compaction IS live (trigger ~14k tokens): older history is replaced by a
    model-written summary, extra summary requests appear, and the file content is gone from later requests."""
    reqs = run_session(tmp_path, 80000)
    sizes = [_size(b) for b in reqs]
    assert not all(_held(reqs)[1:]), "expected compaction to drop the old tool result at this window"
    assert len(reqs) > FILLERS + 2, "expected extra summarisation requests"
    assert sizes != sorted(sizes)
    print(f"\n  window 80000: {len(reqs)} requests, max {max(sizes)}, last {sizes[-1]} chars, target lost")
