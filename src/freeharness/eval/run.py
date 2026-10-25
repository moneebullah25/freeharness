"""
Tool-loop eval: can this provider drive a real agent loop?

    freeharness eval --model gemini
    freeharness eval --model qwen --tasks fix-bug,edit-config --repeat 2
    freeharness eval --list

Each task runs in a fresh temp workspace through the full stack (dsh -> shim ->
web chat). Outcomes are judged on the workspace, not the model's words. Results
are saved as <state dir>/eval/<time>-<model>.json. This spends free-tier messages: ~40 for
a full pass on one provider, so a provider that is cooling down will fail tasks.

Verdict: a provider is "eligible" for the rotation when it passes at least
PASS_BAR of its runs AND never answers without using tools where tools were needed.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from .. import paths
from ..client.rpc import Runtime, ensure_shim, shim_health

from .tasks import BY_ID, TASKS, Task

PASS_BAR = 0.75
RATE_MARKERS = ("RATE_LIMIT", "cooling down", "rate_limit")
WAIT_CAP = 1500            # seconds to wait for a cooling provider before giving up


def materialize(task: Task) -> Path:
    ws = Path(tempfile.mkdtemp(prefix=f"eval-{task.id}-"))
    for rel, text in task.files.items():
        f = ws / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return ws


def grade(task: Task, ws: Path, res) -> tuple[bool, str]:
    """Pure: turn a finished turn + workspace into pass/fail with a reason."""
    if not res.ok:
        return False, f"turn ended {res.reason}"
    if task.needs_tools and not any(c[0] in task.needs_tools for c in res.tool_calls):
        return False, f"no tool use (needed one of {list(task.needs_tools)})"
    return task.check(ws, res)


def run_one(task: Task, model: str, keep: bool = False) -> dict:
    ws = materialize(task)
    rt = Runtime(ws, model=model)
    out = {"task": task.id, "model": model, "ok": False, "detail": "", "steps": 0,
           "tool_calls": 0, "tool_errors": 0, "seconds": 0.0, "error": None}
    t0 = time.monotonic()
    try:
        rt.start()
        res = rt.run_turn(f"eval-{task.id}-{int(t0)}", task.prompt, timeout=task.timeout)
        out.update(steps=res.steps, tool_calls=len(res.tool_calls), tool_errors=res.tool_errors,
                   tools=[c[0] for c in res.tool_calls], final=res.final_text[:300])
        out["ok"], out["detail"] = grade(task, ws, res)
    except Exception as e:                       # noqa: BLE001 -- one bad task must not end the run
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out["detail"] = out["error"]
    finally:
        rt.close()
        out["seconds"] = round(time.monotonic() - t0, 1)
        if not keep:
            shutil.rmtree(ws, ignore_errors=True)
        else:
            out["workspace"] = str(ws)
    return out


def is_rate_limited(r: dict) -> bool:
    return not r["ok"] and any(m in (r["detail"] or "") for m in RATE_MARKERS)


def wait_ready(model: str, cap: float = WAIT_CAP, sleep=time.sleep, log=print) -> bool:
    """Block until the shim has a provider for `model` that is not cooling down.
    A provider being rate-limited says nothing about whether it can do the task,
    so the eval waits instead of recording a failure."""
    deadline = time.monotonic() + cap
    while True:
        h = shim_health() or {"providers": {}}
        provs = h["providers"]
        wanted = [v for k, v in provs.items() if model in ("auto", k)]
        if not provs or any(v["ready"] for v in wanted):
            return True
        wait = min(min(v["cooldown_s"] for v in wanted) + 2, 60)
        if time.monotonic() + wait > deadline:
            return False
        log(f"    {model} is cooling down; waiting {wait}s")
        sleep(wait)


def run_with_patience(task: Task, model: str, keep: bool = False) -> dict:
    """run_one, but a rate-limited run is retried once after the cooldown, and one
    that still cannot run is marked `infra` (inconclusive), not failed."""
    wait_ready(model)
    r = run_one(task, model, keep)
    if is_rate_limited(r):
        r["retried"] = True
        if wait_ready(model):
            r = {**run_one(task, model, keep), "retried": True}
        if is_rate_limited(r):
            r["infra"] = True
    return r


def verdict(results: list[dict]) -> dict:
    infra = [r["task"] for r in results if r.get("infra")]
    judged = [r for r in results if not r.get("infra")]
    n = len(judged)
    passed = sum(r["ok"] for r in judged)
    no_tools = [r["task"] for r in judged if r["detail"].startswith("no tool use")]
    rate = passed / n if n else 0.0
    return {"runs": n, "passed": passed, "pass_rate": round(rate, 2),
            "answered_without_tools": no_tools, "inconclusive": infra,
            "eligible": bool(n) and rate >= PASS_BAR and not no_tools and not infra}


def table(results: list[dict]) -> str:
    rows = [f"{'task':20} {'result':6} {'steps':>5} {'tools':>5} {'err':>3} {'secs':>6}  detail"]
    for r in results:
        label = "INFRA" if r.get("infra") else ("PASS" if r["ok"] else "FAIL")
        rows.append(f"{r['task']:20} {label:6} {r['steps']:>5} "
                    f"{r['tool_calls']:>5} {r['tool_errors']:>3} {r['seconds']:>6.0f}  {r['detail'][:70]}")
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness eval", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="gemini", help="auto | gemini | perplexity | qwen | grok")
    ap.add_argument("--tasks", help="comma-separated task ids (default: all)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--keep", action="store_true", help="keep workspaces for inspection")
    ap.add_argument("--no-shim", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        for t in TASKS:
            print(f"{t.id:20} {t.prompt[:90]}")
        return 0
    ids = args.tasks.split(",") if args.tasks else [t.id for t in TASKS]
    unknown = [i for i in ids if i not in BY_ID]
    if unknown:
        sys.exit(f"unknown task(s) {unknown}; see --list")

    providers = "gemini,perplexity" if args.model == "auto" else args.model
    shim = None if args.no_shim else ensure_shim(providers)
    results = []
    try:
        for _rep in range(args.repeat):
            for tid in ids:
                print(f"[{len(results) + 1}/{len(ids) * args.repeat}] {tid} ...", flush=True)
                r = run_with_patience(BY_ID[tid], args.model, args.keep)
                label = "INFRA" if r.get("infra") else ("PASS" if r["ok"] else "FAIL")
                print(f"    {label}  {r['seconds']:.0f}s  {r['detail'][:90]}", flush=True)
                results.append(r)
        health = shim_health()
    finally:
        if shim and shim.poll() is None:
            shim.terminate()
            try:
                shim.wait(timeout=8)
            except Exception:                    # noqa: BLE001
                shim.kill()
    v = verdict(results)
    print("\n" + table(results))
    print(f"\n{args.model}: {v['passed']}/{v['runs']} passed ({v['pass_rate']:.0%}); "
          f"{'ELIGIBLE for the rotation' if v['eligible'] else 'NOT eligible'}"
          + (f"  [INCONCLUSIVE, could not run: {v['inconclusive']}]" if v['inconclusive'] else "")
          + (f"  [answered without tools: {v['answered_without_tools']}]" if v['answered_without_tools'] else ""))
    out = paths.private_dir(paths.state_dir() / "eval") / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.model}.json"
    out.write_text(json.dumps({"model": args.model, "verdict": v, "results": results,
                               "shim_health": health}, indent=1))
    print(f"saved {out}")
    return 0 if v["eligible"] else 1


if __name__ == "__main__":
    sys.exit(main())
