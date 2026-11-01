import json
import os
import time

from freeharness import prune_cmd as pc
from freeharness import review_oss as ro

REPORT = """Component reviewed: src/pkg

## Candidate 1: parser crashes on empty
Where: src/pkg/p.py:10
Reproduce: python ../scratch/repro1.py
Expected: []
Actual: IndexError
## Candidate 2: tokenizer drops last char
Where: src/pkg/t.py:5
Reproduce: python ../scratch/repro2.py
Expected: abc
Actual: ab
"""


def issue_draft(title):
    return (f"# gh_issue_create on up/proj\n\nmode: dry-run\n\nNEW ISSUE on up/proj\nTitle: {title}\n\nbody of {title}\n\n--\n"
            "issue templates: none\nsearches run (4): x")


PR_DRAFT = ("# gh_pr_create on up/proj\n\nmode: dry-run\n\nNEW PULL REQUEST on up/proj\nfrom me:fix/9-x -> main   closes #9\n"
            "Title: Fix tokenizer\n\nFixes #9\n\n--\nfiles (2):\n  t.py\n  tests/test_t.py\nsearches run (4): x\n"
            "competing-PR check: clean (no notes)")


def no_git(clone, *a, **k):
    return 1, ""


def make_run(tmp_path, name="20260101-000000-aaaa", state=None, run=None, drafts=None, report=REPORT):
    d = tmp_path / name
    (d / "workspace" / "reports").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"repo": "up/proj", "live": False}))
    base = {"repo": "up/proj", "component": "src/pkg", "live": False, "candidate_no": 2, "outcome": "completed", "blocked": None,
            "current": "pr", "order": ["setup", "hunt", "dupcheck", "issue", "fix", "pr"], "stages": {}, "history": [],
            "dropped": [{"no": 1, "title": "parser crashes on empty", "decision": "duplicate:#7", "hit": "Parser crash"}],
            "candidates": [{"no": 1, "title": "parser crashes on empty"},
                           {"no": 2, "title": "tokenizer drops last char",
                            "repro": {"n": 4, "command": "python ../scratch/repro2.py", "output": "got ab\nTraceback X", "exit": 1,
                                      "signal": "Traceback"},
                            "decision": {"kind": "proceed", "number": None, "hit": ""}}],
            "branch": "", "clone": "", "default_branch": "main"}
    base.update(state or {})
    (d / "state.json").write_text(json.dumps(base))
    (d / "run.json").write_text(json.dumps({"status": "completed", **(run or {})}))
    (d / "workspace" / "reports" / "proj_issues.md").write_text(report)
    (d / "ghgate" / "drafts").mkdir(parents=True)
    for i, txt in enumerate(drafts or []):
        (d / "ghgate" / "drafts" / f"{i:02d}-d.md").write_text(txt)
    (d / "stages").mkdir()
    ev = [{"event": "turn"}, {"event": "acceptance", "items": [{"id": "repro_run", "status": "pass", "evidence": "step 4: ok"},
                                                              {"id": "decision_line", "status": "fail", "evidence": "no DECISION"}]}]
    (d / "stages" / "02-hunt.jsonl").write_text("\n".join(json.dumps(e) for e in ev))
    (d / "calls.jsonl").write_text(json.dumps({"n": 1, "name": "gh_search", "text": "#12 [pr open] tokenizer drops char (@bob)"}) + "\n")
    return d


def test_show_renders_everything_and_marks_dropped_draft(tmp_path):
    d = make_run(tmp_path, drafts=[issue_draft("parser crashes on empty"), issue_draft("tokenizer drops last char"), PR_DRAFT])
    txt = ro.render(ro.collect(d, git=no_git))
    assert "repo up/proj" in txt and "component src/pkg" in txt
    assert "tokenizer drops last char" in txt and "python ../scratch/repro2.py" in txt and "got ab" in txt
    assert "[PASS] repro_run: step 4: ok" in txt and "[FAIL] decision_line: no DECISION" in txt
    assert "decision: proceed" in txt
    assert "DO NOT APPROVE" in txt and "belongs to DROPPED candidate 1 (duplicate:#7)" in txt
    assert "*** STALE" in txt and "Fix tokenizer" in txt and "head me:fix/9-x -> main" in txt
    assert "candidate 1: parser crashes on empty  -> duplicate:#7" in txt


def test_show_clean_run_has_no_warning(tmp_path):
    d = make_run(tmp_path, state={"dropped": []}, drafts=[issue_draft("tokenizer drops last char")])
    data = ro.collect(d, git=no_git)
    assert ro.warnings(data) == [] and "no stale draft detected" in ro.render(data)


def test_show_blocked_and_unfinished_runs_warn(tmp_path):
    d = make_run(tmp_path, state={"blocked": {"stage": "dupcheck", "reason": "duplicate:#7"}, "outcome": "blocked: duplicate"},
                 run={"status": "blocked"})
    assert "BLOCKED at dupcheck: duplicate:#7" in ro.render(ro.collect(d, git=no_git))
    d2 = make_run(tmp_path, name="r2", state={"outcome": "running", "dropped": []}, run={"status": "starting"})
    assert "NOT FINISHED" in ro.render(ro.collect(d2, git=no_git))


def test_show_fix_from_git_output(tmp_path):
    d = make_run(tmp_path, state={"branch": "fix/9-x", "clone": "proj"})
    (d / "workspace" / "proj").mkdir()

    def git(clone, *args, **k):
        if args[0] == "rev-parse":
            return 0, "abc\n"
        if args[0] == "log":
            return 0, "1a2b3c fix tokenizer\n"
        if args[:2] == ("diff", "--stat"):
            return 0, " t.py | 2 +-\n 1 file changed\n"
        return 0, "t.py\ntests/test_t.py\n"
    txt = ro.render(ro.collect(d, git=git))
    assert "branch fix/9-x" in txt and "1a2b3c fix tokenizer" in txt and "changed files: t.py, tests/test_t.py" in txt
    assert "t.py | 2 +-" in txt


def test_show_main_unknown_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "home"))
    assert ro.main(["nope"]) == 2


# ---------------------------------------------------------------- prune

def age(d, days):
    t = time.time() - days * 86400
    for p in [d, *d.iterdir()]:
        os.utime(p, (t, t))


def fill(d):
    (d / "workspace" / "clone").mkdir(parents=True, exist_ok=True)
    (d / "workspace" / "clone" / "big.bin").write_bytes(b"x" * 5000)
    (d / "transcript").mkdir(exist_ok=True)
    (d / "transcript" / "s.md").write_text("t" * 300)
    (d / "supervisor.jsonl").write_text("s" * 20)
    (d / "report.md").write_text("report")
    (d / "health.json").write_text("{}")


def test_plan_orders_and_counts_bytes_and_keeps_reports(tmp_path):
    d = make_run(tmp_path, state={"dropped": []})
    fill(d)
    age(d, 40)
    (plan,) = pc.plan_prune(tmp_path, 30)
    names = [i.path.name for i in plan.items]
    assert names[0] == "workspace" and set(names) >= {"transcript", "calls.jsonl", "supervisor.jsonl", "stages"}
    assert "report.md" not in names and "state.json" not in names and "health.json" not in names
    assert plan.items[0].bytes >= 5000
    (p2,) = pc.plan_prune(tmp_path, 30, keep_reports=True)
    assert [i.path.name for i in p2.items] == ["workspace"]
    (p3,) = pc.plan_prune(tmp_path, 30, all_=True)
    assert "report.md" in [i.path.name for i in p3.items]
    assert pc.plan_prune(tmp_path, 60) == []


def test_young_run_not_planned(tmp_path):
    make_run(tmp_path, state={"dropped": []})
    assert pc.plan_prune(tmp_path, 1) == []


def test_running_and_pending_runs_skipped_unless_force(tmp_path):
    a = make_run(tmp_path, name="a", state={"dropped": []}, run={"pid": 4242, "status": "running"})
    b = make_run(tmp_path, name="b", state={"dropped": []}, drafts=[issue_draft("tokenizer drops last char")])
    c = make_run(tmp_path, name="c", state={"dropped": []})
    for d in (a, b, c):
        fill(d)
        age(d, 40)

    def alive(pid):
        return pid == 4242
    plans = {p.run: p for p in pc.plan_prune(tmp_path, 30, alive=alive)}
    assert "running" in plans["a"].skipped and "undecided draft" in plans["b"].skipped
    assert plans["c"].skipped == "" and plans["c"].items
    forced = {p.run: p for p in pc.plan_prune(tmp_path, 30, force=True, alive=alive)}
    assert all(not p.skipped and p.items for p in forced.values())
    (b / "reviews.jsonl").write_text(json.dumps({"draft": "00-d", "outcome": "denied"}) + "\n")      # a decision releases it
    age(b, 40)
    assert {p.run: p for p in pc.plan_prune(tmp_path, 30, alive=alive)}["b"].skipped == ""


def test_dropped_candidate_draft_does_not_block(tmp_path):
    d = make_run(tmp_path, drafts=[issue_draft("parser crashes on empty")])
    fill(d)
    age(d, 40)
    (plan,) = pc.plan_prune(tmp_path, 30)
    assert plan.skipped == ""


def test_main_dry_run_by_default_then_apply_stays_inside_root(tmp_path, capsys):
    root = tmp_path / "oss"
    root.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    d = make_run(root, state={"dropped": []})
    fill(d)
    os.symlink(outside, d / "workspace" / "link")        # a symlink in the workspace must never be followed
    age(d, 40)
    assert pc.main(["--older-than", "30", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "would delete" in out and "would free" in out and (d / "workspace").exists()
    assert pc.main(["--older-than", "30", "--root", str(root), "--dry-run", "--no-dry-run"]) == 0
    assert (d / "workspace").exists()
    assert pc.main(["--older-than", "30", "--root", str(root), "--no-dry-run"]) == 0
    assert not (d / "workspace").exists() and not (d / "transcript").exists()
    assert (d / "report.md").exists() and (d / "state.json").exists() and (outside / "keep.txt").read_text() == "keep"


def test_apply_refuses_paths_outside_root(tmp_path):
    root = tmp_path / "oss"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "f").write_text("x")
    assert pc.apply_plan([pc.RunPlan("x", [pc.Item(victim, 1)])], root) == 0 and (victim / "f").exists()
    assert pc.apply_plan([pc.RunPlan("x", [pc.Item(root, 1)])], root) == 0 and root.exists()


def test_cli_registers_subcommands():
    from freeharness import cli
    assert "prune" in cli.SUBCOMMANDS
