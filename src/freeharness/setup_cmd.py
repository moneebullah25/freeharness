"""
`freeharness setup`: install what the Python package cannot ship itself.

  1. Node >= 24.9 (DeepSeek Harness requires it). Used as-is if found; otherwise the
     official tarball is downloaded and its SHA-256 checked against nodejs.org's
     SHASUMS256.txt. Nothing is installed system-wide.
  2. The pinned DeepSeek Harness, from the committed package-lock.json (`npm ci`),
     into the data directory. Install scripts are disabled.
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import paths
from .client.rpc import find_node_dir

NODE_VERSION = "24.22.1"
_ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
_OS = {"Linux": "linux", "Darwin": "darwin"}


def _download(url: str, dest: Path):
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:  # noqa: S310 (fixed http(s) URL, not user input)
        shutil.copyfileobj(r, f)


def _safe_extract(tar: tarfile.TarFile, dest: Path):
    """Extract only if every member stays inside `dest`.

    `extractall(filter="data")` does this but exists only in Python >= 3.12 and in 3.10.12 / 3.11.4+
    backports (CI caught it failing on an older 3.10), so the checks are done here on every version.
    Refused: absolute paths, `..` escapes, links that point outside, device/fifo members.
    """
    root = dest.resolve()
    for m in tar.getmembers():
        target = (root / m.name).resolve()
        if root != target and root not in target.parents:
            raise RuntimeError(f"unsafe path in archive: {m.name!r}")
        if m.isdev() or m.isfifo():
            raise RuntimeError(f"unsafe member type in archive: {m.name!r}")
        if m.issym() or m.islnk():
            base = target.parent if m.issym() else root
            link = (base / m.linkname).resolve()
            if root != link and root not in link.parents:
                raise RuntimeError(f"link escapes the archive: {m.name!r} -> {m.linkname!r}")
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, filter="data")     # defence in depth where available
    else:
        tar.extractall(dest)                    # noqa: S202 (members validated above)


def install_node(say=print) -> Path:
    osname, arch = _OS.get(platform.system()), _ARCH.get(platform.machine().lower())
    if not osname or not arch:
        raise RuntimeError(f"no automatic Node install for {platform.system()} {platform.machine()}; "
                           f"install Node >= 24.9 yourself and re-run setup")
    base = f"https://nodejs.org/dist/v{NODE_VERSION}"
    name = f"node-v{NODE_VERSION}-{osname}-{arch}.tar.xz"
    dest = paths.data_dir() / "node"
    with tempfile.TemporaryDirectory() as tmp:
        tarball, sums = Path(tmp) / name, Path(tmp) / "SHASUMS256.txt"
        say(f"downloading Node {NODE_VERSION} ({name})…")
        _download(f"{base}/SHASUMS256.txt", sums)
        _download(f"{base}/{name}", tarball)
        want = next((ln.split()[0] for ln in sums.read_text().splitlines() if ln.endswith(name)), None)
        got = hashlib.sha256(tarball.read_bytes()).hexdigest()
        if not want or got != want:
            raise RuntimeError(f"checksum mismatch for {name}: expected {want}, got {got}; not installing")
        say("checksum ok")
        shutil.rmtree(dest, ignore_errors=True)
        with tarfile.open(tarball) as t:
            _safe_extract(t, Path(tmp))
            shutil.move(str(Path(tmp) / name[:-len(".tar.xz")]), dest)
    return dest / "bin"


def install_harness(node_dir: str, say=print):
    src = Path(__file__).parent / "data" / "harness"
    dst = paths.private_dir(paths.harness_dir())
    for f in ("package.json", "package-lock.json"):
        shutil.copy2(src / f, dst / f)
    env = dict(os.environ, PATH=node_dir + os.pathsep + os.environ.get("PATH", ""))
    say("installing the pinned DeepSeek Harness (npm ci, install scripts disabled)…")
    npm = shutil.which("npm", path=env["PATH"])
    if not npm:
        raise RuntimeError("npm not found next to node")
    r = subprocess.run([npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
                       cwd=dst, env=env, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("npm ci failed:\n" + (r.stderr or r.stdout)[-1500:])


def main(argv=None) -> int:
    try:
        try:
            node_dir = find_node_dir()
            print(f"using Node from {node_dir}")
        except RuntimeError:
            node_dir = str(install_node())
        install_harness(node_dir)
        print("installing Playwright's Chromium (used by providers that need no login)…")
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
        if r.returncode:
            raise RuntimeError("playwright install chromium failed")
    except (RuntimeError, OSError) as e:
        print(f"setup failed: {e}", file=sys.stderr)
        return 1
    print("done. Next: `freeharness login` (once), then `freeharness doctor`.")
    return 0
