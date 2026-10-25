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

## Runtime guarantees

Each line below is exercised offline by `tests/test_runtime_guarantees.py` (real `dsh --profile sdk`, scripted stub model, private port; skipped when the harness or Node is missing). Nothing here is claimed for live providers.

- **Step limit**: dsh has none. The client abandons a turn that starts more than `--max-steps` steps (default 60; `0` disables), restarts the runtime and resumes the session, which then accepts new turns. `-p` exits 1.
- **Tool errors**: a failed tool call (e.g. invalid arguments) is returned to the model as an error result, and the next request shows it; the model can retry within the same turn.
- **Hung model**: a turn past `--turn-timeout` (default 900 s) raises `TimeoutError`; the CLI abandons and resumes, and the session works afterwards. `-p` exits 1.
- **Crash / Ctrl-C**: killing the runtime mid-model-call or mid-tool raises `RuntimeDied`; restart + `session/resume` repairs the log, an interrupted tool call is reported to the model as "interrupted, do not retry blindly", and the next turn completes. There is no cancel in the protocol, so this is also how Ctrl-C works.
- **Trust model**: no human answers approvals on this channel, so approval policy is `never` (dsh.patch.yml) and a request needing approval is rejected immediately, never waited on. The sandbox is pinned to `workspace-write` (the client overrides any inherited `DSH_PERMISSION_MODE`): `write`/`edit` outside the workspace and temp dirs are denied, bash runs in dsh's OS sandbox (read-only filesystem except the workspace and a private `/tmp`; network is NOT restricted), and `sandbox_permissions` escalation is rejected. Writes inside the workspace need no approval. Temp dirs are writable, so the fence is the workspace plus `/tmp`, not the workspace alone. Reads are unconfined.
- **`-p` exit codes**: 0 turn completed; 1 turn failed, hit the step limit or the timeout; 2 the runtime could not run (e.g. missing workspace, no Node/harness, model route error).
- **Ownership**: step execution, tools, sandbox, approval and session repair belong to dsh. The client adds only what dsh lacks: the step limit, the wall-clock timeout, and abandon-by-restart.
