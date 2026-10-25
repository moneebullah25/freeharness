import json
import stat
from pathlib import Path

import pytest

from freeharness import transcript
from freeharness.backends import shim

FIXTURE = Path(__file__).parent / "fixtures" / "read_turn.json"


def test_a_real_recorded_turn_becomes_a_readable_markdown_file(tmp_path):
    tr = transcript.Transcript("term-test", "/work/my project", tmp_path)
    tr.user("read hello.txt")
    for ev in json.loads(FIXTURE.read_text()):
        tr.event(ev)
    tr.close()
    text = tr.path.read_text()
    assert text.startswith("# Session term-test") and "/work/my project" in text
    assert "## You" in text and "read hello.txt" in text
    assert "**tool call:** `read  hello.txt`" in text and '"file_path": "hello.txt"' in text
    assert "the secret word is pineapple" in text, "tool results are part of the record"
    assert "### Assistant" in text and "The file says" in text
    assert "_turn ended: completed_" in text
    assert stat.S_IMODE(tr.path.stat().st_mode) == 0o600, "transcripts hold file contents: owner only"


def test_appends_across_turns_without_a_second_header_and_survives_reopening(tmp_path):
    a = transcript.Transcript("s1", "/w", tmp_path)
    a.user("first")
    a.close()
    b = transcript.Transcript("s1", "/w", tmp_path)          # e.g. /resume in a new process
    b.user("second")
    b.close()
    text = (tmp_path / "s1.md").read_text()
    assert text.count("# Session s1") == 1 and "first" in text and "second" in text


def test_huge_tool_results_are_cut_and_say_so(tmp_path):
    tr = transcript.Transcript("big", "/w", tmp_path)
    tr.event({"type": "tool/result", "data": {"message": {"isError": True, "content": [
        {"type": "text", "text": "x" * (transcript.MAX_RESULT + 500)}]}}})
    tr.close()
    text = tr.path.read_text()
    assert "tool result (error)" in text and "500 more characters not saved" in text
    assert len(text) < transcript.MAX_RESULT + 1000


def test_listing_and_printing_by_unique_prefix(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path))
    transcript.Transcript("term-20261025-1", "/w").user("hello there")
    transcript.Transcript("term-20261026-2", "/w").user("other")
    assert transcript.main([]) == 0 and "term-20261025-1" in capsys.readouterr().out
    assert transcript.main(["term-20261025"]) == 0 and "hello there" in capsys.readouterr().out
    assert transcript.main(["term-2026"]) == 1, "an ambiguous prefix must not pick one silently"
    assert transcript.main(["nope"]) == 1


def test_sticky_flag_values():
    keys = ["gemini", "qwen", "chatgpt"]
    assert shim._sticky_keys("auto", keys) == ["gemini"], "only providers verified by the eval"
    assert shim._sticky_keys("", keys) == ["gemini"]
    assert shim._sticky_keys("none", keys) == [] and shim._sticky_keys("off", keys) == []
    assert shim._sticky_keys("all", keys) == keys
    assert shim._sticky_keys("qwen, gemini", keys) == ["qwen", "gemini"]
    with pytest.raises(SystemExit):
        shim._sticky_keys("grok", keys)
