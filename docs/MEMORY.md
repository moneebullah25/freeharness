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
Sticky chats are the **default for every provider** (Gemini, Qwen, DeepSeek, ChatGPT, Grok, Perplexity): the shim keeps ONE chat per
task and sends only what is new (the tool results or your next message, plus a one-line reminder of the tool format; the full rules
were sent once at the start). `--sticky none` (or `FREEHARNESS_STICKY=none`) switches it off: a fresh chat with the whole history on
every step, which is what used to pile up hundreds of chats on the site and re-sent a 60k-char prompt each time.

**Honest status.** Only **Gemini** has been measured live in this mode (2026-10-25: a 5-step task sent ~9K characters, the fresh-chat
equivalent ~39K, computed from earlier prompt sizes rather than a second run; the Gemini eval stayed 8/8 in 388 s instead of 532 s).
For every other provider sticky is **default-on but NOT live-verified**, and the "chat is full" wording below is from memory of each
site and unverified until it has been seen. `STICKY_VERIFIED` in `providers.py` now only records which providers were measured live;
it no longer decides the default.

**When a chat is continued, and when a new one starts.** The shim continues a chat while everything lines up: same browser tab and
page, the history it sent is an unchanged prefix of the new request, the next message is exactly the reply it returned, same tool
list. A session that merely **grows never** starts a new chat. A NEW chat (same site) starts only when:
- the site says the chat is full: the provider's `full_markers` (lowercase substrings of the site's own notice) appear in a short
  reply (<= 1,500 chars, no tool-call block), in an alert/banner on the page, or in the error of a failed ask, or the site reports a
  length/context error. The notice is never passed to the model as an answer; the same request is re-sent in the new chat;
- the history no longer matches (dsh compaction, an edited message, `/resume`, Ctrl-C), the page address or the browser session changed,
  the tool list changed, an error happened, or a client left mid-reply;
- a safety net trips: 400 requests or 600,000 characters in one chat (generous on purpose; not the normal way a chat ends).
Why it started a new chat is logged in `shim.log` ("chat not continued (<reason>)"). When a provider hits its limit and another takes
over, the new site gets the whole conversation from the harness's log.

**What a new chat is sent: a compact history.** `fit_messages` keeps system messages and the latest user message, and shortens
**older tool results** oldest first: the first 400 and last 200 characters stay, the middle becomes
`[... 8,214 chars omitted (of 8,814); re-run the command if you need it]`. The newest 5 results, and every result after the last
assistant message, stay **verbatim** (up to 40,000 characters each). Whole old messages are dropped only if that is still not enough.
This compaction (`compact_messages`) applies to EVERY fresh chat, whatever the provider's limits: a dsh resume, a history mismatch, a new session object, a chat-full signal, or failover to another provider. A 300,000-char history becomes a ~50,000-char first prompt. Only the extra budget trimming (`fit_messages`) waits for a learned limit (below).

**Prompt-size limits are learned, never assumed.** No provider has an assumed ceiling (`max_prompt` is a safety value of 2,000,000
characters). `<state>/limits.json` records per provider `known_good_chars` (largest message that got a real answer) and, only after a
failure with a length-looking signal on a FRESH chat at a size above anything that worked, `suspect_max_chars`. One failure is only a
suspicion: the shim retries once in a new chat at half that size; if that succeeds the limit is **confirmed** and a ceiling of 80% of
the failing size is used, with fresh-chat prompts aimed at 50% of it. Shown in `/health` (`providers.<key>.limits`) and
`freeharness doctor`. If a fresh chat still says "too long" and nothing can be shortened, the shim answers `context_length_exceeded`,
which makes dsh condense and retry once.

**dsh's own condensation** (`dsh-compaction-basic`, the tool-result pruner) is kept out of the way: `contextWindow` in
`data/dsh.patch.yml` is one constant, `&context_window 1000000` (per model overridable). Measured with the real dsh
(`tests/test_dsh_context_window.py`, a scripted 32-request session, no live site): at 1,000,000 every request still contains a file read
at step 1 verbatim (request sizes 1.6k -> 122k chars, no extra summary requests); at 80,000 dsh condensed, added 2 summary requests and the
file text vanished from the later requests. The old value 16,000 did **not** trigger condensation (dsh's trigger is
`min(0.8*W, W - output - 65,536)`, negative for 16,000, so it skips it): the blanking seen live came from the shim trimming at 60,000
characters, not from dsh.

**Auditing a run.** `SHIM_DEBUG_PROMPTS=<file>` makes the shim write the FULL text of the last 3 prompts it sent to `<file>` (rewritten each
time, owner-only) and one summary line per prompt (provider, fresh/sticky, size, first and last 300 characters) to `<file>.index`.
The prompts contain whatever the agent read; the shim token is never part of a prompt and is never written.

**Loop guard (off by default).** `--loop-guard` or `FREEHARNESS_LOOP_GUARD=1`: when the same tool call (name + arguments) returned the same
output 3+ times in the last 8 calls, the interactive client prints a warning, and the `oss run` stage engine ends that turn and sends a
repair turn that quotes the call and says it returned the same output each time and asks for the next DIFFERENT action (it never names
one; at most 2 such turns per stage turn). Off until the root fix above is confirmed live, so that it cannot mask a model that has lost its data.

Every prompt, reply, tool call and result is also written, as it happens, to a readable markdown file per session:
`freeharness transcript` lists them and `freeharness transcript <id>` prints one (`~/.local/share/freeharness/transcripts/`, owner-only,
tool results capped at 20,000 characters). It is the file to open or paste into any chat by hand. It contains whatever the agent read.

## Long sessions and prompt limits
dsh condenses old history itself when its context fills (compaction, inside the harness); with `contextWindow` at 1,000,000 that
effectively never starts early (see above), and a `context_length_exceeded` from the shim still makes it condense once and retry.
The shim itself trims only for a FRESH chat and only when a limit has been learned from the site (see above): older tool results are
shortened to head + tail with a marker, the newest are kept verbatim, whole old messages are dropped last. System/developer messages
and the latest user message are never touched. **Unit-tested only** (`fit_messages`, fake sessions, and the real dsh with a scripted
fake model in `tests/test_dsh_context_window.py`); not exercised with a live site. Verified with the real dsh and `tests/fake_openai.py` (own port, temp DSH_HOME, no browser; `tests/test_context_e2e.py`, skipped if
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
