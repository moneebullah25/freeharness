"""
`freeharness doctor`: check every prerequisite and say how to fix what is missing.
`--report` prints a block safe to paste into a bug report (versions and pass/fail
only: no paths under your home directory, no tokens, no chat content).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import stat
import sys

from . import __version__, logs, paths
from .client import rpc
from .login_cmd import find_chrome, logged_in_sites

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
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        add(WARN, "not running as root", "a browser with your logins should not run as root")
    status = logged_in_sites()
    n = sum(status.values())
    add(OK if n else WARN, "signed-in sites", ", ".join(k for k, v in status.items() if v) or "none: run `freeharness login`")
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
