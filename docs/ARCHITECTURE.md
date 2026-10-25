# Architecture

```
 you ── freeharness (terminal client, Python)
          │  newline-delimited JSON-RPC over stdio (initialize, session/prompt, session/resume, session/wait)
          ▼
        DeepSeek Harness `dsh --profile sdk` (Node >= 24.9, pinned)   ← agent loop, tools, sessions, compaction
          │  OpenAI-compatible HTTP, loopback, bearer token
          ▼
        shim (backends/shim.py)   ← tools rendered into a text protocol, `tool_call` blocks parsed back,
          │                          router with per-provider cooldowns and pre-output failover
          ▼
        Playwright → one signed-in Chrome profile → one tab per site (Gemini, Qwen, ...)
```

Key decisions (each was measured, not assumed; see docs/PROVIDERS.md):
- **No cancel in the SDK protocol**: Ctrl-C kills the runtime, restarts it and `session/resume` repairs the turn.
- **Tool calls in fenced `tool_call` blocks** read from the DOM or clipboard, because rendered markdown corrupts `**` and `_`.
- **A provider is only in rotation if it passes the eval** (8 tool-loop tasks judged on workspace outcomes).
- **One browser, many tabs**: Chromium locks a profile directory, so every `shared` provider is a tab of one browser; a provider failure closes its tab, never the browser.
- **Failover only before output**: once text has streamed to the client, switching provider would corrupt the turn.
- **Cookies**: plain Chrome (for Google sign-in) and headless Playwright must use the same cookie-encryption mode (`--password-store=basic` on Linux, `--use-mock-keychain` on macOS) or Chrome deletes the cookies it cannot decrypt.

Modules: `client/` (rpc, renderer, REPL), `backends/` (providers, bridge Session, shim), `eval/`, plus `setup_cmd`, `login_cmd`, `doctor`, `auth`, `paths`, `logs`.

## Security (stage 1 baseline)

**This is same-kernel confinement (bubblewrap namespaces), not a container or microVM.** A kernel or
bubblewrap escape, or anything running outside the dsh process tree, is out of scope.

Measured against the real dsh 0.2.1-rc.2 with a scripted model (`tests/test_security.py`; offline, free port,
temporary homes). dsh's own defaults, before any freeharness change: `ctx.sandbox` is mounted in the `sdk`
profile, bash runs under bwrap `workspace-write`, `write`/`edit` are fenced in-process to the workspace.
But dsh restricts **neither reads nor the network** (its READMEs say so): a bash call read a canary outside
the workspace, connected to a loopback listener, and the `read` tool read the canary too.

What freeharness adds (all enforced below the model; the model's text is never an input):

| Control | Mechanism | Enforced by |
|---|---|---|
| bash writes only in the workspace (+ a private `/tmp`) | dsh `sandbox-local` + `bash-sandbox` | dsh, bwrap |
| bash cannot read `$HOME`, `/tmp`, `/run`, `/mnt`, `/root`... outside the workspace | `data/fh_sandbox_runner.py` (dsh `runnerCommand`): empty tmpfs over them, workspace bound back | bwrap |
| bash has no network | same runner: `--unshare-net` | bwrap |
| secrets removed from bash's environment (`*KEY*`, `*TOKEN*`, ...) | same runner: `--unsetenv` | bwrap |
| no bwrap, or it cannot start: the bash call fails, never runs unconfined | runner exits 126 (`freeharness-sandbox:`); an unset runner points at a nonexistent path | runner + dsh runner-failure classification |
| `write`/`edit` only in workspace + temp | dsh `fs-sandbox` | dsh (policy fence in trusted code) |
| `read`/`grep`/`glob` cannot reach home, `/tmp`, `/run`... outside the workspace | `data/fh_guard.mjs` on dsh's `tools/execute` | our plugin (policy fence in the dsh process) |
| wider access (`sandbox_permissions=danger-full-access`) needs a human, once, per call | dsh `user-approval` + `client/approval.py` | dsh refuses unless an answerer says `allowed-once` |

**Approval.** dsh's JSON-RPC `sdk` profile never sends the client a request, so the protocol cannot carry an
answer. With no answerer dsh resolves `unavailable` at once (fail closed; it does not hang). The interactive
client therefore runs an answerer: the guard plugin relays each approval request over a private unix socket
(random token, socket hidden from bash) to `TerminalApprover`, which shows the command and the mode requested
and asks `y/N` (default no; no TTY means no). `-p`, the eval and tests have no answerer: dsh policy `never`
(`data/approval_never.patch.yml`) refuses at once and tells the model not to ask. There is no approve-everything
flag. Approval is one-shot: the next call is confined again. If the answerer crashes, closes, or is missing, the
request is `unavailable` or `rejected`.

**`freeharness doctor`** measures instead of assuming (a canary file in `$HOME` and a loopback listener):
`full` = workspace write works, outside write/read blocked, network blocked; `partial` = bwrap starts but at
least one does not hold (warn); `unavailable` = no bwrap or it cannot create a sandbox here (FAIL: bash calls
are refused).

**Not enforced / known limits**
- Linux with bubblewrap only. dsh's Landlock fallback is deliberately not used (it has no network isolation and
  our runner replaces dsh's runner chain). macOS and Windows: bash calls are refused, not run unconfined.
- Denylist, not allowlist: hidden are `$HOME`, `/root`, `/mnt`, `/media`, `/srv`, `/tmp`, `/var/tmp`, `/run`.
  The rest of the host (`/etc`, `/opt`, `/var`, `/usr`...) stays readable by bash. Toolchains under `$HOME` are
  invisible to bash unless listed in `FREEHARNESS_SANDBOX_RO` (colon-separated, mounted read-only).
- The read fence covers `read`, `read_image`, `grep`, `glob`. It is a path check in the dsh process, not a kernel
  boundary; MCP servers (the vault) and any other tool are not covered. If the guard plugin fails to load, the
  client refuses to start (it checks dsh's report), but a dsh launched without the freeharness client has no fence.
- A `danger-full-access` approval removes all confinement for that one command, including network.
- Extra secrets in the workspace itself (a `.env` inside it) are readable: the workspace is trusted by design.
- The model still receives whatever a permitted tool returns, and everything sent to a model goes to the
  chosen third-party site (see PRIVACY.md). Redaction is stage 2.
