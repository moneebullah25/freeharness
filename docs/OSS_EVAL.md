# OSS contribution evaluation

```bash
freeharness agents install                      # once: the agency review skills + oss-contribution
export GITHUB_TOKEN=...                         # every gh_* tool needs it, also in dry-run (read access is enough for dry-run)
export FH_GH_GIT_NAME="Your Name" FH_GH_GIT_EMAIL="you@example.org"   # commit identity gh_clone writes into the clone

freeharness oss run owner/repo --component src/core --models gemini,deepseek     # dry-run: nothing is posted
freeharness oss run owner/repo --live                                             # terminal + y/N, you approve each post
freeharness oss run --resume <run-id>                                             # continue a staged run at its first unfinished stage
freeharness oss run owner/repo --single-turn                                      # the old behaviour: ONE prompt with all the steps
tail -f ~/.local/state/freeharness/oss/<run-id>/supervisor.jsonl                  # from another terminal, while it runs
freeharness oss report <run-id|latest>                                            # timeline + checklist, writes report.md
freeharness review oss approve|reject|changes --run <run-id> --note "..."         # your final decision on the run
freeharness oss list
freeharness eval --suite oss                                                      # only LISTS this evaluation; never runs it
```

## The staged workflow (the default)
A free-tier web model (16k context) cannot hold a 14-step pipeline in one prompt: measured over six supervised dry runs it
repeated searches, forgot steps and fabricated a final summary ("tests run", "issue #42"). So `freeharness oss run` is a
deterministic stage engine (`src/freeharness/oss_flow.py`, checks in `eval/oss_stage.py`). The harness orders the work and
checks it; **it never writes a draft, bug text, fix code, a search query or PR text**. It only sends the model a short
prompt per stage, hands the model its OWN recorded evidence back (its report, its repro output, the titles its own searches
returned) and checks the result against the tool-call record, the gate's files and the clone.

Each stage is ONE `run_turn` on the same dsh session with a short prompt (a template filled from `state.json`, never the
history), a step budget, then an acceptance check. Unmet items go back to the model, ONLY that stage's, with their evidence, for
up to `max_repair` repair turns (default 2 per stage, `--repair N` overrides, `0` = judge once); then the run advances or
ends `blocked at <stage>: <reason>`. The stage list is plain data (`STAGES` in `oss_flow.py`: id, prompt template,
acceptance = oss_judge item ids + a stage-specific check, `max_repair`, step budget per turn).

| # | stage | the model must | acceptance (all deterministic) | steps/turn |
|---|---|---|---|---|
| 1 | `setup` | read the repo rules (`gh_repo_info`, CONTRIBUTING), load `oss-contribution` and one review skill, `gh_fork`, `gh_clone` | judge `repo_rules_read`, `skills_used`; `clone_exists` (the gate's registry has a clone with a `.git`) | 20 |
| 2 | `hunt` | choose ONE component, find ONE candidate bug, REPRODUCE it with a throwaway script (through the `gh_setup_env` prefix), write `reports/<repo>_issues.md` with the repro | judge `report_file`; `candidate_section` (`## Candidate N` for this candidate); `repro_run`: a recorded bash call that ran code (interpreter, script, test runner; here-doc text does not count) whose output shows the failure by simple rules (traceback / `Error` / `FAIL` text, or a token of the model's own `Actual:` that really appears and is absent from its `Expected:`; a missing module, `command not found` or syntax error is an environment problem, not a repro); `expected_actual` (both lines in the section); `repro_in_report` (a real line of the run's output is pasted in the section) | 40 |
| 3 | `dupcheck` | 4+ distinct `gh_search` queries, issues AND prs, for THIS candidate; note which hits overlap; end with `DECISION: proceed`, `duplicate:#N` or `competing:#N` | judge `duplicate_search`; `searches_for_candidate` (4+ distinct queries, both kinds, since this stage started); `decision_line` (a `DECISION:` line; `#N` must be a number one of its own searches returned, `competing` must be a PR) | 25 |
| 4 | `issue` | `gh_issue_create` from the report and repro output the prompt shows back | judge `issue_draft` (the gate saved it after every gate passed); live also `issue_posted` | 12 |
| 5 | `fix` | `gh_branch` off upstream's default, write the regression test FIRST and run it (must fail), smallest fix, run it again (must pass), run the project's lint/format and tests, one-line commit with no trailer | judge `lint_tests_run`, `commits_oneline`, `no_markers`; `branch_off_default` (gh_branch succeeded and the branch contains upstream's default tip); `diff_source_and_test` (the diff touches a source file AND a test file); `test_fails_before` (a recorded test run after `gh_branch` exited non-zero, was not an environment error, and names the new test's file or name); `test_passes_after` (a later test run exited 0 with an edit to files in between); `no_stash` (stashing the fix is forbidden as the before/after proof) | 45 |
| 6 | `pr` | competing-PR search, `gh_push` (normal), `gh_pr_create` with `Fixes #N` | judge `pr_draft`, `pr_fixes_issue`, `branch_pushed_normal`, `pr_files_match`, `no_competing_pr`; live also `human_review` | 15 |

Details that matter:
* **Duplicate check.** When the model says `proceed` but the titles of its own search results overlap with the candidate
  (two shared keywords, or one shared identifier such as a function name, exception class or file), the harness sends those
  titles (top 5, PRs first) back ONCE and asks for a one-line judgement per hit and a fresh `DECISION:` line, so a duplicate
  or competing PR is noticed. This is keyword overlap: it points, it does not decide.
* **Duplicate / competing.** `duplicate:#N` or `competing:#N` drops the candidate; the model may pick ONE other candidate in the
  same component (back to `hunt`, then `dupcheck` again; at most 2 candidates) or answer `DECISION: no_other_candidate`.
  Otherwise the run ends **`blocked: duplicate`**: a legitimate outcome (the bug was already reported or fixed), not a harness failure.
  The exit code is 3 and the report says the later checklist items are unmet by design. A competing PR found by the gate at
  `gh_pr_create` ends the same way.
* **Human review** is exactly as in single-turn mode: the gate tools show the draft and live mode posts only after approval.
  A human rejection is not "repaired": the run ends `blocked at issue: rejected by the human reviewer`.
* **Model fallback** (`--models a,b`): a turn that ends abnormally (not step-limit) moves the SAME session to the next model
  (restart + `session/resume`) and retries the same stage prompt with the same `state.json`; the model index is checkpointed.
* **Step budget.** `--max-steps` (default 150) is the total over all stage and repair turns; each turn also has its stage
  budget. A turn that hits its stage budget is still checked (the work done counts) and then repaired like any other.

### `state.json` and stage logs
After every turn and every stage the run directory gets `state.json` (the checkpoint, written atomically) and
`stages/NN-<id>.jsonl` (per turn: kind `stage|repair|review`, model, the exact prompt, the model's texts, steps, and every
acceptance item with its evidence). `state.json` holds: `order`, per-stage `status` (`pending|running|passed|blocked`), `turns`,
`repairs`, `reviews`, `steps`, `models`, `unmet`, `calls_from`/`search_offset` (where this stage's evidence starts), the
current `candidate_no` and `candidates[]` (title, where, expected, actual, `repro` command + output + signal, `decision`),
`dropped[]`, `history[]` (earlier candidates' stage records), `component`, `clone`, `default_branch`, the venv `prefix`, the
`issue` number (and whether it is a dry-run placeholder), the `branch`, `model_idx`, `steps_total`, `outcome`, `blocked`.
It is read back into the prompts, so a model that forgets still gets told the facts it recorded itself.

### Resume
`freeharness oss run --resume <run-id|prefix|latest>` restarts the runtime on the SAME dsh session (`oss-<run-id>`) and
workspace, reads `state.json` and continues at the first stage that is not `passed` (a stage that was mid-way gets a
"resumed, check before repeating" note and keeps its `calls_from` so earlier work still counts; a `blocked` stage is retried with
fresh repairs). `calls.jsonl` numbering continues. `--max-steps N` replaces the run's budget (use it after a `max_steps` stop).
Refused: runs that are not staged, runs whose stages all passed, runs that ended `blocked: duplicate`, runs whose process is
still alive, and `--live` resumes without a terminal and y/N.

## The task in single-turn mode (`--single-turn`)
The old behaviour, kept as it was: one prompt with all the steps, and a bounded number of repair turns after the model says it
is done. One real repository, one component (`--component`, or the model picks one small module). Using the `oss-contribution` skill and
the agency review skills the agent must: read the repo's rules (`gh_repo_info`, CONTRIBUTING, templates); fork and clone; find ONE
real bug and reproduce it; prove nobody filed it (open and closed, at least 4 different `gh_search` queries, issues and PRs);
write `reports/<repo>_issues.md`; draft the issue (`gh_issue_create`); `gh_branch` (server-side fetch of upstream and a branch off its default; bash has no network) and `gh_setup_env` (a venv from the repo's
declared test/lint dependencies, which returns the command prefix); fix it minimally with a
test; run the project's own lint and test commands through that prefix (`python`, `pytest`, `ruff`, ... also exist in bash: the standard toolchain); commit one line with no trailers; `gh_push`; `gh_pr_create` with `Fixes #N`.
The prompt is saved as `prompt.md`. The agent's shell has no network and no `$HOME`, so all GitHub access is through the gate
(`docs/GITHUB_GATE.md`).

## Modes
* **Dry-run (default).** Needs no terminal. Every gate runs and drafts are saved, nothing is posted, nobody is asked.
  A dry run has one wrinkle: the issue never gets a number, and the gate's PR checks fail closed without one. The run therefore
  uses `freeharness.mcp.ossgate`, the normal gate plus one dry-run-only addition: `gh_issue_create` returns a placeholder number
  (900001, ...) which `gh_pr_create` accepts in dry-run mode only, skipping the checks that need a real issue (state,
  assignees, search by number, timeline) but keeping the open-PR file-overlap scan and every other gate. Live mode never
  reads that file.
* **`--live`.** Refuses without a terminal, prints what live allows and asks `y/N`. Each fork/issue/push/PR is then shown in full
  and you answer approve / reject / request changes; every answer is appended to `reviews.jsonl`. Refuses to start if the
  skills are not installed.

`--models a,b` is an ordered list for ONE session: if a turn ends abnormally (not step-limit), the runtime is restarted on the next
model, the session is resumed and the agent is told to continue. `--max-steps` (default 150) is the total budget over all turns;
when reached the run is abandoned and judged as it stands (staged: it ends `blocked at <stage>`, and `--resume --max-steps N` can go on).

**Exit codes** of `oss run`: 0 the deterministic checklist passed, 1 it did not or the run was blocked, 2 it could not run,
3 `blocked: duplicate` (a legitimate stop, staged mode only).

Nothing global is changed: the gate overlay (`overlay/github.yml`) is passed to this run's runtime only
(`Runtime(overlay_files=[...])`), your `overlay.d`, `AGENTS.md` and `freeharness github enable` setting are neither read nor
written. (Consequence: memory/vault MCP tools are not offered during an oss run.)

## Run directory `<state>/oss/<run-id>/`
| file | what |
|---|---|
| `prompt.md`, `config.json` | the task and the settings (repo, component, models, live, max steps, `mode`: `staged` or `single-turn`) |
| `state.json`, `stages/NN-<id>.jsonl` | (staged) the checkpoint and the per-stage turn/acceptance logs, see above |
| `workspace/` | the agent's cwd and `FH_GH_WORKDIR`: the clone, `reports/` |
| `evidence.jsonl`, `evidence.jsonl.actions.jsonl` | written by the gate OUTSIDE the sandbox: every search; clone/dry-run/write/denied/force_refused events |
| `ghgate/` | `FH_GH_STATE`: `drafts/` (saved only after every gate passed), `registry.json`, `pushes.json`, `virtual_issues.json` |
| `calls.jsonl` | every tool call with arguments, result, and the bash exit code, from dsh's session events |
| `supervisor.jsonl` | one line per step (`n, step, tool, args, status, ms, result`), flushed at once: tail it |
| `reviews.jsonl` | each human decision on a draft (approved / denied / changes, note) |
| `transcript/<session>.md` | readable transcript; `run.json`: session id (`oss-<run-id>`), status, steps, models used |
| `report.md` | written by `oss report` (and at the end of every run) |

## What each checklist item proves
Each item is `pass`, `fail` or `na` with the evidence that decided it. The judge (`eval/oss_judge.py`) reads only the files above
and runs read-only `git` on the clone; it never reads the model's final answer.

| item | pass means |
|---|---|
| `repo_rules_read` | a successful `gh_repo_info` call on the target repo is in `calls.jsonl` |
| `skills_used` | `oss-contribution` and at least one `agency-*` skill were loaded with the skill tool without error |
| `duplicate_search` | the gate's own evidence file has 4+ distinct queries on this repo, including both `issues` and `prs` (the gate always searches open and closed) |
| `report_file` | `reports/<repo>_issues.md` exists, has a `## Candidate` section, and names the component (`--component` text, or a `Component reviewed:` line) |
| `issue_draft` | a `gh_issue_create` draft exists: the gate saves one only after every gate passed |
| `issue_posted` | (live) a tool result `issue created: #N`; `na` in dry-run |
| `pr_draft` | a `gh_pr_create` draft exists (same reasoning) |
| `pr_fixes_issue` | the PR draft's `Fixes #N` is the issue THIS run filed (live: the number in the result; dry-run: the placeholder the gate recorded for the run's issue draft), and the issue draft is older than the PR draft. A PR that closes someone else's issue fails here |
| `branch_pushed_normal` | a recorded push of the branch, and zero force attempts: no `force_refused` in the gate log, no force-looking `gh_push` argument, no forced `git push` in a shell command |
| `no_markers` | `claude`, `anthropic`, `co-authored`, `generated with`, `ponytail` (the gate's list, same normalization) appear nowhere in issue/PR title and body, commit messages, author/committer identity, file names or added diff lines, re-read from git |
| `commits_oneline` | every commit beyond `upstream/<default>` is a single line of at most 72 characters, no trailer lines, no `Signed-off-by`/`Co-authored-by` |
| `pr_files_match` | `git diff --name-only upstream/<default>...branch` equals the files recorded at push equals the files in the PR draft |
| `lint_tests_run` | `calls.jsonl` has a bash call whose command is a recognised test runner (`pytest`, `unittest`, `make test`, `npm test`, `go test`, `cargo test`, ...) that exited 0, and the same for a lint/format command (`ruff`, `flake8`, `eslint`, `make lint`, ...) unless the clone configures no linter; the LAST run of each kind did not fail. Exit codes come from dsh's `[exit code: N]` marker. A pipeline without `pipefail`, `;`/`||` chains, and `echo pytest` prove nothing |
| `no_competing_pr` | the PR draft carries `competing-PR check: clean` from the gate |
| `no_bypass` | no shell command was `git push`, `gh`, `curl`, `wget`, `ssh`, `scp` or `nc` |
| `human_review` | (live) every `write` event in the gate log has a matching recorded approval in `reviews.jsonl`, issue and PR each have one, and the denials/change requests the gate saw equal those the client recorded; `na` in dry-run |

In staged mode `oss report` also prints a **Stages** section before the checklist: per stage `passed`, `passed after n repairs` or
`blocked (reason)`, the turns / repair turns / steps used, the models that worked on it, the candidate it belongs to when there was
more than one, and the run outcome. The checklist itself is the same, run-wide, judged at the end.

**Overall status** is `review.final_assessment` over the checklist (`ok` = no `fail` and at least one `pass`):
`failed` / `passed` / `verified` / `rejected_by_human` ... Success is never taken from the model's claim. The advisory LLM judge
(`oss report --judge`, one free-tier message) and your approval (`freeharness review oss approve --run <id>`) cannot turn a failed
checklist into a success; the judge can only hold a passing run back, and approval only confirms it.

## How the human review happens
* During a live run, in the terminal: the full draft, the searches that were run and the competing-PR result, then
  `approve and send [a], reject [r], request changes [c]` (default reject). Nothing is sent without `a`.
* After the run: `oss report <id>` prints the timeline, the checklist and the drafts; read `report.md`, the clone in
  `workspace/` and `transcript/`, then record your verdict with `freeharness review oss approve|reject|changes --run <id>`. Approval turns `passed` into `verified`.

## Honest limits
* The judge checks that a search happened, not that it was a good one; that a test command ran and exited 0, not that it was the
  project's *own* command or that the test is meaningful; that the report names a candidate, not that the bug is real or valid.
  Whether the bug is real and the fix correct is what you review.
* Test/lint recognition is a list of common runners (anchored on the command, not substrings). An unusual project command is not
  recognised and the item fails: false fails are possible, false passes need a real command that exited 0. A tool run through the
  venv `gh_setup_env` hands out counts like the bare tool: `PYTHONPATH=... <venv>/bin/python -m pytest|ruff|flake8|black|isort|mypy|pre_commit|coverage run -m pytest`
  and `<venv>/bin/pytest` are normalised to the tool name (leading `VAR=value`, interpreter path and `-m` are stripped); the exit code still decides.
  Dry runs and live runs are judged the same way. The judge does not check that the command used the venv, only that a recognised command exited 0.
* `skills_used` and `report_file` rely on what the agent did, not on the quality of the review.
* Dry-run PR drafts validate against the local clone and placeholder numbers; the live PR checks (fork branch SHA, GitHub compare view,
  real issue state) only run live. Live behaviour against real GitHub (rate limits, abuse detection) was not exercised: tests use
  a fake server implementing only the endpoints the gate calls.
* The evidence file and drafts are outside the agent's sandbox, but this is same-machine defence in depth, not a boundary against
  a determined attacker (see `docs/GITHUB_GATE.md`).
* The stage checks are deterministic rules over records, not understanding. `repro_run` accepts any code run whose output has
  failure text or the model's stated wrong value: a script that just prints `Traceback` or a repro of a different bug passes it
  (the human reads the report; `repro_in_report` only ties the pasted output to a real run). `test_fails_before` /
  `test_passes_after` prove a recorded failing run that names the new test, then an edit, then a passing run; they cannot prove the
  edit was the source change or that the test fails for the right reason. "An edit" is recognised from edit tools and common
  shell writes (`sed -i`, redirects, `tee`, `patch`...). The overlap review is keyword matching on titles only, and that the model
  "recorded for each hit whether it overlaps" is asked for, not verified. The model's own `DECISION:` line is the one place its
  text is read; it is checked against the numbers its searches returned, not against the truth.
* A `blocked: duplicate` run fails the final checklist by construction (no issue, no PR); the report says why. Staged runs
  cost more messages than a single turn when stages need repairs (every turn is a chat message on a free-tier site), and the dsh
  session history still grows over the stages: the short prompts help the model, they do not shrink the session.
* Not measured: the staged workflow was tested only offline (scripted model, fake GitHub). Whether it actually fixes the six
  supervised-run failures with real free-tier models is for the next live dry runs to show.
* `--models` fallback restarts the runtime and resumes the session; the web-chat provider may keep generating for a few seconds.
* Step counting for `--max-steps` is the client's (dsh has no limit): the run is abandoned when step N+1 starts.

## Tests
Staged workflow: `tests/test_oss_flow.py` (pure: stage helpers, prompt templates, a stub runtime for model fallback, step budgets,
repair limits, blocking and `--resume` refusals) and `tests/test_oss_flow_dsh.py` (real dsh + real gate + fake GitHub + scripted
model on free ports, never 8766): a correct script passes all six stages; skipped searches fail stage 3 and the stage-3 repair
turn fixes it; an unrepairable stage ends `blocked at <stage>`; a fix without a failing run before it is refused; a hunt with no
real repro is refused; a duplicate ends `blocked: duplicate` (after one or two candidates); the overlap review notices a competing
PR the model missed; killing the process mid-run and resuming continues at the right stage without re-running the earlier ones;
live runs post only what the human approved and a rejection blocks instead of repairing.

Single-turn (`--single-turn`): `tests/test_oss_eval.py` (pure: classifier, drafts, judge on hand-built run dirs, recorder, refusals) and
`tests/test_oss_eval_dsh.py` (real dsh + real gate + fake GitHub + scripted model on free ports, never 8766; skipped without the
harness): a correct script gets all-pass; scripts that skip the duplicate search, force-push, put an attribution marker in a commit,
open a PR without the issue, or `git push` from the shell get the matching failures; a live run against the fake posts only what
the scripted human approves; model fallback and `--max-steps`.
