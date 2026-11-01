"""
Checks to pass BEFORE `freeharness oss run` spends model turns and provider chats.

Each real run so far lost 10 to 30 minutes to something that was knowable up front: a sandbox the OS blocked (every bash call refused),
a provider already at its new-chat cap, a model that is not signed in. `check()` returns rows `(level, name, detail)` like `doctor`;
a FAIL row means the run cannot do its job and is refused (`--no-preflight` overrides), WARN rows are only printed.

The environment-import probe (does `import <project>` work in the sandbox) lives in the gh_setup_env tool, which is where the venv is made.
"""
from __future__ import annotations

import json
import os
import time

from . import paths, security

OK, WARN, FAIL = "ok", "warn", "FAIL"
HOUR = 3600.0


def _cap() -> int:
    try:
        return int(os.environ.get("FREEHARNESS_MAX_NEW_CHATS_PER_HOUR", "6"))
    except ValueError:
        return 6


def chat_budget(models: list[str], chats: dict | None = None, now: float | None = None, cap: int | None = None) -> list[tuple[str, str, str]]:
    """One row per named model: new chats used in the last hour against the cap. All named models at the cap is a FAIL (the run
    would go straight to a 429); one at the cap is a WARN (the router fails over to the next)."""
    cap = _cap() if cap is None else cap
    now = time.time() if now is None else now
    if chats is None:
        try:
            chats = json.loads((paths.state_dir() / "chats.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            chats = {}
    named = [m for m in models if m and m != "auto"]
    rows, full = [], []
    for m in named:
        used = len([t for t in (chats.get(m) or []) if isinstance(t, (int, float)) and now - t < HOUR])
        if cap and used >= cap:
            full.append(m)
            oldest = min(t for t in chats.get(m, []) if now - t < HOUR)
            rows.append((WARN, f"{m}: new-chat cap reached", f"{used}/{cap} in the last hour; a slot frees in {int(HOUR - (now - oldest)) // 60 + 1} min"))
        else:
            rows.append((OK if not cap or used < cap - 1 else WARN, f"{m}: new chats this hour", f"{used}/{cap or 'no cap'}"))
    if named and cap and len(full) == len(named):
        rows.append((FAIL, "every named model is at its new-chat cap", "wait for a slot or name another model; the cap is a deliberate guard and is not bypassed"))
    return rows


def sign_ins(models: list[str], status: dict | None = None) -> list[tuple[str, str, str]]:
    if status is None:
        try:
            from .login_cmd import status_by_site
            status = status_by_site()
        except Exception as e:  # noqa: BLE001  a login store we cannot read is a warning, not a stop
            return [(WARN, "sign-ins", f"could not be read ({str(e)[:60]})")]
    rows = []
    for m in (x for x in models if x and x != "auto"):
        v = status.get(m)
        if v is False:
            rows.append((WARN, f"{m}: not signed in", f"run `freeharness login` ({m}); the router will skip it"))
    return rows


def check(models: list[str], live: bool = False, sandbox=None, chats: dict | None = None, status: dict | None = None,
          now: float | None = None) -> list[tuple[str, str, str]]:
    """All preflight rows. `sandbox` is a security.SandboxStatus (measured with a canary when None)."""
    rows: list[tuple[str, str, str]] = []
    st = sandbox if sandbox is not None else security.probe()
    if st.level == "full":
        rows.append((OK, "bash sandbox: full", st.detail))
    else:
        rows.append((FAIL, f"bash sandbox: {st.level}", f"{st.detail}. Every bash call of the model is refused, so it can never run a repro. "
                     "Run `freeharness doctor` (on Ubuntu 24.04 bwrap needs an AppArmor profile that allows user namespaces)"))
    rows += chat_budget(models, chats, now)
    rows += sign_ins(models, status)
    return rows


def failures(rows) -> list[tuple[str, str, str]]:
    return [r for r in rows if r[0] == FAIL]


def render(rows) -> str:
    return "\n".join(f"  [{lvl:<4}] {name}" + (f": {detail}" if detail else "") for lvl, name, detail in rows)
