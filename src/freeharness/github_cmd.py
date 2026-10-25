"""
`freeharness github enable [--live] | disable | status`: wire the GitHub contribution gate
(mcp/ghgate.py, tools gh_*) into the agent.

  * writes <config>/overlay.d/github.yml, an MCP server row for DeepSeek Harness. The row passes only the
    NAMES of environment variables (GITHUB_TOKEN, the client's confirmation socket); a secret is never
    written to disk by freeharness;
  * adds a short marked block to the user-global AGENTS.md describing the order of work.

Default is DRY-RUN: every gate is checked and the draft is returned, nothing is posted. `--live` asks you
once (y/N) and then lets drafts you approve in the terminal be posted for real. See docs/GITHUB_GATE.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import paths, sessions
from .vault_cmd import overlay_dir

ROW = "github.yml"
BEGIN, END = "<!-- freeharness:github -->", "<!-- /freeharness:github -->"
INSTRUCTIONS = f"""{BEGIN}
## GitHub contribution (gh_* tools)
Bash has no network or git credentials; use only the gh_* tools. Order: gh_repo_info (read CONTRIBUTING), gh_search
issues AND prs (open and closed, 4+ different queries), gh_fork, gh_clone, work on a feature branch in the clone
(plain commits, no attribution or tool names), gh_push, then gh_issue_create / gh_pr_create (PR body needs `Fixes #N`).
Writes show your draft to the user, who approves, rejects, or sends back a note: revise and call again. A refusal
says what to fix; do not try to get around a gate.
{END}
"""


def _row(live: bool) -> str:
    return (
        "# managed by `freeharness github`; remove with `freeharness github disable`\n"
        "- id: mcp-resources            # three generic resource tools we do not need: prompt space\n"
        "  disabled: true\n"
        "- insert:\n"
        "    - id: mcp-github-gate\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: gh\n"
        "        transport: stdio\n"
        f"        command: {json.dumps(sys.executable)}\n"
        "        args: [\"-m\", \"freeharness.mcp.ghgate\"]\n"
        "        toolCallTimeoutMs: 600000\n"
        "        env:\n"
        "          # names only: dsh reads the values from its own environment at start\n"
        "          GITHUB_TOKEN: !!js \"(process.env.GITHUB_TOKEN ?? '')\"\n"
        "          FH_CONFIRM_SOCKET: !!js \"(process.env.FH_APPROVAL_SOCK ?? '')\"\n"
        "          FH_CONFIRM_TOKEN: !!js \"(process.env.FH_APPROVAL_TOKEN ?? '')\"\n"
        f"          FH_GH_DRYRUN: '{'0' if live else '1'}'\n"
    )


def _agents_md() -> Path:
    return paths.dsh_home() / "AGENTS.md"


def _set_block(text: str, block: str | None) -> str:
    text = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", text, flags=re.S)
    if block:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    return text


def is_enabled() -> bool:
    return (overlay_dir() / ROW).exists()


def is_live() -> bool:
    f = overlay_dir() / ROW
    return f.exists() and "FH_GH_DRYRUN: '0'" in f.read_text(encoding="utf-8")


def enable(live: bool = False) -> None:
    sessions.atomic_write(overlay_dir() / ROW, _row(live))
    md = _agents_md()
    paths.private_dir(md.parent)
    sessions.atomic_write(md, _set_block(md.read_text(encoding="utf-8") if md.exists() else "", INSTRUCTIONS))


def disable() -> None:
    (overlay_dir() / ROW).unlink(missing_ok=True)
    md = _agents_md()
    if md.exists():
        new = _set_block(md.read_text(encoding="utf-8"), None)
        sessions.atomic_write(md, new) if new.strip() else md.unlink()


def token_available(env=None) -> bool:
    env = os.environ if env is None else env
    if env.get("GITHUB_TOKEN"):
        return True
    f = env.get("FH_GH_ENVFILE")
    return bool(f) and Path(f).expanduser().is_file()


def live_banner() -> str | None:
    """Printed by the interactive client when real posting is on."""
    if is_enabled() and is_live():
        return ("GitHub gate is LIVE: drafts you approve in this terminal are posted to GitHub "
                "as your account (`freeharness github enable` returns to dry-run).")
    return None


def doctor_row() -> tuple[str, str, str]:
    if not is_enabled():
        return "ok", "github gate", "off (`freeharness github enable` to let the agent do open-source work)"
    problems = []
    if not shutil.which("git"):
        problems.append("git not found")
    if not token_available():
        problems.append("GITHUB_TOKEN is not in the environment (read tools will fail)")
    mode = "LIVE posting" if is_live() else "dry-run"
    return ("warn" if problems else "ok"), "github gate", mode + ("; " + "; ".join(problems) if problems else "")


LIVE_WARNING = """\
--live lets the agent's gh_* tools post to GitHub as YOUR account:
  forks, issues, pull requests, review replies and branch pushes.
Each one is shown to you in this terminal first (approve / reject / request changes);
nothing is sent without your explicit approval, and -p runs can never post.
Rules enforced in code stay on: no force pushes, no default-branch pushes, search evidence
before filing, competing-PR check, no attribution markers, no artifact files."""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness github", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("enable", help="connect the gh_* tools (dry-run unless --live)")
    p.add_argument("--live", action="store_true", help="really post what you approve (asks y/N once)")
    sub.add_parser("disable")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "enable":
        if a.live:
            print(LIVE_WARNING)
            if not sys.stdin.isatty():
                print("refusing --live without a terminal to ask you", file=sys.stderr)
                return 1
            if input("turn live posting on? [y/N] ").strip().lower() not in ("y", "yes"):
                print("not enabled")
                return 1
        enable(a.live)
        print(f"github gate enabled ({'LIVE' if a.live else 'dry-run: nothing is posted'}); restart a running freeharness.\n"
              "Export GITHUB_TOKEN in the shell that starts freeharness (or point FH_GH_ENVFILE at a file holding it).")
    elif a.cmd == "disable":
        disable()
        print("github gate disabled")
    else:
        if not is_enabled():
            print("github gate: not enabled")
        else:
            print(f"github gate: enabled, {'LIVE (approved drafts are posted)' if is_live() else 'dry-run (nothing is posted)'}; "
                  f"GITHUB_TOKEN: {'present in this shell' if token_available() else 'MISSING in this shell'}")
    return 0
