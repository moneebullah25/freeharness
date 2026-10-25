"""
A dsh runtime driven over newline-delimited JSON-RPC on stdio.

Protocol (from @deepseek-ai/dsh-sdk-protocol, read 2026-10-24): requests
`initialize`, `session/prompt`, `session/resume`, `session/wait`, `shutdown`;
notifications `session.event` (every session's log events, as recorded) and
`session.status` (running/idle). There is NO cancel method: a turn is abandoned
by killing the runtime, then `session/resume` repairs the interrupted turn.
That is what `Runtime.restart()` + `resume()` are for.

This module is deliberately free of terminal code so the eval suite can reuse it.
"""
from __future__ import annotations

import collections
import dataclasses
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import sys

from .. import auth, paths, security

DSH_BIN = paths.harness_dir() / "node_modules" / ".bin" / ("dsh.cmd" if sys.platform == "win32" else "dsh")
DSH_HOME = paths.dsh_home()
PATCH = Path(__file__).resolve().parent.parent / "data" / "dsh.patch.yml"
SHIM_URL = "http://127.0.0.1:8766"
SHIM_LOG_MAX = 5_000_000
MIN_NODE = (24, 9)


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{message} (code {code})")
        self.code, self.message = code, message


class RuntimeDied(Exception):
    pass


def _node_version(binary: str) -> tuple | None:
    try:
        out = subprocess.run([binary, "-v"], capture_output=True, text=True, timeout=10).stdout
        m = re.match(r"v(\d+)\.(\d+)", out.strip())
        return (int(m.group(1)), int(m.group(2))) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def find_node_dir() -> str:
    """Directory holding a Node >= 24.9. System Node here is 18, so look in nvm."""
    candidates = []
    if os.environ.get("FREE_AGENT_NODE_BIN"):
        candidates.append(Path(os.environ["FREE_AGENT_NODE_BIN"]))
    which = shutil.which("node")
    if which:
        candidates.append(Path(which).parent)
    own = paths.data_dir() / "node" / "bin"          # what `freeharness setup` installs
    if own.is_dir():
        candidates.append(own)
    nvm = Path(os.environ.get("NVM_DIR", Path.home() / ".nvm")) / "versions" / "node"
    if nvm.is_dir():
        candidates += [d / "bin" for d in sorted(nvm.iterdir(), reverse=True)]
    for d in candidates:
        v = _node_version(str(d / "node"))
        if v and v >= MIN_NODE:
            return str(d)
    raise RuntimeError(f"dsh needs Node >= {MIN_NODE[0]}.{MIN_NODE[1]}; none found "
                       f"(tried {[str(c) for c in candidates]}). Run `freeharness setup` to install one.")


def shim_health(timeout: float = 2.0, token: str | None = None) -> dict | None:
    """Provider status from the shim, or None if it is not answering. Without the
    token the shim only reports liveness ({"ok": true}), without provider detail."""
    req = urllib.request.Request(SHIM_URL + "/health")  # noqa: S310 (fixed http(s) URL, not user input)
    req.add_header("Authorization", "Bearer " + (token or auth.load_or_create_token()))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed http(s) URL, not user input)
            return json.load(r)
    except (OSError, ValueError):
        return None


def _rotate(log: Path):
    """Bound the shim's log: it is appended to by every launch."""
    try:
        if log.exists() and log.stat().st_size > SHIM_LOG_MAX:
            log.replace(log.with_suffix(".log.1"))
    except OSError:
        pass


def _stop_stale_shim(health: dict) -> bool:
    """SIGTERM a shim of OURS whose code differs from the installed code. True if it went away."""
    pid = health.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return False
    if "freeharness.backends.shim" not in cmdline:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(40):
        if not shim_health(0.5):
            return True
        time.sleep(0.25)
    return False


def ensure_shim(providers: str = "gemini", wait: float = 30.0, sticky: str = "",
                restart_stale: bool = False) -> subprocess.Popen | None:
    """Start the OpenAI-compatible shim unless one is already answering.
    Returns the process only if WE started it (so the caller knows to stop it).
    A shim started before the installed code changed is stale: with `restart_stale` (batch runs such as
    eval and oss) it is replaced, otherwise a warning is printed and it is kept (it may be serving a
    live chat)."""
    health = shim_health()
    if health:
        from ..backends.shim import code_fingerprint
        if health.get("code") != code_fingerprint():          # a shim without the field predates it: stale too
            if not (restart_stale and _stop_stale_shim(health)):
                sys.stderr.write("warning: the running shim was started before the code changed; "
                                 "stop it so the new code takes effect\n")
                return None
        else:
            return None
    logdir = paths.private_dir(paths.log_dir())
    logfile = logdir / "shim.log"
    _rotate(logfile)
    log = os.fdopen(os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")
    proc = subprocess.Popen([sys.executable, "-m", "freeharness.backends.shim", "serve",
                             "--providers", providers, *(["--sticky", sticky] if sticky else [])],
                            stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"the shim exited ({proc.returncode}); see {logfile}")
        if shim_health(0.5):
            return proc
        time.sleep(0.3)
    proc.terminate()
    raise RuntimeError(f"the shim did not come up within {wait:.0f}s; see {logfile}")


@dataclasses.dataclass
class TurnResult:
    ok: bool                       # the turn ended with reason.kind == "completed"
    reason: dict                   # turn/end data.reason
    final_text: str                # text of the last assistant message
    steps: int
    tool_calls: list               # [(name, arguments-dict)] in order
    tool_errors: int               # tool results with isError
    seconds: float
    events: list                   # every session event of this turn, in order


class Runtime:
    def __init__(self, cwd: str | Path, provider: str = "bridge", model: str = "auto",
                 home: Path = DSH_HOME, patch: Path = PATCH, init_timeout: float = 90.0,
                 overlay_files: list | None = None, approver=None):
        self.cwd, self.provider, self.model = str(Path(cwd).resolve()), provider, model
        self.home, self.patch, self.init_timeout = Path(home), Path(patch), init_timeout
        self.overlay_files = overlay_files            # None = the user's overlay.d; a list replaces it (hermetic runs)
        self.approver = approver        # client.approval.Approver, or None: approval requests are then refused (policy never)
        self.events: queue.Queue = queue.Queue()      # (method, params) | ("__exit__", {...})
        self.stderr_tail = collections.deque(maxlen=40)
        self._proc: subprocess.Popen | None = None
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._next = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        paths.private_dir(self.home)
        env = dict(os.environ, DSH_HOME=str(self.home), BRIDGE_KEY=auth.load_or_create_token())
        # dsh reads its sandbox mode from this variable; an inherited
        # DSH_PERMISSION_MODE=danger-full-access must not silently widen the trust model.
        env["DSH_PERMISSION_MODE"] = "workspace-write"
        env["FREEHARNESS_CWD"] = self.cwd                 # the memory MCP server scopes project facts by it
        env["PATH"] = find_node_dir() + os.pathsep + env.get("PATH", "")
        env.update(security.dsh_env())                    # sandbox runner + guard plugin (see security.py)
        cmd = [str(DSH_BIN), "--profile", "sdk", "--patch", str(self.patch),
               "--patch", str(security.guard_patch(paths.private_dir(paths.state_dir()) / "dsh"))]
        if self.approver:                                 # a human can answer: ask instead of never
            self.approver.start()
            env.update(self.approver.env())
        cmd += ["--patch", str(self.approver.patch if self.approver else security.APPROVAL_NEVER)]
        for extra in (self.overlays() if self.overlay_files is None else self.overlay_files):  # <config>/overlay.d/*.yml
            cmd += ["--patch", str(extra)]
        # A fresh queue per process: the previous process's reader finishes late
        # and must not post its exit into (or fail requests of) this one.
        self.events = q = queue.Queue()
        if not Path(self.cwd).is_dir():
            raise RuntimeError(f"workspace {self.cwd} is not a directory")
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, cwd=self.cwd, env=env,
                                      start_new_session=True)
        threading.Thread(target=self._read_stdout, args=(self._proc, q), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self._proc,), daemon=True).start()
        # The model adapter registers asynchronously after boot, and the shim may
        # still be finishing an abandoned request, so an early initialize can fail
        # with "no adapter registered". That is transient: retry for a while.
        deadline = time.monotonic() + self.init_timeout
        while True:
            try:
                self.request("initialize", {"cwd": self.cwd, "provider": self.provider,
                                            "model": self.model}, timeout=self.init_timeout)
                if security.guard_failed(self.stderr_tail):      # fail closed: no fence, no approval relay
                    self.close()
                    raise RuntimeError("the freeharness guard plugin did not load: " + self.diagnose(""))
                return
            except RpcError as e:
                if "no adapter" not in e.message and "not initialized" not in e.message:
                    raise
                if time.monotonic() > deadline:
                    raise
                time.sleep(1.0)

    @staticmethod
    def overlays() -> list[Path]:
        d = paths.config_dir() / "overlay.d"
        return sorted(d.glob("*.yml")) if d.is_dir() else []

    def close(self):
        proc, self._proc = self._proc, None
        if not proc:
            return
        if self.approver:
            self.approver.close()
        try:
            if proc.poll() is None:
                try:
                    self._send(proc, {"jsonrpc": "2.0", "id": self._new_id(), "method": "shutdown", "params": {}})
                    proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._fail_pending(proc, "runtime closed")

    def abandon(self, session_id: str):
        """Interrupt = restart + `session/resume` (which closes the interrupted turn)."""
        self.restart()
        self.resume(session_id)

    def restart(self, model: str | None = None):
        """Abandon whatever is running. The only way to interrupt a turn."""
        self.close()
        if model:
            self.model = model
        self.start()

    # -- api ---------------------------------------------------------------

    def prompt(self, session_id: str, text: str) -> str:
        return self.request("session/prompt", {"sessionId": session_id,
                                               "contentBlocks": [{"type": "text", "text": text}]})["messageId"]

    def run_turn(self, session_id: str, text: str, on_event=None, on_tick=None,
                 timeout: float = 900.0, max_steps: int | None = None) -> TurnResult:
        """Send one prompt and block until its turn ends. `on_event(event)` sees
        each log event as it is recorded; `on_tick(elapsed_s)` fires ~4x/second.
        dsh has no step limit and no cancel, so `max_steps` is enforced here: when
        the turn starts step max_steps+1 it is abandoned (runtime restarted, session
        resumed) and a result with reason {"kind": "max_steps", "limit": N} is returned.
        Raises RuntimeDied if the runtime exits, TimeoutError past `timeout`."""
        while True:                                       # stale events of an earlier turn
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        q = self.events                                   # this turn's process, even if restarted
        start = time.monotonic()
        self.prompt(session_id, text)
        seen, calls, steps, errors, final = [], [], 0, 0, ""
        reason = {"kind": "unknown"}
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise TimeoutError(f"turn did not finish within {timeout:.0f}s")
            if on_tick:
                on_tick(elapsed)
            try:
                method, params = q.get(timeout=0.25)
            except queue.Empty:
                continue
            if method == "__exit__":
                raise RuntimeDied(self.diagnose(f"runtime exited (code {params.get('code')})"))
            if method != "session.event" or params.get("sessionId") != session_id:
                continue
            ev = params["event"]
            seen.append(ev)
            if self.approver:
                self.approver.observe(ev)
            d, t = ev.get("data") or {}, ev.get("type")
            if t == "step/start":
                steps += 1
                if max_steps and steps > max_steps:
                    steps -= 1                            # the step we refused to run
                    self.abandon(session_id)
                    reason = {"kind": "max_steps", "limit": max_steps}
                    return TurnResult(False, reason, final, steps, calls, errors,
                                      time.monotonic() - start, seen)
            elif t == "tool/call":
                args = d.get("arguments")
                try:
                    args = json.loads(args) if isinstance(args, str) else (args or {})
                except json.JSONDecodeError:
                    args = {"_raw": args}
                calls.append((d.get("name"), args))
            elif t == "tool/result" and (d.get("message") or {}).get("isError"):
                errors += 1
            elif t == "assistant/message":
                blocks = (d.get("message") or {}).get("content") or []
                txt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
                if txt:
                    final = txt
            if on_event:
                on_event(ev)
            if t == "turn/end":
                reason = d.get("reason") or {"kind": "unknown"}
                break
        return TurnResult(reason.get("kind") == "completed", reason, final, steps, calls,
                          errors, time.monotonic() - start, seen)

    def resume(self, session_id: str):
        self.request("session/resume", {"sessionId": session_id})

    def request(self, method: str, params: dict, timeout: float | None = 60.0):
        rid = self._new_id()
        proc = self._proc
        slot = {"done": threading.Event(), "result": None, "error": None, "proc": proc}
        with self._lock:
            self._pending[rid] = slot
        if proc is None or proc.poll() is not None:
            raise RuntimeDied(self.diagnose("runtime is not running"))
        self._send(proc, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot["done"].wait(timeout):
            raise TimeoutError(f"{method} got no response in {timeout}s. {self.diagnose('')}")
        if slot["error"]:
            if slot["error"].get("code") == "closed":
                raise RuntimeDied(self.diagnose(slot["error"]["message"]))
            raise RpcError(slot["error"].get("code"), slot["error"].get("message", "error"))
        return slot["result"]

    def diagnose(self, lead: str) -> str:
        tail = " | ".join(list(self.stderr_tail)[-6:])
        return (lead + (" -- runtime stderr: " + tail if tail else "")).strip()

    # -- plumbing ----------------------------------------------------------

    def _new_id(self) -> int:
        with self._lock:
            self._next += 1
            return self._next

    @staticmethod
    def _send(proc, obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def _read_stdout(self, proc, q):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue                                  # the transport ignores malformed lines too
            if "method" in msg and "id" not in msg:
                q.put((msg["method"], msg.get("params") or {}))
            elif "id" in msg:
                with self._lock:
                    slot = self._pending.pop(msg["id"], None)
                if slot:
                    slot["result"], slot["error"] = msg.get("result"), msg.get("error")
                    slot["done"].set()
        code = proc.wait()
        self._fail_pending(proc, f"runtime exited with code {code}")
        q.put(("__exit__", {"code": code}))

    def _read_stderr(self, proc):
        for line in proc.stderr:
            self.stderr_tail.append(line.rstrip()[:300])

    def _fail_pending(self, proc, why: str):
        with self._lock:
            mine = {k: v for k, v in self._pending.items() if v["proc"] is proc}
            for k in mine:
                del self._pending[k]
            slots = list(mine.values())
        for s in slots:
            s["error"] = {"code": "closed", "message": why}
            s["done"].set()
