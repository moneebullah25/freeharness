"""
The staged workflow engine behind `freeharness oss run` (the default; `--single-turn` keeps the old one-prompt run).

Why: a free-tier web model (16k context) cannot hold a 14-step pipeline in ONE prompt: it repeats searches, forgets
steps and fabricates a final summary. So the harness orders the work and checks it; the model still does all of it.

  setup -> hunt -> dupcheck -> issue -> fix -> pr          (STAGES below: plain data)

Each stage is ONE `run_turn` on the same dsh session with a SHORT prompt (a template filled from `state.json`:
component, candidate, repro, issue number, branch, venv prefix; never the whole history), a step budget, and an
acceptance check that reads the tool-call record, the gate's own files and git (eval/oss_judge.py items plus the
stage-level helpers of eval/oss_stage.py). Unmet items go back to the model, ONLY that stage's, with their evidence, for
up to `max_repair` repair turns; then the run advances or ends `blocked at <stage>: <reason>`.

The harness never writes a draft, a bug description, a search query, a fix or a PR text. It orders the work, hands the
model its OWN recorded evidence back (its report, its repro output, the titles its own searches returned), and checks.

Persistence: after every turn and stage the run directory gets `state.json` (the checkpoint) and `stages/NN-<id>.jsonl`
(each turn's prompt, the model's texts, the acceptance result). `freeharness oss run --resume <run-id>` restarts the
runtime on the SAME dsh session and workspace and continues at the first unfinished stage. A turn that ends abnormally
moves to the next model of `--models` (same session, same state) and the stage goes on.

Human review gates are untouched: the gate tools show every draft and live mode posts only after approval; a human
rejection ends the run `blocked at <stage>: rejected by the human reviewer` and is never "repaired".
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from string import Template

from . import loopguard, oss_cmd, paths, sessions
from .client import rpc
from .eval import oss_judge as oj
from .eval import oss_stage as st
from .transcript import Transcript

STATE_VERSION = 1
MAX_CANDIDATES = 2
DUPLICATE = "blocked: duplicate"
BLOCKED_EXIT = 3                    # a legitimate stop (duplicate / competing PR): neither a success nor a harness failure


@dataclass(frozen=True)
class Stage:
    id: str
    title: str
    prompt: str                          # string.Template; filled from state.json by Flow.context
    judge: tuple = ()                    # oss_judge checklist ids this stage must satisfy
    live_judge: tuple = ()               # more ids that only apply to a live run
    check: str = ""                      # a stage-specific check in Flow.CHECKS
    max_repair: int = 2
    steps: int = 30                      # step budget of ONE turn of this stage


HEADER = """OSS contribution, stage $n of 6: $title. Repo $repo.
All GitHub access goes ONLY through the gh_* tools; your shell has no network and no git credentials (never `git push`, `gh`, `curl`). If a gh_* tool refuses, read the reason and fix exactly that; if it cannot be fixed, stop and quote it.
$mode
State so far:
$summary
"""

PROMPTS = {
    "setup": """Do exactly this, then end with the line STAGE DONE:
1. skill tool: load `oss-contribution`, then ONE review skill (`agency-code-reviewer`). If one is not installed, say so and go on.
2. gh_repo_info {repo: "$repo"}. Read CONTRIBUTING, the PR template and the issue template with gh_get_file. Note the project's OWN test command, lint/format command and commit style.
3. gh_fork {repo: "$repo"}, then gh_clone {repo: "$repo", dest: "$name"} (a path relative to your working directory).
Do not look for bugs yet and do not search GitHub.""",

    "hunt": """$comp
$extra
1. Read the component's code in the clone `$name`. Use the review skill to look for ONE real bug: a file:line and a concrete input that goes wrong.
2. REPRODUCE it. Call gh_setup_env {repo_dir: "$name"} (it returns a COMMAND PREFIX), write a throwaway script OUTSIDE the clone (`scratch/repro$cand.py` in your working directory) and run it with bash: `cd $name && <prefix> python ../scratch/repro$cand.py`. Its output must show the failure (a traceback or a wrong value).
3. Write `reports/${name}_issues.md` (NOT inside the clone), layout in the oss-contribution skill (references/issues-file-template.md). $report_how Give the candidate section the lines `Where:` (file:line), `What is wrong:`, `Reproduce:` (the exact command), `Expected:`, `Actual:`, and paste the REAL output of the repro run under it.
Do not search GitHub and do not post anything yet. End with the line STAGE DONE.""",

    "dupcheck": """Candidate $cand: $cand_title
Run gh_search AT LEAST 4 times with DIFFERENT queries, for kind "issues" AND kind "prs" (the exact error text, the function name, the symptom in plain words, the file path). Open and closed are both searched. Read what comes back.
For each hit that could be the same bug, write one line in the candidate's duplicate-search table in `reports/${name}_issues.md` saying whether it overlaps and why.
Then end your final message with exactly one of these lines:
DECISION: proceed        (nobody reported or fixed this)
DECISION: duplicate:#N   (issue or PR #N is the same bug)
DECISION: competing:#N   (an open PR #N already fixes it)""",

    "issue": """Report section of the candidate (yours, from the report file):
$section
Repro command (yours): $repro_cmd
Repro output (recorded from your run):
$repro_out
Call gh_issue_create {repo: "$repo", title, body}. Title: short and specific. Body: steps to reproduce (the command above), expected, actual (the real output above), file:line, version or commit. Use the repo's issue template if it has one. $issue_note
Then say the issue number and end with the line STAGE DONE.""",

    "fix": """Issue #$issue ($issue_note). Work only in the clone `$name`. In order:
1. gh_branch {repo_dir: "$name", name: "fix/$issue-<short-slug>"} (it fetches upstream and branches off the default branch; bash has no network, never git fetch or checkout -b).
2. Write the regression test FIRST. Run it through the venv prefix and see it FAIL. That failing run is recorded; do not use git stash or revert the fix to prove it.
3. Make the smallest possible fix. Run the test again: it must PASS.
4. Run the project's own lint/format command and the whole test suite through the prefix, both exit 0 (`cd $name && <prefix> ruff check .`, `<prefix> pytest -q -p no:cacheprovider`). If you have no prefix yet, call gh_setup_env {repo_dir: "$name"}.
5. Commit: `cd $name && git add -A && git commit -m "<one line, imperative, at most 72 characters>"`. No body, no trailers, no Co-Authored-By or Signed-off-by, no mention of tools or AI, never --amend or rebase.
End with the line STAGE DONE.""",

    "pr": """Issue #$issue, branch $branch, clone `$name`.
1. gh_search prs for the issue number and for the key words of the bug (the competing-PR check). If an open PR already fixes it, stop and say so.
2. gh_push {repo_dir: "$name", branch: "$branch"} (plain push, never force).
3. gh_pr_create {repo: "$repo", head: "$branch", base: "$default", title: like the commit line, body: from the repo's PR template with `Fixes #$issue`, issue: $issue}. Tick a checklist box only if it is true.
End with the line STAGE DONE.""",
}

STAGES = [
    Stage("setup", "setup", PROMPTS["setup"], judge=("repo_rules_read", "skills_used"), check="setup", steps=20),
    Stage("hunt", "hunt and reproduce one bug", PROMPTS["hunt"], judge=("report_file",), check="hunt", steps=40),
    Stage("dupcheck", "duplicate check", PROMPTS["dupcheck"], judge=("duplicate_search",), check="dupcheck", steps=25),
    Stage("issue", "issue", PROMPTS["issue"], judge=("issue_draft",), live_judge=("issue_posted",), steps=12),
    Stage("fix", "fix, test, lint, commit", PROMPTS["fix"], judge=("lint_tests_run", "commits_oneline", "no_markers"),
          check="fix", steps=45),
    Stage("pr", "push and pull request", PROMPTS["pr"],
          judge=("pr_draft", "pr_fixes_issue", "branch_pushed_normal", "pr_files_match", "no_competing_pr"),
          live_judge=("human_review",), steps=15),
]
BY_ID = {s.id: s for s in STAGES}

OVERLAP = """The harness compared the titles your own searches returned with your candidate. These look similar:
$hits
For each one, say in one line whether it is the same bug and why (open, closed or merged matters). Then end with exactly one line:
DECISION: proceed | DECISION: duplicate:#N | DECISION: competing:#N"""

CONTINUE = ("The previous model stopped before finishing this stage. Check your earlier tool results in this session, do not "
            "repeat finished work, and finish what is missing.\n")
RESUMED = ("The run was interrupted and resumed. Some of this stage may already be done: check your earlier tool results and "
           "the files before repeating anything.\n")


def flow_prompt(repo: str, component: str, live: bool) -> str:
    """prompt.md of a staged run: what the run is; the model itself only ever sees the per-stage prompts."""
    return (f"OSS contribution task for {repo} (staged workflow).\n\n"
            f"{oss_cmd.mode_text(live)}\n"
            f"{('Component to review: `' + component + '`.') if component else 'The model picks ONE small component.'}\n\n"
            "The harness sends ONE stage at a time (prompt templates in freeharness/oss_flow.py), checks the stage against the "
            "tool-call record and the gate's files, and sends back only that stage's unmet items:\n"
            + "\n".join(f"{i}. {s.id}: {s.title} (max {s.max_repair} repair turns, {s.steps} steps per turn)"
                        for i, s in enumerate(STAGES, 1))
            + "\nState is checkpointed in state.json; `freeharness oss run --resume <run-id>` continues at the first unfinished stage.\n")


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _clip(s, n=300) -> str:
    return oj._clip(s, n)


def new_state(rd: oj.RunDir, repo: str, component: str, live: bool) -> dict:
    return {"version": STATE_VERSION, "run": rd.path.name, "session": "oss-" + rd.path.name, "repo": repo,
            "component": component, "live": live, "mode": "staged", "order": [s.id for s in STAGES],
            "stages": {s.id: {"status": "pending", "turns": 0, "repairs": 0, "reviews": 0, "steps": 0} for s in STAGES},
            "history": [], "candidate_no": 1, "candidates": [], "dropped": [], "model_idx": 0, "steps_total": 0,
            "current": "", "outcome": "starting", "blocked": None, "issue": None, "branch": "", "prefix": "",
            "clone": "", "default_branch": "", "created": oj.now_iso(), "updated": oj.now_iso()}


class Flow:
    """One execution of the stage list over a Runtime. `state` is the checkpoint; everything else is recomputed."""

    def __init__(self, rd: oj.RunDir, state: dict, rt, rec: oss_cmd.Recorder, models: list[str], max_steps: int,
                 turn_timeout: float, repair: int | None, say, info: dict, loop_guard: bool = True):
        self.loop_guard = loop_guard
        self.rd, self.state, self.rt, self.rec = rd, state, rt, rec
        self.models, self.max_steps, self.turn_timeout, self.repair = models, max_steps, turn_timeout, repair
        self.say, self.info = say, info
        self.repo, self.live, self.sid = state["repo"], bool(state["live"]), state["session"]
        self.name = self.repo.split("/")[1]
        self.stop = ""                      # why the run stopped early: 'max_steps' | 'models' | ''
        self.CHECKS = {"setup": self.check_setup, "hunt": self.check_hunt, "dupcheck": self.check_dupcheck,
                       "fix": self.check_fix}

    # ------------------------------------------------------------------ state

    @property
    def steps(self) -> int:
        return int(self.state.get("steps_total", 0))

    def checkpoint(self) -> None:
        self.state["updated"] = oj.now_iso()
        self.state["steps_total"] = self.steps
        oss_cmd.write_json(self.rd.state, self.state)

    def cand(self) -> dict:
        n = self.state["candidate_no"]
        while len(self.state["candidates"]) < n:
            self.state["candidates"].append({"no": len(self.state["candidates"]) + 1})
        return self.state["candidates"][n - 1]

    def srec(self, stage: Stage) -> dict:
        return self.state["stages"][stage.id]

    def note(self, text: str, **kw) -> None:
        self.rec.note("stage", text, **kw)

    def stage_log(self, stage: Stage, entry: dict) -> None:
        paths.private_dir(self.rd.stages_dir)
        idx = self.state["order"].index(stage.id) + 1
        with open(self.rd.stages_dir / f"{idx:02d}-{stage.id}.jsonl", "a", encoding="utf-8") as f:
            f.write(oss_cmd._scrub(json.dumps({"t": oj.now_iso(), **entry}, ensure_ascii=False)) + "\n")

    # ------------------------------------------------------------------ prompts

    def summary(self) -> str:
        s, c = self.state, self.cand() if self.state["candidates"] or self.state["candidate_no"] > 1 else {}
        lines = [f"- repo {s['repo']}" + (f", upstream default branch {s['default_branch']}" if s["default_branch"] else "")]
        if s["clone"]:
            lines.append(f"- clone: `{s['clone']}` in your working directory")
        if s["prefix"]:
            lines.append(f"- venv command prefix (from gh_setup_env): {s['prefix']}")
        if s["component"]:
            lines.append(f"- component: {s['component']}")
        for d in s["dropped"]:
            lines.append(f"- dropped candidate {d['no']}: {_clip(d.get('title', ''), 80)} ({d['decision']})")
        if c.get("title"):
            lines.append(f"- candidate {c['no']}: {_clip(c['title'], 100)}" + (f" at {c['where']}" if c.get("where") else ""))
        if c.get("repro"):
            lines.append(f"- repro: `{_clip(c['repro']['command'], 160)}`")
        if s["issue"]:
            lines.append(f"- issue: #{s['issue']['number']}" + (" (dry-run placeholder)" if s["issue"].get("placeholder") else ""))
        if s["branch"]:
            lines.append(f"- branch: {s['branch']}")
        return "\n".join(lines)

    def context(self, stage: Stage) -> dict:
        s, c = self.state, self.cand() if self.state["candidates"] or self.state["candidate_no"] > 1 else {}
        n = self.state["candidate_no"]
        rep = st.read_report(self.rd, self.repo)
        sec = st.candidate_section(rep, n)
        comp = (f"Component to review: `{self.state['component']}` (a directory or module of the repo). Review only that."
                if self.state["component"] else
                (f"Stay in the component you chose: {s['component']}." if s["component"] else
                 "Pick ONE small component yourself (one module or package) and name it on the report line `Component reviewed:`."))
        extra = ""
        if s["dropped"]:
            d = s["dropped"][-1]
            extra = (f"Your previous candidate ({_clip(d.get('title', ''), 80)}) was dropped: {d['decision']}"
                     f"{' (' + d['hit'] + ')' if d.get('hit') else ''}. Pick a DIFFERENT real bug in the same component. "
                     "If you cannot find another one, say so and end with the line DECISION: no_other_candidate.")
        repro = c.get("repro") or {}
        placeholder = bool((s["issue"] or {}).get("placeholder"))
        return {
            "n": self.state["order"].index(stage.id) + 1, "title": stage.title, "repo": self.repo, "name": self.name,
            "mode": oss_cmd.mode_text(self.live), "summary": self.summary(), "comp": comp, "extra": extra,
            "cand": n, "cand_title": c.get("title", ""), "default": s["default_branch"] or "<the upstream default branch>",
            "report_how": ("Start it with the line `Component reviewed: <path>` and a `## Candidate 1: <title>` section."
                           if n == 1 else "Keep the file and ADD a `## Candidate 2: <title>` section."),
            "section": _clip((sec or {}).get("text", ""), 2500).strip(), "repro_cmd": _clip(repro.get("command", ""), 600),
            "repro_out": _clip(repro.get("output", ""), 1200),
            "issue": (s["issue"] or {}).get("number", "<number>"), "branch": s["branch"] or "<branch>",
            "issue_note": ("dry-run: nothing is posted; the tool returns a PLACEHOLDER number: use it everywhere an issue "
                           "number is needed" if not self.live else
                           "live: the human approves or rejects the draft; use the REAL number from the result")
            if stage.id != "fix" else ("the dry-run placeholder number" if placeholder else "the number the issue got"),
        }

    def render(self, stage: Stage) -> str:
        ctx = self.context(stage)
        return Template(HEADER + stage.prompt).safe_substitute(ctx)

    def repair_prompt(self, stage: Stage, unmet: list[dict]) -> str:
        lines = "\n".join(f"- {i['id']}: {_clip(i['evidence'], 240)}" for i in unmet)
        return (f"Stage {stage.id}: the harness checked your work against this stage's requirements and these are NOT met "
                "yet. They come from tool-call records and the gate's files, not from your messages. Do the missing work "
                "with the tools (do not just say it is done), then end with the line STAGE DONE.\n"
                f"{lines}\nState:\n{self.summary()}")

    # ------------------------------------------------------------------ one turn (with model fallback)

    def turn(self, stage: Stage, prompt: str, kind: str):
        """Run one prompt on the session; on an abnormal end move the SAME session to the next model and retry the same
        prompt. Returns the TurnResult, or None when the run must stop (self.stop says why)."""
        rec_s = self.srec(stage)
        original, cur = prompt, prompt
        guard = loopguard.enabled(self.loop_guard)         # ON unless --no-loop-guard / FREEHARNESS_LOOP_GUARD=0
        watch, loops = loopguard.Watch(), 0
        while True:
            left = (self.max_steps - self.steps) if self.max_steps else None
            if self.max_steps and left <= 0:
                self.stop = "max_steps"
                return None
            budget = min(stage.steps, left) if left else stage.steps
            model = self.models[self.state["model_idx"]]
            self.rec.texts.clear()
            self.note(f"{stage.id}: {kind} turn on {model} (step budget {budget})", stage=stage.id, kind_=kind)
            if self.rec.tr:
                self.rec.tr.user(cur)
            self.state["current"] = stage.id
            self.info.update(status="running", model=model, stage=stage.id)
            oss_cmd.write_json(self.rd.run, self.info)
            self.checkpoint()
            err = ""
            watch.reset()
            try:
                res = self.rt.run_turn(self.sid, cur, on_event=self.rec.event, timeout=self.turn_timeout, max_steps=budget,
                                       **({"stop_when": watch.feed} if guard else {}))
            except (rpc.RuntimeDied, TimeoutError, rpc.RpcError) as e:
                err = str(e)[:300] or type(e).__name__
                self.note(f"{stage.id}: turn failed on {model}: {err}")
                self.info.setdefault("turns", []).append({"model": model, "stage": stage.id, "kind": kind, "error": err})
                try:
                    self.rt.abandon(self.sid)
                except Exception as e2:                          # noqa: BLE001
                    self.note(f"could not recover the runtime: {str(e2)[:200]}")
            else:
                self.state["steps_total"] = self.steps + res.steps
                rec_s["steps"] = rec_s.get("steps", 0) + res.steps
                rec_s["turns"] = rec_s.get("turns", 0) + 1
                if kind == "repair":
                    rec_s["repairs"] = rec_s.get("repairs", 0) + 1
                texts = list(self.rec.texts)
                if res.final_text and (not texts or texts[-1] != res.final_text):
                    texts.append(res.final_text[:3000])
                rec_s["texts"] = (rec_s.get("texts", []) + [t[:1500] for t in texts])[-8:]
                rec_s["final"] = _clip(res.final_text, 600)
                if model not in rec_s.setdefault("models", []):
                    rec_s["models"].append(model)
                self.info.setdefault("turns", []).append({"model": model, "stage": stage.id, "kind": kind, "ok": res.ok,
                                                          "reason": res.reason, "steps": res.steps,
                                                          "seconds": round(res.seconds, 1)})
                self.stage_log(stage, {"event": "turn", "kind": kind, "model": model, "prompt": cur, "texts": texts,
                                       "steps": res.steps, "reason": res.reason, "ok": res.ok})
                self.checkpoint()
                if res.reason.get("kind") == "loop_guard":
                    # The same call returned the same output again and again. End the turn and say so: the repair
                    # turn quotes the call and its output and asks for the next DIFFERENT action (never names one).
                    rep = {k: v for k, v in res.reason.items() if k != "kind"}
                    self.note(f"{stage.id}: loop guard: {loopguard.describe(rep)}")
                    self.stage_log(stage, {"event": "loop_guard", "call": rep["name"], "count": rep["count"]})
                    if loops < 2:
                        loops += 1
                        cur = loopguard.repair_text(rep) + f"\nStage {stage.id} state:\n{self.summary()}"
                        continue
                    return res                       # the same loop again after two repair turns: the acceptance check decides
                if res.ok or res.reason.get("kind") == "max_steps":
                    if not res.ok:
                        self.note(f"{stage.id}: the step budget of this turn ended; the harness checks what was done")
                    return res
                err = json.dumps(res.reason)[:300]
            if self.state["model_idx"] + 1 < len(self.models):
                self.state["model_idx"] += 1
                nxt = self.models[self.state["model_idx"]]
                self.note(f"{stage.id}: switching to model {nxt}, same session and state, and retrying the stage turn")
                try:
                    self.rt.restart(model=nxt)
                    self.rt.resume(self.sid)
                except (rpc.RuntimeDied, TimeoutError, rpc.RpcError) as e:
                    self.note(f"could not move the session to {nxt}: {str(e)[:200]}")
                cur = CONTINUE + original
                self.checkpoint()
                continue
            self.stop = "models"
            self.state["stop_detail"] = err
            return None

    # ------------------------------------------------------------------ acceptance

    def stage_calls(self, stage: Stage) -> list[dict]:
        return st.calls_since(self.rd, int(self.srec(stage).get("calls_from", 0)))

    def evaluate(self, stage: Stage) -> list[dict]:
        by = {i["id"]: i for i in oj.oss_judge(self.rd.path)["checklist"]}
        ids = list(stage.judge) + (list(stage.live_judge) if self.live else [])
        items = [by[i] for i in ids if i in by]
        if stage.check:
            items += self.CHECKS[stage.check](stage)
        return items

    def check_setup(self, stage: Stage) -> list[dict]:
        return [st.clone_item(self.rd, self.repo)]

    def check_hunt(self, stage: Stage) -> list[dict]:
        rep = st.read_report(self.rd, self.repo)
        n = self.state["candidate_no"]
        sec = st.candidate_section(rep, n)
        have = _item_candidate(rep, n, sec)
        items, repro = st.find_repro(self.stage_calls(stage), sec)
        self._repro = repro
        return [have] + items

    def check_dupcheck(self, stage: Stage) -> list[dict]:
        rec_s = self.srec(stage)
        rep = st.read_report(self.rd, self.repo)
        sec = st.candidate_section(rep, self.state["candidate_no"])
        rows = st.result_rows(self.stage_calls(stage))
        decision = st.parse_decision(rec_s.get("texts", []), (sec or {}).get("text", ""))
        self._decision, self._rows = decision, rows
        return [st.dup_search_item(self.rd, self.repo, int(rec_s.get("search_offset", 0))), st.decision_item(decision, rows)]

    def check_fix(self, stage: Stage) -> list[dict]:
        return st.fix_items(self.rd, self.repo, self.stage_calls(stage))

    # ------------------------------------------------------------------ after a stage passed: absorb what it produced

    def absorb(self, stage: Stage) -> None:
        s = self.state
        calls = st.all_calls(self.rd)
        for c in calls:                                              # the venv prefix, as gh_setup_env printed it
            if oj._is(c, "gh_setup_env") and not c.get("isError"):
                m = re.search(r"COMMAND PREFIX[^\n]*\n\s*(\S[^\n]*)", str(c.get("text", "")))
                if m:
                    s["prefix"] = m.group(1).strip()
        clone, reg = st.find_clone(self.rd, self.repo)
        if clone:
            s["clone"] = os.path.relpath(clone, self.rd.workspace)
            s["default_branch"] = reg.get("default_branch", s["default_branch"])
        c = self.cand()
        if stage.id == "hunt":
            rep = st.read_report(self.rd, self.repo)
            sec = st.candidate_section(rep, s["candidate_no"]) or {}
            if not s["component"]:
                s["component"] = rep["component"]
            c.update(title=sec.get("title", ""), where=st.field(sec.get("text", ""), "Where"),
                     expected=st.field(sec.get("text", ""), "Expected"), actual=st.field(sec.get("text", ""), "Actual"),
                     repro=getattr(self, "_repro", None) or c.get("repro"))
        elif stage.id == "dupcheck":
            d = getattr(self, "_decision", None)
            if d:
                hit = next((r for r in getattr(self, "_rows", []) if r["number"] == d["number"]), None)
                c["decision"] = {"kind": d["kind"], "number": d["number"], "hit": (hit or {}).get("title", "")}
        elif stage.id == "issue":
            drafts = [d for d in oj.read_drafts(self.rd) if d["action"] == "gh_issue_create"]
            n = oj._issue_number(self.rd, calls, drafts, self.live)
            if n:
                s["issue"] = {"number": n, "placeholder": not self.live}
        elif stage.id == "fix":
            ch = oj.Change(self.rd, self.repo, [d for d in oj.read_drafts(self.rd) if d["repo"].lower() == self.repo.lower()])
            if ch.branch:
                s["branch"] = ch.branch
                s["default_branch"] = ch.default or s["default_branch"]

    # ------------------------------------------------------------------ one stage

    def human_denied(self, stage: Stage) -> str:
        action = {"issue": "gh_issue_create", "pr": "gh_pr_create"}.get(stage.id)
        if not (self.live and action):
            return ""
        since = int(self.srec(stage).get("reviews_from", 0))
        rows = oj._jsonl(self.rd.reviews)[since:]
        return action if any(r.get("outcome") == "denied" and r.get("action") in (action, "gh_push") for r in rows) else ""

    def competing_seen(self, stage: Stage) -> str:
        if stage.id != "pr":
            return ""
        for c in reversed(self.stage_calls(stage)):
            if oj._is(c, "gh_pr_create") and "someone else is already on this issue" in str(c.get("text", "")):
                return _clip(c["text"], 160)
        return ""

    def block(self, stage: Stage, reason: str, kind: str = "stage") -> str:
        rec_s = self.srec(stage)
        rec_s.update(status="blocked", reason=reason, finished=oj.now_iso())
        self.state["blocked"] = {"stage": stage.id, "reason": reason, "kind": kind}
        self.state["outcome"] = DUPLICATE if kind == "duplicate" else f"blocked at {stage.id}: {reason}"
        self.note(f"{stage.id}: {self.state['outcome']}")
        self.checkpoint()
        return "blocked"

    def run_stage(self, stage: Stage) -> str:
        rec_s = self.srec(stage)
        resumed = rec_s["status"] == "running"
        if rec_s["status"] in ("pending", "blocked"):
            fresh = rec_s["status"] == "pending"
            rec_s.update(status="running", repairs=0, reviews=0, unmet=[], reason="", started=oj.now_iso(),
                         candidate=self.state["candidate_no"])
            if fresh:
                rec_s.update(calls_from=st.last_n(self.rd), search_offset=len(st.search_events(self.rd, self.repo)),
                             reviews_from=len(oj._jsonl(self.rd.reviews)), turns=0, steps=0, texts=[], models=[])
            self.state["blocked"] = None
        cap = self.repair if self.repair is not None else stage.max_repair
        self.state["current"] = stage.id
        self.note(f"stage {stage.id} ({self.state['order'].index(stage.id) + 1}/{len(STAGES)}) "
                  f"{'resumed' if resumed else 'started'}; candidate {self.state['candidate_no']}")
        self.checkpoint()
        prompt, kind = self.render(stage), "stage"
        if resumed:
            prompt = RESUMED + prompt
        while True:
            res = self.turn(stage, prompt, kind)
            if res is None:
                reason = ("the total step budget (--max-steps) is used up" if self.stop == "max_steps"
                          else f"no model could finish the turn ({self.state.get('stop_detail', '')})")
                return self.block(stage, reason, kind=self.stop or "stage")
            if stage.id == "hunt" and self.state["candidate_no"] > 1:
                d = st.parse_decision(list(self.rec.texts) + [res.final_text])
                if d and d["kind"] == "no_other_candidate":
                    return self.block(stage, "no other candidate: the model found no further reproducible bug", kind="duplicate")
            items = self.evaluate(stage)
            unmet = [i for i in items if i["status"] == oj.FAIL]
            rec_s["unmet"] = [f"{i['id']}: {_clip(i['evidence'], 200)}" for i in unmet]
            self.stage_log(stage, {"event": "acceptance", "kind": kind, "items": [
                {"id": i["id"], "status": i["status"], "evidence": _clip(i["evidence"], 300)} for i in items]})
            self.note(f"{stage.id}: acceptance after the {kind} turn: " + (
                f"{len(unmet)} unmet ({', '.join(i['id'] for i in unmet)})" if unmet else "all met"))
            if not unmet:
                follow = self.follow_up(stage)
                if follow:
                    prompt, kind = follow, "review"
                    continue
                self.absorb(stage)
                rec_s.update(status="passed", finished=oj.now_iso(), unmet=[])
                self.checkpoint()
                return "passed"
            denied = self.human_denied(stage)
            if denied:
                return self.block(stage, "rejected by the human reviewer", kind="human")
            if self.competing_seen(stage):
                return self.block(stage, "competing PR: " + self.competing_seen(stage), kind="duplicate")
            if rec_s["repairs"] >= cap:
                return self.block(stage, "unmet after %d repair turn(s): %s" % (
                    rec_s["repairs"], "; ".join(f"{i['id']} ({_clip(i['evidence'], 100)})" for i in unmet)))
            prompt, kind = self.repair_prompt(stage, unmet), "repair"

    def follow_up(self, stage: Stage) -> str:
        """Dupcheck: when the model said `proceed` but its own search results hold similar-looking titles, show them
        back ONCE and ask for a per-hit judgement and a fresh DECISION (so a duplicate or competing PR is noticed)."""
        rec_s = self.srec(stage)
        d = getattr(self, "_decision", None)
        if stage.id != "dupcheck" or rec_s.get("reviews") or not d or d["kind"] != "proceed":
            return ""
        rep = st.read_report(self.rd, self.repo)
        sec = st.candidate_section(rep, self.state["candidate_no"]) or {}
        ov = st.overlapping(getattr(self, "_rows", []), sec.get("text", ""))
        if not ov:
            return ""
        rec_s["reviews"] = 1
        rec_s["overlaps"] = [f"#{r['number']} {r['title']}" for r in ov]
        hits = "\n".join(f"- #{r['number']} [{r['kind']} {r['state']}] {r['title']}  (shared words: {', '.join(r['shared'])})"
                         for r in ov)
        return Template(OVERLAP).safe_substitute(hits=hits)

    # ------------------------------------------------------------------ the loop

    def next_stage(self) -> Stage | None:
        for s in STAGES:
            if self.state["stages"][s.id]["status"] != "passed":
                return s
        return None

    def after_dupcheck(self) -> str:
        """A duplicate / competing decision: another candidate (at most MAX_CANDIDATES) or 'blocked: duplicate'."""
        c = self.cand()
        d = c.get("decision") or {}
        if d.get("kind") in (None, "proceed"):
            return ""
        label = f"{d['kind']}:#{d.get('number')}"
        if self.state["candidate_no"] >= MAX_CANDIDATES:
            self.state["blocked"] = {"stage": "dupcheck", "reason": label, "kind": "duplicate"}
            self.state["outcome"] = DUPLICATE
            self.note(f"dupcheck: {label} on the last allowed candidate: {DUPLICATE}")
            return "blocked"
        self.state["dropped"].append({"no": c["no"], "title": c.get("title", ""), "decision": label, "hit": d.get("hit", "")})
        for sid in ("hunt", "dupcheck"):
            old = self.state["stages"][sid]
            self.state["history"].append({**old, "id": sid})
            self.state["stages"][sid] = {"status": "pending", "turns": 0, "repairs": 0, "reviews": 0, "steps": 0}
        self.state["candidate_no"] += 1
        self.note(f"dupcheck: {label}; the model may pick candidate {self.state['candidate_no']} (max {MAX_CANDIDATES})")
        self.checkpoint()
        return ""

    def run_all(self) -> str:
        while True:
            stage = self.next_stage()
            if stage is None:
                self.state["outcome"] = "completed"
                self.checkpoint()
                return "completed"
            if self.run_stage(stage) == "blocked":
                return "blocked"
            if stage.id == "dupcheck" and self.after_dupcheck() == "blocked":
                self.checkpoint()
                return "blocked"


def _item_candidate(rep: dict, n: int, sec) -> dict:
    title = f"the report has a '## Candidate {n}' section"
    if sec:
        return oj._item("candidate_section", title, oj.PASS, f"candidate {n}: {_clip(sec['title'], 80)}")
    return oj._item("candidate_section", title, oj.FAIL,
                    f"reports/<repo>_issues.md has {len(rep['sections'])} candidate section(s); need a '## Candidate {n}: <title>' one"
                    if rep["path"] else "no reports/<repo>_issues.md yet")


# --------------------------------------------------------------------------- the entry point

def execute_flow(rd: oj.RunDir, repo: str, models: list[str], live: bool, max_steps: int, turn_timeout: float,
                 no_shim: bool, providers: str, approver_factory, console, say, repair: int | None = None,
                 resume: bool = False) -> int:
    sid = "oss-" + rd.path.name
    prior = oj._json(rd.run) if resume else {}
    info = {**{k: v for k, v in prior.items() if k in ("turns", "started", "resumed")}, "run": rd.path.name, "session": sid,
            "status": "starting", "pid": os.getpid(), "mode": "staged", "models_used": [], "steps": 0}
    info.setdefault("started", oj.now_iso())
    if resume:
        info["resumed"] = int(info.get("resumed", 0)) + 1
    state = oj._json(rd.state) if resume else {}
    if not state:
        cfg = oj._json(rd.config)
        state = new_state(rd, repo, cfg.get("component", ""), live)
    state["outcome"] = "running"
    oss_cmd.write_json(rd.run, info)
    approver = approver_factory(rd) if approver_factory else None
    rec = oss_cmd.Recorder(rd, Transcript(sid, str(rd.workspace), directory=rd.transcripts), echo=lambda s: say("  " + s))
    model0 = models[min(int(state.get("model_idx", 0)), len(models) - 1)]
    rt = rpc.Runtime(rd.workspace, model=model0, home=paths.dsh_home(), patch=rpc.PATCH, overlay_files=[rd.overlay],
                     approver=approver)
    flow = Flow(rd, state, rt, rec, models, max_steps, turn_timeout, repair, say, info)
    shim, status, outcome = None, "error", ""
    try:
        if not no_shim:
            shim = rpc.ensure_shim(oss_cmd._provider_list(models, providers), sticky="auto", restart_stale=True)
        try:
            sessions.touch(sid, str(rd.workspace), rt.model)
            sessions.set_active(str(rd.workspace), sid)
        except (OSError, ValueError):
            pass
        rt.start()
        info["runtime_pid"] = getattr(getattr(rt, "_proc", None), "pid", None)
        oss_cmd.write_json(rd.run, info)
        if resume:
            try:
                rt.resume(sid)
            except (rpc.RpcError, rpc.RuntimeDied) as e:
                rec.note("note", f"no earlier dsh session to resume ({str(e)[:120]}); continuing with a new one")
        flow.checkpoint()
        outcome = flow.run_all()
        status = ("max_steps" if flow.stop == "max_steps" else "blocked" if outcome == "blocked" else "completed")
        if flow.stop == "models":
            status = "failed"
    except KeyboardInterrupt:
        status = "interrupted"
        state["outcome"] = "interrupted: resume with --resume " + rd.path.name
        rec.note("note", "interrupted by the user")
    except (RuntimeError, rpc.RuntimeDied, rpc.RpcError, TimeoutError, OSError) as e:
        status = "error"
        say(f"could not run: {e}")
        rec.note("note", f"error: {str(e)[:300]}")
        state["outcome"] = f"error: {str(e)[:200]}"
    finally:
        turns = info.get("turns", [])
        info.update(status=status, steps=flow.steps, models_used=list(dict.fromkeys(t["model"] for t in turns)),
                    finished=oj.now_iso(), blocked=state.get("blocked"), outcome=state.get("outcome"),
                    final_text=oss_cmd._scrub(" | ".join(f"{k}: {v.get('final', '')}" for k, v in state["stages"].items()
                                                         if v.get("final")))[:2000])
        oss_cmd.write_json(rd.run, info)
        try:
            flow.checkpoint()
        except OSError:
            pass
        rec.close()
        if rec.tr:
            rec.tr.close()
        rt.close()
        if shim and shim.poll() is None:
            shim.terminate()
            try:
                shim.wait(timeout=8)
            except Exception:                                    # noqa: BLE001
                shim.kill()
    result = oj.oss_judge(rd.path)
    assessment = oj.assess(rd.path, result)
    oj.write_report(rd.path, result=result, assessment=assessment)
    say(f"\nrun ended: {status} after {flow.steps} step(s); outcome: {state.get('outcome')}")
    for ln in st.stage_lines(state):
        say(ln)
    for i in result["checklist"]:
        say(f"  [{oj.MARK[i['status']]}] {i['id']}: {i['evidence'][:150]}")
    say(f"status: {assessment['status']}  ({'; '.join(assessment['reasons'])})\nreport: {rd.report}\n"
        f"decide: freeharness review oss approve|reject|changes --run {rd.path.name} --note ...")
    if status == "error":
        return 2
    if assessment["success"]:
        return 0
    return BLOCKED_EXIT if (state.get("blocked") or {}).get("kind") == "duplicate" else 1
