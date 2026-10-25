# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). While the major version is 0, minor
releases may include breaking changes; they will be listed under **Changed**.

## [Unreleased]

### Added
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
