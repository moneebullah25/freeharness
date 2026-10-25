# Sessions and long-term memory

Stop today, continue tomorrow. Everything here is local, stdlib-only, and runs through dsh.

## Sessions
- `freeharness --continue` resumes the latest session (with at least one turn) for the current folder that dsh still has on disk;
  otherwise it starts a new one. `--resume ID` and `/resume ID` work as before.
- `freeharness sessions [--all]` and `/sessions` list id, age, turns and a title taken from the first user message.
- The index is `<data>/sessions-index/index.json` (0600, atomic writes). The conversation itself is dsh's own session
  (`dsh-home/sessions/<id>`), restored by `session/resume`; the readable copy stays in `<data>/transcripts/<id>.md`.

## Scratchpad
Per-session key/value notes (`current_task`, `ready_for_testing`, `next_steps`, ...), at
`<data>/sessions-index/scratch/<id>.json`. The model reads and writes them with `scratch_get` / `scratch_set`. They are printed when
a session is resumed and sent once with the next prompt, so the model picks up where it stopped. `/scratch` shows them.

## Long-term memory
Typed facts: `user`, `feedback` (conventions, corrections), `project`, `reference`; scope `global` or `project` (the folder where
it was saved, inherited by sub-folders). Each has source session, created/updated times and a `pinned` flag (default on for
`feedback` and `user`). Store: sqlite at `<data>/memory/memory.sqlite3`; search uses FTS5 when the sqlite build has it, else BM25
in Python. Saving a fact that matches an existing one (same normalised text, or Jaccard >= 0.75 within the same type and scope)
updates it instead of adding a copy. `memory_save` refuses text that looks like a token, key or password (heuristic, not a guarantee).

### How the agent uses it
1. `freeharness setup` enables the MCP server `freeharness.mcp.memory` (overlay row `<config>/overlay.d/memory.yml`), tools
   `mcp__memory__memory_save|memory_recall|memory_forget|scratch_get|scratch_set` (5 tools; `mcp-resources` is disabled, which
   the vault row also does; dsh tolerates both). `freeharness memory enable|disable` toggles it.
2. A managed block in `dsh-home/AGENTS.md` tells the model to recall at task start and save stated conventions, and lists the
   top pinned facts for the current folder (capped at 1500 characters). The client refreshes it before every launch and after
   `/remember` or `memory add|forget`. The block is global to the dsh home, so with two folders running at once the last one started wins.
3. `/remember <text>` and `freeharness memory add|list|search|forget` manage facts by hand.
4. `freeharness memory extract <session>` scans the user's messages in a transcript for "always", "never", "from now on",
   "remember that" and similar, and prints proposals. It saves nothing unless `--yes`.
5. `freeharness memory export [file]` and `memory wipe` ask for confirmation (`--yes` skips; without a terminal they refuse).

Privacy: see [PRIVACY.md](PRIVACY.md). Memory stays on this machine except as text in prompts to your chosen chat site.

## What is tested, honestly
- Real dsh with a scripted model on a free port (`tests/test_memory_dsh.py`, skipped if the harness is not installed): a fact is
  saved in session A, the runtime is closed, and a brand-new session B in a new runtime recalls it and sees it in its first prompt via
  AGENTS.md; the scratchpad lands under the right session; resuming A in a third runtime replays the earlier turns and tool history;
  vault and memory enabled together both load.
- Unit tests only (no real dsh): store, dedupe, FTS and BM25 paths, secret refusal, the MCP server over stdio, extraction heuristics,
  CLI confirmation, and the client REPL paths (`--continue`, `/sessions`, `/remember`, `/scratch`, `/new`, `/resume`) against a fake runtime.
- Not verified: real model behaviour. Whether Gemini or Qwen actually calls `memory_save` / `memory_recall` when instructed has not been
  measured; the scripted model only proves the plumbing. The always-on pinned block does not depend on the model calling anything.
- Extraction is regex-based and conservative; it will miss implicit preferences and may propose sentences you do not want.
