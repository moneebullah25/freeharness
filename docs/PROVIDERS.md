# Providers and eval evidence

A provider is **eligible for rotation** only if `freeharness eval --model <name>` passes at least 75% of its 8 tool-loop
tasks, never answers without using a tool where one was needed, and no task is inconclusive. Outcomes are judged on the
workspace (files, exit codes, numeric answers), not on what the model says. Results below were measured on the dates shown,
headless, on a free account.

| Provider | Login | Eval | Status |
|---|---|---|---|
| **Gemini** | none | **8/8** (2026-10-25, fresh install of this package; also 8/8 on 2026-10-24) | **Eligible: first in the rotation** |
| **Qwen** | free account | **8/8** (2026-10-25, after the clipboard/parser fixes; an earlier run with the old parser was 7/8) | **Eligible: in the default rotation** |
| Perplexity | none | 0/8 (2026-10-24): refuses to use tools in every task | Excluded |
| Grok | free account (+ one-time age prompt) | 0/8 (2026-10-25): answers as if it had its own sandbox ("Created greeting.txt") and never emits our tool calls, so the workspace never changes | Excluded |
| ChatGPT | none/free | not evaluated: Cloudflare challenges headless; works only in a visible window | Registered, not in rotation |
| Duck.ai | none | not evaluated: shows a "select all squares containing a duck" CAPTCHA on a message | Excluded (CAPTCHAs are never bypassed) |
| HuggingChat | free account | not evaluated: free message quota already exhausted ("Upgrade Required") | Excluded |
| Kimi, Z.ai, DeepSeek chat, Claude, Mistral, LMArena, You.com, DeepAI, Blackbox, Phind, Pi | n/a | not usable without payment, a CAPTCHA, or are blocked headless (2026-10-24) | Not registered |

## Failover evidence
With Gemini forced to fail with a quota error, the router put it on a 900 s cooldown and the same request was answered by a live Qwen
session (2026-10-25; the router logic is also unit-tested). Rotation order is the `--providers` order: Gemini first (no login needed),
then Qwen. A site that shows a verification page or hits its free limit is skipped for 15-30 minutes and retried afterwards.

## Adding or re-checking a provider
1. Add a `Provider` in `backends/providers.py` (selectors measured on the live page; note provenance in a comment).
2. Add its id to `data/dsh.patch.yml` (models list) and to `MODELS` in `client/cli.py`.
3. `freeharness eval --model <name>`; only a pass makes it eligible. Record the result here.
