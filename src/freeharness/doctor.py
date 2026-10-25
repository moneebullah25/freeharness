"""
`freeharness doctor`: check every prerequisite and say how to fix what is missing.
`--report` prints a block safe to paste into a bug report (versions and pass/fail
only: no paths under your home directory, no tokens, no chat content).
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import os
import socket
import stat
import sys

from . import __version__, logs, paths, security
from .client import rpc
from .login_cmd import find_chrome, status_by_site

OK, WARN, FAIL = "ok", "warn", "FAIL"


def checks() -> list[tuple[str, str, str]]:
    out = []

    def add(level, name, detail=""):
        out.append((level, name, detail))

    add(OK if sys.version_info >= (3, 10) else FAIL, "python >= 3.10", sys.version.split()[0])
    add(OK if importlib.util.find_spec("playwright") else FAIL, "playwright installed",
        "" if importlib.util.find_spec("playwright") else "pip install playwright")
    chrome = find_chrome()
    add(OK if chrome else FAIL, "Google Chrome found", "" if chrome else "install Chrome or set FREEHARNESS_CHROME")
    try:
        rpc.find_node_dir()
        add(OK, f"node >= {rpc.MIN_NODE[0]}.{rpc.MIN_NODE[1]}", "found")
    except RuntimeError:
        add(FAIL, f"node >= {rpc.MIN_NODE[0]}.{rpc.MIN_NODE[1]}", "run `freeharness setup`")
    add(OK if rpc.DSH_BIN.exists() else FAIL, "DeepSeek Harness installed",
        "" if rpc.DSH_BIN.exists() else "run `freeharness setup`")
    st = security.probe()          # measured with a canary file and a loopback listener, not assumed
    add({"full": OK, "partial": WARN}.get(st.level, FAIL), f"bash sandbox: {st.level}", st.detail)
    tl, tdetail = security.probe_toolchain()
    add(OK if tl == "full" else WARN, f"standard toolchain: {tl}",
        tdetail if tl == "full" else tdetail + (" (bash then has no pytest/ruff/python)" if tl == "missing" else ""))
    add(OK, "read/grep/glob fence", "fh-guard plugin: home, /tmp, /run unreadable outside the workspace "
        "(a policy fence inside dsh, not a kernel boundary)")
    from . import github_cmd
    add(*github_cmd.doctor_row())
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        add(WARN, "not running as root", "a browser with your logins should not run as root")
    status = status_by_site()
    ok_sites = [k for k, v in status.items() if v]
    add(OK if ok_sites else WARN, "signed-in sites", ", ".join(ok_sites) or "none: run `freeharness login`")
    unknown = [k for k, v in status.items() if v is None]
    if unknown:
        add(WARN, "sites whose login cannot be read", ", ".join(unknown) + " (verify with the eval)")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            have = Path(pw.chromium.executable_path).exists()
        add(OK if have else FAIL, "Playwright Chromium installed (used by Gemini)",
            "" if have else "run `freeharness setup`")
    except Exception as e:  # noqa: BLE001
        add(WARN, "Playwright Chromium check", str(e)[:80])
    tf = paths.token_file()
    if tf.exists():
        mode = stat.S_IMODE(tf.stat().st_mode)
        add(OK if mode & 0o077 == 0 else FAIL, "access token private (0600)", oct(mode))
    prof = paths.chrome_profile()
    if prof.exists() and hasattr(os, "getuid"):
        mode = stat.S_IMODE(prof.stat().st_mode)
        add(OK if mode & 0o077 == 0 else WARN, "browser profile private (0700)", oct(mode))
    try:
        paths.private_dir(paths.log_dir())
        add(OK, "log directory writable")
    except OSError as e:
        add(WARN, "log directory writable", str(e))
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 8766))
        add(OK, "port 8766 free")
    except OSError:
        health = rpc.shim_health()
        add(OK if health else WARN, "port 8766", "the shim is already running" if health else "in use by something else")
    finally:
        s.close()
    # Prompt-size limits are never assumed; these are what the sites themselves showed (backends/limits.py).
    from .backends.limits import Limits, default_path
    lim = Limits(default_path())
    for key in sorted(lim.data):
        snap = lim.snapshot(key)
        if snap["ceiling_chars"]:
            add(OK, f"limit {key}", f"confirmed ceiling {snap['ceiling_chars']:,} chars (failed at {snap['suspect_max_chars']:,}, "
                                    f"largest good {snap['known_good_chars']:,})")
        elif snap["suspect_max_chars"]:
            add(WARN, f"limit {key}", f"suspected: failed at {snap['suspect_max_chars']:,} chars, not confirmed yet "
                                      f"(largest good {snap['known_good_chars']:,})")
        else:
            add(OK, f"limit {key}", f"no limit seen; largest good prompt {snap['known_good_chars']:,} chars")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness doctor")
    ap.add_argument("--report", action="store_true", help="print a shareable diagnostic block")
    args = ap.parse_args(argv)
    rows = checks()
    if args.report:
        env = logs.environment()
        print("freeharness diagnostics")
        for k, v in env.items():
            print(f"  {k}: {v}")
        for level, name, _ in rows:
            print(f"  [{level}] {name}")
        print(f"  logs: {paths.log_dir()}   (attach freeharness.log and shim.log if asked)")
        return 0
    width = max(len(n) for _, n, _ in rows)
    for level, name, detail in rows:
        print(f"[{level:4}] {name:{width}}  {detail}")
    bad = [r for r in rows if r[0] == FAIL]
    print(f"\nfreeharness {__version__}: " + ("ready" if not bad else f"{len(bad)} problem(s) to fix"))
    return 1 if bad else 0
