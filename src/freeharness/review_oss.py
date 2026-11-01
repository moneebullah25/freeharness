"""
`freeharness review oss show <run>`: everything a reviewer needs about an OSS run on ONE page of plain text.

Read-only. `collect(run_dir)` reads the run directory (config/state/run json, the report, the gate's drafts, the stage logs,
the call record, and git in the clone for the fix); `render(data)` is a pure function of that dict, so tests build
a fixture run directory (or just a dict) and compare text. A draft that belongs to a DROPPED candidate, or to a run that is
blocked or not finished, is marked `DO NOT APPROVE` at the top and on the draft itself.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .eval import oss_judge as oj
from .eval import oss_stage as st

BODY_MAX, OUT_MAX = 2500, 2000


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", str(s or "").lower()).strip()


def _title_matches(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b and (a == b or a in b or b in a))


def _clip(s: str, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n} more characters]"


# --------------------------------------------------------------------------- collecting

def draft_view(d: dict, dropped: list[dict]) -> dict:
    """One gate draft, parsed, with `stale` = the reasons a reviewer must not approve it ([] if none)."""
    parsed = oj.parse_issue_draft(d["text"]) if d["action"] == "gh_issue_create" else oj.parse_pr_draft(d["text"])
    hit = next((x for x in dropped if _title_matches(parsed.get("title", ""), x.get("title", ""))), None)
    stale = [f"belongs to DROPPED candidate {hit['no']} ({hit.get('decision', '?')})"] if hit else []
    return {"id": d["id"], "action": d["action"], "mode": d["mode"], "parsed": parsed, "stale": stale}


def stage_evidence(rd: oj.RunDir) -> list[dict]:
    """Per stage log file: the LAST acceptance event ({stage, items:[{id,status,evidence}]})."""
    out = []
    for f in sorted(rd.stages_dir.glob("*.jsonl")) if rd.stages_dir.is_dir() else []:
        last = None
        for ev in oj._jsonl(f):
            if ev.get("event") == "acceptance":
                last = ev
        if last:
            out.append({"stage": f.stem.split("-", 1)[-1], "items": last.get("items", [])})
    return out


def fix_info(clone: Path | None, branch: str, base: str, git=oj._git) -> dict:
    info = {"branch": branch, "base": base, "clone": str(clone or ""), "files": [], "diffstat": "", "commits": []}
    if not (clone and branch and base and clone.is_dir()):
        return info
    for ref in (f"origin/{base}", f"upstream/{base}", base):
        if git(clone, "rev-parse", "--verify", "-q", ref)[0] == 0:
            break
    else:
        return info
    code, out = git(clone, "log", "--format=%h %s", f"{ref}..{branch}")
    info["commits"] = out.splitlines() if code == 0 else []
    code, out = git(clone, "diff", "--stat", f"{ref}...{branch}")
    info["diffstat"] = out.strip() if code == 0 else ""
    code, out = git(clone, "diff", "--name-only", f"{ref}...{branch}")
    info["files"] = out.split() if code == 0 else []
    return info


def collect(run_dir, git=oj._git) -> dict:
    rd = oj.RunDir(run_dir)
    cfg, state, run = oj._json(rd.config), oj._json(rd.state), oj._json(rd.run)
    repo = state.get("repo") or cfg.get("repo", "?")
    n = int(state.get("candidate_no") or 1)
    cands = state.get("candidates") or []
    cand = cands[n - 1] if len(cands) >= n else {}
    dropped = state.get("dropped") or []
    rep = st.read_report(rd, repo) if "/" in repo else {"sections": [], "component": "", "path": None}
    sec = st.candidate_section(rep, n)
    calls = st.all_calls(rd)
    rec_dup = (state.get("stages") or {}).get("dupcheck", {})
    events = st.search_events(rd, repo)[int(rec_dup.get("search_offset", 0) or 0):]
    rows = st.result_rows(calls)
    overlap = st.overlapping(rows, sec["text"]) if sec else []
    drafts = [draft_view(d, dropped) for d in oj.read_drafts(rd) if d["repo"].lower() == repo.lower()]
    blocked = state.get("blocked")
    outcome = state.get("outcome") or run.get("outcome") or ""
    finished = outcome == "completed" and run.get("status", "completed") == "completed"
    clone = rd.workspace / state["clone"] if state.get("clone") else None
    pr = next((d["parsed"] for d in reversed(drafts) if d["action"] == "gh_pr_create" and not d["stale"]), {})
    branch = state.get("branch") or pr.get("branch", "")
    base = pr.get("base") or state.get("default_branch", "")
    reviews = oj._jsonl(rd.reviews)
    return {"run": rd.path.name, "repo": repo, "live": bool(state.get("live", cfg.get("live"))),
            "component": state.get("component") or cfg.get("component") or rep.get("component", ""),
            "status": run.get("status", ""), "outcome": outcome, "blocked": blocked, "finished": finished,
            "current": state.get("current", ""), "candidate_no": n, "candidate": cand, "section": sec,
            "dropped": dropped, "searches": [{"kind": e.get("kind"), "query": e.get("query")} for e in events],
            "decision": cand.get("decision"), "overlaps": rec_dup.get("overlaps", []), "rows": rows[:8],
            "overlap_rows": overlap, "drafts": drafts, "fix": fix_info(clone, branch, base, git),
            "evidence": stage_evidence(rd), "stages": st.stage_lines(state) if state else [],
            "reviews": [{"action": r.get("action"), "outcome": r.get("outcome"), "note": r.get("note", "")} for r in reviews]}


# --------------------------------------------------------------------------- rendering (pure)

def warnings(d: dict) -> list[str]:
    w = []
    if d.get("blocked"):
        b = d["blocked"]
        w.append(f"run is BLOCKED at {b.get('stage')}: {b.get('reason')}")
    elif not d.get("finished"):
        w.append(f"run is NOT FINISHED (status {d.get('status') or '?'}, outcome {d.get('outcome') or '?'})")
    for dr in d.get("drafts", []):
        for s in dr["stale"]:
            w.append(f"draft {dr['id']} ({dr['action']}) {s}")
    return w


def _h(title: str) -> list[str]:
    return ["", f"== {title} " + "=" * max(3, 70 - len(title))]


def render(d: dict) -> str:
    out = [f"OSS run {d['run']}  repo {d['repo']}  component {d.get('component') or '?'}  ({'LIVE' if d.get('live') else 'dry-run'})",
           f"status {d.get('status') or '?'} / outcome {d.get('outcome') or '?'} / stage {d.get('current') or '-'} / "
           f"candidate {d.get('candidate_no', 1)}"]
    w = warnings(d)
    if w:
        out += ["", "!" * 72, "DO NOT APPROVE: a draft below may be stale or incomplete"] + [f"  - {x}" for x in w] + ["!" * 72]
    else:
        out += ["", "run finished, no stale draft detected"]

    out += _h("Candidate")
    sec, cand = d.get("section"), d.get("candidate") or {}
    if sec:
        out.append(_clip(sec["text"].strip(), BODY_MAX))
    else:
        out.append(f"(no candidate section in the report; recorded title: {cand.get('title') or '-'})")

    out += _h("Repro")
    rp = cand.get("repro") or {}
    if rp.get("command"):
        out += [f"$ {rp['command']}", f"[exit {rp.get('exit')}; matched: {rp.get('signal')}; recorded at call #{rp.get('n')}]",
                _clip(rp.get("output", ""), OUT_MAX)]
    else:
        out.append("(no recorded repro run: nothing in calls.jsonl showed the failure)")

    out += _h("Duplicate check")
    for s in d.get("searches", []):
        out.append(f"  search [{s.get('kind')}] {s.get('query')}")
    if not d.get("searches"):
        out.append("  (no searches recorded for this candidate)")
    dec = d.get("decision")
    if dec:
        out.append(f"decision: {dec.get('kind')}" + (f" #{dec['number']}" if dec.get("number") else "")
                   + (f"  hit: {dec['hit']}" if dec.get("hit") else ""))
    else:
        out.append("decision: (none recorded)")
    shown = d.get("overlap_rows") or d.get("rows") or []
    if shown:
        out.append("hits compared" + (" (overlapping the candidate)" if d.get("overlap_rows") else "") + ":")
        out += [f"  #{r['number']} [{r['kind']} {r['state']}] {r['title']}"
                + (f"  shared: {', '.join(r['shared'])}" if r.get("shared") else "") for r in shown]
    if d.get("overlaps"):
        out.append("model was shown for a second look: " + "; ".join(d["overlaps"]))

    for action, label in (("gh_issue_create", "Issue draft"), ("gh_pr_create", "PR draft")):
        out += _h(label)
        ds = [x for x in d.get("drafts", []) if x["action"] == action]
        if not ds:
            out.append("(no draft)")
        for x in ds:
            p = x["parsed"]
            flag = ("  *** STALE: " + "; ".join(x["stale"]) + " ***") if x["stale"] else ""
            out.append(f"[{x['id']}, {x['mode']}]{flag}")
            out.append(f"title: {p.get('title', '')}")
            if action == "gh_pr_create":
                out.append(f"head {p.get('owner')}:{p.get('branch')} -> {p.get('base')}  closes #{p.get('issue')}  "
                           f"competing-PR check: {p.get('competing') or '?'}")
                if p.get("files"):
                    out.append("files: " + ", ".join(p["files"]))
            out += ["", _clip(p.get("body", ""), BODY_MAX)]

    out += _h("Fix")
    f = d.get("fix") or {}
    if f.get("branch"):
        out.append(f"branch {f['branch']} (base {f.get('base') or '?'}) in {f.get('clone') or '?'}")
        out.append("commits:" if f.get("commits") else "commits: (none found; clone or branch not readable)")
        out += [f"  {c}" for c in f.get("commits", [])]
        out.append("changed files: " + (", ".join(f["files"]) if f.get("files") else "-"))
        if f.get("diffstat"):
            out += ["diffstat:", f["diffstat"]]
    else:
        out.append("(no fix branch recorded yet)")

    out += _h("Stage evidence")
    for e in d.get("evidence", []):
        out.append(f"{e['stage']}:")
        out += [f"  [{i.get('status', '?').upper():4}] {i.get('id')}: {i.get('evidence')}" for i in e["items"]]
    if not d.get("evidence"):
        out.append("(no acceptance results recorded)")
    out += [""] + list(d.get("stages", []))

    out += _h("Dropped candidates")
    for x in d.get("dropped", []):
        out.append(f"  candidate {x.get('no')}: {x.get('title') or '?'}  -> {x.get('decision')}"
                   + (f" ({x['hit']})" if x.get("hit") else ""))
    if not d.get("dropped"):
        out.append("  none")
    if d.get("reviews"):
        out += _h("Human decisions so far")
        out += [f"  {r['action']}: {r['outcome']} {r['note']}".rstrip() for r in d["reviews"]]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness review oss show", description=__doc__)
    ap.add_argument("run", help="run id, a unique prefix, 'latest', or the run directory")
    a = ap.parse_args(argv)
    from . import oss_cmd
    try:
        d = oss_cmd.resolve_run(a.run)
    except oss_cmd.OssError as e:
        print(e, file=sys.stderr)
        return 2
    print(render(collect(d)), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
