# freeharness

A Claude-Code-style terminal coding agent that runs on **free web-chat sites** instead of a paid API.
The agent loop (tools, sessions, compaction) is [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
(MIT); the model behind it is a set of chat sites (Gemini and Qwen today, more as they pass the eval) driven by Playwright
through an OpenAI-compatible shim, so work continues on another site when one runs out of free quota.

```
you → freeharness (terminal) → DeepSeek Harness (agent loop) → local shim (token-protected) → signed-in Chrome tabs
```

> **Personal tool, not a service.** It operates *your* logged-in browser sessions. Do not host it, share a
> profile, or expose the port. Automating a consumer chat site may breach its terms of use; you use it at
> your own risk. See [SECURITY.md](SECURITY.md) and [docs/PRIVACY.md](docs/PRIVACY.md).

## Requirements
| | |
|---|---|
| OS | **Linux x86-64: tested.** macOS: runs in CI, live sites not verified. Windows: not supported |
| Python | 3.10+ |
| Chrome | Google Chrome installed (used for the logged-in sites) |
| Node | 24.9+ — `freeharness setup` installs a checksum-verified copy if you have none |
| Accounts | a free account on each site you want in rotation (Gemini needs none) |

## Install
```bash
pipx install .            # or: ./scripts/install.sh   (runs setup for you)
freeharness setup         # Node (if needed) + the pinned DeepSeek Harness
freeharness login         # once: plain Chrome opens; sign in to the sites, then close the window with its X
freeharness doctor        # everything should be [ok]
```
Updating: `pipx install --force .` (or a newer wheel). Your logins and sessions are kept outside the package.

## Use
```bash
cd ~/my-project
freeharness                          # interactive; the agent works in this directory
freeharness -p "fix the failing test and run the suite"
freeharness --model gemini           # pin one site;  default rotation is gemini,qwen
freeharness --providers all          # every site that can run headless (see the warning below)
```
Gemini keeps one chat per task and sends only what is new (`--sticky none` for a fresh chat per step); every session is also saved as
readable markdown (`freeharness transcript`).

In the session: `/help` `/model` `/providers` `/new` `/sessions` `/resume <id>` `/status` `/quit`.
**Ctrl-C** abandons the current turn (the harness has no cancel call, so the runtime is restarted and the
session resumed; the site may keep generating for a few seconds).

Run it in a **project directory**, not your home directory: the agent can read and edit files under its working directory.

## Memory, skills, MCP
`freeharness vault <notes folder>` connects a notes vault as read-only memory (ranked search + read, one step instead of several);
skills (`SKILL.md`) and extra MCP servers (`overlay.d/*.yml`) are supported. See [docs/MEMORY.md](docs/MEMORY.md) for what the agent
knows at the start of a turn, how retrieval works and where everything is stored.

Sessions and long-term memory survive restarts: `freeharness --continue` resumes the latest session for the folder, a per-session
scratchpad and typed saved facts (`freeharness memory ...`, `/remember`) are recalled automatically. See
[docs/SESSIONS_AND_MEMORY.md](docs/SESSIONS_AND_MEMORY.md).

`freeharness agents install` adds a small, pinned set of open-source "Agency" agent skills and an `oss-contribution` pipeline skill
to the dsh home (`--set all` is refused without `--yes`: skill descriptions are re-sent every step). See
[docs/AGENTS_SKILLS.md](docs/AGENTS_SKILLS.md).

## Providers
A site joins the rotation only if it passes the tool-use eval (`freeharness eval --model <name>`): 8 coding tasks
judged on the resulting files, never on what the model says. **Today Gemini (8/8) and Qwen (8/8) are eligible and make up the default rotation**; Perplexity and Grok failed (0/8). Qwen needs
`freeharness login qwen` once; without it the shim simply falls back to Gemini. `--providers all` adds Grok and Perplexity at the end of the rotation; they only get a turn when the eligible sites are resting,
and because they tend to answer without using tools, such a turn can "finish" without changing anything. ChatGPT and DeepSeek block headless Chrome,
so each runs in its **own visible Chrome window** with its own profile: sign in with `freeharness login chatgpt`; the window appears
when the site is first used. Evidence: [docs/PROVIDERS.md](docs/PROVIDERS.md).

## Other commands
`freeharness setup | login [site…] [--check] | doctor [--report] | vault <path> | memory … | sessions | transcript [id] | serve | eval | version`

## Where things live
Per-user directories, owner-only; `FREEHARNESS_HOME=/dir` puts everything under one root. See [docs/PRIVACY.md](docs/PRIVACY.md).

## Limitations
- Each model step is a browser round trip (15–120 s); complex edits take minutes.
- Free tiers are small and sites add verification (CAPTCHAs, age prompts). The tool detects these and cools the
  provider down; it never tries to bypass them — complete them once yourself with `freeharness login <site>`.
- Web UIs change without notice; selectors live in `backends/providers.py` and the eval tells you when one broke.
- One signed-in browser profile; one `freeharness` shim per machine.

## Development
```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/freeharness setup
.venv/bin/ruff check src tests && .venv/bin/pytest -q      # no network or accounts needed
python scripts/licenses.py                                  # license inventory (fails on unreviewed licenses)
```
Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Troubleshooting: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) ·
Release checklist: [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) · [CHANGELOG.md](CHANGELOG.md)

## License
MIT (see [LICENSE](LICENSE)). DeepSeek Harness is MIT, © 2026 DeepSeek (`harness/DSH-LICENSE`); all dependency licenses are in
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
