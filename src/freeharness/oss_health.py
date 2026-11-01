"""Run health of a staged OSS run, derived only from files the run already wrote.

Sources: supervisor.jsonl (tool calls, stage notes), stages/*.jsonl (turns and acceptance rounds), state.json.
Nothing here logs anything; `freeharness oss report` prints `render()` and writes `health.json`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ERROR_CLASSES = ("sandbox refused", "module not found", "file-not-read", "no-such-file", "other")

_RULES = (                       # first match wins
    ("file-not-read", re.compile(r"has not been read", re.I)),
    ("module not found", re.compile(r"ModuleNotFoundError|No module named", re.I)),
    ("sandbox refused", re.compile(r"sandbox|bwrap|unconfined", re.I)),
    ("no-such-file", re.compile(r"cannot read .*not found|No such file or directory|cannot access", re.I)),
)
_WAIT = re.compile(r"cooling down; waiting (\d+)s")


def classify_error(result) -> str:
    text = str(result or "")
    for name, rx in _RULES:
        if rx.search(text):
            return name
    return "other"


def _jsonl(p: Path) -> list[dict]:
    out = []
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
    except OSError:
        pass
    return out


def tool_errors(events: list[dict]) -> dict[str, int]:
    errs = dict.fromkeys(ERROR_CLASSES, 0)
    for e in events:
        if e.get("kind") == "tool" and e.get("status") != "ok":
            errs[classify_error(e.get("result"))] += 1
    return errs


def supervisor_counts(events: list[dict]) -> dict:
    """Model failovers and cooldown waits, from the stage lines of supervisor.jsonl."""
    failovers = waits = wait_s = 0
    for e in events:
        if e.get("kind") != "stage":
            continue
        t = str(e.get("text", ""))
        if ": switching to model " in t:
            failovers += 1
        m = _WAIT.search(t)
        if m:
            waits += 1
            wait_s += int(m.group(1))
    return {"failovers": failovers, "cooldown_waits": waits, "cooldown_seconds": wait_s}


def stage_health(rows: list[dict]) -> dict:
    """One stage log (turn and acceptance rows, in order) -> steps, repair turns, wasted steps, unmet checks per round.

    Wasted steps: steps taken after the first acceptance round that reached the stage's best number of passing checks."""
    steps = repairs = 0
    rounds, best_pass, best_at = [], 0, 0
    for r in rows:
        if r.get("event") == "turn":
            steps += int(r.get("steps") or 0)
            repairs += r.get("kind") == "repair"
        elif r.get("event") == "acceptance":
            items = [i for i in (r.get("items") or []) if isinstance(i, dict)]
            unmet = [i.get("id") for i in items if i.get("status") != "pass"]
            n_pass = len(items) - len(unmet)
            rounds.append({"after_steps": steps, "unmet": unmet})
            if n_pass > best_pass:
                best_pass, best_at = n_pass, steps
    return {"steps": steps, "repair_turns": repairs, "wasted_steps": steps - best_at if best_pass else steps,
            "acceptance_rounds": rounds}


def compute(run_dir) -> dict:
    d = Path(run_dir)
    sup = _jsonl(d / "supervisor.jsonl")
    stages: dict[str, dict] = {}
    for f in sorted((d / "stages").glob("*.jsonl")) if (d / "stages").is_dir() else []:
        stages[re.sub(r"^\d+-", "", f.stem)] = stage_health(_jsonl(f))
    errs = tool_errors(sup)
    out = {
        "run": d.name,
        "tool_calls": sum(1 for e in sup if e.get("kind") == "tool"),
        "tool_errors": errs,
        "tool_errors_total": sum(errs.values()),
        "stages": stages,
        "steps_total": sum(s["steps"] for s in stages.values()),
        "repair_turns": sum(s["repair_turns"] for s in stages.values()),
        "wasted_steps": sum(s["wasted_steps"] for s in stages.values()),
    }
    out.update(supervisor_counts(sup))
    return out


def render(h: dict, markdown: bool = False) -> str:
    out = [("## " if markdown else "") + "Run health", ""]
    out.append(f"- tool calls: {h['tool_calls']}   errors: {h['tool_errors_total']}   repair turns: {h['repair_turns']}   "
               f"wasted steps: {h['wasted_steps']}")
    out.append("- tool errors by class: " + ", ".join(f"{k} {v}" for k, v in h["tool_errors"].items()))
    out.append(f"- model failovers: {h['failovers']}   cooldown waits: {h['cooldown_waits']} ({h['cooldown_seconds']}s)")
    for name, s in h["stages"].items():
        out.append(f"- {name}: {s['steps']} step(s), {s['repair_turns']} repair turn(s), {s['wasted_steps']} wasted")
        for i, r in enumerate(s["acceptance_rounds"], 1):
            out.append(f"      round {i} (after {r['after_steps']} steps): "
                       + (f"unmet {', '.join(map(str, r['unmet']))}" if r["unmet"] else "all met"))
    return "\n".join(out)


def write(run_dir) -> dict:
    h = compute(run_dir)
    (Path(run_dir) / "health.json").write_text(json.dumps(h, indent=2) + "\n", encoding="utf-8")
    return h
