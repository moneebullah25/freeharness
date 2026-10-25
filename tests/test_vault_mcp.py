import json
import subprocess
import sys

import pytest

from freeharness.mcp import vault as v


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / "index.md").write_text("# Index\nStart at [[free-agent]] and [[goals]].\n")
    (tmp_path / "wiki" / "free-agent.md").write_text(
        "---\ntitle: x\n---\n# free-agent\nQwen passed the eval 8/8 and Gemini 8/8. See [[goals]] and [[linux-workstation|the box]].\n")
    (tmp_path / "wiki" / "goals.md").write_text("# Goals\nShip Interview Copilot. Grow to Solution Architect.\n")
    (tmp_path / "raw" / "2026-10-25-capture.md").write_text("# capture\nqwen eval raw log: 7/8 before the fix.\n")
    (tmp_path / ".obsidian" / "workspace.md").write_text("# hidden\nqwen\n")
    return v.Vault(tmp_path)


def test_search_ranks_wiki_above_raw_and_shows_links_and_snippets(vault):
    out = vault.search("qwen eval")
    lines = [ln for ln in out.splitlines() if ln[:2] in ("1.", "2.")]
    assert lines[0].startswith("1. wiki/free-agent.md"), out
    assert "raw/2026-10-25-capture.md" in out
    assert "> Qwen passed the eval 8/8" in out
    assert "[[goals]]" in out and "[[linux-workstation]]" in out
    assert ".obsidian" not in out, "editor config must not be indexed"


def test_title_match_beats_body_mentions(vault):
    assert vault.search("goals").splitlines()[0].startswith("1. wiki/goals.md")


def test_no_match_and_empty_query_are_explained_not_errors(vault):
    assert "No notes match" in vault.search("zzzzqqq")
    assert vault.search("   ") == "No query terms."


def test_read_by_path_by_link_name_and_with_paging(vault, tmp_path):
    assert vault.read("wiki/goals.md").startswith("# wiki/goals.md")
    assert "Ship Interview Copilot" in vault.read("[[goals]]")
    (tmp_path / "wiki" / "long.md").write_text("# long\n" + "x" * 5000)
    first = vault.read("long", max_chars=1000)
    assert "more chars" in first and "offset=" in first
    assert vault.read("nonexistent").startswith("No note")


def test_cannot_read_outside_the_vault(vault, tmp_path):
    secret = tmp_path.parent / "secret.md"
    secret.write_text("# secret\ntop secret")
    (tmp_path / "wiki" / "leak.md").symlink_to(secret)
    for ref in ("../secret.md", "../../etc/passwd", "/etc/passwd", "wiki/leak.md", "leak"):
        assert "top secret" not in vault.read(ref) and "root:" not in vault.read(ref), ref
    assert "top secret" not in vault.search("secret")


def test_index_notices_edits(vault, tmp_path):
    assert "No notes match" in vault.search("pineapple")
    (tmp_path / "wiki" / "new.md").write_text("# New\npineapple facts\n")
    assert "wiki/new.md" in vault.search("pineapple")


def test_protocol_handshake_list_and_call_over_stdio(vault, tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "freeharness.mcp.vault", "--vault", str(tmp_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def rpc(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline()) if "id" in msg else None
    try:
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})        # no reply expected
        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        assert [t["name"] for t in tools] == ["vault_search", "vault_read"]
        res = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "vault_search", "arguments": {"query": "gemini eval"}}})["result"]
        assert "wiki/free-agent.md" in res["content"][0]["text"] and not res.get("isError")
        bad = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "rm_rf", "arguments": {}}})
        assert bad["error"]["code"] == -32602
        badargs = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "vault_search", "arguments": {"query": "x", "limit": "many"}}})
        assert badargs["result"]["isError"] is True
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


# ------------------------------------------------------------------ freeharness vault <path>

def test_connect_writes_overlay_and_instructions_and_disconnect_restores(tmp_path, monkeypatch):
    from freeharness import paths, vault_cmd
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "home ünï"))
    notes = tmp_path / "my notes 日本"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n")
    md = paths.dsh_home() / "AGENTS.md"
    md.parent.mkdir(parents=True)
    md.write_text("# My own rules\nBe concise.\n")

    vault_cmd.connect(notes)
    vault_cmd.connect(notes)                                  # idempotent: no duplicated block
    row = (vault_cmd.overlay_dir() / "vault.yml").read_text()
    assert "serverName: vault" in row and "'@deepseek-ai/dsh-mcp-client'" in row
    assert "id: mcp-resources" in row and "disabled: true" in row
    assert "- insert:" in row, "a plain row only patches an existing entry; `insert:` adds one"
    assert vault_cmd.connected_vault() == str(notes.resolve())
    text = md.read_text()
    assert text.count(vault_cmd.BEGIN) == 1 and "Be concise." in text and "vault_search" in text

    vault_cmd.disconnect()
    assert not (vault_cmd.overlay_dir() / "vault.yml").exists() and vault_cmd.connected_vault() is None
    assert md.read_text().strip() == "# My own rules\nBe concise.", "the user's own text must survive"


def test_connect_refuses_a_folder_without_notes(tmp_path, monkeypatch):
    from freeharness import vault_cmd
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "h"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit):
        vault_cmd.connect(tmp_path / "empty")


def test_runtime_loads_every_overlay_file_in_order(tmp_path, monkeypatch):
    from freeharness.client.rpc import Runtime
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path))
    d = tmp_path / "config" / "overlay.d"
    d.mkdir(parents=True)
    (d / "b-extra.yml").write_text("[]\n")
    (d / "a-vault.yml").write_text("[]\n")
    (d / "notes.txt").write_text("ignored")
    assert [p.name for p in Runtime.overlays()] == ["a-vault.yml", "b-extra.yml"]
