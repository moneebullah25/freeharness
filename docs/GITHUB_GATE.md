# GitHub gate (open-source contribution tools)

`freeharness github enable [--live] | disable | status` connects the stdio MCP server `freeharness.mcp.ghgate`
(tools `gh_*`: 14, including `gh_branch` and `gh_setup_env`) to the agent. The agent's bash has no network and no `$HOME`, so git and GitHub access happens
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
| Branch (`gh_branch`) | not a GitHub write, so no confirmation. Needs a clone made by `gh_clone` (same registry, `.git` and config checks as push). Name must match `^[A-Za-z0-9][A-Za-z0-9._/-]*$` and pass `git check-ref-format`; refused: the default branch name (case-insensitive), `HEAD`, `@`, `..`, `//`, leading dots, `.lock`, `@{`, names starting `refs/ heads/ remotes/ upstream/ origin/`, a branch that already exists, a dirty tree (tracked changes or untracked files), a clone config that defines `filter.*` or diff drivers. It then runs the same fetch `gh_push` uses (URL from the registry, never the clone's remotes; one refspec `+refs/heads/<default>:refs/remotes/upstream/<default>`; no tags, no submodules; hooks, fsmonitor, credential helpers and the system config disabled; `GIT_SSH_COMMAND` from the server's environment beats the clone's `core.sshCommand`) and `git checkout --no-track -b <name> refs/remotes/upstream/<default>`. Returns the branch and the HEAD SHA. Exists because the agent's bash has no network: `git fetch && git checkout -b` there fails at the fetch and the chain skips the checkout. |
| Environment (`gh_setup_env`) | not a GitHub write, no confirmation. Builds `<workdir>/<repo>-venv` (outside the clone) so bash can run the project's tests and lint, and returns a ready command prefix (`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<clone>/src:<clone> <venv>/bin/python -m`, used as `cd <clone> && <prefix> pytest -q`). **What is installed:** only requirements the repo declares, read from upstream's committed files (`refs/remotes/upstream/<default>`, so edits the agent made in the working tree cannot add packages): `pyproject.toml` `[project].dependencies`, `optional-dependencies` and `dependency-groups` named dev/test/lint/ci/check/style/typing/quality/qa (or named in `extras`), top-level `requirements*.txt` and `requirements/*.txt` for base/dev/test/lint/ci names (or named in `extras`), `-r` includes inside the repo only. The project itself is never installed and nothing from the repo is executed outside the sandbox (no `setup.py`, no build backend). **Skipped and listed in the result:** `-e`, local paths, VCS/URL requirements (only `https://pypi.org` and `https://files.pythonhosted.org` URLs pass), `--index-url`/`--extra-index-url`/`--find-links`/`--trusted-host`/`--no-index`/other pip options, constraint files, any line that is not a plain `name[extras] version ; marker`. The index is never taken from a repo file: it is `FH_GH_PIP_INDEX` (default `https://pypi.org/simple`). **Where pip runs:** inside a bubblewrap sandbox built by `data/fh_sandbox_runner.py --net-install`, the one caller of that mode: network on; `$HOME`, `/tmp`, `/run`, the whole work folder (clone included) hidden; environment cleared (only proxy and CA variables pass through); `--isolated --no-cache-dir`; the venv directory is the only writable path; hard timeout (`FH_GH_ENV_TIMEOUT`, default 600 s) with the process group killed; output capped (first 1.5 KB + last 6 KB kept). No bwrap or not Linux: refused before anything runs. A failure deletes the half-built venv. The venv inherits the standard toolchain (see below) through a `.pth` file, so pytest/ruff/... are not reinstalled per project. After the build the tool runs the venv's python and each tool's `--version` inside the agent's own bash sandbox and reports what works there. Re-running with the same declared set reuses the venv. |
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

## Installing dependencies (`gh_setup_env`): trust model and limits

The agent's bash has no network. Installing the project's test/lint dependencies therefore has to happen on the
server side, which is the one place a package from a public index runs with network access. The design keeps that
to the narrowest thing that works:

- **Who chooses packages:** the project's maintainers (their committed files at upstream's default branch), filtered
  to plain PyPI requirements. The agent can only pick which of the repo's own declared groups/files to include
  (`extras`, names validated). It cannot name a package, an index, a URL or a pip option.
- **Where code from a package can run:** `pip install` may run build code of source distributions of those
  dependencies (and `--prefer-binary` only prefers wheels). That code runs inside the install sandbox: it can reach the
  network (including loopback services on this host, since the network namespace is shared) and read the host's
  system directories (`/usr`, `/etc`, ...), but not `$HOME`, `/tmp`, `/run`, other folders of the work folder, nor
  the server's environment (token included); it can write only into the new venv. Nothing is installed or run from the
  repo itself in the unsandboxed server.
- **What it does not defend against:** a malicious or compromised package (or index) can still put arbitrary Python into
  the venv, which later runs in the agent's no-network sandbox (harmless there) and, if a human runs the same venv by
  hand, with that human's rights. Hash pinning is not enforced (`--hash` options are dropped). A package can
  exfiltrate what the install sandbox can read (system files) over the network. Same-kernel confinement (bubblewrap),
  not a VM. The standard toolchain is installed the ordinary way by `freeharness toolchain install`, outside any sandbox,
  from versions pinned in `toolchain.py` (dependencies of those float).
- **Failing closed:** no `bwrap`, not Linux, a runner that cannot build the sandbox, a timeout, or a failing pip: nothing is left
  behind and the result says so. Requirements the tool had to skip are listed, never silently dropped.
- **Limits of the result:** the project is not installed (imports come from `PYTHONPATH`): tests that read installed
  metadata, entry points or compiled extensions can fail for that reason; Poetry-only dependency tables are not read;
  `pre-commit` hooks need network and will not run in bash. No network test against a real index exists in the suite
  (it uses a local wheelhouse with `pip --no-index --find-links`); run `freeharness toolchain install` once for a real check.

## The standard toolchain

`freeharness toolchain install` (also run by `freeharness setup`; `status` and `doctor` report it) builds
`<data>/toolchain-venv` with pinned pip, setuptools, wheel, build, pytest, coverage, ruff, flake8, black, isort, mypy and
pre-commit. The sandbox runner binds it **read-only** and puts its `bin/` first on PATH, so bash has `python`, `python3`
and these tools without any install, still with no network and no `$HOME`. `doctor` shows `standard toolchain: full |
partial | missing`, measured by running `command -v` and `--version` inside the real sandbox, not from the files on disk.

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
| `FH_GH_PIP_INDEX` | index URL for `gh_setup_env` (default `https://pypi.org/simple`; operator setting, never from the repo or the model) |
| `FH_GH_PIP_FIND_LINKS` | a local wheelhouse directory instead of an index (`--no-index`; tests, air-gapped setups) |
| `FH_GH_ENV_TIMEOUT` | seconds `gh_setup_env` may take in total (default 600) |
| `FH_TOOLCHAIN` | standard toolchain venv location (default `<data>/toolchain-venv`) |
| `FH_SANDBOX_TOOLS` | extra read-only tool roots for the bash sandbox, `/a:/b` (each root's `bin/` goes on PATH) |
| `FH_GH_CONFIRM_TIMEOUT` | seconds to wait for the human (default 240) |
