"""
Human review of eval runs, and the final assessment that combines all evidence.

    freeharness review <task> approve|reject|changes [--note TEXT] [--run RUN] [--file PATH]
    freeharness review <task> --show

Decisions are appended to <state dir>/eval/reviews.json. `final_assessment` never reports
success on a model's claim, the judge's opinion or a human's approval alone: the
deterministic workspace check must have passed first.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path

from .. import paths

DECISIONS = {"approve": "approved", "reject": "rejected", "changes": "changes_requested"}
JUDGE_MIN = 5            # advisory scores below this ask for a human look


def reviews_path() -> Path:
    return paths.private_dir(paths.state_dir() / "eval") / "reviews.json"


def load(path: Path | None = None) -> list[dict]:
    p = path or reviews_path()
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record(task: str, decision: str, note: str = "", run: str = "", reviewer: str = "",
           path: Path | None = None) -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    row = {"task": task, "run": run, "decision": DECISIONS[decision], "note": note,
           "reviewer": reviewer or getpass.getuser(), "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    p = path or reviews_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(load(p) + [row], indent=1))
    return row


def latest(task: str, run: str = "", path: Path | None = None) -> dict | None:
    rows = [r for r in load(path) if r["task"] == task and (not run or r.get("run") == run)]
    return rows[-1] if rows else None


def final_assessment(deterministic: dict | None, judge: dict | None = None, human: dict | None = None) -> dict:
    """status is one of: failed, unverified, rejected_by_human, needs_changes, needs_review, passed, verified.
    `deterministic` = {"ok": bool, "detail": str, "tool_calls": int}. Only a deterministic pass can lead to
    success; the judge and the human can only hold it back (judge) or confirm it (human)."""
    reasons = []
    if not deterministic or "ok" not in deterministic:
        return {"status": "unverified", "success": False,
                "reasons": ["no deterministic check result: model claims, judge scores and approvals are not evidence"]}
    if not deterministic["ok"]:
        reasons.append(f"deterministic check failed: {deterministic.get('detail', '')}")
        if human and human.get("decision") == "approved":
            reasons.append("human approval ignored: it cannot override a failed check")
        if judge and (judge.get("score") or 0) >= JUDGE_MIN:
            reasons.append("judge score ignored: advisory only")
        return {"status": "failed", "success": False, "reasons": reasons}
    if deterministic.get("tool_calls", 1) == 0:
        return {"status": "unverified", "success": False, "reasons": ["passed without any tool call"]}
    reasons.append("deterministic check passed")
    if human:
        d = human.get("decision")
        if d == "rejected":
            return {"status": "rejected_by_human", "success": False, "reasons": reasons + ["human rejected: " + human.get("note", "")]}
        if d == "changes_requested":
            return {"status": "needs_changes", "success": False, "reasons": reasons + ["human asked for changes: " + human.get("note", "")]}
        if d == "approved":
            return {"status": "verified", "success": True, "reasons": reasons + ["human approved"]}
    if judge and judge.get("score") is not None and judge["score"] < JUDGE_MIN:
        return {"status": "needs_review", "success": True,
                "reasons": reasons + [f"judge scored {judge['score']}/10 ({judge.get('verdict')}): {judge.get('reasoning', '')[:120]}"]}
    return {"status": "passed", "success": True, "reasons": reasons + (["judge " + str(judge.get("score")) + "/10 (advisory)"] if judge else [])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness review", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task")
    ap.add_argument("decision", nargs="?", choices=sorted(DECISIONS))
    ap.add_argument("--note", default="")
    ap.add_argument("--run", default="", help="result file or run label this review is about")
    ap.add_argument("--file", type=Path, help="reviews file (default: <state dir>/eval/reviews.json)")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args(argv)
    if a.show or not a.decision:
        rows = [r for r in load(a.file) if r["task"] == a.task]
        for r in rows:
            print(f"{r['time']}  {r['decision']:18} {r['reviewer']}  {r['note']}")
        if not rows:
            print(f"no reviews recorded for {a.task}")
        return 0
    row = record(a.task, a.decision, a.note, a.run, path=a.file)
    print(f"recorded: {a.task} {row['decision']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
