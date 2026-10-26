"""
The staged workflow of `freeharness oss run` (src/freeharness/oss_flow.py) end to end, offline: the real DeepSeek Harness,
the real GitHub gate, a fake GitHub on a free port (never 8766), local bare repos and the scripted fake model. The script
is ONE flat list of steps; each stage turn ends with a text step, and a repair turn consumes the steps that follow.
Skipped without node / the harness.
"""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from freeharness import oss_cmd, paths
from freeharness.client import rpc
from freeharness.eval import oss_judge as oj

from test_oss_eval import SRC
from test_oss_eval_dsh import (BRANCH, COMMIT, LINT_TEST, PR_BODY, SEARCHES, Human, LiveFake, World,  # noqa: F401
                               assert_nothing_posted_and_no_global_change, bash, gh, needs_harness, skill)

DONE = {"text": "Finished this stage.\nSTAGE DONE"}
REPRO = bash("cd proj && PYTHONPATH=src python3 -B -c 'from app import average; print(average([]))'")
REPORT = ("mkdir -p reports && cat > reports/proj_issues.md <<'EOF'\n# up/proj: candidate issues\n\nComponent reviewed: src\n"
          "Upstream default branch: main\n\n## Candidate 1: average() raises ZeroDivisionError on an empty list\n"
          "- Where: src/app.py:2\n- What is wrong: it divides by len(xs) even when xs is empty\n"
          "- Reproduce: `cd proj && PYTHONPATH=src python3 -B -c 'from app import average; print(average([]))'`\n"
          "- Expected: 0 or a clear error\n- Actual: ZeroDivisionError: division by zero\n"
          "- Documented: src/app.py:1 (the docstring says average() of a list)\n"
          "- Quote: sum(xs) / len(xs)\n- Intent checked: src/app.py, no comment calls the empty case deliberate\n"
          "- Valid input because: an empty list is a list, which the signature accepts\n\nRepro output:\n\n"
          "    ZeroDivisionError: division by zero\n\n- Duplicate search: pending\nEOF")
REPRO2 = bash("cd proj && PYTHONPATH=src python3 -B -c 'from app import average; print(average(None))'")
REPORT2 = ("cat >> reports/proj_issues.md <<'EOF'\n\n## Candidate 2: average(None) raises TypeError\n- Where: src/app.py:2\n"
           "- What is wrong: no input validation\n- Reproduce: `cd proj && python3 -c 'from app import average; average(None)'`\n"
           "- Expected: a clear ValueError\n- Actual: TypeError: 'NoneType' object is not iterable\n"
           "- Documented: src/app.py:1 (the docstring)\n- Quote: sum(xs) / len(xs)\n- Intent checked: src/app.py has no note on None\n- Valid input because: None is what a caller passes for no data\n\nRepro output:\n\n"
           "    TypeError: 'NoneType' object is not iterable\nEOF")
ADD_TEST = bash("cd proj && cat >> tests/test_app.py <<'EOF'\n\n\nclass TestEmpty(unittest.TestCase):\n    def test_empty(self):\n"
                "        self.assertEqual(average([]), 0)\nEOF")
FIX = bash("cd proj && sed -i 's|return sum(xs) / len(xs)|return sum(xs) / len(xs) if xs else 0|' src/app.py")
COMMIT_CMD = bash(f"cd proj && git add -A && git commit -q -m '{COMMIT}'")
DUP_SEARCHES = [gh("gh_search", repo="up/proj", query="ZeroDivisionError average", kind="issues"),
                gh("gh_search", repo="up/proj", query="average empty", kind="prs"),
                gh("gh_search", repo="up/proj", query="division by zero empty list", kind="issues"),
                gh("gh_search", repo="up/proj", query="src/app.py average", kind="prs")]
DUP2_SEARCHES = [gh("gh_search", repo="up/proj", query="average none typeerror", kind="issues"),
                 gh("gh_search", repo="up/proj", query="average empty", kind="prs"),
                 gh("gh_search", repo="up/proj", query="NoneType object is not iterable", kind="issues"),
                 gh("gh_search", repo="up/proj", query="app.py none", kind="prs")]


def s_setup():
    return [skill("oss-contribution"), skill("agency-code-reviewer"), gh("gh_repo_info", repo="up/proj"),
            gh("gh_get_file", repo="up/proj", path="CONTRIBUTING.md"), gh("gh_fork", repo="up/proj"),
            gh("gh_clone", repo="up/proj", dest="proj"), DONE]


def s_hunt():
    return [REPRO, bash(REPORT), DONE]


def s_dup(searches=DUP_SEARCHES, decision="DECISION: proceed"):
    return [*searches, {"text": f"None of the hits is the same bug.\n{decision}"}]


def s_issue(number=900001):
    return [gh("gh_issue_create", repo="up/proj", title="average() raises ZeroDivisionError on an empty list",
               body="Steps: cd proj && PYTHONPATH=src python3 -c 'from app import average; average([])'\n"
                    "Expected: 0 or a clear error.\nActual: ZeroDivisionError: division by zero\nWhere: src/app.py:2"), DONE]


def s_fix(branch=BRANCH, before=True):
    steps = [gh("gh_branch", repo_dir="proj", name=branch), ADD_TEST]
    if before:
        steps.append(bash("cd proj && make test"))                        # must FAIL: the new test, before the fix
    return steps + [FIX, LINT_TEST, COMMIT_CMD, DONE]


def s_pr(issue=900001, branch=BRANCH):
    return [gh("gh_search", repo="up/proj", query=f"#{issue} empty average", kind="prs"),
            gh("gh_push", repo_dir="proj", branch=branch),
            gh("gh_pr_create", repo="up/proj", head=branch, base="main", title=COMMIT, body=PR_BODY.format(n=issue), issue=issue),
            DONE]


def good(issue=900001, branch=BRANCH):
    return s_setup() + s_hunt() + s_dup() + s_issue() + s_fix(branch) + s_pr(issue, branch)


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    yield w
    w.close()


@pytest.fixture
def live_world(tmp_path, monkeypatch):
    fake = LiveFake()
    fake.base = tmp_path / "git"
    w = World(tmp_path, monkeypatch, fake=fake)
    w.fake.add_item("up/proj", 77, "issue", "Placeholder for the number the fake hands out", "n/a", user="me")
    yield w
    w.close()


def run_flow(w, steps, **kw):
    w.serve(steps)
    code, rd = oss_cmd.run("up/proj", "src", ["auto"], False, kw.pop("max_steps", 120), 300.0, no_shim=True,
                           root=w.tmp / "runs", say=lambda s: None, **kw)
    assert rd is not None
    return code, rd


def state(rd):
    return json.loads(rd.state.read_text())


def checklist(rd):
    res = oj.oss_judge(rd.path)
    return res, {i["id"]: i for i in res["checklist"]}


def stage_log(rd, sid):
    (f,) = [p for p in rd.stages_dir.glob(f"*-{sid}.jsonl")]
    return [json.loads(x) for x in f.read_text().splitlines()]


# ======================================================================================= the happy path

@needs_harness
def test_a_correct_scripted_run_passes_all_six_stages(world):
    code, rd = run_flow(world, good())
    res, it = checklist(rd)
    assert res["ok"] and res["failed"] == [] and code == 0, res
    s = state(rd)
    assert s["outcome"] == "completed" and s["order"] == ["setup", "hunt", "dupcheck", "issue", "fix", "pr"]
    assert all(v["status"] == "passed" and v["repairs"] == 0 and v["turns"] == 1 for v in s["stages"].values()), s["stages"]
    # what the harness learned from the model's OWN records and put into state.json
    assert s["clone"] == "proj" and s["default_branch"] == "main" and s["component"] == "src"
    c = s["candidates"][0]
    assert c["title"] == "average() raises ZeroDivisionError on an empty list" and c["where"] == "src/app.py:2"
    assert "ZeroDivisionError" in c["repro"]["output"] and "python3" in c["repro"]["command"]
    assert c["decision"]["kind"] == "proceed"
    assert s["issue"] == {"number": 900001, "placeholder": True} and s["branch"] == BRANCH
    # short, focused prompts built from the state: never the whole history
    prompts = {sid: stage_log(rd, sid)[0]["prompt"] for sid in s["order"]}
    assert all(len(p) < 3500 for p in prompts.values()), {k: len(v) for k, v in prompts.items()}
    assert "ZeroDivisionError: division by zero" in prompts["issue"] and "Candidate 1" in prompts["issue"]
    assert "Issue #900001" in prompts["fix"] and "branch " + BRANCH in prompts["pr"]
    assert "venv" not in prompts["setup"] and "clone: `proj`" in prompts["hunt"]
    # the same dsh session for every stage, one checkpoint log per stage, the run record
    run = json.loads(rd.run.read_text())
    assert run["status"] == "completed" and run["session"] == "oss-" + rd.path.name and run["mode"] == "staged"
    assert len(list(rd.stages_dir.glob("*.jsonl"))) == 6
    assert json.loads(rd.config.read_text())["mode"] == "staged"
    calls = [json.loads(x) for x in rd.calls.read_text().splitlines()]
    assert [c["n"] for c in calls] == list(range(1, len(calls) + 1))
    # report: per-stage status and the final checklist
    text = oj.render_report(rd.path)
    assert "Stages" in text and "- fix: passed" in text and "[PASS] pr_files_match" in text and "status: passed" in text
    assert_nothing_posted_and_no_global_change(world, rd)


@needs_harness
def test_the_cli_runs_staged_by_default_and_report_and_list_work(world, capsys):
    world.serve(good())
    argv = ["run", "up/proj", "--component", "src", "--models", "auto", "--max-steps", "120", "--no-shim"]
    assert oss_cmd.main(argv) == 0
    (rd_path,) = list((paths.state_dir() / "oss").iterdir())
    assert json.loads((rd_path / "config.json").read_text())["mode"] == "staged"
    capsys.readouterr()
    assert oss_cmd.main(["report", "latest"]) == 0
    out = capsys.readouterr().out
    assert "Stages" in out and "- dupcheck: passed" in out and "[PASS] issue_draft" in out
    assert oss_cmd.main(["list"]) == 0 and rd_path.name in capsys.readouterr().out
    assert oss_cmd.main(["run"]) == 2                                  # neither a repo nor --resume
    assert oss_cmd.main(["run", "--resume", rd_path.name, "--no-shim"]) == 2     # every stage already passed


# ======================================================================================= repair

@needs_harness
def test_skipping_the_searches_fails_stage_3_and_the_repair_turn_fixes_it(world):
    steps = (s_setup() + s_hunt() + [{"text": "Checked. No duplicates.\nDECISION: proceed"}]      # no gh_search at all
             + s_dup()                                                                              # the repair turn
             + s_issue() + s_fix() + s_pr())
    code, rd = run_flow(world, steps)
    res, it = checklist(rd)
    assert res["ok"] and code == 0, res
    s = state(rd)
    d = s["stages"]["dupcheck"]
    assert d["status"] == "passed" and d["repairs"] == 1 and d["turns"] == 2
    assert all(s["stages"][k]["repairs"] == 0 for k in s["order"] if k != "dupcheck")
    log = stage_log(rd, "dupcheck")
    first = next(e for e in log if e["event"] == "acceptance")
    assert {i["id"] for i in first["items"] if i["status"] == "fail"} == {"duplicate_search", "searches_for_candidate"}
    repair = next(e for e in log if e["event"] == "turn" and e["kind"] == "repair")
    # only THIS stage's unmet items go back, with their evidence: nothing about the other stages' items
    assert "searches_for_candidate" in repair["prompt"] and "0 distinct queries" in repair["prompt"]
    assert "report_file" not in repair["prompt"] and "issue_draft" not in repair["prompt"]
    text = oj.render_report(rd.path)
    assert "- dupcheck: passed after 1 repair" in text


@needs_harness
def test_a_stage_that_cannot_be_repaired_ends_blocked_at_that_stage(world):
    steps = s_setup() + [{"text": "I looked and found nothing."}] * 3 + [{"text": "still nothing"}] * 5
    code, rd = run_flow(world, steps)
    s = state(rd)
    assert s["stages"]["hunt"]["status"] == "blocked" and s["stages"]["hunt"]["repairs"] == 2
    assert s["outcome"].startswith("blocked at hunt: unmet after 2 repair turn(s)") and "candidate_section" in s["outcome"]
    assert s["stages"]["dupcheck"]["status"] == "pending" and s["stages"]["setup"]["status"] == "passed"
    assert code == 1 and json.loads(rd.run.read_text())["status"] == "blocked"
    assert "gh_search" not in rd.calls.read_text(), "later stages never ran"
    assert "blocked at hunt" in oj.render_report(rd.path)


@needs_harness
def test_a_fix_without_a_failing_run_before_it_is_not_accepted(world):
    steps = (s_setup() + s_hunt() + s_dup() + s_issue() + s_fix(before=False) + [{"text": "done"}] * 4)
    code, rd = run_flow(world, steps)
    s = state(rd)
    assert s["stages"]["fix"]["status"] == "blocked" and code == 1
    unmet = " ".join(s["stages"]["fix"]["unmet"])
    assert "test_fails_before" in unmet and "test_passes_after" in unmet
    assert s["stages"]["pr"]["status"] == "pending"
    assert "write the test first and run it once BEFORE the fix" in stage_log(rd, "fix")[-2]["prompt"]


@needs_harness
def test_the_hunt_needs_a_repro_run_that_really_shows_the_failure(world):
    steps = (s_setup() + [bash("cd proj && cat src/app.py"), bash(REPORT), DONE]      # reading is not reproducing
             + [REPRO, DONE] + [{"text": "x"}] * 4)
    code, rd = run_flow(world, steps)
    log = stage_log(rd, "hunt")
    first = next(e for e in log if e["event"] == "acceptance")
    got = {i["id"]: i for i in first["items"]}
    assert got["repro_run"]["status"] == "fail" and "no bash call in this stage ran code" in got["repro_run"]["evidence"]
    assert got["repro_in_report"]["status"] == "fail"
    second = [e for e in log if e["event"] == "acceptance"][1]
    assert {i["id"]: i["status"] for i in second["items"]}["repro_run"] == "pass"


# ======================================================================================= duplicates

@needs_harness
def test_a_duplicate_ends_blocked_duplicate_after_at_most_two_candidates(world):
    world.fake.add_item("up/proj", 12, "pr", "Handle empty list in average()", "return 0 for empty input, avoids division by zero")
    steps = (s_setup() + s_hunt() + s_dup(decision="DECISION: duplicate:#12")
             + [REPRO2, bash(REPORT2), DONE]                                       # candidate 2
             + s_dup(DUP2_SEARCHES, "DECISION: competing:#12"))
    code, rd = run_flow(world, steps)
    s = state(rd)
    assert s["outcome"] == "blocked: duplicate" and s["blocked"]["kind"] == "duplicate" and code == oss_cmd_blocked()
    assert s["candidate_no"] == 2 and [d["decision"] for d in s["dropped"]] == ["duplicate:#12"]
    assert s["candidates"][0]["decision"]["number"] == 12 and "Handle empty list" in s["candidates"][0]["decision"]["hit"]
    assert s["candidates"][1]["decision"]["kind"] == "competing"
    assert s["stages"]["issue"]["status"] == "pending" and oj.read_drafts(rd) == []
    assert json.loads(rd.run.read_text())["status"] == "blocked"
    text = oj.render_report(rd.path)
    assert "outcome: blocked: duplicate" in text and "legitimate" in text and "candidate 2" in text
    assert world.fake.writes == []
    # the stop is final: there is nothing to resume
    assert oss_cmd.main(["run", "--resume", rd.path.name, "--no-shim"]) == 2


def oss_cmd_blocked():
    from freeharness import oss_flow
    return oss_flow.BLOCKED_EXIT


@needs_harness
def test_a_duplicate_without_another_candidate_ends_blocked_duplicate(world):
    world.fake.add_item("up/proj", 12, "issue", "average() crashes on []", "ZeroDivisionError average empty")
    steps = (s_setup() + s_hunt() + s_dup(decision="DECISION: duplicate:#12")
             + [{"text": "I looked again and there is no other reproducible bug here.\nDECISION: no_other_candidate"}])
    code, rd = run_flow(world, steps)
    s = state(rd)
    assert s["outcome"] == "blocked: duplicate" and code == oss_cmd_blocked()
    assert s["stages"]["hunt"]["status"] == "blocked" and "no other candidate" in s["stages"]["hunt"]["reason"]
    hunt2 = [e for e in stage_log(rd, "hunt") if e["event"] == "turn"][-1]["prompt"]
    assert "was dropped: duplicate:#12" in hunt2 and "average() crashes on []" in hunt2
    assert "DECISION: no_other_candidate" in hunt2


@needs_harness
def test_the_harness_feeds_back_overlapping_titles_so_a_competing_pr_is_noticed(world):
    world.fake.add_item("up/proj", 12, "pr", "Handle empty list in average()", "return 0 for empty input, avoids division by zero")
    steps = (s_setup() + s_hunt()
             + s_dup(decision="DECISION: proceed")                                                 # the model misses it ...
             + [{"text": "#12 is the same fix: an open PR for exactly this.\nDECISION: competing:#12"}]   # ... the review turn
             + [{"text": "no other candidate\nDECISION: no_other_candidate"}])
    code, rd = run_flow(world, steps)
    s = state(rd)
    hist = [h for h in s["history"] if h["id"] == "dupcheck"][0]
    assert hist["reviews"] == 1 and hist["turns"] == 2 and hist["repairs"] == 0
    assert hist["overlaps"] == ["#12 Handle empty list in average()"] and hist["status"] == "passed"
    review = [e for e in stage_log(rd, "dupcheck") if e["event"] == "turn" and e["kind"] == "review"][0]["prompt"]
    assert "#12 [pr open] Handle empty list in average()" in review and "shared words" in review
    assert s["dropped"][0]["decision"] == "competing:#12" and s["outcome"] == "blocked: duplicate" and code == oss_cmd_blocked()
    assert s["stages"]["issue"]["status"] == "pending"


# ======================================================================================= resume

@needs_harness
def test_resume_after_killing_the_process_continues_at_the_unfinished_stage(world, monkeypatch):
    slow = {"sleep": 40, "then": s_dup()[0]}
    steps = s_setup() + s_hunt() + [slow] + s_dup()[1:] + s_issue() + s_fix() + s_pr()
    world.serve(steps)
    code = (f"import sys\nfrom pathlib import Path\nimport freeharness.client.rpc as r\n"
            f"r.PATCH = Path({str(rpc.PATCH)!r})\nr.DSH_BIN = Path({str(rpc.DSH_BIN)!r})\n"
            "from freeharness import oss_cmd\n"
            "sys.exit(oss_cmd.main(['run', 'up/proj', '--component', 'src', '--models', 'auto', '--max-steps', '120', '--no-shim']))\n")
    env = dict(os.environ, PYTHONPATH=SRC)
    err = world.tmp / "run.err"
    with open(err, "w") as errf:
        proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=errf, stderr=subprocess.STDOUT)
    runs = paths.state_dir() / "oss"
    deadline, st = time.monotonic() + 150, {}
    while time.monotonic() < deadline:
        found = list(runs.glob("*/state.json")) if runs.is_dir() else []
        if found:
            try:
                st = json.loads(found[0].read_text())
            except ValueError:
                st = {}
            if st.get("stages", {}).get("hunt", {}).get("status") == "passed" and st.get("current") == "dupcheck":
                break
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("the run never reached stage 3: " + err.read_text()[:2500] + json.dumps(st)[:800])
    time.sleep(2.0)                                           # the stage-3 turn is now waiting on the slow model
    rd = oj.RunDir(found[0].parent)
    run = json.loads(rd.run.read_text())
    proc.kill()                                               # the process dies mid-run ...
    proc.wait(10)
    os.killpg(run["runtime_pid"], signal.SIGKILL)             # ... and so does its dsh (own process group)
    assert state(rd)["stages"]["dupcheck"]["status"] == "running" and state(rd)["stages"]["hunt"]["status"] == "passed"
    before = rd.calls.read_text().splitlines()
    assert [oj.tool_base(json.loads(x)["name"]) for x in before].count("gh_repo_info") == 1
    steps[steps.index(slow)] = s_dup()[0]                     # the model is fast now; the script keeps its place
    world.script.write_text(json.dumps(steps))
    assert oss_cmd.main(["run", "--resume", rd.path.name, "--no-shim"]) == 0
    s = state(rd)
    assert s["outcome"] == "completed" and all(v["status"] == "passed" for v in s["stages"].values())
    assert json.loads(rd.run.read_text())["resumed"] == 1
    calls = [json.loads(x) for x in rd.calls.read_text().splitlines()]
    names = [oj.tool_base(c["name"]) for c in calls]
    assert names.count("gh_repo_info") == 1 and names.count("gh_clone") == 1, "stages 1 and 2 were not run again"
    assert [c["n"] for c in calls] == list(range(1, len(calls) + 1))
    assert [json.loads(x) for x in before] == calls[:len(before)]
    assert s["stages"]["setup"]["turns"] == 1 and s["stages"]["hunt"]["turns"] == 1
    assert "resumed" in rd.supervisor.read_text()
    res, it = checklist(rd)
    assert res["ok"], res
    assert oj.assess(rd.path, res)["status"] == "passed"


# ======================================================================================= human review gates (live, fake GitHub)

def run_live(w, steps, confirm="confirmed"):
    w.serve(steps)
    return oss_cmd.run("up/proj", "src", ["auto"], True, 120, 300.0, no_shim=True, root=w.tmp / "runs", say=lambda s: None,
                       approver_factory=lambda rd: Human(rd, confirm))


@needs_harness
def test_live_staged_run_posts_only_what_the_human_approved(live_world):
    w = live_world
    code, rd = run_live(w, good(issue=77, branch="fix/77-empty-average"))
    res, it = checklist(rd)
    assert res["failed"] == [] and it["human_review"]["status"] == "pass" and it["issue_posted"]["status"] == "pass", res
    assert [(m, p) for m, p, _ in w.fake.writes] == [("POST", "/repos/up/proj/issues"), ("POST", "/repos/up/proj/pulls")]
    s = state(rd)
    assert s["issue"] == {"number": 77, "placeholder": False} and s["outcome"] == "completed" and code == 0
    assert "(dry-run placeholder)" not in stage_log(rd, "pr")[0]["prompt"]


@needs_harness
def test_live_staged_run_where_the_human_rejects_the_issue_is_blocked_not_repaired(live_world):
    w = live_world
    code, rd = run_live(w, s_setup() + s_hunt() + s_dup() + s_issue(77) + [{"text": "x"}] * 4, confirm="denied")
    s = state(rd)
    assert w.fake.writes == [] and code == 1
    assert s["stages"]["issue"]["status"] == "blocked" and s["stages"]["issue"]["repairs"] == 0
    assert s["outcome"] == "blocked at issue: rejected by the human reviewer"
    assert s["stages"]["fix"]["status"] == "pending"
    reviews = [json.loads(x) for x in rd.reviews.read_text().splitlines()]
    assert reviews and all(r["outcome"] == "denied" for r in reviews)



# ======================================================================================= --from-issues

ISSUE_BRANCH = "fix/6-empty-average"
REPORT_ISSUE = REPORT.replace("- Where: src/app.py:2\n", "- Issue: #6\n- Where: src/app.py:2\n", 1)


def s_hunt_issue(read=6, report=REPORT_ISSUE):
    return [gh("gh_list_issues", repo="up/proj", label="bug"), gh("gh_issue_read", repo="up/proj", number=read),
            REPRO, bash(report), DONE]


def run_issues(w, steps, **kw):
    w.serve(steps)
    code, rd = oss_cmd.run("up/proj", "", ["auto"], False, kw.pop("max_steps", 120), 300.0, no_shim=True,
                           root=w.tmp / "runs", say=lambda s: None, from_issues=True, **kw)
    assert rd is not None
    return code, rd


@needs_harness
def test_from_issues_adopts_an_open_labelled_bug_and_files_no_new_issue(world):
    world.fake.add_item("up/proj", 6, "issue", "average() crashes on an empty list", "average([]) divides by zero",
                        labels=["bug"])
    steps = s_setup() + s_hunt_issue() + s_dup() + s_fix(ISSUE_BRANCH) + s_pr(6, ISSUE_BRANCH)
    code, rd = run_issues(world, steps)
    res, it = checklist(rd)
    assert res["ok"] and res["failed"] == [] and code == 0, res
    s = state(rd)
    assert s["source"] == "issues" and s["outcome"] == "completed"
    assert s["issue"] == {"number": 6, "placeholder": False, "existing": True} and s["branch"] == ISSUE_BRANCH
    assert s["stages"]["issue"]["status"] == "passed" and s["stages"]["issue"]["turns"] == 0
    assert s["candidates"][0]["issue_no"] == 6
    assert "gh_issue_create" not in rd.calls.read_text()
    assert it["issue_draft"]["status"] == "na" and it["issue_posted"]["status"] == "na"
    assert it["issue_adopted"]["status"] == "pass" and it["pr_fixes_issue"]["status"] == "pass", it["pr_fixes_issue"]
    prompts = {sid: stage_log(rd, sid)[0]["prompt"] for sid in ("hunt", "dupcheck", "fix", "pr")}
    assert "gh_list_issues" in prompts["hunt"] and "label: \"bug\"" in prompts["hunt"]
    assert "open issue #6" in prompts["dupcheck"] and "an existing open issue of the project" in prompts["fix"]
    assert json.loads(rd.config.read_text())["source"] == "issues"
    text = oj.render_report(rd.path)
    assert "- issue: passed" in text and "[PASS] issue_adopted" in text
    assert_nothing_posted_and_no_global_change(world, rd)


@needs_harness
def test_from_issues_refuses_an_assigned_issue_and_one_that_was_not_read(world):
    world.fake.add_item("up/proj", 6, "issue", "average() crashes on an empty list", "x", labels=["bug"], assignees=["bob"])
    code, rd = run_issues(world, s_setup() + s_hunt_issue() + [{"text": "nothing else"}] * 6)
    s = state(rd)
    assert s["stages"]["hunt"]["status"] == "blocked" and code == 1
    assert "issue_chosen" in " ".join(s["stages"]["hunt"]["unmet"]) and "assigned to someone" in " ".join(s["stages"]["hunt"]["unmet"])
    assert s["stages"]["dupcheck"]["status"] == "pending"


def test_from_issues_cli_rules(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "fh"))
    assert oss_cmd.main(["run", "up/proj", "--from-issues", "--single-turn"]) == 2
    assert "staged flow only" in capsys.readouterr().out + capsys.readouterr().err
    assert oss_cmd.main(["run", "--resume", "latest", "--from-issues"]) == 2
