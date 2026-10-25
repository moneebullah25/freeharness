"""
Advisory LLM judge for eval runs. It reads an EVIDENCE PACKET (what changed in the
workspace, which tools ran, what they printed) and returns a structured score with its
reasoning. It is advisory only: the deterministic check in tasks.py decides pass/fail,
and nothing here can flip that result (see review.final_assessment).

    packet = build_packet(task, ws, res, ok, detail)
    verdict = judge(packet, ask)            # ask(prompt) -> str ; default asks the shim
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

MAX_FILE = 1500          # chars of one file shown to the judge
MAX_TOOL_OUT = 400
MAX_CALLS = 40
SCORE_MAX = 10


def snapshot(ws: Path) -> dict:
    """Relative path -> text of every regular file in the workspace (skips dot-dirs' contents except .dsh)."""
    out = {}
    for p in sorted(ws.rglob("*")):
        rel = p.relative_to(ws)
        if not p.is_file() or "__pycache__" in rel.parts:
            continue
        try:
            out[rel.as_posix()] = p.read_text(errors="replace")
        except OSError:
            continue
    return out


def changed_files(initial: dict, ws: Path) -> list[dict]:
    now = snapshot(ws)
    rows = []
    for rel in sorted(set(initial) | set(now)):
        if rel not in now:
            rows.append({"path": rel, "change": "deleted"})
        elif rel not in initial:
            rows.append({"path": rel, "change": "added", "content": now[rel][:MAX_FILE]})
        elif initial[rel] != now[rel]:
            rows.append({"path": rel, "change": "modified", "content": now[rel][:MAX_FILE]})
    return rows


def _tool_outputs(events: list) -> dict:
    """callId -> printed text of the tool result."""
    out = {}
    for ev in events or []:
        if ev.get("type") != "tool/result":
            continue
        msg = (ev.get("data") or {}).get("message") or {}
        txt = "".join(b.get("text", "") for b in msg.get("content") or [] if isinstance(b, dict))
        out[msg.get("toolCallId")] = (txt[:MAX_TOOL_OUT], bool(msg.get("isError")))
    return out


def build_packet(task, ws: Path, res, ok: bool, detail: str) -> dict:
    """Everything the judge may see. Deterministic result included so it can comment on it, not change it."""
    outs = _tool_outputs(res.events)
    call_ids = [(ev.get("data") or {}).get("callId") for ev in res.events or [] if ev.get("type") == "tool/call"]
    calls = []
    for i, (name, args) in enumerate(res.tool_calls[:MAX_CALLS]):
        text, err = outs.get(call_ids[i] if i < len(call_ids) else None, ("", False))
        calls.append({"tool": name, "arguments": json.dumps(args)[:300], "output": text, "error": err})
    return {"task": task.id, "prompt": task.prompt, "files_changed": changed_files(task.files, ws),
            "tool_calls": calls, "final_answer": (res.final_text or "")[:600],
            "deterministic": {"ok": bool(ok), "detail": detail}}


INSTRUCTIONS = (
    "You review an AI coding agent's work for quality. You get an evidence packet: the task, the files the "
    "agent changed, the tools it ran with their output, and its final answer. The agent's own claims are not "
    "evidence; judge only what the files and tool output show. Your score is advisory and cannot change the "
    f"automated pass/fail. Reply with ONLY a JSON object: {{\"score\": integer 0-{SCORE_MAX}, "
    "\"verdict\": \"good\"|\"acceptable\"|\"poor\", \"reasoning\": short text, \"concerns\": [short strings]}.")


def prompt_for(packet: dict) -> str:
    return INSTRUCTIONS + "\n\nEVIDENCE PACKET:\n" + json.dumps(packet, indent=1)


def parse(text: str) -> dict:
    """Structured verdict from a model reply; tolerant of fences/prose, never raises."""
    base = {"score": None, "verdict": "unparseable", "reasoning": (text or "")[:500], "concerns": [], "advisory": True}
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return base
    try:
        d = json.loads(m.group(0))
        score = int(d["score"])
    except (ValueError, KeyError, TypeError):
        return base
    verdict = d.get("verdict") if d.get("verdict") in ("good", "acceptable", "poor") else (
        "good" if score >= 8 else "acceptable" if score >= 5 else "poor")
    return {"score": max(0, min(SCORE_MAX, score)), "verdict": verdict, "reasoning": str(d.get("reasoning", ""))[:1000],
            "concerns": [str(c)[:200] for c in (d.get("concerns") or [])][:10], "advisory": True}


def ask_shim(prompt: str, model: str = "auto", timeout: float = 300.0) -> str:
    """Ask the local shim (OpenAI-compatible). Costs one free-tier message."""
    from .. import auth
    from ..client.rpc import SHIM_URL
    body = json.dumps({"model": model, "stream": False, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(SHIM_URL + "/v1/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + auth.load_or_create_token()})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed local URL)
        return json.load(r)["choices"][0]["message"]["content"] or ""


def judge(packet: dict, ask=ask_shim) -> dict:
    """Advisory verdict. The result records the reasoning and the deterministic result it saw,
    and has no field that could override it. A failing `ask` yields an unavailable verdict."""
    try:
        v = parse(ask(prompt_for(packet)))
    except Exception as e:                     # noqa: BLE001 -- the judge is optional
        v = {"score": None, "verdict": "unavailable", "reasoning": f"{type(e).__name__}: {str(e)[:200]}",
             "concerns": [], "advisory": True}
    v["saw_deterministic"] = packet["deterministic"]
    return v
