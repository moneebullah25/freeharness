"""
freeharness: a terminal coding agent (DeepSeek Harness) on free web-chat backends.

    freeharness                      interactive session in the current directory
    freeharness -p "task"            one turn, then exit
    freeharness setup                install Node + the pinned harness (once)
    freeharness login [site ...]     sign in to chat sites (once)
    freeharness doctor [--report]    check prerequisites
    freeharness serve                run only the OpenAI-compatible shim
    freeharness eval --model NAME    gate a provider with the tool-use eval
    freeharness transcript [id]      list / print the readable record of past sessions
    freeharness vault <path>         connect a notes vault as read-only memory (--off to remove)
    freeharness --continue           resume the latest session for this folder
    freeharness sessions [--all]     list sessions (title, age)
    freeharness memory ...           long-term memory: list | add | search | forget | extract | export | wipe
    freeharness agents ...           open-source agent skills: install [--set oss|all] | list | remove
    freeharness version
"""
from __future__ import annotations

import sys

from . import __version__

SUBCOMMANDS = ("setup", "login", "doctor", "serve", "eval", "review", "vault", "transcript", "memory", "sessions", "agents", "version")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv and argv[0] in SUBCOMMANDS else None
    rest = argv[1:] if cmd else argv
    if cmd == "version":
        print(f"freeharness {__version__}")
        return 0
    if cmd == "setup":
        from .setup_cmd import main as run
        return run(rest)
    if cmd == "login":
        from .login_cmd import main as run
        return run(rest)
    if cmd == "transcript":
        from .transcript import main as run
        return run(rest)
    if cmd == "memory":
        from .memory_cmd import main as run
        return run(rest)
    if cmd == "sessions":
        from .sessions import main as run
        return run(rest)
    if cmd == "agents":
        from .agents_cmd import main as run
        return run(rest)
    if cmd == "vault":
        from .vault_cmd import main as run
        return run(rest)
    if cmd == "doctor":
        from .doctor import main as run
        return run(rest)
    if cmd == "serve":
        from .backends import shim
        sys.argv = ["freeharness serve", "serve", *rest]
        shim.main()
        return 0
    if cmd == "eval":
        from .eval.run import main as run
        return run(rest) or 0
    if cmd == "review":
        from .eval.review import main as run
        return run(rest)
    from .client.cli import main as chat
    return chat(rest)


if __name__ == "__main__":
    sys.exit(main())
