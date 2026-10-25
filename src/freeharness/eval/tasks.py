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


def _py(ws: Path, *args, timeout=30):
    return subprocess.run([sys.executable, *args], cwd=ws, capture_output=True, text=True, timeout=timeout)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_\-]+", text or "")


def _used(res, *names) -> bool:
    return any(c[0] in names for c in res.tool_calls)


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
    ok = "5050" in (res.final_text or "") and _used(res, "bash")
    return ok, "ran a command and reported 5050" if ok else f"bash used={_used(res, 'bash')}, answer {res.final_text[:60]!r}"


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

BY_ID = {t.id: t for t in TASKS}
