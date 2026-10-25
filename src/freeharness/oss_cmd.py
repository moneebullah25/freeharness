"""
`freeharness oss run <owner/repo> [--component NAME] [--models gemini,deepseek] [--live] [--max-steps N]`
`freeharness oss report <run-id> [--judge]`            `freeharness oss list`

The "OSS contribution" evaluation: give the agent one real repo and the task of finding ONE real bug in ONE
component, proving nobody filed it (open and closed, 4+ query variants), drafting the issue, and, after a human
approves, posting it; then fixing it minimally with tests on a branch off the upstream default, running the
project's own lint and tests, and opening the PR after a second human approval. All GitHub access goes through
the gh_* tools (mcp/ossgate.py = the GitHub gate), because the agent's sandbox has no network and no $HOME.

Every run gets a directory <state>/oss/<run-id>/ (prompt, config, clone workspace, the gate's evidence and drafts,
transcript, calls.jsonl, supervisor.jsonl, reviews.jsonl, run.json, report.md). The gate overlay is passed to
this run's runtime only (Runtime(overlay_files=[...])): nothing global is written or changed.

Dry-run is the default and needs no terminal: every gate is validated, drafts are saved, nothing is posted.
`--live` needs a terminal and an explicit y/N; each post is then shown to you for approve / reject / request changes.
Follow a run from another terminal with:  tail -f <run dir>/supervisor.jsonl
Judging is deterministic (eval/oss_judge.py); see docs/OSS_EVAL.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

from . import github_cmd, paths, sessions
from .client import rpc
from .client.approval import TerminalApprover
from .client.render import describe_call, extract_text, parse_args
from .eval import oss_judge as oj
from .mcp.ghgate import REPO_RE, TOKEN_PAT
from .transcript import Transcript

DEFAULT_MAX_STEPS = 150
DEFAULT_TURN_TIMEOUT = 3600.0


class OssError(RuntimeError):
    pass


suite_listing = oj.suite_listing


# --------------------------------------------------------------------------- the task

DEFAULT_REPAIR = 2


def build_prompt(repo: str, component: str, live: bool) -> str:
    name = repo.split("/")[1]
    comp = (f"Component to review: `{component}` (a directory or module of the repo). Review only that."
            if component else
            "Pick ONE small component yourself (one module or package) and name it on the report line `Component reviewed:`.")
    mode = (
        "Mode: LIVE. gh_fork, gh_issue_create, gh_push and gh_pr_create each show their draft to the human, who approves, "
        "rejects, or sends a note asking for changes. Approved = posted. A note = revise and call the tool again. "
        "Rejected = stop and report. Do not ask in chat before calling them: the tool asks the human. "
        "In the PR use the REAL issue number from the gh_issue_create result."
        if live else
        "Mode: DRY-RUN. Nothing is posted: each gh_* write validates every gate, saves a draft and returns what it would send. "
        "After gh_issue_create succeeds the tool gives you a PLACEHOLDER issue number: use that number in the branch name, "
        "in `issue=` and in `Fixes #N` for gh_pr_create. Do not ask in chat before calling the tools.")
    return f"""OSS contribution task for {repo}.

All GitHub access goes ONLY through the gh_* tools (gh_repo_info, gh_search, gh_get_file, gh_fork, gh_clone, gh_push, gh_issue_create, gh_pr_create). Your shell has no network, no HOME and no git credentials: never try `git push`, `gh`, `curl` or any other route. If a gh_* tool refuses, read the reason, fix exactly what it says and call again; if it cannot be fixed, stop and report the refusal text.

{mode}
{comp}

Steps, in order:
1. Load the `oss-contribution` skill with the skill tool and follow it. Also load the agency review skills you need (`agency-code-reviewer`, `agency-application-security-engineer`, `agency-backend-architect`, others if installed) and use them to review the component. If a skill is not installed, say so in the final summary and continue by hand.
2. gh_repo_info {repo}. Read CONTRIBUTING, the PR template and the issue template with gh_get_file. Find the project's OWN test command and lint/format command.
3. gh_fork {repo}, then gh_clone {repo} into the folder `{name}` (a path relative to your working directory).
4. Review the component. Find ONE real bug: a file:line and a concrete input that goes wrong. Reproduce it by running code in the clone.
5. Duplicate check: gh_search with at least 4 DIFFERENT queries (error text, function name, symptom in plain words, file path), for issues AND for prs. Open and closed are both searched. If an issue or PR for this bug exists, drop the candidate and pick another.
6. Write `reports/{name}_issues.md` in your working directory (NOT inside the clone), using the template in the oss-contribution skill (references/issues-file-template.md): the line `Component reviewed: <path>`, one `## Candidate N: title` section per candidate with your duplicate-search table, and the decision.
7. gh_issue_create with a title and a body (steps to reproduce, expected, actual, file:line).
8. In the clone: `git checkout -b fix/<issue-number>-<short-slug> upstream/<default branch>`. Make the smallest possible fix and add a test that fails before the fix and passes after.
9. Run the project's own lint/format command and its test suite in the clone with bash. Both must exit 0; fix what they report.
10. Commit with `git commit -m "<one line, imperative, at most 72 characters>"`: no body, no trailers, no Co-Authored-By or Signed-off-by, no mention of tools or AI, never --amend or rebase. The commit identity is already set by gh_clone.
11. Competing check: gh_search prs for the issue number and key words.
12. gh_push {{repo_dir: "{name}", branch: "<branch>"}} (plain push, never force), then gh_pr_create (base = the upstream default branch, title like the commit line, body from the repo's PR template with `Fixes #<issue number>`).
13. Finish with a short summary: component, issue number, PR, the exact lint and test commands you ran with their exit codes, and any gate refusal."""


# --------------------------------------------------------------------------- run directory

def oss_root() -> Path:
    return paths.state_dir() / "oss"


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def resolve_run(arg: str, root: Path | None = None) -> Path:
    root = root or oss_root()
    if os.sep in arg or arg.startswith("."):
        p = Path(arg).expanduser()
        if (p / "config.json").is_file():
            return p
    runs = sorted((d for d in root.iterdir() if (d / "config.json").is_file()), key=lambda d: d.name) if root.is_dir() else []
    if arg == "latest":
        if runs:
            return runs[-1]
        raise OssError(f"no oss runs yet in {root}")
    hit = [d for d in runs if d.name == arg] or [d for d in runs if d.name.startswith(arg)]
    if len(hit) != 1:
        raise OssError(f"{'no' if not hit else 'several'} oss run matches {arg!r} (see `freeharness oss list`)")
    return hit[0]


def overlay_row(rd: oj.RunDir, live: bool) -> str:
    """The gh MCP row for THIS run: github_cmd's row, our dry-run-capable gate module, and this run's folders."""
    row = github_cmd._row(live).replace("freeharness.mcp.ghgate", "freeharness.mcp.ossgate")
    pad = " " * 10
    lines = [f"{pad}FH_GH_WORKDIR: {json.dumps(str(rd.workspace))}",
             f"{pad}FH_GH_EVIDENCE: {json.dumps(str(rd.evidence))}",
             f"{pad}FH_GH_STATE: {json.dumps(str(rd.gh_state))}"]
    for name in ("FH_GH_API", "FH_GH_GIT_URL", "FH_GH_GIT_NAME", "FH_GH_GIT_EMAIL", "FH_GITHUB_SSH_HOST", "FH_GH_ENVFILE",
                 "FH_GH_CONFIRM_TIMEOUT"):          # names only: the values stay in the shell that started freeharness
        lines.append(f"{pad}{name}: !!js \"(process.env.{name} ?? '')\"")
    return row.rstrip("\n") + "\n" + "\n".join(lines) + "\n"


def prepare(repo: str, component: str, models: list[str], live: bool, max_steps: int, turn_timeout: float,
            root: Path | None = None) -> oj.RunDir:
    rd = oj.RunDir((root or oss_root()) / new_run_id())
    for d in (rd.path, rd.workspace, rd.gh_state, rd.overlay.parent, rd.transcripts):
        paths.private_dir(d)
    rd.prompt.write_text(build_prompt(repo, component, live), encoding="utf-8")
    rd.config.write_text(json.dumps({"repo": repo, "component": component, "models": models, "live": live,
                                     "max_steps": max_steps, "turn_timeout": turn_timeout, "created": oj.now_iso(),
                                     "task": "oss-contribution"}, indent=1), encoding="utf-8")
    rd.overlay.write_text(overlay_row(rd, live), encoding="utf-8")
    return rd


def write_json(p: Path, data: dict) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# --------------------------------------------------------------------------- recording

_EXIT = re.compile(r"\[exit code: (-?\d+)\]\s*$")
MAX_ARGS, MAX_TEXT = 20_000, 8_000


def _scrub(text: str) -> str:
    tok = os.environ.get("GITHUB_TOKEN", "")
    if len(tok) >= 4:
        text = text.replace(tok, "***")
    return TOKEN_PAT.sub("***", text)


def result_status(name: str, text: str, is_error: bool) -> tuple[str, int | None]:
    """('ok'|'refused'|'error'|'exit N'|'dry-run'|'changes', bash exit code or None)."""
    base = oj.tool_base(name)
    if base == "bash":
        m = _EXIT.search(text)
        code = int(m.group(1)) if m else (None if is_error else 0)
        return ("error" if is_error else ("ok" if code == 0 else f"exit {code}")), code
    if text.startswith("REFUSED"):
        return "refused", None
    if is_error:
        return "error", None
    if text.startswith("DRY-RUN"):
        return "dry-run", None
    if "requested changes" in text[:80]:
        return "changes", None
    return "ok", None


class Recorder:
    """Session events -> supervisor.jsonl (one compact line per tool step, flushed at once so `tail -f` works),
    calls.jsonl (the full record the judge reads) and the transcript. Pairs each tool call with its result."""

    def __init__(self, rd: oj.RunDir, transcript: Transcript | None = None, echo=None):
        self.rd, self.tr, self.echo = rd, transcript, echo
        self.pending: dict[str, dict] = {}
        self.n = 0
        self._lock = threading.Lock()
        self._sup = open(rd.supervisor, "a", encoding="utf-8", buffering=1)      # noqa: SIM115
        self._calls = open(rd.calls, "a", encoding="utf-8", buffering=1)         # noqa: SIM115

    def _line(self, f, rec: dict) -> None:
        f.write(_scrub(json.dumps(rec, ensure_ascii=False)) + "\n")
        f.flush()

    def note(self, kind: str, text: str, **kw) -> None:
        with self._lock:
            rec = {"t": oj.now_iso(), "kind": kind, "text": text, **kw}
            self._line(self._sup, rec)
        if self.echo:
            self.echo(f"[{kind}] {text}")

    def event(self, ev: dict) -> None:
        if self.tr:
            self.tr.event(ev)
        t, d = ev.get("type"), ev.get("data") or {}
        if t == "tool/call":
            self.pending[str(d.get("callId"))] = {"name": d.get("name", "?"), "args": parse_args(d.get("arguments")),
                                                  "step": d.get("step"), "t0": time.monotonic()}
        elif t == "tool/result":
            m = d.get("message") or {}
            cid = str(m.get("toolCallId") or (m.get("source") or {}).get("callId"))
            call = self.pending.pop(cid, None) or {"name": "?", "args": {}, "step": d.get("step"), "t0": time.monotonic()}
            text, err = extract_text(m.get("content")).strip(), bool(m.get("isError"))
            status, code = result_status(call["name"], text, err)
            with self._lock:
                self.n += 1
                shown = describe_call(call["name"], call["args"]).split("  ", 1)
                summary = shown[1] if len(shown) > 1 else ""
                self._line(self._sup, {"t": oj.now_iso(), "kind": "tool", "n": self.n, "step": call["step"],
                                       "tool": call["name"], "args": summary[:160], "status": status,
                                       "ms": int((time.monotonic() - call["t0"]) * 1000), "result": " ".join(text.split())[:160]})
                args_json = json.dumps(call["args"])
                self._line(self._calls, {"n": self.n, "step": call["step"], "callId": cid, "name": call["name"],
                                         "args": call["args"] if len(args_json) <= MAX_ARGS else {"_clipped": args_json[:MAX_ARGS]},
                                         "isError": err, "exit": code, "text": text[:MAX_TEXT], "time": oj.now_iso()})
            if self.echo:
                self.echo(f"#{self.n:<3} {status:8} {oj.tool_base(call['name'])}  {summary[:110]}")
        elif t == "assistant/message":
            txt = extract_text((d.get("message") or {}).get("content")).strip()
            if txt:
                self.note("text", " ".join(txt.split())[:300])
        elif t == "turn/end":
            self.note("turn_end", json.dumps(d.get("reason") or {}), reason=d.get("reason"))

    def close(self) -> None:
        for f in (self._sup, self._calls):
            try:
                f.close()
            except OSError:
                pass


def record_review(rd: oj.RunDir, request: dict, answer) -> None:
    """Append one human decision on a GitHub draft to reviews.jsonl (the judge's `human_review` evidence)."""
    outcome = answer.get("outcome") if isinstance(answer, dict) else answer
    row = {"time": oj.now_iso(), "action": request.get("action"), "repo": request.get("repo"), "draft": request.get("draft"),
           "outcome": "changes" if outcome == "changes" else ("confirmed" if outcome == "confirmed" else "denied"),
           "note": answer.get("note", "") if isinstance(answer, dict) else ""}
    with open(rd.reviews, "a", encoding="utf-8") as f:
        f.write(_scrub(json.dumps(row, ensure_ascii=False)) + "\n")


class OssApprover(TerminalApprover):
    """The terminal approver, plus a record of every human decision on a GitHub draft (reviews.jsonl)."""

    def __init__(self, console, rd: oj.RunDir, **kw):
        super().__init__(console, **kw)
        self.rd = rd

    def confirm(self, request):
        answer = super().confirm(request)
        record_review(self.rd, request, answer)
        return answer


# --------------------------------------------------------------------------- the run

def skills_missing() -> list[str]:
    root = paths.dsh_home() / "skills"
    return [s for s in ("oss-contribution", "agency-code-reviewer") if not (root / s / "SKILL.md").is_file()]


def run(repo: str, component: str = "", models=("auto",), live: bool = False, max_steps: int = DEFAULT_MAX_STEPS,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT, no_shim: bool = False, providers: str = "", approver_factory=None,
        console=None, root: Path | None = None, say=None, repair: int = DEFAULT_REPAIR) -> tuple[int, oj.RunDir | None]:
    """Run the evaluation. `approver_factory(rd)` -> an Approver (the CLI passes OssApprover when there is a terminal;
    tests pass a scripted human). Returns (exit code, run dir): 0 deterministic checks passed, 1 they did not or
    the run did not finish, 2 it could not run."""
    from rich.console import Console
    console = console or Console()
    say = say or (lambda s: console.print(s, markup=False, highlight=False))
    if not REPO_RE.match(repo or ""):
        say(f"repo must look like owner/name, got {repo!r}")
        return 2, None
    models = [m for m in models if m]
    if not models:
        models = ["auto"]
    if live and approver_factory is None:
        say("--live needs a terminal to show each draft to you; refusing")
        return 2, None
    if not github_cmd.token_available():
        say("GITHUB_TOKEN is not in the environment (or FH_GH_ENVFILE): every gh_* tool needs it, also in dry-run. Nothing was started.")
        return 2, None
    missing = skills_missing()
    if missing:
        msg = f"skills not installed in the dsh home: {', '.join(missing)} (run `freeharness agents install`)"
        if live:
            say(msg + "; refusing a live run")
            return 2, None
        say("warning: " + msg + "; the model will have to work without them")
    if not (os.environ.get("FH_GH_GIT_NAME") and os.environ.get("FH_GH_GIT_EMAIL")):
        say("note: FH_GH_GIT_NAME / FH_GH_GIT_EMAIL are not set; gh_clone then leaves the clone without a commit identity "
            "and the agent's `git commit` may fail")
    rd = prepare(repo, component, models, live, max_steps, turn_timeout, root)
    say(f"run {rd.path.name}  ({'LIVE' if live else 'dry-run: nothing is posted'})\n  dir: {rd.path}\n"
        f"  follow: tail -f {rd.supervisor}")
    code = execute(rd, repo, models, live, max_steps, turn_timeout, no_shim, providers, approver_factory, console, say,
                   repair)
    return code, rd


def _provider_list(models: list[str], override: str) -> str:
    if override:
        return override
    named = [m for m in models if m != "auto"]
    return "gemini,qwen" if ("auto" in models or not named) else ",".join(dict.fromkeys(named))


def repair_prompt(unmet: list[dict]) -> str:
    """Evidence for the model: what the harness's own checks found missing. No hints about HOW."""
    lines = "\n".join(f"- {i['id']}: {i['evidence'][:200]}" for i in unmet)
    return ("The harness checked your work against the task's requirements and these are NOT met yet. "
            "They come from tool-call records, not from your summary. Do the missing work with the tools "
            "(do not just say it is done), then stop.\n" + lines)


def execute(rd: oj.RunDir, repo: str, models: list[str], live: bool, max_steps: int, turn_timeout: float,
            no_shim: bool, providers: str, approver_factory, console, say, repair: int = 0) -> int:
    sid = "oss-" + rd.path.name
    info = {"run": rd.path.name, "session": sid, "status": "starting", "pid": os.getpid(), "started": oj.now_iso(),
            "models_used": [], "steps": 0}
    write_json(rd.run, info)
    approver = approver_factory(rd) if approver_factory else None
    rec = Recorder(rd, Transcript(sid, str(rd.workspace), directory=rd.transcripts), echo=lambda s: say("  " + s))
    rt = rpc.Runtime(rd.workspace, model=models[0], home=paths.dsh_home(), patch=rpc.PATCH, overlay_files=[rd.overlay],
                     approver=approver)
    shim, status, final = None, "error", ""
    steps, turns = 0, []
    repair_left = max(0, repair)
    try:
        if not no_shim:
            shim = rpc.ensure_shim(_provider_list(models, providers), sticky="auto")
        try:
            sessions.touch(sid, str(rd.workspace), rt.model)
            sessions.set_active(str(rd.workspace), sid)
        except (OSError, ValueError):
            pass
        rt.start()
        prompt = rd.prompt.read_text(encoding="utf-8")
        for i, model in enumerate(models):
            if i:
                rec.note("note", f"switching to model {model} and resuming the session")
                rt.restart(model=model)
                rt.resume(sid)
                prompt = ("Continue the task from where you stopped. Check your earlier tool results, do not repeat finished "
                          "steps, and finish the remaining ones.")
            left = (max_steps - steps) if max_steps else None
            if max_steps and left <= 0:
                status = "max_steps"
                break
            info.update(status="running", model=model)
            write_json(rd.run, info)
            rec.note("note", f"turn {i + 1} on model {model}" + (f" (step budget left: {left})" if left else ""))
            try:
                res = rt.run_turn(sid, prompt, on_event=rec.event, timeout=turn_timeout, max_steps=left)
            except (rpc.RuntimeDied, TimeoutError, rpc.RpcError) as e:
                rec.note("note", f"turn failed on {model}: {str(e)[:300]}")
                turns.append({"model": model, "error": str(e)[:300]})
                status = "failed"
                try:
                    rt.abandon(sid)
                except Exception as e2:                          # noqa: BLE001
                    rec.note("note", f"could not recover the runtime: {str(e2)[:200]}")
                continue
            steps += res.steps
            turns.append({"model": model, "ok": res.ok, "reason": res.reason, "steps": res.steps, "seconds": round(res.seconds, 1)})
            final = res.final_text or final
            if res.ok:
                status = "completed"
                # The model said it is finished: the judge, not the model, decides. Unmet requirements go back
                # to it as evidence for a bounded number of repair turns (the same model keeps the session).
                while repair_left > 0 and not (max_steps and steps >= max_steps):
                    unmet = [i for i in oj.oss_judge(rd.path)["checklist"] if i["status"] == "fail"]
                    if not unmet:
                        break
                    repair_left -= 1
                    rec.note("note", f"repair turn: {len(unmet)} requirement(s) unmet after the model said it was done")
                    feedback = repair_prompt(unmet)
                    try:
                        res = rt.run_turn(sid, feedback, on_event=rec.event, timeout=turn_timeout,
                                          max_steps=(max_steps - steps) if max_steps else None)
                    except (rpc.RuntimeDied, TimeoutError, rpc.RpcError) as e:
                        rec.note("note", f"repair turn failed: {str(e)[:300]}")
                        break
                    steps += res.steps
                    turns.append({"model": model, "repair": True, "ok": res.ok, "reason": res.reason,
                                  "steps": res.steps, "seconds": round(res.seconds, 1)})
                    final = res.final_text or final
                    if not res.ok:
                        break
                break
            status = "max_steps" if res.reason.get("kind") == "max_steps" else "failed"
            if status == "max_steps":
                break
    except KeyboardInterrupt:
        status = "interrupted"
        rec.note("note", "interrupted by the user")
    except (RuntimeError, rpc.RuntimeDied, rpc.RpcError, TimeoutError, OSError) as e:
        status = "error"
        say(f"could not run: {e}")
        rec.note("note", f"error: {str(e)[:300]}")
    finally:
        info.update(status=status, steps=steps, turns=turns, models_used=[t["model"] for t in turns], finished=oj.now_iso(),
                    final_text=_scrub(final)[:2000])
        write_json(rd.run, info)
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
    say(f"\nrun ended: {status} after {steps} step(s)")
    for i in result["checklist"]:
        say(f"  [{oj.MARK[i['status']]}] {i['id']}: {i['evidence'][:150]}")
    say(f"status: {assessment['status']}  ({'; '.join(assessment['reasons'])})\nreport: {rd.report}\n"
        f"decide: freeharness review oss approve|reject|changes --run {rd.path.name} --note ...")
    return 2 if status == "error" else (0 if assessment["success"] else 1)


# --------------------------------------------------------------------------- CLI

LIVE_NOTE = """\
--live: the agent's gh_* tools may post to GitHub as YOUR account (fork, issue, push to your fork, pull request).
Each one is shown to you in this terminal first (approve / reject / request changes); nothing is sent without your
explicit approval. Rules enforced in code stay on: no force pushes, no default-branch pushes, 4+ search queries before
an issue, competing-PR check, no attribution markers, no artifact files."""


def cmd_run(a, console) -> int:
    from .client.cli import MODELS
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    bad = [m for m in models if m not in MODELS]
    if bad:
        print(f"unknown model(s) {bad}; choose from {', '.join(MODELS)}", file=sys.stderr)
        return 2
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    if a.live:
        print(LIVE_NOTE)
        if not tty:
            print("refusing --live without a terminal to ask you", file=sys.stderr)
            return 2
        if input(f"run LIVE against {a.repo} and let approved drafts be posted? [y/N] ").strip().lower() not in ("y", "yes"):
            print("not started")
            return 2

    def factory(rd):
        return OssApprover(console, rd) if tty else None
    code, _ = run(a.repo, a.component or "", models, a.live, a.max_steps, a.turn_timeout, a.no_shim, a.providers,
                  factory if tty else None, console, repair=a.repair)
    return code


def cmd_report(a) -> int:
    try:
        d = resolve_run(a.run)
    except OssError as e:
        print(e, file=sys.stderr)
        return 2
    result = oj.oss_judge(d)
    if a.judge:
        v = oj.attach_advisory(d, result)
        print(f"advisory judge: {v.get('score')} {v.get('verdict')} (cannot change the checklist)")
    assessment = oj.assess(d, result)
    print(oj.render_report(d, result, assessment))
    oj.write_report(d, result=result, assessment=assessment)
    print(f"written: {oj.RunDir(d).report}")
    return 0 if assessment["success"] else 1


def cmd_list() -> int:
    root = oss_root()
    runs = sorted((d for d in root.iterdir() if (d / "config.json").is_file()), reverse=True) if root.is_dir() else []
    if not runs:
        print(f"no oss runs yet ({root})")
    for d in runs:
        cfg, run_ = oj._json(d / "config.json"), oj._json(d / "run.json")
        print(f"{d.name}  {cfg.get('repo', '?'):30} {'LIVE' if cfg.get('live') else 'dry '}  {run_.get('status', '?'):10} "
              f"{run_.get('steps', '?')} step(s)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness oss", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run the evaluation on one repo (dry-run unless --live)")
    p.add_argument("repo", help="owner/name")
    p.add_argument("--component", help="directory or module to review (default: the model picks one)")
    p.add_argument("--models", default="auto", help="comma-separated, tried in order: a turn that fails moves the SAME "
                   "session to the next model (default: %(default)s)")
    p.add_argument("--live", action="store_true", help="really post what you approve (needs a terminal and y/N)")
    p.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, metavar="N",
                   help="abandon the run after N model steps in total; 0 = no limit (default: %(default)s)")
    p.add_argument("--turn-timeout", type=float, default=DEFAULT_TURN_TIMEOUT, metavar="S")
    p.add_argument("--repair", type=int, default=DEFAULT_REPAIR, metavar="N",
                   help="when the model says it is done but the checklist still fails, send the unmet items back as "
                        "evidence for up to N more turns (default: %(default)s; 0 = judge once, no feedback)")
    p.add_argument("--providers", default="", help="providers for a shim this command starts (default: from --models)")
    p.add_argument("--no-shim", action="store_true", help="do not start or check the shim")
    q = sub.add_parser("report", help="timeline, checklist and assessment of a run; writes report.md")
    q.add_argument("run", help="run id, a unique prefix, 'latest', or the run directory")
    q.add_argument("--judge", action="store_true", help="also ask the advisory LLM judge (one message; cannot change the result)")
    sub.add_parser("list", help="past runs")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        from rich.console import Console
        return cmd_run(a, Console())
    if a.cmd == "report":
        return cmd_report(a)
    return cmd_list()


if __name__ == "__main__":
    sys.exit(main())
