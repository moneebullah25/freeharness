"""
Stage 1 security baseline: what freeharness adds on top of dsh's own sandbox services.

dsh (sandbox-local + bash-sandbox + fs-sandbox + user-approval) provides workspace-write
confinement for bash and the write/edit tools, and an approval seam that fails closed.
It does NOT restrict reads or the network (its READMEs say so). This module wires in:

  * data/fh_sandbox_runner.py  the `runnerCommand` of sandbox-local: dsh's bwrap profile plus
                               no network, private places (HOME, /tmp, /run ...) hidden, env
                               secrets removed; no bwrap -> the command does not run.
  * data/fh_guard.mjs          a plugin in the dsh runtime: relays approval requests to the
                               client, and fences the read/grep/glob tools.
  * `probe()`                  what `freeharness doctor` reports: full / partial / unavailable,
                               measured with a canary file and a loopback listener, not assumed.

Same-kernel confinement (bubblewrap namespaces), not a container or microVM.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
RUNNER = DATA / "fh_sandbox_runner.py"
GUARD = DATA / "fh_guard.mjs"
APPROVAL_NEVER = DATA / "approval_never.patch.yml"   # no human to ask: approval policy `never`


def runner_command() -> list[str]:
    """The sandbox-local `runnerCommand` argv (the profile and the command are appended by dsh)."""
    return [sys.executable, str(RUNNER)]


def dsh_env() -> dict[str, str]:
    """Environment the dsh patch reads (see dsh.patch.yml): where the sandbox runner lives."""
    return {"FH_SANDBOX_RUNNER": json.dumps(runner_command())}


def guard_patch(directory: Path) -> Path:
    """A dsh patch file mounting the guard plugin (a plugin row needs an absolute path, known only at runtime)."""
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / "fh-guard.patch.yml"
    text = f"- insert:\n    - id: fh-guard\n      name: {json.dumps(str(GUARD))}\n"
    if not f.exists() or f.read_text() != text:
        tmp = f.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text)
        tmp.replace(f)
    return f


def guard_failed(stderr_lines) -> bool:
    """True when dsh reported that it dropped the guard plugin (it then runs WITHOUT the read fence
    and approval relay, so the client treats it as a failed start)."""
    return any("fh-guard" in ln and ("disabling" in ln or "cannot" in ln.lower() or "error" in ln.lower())
               for ln in stderr_lines)


@dataclasses.dataclass
class SandboxStatus:
    level: str                       # "full" | "partial" | "unavailable"
    detail: str
    checks: dict                     # name -> bool (True = enforced)

    def __str__(self):
        return f"{self.level}: {self.detail}"


def _run_confined(cmd: str, workspace: Path, writable: bool = True, timeout: float = 20.0):
    """Run `bash -c cmd` exactly as dsh would (profile args + runner), in `workspace`."""
    profile = ["--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--die-with-parent"]
    if writable:
        profile += ["--tmpfs", "/tmp", "--bind", str(workspace), str(workspace)]  # noqa: S108
    argv = [*runner_command(), *profile, "--", "bash", "-c", cmd]
    return subprocess.run(argv, cwd=workspace, capture_output=True, text=True, timeout=timeout)


def probe() -> SandboxStatus:
    """Measure the bash sandbox on this host.

    full         bash cannot read a canary in $HOME, cannot write outside the workspace,
                 cannot reach a loopback listener, and can write inside the workspace
    partial      the sandbox starts but at least one of those does not hold
    unavailable  no bubblewrap, or it cannot create the sandbox here (fail closed: bash
                 tool calls are refused, not run unconfined)
    """
    if sys.platform != "linux":
        return SandboxStatus("unavailable", f"no enforcing runner configured for {sys.platform} (freeharness supports bwrap on Linux)", {})
    if not shutil.which("bwrap"):
        return SandboxStatus("unavailable", "bubblewrap (bwrap) not installed; bash calls will be refused (apt install bubblewrap)", {})
    home = Path.home()
    base = home / ".cache"
    try:
        base.mkdir(parents=True, exist_ok=True)
        outside = Path(tempfile.mkdtemp(prefix="fh-probe-", dir=base if os.access(base, os.W_OK) else None))
    except OSError as e:
        return SandboxStatus("unavailable", f"cannot create probe files: {e}", {})
    ws = Path(tempfile.mkdtemp(prefix="fh-probe-ws-"))
    srv = socket.socket()
    hits: list[int] = []
    try:
        (outside / "canary").write_text("canary")
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        srv.settimeout(0.2)
        done = threading.Event()

        def accept():
            while not done.is_set():
                try:
                    c, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                hits.append(1)
                c.close()
        t = threading.Thread(target=accept, daemon=True)
        t.start()
        try:
            base_run = _run_confined("echo ok > ws-ok.txt && echo started", ws)
        except (OSError, subprocess.SubprocessError) as e:
            return SandboxStatus("unavailable", f"sandbox runner failed to start: {e}", {})
        if base_run.returncode != 0 or "started" not in base_run.stdout:
            why = (base_run.stderr or base_run.stdout).strip().splitlines()[-1:] or ["no output"]
            return SandboxStatus("unavailable", f"bwrap cannot create the sandbox here ({why[0][:120]}); bash calls will be refused", {})
        checks = {
            "workspace write allowed": (ws / "ws-ok.txt").exists(),
            "outside write blocked": _run_confined(f"echo x > {outside}/w 2>/dev/null; test -e {outside}/w", ws).returncode != 0,
            "outside read blocked": "canary" not in _run_confined(f"cat {outside}/canary 2>/dev/null", ws).stdout,
            "network blocked": _run_confined(f"exec 3<>/dev/tcp/127.0.0.1/{port}", ws).returncode != 0,
        }
        time.sleep(0.3)
        checks["network blocked"] = checks["network blocked"] and not hits
        done.set()
    finally:
        srv.close()
        shutil.rmtree(outside, ignore_errors=True)
        shutil.rmtree(ws, ignore_errors=True)
    bad = [k for k, v in checks.items() if not v]
    if not bad:
        return SandboxStatus("full", "bwrap: workspace-only writes, home and /tmp hidden, no network (bash tool only)", checks)
    return SandboxStatus("partial", "sandbox starts but does not enforce: " + ", ".join(bad), checks)
