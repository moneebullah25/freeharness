"""
The standard toolchain: one shared, host-side virtualenv with the tools a contributor needs to run a project's own
tests and lint (pytest, ruff, flake8, black, isort, mypy, pre-commit, coverage, build tools).

    freeharness toolchain install     create/refresh <data>/toolchain-venv (normal pip, pinned versions below)
    freeharness toolchain status      full / partial / missing, from the files on disk

The sandboxed bash has no network and no $HOME, so it could never install these itself. The sandbox runner
(data/fh_sandbox_runner.py) binds this venv READ-ONLY and puts its bin/ first on PATH, which also gives the
sandbox a `python` and `python3`. Per-project dependencies are added by the gh_setup_env tool, which makes a small
per-project venv that inherits this one (a .pth file) and installs only what the project itself declares.

Trust: the versions are pinned here (top-level packages only; their dependencies float and nothing is
hash-checked), installed by pip from PyPI on the user's own command (`toolchain install`, also run by
`freeharness setup`), outside any sandbox, like any `pip install`. The venv is read-only for the agent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths

# (name, version, applies from Python, applies below Python). Newer releases of some tools need Python 3.11+.
PINNED: list[tuple[str, str, tuple | None, tuple | None]] = [
    ("pip", "26.2.2", None, None),
    ("setuptools", "84.2.1", None, None),
    ("wheel", "0.48.0", None, None),
    ("build", "1.6.1", None, None),
    ("pytest", "9.1.1", None, None),
    ("coverage", "7.17.0", (3, 11), None),
    ("coverage", "7.10.7", None, (3, 11)),
    ("ruff", "0.17.2", None, None),
    ("flake8", "7.4.1", None, None),
    ("black", "26.10.1", None, None),
    ("isort", "9.0.2", None, None),
    ("mypy", "2.4.0", None, None),
    ("pre-commit", "4.7.0", (3, 11), None),
    ("pre-commit", "4.2.0", None, (3, 11)),
]
# console scripts that must exist in bin/ for the toolchain to count as complete
COMMANDS = ["python", "python3", "pip", "pytest", "coverage", "ruff", "flake8", "black", "isort", "mypy", "pre-commit"]
RUN_CHECK = ("pytest", "ruff")          # doctor runs `<tool> --version` for these inside the bash sandbox
MANIFEST = "toolchain.json"


def toolchain_dir() -> Path:
    """<data>/toolchain-venv; FH_TOOLCHAIN overrides it (tests, a shared install)."""
    override = os.environ.get("FH_TOOLCHAIN")
    return Path(override).expanduser() if override else paths.data_dir() / "toolchain-venv"


def _applies(lo, hi, py: tuple) -> bool:
    return (lo is None or py >= lo) and (hi is None or py < hi)


def _marker(lo, hi) -> str:
    parts = ([f'python_version >= "{lo[0]}.{lo[1]}"'] if lo else []) + ([f'python_version < "{hi[0]}.{hi[1]}"'] if hi else [])
    return "; " + " and ".join(parts) if parts else ""


def requirement_lines() -> list[str]:
    return [f"{n}=={v}{_marker(lo, hi)}" for n, v, lo, hi in PINNED]


def _site_packages(venv: Path) -> Path | None:
    hits = sorted((venv / "lib").glob("python*/site-packages")) if (venv / "lib").is_dir() else []
    return hits[0] if hits else None


def installed_versions(venv: Path) -> dict[str, str]:
    """name -> version of what is installed in `venv`, read from the .dist-info folders (no interpreter is run)."""
    sp = _site_packages(venv)
    out: dict[str, str] = {}
    if not sp:
        return out
    for d in sp.glob("*.dist-info"):
        name, _, ver = d.name[:-len(".dist-info")].rpartition("-")
        if name:
            out[name.lower().replace("_", "-")] = ver
    return out


def _expected(py: tuple) -> dict[str, str]:
    """The pins that apply to this Python."""
    return {n: v for n, v, lo, hi in PINNED if _applies(lo, hi, py)}


def status(venv: Path | None = None) -> tuple[str, str, dict]:
    """("full" | "partial" | "missing", detail, {tool: version}). Reads files only; never runs the venv."""
    venv = venv or toolchain_dir()
    if not (venv / "pyvenv.cfg").is_file():
        return "missing", "not installed: run `freeharness toolchain install`", {}
    try:
        meta = json.loads((venv / MANIFEST).read_text(encoding="utf-8"))
        py = tuple(meta["python"][:2])
    except (OSError, ValueError, KeyError, TypeError):
        return "partial", "install did not finish (no manifest): run `freeharness toolchain install`", {}
    have = installed_versions(venv)
    want = _expected(py)
    wrong = [f"{n} {have.get(n.lower(), 'absent')} (pinned {v})" for n, v in want.items() if have.get(n.lower()) != v]
    missing_cmds = [c for c in COMMANDS if not (venv / "bin" / c).exists()]
    shown = {n: have[n] for n in want if n in have}
    if wrong or missing_cmds:
        more = f" and {len(wrong) - 3} more" if len(wrong) > 3 else ""
        parts = ([f"{len(wrong)} pinned tool(s) absent or at another version: " + "; ".join(wrong[:3]) + more] if wrong else []) \
            + ([f"no bin/{', bin/'.join(missing_cmds)}"] if missing_cmds else [])
        return "partial", " | ".join(parts)[:400], shown
    return "full", f"python {'.'.join(map(str, py))}; " + ", ".join(f"{n} {v}" for n, v in shown.items()), shown


def install(say=print, dest: Path | None = None, requirements: list[str] | None = None,
            pip_args: list[str] | None = None, timeout: float = 1800.0) -> Path:
    """Create the venv and pip-install the pinned tools (the whole thing is rebuilt: venvs are not relocatable).
    `requirements`/`pip_args` exist for tests (a local wheelhouse); the defaults are the pins above from PyPI."""
    dest = dest or toolchain_dir()
    reqs = requirements if requirements is not None else requirement_lines()
    if dest.is_symlink():
        raise RuntimeError(f"{dest} is a symlink; refusing to rebuild through it")
    t0 = time.time()
    if dest.exists():
        shutil.rmtree(dest)
    paths.private_dir(dest.parent)
    say(f"creating {dest} with {Path(sys.executable).name} {sys.version.split()[0]}")
    r = subprocess.run([sys.executable, "-m", "venv", str(dest)], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError("python -m venv failed:\n" + (r.stderr or r.stdout)[-800:])
    req = dest / ".requirements.txt"
    req.write_text("\n".join(reqs) + "\n", encoding="utf-8")
    say(f"installing {len(reqs)} pinned packages with pip (this downloads about 100 MB)...")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    r = subprocess.run([str(dest / "bin" / "python"), "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
                        "--no-warn-script-location", "--progress-bar", "off", *(pip_args or []), "-r", str(req)],
                       capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError("pip install failed:\n" + (r.stderr or r.stdout)[-1500:])
    (dest / MANIFEST).write_text(json.dumps({"python": list(sys.version_info[:2]), "pinned": reqs,
                                             "installed_at": int(time.time()),
                                             "seconds": round(time.time() - t0, 1)}, indent=1), encoding="utf-8")
    say(f"done in {time.time() - t0:.0f}s")
    return dest


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if cmd in ("-h", "--help"):
        print(__doc__)
        return 0
    if cmd == "install":
        try:
            install()
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print(f"toolchain install failed: {e}", file=sys.stderr)
            return 1
    elif cmd != "status":
        print("usage: freeharness toolchain install | status", file=sys.stderr)
        return 2
    level, detail, _ = status()
    print(f"toolchain: {level} ({toolchain_dir()})\n  {detail}")
    return 0 if level == "full" else 1
