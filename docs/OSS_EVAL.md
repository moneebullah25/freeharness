# OSS contribution evaluation

```bash
freeharness agents install                      # once: the agency review skills + oss-contribution
export GITHUB_TOKEN=...                         # every gh_* tool needs it, also in dry-run (read access is enough for dry-run)
export FH_GH_GIT_NAME="Your Name" FH_GH_GIT_EMAIL="you@example.org"   # commit identity gh_clone writes into the clone

freeharness oss run owner/repo --component src/core --models gemini,deepseek     # dry-run: nothing is posted
freeharness oss run owner/repo --live                                             # terminal + y/N, you approve each post
tail -f ~/.local/state/freeharness/oss/<run-id>/supervisor.jsonl                  # from another terminal, while it runs
freeharness oss report <run-id|latest>                                            # timeline + checklist, writes report.md
freeharness review oss approve|reject|changes --run <run-id> --note "..."         # your final decision on the run
freeharness oss list
freeharness eval --suite oss                                                      # only LISTS this evaluation; never runs it
```

## The task the agent gets
One real repository, one component (`--component`, or the model picks one small module). Using the `oss-contribution` skill and
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
when reached the run is abandoned and judged as it stands.

Nothing global is changed: the gate overlay (`overlay/github.yml`) is passed to this run's runtime only
(`Runtime(overlay_files=[...])`), your `overlay.d`, `AGENTS.md` and `freeharness github enable` setting are neither read nor
written. (Consequence: memory/vault MCP tools are not offered during an oss run.)

## Run directory `<state>/oss/<run-id>/`
| file | what |
|---|---|
| `prompt.md`, `config.json` | the task and the settings (repo, component, models, live, max steps) |
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
* `--models` fallback restarts the runtime and resumes the session; the web-chat provider may keep generating for a few seconds.
* Step counting for `--max-steps` is the client's (dsh has no limit): the run is abandoned when step N+1 starts.

## Tests
`tests/test_oss_eval.py` (pure: classifier, drafts, judge on hand-built run dirs, recorder, refusals) and
`tests/test_oss_eval_dsh.py` (real dsh + real gate + fake GitHub + scripted model on free ports, never 8766; skipped without the
harness): a correct script gets all-pass; scripts that skip the duplicate search, force-push, put an attribution marker in a commit,
open a PR without the issue, or `git push` from the shell get the matching failures; a live run against the fake posts only what
the scripted human approves; model fallback and `--max-steps`.
