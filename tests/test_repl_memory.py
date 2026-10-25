"""The terminal client's session/memory paths with a fake runtime (no dsh, no network)."""
import argparse
import io
from pathlib import Path

import pytest
from rich.console import Console

from freeharness import memory, memory_cmd, paths, sessions
from freeharness.client import cli
from freeharness.client.rpc import TurnResult


class FakeRuntime:
    def __init__(self, cwd, model="auto", **kw):
        self.cwd, self.model = str(Path(cwd).resolve()), model
        self.sent, self.resumed, self.started = [], [], False

    def start(self):
        self.started = True

    def close(self):
        pass

    def resume(self, sid):
        self.resumed.append(sid)

    def restart(self, model=None):
        pass

    def run_turn(self, sid, text, on_event=None, on_tick=None, timeout=0, max_steps=0):
        self.sent.append((sid, text))
        return TurnResult(True, {"kind": "completed"}, "ok", 1, [], 0, 0.1, [])


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "fh"))
    monkeypatch.setattr(cli, "Runtime", FakeRuntime)
    monkeypatch.setattr(cli, "DSH_HOME", paths.dsh_home())      # import-time constant otherwise points at the real home
    ws = tmp_path / "ws"
    ws.mkdir()
    out = io.StringIO()

    def make(**over):
        ns = dict(cwd=str(ws), model="auto", providers="gemini", sticky="auto", no_shim=True, resume=None,
                  prompt=None, cont=False, turn_timeout=900.0, max_steps=60)
        ns.update(over)
        return cli.App(argparse.Namespace(**ns), Console(file=out, width=120, force_terminal=False))
    make.ws, make.out = ws, out
    return make


def fake_session_dir(*ids):
    for i in ids:
        (paths.dsh_home() / "sessions" / i).mkdir(parents=True)


def test_continue_resumes_latest_session_for_this_folder_only(app_factory):
    fake_session_dir("term-1", "term-2", "term-3")
    sessions.record_turn("term-1", app_factory.ws, "first")
    sessions.record_turn("term-2", app_factory.ws, "second")
    sessions.record_turn("term-3", app_factory.ws.parent, "other folder")
    a = app_factory(cont=True)
    assert a.sid == "term-2" and a.resumed
    a.start()
    assert a.rt.resumed == ["term-2"]
    assert sessions.active_for(app_factory.ws) == "term-2"


def test_continue_without_history_starts_fresh_and_ignores_sessions_dsh_lost(app_factory):
    a = app_factory(cont=True)
    assert a.sid.startswith("term-") and not a.resumed
    sessions.record_turn("term-gone", app_factory.ws, "dsh no longer has it")      # no dsh session dir
    assert not app_factory(cont=True).resumed


def test_turn_records_title_turns_and_scratchpad_is_reinjected_once_after_resume(app_factory):
    fake_session_dir("term-9")
    sessions.record_turn("term-9", app_factory.ws, "port the parser", "gemini")
    sessions.scratch_set("term-9", "next_steps", "write the tests")
    a = app_factory(resume="term-9")
    a.start()
    assert "next_steps: write the tests" in app_factory.out.getvalue(), "shown at session start"
    assert a.turn("continue please")
    assert a.turn("and now this")
    first, second = a.rt.sent[0][1], a.rt.sent[1][1]
    assert "write the tests" in first and first.endswith("continue please")
    assert second == "and now this", "scratchpad is injected once, not every turn"
    e = sessions.get("term-9")
    assert e["title"] == "port the parser" and e["turns"] == 3
    a.stop()


def test_fresh_session_turn_creates_index_entry_with_title(app_factory):
    a = app_factory()
    a.start()
    a.turn("Fix the flaky test in tests/test_x.py")
    e = sessions.get(a.sid)
    assert e["title"] == "Fix the flaky test in tests/test_x.py" and e["turns"] == 1
    assert e["cwd"] == str(app_factory.ws.resolve())
    a.stop()


def test_slash_commands_sessions_remember_scratch(app_factory):
    memory_cmd.enable(app_factory.ws)
    a = app_factory()
    a.start()
    a.turn("hello there")
    a.command("/sessions")
    assert "hello there" in app_factory.out.getvalue() and "turns" in app_factory.out.getvalue()
    a.command("/remember Commit subjects are one line, no trailers")
    s = memory.MemoryStore()
    assert [f["text"] for f in s.list()] == ["Commit subjects are one line, no trailers"]
    assert s.list()[0]["source_session"] == a.sid
    s.close()
    assert "no trailers" in (paths.dsh_home() / "AGENTS.md").read_text(), "pinned block refreshed"
    a.command("/remember password is hunter2hunter2")
    assert "looks like a secret" in app_factory.out.getvalue()
    sessions.scratch_set(a.sid, "current_task", "write docs")
    a.command("/scratch")
    assert "current_task: write docs" in app_factory.out.getvalue()
    a.command("/remember")
    assert "usage" in app_factory.out.getvalue()
    a.stop()


def test_new_clears_pending_scratch_preamble_and_resume_loads_it(app_factory):
    fake_session_dir("term-5")
    sessions.scratch_set("term-5", "current_task", "T")
    a = app_factory()
    a.start()
    a.command("/resume term-5")
    assert "current_task: T" in a.preamble and sessions.active_for(app_factory.ws) == "term-5"
    a.command("/new")
    assert a.preamble == "" and sessions.active_for(app_factory.ws) == a.sid
