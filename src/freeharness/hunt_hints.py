"""Deterministic "where to hunt" hints for the OSS run, computed by the harness from a local clone.

Pure functions: git log and a file listing of the clone, plus optional GitHub data passed in as plain dicts
(never a network call here). Signals: churn (180 days), source modules no test references, directories of
recent bug-fix commits (365 days); directories with 2+ open PRs are dropped. Output: at most 5 (path, reason).
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import Counter

MAX_HINTS = 5
CHURN_DAYS = 180
FIX_DAYS = 365
SRC_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php",
           ".kt", ".swift", ".scala"}
SKIP_DIRS = {"test", "tests", "testing", "spec", "specs", "__tests__", "docs", "doc", "documentation", "examples", "example",
             "vendor", "vendored", "third_party", "thirdparty", "node_modules", "dist", "build", "generated", "gen",
             "migrations", "migration", "site-packages", ".github", "benchmarks", "fixtures", "_vendor"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum",
             "composer.lock", "Gemfile.lock", "uv.lock"}
FIX_RE = re.compile(r"\b(fix(es|ed)?|bug(s|fix)?|regression)\b", re.I)
GENERATED_RE = re.compile(r"(_pb2(_grpc)?\.py$|\.min\.\w+$|\.generated\.|\.gen\.|_generated\.|\.g\.dart$|\.d\.ts$)")


def _git(clone: str, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", clone, *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def is_source(path: str) -> bool:
    """A source file worth auditing: right extension, not under test/docs/vendor/generated/migration dirs, not a lockfile."""
    parts = path.split("/")
    name = parts[-1]
    if name in LOCKFILES or os.path.splitext(name)[1].lower() not in SRC_EXT:
        return False
    if any(p.lower() in SKIP_DIRS for p in parts[:-1]):
        return False
    low = name.lower()
    if low.startswith("test_") or low.endswith(("_test.py", "_test.go", ".test.js", ".test.ts", ".spec.js", ".spec.ts")):
        return False
    return low != "conftest.py" and not GENERATED_RE.search(name)


def _dir(path: str) -> str:
    return os.path.dirname(path) or "."


def log_files(clone: str, days: int, grep_fix: bool = False) -> list[list[str]]:
    """One list of changed files per commit in the last `days` days (optionally only fix|bug|regression subjects)."""
    out = _git(clone, "log", f"--since={days}.days.ago", "--name-only", "--no-merges", "--format=@@%s")
    commits: list[list[str]] = []
    subject_ok = True
    for line in out.splitlines():
        if line.startswith("@@"):
            subject_ok = bool(FIX_RE.search(line[2:])) if grep_fix else True
            commits.append([])
        elif line.strip() and commits and subject_ok:
            commits[-1].append(line.strip())
    return [c for c in commits if c]


def list_files(clone: str) -> list[str]:
    return sorted(f for f in _git(clone, "ls-files").splitlines() if f)


def churn(commits: list[list[str]]) -> Counter:
    c: Counter = Counter()
    for files in commits:
        for f in files:
            if is_source(f):
                c[f] += 1
    return c


def test_gaps(files: list[str], sizes: dict[str, int], test_texts: dict[str, str]) -> dict[str, int]:
    """Source modules whose name no test file mentions (path or text); value = size in bytes (weight)."""
    blob = "\n".join(list(test_texts) + list(test_texts.values())).lower()
    gaps = {}
    for f in files:
        if not is_source(f):
            continue
        stem = os.path.splitext(os.path.basename(f))[0].lower()
        if stem in ("__init__", "__main__", "index", "main", "mod") or len(stem) < 3:
            continue
        if not re.search(r"(?<![a-z0-9])" + re.escape(stem) + r"(?![a-z0-9])", blob):
            gaps[f] = sizes.get(f, 0)
    return gaps


def open_pr_dirs(open_prs: list[dict] | None) -> Counter:
    """Directories (and their parents) touched by open PRs; each PR counts once per directory.
    Each PR is a dict like {"title": str, "files": [paths]}."""
    c: Counter = Counter()
    for pr in open_prs or []:
        ds: set[str] = set()
        for f in pr.get("files") or []:
            d = _dir(str(f))
            while d and d != ".":
                ds.add(d)
                d = _dir(d)
        c.update(ds)
    return c


def _under(path: str, d: str) -> bool:
    return path == d or path.startswith(d.rstrip("/") + "/")


def compute_hints(commits_churn: list[list[str]], commits_fix: list[list[str]], files: list[str], sizes: dict[str, int],
                  test_texts: dict[str, str], open_prs: list[dict] | None = None, limit: int = MAX_HINTS) -> list[dict]:
    """Rank and return up to `limit` [{"path", "reason"}], deterministic (score desc, then path)."""
    cand: dict[str, tuple[float, list[str]]] = {}

    def add(path, score, reason):
        s, rs = cand.get(path, (0.0, []))
        cand[path] = (s + score, rs + [reason])

    for f, n in churn(commits_churn).items():
        if n >= 2:
            add(f, 3.0 * n, f"{n} commits in the last {CHURN_DAYS} days")
    fix_files = churn(commits_fix)
    for f, n in fix_files.items():
        add(f, 4.0 * n, f"touched by {n} bug-fix commit(s) in the last {FIX_DAYS} days")
    gaps = test_gaps(files, sizes, test_texts)
    if gaps:
        biggest = max(gaps.values()) or 1
        for f, sz in gaps.items():
            add(f, 2.0 * (sz / biggest), "no test references this module")
    busy = [d for d, n in open_pr_dirs(open_prs).items() if n >= 2]
    ranked = sorted((-s, p, rs) for p, (s, rs) in cand.items() if s > 0 and not any(_under(p, d) for d in busy))
    return [{"path": p, "reason": "; ".join(rs)} for _, p, rs in ranked[:limit]]


def hints_for_clone(clone: str, open_prs: list[dict] | None = None, limit: int = MAX_HINTS) -> list[dict]:
    """Gather the inputs from a local clone and compute the hints. [] when the clone is not a git repo."""
    files = list_files(clone)
    if not files:
        return []
    sizes = {}
    for f in files:
        try:
            sizes[f] = os.path.getsize(os.path.join(clone, f))
        except OSError:
            pass
    tests: dict[str, str] = {}
    for f in files:
        parts = f.lower().split("/")
        if any(p in ("tests", "test", "__tests__", "spec", "specs") for p in parts[:-1]) or parts[-1].startswith("test_") \
                or parts[-1].endswith(("_test.go", ".test.ts", ".test.js", ".spec.ts", ".spec.js")):
            try:
                with open(os.path.join(clone, f), encoding="utf-8", errors="ignore") as fh:
                    tests[f] = fh.read(200_000)
            except OSError:
                pass
    return compute_hints(log_files(clone, CHURN_DAYS), log_files(clone, FIX_DAYS, grep_fix=True), files, sizes, tests,
                         open_prs, limit)


def safe_hints(clone: str, open_prs: list[dict] | None = None) -> list[dict]:
    """Like hints_for_clone, but any failure yields no hints."""
    try:
        return hints_for_clone(clone, open_prs)
    except Exception:
        return []


def render_hints(hints: list[dict]) -> str:
    return "\n".join(f"- `{h['path']}`: {h['reason']}" for h in hints)


def prompt_line(hints: list[dict] | None) -> str:
    """The line the hunt prompt carries ('' when there are no hints)."""
    if not hints:
        return ""
    return ("Suggested starting points (pick one or ignore them): "
            + "; ".join(f"`{h['path']}` ({h['reason']})" for h in hints[:MAX_HINTS]) + ".")
