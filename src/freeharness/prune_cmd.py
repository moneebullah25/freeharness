"""
`freeharness prune --older-than DAYS [--dry-run] [--keep-reports] [--all] [--force] [--root DIR]`: retention for oss run dirs.

Order of deletion inside one run: workspace/ (clones, venvs) first, then transcript/, calls.jsonl, supervisor.jsonl, stages/;
report.md, state.json, run.json, health.json and the rest stay unless --all (which removes the whole run directory).
--keep-reports deletes ONLY workspace/ (keeps every record). A run whose process is alive, or that holds a draft nobody
decided on, is skipped unless --force. Without a flag the command only PRINTS the plan; `--no-dry-run` applies it.
Nothing outside the root is ever touched: every target is checked to live below it and a symlink is unlinked, not followed.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .eval import oss_judge as oj
from .review_oss import draft_view

DAY = 86400.0
TIER1 = ("workspace",)
TIER2 = ("transcript", "calls.jsonl", "supervisor.jsonl", "stages")


@dataclass
class Item:
    path: Path
    bytes: int


@dataclass
class RunPlan:
    run: str
    items: list[Item] = field(default_factory=list)
    skipped: str = ""

    @property
    def bytes(self) -> int:
        return sum(i.bytes for i in self.items)


def size_of(p: Path) -> int:
    if p.is_symlink():
        return 0
    if p.is_file():
        return p.stat().st_size
    total = 0
    for dp, _dn, fns in os.walk(p):
        for fn in fns:
            fp = Path(dp) / fn
            try:
                if not fp.is_symlink():
                    total += fp.stat().st_size
            except OSError:
                pass
    return total


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def is_running(rd: oj.RunDir, alive=pid_alive) -> bool:
    run = oj._json(rd.run)
    pids = [run.get("pid"), run.get("runtime_pid")]
    return not run.get("finished") and any(p and alive(p) for p in pids)


def pending_drafts(rd: oj.RunDir) -> list[str]:
    """Gate drafts with no recorded human decision (reviews.jsonl names the draft id). A draft of a dropped candidate is
    stale, so it does not hold the run back."""
    dropped = oj._json(rd.state).get("dropped") or []
    decided = {str(r.get("draft")) for r in oj._jsonl(rd.reviews)
               if r.get("outcome") in ("confirmed", "denied", "changes", "approved", "rejected")}
    return [d["id"] for d in oj.read_drafts(rd) if d["id"] not in decided and not draft_view(d, dropped)["stale"]]


def run_age_days(rd: oj.RunDir, now: float) -> float:
    ts = [p.stat().st_mtime for p in (rd.path, rd.run, rd.state, rd.report) if p.exists()]
    return (now - max(ts)) / DAY if ts else 0.0


def plan_prune(root: Path, older_than: float, now: float | None = None, keep_reports: bool = False, all_: bool = False,
               force: bool = False, alive=pid_alive) -> list[RunPlan]:
    """Pure planning (reads, never deletes): one RunPlan per run dir under `root` that is old enough."""
    now = time.time() if now is None else now
    plans = []
    root = Path(root)
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_symlink() or not d.is_dir() or not (d / "config.json").is_file():
            continue
        rd = oj.RunDir(d)
        if run_age_days(rd, now) < older_than:
            continue
        plan = RunPlan(d.name)
        if not force:
            if is_running(rd, alive):
                plan.skipped = "running (pid alive)"
            else:
                pend = pending_drafts(rd)
                if pend:
                    plan.skipped = f"undecided draft(s) awaiting review: {', '.join(pend)}"
        if not plan.skipped:
            names = [p.name for p in d.iterdir()] if all_ else list(TIER1) + ([] if keep_reports else list(TIER2))
            known = TIER1 + TIER2
            for n in [n for n in known if n in names] + [n for n in names if n not in known]:
                p = d / n
                if p.exists() or p.is_symlink():
                    plan.items.append(Item(p, size_of(p)))
        plans.append(plan)
    return plans


def _below(p: Path, root: Path) -> bool:
    """p's parent directory (symlinks resolved) is `root` or inside it; p itself is never followed."""
    parent, base = os.path.realpath(p.parent), os.path.realpath(root)
    return parent == base or parent.startswith(base + os.sep)


def apply_plan(plans: list[RunPlan], root: Path) -> int:
    """Delete what the plan lists, refusing anything not below `root`. Returns bytes freed."""
    root = Path(root)
    freed = 0
    for plan in plans:
        for it in plan.items:
            p = it.path
            if os.path.realpath(p) == os.path.realpath(root) or not _below(p, root):
                continue
            if p.is_symlink() or p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            freed += it.bytes
    return freed


def _fmt(n: float) -> str:
    for u in ("B", "KiB", "MiB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} GiB"


def render_plan(plans: list[RunPlan], applied: bool) -> str:
    lines, total = [], 0
    for p in plans:
        if p.skipped:
            lines.append(f"{p.run}: SKIPPED, {p.skipped}")
            continue
        total += p.bytes
        lines.append(f"{p.run}: {'deleted' if applied else 'would delete'} {_fmt(p.bytes)}")
        lines += [f"    {i.path.name}  {_fmt(i.bytes)}" for i in p.items]
    if not plans:
        lines.append("no run is old enough")
    lines.append(f"{'freed' if applied else 'would free'} {_fmt(total)} in total"
                 + ("" if applied else "  (dry run; add --no-dry-run to delete)"))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness prune", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--older-than", type=float, required=True, metavar="DAYS", help="only runs untouched for this many days")
    ap.add_argument("--dry-run", action="store_true", help="print the plan only (the default)")
    ap.add_argument("--no-dry-run", action="store_true", help="really delete")
    ap.add_argument("--keep-reports", action="store_true", help="delete only workspace/ (keep transcripts, calls, stage logs)")
    ap.add_argument("--all", action="store_true", help="remove the whole run directory, report and state included")
    ap.add_argument("--force", action="store_true", help="also prune runs that are running or hold an undecided draft")
    ap.add_argument("--root", type=Path, help="oss runs directory (default: <state dir>/oss)")
    a = ap.parse_args(argv)
    if a.older_than < 0:
        ap.error("--older-than must be >= 0")
    from . import oss_cmd
    root = a.root or oss_cmd.oss_root()
    plans = plan_prune(root, a.older_than, keep_reports=a.keep_reports, all_=a.all, force=a.force)
    do = a.no_dry_run and not a.dry_run
    if do:
        apply_plan(plans, root)
    print(render_plan(plans, do))
    return 0


if __name__ == "__main__":
    sys.exit(main())
