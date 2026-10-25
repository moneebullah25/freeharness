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
