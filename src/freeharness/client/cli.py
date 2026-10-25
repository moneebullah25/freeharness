"""
freeharness: the terminal client for DeepSeek Harness running on free web-chat
backends (chatgpt-bridge/shim.py).

    freeharness                      # interactive, in the current directory
    freeharness -p "fix the failing test"      # one turn, then exit
    freeharness --resume term-20261024-211500  # continue a saved session
    freeharness --continue                     # continue the latest session for this folder
    freeharness --model gemini --providers gemini,perplexity

Inside: /help /model /providers /new /resume /sessions /remember /scratch /status /quit.
Ctrl-C while a turn runs abandons it (restarts the runtime and resumes the
session: the protocol has no cancel). The model may keep generating in the
background browser for a few seconds, and the next request waits for it.
"""
from __future__ import annotations

import argparse
import sys
import time

from rich.console import Console
from rich.rule import Rule

from .. import memory, memory_cmd, paths, sessions
from ..transcript import Transcript
from .approval import TerminalApprover
from .render import Renderer, turn_outcome
from .rpc import (DSH_HOME, Runtime, RuntimeDied, RpcError, ensure_shim,
                  shim_health)

MODELS = ("auto", "gemini", "perplexity", "qwen", "grok", "chatgpt", "deepseek")
HELP = """\
[bold]/help[/]               this list
[bold]/model[/] [dim][name][/]    show or switch the model: auto (rotate), gemini, perplexity, qwen, grok
[bold]/providers[/]          which web chat sites are ready or cooling down
[bold]/new[/]                start a fresh session (empty context)
[bold]/sessions[/]           saved sessions            [bold]/resume[/] [dim]<id>[/]  continue one
[bold]/remember[/] [dim]<text>[/]  save a convention or fact to long-term memory
[bold]/scratch[/]            this session's scratchpad (task, ready for testing, next steps)
[bold]/status[/]             session, model, workspace
[bold]/quit[/]               leave (also Ctrl-D)
Ctrl-C during a turn abandons it."""


def new_session_id() -> str:
    return "term-" + time.strftime("%Y%m%d-%H%M%S")


def saved_sessions() -> list[str]:
    root = DSH_HOME / "sessions"
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if p.name.startswith("term-")), reverse=True)


class App:
    def __init__(self, args, console: Console):
        self.args, self.c = args, console
        # Only a person at a terminal can approve (y/N, default no); `-p` has nobody to ask, so dsh refuses.
        self.approver = None if args.prompt else TerminalApprover(console)
        self.rt = Runtime(args.cwd, model=args.model, approver=self.approver)
        self.sid = args.resume or self.continue_id() or new_session_id()
        self.shim_proc = None
        self.resumed = bool(args.resume) or self._continued
        self.preamble = ""
        self.has_history = self.resumed
        self.tr: Transcript | None = None

    def continue_id(self) -> str | None:
        self._continued = False
        if getattr(self.args, "cont", False):
            sid = sessions.latest_for_cwd(self.rt.cwd, set(saved_sessions()))
            if sid:
                self._continued = True
                return sid
        return None

    def load_scratch(self) -> str:
        """The scratchpad as a note for the model (sent once, with the next prompt)."""
        try:
            pad = sessions.render_scratch(self.sid)
        except ValueError:
            return ""
        return ("[Your scratchpad from earlier in this session, restored after a restart:\n" + pad + "]\n\n") if pad else ""

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if not self.args.no_shim:
            with self.c.status("starting the model shim…"):
                self.shim_proc = ensure_shim(self.args.providers, sticky=self.args.sticky)
        try:
            memory_cmd.refresh(self.rt.cwd)         # pinned conventions for this folder -> AGENTS.md
            sessions.touch(self.sid, self.rt.cwd, self.rt.model)
            sessions.set_active(self.rt.cwd, self.sid)
        except (OSError, ValueError) as e:
            self.c.print(f"[yellow]memory/session index unavailable:[/] {e}")
        with self.c.status("starting dsh…"):
            self.rt.start()
            if self.resumed:
                self.rt.resume(self.sid)
        if self.resumed:
            self.preamble = self.load_scratch()
        pad = sessions.render_scratch(self.sid) if self.resumed else ""
        if pad:
            self.c.print(f"[dim]scratchpad:[/]\n{pad}")

    def stop(self):
        if self.tr:
            self.tr.close()
        self.rt.close()
        if self.shim_proc and self.shim_proc.poll() is None:
            self.shim_proc.terminate()          # the shim's SIGTERM handler closes its browsers
            try:
                self.shim_proc.wait(timeout=8)
            except Exception:                   # noqa: BLE001
                self.shim_proc.kill()

    # -- one turn ----------------------------------------------------------

    def turn(self, text: str) -> bool:
        rend = Renderer(self.c)
        if self.tr is None or self.tr.path.stem != self.sid:        # /new and /resume change the session
            if self.tr:
                self.tr.close()
            self.tr = Transcript(self.sid, self.rt.cwd)
        self.tr.user(text)
        send, self.preamble = self.preamble + text, ""
        label = {"text": "waiting for the model…"}
        started = time.monotonic()
        with self.c.status(label["text"]) as status:
            if self.approver:
                self.approver.status = status               # paused while the approval question is asked
            def on_event(ev):
                self.tr.event(ev)
                new = rend.handle(ev)
                if new:
                    label["text"] = new

            def on_tick(elapsed):
                status.update(f"{label['text']}  [dim]{elapsed:.0f}s[/]")

            try:
                result = self.rt.run_turn(self.sid, send, on_event=on_event, on_tick=on_tick,
                                          timeout=self.args.turn_timeout, max_steps=self.args.max_steps)
            except KeyboardInterrupt:
                status.stop()
                self.c.print("[yellow]interrupted[/] – restarting the runtime and resuming the session…")
                self.recover()
                return False
            except (RuntimeDied, TimeoutError, RpcError) as e:
                status.stop()
                self.c.print(f"[red]runtime problem:[/] {e}")
                self.recover()
                return False
        try:
            entry = sessions.get(self.sid) or {}
            sessions.record_turn(self.sid, self.rt.cwd, "" if entry.get("title") else text, self.rt.model)
            sessions.set_active(self.rt.cwd, self.sid)
        except (OSError, ValueError):
            pass
        ok, why = turn_outcome(result.reason)
        health = shim_health() if not self.args.no_shim else None
        note = (f"{result.steps} step(s) · {len(result.tool_calls)} tool call(s) · "
                f"{time.monotonic() - started:.0f}s · {self.sid}")
        self.c.print(Rule(f"[{'green' if ok else 'red'}]{why}[/] [dim]{note}[/]", style="dim"))
        if not ok:
            self.hint(health)
        return ok

    def recover(self):
        try:
            self.rt.abandon(self.sid)
        except Exception as e:                  # noqa: BLE001
            self.c.print(f"[red]could not recover:[/] {e}\nStart a new session with /new.")

    def hint(self, health):
        if not health:
            return
        down = {k: v for k, v in health["providers"].items() if not v["ready"]}
        for k, v in down.items():
            self.c.print(f"[yellow]{k} is cooling down for {v['cooldown_s']}s[/] [dim]({v['reason']})[/]")

    # -- slash commands ----------------------------------------------------

    def command(self, line: str) -> bool:
        """True to keep going, False to quit."""
        cmd, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd == "help":
            self.c.print(HELP)
        elif cmd == "model":
            if not arg:
                self.c.print(f"model: [bold]{self.rt.model}[/]   choices: {', '.join(MODELS)}")
            elif arg not in MODELS:
                self.c.print(f"[red]unknown model {arg!r}[/]; choose {', '.join(MODELS)}")
            else:
                with self.c.status(f"switching to {arg}…"):
                    self.rt.restart(model=arg)
                    self.rt.resume(self.sid) if self.has_history else None
                self.c.print(f"model: [bold]{arg}[/]")
        elif cmd == "providers":
            h = shim_health()
            if not h:
                self.c.print("[red]the shim is not answering[/]")
            for k, v in (h or {}).get("providers", {}).items():
                state = "[green]ready[/]" if v["ready"] else f"[yellow]cooling {v['cooldown_s']}s[/] {v['reason']}"
                self.c.print(f"  {k:11} {state}")
        elif cmd == "new":
            self.sid, self.has_history, self.preamble = new_session_id(), False, ""
            sessions.set_active(self.rt.cwd, self.sid)
            self.c.print(f"new session [bold]{self.sid}[/]")
        elif cmd == "sessions":
            items = sessions.list_sessions(None if arg == "all" else self.rt.cwd, 15)
            for line in sessions.format_list(items) or ["(none yet; /sessions all shows every folder)"]:
                self.c.print(f"  {line}", markup=False)
        elif cmd == "remember":
            if not arg:
                self.c.print("[red]usage:[/] /remember <text>")
            else:
                store = memory.MemoryStore()
                try:
                    f = store.save(arg, "feedback", "global", self.rt.cwd, session=self.sid)
                except ValueError as e:
                    self.c.print(str(e), markup=False)
                    return True
                finally:
                    store.close()
                memory_cmd.refresh(self.rt.cwd)
                self.c.print(f"{f['action']}: {memory.format_fact(f)}", markup=False, highlight=False)
                self.c.print("[dim]pinned in AGENTS.md from the next session; the agent can recall it now[/]")
        elif cmd == "scratch":
            self.c.print(sessions.render_scratch(self.sid) or "(scratchpad is empty)", markup=False)
        elif cmd == "resume":
            if not arg:
                self.c.print("[red]usage:[/] /resume <session id>")
            else:
                try:
                    self.rt.resume(arg)
                    self.sid, self.has_history = arg, True
                    sessions.touch(arg, self.rt.cwd, self.rt.model)
                    sessions.set_active(self.rt.cwd, arg)
                    self.preamble = self.load_scratch()
                    self.c.print(f"resumed [bold]{arg}[/]")
                except RpcError as e:
                    self.c.print(f"[red]{e}[/]")
        elif cmd == "status":
            self.c.print(f"session [bold]{self.sid}[/] · model [bold]{self.rt.model}[/] · cwd {self.rt.cwd}")
        else:
            self.c.print(f"[red]unknown command /{cmd}[/]  (try /help)")
        return True


    # -- repl --------------------------------------------------------------

    def repl(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        hist = paths.private_dir(paths.state_dir()) / "history"
        session = PromptSession(history=FileHistory(str(hist)))
        self.c.print(f"[bold]freeharness[/] · {self.rt.cwd}\nsession {self.sid} · model {self.rt.model} · /help")
        while True:
            try:
                line = session.prompt("› ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line.startswith("/"):
                if not self.command(line):
                    break
                continue
            self.has_history = True
            self.turn(line)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--prompt", help="run one turn and exit")
    ap.add_argument("--cwd", default=".", help="workspace (default: current directory)")
    ap.add_argument("--model", default="auto", choices=MODELS)
    ap.add_argument("--providers", default="gemini,qwen",
                    help="providers for a shim this client starts: comma-separated, or 'all' (default: %(default)s)")
    ap.add_argument("--sticky", default="auto",
                    help="providers that keep ONE chat per task and send only what is new: 'auto' (those verified by the eval: "
                         "gemini), 'all', 'none', or a comma-separated list (default: auto)")
    ap.add_argument("--max-steps", type=int, default=60, metavar="N",
                    help="abandon a turn that starts more than N model steps; 0 = no limit (default: %(default)s)")
    ap.add_argument("--turn-timeout", type=float, default=900.0, metavar="S",
                    help="abandon a turn that runs longer than S seconds (default: %(default)s)")
    ap.add_argument("--no-shim", action="store_true", help="do not start or check the shim")
    ap.add_argument("--resume", metavar="ID", help="resume a saved session")
    ap.add_argument("--continue", dest="cont", action="store_true",
                    help="resume the latest session for this folder (starts a new one if there is none)")
    args = ap.parse_args(argv)

    console = Console()
    app = App(args, console)
    try:
        app.start()
        if args.prompt:                      # exit 0 turn completed, 1 turn failed/abandoned, 2 could not run
            return 0 if app.turn(args.prompt) else 1
        app.repl()
        return 0
    except RuntimeError as e:
        console.print(f"[red]{e}[/]")
        return 2
    except (RuntimeDied, RpcError, TimeoutError) as e:
        console.print(f"[red]{e}[/]")
        return 2
    finally:
        app.stop()


if __name__ == "__main__":
    sys.exit(main())
