"""Real dsh + tests/fake_openai.py (own port, temp DSH_HOME): what actually reaches the model.

Skipped when node or the dsh build is missing. No browser, no live provider.
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

from freeharness import vault_cmd
from freeharness.client import rpc
from freeharness.transcript import Transcript

HERE = Path(__file__).resolve().parent

try:
    rpc.find_node_dir()
    HAVE = rpc.DSH_BIN.exists()
except Exception:  # noqa: BLE001
    HAVE = False
pytestmark = pytest.mark.skipif(not HAVE, reason="node / dsh harness not installed")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    port = free_port()
    log = tmp_path / "log"
    log.mkdir()
    p = subprocess.Popen([sys.executable, str(HERE / "fake_openai.py")], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         env=dict(os.environ, FAKE_OPENAI_PORT=str(port), FAKE_OPENAI_LOG=str(log)))
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=1).read()
            break
        except OSError:
            time.sleep(0.1)
    patch = tmp_path / "patch.yml"
    patch.write_text(rpc.PATCH.read_text().replace("127.0.0.1:8766", f"127.0.0.1:{port}"))
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("GLOBAL-RULE-BETA\n")
    proj = tmp_path / "proj"
    (proj / ".dsh" / "skills" / "demo-skill").mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-RULE-ALPHA\n")
    (proj / ".dsh" / "skills" / "demo-skill" / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: SKILL-DESC-GAMMA do the demo thing\n---\nSteps.\n")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Pineapple\nthe vault secret is DELTA\n")
    ov = tmp_path / "vault.yml"
    ov.write_text(vault_cmd._row(vault))
    monkeypatch.setattr(rpc.Runtime, "overlays", staticmethod(lambda: [ov]))
    yield {"proj": proj, "home": home, "patch": patch, "log": log / "freeharness-fake-requests.jsonl"}
    p.terminate()
    p.wait(5)


def bodies(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_context_tools_resume_and_transcript(env, tmp_path):
    sess = f"ctx-{uuid.uuid4().hex[:8]}"
    rt = rpc.Runtime(env["proj"], home=env["home"], patch=env["patch"])
    try:
        rt.start()
        r = rt.run_turn(sess, "FIRST-USER-MESSAGE hello", timeout=120)
        assert r.ok, r.reason
    finally:
        rt.close()
    first = bodies(env["log"])[0]
    text = json.dumps(first)
    assert "PROJECT-RULE-ALPHA" in text, "project AGENTS.md missing from the request"
    assert "GLOBAL-RULE-BETA" in text, "global dsh-home AGENTS.md missing from the request"
    assert "SKILL-DESC-GAMMA" in text and "demo-skill" in text, "skill not advertised"
    names = [t["function"]["name"] for t in first.get("tools", [])]
    assert "mcp__vault__vault_search" in names and "mcp__vault__vault_read" in names, names
    assert "skill" in names, names

    # resume in a brand-new runtime: history is replayed to the model
    n = len(bodies(env["log"]))
    rt2 = rpc.Runtime(env["proj"], home=env["home"], patch=env["patch"])
    try:
        rt2.start()
        rt2.resume(sess)
        r2 = rt2.run_turn(sess, "SECOND-USER-MESSAGE", timeout=120)
        assert r2.ok, r2.reason
    finally:
        rt2.close()
    t2 = json.dumps(bodies(env["log"])[n])
    assert "FIRST-USER-MESSAGE" in t2 and "SECOND-USER-MESSAGE" in t2
    assert t2.index("FIRST-USER-MESSAGE") < t2.index("SECOND-USER-MESSAGE")

    # transcript across resume: one header, both turns, in order
    tdir = tmp_path / "tr"
    for res, msg in ((r, "FIRST-USER-MESSAGE"), (r2, "SECOND-USER-MESSAGE")):
        tr = Transcript(sess, str(env["proj"]), directory=tdir)
        tr.user(msg)
        for ev in res.events:
            tr.event(ev)
        tr.close()
    md = (tdir / f"{sess}.md").read_text()
    assert md.count(f"# Session {sess}") == 1
    assert md.index("FIRST-USER-MESSAGE") < md.index("SECOND-USER-MESSAGE")
    assert md.count("_turn ended: completed_") == 2
