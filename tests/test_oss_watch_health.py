"""oss watch (oss_watch.py) and the run-health section (oss_health.py): synthetic run dirs, no provider, no network."""
import json

from freeharness import oss_health as oh
from freeharness import oss_watch as ow


def _tool(n, status="ok", tool="bash", result="", t="2026-11-01T10:32:45"):
    return {"t": t, "kind": "tool", "n": n, "step": n, "tool": tool, "args": "x=1", "status": status, "ms": 5, "result": result}


def _stage(text, kind_=None, t="2026-11-01T10:32:22"):
    e = {"t": t, "kind": "stage", "text": text}
    if kind_:
        e["kind_"] = kind_
    return e


def _write(p, rows):
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _run(tmp_path):
    d = tmp_path / "20261101-000000-abcd"
    (d / "stages").mkdir(parents=True)
    _write(d / "supervisor.jsonl", [
        _stage("hunt: stage turn on qwen (step budget 20)", "stage"),
        _tool(1),
        _tool(2, "error", "edit", 'Error: cannot modify "/w/a.py": file has not been read — read the file'),
        _tool(3, "error", "read", 'Error: cannot read "/w/b.py": not found'),
        _tool(4, "exit", "bash", "ModuleNotFoundError: No module named 'x' [exit code: 1]"),
        _tool(5, "error", "bash", "Error: sandbox mode \"workspace-write\" is requested but no sandbox backend is usable"),
        _tool(6, "error", "mcp__gh__gh_push", "Error: REFUSED: nothing to push"),
        _stage("hunt: every provider is cooling down; waiting 122s and retrying the same turn (1/3)"),
        _stage("hunt: switching to model gemini, same session and state, and retrying the stage turn"),
        _stage("hunt: repair turn on gemini (step budget 40)", "repair"),
    ])
    ok = lambda *ids: [{"id": i, "status": "pass"} for i in ids]  # noqa: E731
    bad = lambda *ids: [{"id": i, "status": "fail"} for i in ids]  # noqa: E731
    _write(d / "stages" / "02-hunt.jsonl", [
        {"event": "turn", "kind": "stage", "steps": 5},
        {"event": "acceptance", "items": ok("a") + bad("b", "c")},
        {"event": "turn", "kind": "repair", "steps": 3},
        {"event": "acceptance", "items": ok("a", "b") + bad("c")},
        {"event": "turn", "kind": "repair", "steps": 4},
        {"event": "acceptance", "items": ok("a", "b") + bad("c")},
    ])
    return d


def test_classify_error():
    assert oh.classify_error("cannot modify x: file has not been read — read") == "file-not-read"
    assert oh.classify_error('cannot read "/w/b.py": not found') == "no-such-file"
    assert oh.classify_error("ls: cannot access 'p': No such file or directory") == "no-such-file"
    assert oh.classify_error("ModuleNotFoundError: No module named 'x'") == "module not found"
    assert oh.classify_error("bwrap: setting up uid map: Permission denied") == "sandbox refused"
    assert oh.classify_error("REFUSED: nothing to push") == "other"
    assert oh.classify_error(None) == "other"


def test_stage_health_wasted_and_rounds():
    rows = [{"event": "turn", "kind": "stage", "steps": 5},
            {"event": "acceptance", "items": [{"id": "a", "status": "pass"}, {"id": "b", "status": "fail"}]},
            {"event": "turn", "kind": "repair", "steps": 3},
            {"event": "acceptance", "items": [{"id": "a", "status": "pass"}, {"id": "b", "status": "fail"}]}]
    h = oh.stage_health(rows)
    assert (h["steps"], h["repair_turns"], h["wasted_steps"]) == (8, 1, 3)
    assert h["acceptance_rounds"] == [{"after_steps": 5, "unmet": ["b"]}, {"after_steps": 8, "unmet": ["b"]}]
    assert oh.stage_health([{"event": "turn", "steps": 4}])["wasted_steps"] == 4     # nothing ever passed


def test_compute_and_write(tmp_path):
    d = _run(tmp_path)
    h = oh.compute(d)
    assert h["tool_calls"] == 6 and h["tool_errors_total"] == 5
    assert h["tool_errors"] == {"sandbox refused": 1, "module not found": 1, "file-not-read": 1, "no-such-file": 1, "other": 1}
    assert (h["failovers"], h["cooldown_waits"], h["cooldown_seconds"]) == (1, 1, 122)
    hunt = h["stages"]["hunt"]
    assert (hunt["steps"], hunt["repair_turns"], hunt["wasted_steps"]) == (12, 2, 4)
    assert hunt["acceptance_rounds"][0]["unmet"] == ["b", "c"]
    text = oh.render(h, markdown=True)
    assert text.startswith("## Run health") and "round 1" in text and "unmet b, c" in text
    oh.write(d)
    assert json.loads((d / "health.json").read_text())["repair_turns"] == 2


def test_compute_empty_dir(tmp_path):
    h = oh.compute(tmp_path)
    assert h["tool_calls"] == 0 and h["stages"] == {}


def test_format_event_and_line():
    assert ow.format_event(_tool(3)) == "10:32:45 ok  #3 bash x=1"
    err = ow.format_event(_tool(4, "error", result="boom"))
    assert err == "10:32:45 ERR #4 bash x=1 :: boom"
    assert ow.format_event(_stage("hello", "stage")) == "10:32:22 [stage] hello"
    text, ev = ow.format_line("not json")
    assert text == "RAW not json" and ev is None
    assert "ALERT" in ow.alert_text(250)


def test_tally_summary():
    t = ow.Tally()
    for e in (_stage("hunt: stage turn on qwen (step budget 20)", "stage"), _tool(1), _tool(2, "error", result="No module named 'x'"),
              _stage("hunt: repair turn on gemini (step budget 40)", "repair")):
        t.add(e)
    s = t.summary()
    assert "2 tool call(s)" in s and "module not found 1" in s and "repair turns 1" in s and "gemini, qwen" in s


def test_finished(tmp_path):
    assert ow.finished(tmp_path, alive=lambda p: True) == ""
    (tmp_path / "run.json").write_text('{"pid": 99}')
    assert "gone" in ow.finished(tmp_path, alive=lambda p: False)
    (tmp_path / "state.json").write_text('{"outcome": "running"}')
    assert ow.finished(tmp_path, alive=lambda p: True) == ""
    (tmp_path / "state.json").write_text('{"outcome": "completed"}')
    assert ow.finished(tmp_path, alive=lambda p: True) == "outcome: completed"


def test_watch_follows_alerts_and_stops(tmp_path):
    d = _run(tmp_path)
    (d / "state.json").write_text('{"outcome": "completed"}')
    lines, clock = [], [0.0]
    ow.watch(d, out=lines.append, sleep=lambda s: clock.__setitem__(0, clock[0] + 300), now=lambda: clock[0], alive=lambda p: True)
    assert lines[0].startswith("watching ")
    assert sum(1 for line in lines if " #" in line) == 6
    assert any(line.startswith("run ended (outcome: completed)") for line in lines)
    assert lines[-1].startswith("summary: 6 tool call(s)")


def test_watch_alert_after_silence_and_half_line(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    (d / "supervisor.jsonl").write_text(json.dumps(_tool(1)) + "\n" + json.dumps(_tool(2))[:20])    # second line unfinished
    lines, clock = [], [0.0]

    def sleep(s):
        clock[0] += 100

    ow.watch(d, out=lines.append, sleep=sleep, now=lambda: clock[0], alive=lambda p: True, max_polls=4)
    assert sum(1 for line in lines if " #" in line) == 1            # the unfinished line is not printed
    assert any(line.startswith("ALERT") for line in lines)
