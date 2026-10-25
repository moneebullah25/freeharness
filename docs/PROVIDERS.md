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
| ChatGPT | free account | not run through the full eval. Page selectors re-measured 2026-10-25 (the site was rebuilt): plain question 24 s, correct. **Tool use is unreliable**: in 2 sampled agent runs, one called the tool and one invented a failure ("the file operation failed") without calling any; the successful run also claimed a read-back it never made. Runs in its own **visible** window (Cloudflare blocks headless) with its own profile: `freeharness login chatgpt` | In `--providers all` only; excluded from the default rotation |
| DeepSeek | free account | **not run through the full eval** (by request). Spot checks 2026-10-25: plain question 12 s; correct `tool_call`; a write-then-read agent task completed in 3 steps / 27 s. Runs in its own **visible** Chrome window (headless gets HTTP 403) with its own profile: `freeharness login deepseek` | In `--providers all` only, last; becomes eligible after a passing eval |
| Duck.ai | none | not evaluated: shows a "select all squares containing a duck" CAPTCHA on a message | Excluded (CAPTCHAs are never bypassed) |
| HuggingChat | free account | not evaluated: free message quota already exhausted ("Upgrade Required") | Excluded |
| Kimi, Z.ai, DeepSeek chat, Claude, Mistral, LMArena, You.com, DeepAI, Blackbox, Phind, Pi | n/a | not usable without payment, a CAPTCHA, or are blocked headless (2026-10-24) | Not registered |

## How to read eval results
`freeharness eval --model <name> [--suite core|full] [--tasks a,b] [--repeat N]`. Every task runs through the real stack (dsh tool loop ->
shim -> web chat) in a fresh temp workspace and is judged by deterministic code on the resulting files or the captured tool calls, never on
the model's claim of success.
- **core** (default) is the original 8 tasks; the 75% bar and the "Eval" column above are measured on core only.
- **full** adds 5 tasks: `explore-dir` (find/grep/wc over a generated tree), `refactor-3files` (rename across 3 files, judged by running an
  untouched `check.py`), `skill-use` (a project skill in `.dsh/skills`; the `skill` tool call is required), `vault-mcp` (a temp vault;
  `vault_search`/`vault_read` required, runs without your own overlay) and `resume-continue` (two turns, runtime restart + `session/resume`
  between them, the second turn needs a fact from the first). Report full results separately; they do not change eligibility.
- Result file `<state dir>/eval/<time>-<model>.json`: `suite`, `verdict`, and per task `ok`, `detail` (the reason), `steps`, `tools` (names in
  order), `tool_counts`, `tool_calls`, `tool_errors`, `turns`, `turn_seconds`, `seconds`, `final` (the model's words, for reading only).
- Verdict: `eligible` needs >= 75% of judged runs passed, no run that `answered_without_tools` (detail starts with "no tool use": the model
  answered without touching the workspace) and nothing `inconclusive` (rate-limited after one retry; such runs are INFRA, not failures).
- A high `tool_errors` or `steps` with a pass means the model got there the hard way; a pass with `final` contradicting the files is still a pass.

### Advisory judge and human review
`eval/judge.py` builds an evidence packet (changed files, tool calls with output, deterministic result) and asks a model for a 0-10 score with
recorded reasoning; it is advisory and cannot change a result. `freeharness review <task> approve|reject|changes [--note ..]` records a human
decision in `<state dir>/eval/reviews.json`. `review.final_assessment()` combines the three: a failed or missing deterministic check is always
`failed`/`unverified`; a pass is `passed`, `needs_review` (judge < 5), `verified` (human approved), `needs_changes` or `rejected_by_human`.

## Using more than the eligible set
`freeharness --providers all` (or `freeharness serve --providers all`) puts every registered site that can run into the rotation,
eligible ones first: Gemini, Qwen, then Grok and Perplexity. The shim prints a warning for the ones that failed the eval. The cost is
real: when Gemini and Qwen are both resting, a turn that lands on Grok or Perplexity often replies "done" without calling a tool, so the
agent can report success with nothing changed. Waiting for an eligible site (HTTP 429 + Retry-After) is the safer default.
Sites that block headless Chrome (ChatGPT; DeepSeek returns HTTP 403 to the headless user agent; ChatGPT works in its visible window after its selectors were re-measured on 2026-10-25, but its tool use is unreliable)) run in their **own visible Chrome
window with their own profile**, because Chromium locks a profile to one browser and the shared one is headless. Their challenges are
never bypassed and the browser is not disguised: a normal visible Chrome simply is not blocked. HuggingChat and Duck.ai have no provider entry (free quota used up / CAPTCHA).

## Sticky chats
Sticky (one chat per task, only new messages sent) is **on for every provider by default**; `--sticky none` / `FREEHARNESS_STICKY=none` turns it off.
`STICKY_VERIFIED` in `providers.py` now means only "measured live": **Gemini** 8/8 in 388 s over 28 steps (vs 8/8 in 532 s over 33 steps with a
fresh chat per step; single runs, 2026-10-25). For Qwen, DeepSeek, ChatGPT, Grok and Perplexity sticky is **default-on but NOT live-verified**:
nobody has yet run a multi-step task on them in one chat and read the logs. The lead verifies each live (`SHIM_DEBUG_PROMPTS=<file>` shows what
the site received; `freeharness eval --model <name> --sticky` compares).

### "Chat is full" markers (`Provider.full_markers`) - UNVERIFIED until seen live
Lowercase substrings of the site's own notice, checked in a short reply (<= 1,500 chars, no tool-call block), in alert/banner/last-reply text on
the page (`Session.chat_full`) and in the error of a failed ask. Seen => the chat is not continued; the same request is re-sent in a NEW chat
with a compact history. The wording below is from memory of each site, not captured; an entry is only trustworthy once a log shows the real notice.

| Provider | `full_markers` |
|---|---|
| ChatGPT | `maximum length for this conversation`, `conversation is too long`, `start a new chat`, `message you submitted was too long`, `message is too long` |
| DeepSeek | `length limit`, `conversation is too long`, `start a new chat`, `conversation limit` |
| Gemini | `conversation is too long`, `start a new chat`, `start a new conversation`, `too long for me to` |
| Qwen | `too long`, `new chat`, `context length`, `exceeds the maximum` (broad on purpose; only applied to short replies) |
| Grok | `conversation is too long`, `start a new conversation`, `start a new chat`, `context length`, `maximum length` |
| Perplexity | `thread is too long`, `conversation is too long`, `start a new thread`, `start a new chat` |

Gemini also has `size_errors` = `i encountered an error doing what you asked` (one 58k-char prompt got it on 2026-10-25 while 8-12k steps were fine:
a single observation, so it only counts as a length-looking failure, never as a limit by itself).

### Prompt-size limits
No provider has an assumed `max_prompt` any more (safety value 2,000,000 chars). A failed FRESH chat with a length-looking signal at a size above
anything that worked records `suspect_max_chars` in `<state>/limits.json`; a smaller success afterwards confirms it (ceiling = 80% of the failing
size, fresh-chat prompts aim at 50% of the ceiling). Visible in `/health` and `freeharness doctor`. Until a site has actually failed, nothing is
trimmed for it. The old guesses (Gemini 30,000 "measured", ChatGPT 100,000, default 60,000) are gone: the 60,000 default is what trimmed every
DeepSeek prompt and made the model lose files it had read.

## Failover evidence
With Gemini forced to fail with a quota error, the router put it on a 900 s cooldown and the same request was answered by a live Qwen
session (2026-10-25; the router logic is also unit-tested). Rotation order is the `--providers` order: Gemini first (no login needed),
then Qwen. A site that shows a verification page or hits its free limit is skipped for 15-30 minutes and retried afterwards.

## Adding or re-checking a provider
1. Add a `Provider` in `backends/providers.py` (selectors measured on the live page; note provenance in a comment).
2. Add its id to `data/dsh.patch.yml` (models list) and to `MODELS` in `client/cli.py`.
3. `freeharness eval --model <name>`; only a pass makes it eligible. Record the result here.
