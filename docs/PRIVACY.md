# Privacy

freeharness has **no telemetry** and sends nothing to its authors.

What it does send, and where:
- Your prompts, tool results and file excerpts the agent decides to read go to the **chat sites you
  enabled** (Gemini, Qwen, ...), exactly as if you pasted them into those sites' web UIs, under your
  accounts and those sites' privacy terms. Conversations appear in those accounts' histories.
- `freeharness setup` downloads Node from nodejs.org and the harness from the npm registry.

What it stores locally (per user, owner-only permissions):
| Data | Location (Linux) |
|---|---|
| Browser login state | `~/.local/share/freeharness/chrome-profile/` |
| Agent sessions (full transcripts) | `~/.local/share/freeharness/dsh-home/` |
| Access token | `~/.config/freeharness/shim-token` |
| Logs (sizes/outcomes only, rotated) | `~/.local/state/freeharness/logs/` |
| Eval results | `~/.local/state/freeharness/eval/` |

`FREEHARNESS_HOME=/some/dir` moves all of it under one directory. Delete that directory to remove
everything; sign out in the chat sites to revoke the sessions.
