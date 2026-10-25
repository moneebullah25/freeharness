# Context, memory, skills and MCP

## What the agent knows at the start of a turn
1. **Instructions**: `AGENTS.md` / `CLAUDE.md` (+ `.local` variants) from the project root, and a user-global
   `<data>/dsh-home/AGENTS.md` (`~/.local/share/freeharness/dsh-home/AGENTS.md` on Linux). Loaded on the first request of a session.
2. **The session so far**: replayed from the session log; older history is condensed (compaction) when the context fills.
3. **Tool descriptions**: every enabled tool adds text to *every* prompt, which is why the toolset is small (11 tools).

There is no automatic long-term memory or self-improvement: the agent only "remembers" what is in those files, in a skill, in
the vault, or in the session log. It can write project files (including a project `AGENTS.md` or a skill) when you ask; it cannot
write to the user-global folders or the vault (sandbox + read-only server).

## Notes vault as memory: `freeharness vault <path>`
```bash
freeharness vault ~/Desktop/Muneeb/MuneebVault     # connect (restart any running freeharness)
freeharness vault                                   # status
freeharness vault --off                             # disconnect
```
This installs a read-only MCP server (`freeharness.mcp.vault`) with two tools, plus a short instruction block in the global `AGENTS.md`:

- `vault_search(query, limit)`: BM25 ranking over every `.md` note (title/filename boosted, `wiki/` above `raw/`), returning path,
  best-matching lines and the note's `[[links]]` in one call.
- `vault_read(path | [[link name]], offset)`: one note, paged.

**Why not embeddings/RAG.** The expensive thing on free chat sites is the *number of model steps* (15-120 s each), not retrieval
accuracy. Grep-then-read is 3-5 steps; one ranked search that already shows the snippets and links is 1-2. For a vault of tens to
hundreds of linked notes, keyword ranking plus the link graph finds what a vector index would, with no embedding model, no index to
rebuild, no stale vectors, and no extra dependency. If a vault grows to thousands of notes with vague wording, add an embedding
retriever behind the same two tool names; nothing else changes.

Safety: the server is read-only, resolves every path and refuses anything outside the vault (including symlinks and `..`), indexes
only `.md` files under 1 MB and never writes. The instruction block tells the model that notes are reference material, not
commands. Be aware the *content you retrieve is sent to the chat site* you are using, like everything else in the prompt.

Measured (2026-10-25, Gemini): a question answerable only from the vault, asked from an unrelated folder, was answered correctly in
3 steps / 71 s (`vault_search` then `vault_read`).

## Skills
A skill is `<name>/SKILL.md` (YAML frontmatter `name`, `description`, then instructions) in `<project>/.dsh/skills`,
`<project>/.agents/skills`, `<data>/dsh-home/skills` or `~/.agents/skills`. The model sees only the name and description until it
calls the `skill` tool, so skills are cheap to keep around. Measured: a project skill defining an output format was loaded and followed.
You write them (or ask the agent to write one into the project); nothing creates skills automatically.

## More MCP servers
`freeharness` loads every `<config>/overlay.d/*.yml` as an extra harness patch. To add a server, drop a file there:
```yaml
- insert:
    - id: mcp-github
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: github
        transport: stdio
        command: npx
        args: ['-y', '@modelcontextprotocol/server-github']
        env:
          GITHUB_TOKEN: !!js process.env.GITHUB_TOKEN
```
A plain row (without `insert:`) only *patches* an existing entry and is skipped with "entry not found". Each server's tools are
named `mcp__<serverName>__<tool>` and each adds prompt text, so add few. Third-party servers run with your permissions: review them.

## One chat per task ("sticky" chats) and the readable record
By default every model step used to open a fresh chat on the site and resend the whole history. Now, for providers that passed the
eval in this mode (**Gemini**), the shim keeps ONE chat per task and sends only what is new (the tool results or your next message,
plus a one-line reminder of the tool format; the full rules were sent once at the start). Measured 2026-10-25: a 5-step task sent
~9K characters (the fresh-chat equivalent, ~39K, is computed from the prompt sizes of earlier runs, not a second run), and the Gemini eval stayed 8/8 while taking 388 s instead of 532 s.

The shim continues a chat only if everything still lines up: same browser tab and page, the history it sent is an unchanged prefix
of the new request, the next message is exactly the reply it returned, same tool list, and the chat has taken fewer than 20 steps
and 60,000 characters. Anything else (Ctrl-C, `/resume`, compaction, an error, a client that left mid-reply) starts a **fresh chat
with the full history**, which is also what happens when a provider hits its limit and another takes over: the new site gets the
whole conversation from the harness's log. Control it with `--sticky auto|none|all|gemini,qwen`.

Every prompt, reply, tool call and result is also written, as it happens, to a readable markdown file per session:
`freeharness transcript` lists them and `freeharness transcript <id>` prints one (`~/.local/share/freeharness/transcripts/`, owner-only,
tool results capped at 20,000 characters). It is the file to open or paste into any chat by hand. It contains whatever the agent read.

## Long sessions and prompt limits
dsh condenses old history itself when its context fills (compaction, inside the harness). On top of that the shim now guards each
provider's `max_prompt`: if the rendered prompt is too big it first replaces the oldest tool-result bodies with
`[older output omitted to fit the provider's prompt limit]` (newest kept), then drops the oldest whole messages (one
`[earlier conversation omitted ...]` marker). System/developer messages and the latest user message are never touched. Only if that
still does not fit does the shim return `context_length_exceeded` (PromptTooLarge). **Unit-tested only** (`fit_messages`, and an HTTP
test with a fake provider); it has not been exercised with the real dsh or a live site. Verified with the real dsh and `tests/fake_openai.py` (own port, temp DSH_HOME, no browser; `tests/test_context_e2e.py`, skipped if
dsh/node is missing): the captured request contains project `AGENTS.md`, the global `dsh-home/AGENTS.md`, the skill name and
description, the `skill` tool and `mcp__vault__vault_search`/`vault_read`; after `session/resume` in a fresh runtime the request replays
the earlier user message before the new one; a transcript appended across a resume has one header and both turns in order (that part
drives the `Transcript` class directly, not the CLI). NOT covered: the model actually calling a vault tool (the fake never does), dsh's
own compaction, and the trimming against a live site.

## Where things are stored
| What | Where (Linux) |
|---|---|
| Session transcripts | `~/.local/share/freeharness/dsh-home/sessions/<workspace>/<session>/session.v4.jsonl.zstd` |
| Readable transcripts | `~/.local/share/freeharness/transcripts/<session id>.md` |
| Global instructions | `~/.local/share/freeharness/dsh-home/AGENTS.md` |
| Global skills | `~/.local/share/freeharness/dsh-home/skills/` |
| MCP / extra overlays | `~/.config/freeharness/overlay.d/*.yml` |
| Shim token | `~/.config/freeharness/shim-token` |
| Logs, prompt history, eval results | `~/.local/state/freeharness/` |
| Copies of your prompts | the chat sites' own conversation histories |
