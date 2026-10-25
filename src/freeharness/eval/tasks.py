"""
Tool-loop eval tasks. Each one is a tiny coding job whose outcome is checked on
the WORKSPACE (file contents, exit codes, the numeric answer) -- never on the
model's claim that it succeeded. That matters: a provider that confidently says
"done" without doing the work must fail.

A Task has:
  files   initial workspace contents
  prompt  what the agent is asked
  check   (ws, TurnResult) -> (ok, detail)
  solve   (ws, TurnResult-factory) -> None: writes a known-correct end state, so the
          tests can prove the checker rejects the initial state and accepts a solution
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable


@dataclasses.dataclass
class Task:
    id: str
    prompt: str
    files: dict
    check: Callable
    solve: Callable
    needs_tools: tuple = ()          # at least ONE of these must appear in the tool trace:
                                     # answering without touching the workspace is not doing the task
    timeout: float = 600.0
    setup: Callable | None = None    # (ws) -> [dsh patch yml, ...]; extra runtime config (e.g. an MCP server).
                                     # The patches' parent dir is temporary and removed after the run.
    followups: tuple = ()            # further prompts sent in the SAME session after the first turn
    resume: bool = False             # restart the runtime and session/resume between turns


def tool_matches(name: str, wanted: str) -> bool:
    """A tool counts if it is `wanted` or an MCP-prefixed form of it (e.g. mcp__vault__vault_search)."""
    return bool(name) and (name == wanted or name.endswith("_" + wanted))


def _py(ws: Path, *args, timeout=30):
    return subprocess.run([sys.executable, *args], cwd=ws, capture_output=True, text=True, timeout=timeout)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_\-]+", text or "")


def _used(res, *names) -> bool:
    return any(tool_matches(c[0], n) for c in res.tool_calls for n in names)


# 1 -------------------------------------------------------------- read-answer
def _check_read(ws, res):
    ok = "ORCHID-7731" in (res.final_text or "")
    return ok, "answer contains the code" if ok else f"answer was {res.final_text[:80]!r}"


# 2 --------------------------------------------------------------- write-file
def _check_write(ws, res):
    f = ws / "greeting.txt"
    if not f.exists():
        return False, "greeting.txt was not created"
    got = f.read_text()
    return got.strip() == "Hello, free agent!" and got.strip().count("\n") == 0, f"content {got!r}"


# 3 ------------------------------------------------------------------ fix-bug
BUGGY = "def add(a, b):\n    return a - b\n"
TEST_ADD = ('from add import add\nassert add(2, 3) == 5, f"add(2,3) gave {add(2,3)}"\n'
            'assert add(-1, 1) == 0\nprint("all tests passed")\n')


def _check_fix(ws, res):
    if (ws / "test_add.py").read_text() != TEST_ADD:
        return False, "the test file was modified (cheating)"
    r = _py(ws, "test_add.py")
    return r.returncode == 0 and "all tests passed" in r.stdout, (r.stderr or r.stdout).strip()[-120:]


# 4 -------------------------------------------------------------- edit-config
def _check_config(ws, res):
    try:
        cfg = json.loads((ws / "config.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        return False, f"config.json unreadable: {e}"
    ok = cfg == {"debug": False, "port": 9090, "name": "svc", "tags": ["a", "b"]}
    return ok, f"config is {cfg}"


# 5 -------------------------------------------------------------- count-todos
TODO_FILES = {
    "src/a.py": "# TODO: tidy\nx = 1\n",
    "src/b.py": "x = 2\n",
    "src/c.py": "# TODO one\n# TODO two\n",           # two TODO lines, still ONE file
    "src/d.py": "x = 4\n",
    "src/e.py": "def f():\n    pass  # TODO\n",
}


def _check_todos(ws, res):
    nums = [w for w in _words(res.final_text) if w.isdigit()]
    return nums == ["3"], f"numbers in answer: {nums}"


# 6 ----------------------------------------------------------- rename-function
RENAME_FILES = {
    "mod.py": "def calc_total(xs):\n    return sum(xs)\n",
    "a.py": "from mod import calc_total\n\ndef run_a():\n    return calc_total([1, 2, 3])\n",
    "b.py": "import mod\n\ndef run_b():\n    return mod.calc_total([10, 20])\n",
    "main.py": "from a import run_a\nfrom b import run_b\nprint(run_a(), run_b())\n",
}


def _check_rename(ws, res):
    left = [p.name for p in ws.glob("*.py") if "calc_total" in p.read_text()]
    if left:
        return False, f"calc_total still in {left}"
    if "compute_total" not in (ws / "mod.py").read_text():
        return False, "mod.py has no compute_total"
    r = _py(ws, "main.py")
    return r.returncode == 0 and r.stdout.strip() == "6 30", (r.stderr or r.stdout).strip()[-120:]


# 7 ----------------------------------------------------------- run-and-report
def _check_run(ws, res):
    # 5050 is guessable, so also require that a bash call really computed the sum
    ran = any(c[0] == "bash" and "sum" in str((c[1] or {}).get("command", "")) and
              "range" in str((c[1] or {}).get("command", "")) for c in res.tool_calls)
    ok = "5050" in (res.final_text or "") and ran
    return ok, "ran the command and reported 5050" if ok else f"ran sum command={ran}, answer {res.final_text[:60]!r}"


# 8 ------------------------------------------------------ write-and-run-test
def _check_test(ws, res):
    t = ws / "test_strings.py"
    if not t.exists():
        return False, "test_strings.py was not created"
    src = t.read_text()
    if len(re.findall(r"^\s*assert\b", src, re.M)) < 2:
        return False, "fewer than 2 assert statements"
    if "shout" not in src:
        return False, "the test does not exercise shout()"
    r = _py(ws, "test_strings.py")
    return r.returncode == 0, (r.stderr or "test passes").strip()[-120:]


def _solve_write(ws):
    (ws / "greeting.txt").write_text("Hello, free agent!\n")


TASKS: list[Task] = [
    Task("read-answer", "Read notes.txt and tell me the launch code.",
         {"notes.txt": "The launch code is ORCHID-7731.\nIt expires tomorrow.\n"},
         _check_read, lambda ws: None, needs_tools=("read", "bash", "grep")),
    Task("write-file", "Create a file greeting.txt in the working directory containing exactly one line: Hello, free agent!",
         {}, _check_write, _solve_write, needs_tools=("write", "bash", "edit")),
    Task("fix-bug", "Run python3 test_add.py. If it fails, fix the bug in add.py (do not change the test), then run the test again and tell me the result.",
         {"add.py": BUGGY, "test_add.py": TEST_ADD}, _check_fix,
         lambda ws: (ws / "add.py").write_text("def add(a, b):\n    return a + b\n"),
         needs_tools=("edit", "write", "bash")),
    Task("edit-config", "In config.json change the port to 9090. Change nothing else.",
         {"config.json": json.dumps({"debug": False, "port": 8000, "name": "svc", "tags": ["a", "b"]}, indent=2) + "\n"},
         _check_config,
         lambda ws: (ws / "config.json").write_text(json.dumps(
             {"debug": False, "port": 9090, "name": "svc", "tags": ["a", "b"]}, indent=2) + "\n"),
         needs_tools=("edit", "write", "bash")),
    Task("count-todos", "How many files under src/ contain the text TODO? Answer with just the number.",
         TODO_FILES, _check_todos, lambda ws: None, needs_tools=("grep", "bash", "read", "glob")),
    Task("rename-function", "Rename the function calc_total to compute_total everywhere in this project, then run python3 main.py to confirm it still works.",
         RENAME_FILES, _check_rename,
         lambda ws: [(ws / n).write_text(t.replace("calc_total", "compute_total")) for n, t in RENAME_FILES.items()],
         needs_tools=("edit", "write", "bash")),
    Task("run-and-report", "Run the command  python3 -c 'print(sum(range(1,101)))'  and tell me exactly what it printed.",
         {}, _check_run, lambda ws: None, needs_tools=("bash",)),
    Task("write-and-run-test", "strings.py has a function shout(). Write test_strings.py with at least two plain assert checks of shout() (no pytest), run it, and report whether it passed.",
         {"strings.py": 'def shout(s):\n    return s.upper() + "!"\n'}, _check_test,
         lambda ws: (ws / "test_strings.py").write_text(
             'from strings import shout\nassert shout("hi") == "HI!"\nassert shout("a b") == "A B!"\nprint("ok")\n'),
         needs_tools=("write", "bash")),
]

CORE_TASKS = TASKS            # the original 8: the default suite, what the rotation bar is measured on

# ======================================================== extended suite (--suite full)
# 9 ------------------------------------------------------------- explore-dir
_LOGS = {"alpha/one.log": 3, "alpha/two.log": 4, "beta/a.log": 2, "beta/b.log": 5, "beta/c.log": 1,
         "beta/d.log": 3, "gamma/x.log": 6, "gamma/deep/y.log": 2}
EXPLORE_FILES = {f"data/{k}": "".join(f"line {i}\n" for i in range(n)) for k, n in _LOGS.items()}
EXPLORE_FILES["data/beta/readme.txt"] = "not a log\n" * 9
EXPLORE_FILES["data/alpha/old.log.bak"] = "x\n" * 7          # a trap: not a .log file


def _check_explore(ws, res):
    counts: dict = {}
    for k in _LOGS:
        top = k.split("/")[0]
        counts[top] = counts.get(top, 0) + 1
    best = max(counts, key=counts.get)
    total = sum(_LOGS.values())
    words = _words(res.final_text)
    nums = [w for w in words if w.isdigit()]
    ok = nums == [str(total)] and best in words and not any(d in words for d in counts if d != best)
    return ok, f"expected '{best} {total}', answer words {words[:6]}"


# 10 ----------------------------------------------------------- refactor-3files
REFACTOR_FILES = {
    "pkg/__init__.py": "",
    "pkg/rows.py": 'def parse_row(line):\n    """parse_row splits one CSV line."""\n    return line.split(",")\n\n\n'
                   "def parse_rows(text):\n    return [parse_row(l) for l in text.splitlines()]\n",
    "pkg/report.py": "from pkg.rows import parse_row\n\n\ndef first_cell(line):\n    return parse_row(line)[0]\n",
    "pkg/cli.py": "import pkg.rows\n\n\ndef width(line):\n    return len(pkg.rows.parse_row(line))\n",
    "check.py": "from pkg.rows import parse_record, parse_rows\nfrom pkg.report import first_cell\nfrom pkg.cli import width\n"
                'assert parse_record("a,b") == ["a", "b"]\nassert parse_rows("a,b\\nc,d") == [["a", "b"], ["c", "d"]]\n'
                'assert first_cell("x,y,z") == "x" and width("x,y,z") == 3\nprint("refactor ok")\n',
}
_PARSE_ROW = re.compile(r"\bparse_row\b")


def _check_refactor(ws, res):
    if (ws / "check.py").read_text() != REFACTOR_FILES["check.py"]:
        return False, "check.py was modified (cheating)"
    left = [p.name for p in (ws / "pkg").glob("*.py") if _PARSE_ROW.search(p.read_text())]
    if left:
        return False, f"parse_row still in {left}"
    if "def parse_rows" not in (ws / "pkg/rows.py").read_text():
        return False, "parse_rows (a different function) must be kept"
    r = _py(ws, "check.py")
    return r.returncode == 0 and "refactor ok" in r.stdout, (r.stderr or r.stdout).strip()[-120:]


def _solve_refactor(ws):
    for n in ("pkg/rows.py", "pkg/report.py", "pkg/cli.py"):
        (ws / n).write_text(_PARSE_ROW.sub("parse_record", (ws / n).read_text()))


# 11 --------------------------------------------------------------- skill-use
SKILL_TAG = "rel-2.4.0-HALCYON-5521"
SKILL_FILES = {
    ".dsh/skills/release-tag/SKILL.md":
        "---\nname: release-tag\ndescription: How release tags are named in this project. Use whenever a release tag must be written.\n---\n"
        "# Release tags\n\nA release tag is `rel-<version>-HALCYON-5521`, where <version> is the version number.\n"
        "Write the tag alone on one line.\n",
}


def _check_skill(ws, res):
    if not _used(res, "skill"):
        return False, "the skill tool was never called"
    f = ws / "tag.txt"
    if not f.exists():
        return False, "tag.txt was not created"
    got = f.read_text().strip()
    return got == SKILL_TAG, f"tag.txt is {got!r}"


# 12 ---------------------------------------------------------------- vault-mcp
VAULT_NOTES = {
    "ops-runbook.md": "# Ops runbook\n\nThe pager rotation code is MERIDIAN-3318. Rotation changes weekly.\nSee [[escalation]].\n",
    "escalation.md": "# Escalation\n\nPage the on-call, then the manager. Unrelated code: DECOY-0001.\n",
    "lunch.md": "# Lunch\n\nThe canteen menu changes daily.\n",
}


def _setup_vault(ws):
    import tempfile
    from ..vault_cmd import _row
    root = Path(tempfile.mkdtemp(prefix="eval-vault-"))
    vault = root / "vault"
    for rel, text in VAULT_NOTES.items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(text)
    patch = root / "vault.yml"
    patch.write_text(_row(vault))
    return [patch]


def _check_vault(ws, res):
    if not _used(res, "vault_search", "vault_read"):
        return False, "no vault_search/vault_read call"
    f = ws / "answer.txt"
    if not f.exists():
        return False, "answer.txt was not created"
    got = f.read_text().strip()
    return got == "MERIDIAN-3318", f"answer.txt is {got!r}"


# 13 ---------------------------------------------------------- resume-continue
def _check_resume(ws, res):
    a, b = ws / "step1.txt", ws / "step2.txt"
    if not a.exists() or a.read_text().strip() != "ready":
        return False, "step1.txt missing or not 'ready'"
    if not b.exists():
        return False, "step2.txt was not created"
    got = b.read_text().strip()
    return got == "QUARTZ-9082", f"step2.txt is {got!r} (the token from turn 1)"


def _solve_resume(ws):
    (ws / "step1.txt").write_text("ready\n")
    (ws / "step2.txt").write_text("QUARTZ-9082\n")


_W = ("write", "bash", "edit")
EXTRA_TASKS: list[Task] = [
    Task("explore-dir", "Using shell tools (find, grep, wc, tree), answer: which top-level directory under data/ holds the most .log files "
         "(searching recursively; only names ending exactly in .log count), and how many lines do ALL those .log files contain in total? "
         "Answer exactly as: <dirname> <total>",
         EXPLORE_FILES, _check_explore, lambda ws: None, needs_tools=("bash", "grep", "glob")),
    Task("refactor-3files", "Rename the function parse_row to parse_record everywhere under pkg/ (definition, imports, calls and docstring), "
         "but keep parse_rows as it is. Do not edit check.py. Then run python3 check.py.",
         REFACTOR_FILES, _check_refactor, _solve_refactor, needs_tools=_W),
    Task("skill-use", "This project has a skill about release tags. Load and follow it to write the tag for version 2.4.0 into tag.txt.",
         SKILL_FILES, _check_skill, lambda ws: (ws / "tag.txt").write_text(SKILL_TAG + "\n"), needs_tools=("skill",)),
    Task("vault-mcp", "Use the vault tools (vault_search, then vault_read) to find the note about the pager rotation, "
         "and write its rotation code, alone, into answer.txt.",
         {}, _check_vault, lambda ws: (ws / "answer.txt").write_text("MERIDIAN-3318\n"),
         needs_tools=("vault_search", "vault_read"), setup=_setup_vault),
    Task("resume-continue", "Remember this: the deploy token is QUARTZ-9082. Now create step1.txt containing exactly the word: ready",
         {}, _check_resume, _solve_resume, needs_tools=_W,
         followups=("Continue: create step2.txt containing only the deploy token I told you earlier.",), resume=True),
]
FULL_TASKS = CORE_TASKS + EXTRA_TASKS
SUITES = {"core": CORE_TASKS, "full": FULL_TASKS}

BY_ID = {t.id: t for t in FULL_TASKS}
