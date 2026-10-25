"""Offline unit tests: session index, scratchpad, memory store, memory MCP server, memory CLI/wiring."""
import json
import os
import stat
import subprocess
import sys
import time

import pytest

from freeharness import memory, memory_cmd, paths, sessions
from freeharness.mcp import memory as mcp_memory


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FREEHARNESS_SESSION", raising=False)
    return tmp_path


# ------------------------------------------------------------------ sessions

def test_index_records_title_once_counts_turns_and_is_private(home):
    cwd = home / "proj"
    cwd.mkdir()
    sessions.record_turn("term-1", cwd, "  fix the\nfailing   test please  ", "gemini")
    e = sessions.record_turn("term-1", cwd, "something else entirely", "gemini")
    assert e["title"] == "fix the failing test please" and e["turns"] == 2
    f = sessions.index_dir() / "index.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not [p for p in f.parent.iterdir() if p.name != "index.json"], "no temp files left behind"
    long = sessions.make_title("x" * 300)
    assert len(long) == sessions.MAX_TITLE and long.endswith("…")


def test_latest_for_cwd_picks_most_recent_with_turns_and_existing(home):
    a, b = home / "a", home / "b"
    a.mkdir()
    b.mkdir()
    sessions.record_turn("old", a, "old one")
    time.sleep(0.01)
    sessions.record_turn("new", a, "new one")
    sessions.record_turn("other", b, "elsewhere")
    sessions.touch("untouched", a)                         # registered but zero turns: never continued
    assert sessions.latest_for_cwd(a) == "new"
    assert sessions.latest_for_cwd(a, existing={"old"}) == "old"
    assert sessions.latest_for_cwd(b) == "other"
    assert sessions.latest_for_cwd(home) is None


def test_corrupt_index_is_survivable_and_age_format(home):
    sessions.index_dir().mkdir(parents=True)
    (sessions.index_dir() / "index.json").write_text("{not json")
    assert sessions.list_sessions() == []
    sessions.record_turn("s", home, "hi")
    assert sessions.get("s")["turns"] == 1
    now = 1_000_000.0
    assert sessions.age(now - 5, now) == "just now" and sessions.age(now - 7200, now) == "2h ago"
    assert sessions.age(now - 3 * 86400, now) == "3d ago"


def test_scratchpad_roundtrip_limits_and_bad_ids(home):
    sessions.scratch_set("term-1", "current_task", "port the parser")
    sessions.scratch_set("term-1", "next_steps", "1. tests")
    assert sessions.scratch_get("term-1", "current_task") == "port the parser"
    assert "- next_steps: 1. tests" in sessions.render_scratch("term-1")
    sessions.scratch_set("term-1", "next_steps", "")
    assert "next_steps" not in sessions.scratch_all("term-1")
    with pytest.raises(ValueError):
        sessions.scratch_set("term-1", "bad/key", "x")
    with pytest.raises(ValueError):
        sessions.scratch_all("../escape")
    assert len(sessions.scratch_set("term-1", "big", "y" * 9999)["big"]) == sessions.MAX_VALUE
    assert sessions.render_scratch("nobody") == ""


# ------------------------------------------------------------------ memory store

@pytest.fixture(params=["fts", "bm25"])
def store(request, home):
    s = memory.MemoryStore(home / f"{request.param}.sqlite3", fts=None if request.param == "fts" else False)
    if request.param == "fts" and not s.fts:
        pytest.skip("this sqlite has no FTS5")
    yield s
    s.close()


def test_save_recall_scope_and_project_inheritance(store, home):
    proj = home / "proj"
    (proj / "sub").mkdir(parents=True)
    store.save("Commit subjects are one line with no trailers", "feedback", "global", session="s1")
    store.save("The proj repo uses pnpm not npm", "project", "project", proj)
    store.save("Unrelated gardening note about tomatoes", "reference", "global")
    assert [f["text"] for f in store.search("commit subjects", proj)][0].startswith("Commit subjects")
    assert store.search("pnpm", proj) and store.search("pnpm", proj / "sub"), "sub-folders inherit project facts"
    assert not store.search("pnpm", home), "other folders must not see a project fact"
    assert store.search("tomatoes", None, type="reference") and not store.search("tomatoes", None, type="user")
    assert store.search("???") == []
    assert store.get(1)["source_session"] == "s1" and store.get(1)["updated"] >= store.get(1)["created"]


def test_dedupe_updates_instead_of_duplicating(store):
    a = store.save("Always run the tests before committing", "feedback")
    b = store.save("always RUN the tests, before committing!", "feedback")
    c = store.save("Always run the tests before committing to main", "feedback", session="s2")
    assert (a["action"], b["action"], c["action"]) == ("added", "updated", "updated")
    assert len(store.list()) == 1 and store.list()[0]["text"].endswith("to main")
    assert store.list()[0]["source_session"] == "s2"
    assert store.save("Always run the linter", "feedback")["action"] == "added"
    # different type, same sentence words: not a near-duplicate, but identical text still is
    assert store.save("Always run the linter", "user")["action"] == "updated"


def test_forget_by_id_and_by_unique_query(store):
    a = store.save("Prefer tabs over spaces in Makefiles", "user")
    store.save("Deploys happen on Fridays only", "project", "global")
    store.save("Deploys also need a changelog entry", "project", "global")
    with pytest.raises(ValueError):
        store.forget("deploys")                            # ambiguous: refuse, list ids
    assert [f["id"] for f in store.forget("tabs")] == [a["id"]]
    assert store.forget(999) == [] and store.forget("nothing like this") == []
    assert not store.search("tabs")


def test_validation_and_pinning_defaults(store, home):
    with pytest.raises(ValueError):
        store.save("  ")
    with pytest.raises(ValueError):
        store.save("x y z", type="bogus")
    with pytest.raises(ValueError):
        store.save("x y z", scope="project")               # needs a cwd
    assert store.save("conv one here", "feedback")["pinned"] == 1
    assert store.save("a project fact here", "project")["pinned"] == 0
    assert [f["text"] for f in store.list(pinned_only=True)] == ["conv one here"]
    assert store.wipe() == 2 and store.list() == []


def test_database_file_is_private(home):
    s = memory.MemoryStore()
    s.save("hello there world", "user")
    s.close()
    assert stat.S_IMODE(memory.db_path().stat().st_mode) == 0o600


# ------------------------------------------------------------------ MCP server

def _ctx(home, cwd=None, session="term-t"):
    return mcp_memory.Context(memory.MemoryStore(home / "mcp.sqlite3"), str(cwd or home), session)


def test_tool_calls_in_process(home):
    ctx = _ctx(home)
    out = mcp_memory.call_tool(ctx, "memory_save", {"text": "Commit subjects: one line, no trailers"})
    assert out.startswith("added: [1] (feedback, global pinned)")
    assert mcp_memory.call_tool(ctx, "memory_save", {"text": "commit subjects one line no trailers"}).startswith("updated")
    assert "commit subjects" in mcp_memory.call_tool(ctx, "memory_recall", {"query": "commit"})
    assert mcp_memory.call_tool(ctx, "memory_recall", {"query": "zzz"}) == "No saved memory matches."
    mcp_memory.call_tool(ctx, "scratch_set", {"key": "current_task", "value": "write docs"})
    assert mcp_memory.call_tool(ctx, "scratch_get", {"key": "current_task"}) == "write docs"
    assert "current_task: write docs" in mcp_memory.call_tool(ctx, "scratch_get", {})
    assert mcp_memory.call_tool(ctx, "memory_forget", {"id": 1}).startswith("forgot:")
    with pytest.raises(ValueError):
        mcp_memory.call_tool(ctx, "memory_forget", {})


def test_session_comes_from_active_pointer(home):
    ctx = mcp_memory.Context(memory.MemoryStore(home / "m.sqlite3"), str(home), None)
    assert ctx.session == "default"
    sessions.set_active(home, "term-active")
    assert ctx.session == "term-active"


def test_protocol_over_stdio_and_five_tools(home):
    env = dict(os.environ, FREEHARNESS_HOME=str(home / "home"), FREEHARNESS_CWD=str(home),
               FREEHARNESS_SESSION="term-stdio", PYTHONPATH=os.pathsep.join(sys.path))
    proc = subprocess.Popen([sys.executable, "-m", "freeharness.mcp.memory"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=env)

    def rpc(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline()) if "id" in msg else None
    try:
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        assert init["result"]["serverInfo"]["name"] == "freeharness-memory"
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        assert [t["name"] for t in tools] == ["memory_save", "memory_recall", "memory_forget", "scratch_get", "scratch_set"]
        save = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "memory_save", "arguments": {"text": "Use rebase, never merge commits", "scope": "project"}}})
        assert save["result"]["content"][0]["text"].startswith("added:")
        rec = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                   "params": {"name": "memory_recall", "arguments": {"query": "rebase"}}})["result"]
        assert "never merge commits" in rec["content"][0]["text"] and "project" in rec["content"][0]["text"]
        assert rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "rm_rf", "arguments": {}}})["error"]["code"] == -32602
        bad = rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                   "params": {"name": "memory_recall", "arguments": {"query": "x", "limit": "many"}}})
        assert bad["result"]["isError"] is True
        bad = rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "memory_save", "arguments": {"text": "x y z", "type": "bogus"}}})
        assert bad["result"]["isError"] is True
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


# ------------------------------------------------------------------ wiring, rendering, extraction, CLI

def test_enable_disable_overlay_and_block_preserve_user_text(home):
    md = paths.dsh_home() / "AGENTS.md"
    md.parent.mkdir(parents=True)
    md.write_text("# My own rules\nBe concise.\n")
    memory_cmd.enable(home)
    memory_cmd.enable(home)                                # idempotent
    row = (memory_cmd.overlay_dir() / "memory.yml").read_text()
    assert "serverName: memory" in row and "freeharness.mcp.memory" in row and "- insert:" in row
    text = md.read_text()
    assert text.count(memory_cmd.BEGIN) == 1 and "Be concise." in text and "mcp__memory__memory_recall" in text
    memory_cmd.disable()
    assert not (memory_cmd.overlay_dir() / "memory.yml").exists()
    assert md.read_text().strip() == "# My own rules\nBe concise."


def test_block_renders_pinned_for_cwd_only_and_is_size_capped(home):
    proj, other = home / "p", home / "q"
    proj.mkdir()
    other.mkdir()
    s = memory.MemoryStore()
    s.save("Commit subjects one line no trailers", "feedback")
    s.save("Proj uses pnpm", "feedback", "project", proj)
    s.save("not pinned reference fact", "reference", pinned=False)
    block = memory_cmd.render_block(proj, store=s)
    assert "one line no trailers" in block and "Proj uses pnpm" in block and "not pinned" not in block
    assert "Proj uses pnpm" not in memory_cmd.render_block(other, store=s)
    for i in range(60):
        s.save(f"convention number {i} " + "word " * 20 + f"u{i}", "feedback")
    big = memory_cmd.render_block(proj, cap=600, store=s)
    assert "more pinned; use memory_recall" in big
    assert len(big) < len(memory_cmd.INSTRUCTIONS) + 600 + 300


def test_refresh_is_noop_until_enabled_and_tracks_changes(home):
    assert memory_cmd.refresh(home) is False and not (paths.dsh_home() / "AGENTS.md").exists()
    memory_cmd.enable(home)
    s = memory.MemoryStore()
    s.save("Never force-push to main", "feedback")
    s.close()
    memory_cmd.refresh(home)
    assert "Never force-push to main" in (paths.dsh_home() / "AGENTS.md").read_text()


TRANSCRIPT = """# Session term-x

workspace: `/tmp/x`

## You  (10:00:00)

Please fix the parser. Always run the tests before you commit.
Can you never mention it again?

### Assistant

Done. I will always be careful.

**tool call:** `bash  $ ls`

## You  (10:05:00)

From now on, commit subjects are one line with no trailers.
Remember that the deploy box is called atlas.

_turn ended: completed_
"""


def test_extraction_is_conservative_about_what_it_proposes():
    got = memory_cmd.propose(TRANSCRIPT)
    assert "Always run the tests before you commit." in got
    assert "From now on, commit subjects are one line with no trailers." in got
    assert "Remember that the deploy box is called atlas." in got
    assert not any("careful" in g for g in got), "assistant text must never be proposed"
    assert not any(g.endswith("?") for g in got), "questions are not statements"
    assert memory_cmd.propose("## You  (1)\n\nplease fix the bug\n") == []


def test_cli_extract_only_saves_with_yes_and_wipe_needs_confirmation(home, capsys):
    from freeharness import transcript
    d = transcript.transcript_dir()
    d.mkdir(parents=True)
    (d / "term-x.md").write_text(TRANSCRIPT)
    assert memory_cmd.main(["extract", "term-x"]) == 0
    assert "proposed: Always run the tests" in capsys.readouterr().out
    assert memory_cmd.main(["list", "--all"]) == 0 and "no saved facts" in capsys.readouterr().out
    assert memory_cmd.main(["extract", "term-x", "--yes"]) == 0
    assert "added:" in capsys.readouterr().out
    memory_cmd.main(["list", "--all"])
    assert capsys.readouterr().out.count("[") >= 3
    assert memory_cmd.main(["add", "I prefer", "tabs", "--type", "user"]) == 0
    assert memory_cmd.main(["search", "tabs"]) == 0 and "I prefer tabs" in capsys.readouterr().out
    assert memory_cmd.main(["wipe"]) == 1, "no tty and no --yes: must refuse"
    capsys.readouterr()
    assert memory_cmd.main(["export"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 4
    out_file = home / "dump.json"
    assert memory_cmd.main(["export", str(out_file)]) == 1 and not out_file.exists()
    assert memory_cmd.main(["export", str(out_file), "--yes"]) == 0
    assert len(json.loads(out_file.read_text())) == 4 and stat.S_IMODE(out_file.stat().st_mode) == 0o600
    assert memory_cmd.main(["wipe", "--yes"]) == 0
    assert memory_cmd.main(["status"]) == 0 and "0 fact(s)" in capsys.readouterr().out.split("wiped")[-1]


def test_cli_dispatch_knows_new_subcommands():
    from freeharness import cli
    assert "memory" in cli.SUBCOMMANDS and "sessions" in cli.SUBCOMMANDS


@pytest.mark.parametrize("text", [
    "my password is hunter2hunter2",
    "use api key sk-abcdefghijklmnop1234 for the build",
    "token: ghp_abcdefghijklmnopqrstuvwx",
    "Authorization: Bearer abcdefgh12345678",
    "aws AKIAABCDEFGHIJKLMNOP",
    "-----BEGIN RSA PRIVATE KEY----- abc",
    "the digest is 0123456789abcdef0123456789abcdef01234567",
    "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc",
])
def test_secrets_are_refused(store, text):
    with pytest.raises(ValueError, match="secret"):
        store.save(text, "user")
    assert store.list() == []


def test_ordinary_conventions_are_not_mistaken_for_secrets(store):
    for t in ("Commit subjects are one line with no trailers", "Never print the token value in logs",
              "Prefer the tokenizer in src/tok.py", "Deploy box is called atlas, ssh as the deploy user"):
        assert store.save(t, "feedback")["action"] == "added", t


def test_mcp_save_reports_secret_refusal_as_tool_error(home):
    ctx = _ctx(home)
    with pytest.raises(ValueError):
        mcp_memory.call_tool(ctx, "memory_save", {"text": "password = correcthorse99"})
    r = mcp_memory.handle(ctx, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "memory_save", "arguments": {"text": "password = correcthorse99"}}})
    assert r["result"]["isError"] is True and "secret" in r["result"]["content"][0]["text"]
