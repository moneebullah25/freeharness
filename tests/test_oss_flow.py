"""
The staged OSS workflow without dsh: the stage-level evaluation helpers (eval/oss_stage.py), the prompt templates and
state handling of oss_flow.py, model fallback / step budgets / blocking with a stub runtime, and `--resume` refusals.
The end-to-end runs (real dsh, real gate, fake GitHub, scripted model) are tests/test_oss_flow_dsh.py.
"""
import json
import os

import pytest

from freeharness import oss_cmd, oss_flow
from freeharness.client.rpc import TurnResult
from freeharness.eval import oss_judge as oj
from freeharness.eval import oss_stage as st

from test_oss_eval import TOKEN

REPORT = """# up/proj: candidate issues

Component reviewed: src
Upstream default branch: main

## Candidate 1: average() raises ZeroDivisionError on an empty list
- Where: src/app.py:2
- What is wrong: it divides by len(xs) even when xs is empty
- Reproduce: `python3 -c 'from app import average; average([])'`
- Expected: 0 or a clear error
- Actual: ZeroDivisionError: division by zero

Repro output:

    ZeroDivisionError: division by zero

### Duplicate search
| query | hits |

## Candidate 2: median() is wrong for even lengths
- Where: src/stats.py:9
- Expected: 2.5
- Actual: 3

## Dropped
- nothing
"""


def call(n, cmd, text, exit_=0, name="bash"):
    return {"n": n, "name": name, "args": {"command": cmd}, "text": text, "exit": exit_, "isError": False}


# ======================================================================================= pure helpers

def test_decision_lines_are_parsed_and_the_last_one_wins():
    p = st.parse_decision
    assert p(["thinking\nDECISION: proceed"])["kind"] == "proceed"
    assert p(["DECISION: duplicate:#12"]) == {"kind": "duplicate", "number": 12, "raw": "DECISION: duplicate:#12"}
    assert p(["**DECISION: competing: 7**"])["number"] == 7
    assert p(["- Decision: duplicate #3"]) is None                     # needs the colon form
    assert p(["DECISION: proceed", "later\nDECISION: competing:#9"])["kind"] == "competing"
    assert p(["nothing here"]) is None and p([]) is None
    assert p(["x"], "- Decision: proceed")["kind"] == "proceed"          # the report's own Decision line is the fallback
    assert p(["DECISION: no_other_candidate"])["kind"] == "no_other_candidate"


def test_a_duplicate_decision_must_name_a_number_the_searches_returned():
    rows = [{"number": 12, "kind": "pr", "state": "open", "title": "Handle empty list"},
            {"number": 3, "kind": "issue", "state": "closed", "title": "windows paths"}]
    d = st.decision_item
    assert d(st.parse_decision(["DECISION: duplicate:#12"]), rows)["status"] == "pass"
    assert d(st.parse_decision(["DECISION: duplicate:#99"]), rows)["status"] == "fail"
    bad = d(st.parse_decision(["DECISION: competing:#3"]), rows)
    assert bad["status"] == "fail" and "not a pull request" in bad["evidence"]
    assert d(None, rows)["status"] == "fail" and d(st.parse_decision(["DECISION: proceed"]), [])["status"] == "pass"


def test_overlap_is_keyword_overlap_and_only_points():
    sec = REPORT[REPORT.index("## Candidate 1"):REPORT.index("## Candidate 2")]
    rows = [{"number": 12, "kind": "pr", "state": "open", "title": "Handle empty list in average()", "user": "a"},
            {"number": 5, "kind": "issue", "state": "closed", "title": "Typo in the readme", "user": "b"},
            {"number": 7, "kind": "issue", "state": "open", "title": "ZeroDivisionError in stats", "user": "c"},
            {"number": 8, "kind": "issue", "state": "open", "title": "empty things happen", "user": "c"}]
    got = st.overlapping(rows, sec)
    assert [r["number"] for r in got] == [12, 7]                  # two shared words, or one identifier; PRs first on ties
    assert "average" in got[0]["shared"] and st.overlapping([], sec) == []
    assert st.overlapping(rows[1:2], sec) == []


def test_search_result_rows_are_read_from_the_models_own_gh_search_calls():
    text = ("2 result(s) for prs 'average' (open and closed)\n#12 [pr open] Handle empty list in average() (@alice)\n"
            "#3 [issue closed] Old thing (@bob)")
    rows = st.result_rows([call(1, "", text, name="mcp__github__gh_search"), call(2, "ls", "x")])
    assert [(r["number"], r["kind"], r["state"], r["title"]) for r in rows] == [
        (12, "pr", "open", "Handle empty list in average()"), (3, "issue", "closed", "Old thing")]


@pytest.mark.parametrize("out,exp,act,sig", [
    ("Traceback (most recent call last):\n  File \"x\", line 1\nZeroDivisionError: division by zero\n[exit code: 1]", "", "", True),
    ("5\n", "6", "5", True),                                       # the model's stated wrong value really appears
    ("6\n", "6", "5", False),
    ("ok\n", "", "", False),
    ("FAILED tests/test_x.py::test_a - assert 1 == 2", "", "", True),
    ("ModuleNotFoundError: No module named 'yaml'", "", "", False),   # an environment problem is not the bug
    ("bash: foo: command not found\n[exit code: 127]", "", "", False),
    ("", "x", "y", False),
])
def test_failure_signal_rules(out, exp, act, sig):
    assert bool(st.failure_signal(out, exp, act)[0]) is sig


def test_environment_failures_are_named_not_counted():
    sig, prob = st.failure_signal("ModuleNotFoundError: No module named 'yaml'")
    assert sig == "" and "environment" in prob


@pytest.mark.parametrize("cmd,yes", [
    ("python3 -c 'print(1)'", True), ("cd proj && PYTHONPATH=src python3 -B ../scratch/r.py", True),
    ("/ws/venv/bin/python ../scratch/repro.py 2>&1 | tail -5", True), ("make test", True), ("pytest -k x", True),
    ("node repro.js", True), ("cat src/app.py", False), ("grep -n python Makefile", False), ("ls && echo python3", False),
    ("cat > r.md <<'EOF'\n- Reproduce: `cd p && python3 -c x`\nEOF", False),
    ("cat > repro.py <<'EOF'\nprint(1)\nEOF\npython3 repro.py", True),
])
def test_what_counts_as_running_code(cmd, yes):
    assert st.is_run_command(cmd) is yes


@pytest.mark.parametrize("c,yes", [
    ({"name": "edit", "args": {}}, True), ({"name": "mcp__x__write", "args": {}}, True),
    ({"name": "bash", "args": {"command": "sed -i 's/a/b/' f.py"}}, True),
    ({"name": "bash", "args": {"command": "cat >> tests/t.py <<'EOF'\nx\nEOF"}}, True),
    ({"name": "bash", "args": {"command": "echo hi > f"}}, True), ({"name": "bash", "args": {"command": "ls 2>&1 | head"}}, False),
    ({"name": "bash", "args": {"command": "make test >/dev/null"}}, False), ({"name": "read", "args": {}}, False),
])
def test_what_counts_as_an_edit_between_the_failing_and_the_passing_run(c, yes):
    assert st.modifies_files(c) is yes


def test_report_sections_fields_and_repro_acceptance(tmp_path):
    rd = oj.RunDir(tmp_path / "run")
    (rd.workspace / "reports").mkdir(parents=True)
    (rd.workspace / "reports" / "proj_issues.md").write_text(REPORT)
    rep = st.read_report(rd, "up/proj")
    assert rep["component"] == "src" and [s["no"] for s in rep["sections"]] == [1, 2]
    s1, s2 = rep["sections"]
    assert s1["title"].startswith("average()") and "### Duplicate search" in s1["text"] and "Candidate 2" not in s1["text"]
    assert "median()" in s2["title"] and "Dropped" not in s2["text"]
    assert st.field(s1["text"], "Expected") == "0 or a clear error" and st.field(s1["text"], "Where") == "src/app.py:2"
    assert st.candidate_section(rep, 3) is None
    tb = "Traceback (most recent call last):\n  File \"<string>\", line 1\nZeroDivisionError: division by zero\n[exit code: 1]"
    items, repro = st.find_repro([call(1, "cat src/app.py", "def average"), call(2, "python3 -c 'x'", tb, 1)], s1)
    got = {i["id"]: i for i in items}
    assert [i["status"] for i in items] == ["pass"] * 3 and repro["n"] == 2 and "ZeroDivisionError" in repro["output"]
    assert "failure text" in got["repro_run"]["evidence"] and "found in the report" in got["repro_in_report"]["evidence"]
    # a wrong VALUE is accepted when the model's own Actual line says so and the output shows it
    items, repro = st.find_repro([call(1, "python3 stats.py", "3\n")], s2)
    assert repro and "stated actual value" in repro["signal"]
    # no run, no section, nothing pasted: each is reported by its own item
    items, repro = st.find_repro([call(1, "cat x", "y")], None)
    assert repro is None and [i["status"] for i in items] == ["fail"] * 3


def test_stage_lines_show_passed_repaired_blocked_and_the_candidate(tmp_path):
    state = oss_flow.new_state(oj.RunDir(tmp_path), "up/proj", "", False)
    state["stages"]["setup"].update(status="passed")
    state["stages"]["hunt"].update(status="passed", repairs=1, turns=2, steps=9)
    state["stages"]["dupcheck"].update(status="blocked", reason="unmet after 2 repair turn(s)", repairs=2, unmet=["decision_line: x"])
    lines = st.stage_lines(state)
    assert lines[0].startswith("- setup: passed  [0 turn(s)")
    assert lines[1].startswith("- hunt: passed after 1 repair  [2 turn(s), 1 repair turn(s), 9 step(s)")
    assert lines[2].startswith("- dupcheck: blocked (unmet after 2") and "unmet: decision_line: x" in lines[3]
    state["candidate_no"] = 2
    state["history"].append({"id": "hunt", "status": "passed", "candidate": 1, "repairs": 0})
    assert any("hunt (candidate 1): passed" in x for x in st.stage_lines(state))


# ======================================================================================= prompts and state

def make_flow(tmp_path, live=False):
    rd = oj.RunDir(tmp_path / "run")
    for p in (rd.path, rd.workspace / "reports"):
        p.mkdir(parents=True, exist_ok=True)
    (rd.workspace / "reports" / "proj_issues.md").write_text(REPORT)
    state = oss_flow.new_state(rd, "up/proj", "src", live)
    return oss_flow.Flow(rd, state, None, None, ["auto"], 100, 10.0, None, lambda s: None, {})


def test_every_stage_prompt_is_short_filled_from_state_and_never_writes_the_models_text(tmp_path):
    f = make_flow(tmp_path)
    f.state.update(clone="proj", prefix="PYTHONPATH=/w /v/bin/python -m", default_branch="main", branch="fix/900001-x",
                   issue={"number": 900001, "placeholder": True})
    f.state["candidates"] = [{"no": 1, "title": "average() raises ZeroDivisionError on an empty list", "where": "src/app.py:2",
                              "repro": {"command": "python3 -c 'x'", "output": "ZeroDivisionError: division by zero"}}]
    seen = {}
    for stage in oss_flow.STAGES:
        text = f.render(stage)
        seen[stage.id] = text
        assert "$" not in text and len(text) < 3500, (stage.id, len(text))
        assert f"stage {f.state['order'].index(stage.id) + 1} of 6" in text and "venv command prefix" in text
    assert "ZeroDivisionError: division by zero" in seen["issue"] and "## Candidate 1" in seen["issue"]
    assert "issue: #900001 (dry-run placeholder)" in seen["fix"] and "Issue #900001" in seen["fix"]
    assert 'head: "fix/900001-x", base: "main"' in seen["pr"] and "issue: 900001" in seen["pr"]
    assert "Component to review: `src`" in seen["hunt"] and "## Candidate 1" in seen["hunt"]
    # nothing in the prompts is a draft, a query, a fix or PR text: they only order the work
    assert "gh_search AT LEAST 4 times" in seen["dupcheck"] and "title: like the commit line" in seen["pr"]
    f.state["candidate_no"] = 2
    f.state["dropped"] = [{"no": 1, "title": "average() raises", "decision": "duplicate:#12", "hit": "Handle empty list"}]
    hunt2 = f.render(oss_flow.BY_ID["hunt"])
    assert "was dropped: duplicate:#12 (Handle empty list)" in hunt2 and "ADD a `## Candidate 2" in hunt2
    assert "no_other_candidate" in hunt2


def test_the_stage_table_is_data_and_matches_the_spec():
    ids = [s.id for s in oss_flow.STAGES]
    assert ids == ["setup", "hunt", "dupcheck", "issue", "fix", "pr"]
    known = {"repo_rules_read", "skills_used", "duplicate_search", "report_file", "issue_draft", "issue_posted", "pr_draft",
             "pr_fixes_issue", "branch_pushed_normal", "no_markers", "commits_oneline", "pr_files_match", "lint_tests_run",
             "no_competing_pr", "no_bypass", "human_review"}
    assert all(set(s.judge + s.live_judge) <= known for s in oss_flow.STAGES), "stage acceptance reuses oss_judge ids"
    assert all(s.check in ("", "setup", "hunt", "dupcheck", "fix") for s in oss_flow.STAGES)
    assert oss_flow.BY_ID["setup"].judge == ("repo_rules_read", "skills_used") and oss_flow.BY_ID["setup"].check == "setup"
    assert oss_flow.BY_ID["dupcheck"].judge == ("duplicate_search",)
    assert oss_flow.BY_ID["pr"].judge == ("pr_draft", "pr_fixes_issue", "branch_pushed_normal", "pr_files_match", "no_competing_pr")
    assert oss_flow.BY_ID["issue"].live_judge == ("issue_posted",)
    assert all(s.max_repair == 2 and s.steps > 0 for s in oss_flow.STAGES)


# ======================================================================================= stub runtime: models, budgets, blocking

class StubRuntime:
    """Stands in for rpc.Runtime: no tools run, so every acceptance fails; records how the engine drives ONE session."""
    log: list = []
    fail_first_on: str = ""

    def __init__(self, cwd, model="auto", **kw):
        self.model, self.cwd, self.kw = model, cwd, kw
        StubRuntime.log.append(("init", model))

    def start(self):
        StubRuntime.log.append(("start", self.model))

    def restart(self, model=None):
        self.model = model or self.model
        StubRuntime.log.append(("restart", self.model))

    def resume(self, sid):
        StubRuntime.log.append(("resume", sid))

    def abandon(self, sid):
        StubRuntime.log.append(("abandon", sid))

    def close(self):
        StubRuntime.log.append(("close",))

    def run_turn(self, sid, text, on_event=None, timeout=0, max_steps=None):
        StubRuntime.log.append(("turn", self.model, sid, max_steps, text))
        if self.model == StubRuntime.fail_first_on and sum(1 for e in StubRuntime.log if e[0] == "turn") == 1:
            return TurnResult(False, {"kind": "error"}, "", 4, [], 0, 1.0, [])
        return TurnResult(True, {"kind": "completed"}, "done", 3, [], 0, 1.0, [])


@pytest.fixture
def stub(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "fh"))
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr(oss_cmd.rpc, "Runtime", StubRuntime)
    StubRuntime.log, StubRuntime.fail_first_on = [], ""
    return tmp_path


def test_a_failed_turn_retries_the_same_stage_on_the_next_model_with_the_same_session_and_state(stub):
    StubRuntime.fail_first_on = "gemini"
    code, rd = oss_cmd.run("up/proj", "src", ["gemini", "deepseek"], False, 50, 60.0, no_shim=True, root=stub / "runs",
                           say=lambda s: None)
    sid = "oss-" + rd.path.name
    kinds = [e[0] for e in StubRuntime.log]
    assert kinds[:6] == ["init", "start", "turn", "restart", "resume", "turn"] and kinds[-1] == "close"
    turns = [e for e in StubRuntime.log if e[0] == "turn"]
    assert [t[1] for t in turns] == ["gemini"] + ["deepseek"] * (len(turns) - 1) and {t[2] for t in turns} == {sid}
    first, second = turns[0], turns[1]
    assert "OSS contribution, stage 1 of 6" in first[4] and first[3] == 20
    assert second[4].startswith("The previous model stopped before finishing this stage") and first[4] in second[4]
    assert second[3] == 20                                         # the stage budget; the total (50 - 4) is not the limit yet
    s = json.loads(rd.state.read_text())
    assert s["model_idx"] == 1 and s["session"] == sid and s["stages"]["setup"]["models"] == ["gemini", "deepseek"]
    assert s["stages"]["setup"]["status"] == "blocked" and s["outcome"].startswith("blocked at setup: unmet after 2 repair turn(s)")
    assert s["stages"]["setup"]["repairs"] == 2 and "clone_exists" in s["outcome"] and s["stages"]["hunt"]["status"] == "pending"
    repairs = [t for t in turns if t[4].startswith("Stage setup: the harness checked")]
    assert len(repairs) == 2 and "repo_rules_read" in repairs[0][4] and "clone_exists" in repairs[0][4]
    run = json.loads(rd.run.read_text())
    assert run["status"] == "blocked" and run["models_used"] == ["gemini", "deepseek"] and code == 1
    assert oss_cmd.main(["report", str(rd.path)]) == 1 and "blocked at setup" in rd.report.read_text()


def test_the_repair_limit_is_settable_and_zero_means_judge_once(stub):
    code, rd = oss_cmd.run("up/proj", "src", ["auto"], False, 0, 60.0, no_shim=True, root=stub / "runs", say=lambda s: None,
                           repair=0)
    s = json.loads(rd.state.read_text())
    assert s["stages"]["setup"]["turns"] == 1 and s["stages"]["setup"]["repairs"] == 0
    assert s["outcome"].startswith("blocked at setup: unmet after 0 repair turn(s)")
    code, rd = oss_cmd.run("up/proj", "src", ["auto"], False, 0, 60.0, no_shim=True, root=stub / "runs", say=lambda s: None,
                           repair=1)
    assert json.loads(rd.state.read_text())["stages"]["setup"]["repairs"] == 1


def test_the_total_step_budget_is_shared_by_all_stage_turns(stub):
    code, rd = oss_cmd.run("up/proj", "src", ["auto"], False, 5, 60.0, no_shim=True, root=stub / "runs", say=lambda s: None)
    turns = [e for e in StubRuntime.log if e[0] == "turn"]
    assert [t[3] for t in turns] == [5, 2]                          # min(stage budget 20, left): 5, then 5 - 3
    run = json.loads(rd.run.read_text())
    s = json.loads(rd.state.read_text())
    assert run["status"] == "max_steps" and s["steps_total"] == 6 and "--max-steps" in s["outcome"] and code == 1


def test_single_turn_keeps_the_old_prompt_and_state_free_run(stub):
    code, rd = oss_cmd.run("up/proj", "src", ["auto"], False, 10, 60.0, no_shim=True, root=stub / "runs", say=lambda s: None,
                           repair=0, single_turn=True)
    (turn,) = [e for e in StubRuntime.log if e[0] == "turn"]
    assert turn[4].startswith("OSS contribution task for up/proj") and "13. Finish with a short summary" in turn[4]
    assert not rd.state.exists() and not rd.stages_dir.exists()
    assert json.loads(rd.config.read_text())["mode"] == "single-turn" and json.loads(rd.run.read_text())["status"] == "completed"


# ======================================================================================= --resume refusals

def staged_run(stub, **state_edit):
    rd = oss_cmd.prepare("up/proj", "src", ["auto"], False, 100, 60.0, stub / "runs")
    state = oss_flow.new_state(rd, "up/proj", "src", False)
    for k, v in state_edit.items():
        state[k] = v
    oss_cmd.write_json(rd.state, state)
    return rd, state


def test_resume_refuses_what_cannot_be_resumed(stub):
    say = []
    rd, state = staged_run(stub)
    old = oss_cmd.prepare("up/proj", "src", ["auto"], False, 100, 60.0, stub / "runs", single_turn=True)
    assert oss_cmd.resume_run(old, say=say.append)[0] == 2 and "not a staged run" in say[-1]
    for v in state["stages"].values():
        v["status"] = "passed"
    oss_cmd.write_json(rd.state, state)
    assert oss_cmd.resume_run(rd, say=say.append)[0] == 2 and "every stage already passed" in say[-1]
    state["stages"]["pr"]["status"] = "pending"
    state["blocked"] = {"stage": "dupcheck", "reason": "duplicate:#12", "kind": "duplicate"}
    oss_cmd.write_json(rd.state, state)
    assert oss_cmd.resume_run(rd, say=say.append)[0] == 2 and "legitimate outcome" in say[-1]
    state["blocked"] = None
    oss_cmd.write_json(rd.state, state)
    oss_cmd.write_json(rd.run, {"status": "running", "pid": os.getppid()})        # another live process owns the run
    assert oss_cmd.resume_run(rd, say=say.append)[0] == 2 and "still running" in say[-1]
    assert oss_cmd.main(["run", "--resume", rd.path.name, "--single-turn"]) == 2
    assert oss_cmd.main(["run", "other/repo", "--resume", rd.path.name]) == 2


def test_resume_picks_up_a_blocked_stage_and_keeps_finished_ones(stub):
    rd, state = staged_run(stub)
    state["stages"]["setup"].update(status="passed", turns=1, steps=7)
    state["stages"]["hunt"].update(status="blocked", reason="unmet after 2 repair turn(s): x", repairs=2, turns=3, calls_from=0)
    state["blocked"] = {"stage": "hunt", "reason": "x", "kind": "stage"}
    state["outcome"] = "blocked at hunt: x"
    oss_cmd.write_json(rd.state, state)
    code, _ = oss_cmd.resume_run(rd, no_shim=True, say=lambda s: None, repair=0)
    s = json.loads(rd.state.read_text())
    assert s["stages"]["setup"]["turns"] == 1 and s["stages"]["setup"]["status"] == "passed"
    assert s["stages"]["hunt"]["turns"] == 4 and s["stages"]["hunt"]["status"] == "blocked"       # retried with fresh repairs
    assert json.loads(rd.run.read_text())["resumed"] == 1
    turns = [e for e in StubRuntime.log if e[0] == "turn"]
    assert "OSS contribution, stage 2 of 6" in turns[0][4] and ("resume", "oss-" + rd.path.name) in StubRuntime.log


def test_resume_can_give_a_run_that_hit_max_steps_a_bigger_budget(stub):
    code, rd = oss_cmd.run("up/proj", "src", ["auto"], False, 5, 60.0, no_shim=True, root=stub / "runs", say=lambda s: None)
    assert json.loads(rd.run.read_text())["status"] == "max_steps"
    n_turns = sum(1 for e in StubRuntime.log if e[0] == "turn")
    assert oss_cmd.main(["run", "--resume", str(rd.path), "--max-steps", "60", "--no-shim", "--repair", "0"]) == 1
    assert json.loads(rd.config.read_text())["max_steps"] == 60
    s = json.loads(rd.state.read_text())
    assert s["outcome"].startswith("blocked at setup: unmet after 0 repair turn(s)")      # it ran again instead of stopping at once
    assert sum(1 for e in StubRuntime.log if e[0] == "turn") == n_turns + 1
