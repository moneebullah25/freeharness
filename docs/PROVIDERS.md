# Providers and eval evidence

A provider is **eligible for rotation** only if `freeharness eval --model <name>` passes at least 75% of its 8 tool-loop
tasks, never answers without using a tool where one was needed, and no task is inconclusive. Outcomes are judged on the
workspace (files, exit codes, numeric answers), not on what the model says. Results below were measured on the dates shown,
headless, on a free account.

| Provider | Login | Eval | Status |
|---|---|---|---|
| **Gemini** | none | **8/8** (2026-10-25, fresh install of this package; also 8/8 on 2026-10-24) | **Eligible: the default** |
| Qwen | free account | 7/8 on 2026-10-25 with the earlier parser; the one failure was three batched tool calls read back as empty labels (Monaco code viewer). Fixed by reading the reply's Copy-button clipboard; **not re-evaluated on the fix** because Qwen then showed a slide-to-verify page that only a human can complete | **Candidate**: opt in with `--providers gemini,qwen`; becomes eligible after a passing re-run |
| Perplexity | none | 0/8 (2026-10-24): refuses to use tools in every task | Excluded |
| Grok | free account (+ one-time age prompt) | 0/8 (2026-10-25): answers as if it had its own sandbox ("Created greeting.txt") and never emits our tool calls, so the workspace never changes | Excluded |
| ChatGPT | none/free | not evaluated: Cloudflare challenges headless; works only in a visible window | Registered, not in rotation |
| Duck.ai | none | not evaluated: shows a "select all squares containing a duck" CAPTCHA on a message | Excluded (CAPTCHAs are never bypassed) |
| HuggingChat | free account | not evaluated: free message quota already exhausted ("Upgrade Required") | Excluded |
| Kimi, Z.ai, DeepSeek chat, Claude, Mistral, LMArena, You.com, DeepAI, Blackbox, Phind, Pi | n/a | not usable without payment, a CAPTCHA, or are blocked headless (2026-10-24) | Not registered |

## Why one provider is not "rotation" yet
The router (per-provider cooldowns, failover before any output is streamed, 429 + Retry-After when all are resting) is unit-tested
but has never failed over between two real providers, because only Gemini is eligible. The next step toward real rotation is a passing
Qwen re-eval (after completing its verification once with `freeharness login qwen`) or another provider that passes the eval.

## Adding or re-checking a provider
1. Add a `Provider` in `backends/providers.py` (selectors measured on the live page; note provenance in a comment).
2. Add its id to `data/dsh.patch.yml` (models list) and to `MODELS` in `client/cli.py`.
3. `freeharness eval --model <name>`; only a pass makes it eligible. Record the result here.
