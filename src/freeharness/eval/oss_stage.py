"""
Stage-level evaluation for the staged OSS workflow (`freeharness oss run`, src/freeharness/oss_flow.py).

Like eval/oss_judge.py, every function here reads only things the model cannot write: the gate's own files, the
tool-call record (calls.jsonl, from dsh's session events), and read-only git on the clone. The model's FINAL TEXT is
used in exactly one place, the `DECISION:` line of the duplicate check, which is the model's own statement and is
cross-checked against the search results the gate returned. Nothing here writes a draft, a bug description, a
search query, a fix or a PR text: the helpers only look at what the model already did.

Every `*_item` function returns an oss_judge-style item: {id, title, status: pass|fail|na, evidence}.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import oss_judge as oj
from .oss_judge import FAIL, PASS, _item

MIN_QUERIES = oj.MIN_QUERIES


# --------------------------------------------------------------------------- the call record

def all_calls(rd: oj.RunDir) -> list[dict]:
    return oj._jsonl(rd.calls)


def last_n(rd: oj.RunDir) -> int:
    return max((int(c.get("n") or 0) for c in all_calls(rd)), default=0)


def calls_since(rd: oj.RunDir, n0: int) -> list[dict]:
    return [c for c in all_calls(rd) if int(c.get("n") or 0) > n0]


def search_events(rd: oj.RunDir, repo: str) -> list[dict]:
    return [e for e in oj._jsonl(rd.evidence)
            if e.get("event") == "search" and str(e.get("repo", "")).lower() == repo.lower()]


def _ok_call(c: dict, tool: str) -> bool:
    return oj._is(c, tool) and not c.get("isError") and not str(c.get("text", "")).startswith("REFUSED")


# --------------------------------------------------------------------------- stage 1: setup

def find_clone(rd: oj.RunDir, repo: str) -> tuple[Path | None, dict]:
    """The clone `gh_clone` registered for this repo (the gate's registry, not the model's word)."""
    best = None
    for p, r in oj._json(rd.gh_state / "registry.json").items():
        if str(r.get("upstream", "")).lower() == repo.lower() and (Path(p) / ".git").exists():
            if best is None or r.get("time", 0) >= best[2].get("time", 0):
                best = (Path(p), p, r)
    return (best[0], best[2]) if best else (None, {})


def clone_item(rd: oj.RunDir, repo: str) -> dict:
    clone, reg = find_clone(rd, repo)
    title = "the repo is cloned into the workspace (gh_clone registered a clone with a .git directory)"
    if clone:
        return _item("clone_exists", title, PASS, f"{clone.name}: default branch {reg.get('default_branch', '?')}")
    return _item("clone_exists", title, FAIL, f"no clone of {repo} registered by gh_clone in this run")


# --------------------------------------------------------------------------- the report

CAND_RE = re.compile(r"^#{2,3}[ \t]+Candidate\b[ \t]*(\d+)?[ \t]*[:.\-]?[ \t]*(.*)$", re.M | re.I)
_HEAD12 = re.compile(r"(?m)^#{1,2}\s")


def field(text: str, name: str) -> str:
    """`- Expected: ...` / `**Expected:** ...` -> the rest of that line ('' if absent)."""
    m = re.search(rf"(?im)^[\s>*_-]*\**{name}\**\s*:\s*\**\s*(\S.*?)\s*$", text or "")
    return m.group(1).strip("* ") if m else ""


def read_report(rd: oj.RunDir, repo: str) -> dict:
    """{path, text, component, sections: [{no, title, text}]} of reports/<repo>_issues.md (empty if there is none)."""
    found = next((rd.workspace / "reports" / n for n in oj._name_variants(repo)
                  if (rd.workspace / "reports" / n).is_file()), None)
    out = {"path": found, "text": "", "component": "", "sections": []}
    if not found:
        return out
    text = found.read_text(encoding="utf-8", errors="replace")
    out["text"] = text
    m = re.search(r"^\s*Component reviewed:\s*(\S.*)$", text, re.M | re.I)
    out["component"] = m.group(1).strip() if m else ""
    heads = list(CAND_RE.finditer(text))
    for i, h in enumerate(heads):
        end = len(text)
        nxt = _HEAD12.search(text, h.end())
        if nxt:
            end = nxt.start()
        if i + 1 < len(heads):
            end = min(end, heads[i + 1].start())
        out["sections"].append({"no": int(h.group(1)) if h.group(1) else i + 1, "title": h.group(2).strip(),
                                "text": text[h.start():end]})
    return out


def candidate_section(report: dict, cand_no: int) -> dict | None:
    secs = report["sections"]
    return secs[cand_no - 1] if len(secs) >= cand_no else None


# --------------------------------------------------------------------------- stage 2: hunt, the reproduction

INFRA = re.compile(r"command not found|No such file or directory|ModuleNotFoundError|No module named|SyntaxError|"
                   r"IndentationError|Permission denied|not recognized as|Network is unreachable")
FAILSIG = re.compile(r"Traceback \(most recent call last\)|\b[A-Z]\w*(?:Error|Exception)\b|panic:|\bFAILED\b|\bFAIL\b|"
                     r"AssertionError|Segmentation fault|error\[E\d+\]|Unhandled")
_RUN = re.compile(r"^(python[0-9.]*|py|node|nodejs|deno|bun|ruby|php|perl|lua|julia|java|go run|cargo run|dotnet run|\./\S+)\b")
_EXIT = re.compile(r"\n?\[exit code: -?\d+\]\s*$")
_WORD = re.compile(r"[A-Za-z0-9_.+\-]+")
_STOP = {"the", "a", "an", "is", "are", "was", "of", "to", "in", "on", "and", "or", "but", "got", "returns", "return", "instead",
         "should", "not", "it", "its", "with", "for", "no", "yes", "none", "value", "result", "output", "raises", "raise"}


_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1(?P<rest>[^\n]*)\n.*?(?:^|\n)[ \t]*\2[ \t]*(?:\n|$)", re.S)


def strip_heredocs(command: str) -> str:
    """The command without here-document bodies: text written to a file is not a command that ran."""
    return _HEREDOC.sub(lambda m: "<<H" + m.group("rest") + "\n", command or "")


def is_run_command(command: str) -> bool:
    """Does the command RUN code (an interpreter, a script, a test runner)? `cat`, `grep`, `ls`, `echo` do not."""
    for seg, _ in oj._segments(strip_heredocs(command)):
        n = oj.normalize(seg.split("|")[0])
        if _RUN.search(n) or any(r.search(n) for r in oj._TEST):
            return True
    return False


def wrong_value(expected: str, actual: str, body: str) -> str:
    """A token of the model's stated ACTUAL (absent from its stated EXPECTED) that really appears in the run's output."""
    exp = {w.lower().strip(".-+") for w in _WORD.findall(expected or "")}
    for w in _WORD.findall(actual or ""):
        w = w.strip(".-+")
        if not w or w.lower() in exp or w.lower() in _STOP:
            continue
        if re.search(rf"(?<![\w.]){re.escape(w)}(?![\w.])", body):
            return w
    return ""


def failure_signal(output: str, expected: str = "", actual: str = "") -> tuple[str, str]:
    """(signal, problem). signal is why this output shows a failure ('' = it does not); problem names an environment
    failure (missing module, command not found...), which is a broken repro, not the bug."""
    body = _EXIT.sub("", output or "")
    if INFRA.search(body):
        return "", "the output shows an environment/syntax problem (" + INFRA.search(body).group(0) + "), not the bug"
    if FAILSIG.search(body):
        return "failure text in the output (" + FAILSIG.search(body).group(0)[:40] + ")", ""
    w = wrong_value(expected, actual, body)
    if w:
        return f"the output contains the stated actual value {w!r}", ""
    return "", ""


def _output_lines(output: str) -> list[str]:
    body = _EXIT.sub("", output or "")
    return [ln.strip() for ln in body.splitlines() if len(ln.strip()) >= 6]


def find_repro(stage_calls: list[dict], section: dict | None) -> tuple[list[dict], dict | None]:
    """The stage-2 acceptance items and the repro record: the LAST bash call of the stage that ran code and whose output
    shows a failure by deterministic rules (traceback/error/FAIL text, or the model's stated wrong value)."""
    text = (section or {}).get("text", "")
    expected, actual = field(text, "Expected"), field(text, "Actual")
    repro, problem, tried = None, "", 0
    for c in reversed([c for c in stage_calls if oj._is(c, "bash") and not c.get("isError")]):
        cmd = (c.get("args") or {}).get("command", "")
        if not is_run_command(cmd):
            continue
        tried += 1
        sig, prob = failure_signal(c.get("text", ""), expected, actual)
        problem = problem or prob
        if sig:
            repro = {"n": c.get("n"), "command": cmd[:1500], "output": _EXIT.sub("", c.get("text", ""))[:2000],
                     "exit": c.get("exit"), "signal": sig}
            break
    if repro:
        run = _item("repro_run", "a recorded run of code whose output shows the failure", PASS,
                    f"step {repro['n']}: {oj._clip(repro['command'], 70)} -> {repro['signal']}")
    else:
        why = (problem if problem else
               (f"{tried} run(s) of code in this stage, none whose output shows a traceback, an error, FAIL text or your stated "
                f"actual value" if tried else "no bash call in this stage ran code (python, node, a script, a test runner)"))
        run = _item("repro_run", "a recorded run of code whose output shows the failure", FAIL, why)
    ea = _item("expected_actual", "the candidate section states Expected and Actual",
               PASS if expected and actual else FAIL,
               f"Expected: {oj._clip(expected, 60)} | Actual: {oj._clip(actual, 60)}" if expected and actual else
               "the candidate section lacks " + " and ".join(n for n, v in (("an 'Expected:' line", expected),
                                                                              ("an 'Actual:' line", actual)) if not v))
    if repro and text:
        copied = [ln for ln in _output_lines(repro["output"]) if ln in text]
        rep = _item("repro_in_report", "the repro command's real output is pasted in the candidate section",
                    PASS if copied else FAIL,
                    f"line from the run found in the report: {oj._clip(copied[0], 70)}" if copied else
                    "no line of the repro's output appears in the candidate section: paste the real output there")
    else:
        rep = _item("repro_in_report", "the repro command's real output is pasted in the candidate section", FAIL,
                    "no recorded repro run yet" if not repro else "no candidate section in the report yet")
    return [run, ea, rep], repro


# --------------------------------------------------------------------------- stage 3: duplicate check

_ROW = re.compile(r"^#(\d+) \[(pr|issue) (\w+)\] (.*?) \(@([^)]*)\)\s*$", re.M)
DEC = re.compile(r"(?im)^[\s>*\-`_]*DECISION\s*:\s*[`*]*\s*(?:(proceed)|(no_other_candidate)|"
                 r"(duplicate)\s*:\s*#?(\d+)|(competing)\s*:\s*#?(\d+))\b")
_KW_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "when", "than", "then", "have", "has", "not", "but",
            "are", "was", "were", "should", "would", "could", "return", "returns", "returned", "raise", "raises", "raised",
            "instead", "error", "errors", "issue", "bug", "fails", "fail", "failure", "does", "using", "used", "after",
            "before", "also", "only", "where", "what", "wrong", "value", "values", "input", "output", "function", "method",
            "class", "code", "file", "line", "test", "tests", "expected", "actual", "repro", "reproduce", "call", "called",
            "calls", "gets", "get", "make", "makes", "add", "fix"}


def result_rows(calls: list[dict]) -> list[dict]:
    """Rows of the model's own gh_search results (as the gate returned them), newest search first, deduplicated."""
    rows, seen = [], set()
    for c in reversed([c for c in calls if oj._is(c, "gh_search") and not c.get("isError")]):
        for m in _ROW.finditer(str(c.get("text", ""))):
            key = (int(m.group(1)), m.group(2))
            if key not in seen:
                seen.add(key)
                rows.append({"number": int(m.group(1)), "kind": m.group(2), "state": m.group(3), "title": m.group(4),
                             "user": m.group(5)})
    return rows


def dup_search_item(rd: oj.RunDir, repo: str, offset: int) -> dict:
    ev = search_events(rd, repo)[offset:]
    queries = {str(e.get("query", "")).lower() for e in ev}
    kinds = {e.get("kind") for e in ev}
    ok = len(queries) >= MIN_QUERIES and {"issues", "prs"} <= kinds
    return _item("searches_for_candidate", f">={MIN_QUERIES} distinct queries for THIS candidate, issues AND prs",
                 PASS if ok else FAIL,
                 f"{len(queries)} distinct queries for this candidate ({sum(1 for e in ev if e.get('kind') == 'issues')} issues, "
                 f"{sum(1 for e in ev if e.get('kind') == 'prs')} prs)" + ("" if ok else f"; need {MIN_QUERIES}+ and both kinds"))


def parse_decision(texts: list[str], section_text: str = "") -> dict | None:
    """The model's last `DECISION:` line: {kind: proceed|duplicate|competing|no_other_candidate, number}."""
    for t in list(reversed(texts)) + [section_text]:
        ms = list(DEC.finditer(t or ""))
        if ms:
            m = ms[-1]
            kind = m.group(1) or m.group(2) or m.group(3) or m.group(5)
            num = m.group(4) or m.group(6)
            return {"kind": kind.lower(), "number": int(num) if num else None, "raw": m.group(0).strip()}
    return None


def decision_item(decision: dict | None, rows: list[dict]) -> dict:
    title = "an explicit decision line: DECISION: proceed | duplicate:#N | competing:#N"
    if not decision or decision["kind"] == "no_other_candidate":
        return _item("decision_line", title, FAIL, "no 'DECISION: proceed|duplicate:#N|competing:#N' line in your last messages")
    if decision["kind"] in ("duplicate", "competing"):
        hit = next((r for r in rows if r["number"] == decision["number"]), None)
        if not hit:
            return _item("decision_line", title, FAIL,
                         f"{decision['raw']}: #{decision['number']} is not a number any of your searches returned")
        if decision["kind"] == "competing" and hit["kind"] != "pr":
            return _item("decision_line", title, FAIL, f"competing:#{decision['number']} is an issue, not a pull request")
        return _item("decision_line", title, PASS, f"{decision['raw']} ({hit['kind']} {hit['state']}: {oj._clip(hit['title'], 60)})")
    return _item("decision_line", title, PASS, "DECISION: proceed")


def _idents(text: str) -> set[str]:
    out = {m.group(1).lower() for m in re.finditer(r"\b([A-Za-z_]\w*)\(\)", text)}
    out |= {w.lower() for w in re.findall(r"\b\w*_\w*\b", text) if len(w) >= 4}
    out |= {w.lower() for w in re.findall(r"\b[A-Za-z]+(?:Error|Exception)\b", text)}
    out |= {w.lower() for w in re.findall(r"\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z]+[A-Z]\w*\b", text)}
    out |= {Path(p).stem.lower() for p in re.findall(r"[\w./-]+\.\w{1,4}\b", text) if "/" in p or p.endswith(".py")}
    return {w for w in out if len(w) >= 3}


def candidate_text(section_text: str) -> str:
    """The parts of a candidate section that describe the bug (not the search table)."""
    head = section_text.splitlines()[0] if section_text else ""
    return "\n".join([head] + [f"{n}: {field(section_text, n)}" for n in ("Where", "What is wrong", "Actual", "Reproduce")])


def overlapping(rows: list[dict], section_text: str, top: int = 5) -> list[dict]:
    """Search hits whose title shares keywords with the candidate: two or more distinct words, or one identifier
    (a function name, an exception class, a file). Pure keyword overlap: it can only point, never decide."""
    cand = candidate_text(section_text)
    words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", cand)} - _KW_STOP
    idents = _idents(cand)
    scored = []
    for r in rows:
        tw = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", r["title"])} - _KW_STOP
        shared = words & tw
        score = len(shared) + 2 * len(idents & tw)
        if len(shared) >= 2 or idents & tw:
            scored.append((-score, 0 if r["kind"] == "pr" else 1, r["number"], {**r, "shared": sorted(shared | (idents & tw))}))
    return [s[3] for s in sorted(scored, key=lambda s: s[:3])[:top]]


# --------------------------------------------------------------------------- stage 5: fix

_DOC_EXT = {".md", ".rst", ".txt", ".adoc", ".png", ".jpg", ".svg", ".lock"}
_TESTFILE = re.compile(r"(^|/)(tests?|__tests__|specs?)/|(^|/)test_[^/]*$|_tests?\.\w+$|\.(test|spec)\.\w+$|(^|/)Test[A-Z]\w*\.\w+$")
_EDIT_TOOLS = {"write", "edit", "multiedit", "multi_edit", "apply_patch", "patch", "str_replace", "str_replace_editor"}
_WRITE_CMD = [re.compile(p) for p in (
    r"\bsed\s+(?:-\S+\s+)*-\S*i", r"\btee\b", r"\bpatch\b", r"\bgit\s+apply\b", r"\bperl\s+-\S*i",
    r"(?<![0-9&>])>>?\s*(?!/dev/null|&)\S", r"write_text|\.write\(|open\([^)]*['\"][wa]", r"\b(mv|cp|rm|touch|truncate|install)\s")]


def is_test_file(path: str) -> bool:
    return bool(_TESTFILE.search(path))


def is_source_file(path: str) -> bool:
    return not is_test_file(path) and Path(path).suffix.lower() not in _DOC_EXT


def modifies_files(call: dict) -> bool:
    if oj.tool_base(call.get("name", "")) in _EDIT_TOOLS:
        return True
    if oj._is(call, "bash"):
        cmd = (call.get("args") or {}).get("command", "")
        return any(r.search(cmd) for r in _WRITE_CMD)
    return False


def _test_call(call: dict) -> bool:
    cmd = (call.get("args") or {}).get("command", "")
    return any(any(r.search(oj.normalize(s)) for r in oj._TEST) for s, _ in oj._segments(cmd))


def added_test_names(ch: oj.Change) -> set[str]:
    names = set()
    for ln in ch.added_text().splitlines():
        m = re.search(r"\b(?:def|func|fn|it|test)\s*\(?\s*['\"]?(test_?\w*|Test\w*|\w*[Tt]est\w*)", ln)
        if m:
            names.add(m.group(1))
    return {n for n in names if len(n) >= 4}


def fix_items(rd: oj.RunDir, repo: str, stage_calls: list[dict]) -> list[dict]:
    """Stage 5 checks from the tool-call record and git: the branch is off upstream's default; a new test FAILED before
    the edit and PASSED after, both as recorded bash calls with exit codes (no `git stash` trick); the diff touches a
    source file AND a test file."""
    drafts = [d for d in oj.read_drafts(rd) if d["repo"].lower() == repo.lower()]
    ch = oj.Change(rd, repo, drafts)
    items = []
    bcalls = [c for c in stage_calls if _ok_call(c, "gh_branch")]
    branch_ok = bool(bcalls) and ch.ready()
    if branch_ok:
        rc, _ = oj._git(ch.clone, "merge-base", "--is-ancestor", f"refs/remotes/upstream/{ch.default}", f"refs/heads/{ch.branch}")
        branch_ok = rc == 0
    items.append(_item("branch_off_default", "gh_branch created the branch and it contains upstream's default branch tip",
                       PASS if branch_ok else FAIL,
                       f"{ch.branch} off upstream/{ch.default}" if branch_ok else
                       ("no successful gh_branch call in this stage" if not bcalls else
                        "the branch is missing or does not contain upstream's default branch tip")))
    files = ch.files() if ch.ready() else []
    tests = [f for f in files if is_test_file(f)]
    srcs = [f for f in files if is_source_file(f)]
    items.append(_item("diff_source_and_test", "the diff beyond upstream touches a source file AND a test file",
                       PASS if srcs and tests else FAIL,
                       f"source: {', '.join(srcs[:3])}; tests: {', '.join(tests[:3])}" if srcs and tests else
                       f"diff files: {', '.join(files[:6]) or 'none'}; " + ("no test file" if not tests else "no source file")))
    shell = [c for c in stage_calls if oj._is(c, "bash")]
    stash = [c for c in shell if re.search(r"\bgit\s+(?:-\S+\s+)*stash\b", (c.get("args") or {}).get("command", ""))]
    items.append(_item("no_stash", "the before/after proof does not use `git stash`", FAIL if stash else PASS,
                       f"step {stash[0].get('n')} used git stash: run the test before the fix instead" if stash else "no git stash"))
    first = bcalls[0].get("n", 0) if bcalls else 0
    runs = [c for c in shell if int(c.get("n") or 0) > first and _test_call(c)]
    tokens = {Path(f).name for f in tests} | {Path(f).stem for f in tests} | added_test_names(ch)
    fail_run = next((c for c in runs if c.get("exit") not in (0, None) and not INFRA.search(c.get("text", ""))
                     and any(t in c.get("text", "") for t in tokens)), None)
    if fail_run:
        items.append(_item("test_fails_before", "a recorded test run FAILED and its output names the new test", PASS,
                           f"step {fail_run.get('n')} exit {fail_run.get('exit')}: {oj._clip((fail_run.get('args') or {}).get('command', ''), 60)}"))
    else:
        items.append(_item("test_fails_before", "a recorded test run FAILED and its output names the new test", FAIL,
                           ("no test run after gh_branch exited non-zero" if not any(c.get("exit") not in (0, None) for c in runs)
                            else "no failing test run names the new test (file or test name) or the failure is an environment error")
                           + ": write the test first and run it once BEFORE the fix; it must fail"))
    after = None
    if fail_run:
        for p in runs:
            if p.get("exit") == 0 and oj.classify((p.get("args") or {}).get("command", ""), 0)["test"] and p["n"] > fail_run["n"]:
                if any(modifies_files(m) for m in shell if fail_run["n"] < m["n"] < p["n"]):
                    after = p
    items.append(_item("test_passes_after", "after an edit, a recorded test run exited 0",
                       PASS if after else FAIL,
                       f"step {after['n']} exit 0 after an edit since the failing run (step {fail_run['n']})" if after else
                       "no passing test run (exit 0) after both the failing run and an edit to files"))
    return items


# --------------------------------------------------------------------------- the stage table for oss report

def stage_lines(state: dict) -> list[str]:
    """Per-stage status: passed / repaired n times / blocked, repair turns used, steps."""
    rows = []
    for st in state.get("history", []) + [dict(s, id=k) for k, s in (state.get("stages") or {}).items()]:
        rows.append(st)
    order = state.get("order") or []
    rows.sort(key=lambda s: (order.index(s.get("id")) if s.get("id") in order else 99, s.get("candidate", 1)))
    out = []
    for s in rows:
        status, rep = s.get("status", "pending"), int(s.get("repairs", 0))
        if status == "passed":
            label = "passed" if not rep else f"passed after {rep} repair{'s' if rep != 1 else ''}"
        elif status == "blocked":
            label = f"blocked ({s.get('reason', '?')})"
        else:
            label = status
        cand = f" (candidate {s.get('candidate', 1)})" if state.get("candidate_no", 1) > 1 else ""
        extra = f"{int(s.get('turns', 0))} turn(s), {rep} repair turn(s), {int(s.get('steps', 0))} step(s)"
        if s.get("reviews"):
            extra += f", {s['reviews']} overlap review(s)"
        out.append(f"- {s.get('id')}{cand}: {label}  [{extra}]" + (f"  models: {', '.join(s['models'])}" if s.get("models") else ""))
        if status in ("blocked", "running") and s.get("unmet"):
            out += [f"    unmet: {u}" for u in s["unmet"][:5]]
    return out

