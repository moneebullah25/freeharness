"""`freeharness oss watch <run>`: follow a running staged OSS run, one flushed line per event.

The formatting functions are pure (events in, text out) so they are testable without a run directory.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

from . import oss_health

ALERT_AFTER = 240.0
RUNNING = ("starting", "running", "")


def format_event(e: dict) -> str:
    t = str(e.get("t", ""))[11:]
    if e.get("kind") == "tool":
        bad = e.get("status") != "ok"
        msg = f"{t} {'ERR' if bad else 'ok '} #{e.get('n')} {e.get('tool')} {str(e.get('args'))[:90]}"
        if bad:
            msg += " :: " + str(e.get("result"))[:200]
        return msg
    return f"{t} [{e.get('kind')}] {str(e.get('text'))[:300]}"


def format_line(line: str) -> tuple[str, dict | None]:
    try:
        e = json.loads(line)
        if not isinstance(e, dict):
            raise ValueError
    except ValueError:
        return "RAW " + line[:200], None
    return format_event(e), e


def alert_text(quiet: float) -> str:
    return f"ALERT: no activity for {int(quiet)}s (stalled model turn or provider?)"


class Tally:
    def __init__(self) -> None:
        self.tools = 0
        self.errors: Counter = Counter()
        self.repairs = 0
        self.models: set[str] = set()

    def add(self, e: dict) -> None:
        if e.get("kind") == "tool":
            self.tools += 1
            if e.get("status") != "ok":
                self.errors[oss_health.classify_error(e.get("result"))] += 1
        elif e.get("kind") == "stage":
            text = str(e.get("text", ""))
            if e.get("kind_") == "repair":
                self.repairs += 1
            if e.get("kind_") in ("stage", "repair") and " turn on " in text:
                self.models.add(text.split(" turn on ", 1)[1].split()[0])
            elif "switching to model " in text:
                self.models.add(text.split("switching to model ", 1)[1].split(",")[0].split()[0])

    def summary(self) -> str:
        errs = ", ".join(f"{k} {v}" for k, v in sorted(self.errors.items())) or "none"
        return (f"summary: {self.tools} tool call(s); errors {sum(self.errors.values())} ({errs}); "
                f"repair turns {self.repairs}; models {', '.join(sorted(self.models)) or '?'}")


def _json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def finished(run_dir: Path, alive=_alive) -> str:
    """'' while the run goes on, else why the watcher should stop."""
    outcome = _json(run_dir / "state.json").get("outcome")
    if outcome and outcome not in RUNNING:
        return f"outcome: {outcome}"
    run = _json(run_dir / "run.json")
    if run.get("pid") and not alive(run["pid"]):
        return f"run pid {run['pid']} is gone"
    return ""


def _flush_print(s: str) -> None:
    print(s, flush=True)


def watch(run_dir, out=_flush_print, poll: float = 1.0, alert_after: float = ALERT_AFTER, sleep=time.sleep, now=time.time,
          alive=_alive, max_polls: int | None = None, from_start: bool = True) -> int:
    """Print every supervisor.jsonl line (existing ones first), then follow until the run ends."""
    d = Path(run_dir)
    sup, buf, tally = d / "supervisor.jsonl", b"", Tally()
    pos = 0 if from_start or not sup.exists() else sup.stat().st_size
    last, polls = now(), 0
    out(f"watching {d.name} (ctrl-c to stop)")
    try:
        while True:
            data = b""
            try:
                with open(sup, "rb") as f:
                    f.seek(pos)
                    data = f.read()
            except OSError:
                pass
            pos += len(data)
            *lines, buf = (buf + data).split(b"\n")      # a half-written last line waits for its newline
            for raw in lines:
                if raw.strip():
                    last = now()
                    text, ev = format_line(raw.decode("utf-8", "replace"))
                    if ev:
                        tally.add(ev)
                    out(text)
            why = "" if data else finished(d, alive)
            if why:
                out(f"run ended ({why})")
                break
            if now() - last > alert_after:
                out(alert_text(now() - last))
                last = now()
            polls += 1
            if max_polls is not None and polls >= max_polls:
                break
            sleep(poll)
    except KeyboardInterrupt:
        pass
    out(tally.summary())
    return 0
