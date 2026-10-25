# GitHub gate (open-source contribution tools)

`freeharness github enable [--live] | disable | status` connects the stdio MCP server `freeharness.mcp.ghgate`
(tools `gh_*`) to the agent. The agent's bash has no network and no `$HOME`, so git and GitHub access happens
**only** through this server, which runs outside that sandbox. The user's pipeline rules are code paths in the
server, not prompt text. Tests: `tests/test_ghgate.py` (offline, fake GitHub REST/GraphQL server on a free port,
local bare git repos, a real confirmation socket) and `tests/test_ghgate_dsh.py` (real dsh, scripted model).

## Flow the user sees

The agent drafts an issue or PR. The terminal shows the **full draft** (title, body, base/head, file list, the
searches that were run, the competing-PR check result) and asks: **approve [a] / reject [r] / request changes
[c]** (default reject). Approve: the server performs the write and returns the URL and number. Reject: refused.
Request changes: your note is returned to the model as the tool result ("nothing was sent, revise and call again").
Every draft is also saved to `<state>/ghgate/drafts/<id>.md`.

Modes: **dry-run is the default.** Every gate is validated and the tool returns what would be sent (and saves
the draft), with no write to GitHub and no prompt. `freeharness github enable --live` shows what live allows and
asks y/N once (it refuses without a terminal); the interactive client then prints a red banner at start.
`freeharness github enable` (no flag) returns to dry-run. Inside the server only the exact value
`FH_GH_DRYRUN=0` is live; unset, empty or anything else is dry-run.

## Enforced in code (each has tests)

| Gate | What is enforced |
|---|---|
| Human confirmation | fork, issue, PR, push, review reply and thread resolve ask through the client's unix socket (token-checked; same socket and `Approver` class as sandbox approvals, new message `type: "confirm"`, the sandbox protocol is unchanged). No socket, no TTY, `-p`, wrong token, timeout (`FH_GH_CONFIRM_TIMEOUT`, default 240 s), a crashing answerer, or anything but an explicit approve = refused. The model's text is never consulted. Terminal escapes in the model's text are stripped before display. |
| Dry-run | default on; no GitHub write, no push; all gates still run. |
| Token | read from `GITHUB_TOKEN` (or the `GITHUB_TOKEN` line of the file named by `FH_GH_ENVFILE`; no other key is read). Every tool result, protocol message, log line, draft and state file passes through `redact()`; the overlay row carries only the variable *name*. Test greps outputs, stderr, evidence, actions log, drafts and state for the token, including an API error that echoes it. Redirects are never followed (the token cannot be forwarded elsewhere). |
| Issue | refused unless: issues enabled, repo not archived, no approval/permission/assignment requirement detected in CONTRIBUTING, >=4 distinct `gh_search` queries on that repo **in this run** (default evidence file is tagged per server run) with at least one `issues` and one `prs` kind, no tooling/attribution markers in title/body, human approved. |
| Pull request | refused unless: `pull_request_creation_policy` allows (unset = allowed; any value other than `all` refuses), base is the default branch, head is on your own fork, body contains `Fixes #<issue>`, no markers, a recorded `gh_push` of that branch whose head SHA is the one on your fork, the PR's file list (GitHub compare) equals the pushed diff's file list, no artifact-looking file (`.db .sqlite .bin .pyc .so .exe .jar .zip ...`, `.env*`, `__pycache__`, `node_modules`, over 1 MB), the competing-PR check (below) clean, human approved. |
| Competing-PR check | issue must be open, a PR not an issue, and not assigned to someone else; no other user's open or merged PR found by searching PRs for the issue number, no such PR in the issue timeline cross-references, issue not closed by a commit, and none of the 40 most recently updated other open PRs touches the same files. **Any API error or unexpected response refuses (fail closed).** |
| Push | normal `git push` of `refs/heads/<branch>:refs/heads/<branch>` to your fork only (URL comes from a registry outside the workspace, not from the clone's config). Refuses the default branch; refuses and logs `--force`, `-f` (also combined like `-fu`), `--force-with-lease`, `+refspec`; never amends (a rewritten branch is rejected by git as non-fast-forward and reported, never forced). Before pushing it fetches upstream's default branch fresh, then scans commit messages, author/committer identity, changed file names and all added diff lines for `claude`, `anthropic`, `co-authored`, `generated with`, `ponytail` (case-insensitive, Unicode-normalized, zero-width characters removed); refuses artifact-looking files. Clone hooks are disabled (`core.hooksPath=/dev/null`, `--no-verify`), a clone whose config rewrites URLs or includes other config is refused, `.git` must be a real directory. Origin must be your own fork of the upstream. |
| Clone | destination must resolve (symlinks included) inside `FH_GH_WORKDIR` (default: the session workspace `FREEHARNESS_CWD`); SSH via host alias `FH_GITHUB_SSH_HOST` (default `github.com`); adds `upstream`, restricts `remote.upstream.fetch` to the default branch, sets `remote.upstream.pushurl=DISABLED`. Live clone requires your fork to exist; a dry-run clone of an unforked repo is read-only (origin pushing disabled). |
| Review replies | only on your own PRs, thread must belong to the PR, `sha` must be a commit in the PR, the sha is added to the text if missing, markers refused; resolving requires a recorded live reply to that thread first (a dry-run reply does not count for live). |
| Search | `repo:`, `is:`, `state:`, `type:`, `org:`, `user:`, `archived:` qualifiers are stripped from the model's query so open **and** closed are always searched; each successful call appends `{query, kind, repo, time, count}` to the evidence file. |

## Best-effort or not covered

- **Approval/CLA detection** is regex matching on CONTRIBUTING only (README, wikis, bots and maintainer comments
  are not read). A missed rule or a false positive is possible; CLA and DCO are reported, not gated.
- **Marker scan** is a substring match: repos or paths that legitimately contain those words (for example the
  anthropic SDK) are refused until the user edits by hand. Paraphrased attribution ("AI-assisted") is not caught.
- `pull_request_creation_policy` is read from the repo REST object; if GitHub does not return it the repo is
  treated as unrestricted.
- Competing check compares files for the 40 most recently updated other open PRs only (a note says when truncated)
  and the search relies on the PR text mentioning `#N`. A PR that fixes the issue without saying so is invisible.
- Evidence quality: the gate counts distinct queries and kinds; it cannot judge whether they were good searches.
- The default evidence file is per server run, so a runtime restart (step limit, Ctrl-C) resets it; set
  `FH_GH_EVIDENCE` to keep one file across runs.
- Git: repo-local config that can run programs (filters, `core.*` programs other than those overridden) is not
  exhaustively neutralized; the clone lives in the agent-writable workspace. This is same-machine defense in
  depth, not a boundary against a determined attacker. The server and the dsh process are the trusted side.
- Commit identity: the sandbox has no `$HOME`. Set `FH_GH_GIT_NAME` / `FH_GH_GIT_EMAIL` in your shell and they are
  written to the clone's local config by `gh_clone`; freeharness never invents an identity.
- Live GitHub behaviour (rate limits, abuse detection, GraphQL schema drift) was not exercised: tests use a fake
  server that implements only the endpoints the gate calls.

## Environment

| Variable | Meaning |
|---|---|
| `GITHUB_TOKEN` | token for the REST/GraphQL API (needs repo scope); export it where you start freeharness |
| `FH_GH_ENVFILE` | alternative: file with a `GITHUB_TOKEN=` line |
| `FH_GH_DRYRUN` | `0` = live; anything else = dry-run (written by `enable`) |
| `FH_GH_WORKDIR` | folder clones must live in (default: session workspace) |
| `FH_GH_EVIDENCE` | evidence file (default `<state>/ghgate/evidence.jsonl`, per run) |
| `FH_GH_STATE` | state folder (default `<state>/ghgate`: registry, pushes, replies, drafts, actions log) |
| `FH_GITHUB_SSH_HOST` | SSH host alias for clone/push (default `github.com`) |
| `FH_GH_API` | API base URL (tests; default `https://api.github.com`) |
| `FH_GH_GIT_URL` | git URL template with `{host}` `{repo}` (tests) |
| `FH_CONFIRM_SOCKET`, `FH_CONFIRM_TOKEN` | set by the client for the server (mapped from the approval socket by the overlay row) |
| `FH_GH_CONFIRM_TIMEOUT` | seconds to wait for the human (default 240) |
