"""
Helpers for driving the REAL dsh runtime offline against a ScriptedModel:
free port, temporary DSH_HOME and FREEHARNESS_HOME, no browsers, no providers.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import socket
import tempfile
import threading
import uuid
from pathlib import Path

from freeharness.client import rpc

from scripted_model import ScriptedModel  # noqa: F401  (re-export)


def have_node() -> bool:
    try:
        rpc.find_node_dir()
        return True
    except RuntimeError:
        return False


class Listener:
    """A loopback TCP listener: the 'internet' for the network-egress tests.
    `hits` collects every connection and whatever bytes it sent."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.hits: list[bytes] = []
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            c.settimeout(1.0)
            data = b""
            with contextlib.suppress(OSError):
                data = c.recv(65536)
            self.hits.append(data)
            c.close()

    def close(self):
        self._stop = True
        self.sock.close()


class Sandbox:
    """Workspace + canary dirs + temp homes. Use as a context manager."""

    def __init__(self, patch_overrides=None, canary_in_home=True):
        self.root = Path(tempfile.mkdtemp(prefix="fh-sec-"))
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        (self.workspace / "hello.txt").write_text("hello\n")
        # the canary sits OUTSIDE the workspace; in $HOME (a stand-in for ~/.ssh or .env)
        # because /tmp is replaced by a fresh tmpfs by every sandbox profile anyway.
        base = Path.home() / ".cache" if canary_in_home else self.root
        base.mkdir(parents=True, exist_ok=True)
        self.outside = Path(tempfile.mkdtemp(prefix="fh-canary-", dir=base))
        self.canary = self.outside / "secret.txt"
        self.canary_text = "CANARY-" + uuid.uuid4().hex
        self.canary.write_text(self.canary_text + "\n")
        self.dsh_home = self.root / "dsh-home"
        self.fh_home = self.root / "fh-home"
        self.patch_overrides = patch_overrides

    def __enter__(self):
        return self

    def __exit__(self, *a):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def patch_for(self, model_url: str, extra: str = "") -> Path:
        """The shipped dsh.patch.yml with the model endpoint moved to a free port."""
        text = rpc.PATCH.read_text().replace("http://127.0.0.1:8766/v1", model_url)
        assert model_url in text, "baseURL not found in dsh.patch.yml"
        p = self.root / "test.patch.yml"
        p.write_text(text + "\n" + extra)
        return p


@contextlib.contextmanager
def runtime(sb: Sandbox, model: ScriptedModel, approver=None, extra_patch: str = "", env: dict | None = None):
    """A started Runtime on the sandbox's workspace, closed on exit."""
    old = {k: os.environ.get(k) for k in ["FREEHARNESS_HOME"] + list((env or {}))}
    os.environ["FREEHARNESS_HOME"] = str(sb.fh_home)
    os.environ.update(env or {})
    rt = rpc.Runtime(sb.workspace, model="auto", home=sb.dsh_home, patch=sb.patch_for(model.url, extra_patch),
                     **({"approver": approver} if approver is not None else {}))
    try:
        rt.start()
        yield rt
    finally:
        rt.close()
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run(rt, text="go", timeout=120.0):
    return rt.run_turn("sec-" + uuid.uuid4().hex[:8], text, timeout=timeout)


def tool_outputs(result) -> list[str]:
    """Text of each tool/result event of a turn."""
    out = []
    for ev in result.events:
        if ev.get("type") == "tool/result":
            msg = (ev.get("data") or {}).get("message") or {}
            c = msg.get("content")
            if isinstance(c, list):
                c = "".join(b.get("text", "") for b in c if isinstance(b, dict))
            out.append(c if isinstance(c, str) else str(c))
    return out
