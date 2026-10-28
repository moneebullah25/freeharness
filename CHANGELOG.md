# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). While the major version is 0, minor
releases may include breaking changes; they will be listed under **Changed**.

## [Unreleased]

### Changed
- New chats are capped at 6 per provider per hour (`FREEHARNESS_MAX_NEW_CHATS_PER_HOUR`, 0 = no cap), counted in `<state>/chats.json` so a restart does
  not reset it; the shim logs a warning from the second new chat. A fresh chat gets a digest of older steps plus the newest 40 messages.
- The `oss run` hunt stage now also requires `Documented:`, `Quote:`, `Intent checked:` and `Valid input because:` in the candidate
  (misuse of an API is not a bug). The quote must appear in the cited file or in a tool result of the stage, and `Intent checked:` must name
  a file in the clone: a comment such as "nesting takes precedence" can no longer be skipped without a trace.

- The `oss run` hunt check `repro_run` also accepts a run whose output lacks a number-like value the candidate says was expected (a dropped
  value shows no traceback), and an environment error (missing module) is reported as the reason only when the latest run had it, not an
  older one fixed since.

- ChatGPT is now told, before the first message of a chat (and by a one-line reminder on later ones), that the tools are real and run on
  the user's own machine through the harness. Without it ChatGPT answered "the gh_* tools aren't exposed in the current tool interface"
  and called nothing (new `Provider.tool_preamble` / `tool_reminder`; no other provider's prompt changes).

- A sticky request that the client drops while the site is still answering (dsh gives up on a silent request) no longer costs a new
  chat: the shim keeps the chat (the site's answer is not stopped), and when the same request comes back it takes over the answer
  already on the page (`Session.adopt`); if nothing arrived it sends the message again in the SAME chat. dsh's own idle limit for the
  bridge provider is now 1500s instead of 300s (`streamIdleTimeoutMs`). The prompt debug log records what is typed into the site.
  A reply that never starts ("no reply started within Ns") leaves the message in its chat the same way, and a cooldown of at most 300s is
  waited out in place when the request has a chat to resume (dsh retries at once and would otherwise fail the stage). ChatGPT waits up
  to 600s for a reply to start (it thinks first) and 420s for it to finish.

- ROOT CAUSE of the dropped requests: with tools the shim held the whole HTTP response until the reply was ready, and dsh's HTTP client gives
  up on a response without headers after 300s (the drops came at 301s). A request still waiting after 15s now gets its headers and an SSE
  keepalive comment (`: keepalive`) every 15s; fast replies are unchanged. Checked against real dsh (tests/test_shim_dsh_keepalive.py).

- A "message too long / chat is full" notice on the page now ends the wait for a reply at once (it used to wait the full reply timeout:
  ChatGPT shows the notice and never answers an oversize message).

- A tool call written as `{"tool": "read", "file_path": ...}` (ChatGPT's own variant; the key `tool`, `tool_name` or `function` instead of
  `name`, arguments beside it) is read as a call when it names a real tool; before, three such calls were dropped and the turn ended as
  plain text. The shim now also logs a WARNING when a reply mentions `tool_call` but no call could be read from it.

### Added
- `freeharness oss run <owner/repo> --from-issues [--label bug]`: instead of hunting in the code, the model lists the repo's open issues with a
  label (new read-only gate tools `gh_list_issues`, `gh_issue_read`), picks ONE that is open and unassigned, reproduces it itself,
  and the usual dupcheck, fix and PR stages follow. No new issue is filed (the issue stage adopts the existing one). New checks:
  `issue_chosen` (the issue was read in the hunt stage, is open and unassigned), judge item `issue_adopted`; `issue_draft` and
  `issue_posted` are n/a.
- One chat per task is now the default for EVERY provider (`--sticky auto` = all; `--sticky none` is the off switch). Only Gemini is
  live-verified. A chat ends on the site's own "full" signals (new per-provider `full_markers`), a history mismatch or an error, not
  on a step/char cap (now safety nets: 400 requests / 600,000 chars); the shim no longer pre-trims the harness's messages.
- No assumed prompt limits: `max_prompt` is a 2,000,000-char safety value; real limits are learned from site failures and kept in
  `<state>/limits.json` (`/health`, `freeharness doctor`). The InterviewCopilot 100k cut no longer applies to shim prompts.
- A history for a fresh chat keeps the head and tail of older tool results with an "N chars omitted" marker instead of blanking them;
  the newest results stay verbatim. `contextWindow` in `data/dsh.patch.yml` is now one constant of 1,000,000 tokens.

### Added
- `SHIM_DEBUG_PROMPTS=<file>`: the last 3 full prompts sent (owner-only) plus a one-line-per-prompt index.
- Loop guard (on by default; `--no-loop-guard` / `FREEHARNESS_LOOP_GUARD=0` turns it off): `loopguard.py`.
- Staged workflow engine for `freeharness oss run` (new `oss_flow.py`, stage checks in `eval/oss_stage.py`): the default is now six
  stages (setup, hunt, dupcheck, issue, fix, pr), each ONE short prompt on the same dsh session, built from `state.json`, with a
  step budget and a deterministic acceptance check (oss_judge items plus stage checks: a recorded repro run whose output shows the
  failure, per-candidate search evidence and a `DECISION:` line checked against the model's own search results, a new test that
  FAILED before the edit and PASSED after as recorded bash calls, a diff touching a source and a test file). Only the unmet items of
  the stage go back to the model, up to 2 repair turns (`--repair N`), then the run advances or ends `blocked at <stage>: <reason>`.
  A duplicate or competing PR ends `blocked: duplicate` (exit code 3) unless the model picks another candidate (max 2); the harness
  shows the model the titles of its own overlapping search results once. State is checkpointed after every turn (`state.json`,
  `stages/NN-<id>.jsonl`); `freeharness oss run --resume <run-id>` continues at the first unfinished stage on the same session and
  workspace (`--max-steps N` replaces the budget). Model fallback keeps the stage state. `oss report` shows per-stage status
  (passed / repaired n times / blocked), repair turns and steps before the unchanged checklist. `--single-turn` keeps the old
  one-prompt run. The harness never writes drafts, bug text, fixes, queries or PR text. Offline tests: `tests/test_oss_flow.py`,
  `tests/test_oss_flow_dsh.py`; docs/OSS_EVAL.md.
- GitHub gate tools `gh_branch` (server-side fetch of upstream + `checkout -b` from its default branch; refuses the default
  branch name, unsafe names, a dirty tree, an existing branch) and `gh_setup_env` (a per-project venv outside the clone from the
  repo's own declared test/lint dependencies, read from upstream's committed files; unsafe lines skipped and listed; pip runs
  inside a bubblewrap sandbox with network and nothing else, timeout and output cap, fail closed without bwrap; returns the exact
  command prefix to run pytest/ruff in bash). The gate now has 14 tools. New modules `depfiles.py`, `toolchain.py`.
- Standard toolchain: `freeharness toolchain install|status` (also run by `setup`) builds `<data>/toolchain-venv` with pinned
  pytest, coverage, ruff, flake8, black, isort, mypy, pre-commit, pip, setuptools, wheel, build. The sandbox runner binds it
  read-only and puts its `bin/` first on PATH, so bash has `python`, `python3` and these tools with no per-run install; the Node
  dsh runs on (when under `$HOME`) is exposed read-only the same way (`FH_SANDBOX_TOOLS` for more). `doctor` reports
  `standard toolchain: full | partial | missing` from real runs inside the sandbox.
- OSS judge: `lint_tests_run` recognises `pre_commit` and `coverage run -m pytest` through the venv prefix (evidence tests added).
- OSS-contribution evaluation: `freeharness oss run <owner/repo> [--component NAME] [--models a,b] [--live] [--max-steps N]`,
  `oss report <run-id> [--judge]`, `oss list`, and `freeharness eval --suite oss` (lists it, never runs it). One run directory
  `<state>/oss/<run-id>/` (prompt, config, clone workspace, the gate's evidence and drafts, transcript, `calls.jsonl`, live
  `supervisor.jsonl`, `reviews.jsonl`, `run.json`, `report.md`); the gate overlay is passed to that run's runtime only. Dry-run is
  the default and needs no terminal (new `freeharness.mcp.ossgate`: the normal gate plus placeholder issue numbers so a dry-run PR
  can be validated); `--live` needs a terminal and y/N. `eval/oss_judge.py` is a deterministic 16-item checklist (pass/fail/na with
  evidence) over the gate's own files, the tool-call record and read-only git; overall status via `review.final_assessment`.
  docs/OSS_EVAL.md, tests/test_oss_eval.py, tests/test_oss_eval_dsh.py.
- `freeharness agents install [--set oss|all] [--ref SHA] | list | remove`: installs open-source Agency agent skills
  (msitarzewski/agency-agents, MIT) into the dsh home by running its own `convert.sh`/`install.sh --tool dsh` from a fresh clone
  pinned to a full commit SHA (HEAD verified, scripts checksummed, scrubbed environment, `--path` forced). Default set `oss` is 9
  curated agents (listing ~2.8k chars instead of ~74k for all 284); `--set all` warns and needs `--yes`. Also installs the bundled
  `oss-contribution` skill (14-step OSS pipeline for the `gh_*` tools). docs/AGENTS_SKILLS.md, tests/test_agents_cmd.py.
- Session index (`sessions.py`): `freeharness --continue` resumes the latest session for the current folder, `freeharness sessions`
  and `/sessions` list title and age, per-session scratchpad shown and re-injected after resume.
- Long-term memory: typed facts (user | feedback | project | reference, global or per folder) in a local sqlite file with
  FTS5/BM25 search, dedupe-by-update, secret refusal, `freeharness memory list|add|search|forget|extract|export|wipe|enable|disable`,
  `/remember`, and the stdio MCP server `freeharness.mcp.memory` (memory_save, memory_recall, memory_forget, scratch_get,
  scratch_set), enabled by `freeharness setup`. Pinned conventions are rendered into the managed AGENTS.md block before each run.
- docs/SESSIONS_AND_MEMORY.md; memory section in docs/PRIVACY.md.
- Stage 1 security baseline: bash runs in a bubblewrap sandbox with no network and `$HOME`, `/tmp`, `/run`
  hidden; read/grep/glob are fenced to the workspace; sandbox escalation needs a human (`y/N`, default no) and
  `-p` refuses it; `freeharness doctor` reports the bash sandbox as full/partial/unavailable from real probes.
  Same-kernel confinement, not a container. Offline tests in `tests/test_security.py`.

- GitHub gate: `freeharness github enable [--live] | disable | status` and the stdio MCP server `freeharness.mcp.ghgate`
  (`gh_repo_info`, `gh_search`, `gh_get_file`, `gh_list_pr_files`, `gh_pr_threads`, `gh_fork`, `gh_clone`, `gh_push`,
  `gh_issue_create`, `gh_pr_create`, `gh_pr_reply`, `gh_pr_resolve_thread`). Contribution rules are enforced in code:
  human review of every draft (approve / reject / request changes) over the approval socket, dry-run by default, no force
  pushes, no default-branch pushes, search evidence before issues, fail-closed competing-PR check, attribution-marker and
  artifact refusals, token never printed or stored. `freeharness doctor` reports it; the client prints a banner when live.
  See docs/GITHUB_GATE.md.

### Changed
- The approval socket accepts a second message type (`confirm`, used by the GitHub gate); the sandbox approval protocol is unchanged.
- Without bubblewrap (or off Linux) bash tool calls are refused instead of run unconfined.

## [0.1.0] - 2026-10-25

First release.

### Added
- Terminal client (`freeharness`) driving a pinned DeepSeek Harness 0.2.1-rc.2 over its SDK protocol,
  with interrupt-and-resume (Ctrl-C), saved sessions, `/model`, `/providers`, `/resume`.
- OpenAI-compatible shim in front of Playwright-driven web chat sites, with per-provider cooldowns,
  failover before the first output, and tool calls carried in fenced `tool_call` blocks.
- Providers: Gemini (no login; eval 8/8) and Qwen (login; eval 8/8) form the default rotation, with failover verified against
  a live second provider. Perplexity, Grok and ChatGPT are registered but excluded (see docs/PROVIDERS.md).
- `freeharness setup` (checksum-verified Node + `npm ci` of the pinned harness), `login`, `doctor`,
  `eval`, `serve`.
- Shim access control: per-install bearer token (0600) and loopback-only Host check.
- Tool-use eval suite (8 tasks) that gates providers on workspace outcomes, not model claims.

- Sticky chats: Gemini continues ONE chat per task and sends only what is new (verified: eval 8/8, 388 s vs 532 s; about 9K characters sent for a 5-step task instead of an estimated 39K),
  with a safe fallback to a fresh chat on any mismatch; `--sticky auto|none|all|list`.
- `freeharness transcript`: a readable markdown record of every session (prompts, replies, tool calls, results).
- `freeharness vault <path>`: read-only notes-vault memory (MCP server with BM25 `vault_search` and `vault_read`), loaded through the new
  `<config>/overlay.d/*.yml` overlays; the skill tool is enabled; docs/MEMORY.md.
- DeepSeek provider (visible window, own profile). Spot-checked, not yet through the eval. ChatGPT's visible window works after re-measuring its (rebuilt) page selectors, with one reload-and-retry for a composer that has not hydrated; its tool use is unreliable, so it stays out of the default rotation.
- Sites that block headless Chrome run in their own visible window and profile (`freeharness login chatgpt`); `setup` now also installs Playwright's Chromium.
- `--providers all` to rotate over every registered site that can run, eligible first, with a warning for those that failed the eval.

### Security
- Browser profiles, token, and logs live in per-user directories with owner-only permissions.
- `npm ci --ignore-scripts`; Node tarball verified against nodejs.org SHASUMS256.
