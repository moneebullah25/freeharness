"""Prove every tool call is executed by dsh's own tool plugins (real dsh, scripted model, no shim,
no browser). The stub listens on a free port and the overlay patch is rewritten to point at it,
so a live shim on 8766 is never touched. Skipped when node/the harness is not installed."""
import json
import socket
import subprocess
import sys
import tempfile
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
pytestmark = pytest.mark.skipif(not HAVE, reason="node >= 24.9 and the dsh harness are not installed")

ENABLED = {"bash", "read", "write", "edit", "glob", "grep", "todo_write", "skill"}


def run_script(steps, files=None, tmp=None):
    """Run one turn against the real dsh. Returns (TurnResult, [(name, args, result_text, is_error)], workspace, request_log)."""
    tmp = Path(tmp or tempfile.mkdtemp(prefix="fh-tools-"))
    ws = tmp / "ws"
    ws.mkdir()
    for name, content in (files or {}).items():
        (ws / name).parent.mkdir(parents=True, exist_ok=True)
        (ws / name).write_text(content)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script, log = tmp / "script.json", tmp / "requests.jsonl"
    script.write_text(json.dumps(steps + [{"text": "all done"}]))
    patch = tmp / "patch.yml"
    patch.write_text(rpc.PATCH.read_text().replace("127.0.0.1:8766", f"127.0.0.1:{port}"))
    srv = subprocess.Popen([sys.executable, str(HERE / "scripted_openai.py"), str(port), str(script), str(log)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rt = None
    saved_overlays = rpc.Runtime.__dict__["overlays"]
    rpc.Runtime.overlays = staticmethod(lambda: [])      # ignore the user's overlay.d (MCP servers etc.)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        rt = rpc.Runtime(ws, home=tmp / "home", patch=patch)
        rt.start()
        res = rt.run_turn(f"tools-{uuid.uuid4().hex[:8]}", "go", timeout=180)
    finally:
        rpc.Runtime.overlays = saved_overlays
        if rt:
            rt.close()
        srv.terminate()
        srv.wait(5)
    calls = {}
    out = []
    for ev in res.events:
        d = ev.get("data") or {}
        if ev.get("type") == "tool/call":
            calls[d.get("id") or d.get("callId")] = d
        elif ev.get("type") == "tool/result":
            m = d.get("message") or {}
            c = m.get("content")
            text = c if isinstance(c, str) else "".join(b.get("text", "") for b in (c or []) if isinstance(b, dict))
            out.append((m, text))
    return res, out, ws, log


def results(res, out):
    return [(n, t) for (n, _a), (_m, t) in zip(res.tool_calls, out)]


def test_tool_list_and_schemas():
    res, out, ws, log = run_script([{"text": "hi"}])
    body = json.loads(log.read_text().splitlines()[0])
    names = {t["function"]["name"] for t in body["tools"]}
    assert names == ENABLED, names
    by = {t["function"]["name"]: t["function"]["parameters"] for t in body["tools"]}
    assert "command" in by["bash"]["properties"] and "run_in_background" not in by["bash"]["properties"]
    assert "file_path" in by["read"]["properties"]


def test_bash_pipelines_run_in_dsh():
    files = {"src/a.py": "import os\nTODO one\n", "src/b.py": "x = 1\nTODO two\nTODO three\n", "n.txt": "foo\nbar\n"}
    cmds = ["find . -name '*.py' | sort", "grep -rn TODO src | wc -l", "sed -n '2p' n.txt", "ls src | head -1; pwd",
            "tree -L 1 . 2>&1 || echo no-tree", "awk 'NR==1{print $1}' n.txt"]
    res, out, ws, _ = run_script([{"calls": [{"name": "bash", "args": {"description": "t", "command": c}} for c in cmds]}], files)
    assert res.ok and res.tool_errors == 0
    texts = [t for _n, t in results(res, out)]
    assert len(texts) == len(cmds)
    assert "./src/a.py" in texts[0] and "./src/b.py" in texts[0]
    assert texts[1].strip().split()[-1] == "3"
    assert "bar" in texts[2]
    assert "a.py" in texts[3] and str(ws.resolve()) in texts[3]       # cwd confinement: starts in the workspace
    assert "no-tree" in texts[4] or "src" in texts[4]
    assert "foo" in texts[5]


def test_bash_nonzero_exit_reaches_the_model():
    res, out, ws, log = run_script([{"calls": [{"name": "bash", "args": {"description": "t", "command": "echo oops >&2; exit 3"}}]}])
    (_n, t), = results(res, out)
    assert "3" in t and "oops" in t, t
    # the model's next request carries the error back
    last = json.loads(log.read_text().splitlines()[-1])
    assert any(m["role"] == "tool" and "oops" in json.dumps(m["content"]) for m in last["messages"])


def test_bash_timeout():
    res, out, ws, _ = run_script([{"calls": [{"name": "bash", "args": {"description": "t", "command": "sleep 30", "timeoutMs": 1500}}]}])
    (_n, t), = results(res, out)
    assert "background" not in t and "job_output" not in t, t     # tool-jobs is disabled: never promote
    assert "timed out after 1500ms" in t and "SIGTERM" in t, t     # dsh kills it and says so (isError stays unset)
    assert res.seconds < 25


def test_grep_glob_read_write_edit():
    files = {"a.txt": "\n".join(f"line{i}" for i in range(1, 11)) + "\n", "d/b.md": "needle\n", "dup.txt": "x\nx\n"}
    steps = [{"calls": [
        {"name": "grep", "args": {"pattern": "needle"}},
        {"name": "glob", "args": {"pattern": "**/*.md"}},
        {"name": "read", "args": {"file_path": "a.txt", "offset": 4, "limit": 2}},
        {"name": "write", "args": {"file_path": "new/out.txt", "content": "written\n"}},
        {"name": "edit", "args": {"file_path": "dup.txt", "old_string": "x", "new_string": "y"}},
        {"name": "read", "args": {"file_path": "../../etc/hostname"}},
    ]}]
    res, out, ws, _ = run_script(steps, files)
    r = [t for _n, t in results(res, out)]
    assert "b.md" in r[0] and "needle" in r[0]
    assert "b.md" in r[1]
    assert "line4" in r[2] and "line5" in r[2] and "line1\n" not in r[2] and "line9" not in r[2], r[2]
    assert (ws / "new" / "out.txt").read_text() == "written\n"
    assert (ws / "dup.txt").read_text() == "x\nx\n", "a non-unique edit must not modify the file"
    assert out[4][0].get("isError") or "unique" in r[4].lower() or "multiple" in r[4].lower() or "2" in r[4], r[4]
    assert res.tool_errors >= 1


def test_shim_renders_the_rules_that_matter_for_dsh_tools():
    from freeharness.backends import shim
    res, out, ws, log = run_script([{"text": "hi"}])
    tools = json.loads(log.read_text().splitlines()[0])["tools"]
    text = "\n".join(shim.render_tool(t) for t in tools)
    assert "must appear exactly once" in text                      # edit.replace_all
    assert "truncated to its tail" in text                         # bash output limit
    assert "up to 250 matches" in text                             # grep limit
    assert "Defaults to 2000" in text                              # read limit
    assert "sandbox_permissions" not in text and "justification" not in text
    assert len(text) < 4200, len(text)                             # the prompt stays small


def test_output_truncation_limits():
    files = {"big.txt": "".join(f"row{i}\n" for i in range(1, 3001)), "long.txt": "z" * 5000 + "\n"}
    steps = [{"calls": [
        {"name": "bash", "args": {"description": "t", "command": "seq 1 40000"}},         # ~239 KB, cap is 64 KB
        {"name": "read", "args": {"file_path": "big.txt"}},                              # default limit 2000 lines
        {"name": "read", "args": {"file_path": "long.txt"}},                             # 2000-char line cap
    ]}]
    res, out, ws, _ = run_script(steps, files)
    b, rd, lg = [t for _n, t in results(res, out)]
    assert len(b) < 90_000 and "40000" in b and "\n1\n2\n" not in b, len(b)   # tail kept, head dropped
    assert "row2000" in rd and "row2001" not in rd, "read must stop at 2000 lines"
    assert len(lg) < 4000, len(lg)
