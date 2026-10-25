# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). While the major version is 0, minor
releases may include breaking changes; they will be listed under **Changed**.

## [Unreleased]

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
