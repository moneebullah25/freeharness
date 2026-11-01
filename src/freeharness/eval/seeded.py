"""
Planted-bug audit: does the harness, with its skills, find bugs we know are there?

`evals/seeded_audit/project` is a small orders service with 14 planted bugs (security, API, logic) and 3 correct-looking decoys. The
answer key (`evals/seeded_audit/key.json`) is NEVER copied into the model's workspace: only `project/` is. The model audits the
code and writes `findings.md`; `grade()` matches each finding block against the key.

Three variants measure what the skills add:
  A  only agency-code-reviewer (how the hunt prompt used to work)
  B  the full set of lenses (code reviewer, appsec, penetration tester, API tester, identity, secrets, performance)
  C  no skills at all
  D  one lens per pass: the A prompt, then one follow-up turn per remaining lens ("load X, review again through it, add NEW findings"),
     graded after every pass so the gain of each lens shows. The follow-ups name only the skill: no hint of what to look for.

    python -m freeharness.eval.seeded --model gemini --variants A,B,C
    python -m freeharness.eval.seeded --grade path/to/findings.md        (grade an existing findings file, no model)

Matching rule: a planted bug is FOUND when a finding block names its function (the `Function:` line, else anywhere in the block) and the
block matches the bug's pattern. A decoy is a FALSE POSITIVE when a block names the decoy's function and matches its pattern.
Blocks that match nothing are listed as `extra` for a human to read: they can be real.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "evals" / "seeded_audit"
PROJECT = ROOT / "project"
KEY = ROOT / "key.json"

COMMON = """This directory is a small orders service (Python, sqlite). Audit it for real bugs and security flaws. Do NOT change any source file.
You may run its tests with `python3 -m pytest -q`. Write `findings.md` in this directory, one block per finding, in exactly this layout (each block starts with a `### n. title` line):

### 1. <short title>
File: <path>
Function: <function or variable name>
Why: <what is wrong and how it goes wrong>
Input: <a concrete input or call that shows it>

List every real problem you find (up to 20), most certain first. Say DONE when the file is written."""

LENSES = ("agency-code-reviewer", "agency-application-security-engineer", "agency-penetration-tester", "agency-api-tester",
          "agency-identity-access-engineer", "agency-secrets-credential-hygiene-engineer", "agency-performance-benchmarker")

METHOD = """Method (follow it in order, do not skip steps):
1. Inventory: list every function and module-level setting in every source file (`grep -n "def \\|^[A-Z_]* =" -r .`). Read each file completely.
2. Walk: for EACH function write yourself a note: who controls its inputs, what it returns, what it touches (database, files, secrets, network), and what happens on bad input (empty, negative, zero, huge, wrong type, first/last element).
3. Check each function against every one of these classes: untrusted input reaching a sink (SQL, file path, shell, response body); secrets and configuration; hashing, signing and comparisons; units, types, rounding, integer-vs-float and operator precedence; boundaries and off-by-one; shared or mutable state; who may call it and whose data it returns; HTTP status codes and error bodies; validation of every field.
4. Prove: for each suspect, write a tiny `python3 -c` or script that imports the code and shows the wrong behaviour. A suspect you could not prove is still listed, marked `unconfirmed`. A function you checked and found correct is simply not listed.
"""

LEDGER = """Method (follow it in order):
1. Inventory every function and module-level setting in every source file and read each file completely.
2. Write `ledger.md` FIRST: one line per function or setting, `name | file | verdict | reason`, where verdict is `bug`, `suspect` or `ok`. Before giving a verdict, ask of that item each of these: can an outsider control its input, and where does it go (SQL, file path, response)? Is anything secret or hard-coded? Are hashes, signatures and comparisons sound? Are units, types, rounding and operator precedence right? What happens at the edges (empty, zero, negative, first/last, off-by-one)? Is state shared between calls? Who is allowed to call it, and whose data does it return? Are HTTP status codes and error bodies correct and safe? Is every input field validated? Give `ok` only with a reason that names what you checked.
3. For each `bug` or `suspect`, prove it with a tiny python snippet that imports the code (a `suspect` you cannot prove still goes in the findings, marked `unconfirmed`). Weak practice and missing hardening count: a finding does not need a working exploit to be real.
4. Then write `findings.md` with one block per `bug` and `suspect` line of the ledger.
"""

VARIANTS = {
    "A": "Before reading any code, load the skill `agency-code-reviewer` with the skill tool and review through it.\n\n" + COMMON,
    "B": ("Before reading any code, load each of these skills with the skill tool and review the code through every one of them: "
          + ", ".join(f"`{s}`" for s in LENSES) + ".\n\n" + COMMON),
    "C": "Do not use the skill tool.\n\n" + COMMON,
    "D": "Before reading any code, load the skill `agency-code-reviewer` with the skill tool and review through it.\n\n" + COMMON,
    "E": "Before reading any code, load the skill `agency-code-reviewer` with the skill tool and review through it.\n\n" + METHOD + "\n" + COMMON,
    "G": "Before reading any code, load the skill `agency-code-reviewer` with the skill tool and review through it.\n\n" + LEDGER + "\n" + COMMON,
    "H": "Before reading any code, load the skill `agency-code-reviewer` with the skill tool and review through it.\n\n" + LEDGER + "\n" + COMMON,
    "F": "Before reading any code, load the skill `agency-code-reviewer` with the skill tool and review through it.\n\n" + METHOD + "\n" + COMMON,
}

FOLLOWUP = """Second look, through a different lens. Load the skill `{skill}` with the skill tool and review the SAME code again, strictly through that
skill: look for what it treats as important. Append only NEW findings to `findings.md` (same layout, keep numbering on from the last block;
do not repeat or edit earlier ones). If this lens finds nothing new, append nothing. Say DONE."""

LEDGER_FOLLOWUP = """Second look, through a different lens. Load the skill `{skill}` with the skill tool. Go back over EVERY row of `ledger.md`, including the `ok` rows, and re-judge each strictly through that skill: what does it treat as important that you did not ask the first time? Change a verdict in the ledger when it now looks wrong, and append only NEW findings to `findings.md` (same layout, numbering on from the last block; do not repeat earlier ones). If nothing is new, append nothing. Say DONE."""

FOLLOWUPS = {"H": [LEDGER_FOLLOWUP.format(skill=s) for s in LENSES[1:]], "D": [FOLLOWUP.format(skill=s) for s in LENSES[1:]], "F": [FOLLOWUP.format(skill=s) for s in LENSES[1:]]}


# ---------------------------------------------------------------------------------------------- grading (pure)

def use_fixture(name: str) -> None:
    """Point the runner at evals/<name> (`seeded_audit` is the default; `seeded_holdout` has different code and bugs of the same classes)."""
    global ROOT, PROJECT, KEY
    ROOT = Path(__file__).resolve().parents[3] / "evals" / name
    PROJECT, KEY = ROOT / "project", ROOT / "key.json"


def load_key(path: Path | None = None) -> dict:
    return json.loads(Path(path or KEY).read_text(encoding="utf-8"))


_BLOCK_START = re.compile(r"(?m)^(?:#{2,4}[ \t]+|\d+[.)][ \t]+|[-*][ \t]+\*\*)")


def split_blocks(text: str) -> list[str]:
    """The finding blocks of a findings file: each starts at a `###` heading (or a numbered / bold bullet)."""
    starts = [m.start() for m in _BLOCK_START.finditer(text or "")]
    if not starts:
        return [text.strip()] if (text or "").strip() else []
    starts.append(len(text))
    return [text[a:b].strip() for a, b in zip(starts, starts[1:], strict=False) if text[a:b].strip()]


def block_function(block: str) -> str:
    m = re.search(r"(?im)^\s*[-*]?\s*\**function\**\s*:\s*(.+)$", block)
    return re.sub(r"[`*()\s]", "", m.group(1)).lower() if m else ""


def names(block: str, func: str) -> bool:
    """Does the block name `func`? Through its Function: line when it has one, else anywhere in its text."""
    f = block_function(block)
    want = func.lower()
    if f:
        return want in f or f in want
    return re.search(rf"(?<![\w]){re.escape(want)}(?![\w])", block.lower()) is not None


def grade(findings: str, key: dict) -> dict:
    blocks = split_blocks(findings)
    found, matched = {}, set()
    for bug in key["bugs"]:
        pat = re.compile(bug["pattern"], re.I)
        for i, b in enumerate(blocks):
            if names(b, bug["function"]) and pat.search(b):
                found[bug["id"]] = i
                matched.add(i)
                break
    false_pos = []
    for decoy in key.get("decoys", []):
        pat = re.compile(decoy["pattern"], re.I)
        for i, b in enumerate(blocks):
            if i in matched:
                continue
            if names(b, decoy["function"]) and pat.search(b) and decoy["id"] not in false_pos:
                false_pos.append(decoy["id"])
                matched.add(i)
    by_class: dict[str, list[int]] = {}
    for bug in key["bugs"]:
        c = by_class.setdefault(bug["class"], [0, 0])
        c[1] += 1
        c[0] += 1 if bug["id"] in found else 0
    extra = [blocks[i].splitlines()[0][:100] for i in range(len(blocks)) if i not in matched]
    return {"found": sorted(found), "missed": [b["id"] for b in key["bugs"] if b["id"] not in found],
            "recall": round(len(found) / len(key["bugs"]), 3), "by_class": {k: f"{v[0]}/{v[1]}" for k, v in by_class.items()},
            "total": len(key["bugs"]), "false_positives": false_pos, "extra_blocks": extra, "blocks": len(blocks)}


# ---------------------------------------------------------------------------------------------- running a variant

def run_variant(variant: str, model: str, timeout: float = 1500.0, keep: bool = False, out_dir: Path | None = None) -> dict:
    from ..client.rpc import Runtime
    from .run import merge_turns, wait_ready

    ws = Path(tempfile.mkdtemp(prefix=f"seeded-{variant}-"))
    for item in PROJECT.iterdir():                           # only the project: the key is never inside the workspace
        if item.name in {"__pycache__", ".pytest_cache"}:
            continue
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, ws / item.name)
    assert not (ws / "key.json").exists()
    res = {"variant": variant, "model": model, "ok": False, "error": None}
    t0 = time.monotonic()
    rt = Runtime(ws, model=model)
    try:
        wait_ready(model)
        rt.start()
        sid = f"seeded-{variant}-{int(t0)}"
        turns, progress = [], []
        for i, prompt in enumerate([VARIANTS[variant], *FOLLOWUPS.get(variant, [])]):
            turns.append(rt.run_turn(sid, prompt, timeout=timeout))
            f = ws / "findings.md"
            g = grade(f.read_text(encoding="utf-8", errors="replace") if f.is_file() else "", load_key())
            progress.append({"turn": i, "lens": "first pass" if i == 0 else LENSES[i], "found": len(g["found"]), "ids": g["found"]})
            if not turns[-1].ok:
                break
        res["progress"] = progress
        turn = turns[-1]
        merged = merge_turns(turns)
        names_called = [c[0] for c in merged.tool_calls]
        skills = [str((c[1] or {}).get("name", "")) for c in merged.tool_calls if c[0] == "skill"]
        findings_file = ws / "findings.md"
        findings = findings_file.read_text(encoding="utf-8", errors="replace") if findings_file.is_file() else ""
        res.update(ok=all(t.ok for t in turns), reason=turn.reason, steps=merged.steps, tool_calls=len(names_called), tool_errors=merged.tool_errors,
                   skills_loaded=skills, wrote_findings=bool(findings), findings=findings,
                   modified_source=_modified(ws), seconds=round(time.monotonic() - t0, 1))
        res["grade"] = grade(findings, load_key())
    except Exception as e:                                   # noqa: BLE001  one failed variant must not end the comparison
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        rt.close()
        if keep:
            res["workspace"] = str(ws)
        else:
            shutil.rmtree(ws, ignore_errors=True)
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{variant}-{model}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def _modified(ws: Path) -> list[str]:
    """Source files the model changed (it was told not to)."""
    changed = []
    for src in PROJECT.rglob("*.py"):
        rel = src.relative_to(PROJECT)
        dst = ws / rel
        if not dst.is_file() or dst.read_bytes() != src.read_bytes():
            changed.append(str(rel))
    return changed


def summary_line(r: dict) -> str:
    if r.get("error") or "grade" not in r:
        return f"  {r['variant']}: ERROR {r.get('error')}"
    g = r["grade"]
    steps_txt = ""
    if len(r.get("progress", [])) > 1:
        steps_txt = "  per pass " + " -> ".join(f"{p['found']}" for p in r["progress"])
    return (f"  {r['variant']}{steps_txt}: found {len(g['found'])}/{g['total']} ({g['recall']:.0%})  by class {g['by_class']}  false positives {len(g['false_positives'])}"
            f"  extra {len(g['extra_blocks'])}  steps {r['steps']}  skills {r['skills_loaded'] or 'none'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness.eval.seeded", description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", default="gemini")
    ap.add_argument("--variants", default="A,B,C")
    ap.add_argument("--grade", metavar="FILE", help="grade an existing findings file against the key and exit")
    ap.add_argument("--fixture", default="seeded_audit", help="evals/<name> holding project/ and key.json")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--no-shim", action="store_true")
    ap.add_argument("--timeout", type=float, default=1500.0)
    a = ap.parse_args(argv)
    use_fixture(a.fixture)
    if a.grade:
        print(json.dumps(grade(Path(a.grade).read_text(encoding="utf-8"), load_key()), indent=1))
        return 0
    variants = [v.strip().upper() for v in a.variants.split(",") if v.strip()]
    bad = [v for v in variants if v not in VARIANTS]
    if bad:
        print(f"unknown variant(s) {bad}; choose from {', '.join(VARIANTS)}")
        return 2
    from ..client.rpc import ensure_shim
    from .. import paths
    shim = None if a.no_shim else ensure_shim(a.model, sticky="auto", restart_stale=True)
    out_dir = paths.state_dir() / "seeded" / time.strftime("%Y%m%d-%H%M%S")
    results = []
    try:
        for v in variants:
            print(f"variant {v} on {a.model} ...", flush=True)
            r = run_variant(v, a.model, a.timeout, a.keep, out_dir)
            results.append(r)
            print(summary_line(r), flush=True)
    finally:
        if shim and shim.poll() is None:
            shim.terminate()
    print(f"results in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
