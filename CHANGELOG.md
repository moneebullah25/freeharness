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

### Security
- Browser profiles, token, and logs live in per-user directories with owner-only permissions.
- `npm ci --ignore-scripts`; Node tarball verified against nodejs.org SHASUMS256.
