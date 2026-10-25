#!/usr/bin/env python3
"""Advisory judge, human review and final_assessment, offline with a stub judge (no model, no shim)."""
import json
import tempfile
from pathlib import Path

from freeharness.client.rpc import TurnResult
from freeharness.eval import judge, review
from freeharness.eval import run as ev
from freeharness.eval.tasks import BY_ID


def _run(task_id, solved):
    t = BY_ID[task_id]
    ws = ev.materialize(t)
    if solved:
        t.solve(ws)
    events = [{"type": "tool/call", "data": {"callId": "c1", "name": "bash"}},
              {"type": "tool/result", "data": {"message": {"toolCallId": "c1", "isError": False,
                                                           "content": [{"type": "text", "text": "refactor ok"}]}}}]
    res = TurnResult(True, {"kind": "completed"}, "done", 2, [("bash", {"command": "python3 check.py"})], 0, 1.0, events)
    ok, detail = t.check(ws, res)
    return t, ws, res, ok, detail


def test_packet_has_changed_files_tool_calls_and_output():
    t, ws, res, ok, detail = _run("refactor-3files", True)
    p = judge.build_packet(t, ws, res, ok, detail)
    changed = {f["path"]: f["change"] for f in p["files_changed"]}
    assert changed == {"pkg/rows.py": "modified", "pkg/report.py": "modified", "pkg/cli.py": "modified"}
    assert p["tool_calls"][0]["tool"] == "bash" and p["tool_calls"][0]["output"] == "refactor ok"
    assert p["deterministic"]["ok"] is True
    assert "parse_record" in judge.prompt_for(p)


def test_parse_is_structured_and_tolerant():
    v = judge.parse('```json\n{"score": 9, "verdict": "good", "reasoning": "clean", "concerns": ["x"]}\n```')
    assert v["score"] == 9 and v["verdict"] == "good" and v["reasoning"] == "clean" and v["advisory"]
    assert judge.parse('{"score": 99, "reasoning": "r"}')["score"] == 10
    assert judge.parse("no json here")["verdict"] == "unparseable"
    assert judge.parse('{"score": "high"}')["score"] is None


def test_judge_records_reasoning_and_survives_a_broken_ask():
    t, ws, res, ok, detail = _run("refactor-3files", True)
    p = judge.build_packet(t, ws, res, ok, detail)
    v = judge.judge(p, lambda prompt: '{"score": 3, "reasoning": "left a TODO", "verdict": "poor"}')
    assert v["score"] == 3 and v["reasoning"] == "left a TODO" and v["saw_deterministic"]["ok"] is True

    def boom(prompt):
        raise OSError("shim down")
    v = judge.judge(p, boom)
    assert v["verdict"] == "unavailable" and v["score"] is None


def test_judge_cannot_override_a_failed_deterministic_check():
    t, ws, res, ok, detail = _run("refactor-3files", False)          # untouched workspace
    assert not ok
    v = judge.judge(judge.build_packet(t, ws, res, ok, detail), lambda p: '{"score": 10, "verdict": "good", "reasoning": "perfect"}')
    a = review.final_assessment({"ok": ok, "detail": detail, "tool_calls": 1}, v, {"decision": "approved"})
    assert a["status"] == "failed" and not a["success"]


def test_final_assessment_rules():
    ok = {"ok": True, "detail": "", "tool_calls": 2}
    assert review.final_assessment(None, {"score": 10}, {"decision": "approved"})["status"] == "unverified"
    assert review.final_assessment({"ok": True, "tool_calls": 0})["status"] == "unverified"
    assert review.final_assessment(ok)["status"] == "passed"
    assert review.final_assessment(ok, {"score": 9, "verdict": "good"})["status"] == "passed"
    low = review.final_assessment(ok, {"score": 2, "verdict": "poor", "reasoning": "sloppy"})
    assert low["status"] == "needs_review" and low["success"]
    assert review.final_assessment(ok, {"score": 2}, {"decision": "approved"})["status"] == "verified"
    assert review.final_assessment(ok, {"score": 9}, {"decision": "rejected", "note": "n"})["status"] == "rejected_by_human"
    assert review.final_assessment(ok, None, {"decision": "changes_requested"})["status"] == "needs_changes"


def test_review_cli_records_and_shows():
    f = Path(tempfile.mkdtemp(prefix="eval-review-")) / "reviews.json"
    assert review.main(["skill-use", "approve", "--note", "fine", "--file", str(f)]) == 0
    assert review.main(["skill-use", "changes", "--note", "tidy", "--run", "r2", "--file", str(f)]) == 0
    rows = json.loads(f.read_text())
    assert [r["decision"] for r in rows] == ["approved", "changes_requested"] and rows[1]["run"] == "r2"
    assert review.latest("skill-use", path=f)["decision"] == "changes_requested"
    assert review.latest("skill-use", "none", path=f) is None
    assert review.main(["skill-use", "--show", "--file", str(f)]) == 0
    try:
        review.record("x", "maybe", path=f)
        raise AssertionError("bad decision accepted")
    except ValueError:
        pass


def test_cli_dispatches_review():
    from freeharness import cli
    assert "review" in cli.SUBCOMMANDS
