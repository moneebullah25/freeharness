#!/usr/bin/env python3
"""The whole eval harness (dsh runtime -> JSON-RPC -> OpenAI endpoint -> real tools -> judge)
run offline against tests/fake_openai.py with SCRIPTED model replies: one passing and one
failing script for every task of the extended suite. Proves the judges decide from the
resulting workspace and the captured tool calls, whatever the "model" says.

Needs the DeepSeek Harness and Node >= 24.9 (`freeharness setup`); skipped without them.
The stub listens on its own free port (a shim running on 8766 is left alone). No browser, no network.

Run: .venv/bin/python tests/test_eval_harness.py     or pytest tests/test_eval_harness.py
"""
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from freeharness.client import rpc  # noqa: E402
from freeharness.eval import run as ev  # noqa: E402
from freeharness.eval.tasks import SUITES  # noqa: E402


def _available() -> str | None:
    if not rpc.DSH_BIN.exists():
        return "DeepSeek Harness not installed (run `freeharness setup`)"
    try:
        rpc.find_node_dir()
    except RuntimeError as e:
        return str(e)[:80]
    return None


WHY = _available()
needs_harness = pytest.mark.skipif(WHY is not None, reason=WHY or "")

SCRIPT = Path(tempfile.gettempdir()) / "freeharness-eval-harness-script.json"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def stub():
    """Stub endpoint on a free port, plus a dsh patch pointing the bridge provider at it."""
    port = _free_port()
    patch = Path(tempfile.mkdtemp(prefix="eval-harness-patch-")) / "dsh.patch.yml"
    patch.write_text(rpc.PATCH.read_text().replace("127.0.0.1:8766", f"127.0.0.1:{port}"))
    p = subprocess.Popen([sys.executable, str(HERE / "fake_openai.py")],
                         env=dict(os.environ, FAKE_SCRIPT=str(SCRIPT), FAKE_OPENAI_PORT=str(port)),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    real_runtime = ev.Runtime
    ev.Runtime = lambda *a, **k: real_runtime(*a, patch=patch, **k)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("stub did not start")
        yield
    finally:
        ev.Runtime = real_runtime
        p.terminate()
        p.wait(5)


def bash(cmd):
    return {"tool": "bash", "args": {"command": cmd, "description": "scripted"}}


def text(t):
    return {"text": t}


# task id -> (passing script, failing script, substring expected in the failure detail)
SCRIPTS = {
    "explore-dir": (
        [bash("find data -name '*.log' | wc -l; find data -name '*.log' -exec cat {} + | wc -l"), text("beta 26")],
        [bash("find data -name '*.log' | wc -l"), text("alpha 26")],
        "expected 'beta 26'"),
    "refactor-3files": (
        [bash(r"sed -i 's/\bparse_row\b/parse_record/g' pkg/rows.py pkg/report.py pkg/cli.py"),
         bash("python3 check.py"), text("renamed")],
        [bash("python3 check.py"), text("renamed everything, it works")],        # claims success, did nothing
        "parse_row still in"),
    "skill-use": (
        [{"tool": "skill", "args": {"name": "release-tag"}}, bash("echo rel-2.4.0-HALCYON-5521 > tag.txt"), text("done")],
        [bash("echo rel-2.4.0-HALCYON-5521 > tag.txt"), text("done")],             # right file, skill never loaded
        "no tool use"),
    "vault-mcp": (
        [{"tool": "vault_search", "args": {"query": "pager rotation"}},
         {"tool": "vault_read", "args": {"path": "ops-runbook.md"}}, bash("echo MERIDIAN-3318 > answer.txt"), text("done")],
        [{"tool": "vault_search", "args": {"query": "pager rotation"}}, bash("echo DECOY-0001 > answer.txt"), text("done")],
        "answer.txt is 'DECOY-0001'"),
    "resume-continue": (
        [bash("echo ready > step1.txt"), text("ok"), bash("echo QUARTZ-9082 > step2.txt"), text("done")],
        [bash("echo ready > step1.txt"), text("ok"), bash("echo UNKNOWN > step2.txt"), text("done")],
        "step2.txt is 'UNKNOWN'"),
}


def _run(task_id, script):
    SCRIPT.write_text(json.dumps(script))
    return ev.run_one(ev.BY_ID[task_id], "auto")


@needs_harness
@pytest.mark.parametrize("task_id", sorted(SCRIPTS))
def test_extended_task_pass_and_fail(task_id):
    good, bad, why = SCRIPTS[task_id]
    with stub():
        r = _run(task_id, good)
        assert r["ok"], f"{task_id} should pass: {r['detail']} {r.get('error')}"
        assert r["tool_errors"] == 0, f"{task_id}: a scripted tool call was rejected by dsh (bad arguments in the script)"
        assert r["steps"] >= 2 and r["tool_calls"] >= 1 and r["tools"] and r["seconds"] > 0
        assert r["tool_counts"] and len(r["turn_seconds"]) == r["turns"]
        r = _run(task_id, bad)
        assert not r["ok"] and why in r["detail"], f"{task_id} should fail with {why!r}: {r['detail']}"
    SCRIPT.unlink(missing_ok=True)


@needs_harness
def test_answering_without_tools_fails_and_disqualifies():
    with stub():
        r = _run("vault-mcp", [text("The code is MERIDIAN-3318.")])
    assert not r["ok"] and r["detail"].startswith("no tool use")
    assert ev.verdict([r] + [{"task": "x", "ok": True, "detail": ""}] * 7)["answered_without_tools"] == ["vault-mcp"]
    SCRIPT.unlink(missing_ok=True)


@needs_harness
def test_main_records_suite_and_per_task_steps_tools_time():
    state = tempfile.mkdtemp(prefix="eval-state-")
    old = {k: os.environ.get(k) for k in ("XDG_STATE_HOME",)}
    os.environ["XDG_STATE_HOME"] = state
    orig, orig_health = ev.wait_ready, ev.shim_health
    ev.wait_ready = lambda *a, **k: True
    ev.shim_health = lambda *a, **k: None
    SCRIPT.write_text(json.dumps(SCRIPTS["vault-mcp"][0]))
    try:
        with stub():
            code = ev.main(["--model", "auto", "--no-shim", "--tasks", "vault-mcp"])
    finally:
        ev.wait_ready, ev.shim_health = orig, orig_health
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        SCRIPT.unlink(missing_ok=True)
    saved = list(Path(state).rglob("*-auto.json"))
    assert saved, "no result file written"
    data = json.loads(saved[0].read_text())
    row = data["results"][0]
    assert data["suite"] == "custom" and row["task"] == "vault-mcp" and row["ok"]
    assert row["steps"] >= 3 and any(t.endswith("vault_search") for t in row["tools"]) and row["seconds"] > 0
    assert code == 0


def test_suites_core_is_the_original_eight_and_full_is_a_superset():
    core, full = [t.id for t in SUITES["core"]], [t.id for t in SUITES["full"]]
    assert core == ["read-answer", "write-file", "fix-bug", "edit-config", "count-todos",
                    "rename-function", "run-and-report", "write-and-run-test"]
    assert full[:8] == core and set(full[8:]) == set(SCRIPTS) and len(full) == 13


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
