# Harness requirements: gap analysis and plan

Source: the "Complete AI Harness" master prompt (15 acceptance criteria). freeharness is built on
DeepSeek Harness (dsh), which already owns most of the harness. The rule for this work is
**use dsh's own service for each requirement; write freeharness code only where dsh has none.**
Status values: `dsh` = provided by dsh, `fh` = freeharness code, `verify` = believed present but not
yet proven here, `gap` = nothing yet. Nothing below is marked done until a test proves it.

## Component map

| Master-prompt module | Provided by | Status |
|---|---|---|
| model_gateway | dsh `llm-pi-ai` -> our shim -> web chats (`fh`: providers, router, failover, sticky) | done, live-tested on Gemini/Qwen |
| orchestrator (loop, step/tool limits) | dsh `agent-loop`, `timeout`, `tool-call-timeout-policy`, `repeat-tool-reminder` | verify (runtime agent) |
| context_manager (budgeting) | dsh `compaction*`, `tool-result-pruner`; `fh` `fit_messages` in the shim | fit_messages tested offline; dsh compaction unverified |
| session_service | dsh `session-persistence-jsonl`, `session-query-sqlite` (FTS5 search); `fh` transcripts | dsh sessions verified on resume; DB-backed index = agent in progress |
| state_manager (scratchpad) | none in dsh | `fh` in progress (memory agent) |
| memory_service | none in dsh; `fh` vault MCP done, memory MCP in progress | partial |
| tool_registry / tool_executor | dsh `tools`, `tool-bash`, `tool-fs`, `tool-fs-search`, `tool-str-replace-editor`, `tool-todo`, `tool-skill`, `tool-session-query` | verify (tools agent) |
| mcp_client | dsh `mcp-client` (one row per server, explicit config) | done (vault) |
| workflow_engine | dsh `workflow`, `tool-workflow` (model-written fan-out scripts), `goal`, `jobs`; disabled by us for prompt size | partly: one deterministic sequential flow exists (the OSS stage engine, `oss_flow.py`); no general flow format, no branching/parallel |
| sandbox_manager | dsh `sandbox`, `sandbox-local`, `sandbox-policy`, `bash-sandbox`, `fs-sandbox`; `fh` bwrap runner (no network, home/tmp hidden), `fh` read fence, `security.probe` | **tested (stage 1)**, Linux + bubblewrap only; dsh alone restricts neither reads nor network (measured); same-kernel, not a container |
| policy_engine / approval_service | dsh `user-approval` (ask/never, fail-closed, audited), `permission-presets`; `fh` terminal answerer over a private socket | **tested (stage 1)**: no answerer = refused at once (not a hang); `-p` = never; interactive = y/N, default no, one call only |
| evaluation_engine | `fh` `eval/` (deterministic, workspace-judged) | deterministic done; LLM-judge and human review = gap |
| audit_service / observability | dsh session log, `otel`, `token-meter`, `session-stats`; `fh` rotating logs | verify; redaction = gap |
| api / frontend | dsh `api-gateway`, `host-webserver`, `web-app`; `fh` terminal client | gap: no task API/UI for freeharness (decision needed) |

## Acceptance criteria status (honest)

| # | Criterion | Status |
|---|---|---|
| 1 | Task ID | session id exists; no "task" object |
| 2 | Model calls validated/authorized by harness | tool schemas validated by dsh; file/network/privilege authorization tested for bash, write/edit, read/grep/glob (stage 1); other tools (MCP) not covered |
| 3 | Tool results loop back | proven (evals) |
| 4 | Persistence across restart | dsh sessions proven; scratchpad in progress |
| 5 | Cross-session memory | in progress |
| 6 | Workflow branching/parallel/retry/approval | dsh workflow exists but disabled; retry/repair, checkpoints, resume and human-approval gates exist in the OSS stage engine only; branching/parallel: gap |
| 7 | Isolated unsafe execution | tested: bash confined by bwrap (workspace-only writes, home/tmp hidden, no network); same-kernel only, NOT a container; Linux+bubblewrap only (fails closed elsewhere) |
| 8 | Prompt-injection can't bypass permissions | tested for file/network/escalation: an obeying scripted model reading an injected file cannot read the canary, reach the network or self-approve; MCP tools and in-workspace secrets not covered |
| 9 | Retention, access, redaction | gap (redaction of logs/transcripts/memory) |
| 10 | Deterministic / LLM / human eval independent | deterministic only |
| 11 | Bounded repair attempts | dsh step limits; no explicit repair loop |
| 12 | Cancel + recover | runtime agent in progress |
| 13 | Audit trail | dsh session log; no per-task report |
| 14 | CSV export demo, synthetic data | gap |
| 15 | Accurate final report | this file + CHANGELOG, updated per stage |

## Stages

1. **Security baseline (first, highest risk). STATUS: done and tested on Linux with bubblewrap (`tests/test_security.py`); see ARCHITECTURE.md "Security" for what is and is not enforced.** Probe real dsh offline: is `ctx.sandbox` mounted in the
   sdk profile, which mode do bash/write/edit run in, what happens on an approval request with no
   answerer (hang vs fail-closed), can bash read `~/.ssh` or `.env` or reach the network. Configure
   the least-privilege default (workspace-write, ask for wider, network off) in `dsh.patch.yml`;
   expose an approval answerer in the terminal client (approve/deny prompt). Adversarial tests:
   injected instructions in a file/tool result trying to read secrets, escape the workspace, curl out.
2. **Secrets and redaction.** Redact tokens/keys/emails patterns in transcripts, memory writes and logs;
   `.env`/key files denied to the model by sandbox policy; retention settings (`freeharness prune`).
3. **Task layer.** `freeharness task run|status|resume|list|cancel`: task id, state machine
   (created/running/blocked/failed/cancelled/completed), checkpoints in the scratchpad, per-task report
   (changed files via dsh `workspace-changes`, tests, timeline from session log).
4. **Workflows. STATUS: partly done.** Done and tested offline: a sequential stage engine with per-stage acceptance checks, bounded
   repair turns, persistent state, checkpoints and resume, used by `freeharness oss run` (`src/freeharness/oss_flow.py`, docs/OSS_EVAL.md).
   Not done: the engine is specific to the OSS pipeline (its stage list is data, but there is no general flow definition format or CLI),
   there is no branching or parallel execution (no per-branch git worktrees), and the CSV-export reference flow does not exist. Not yet
   shown to help with real free-tier models: only scripted-model runs have been done. Original scope: deterministic flows in Python on top of the dsh runtime: sequential, branch, parallel
   (each branch in its own git worktree), bounded retries/repair, approval gates; the CSV-export flow
   as the reference.
5. **Evaluation.** Combine deterministic checks (existing) + LLM judge (through the same shim, evidence
   packet only, recorded reasoning) + human review gate (`freeharness review`), final assessment never
   "success" on model claim alone.
6. **API/UI.** Decision needed: dsh already ships a local web UI and API gateway; likely wrap it instead
   of building a second one.
7. **Demo and report.** CSV export end to end on synthetic data, final honest status table.

## Known limits to keep stating

- The sandbox is same-kernel confinement (bwrap/Landlock), not a container or microVM.
- Free web-chat models are unreliable tool users; the LLM judge is therefore advisory only.
- Everything sent to a model goes to the user's chosen third-party site (see PRIVACY.md).
- "Production-ready" will not be claimed until stage 1 tests and the acceptance table are green.
