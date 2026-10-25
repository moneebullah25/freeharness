# Open-source agent skills: `freeharness agents`

```bash
freeharness agents install              # the curated set (`--set oss`): 9 Agency skills + oss-contribution
freeharness agents install --set all    # the whole roster: prints a warning and needs --yes
freeharness agents list                 # what is installed, and how big the skills listing is
freeharness agents remove               # removes exactly what `install` put there
```
Restart a running `freeharness` afterwards; skills are scanned at startup.

## What it installs
[The Agency](https://github.com/msitarzewski/agency-agents) (MIT) publishes about 280 agent personas and a converter for
DeepSeek Harness: `convert.sh --tool dsh` then `install.sh --tool dsh` write `<dsh home>/skills/agency-<slug>/SKILL.md`.
`freeharness agents install` runs those two upstream scripts for you, into the freeharness dsh home
(`~/.local/share/freeharness/dsh-home/skills` on Linux, `paths.dsh_home()`), and nothing else.

## Why only a handful: the prompt budget
dsh sends every skill's name and description to the model on every step (one message listing `- \`name\`: description`).
Measured with the real harness and `tests/fake_openai.py` (dsh 0.2.1-rc.2, 2026-10-25):

| installed | skills | listing in the request | ~tokens |
|---|---|---|---|
| nothing | 0 | 0 chars (no message) | 0 |
| `--set oss` | 10 | 2,798 chars | ~700 |
| `--set all` | 284 | 74,270 chars | ~18,600 |

The budget is 16k tokens in total, so `--set all` would not even leave room for the question. It exists for people who
use a larger-context provider; it prints that warning and refuses without `--yes`.

### The `oss` set (picked by reading the agent files)
| skill | why |
|---|---|
| `agency-code-reviewer` | correctness/security review of one component: the bug-hunting lens |
| `agency-reality-checker` | defaults to "NEEDS WORK", demands proof: the skeptic for the vote gate |
| `agency-application-security-engineer` | secure code review (the Agency has no plain "security engineer"; this one reviews code, the others are SOC/pentest/cloud) |
| `agency-software-architect` | design-level problems |
| `agency-backend-architect` | APIs, data, server code |
| `agency-evidence-collector` | the QA pick: reports only reproducible issues, marks untested scope. Its persona leans on screenshots; the review rules still apply without a browser |
| `agency-technical-writer` | README, docs, issue and PR text |
| `agency-git-workflow-master` | branches, commit hygiene, conflicts |
| `agency-minimal-change-engineer` | smallest-diff discipline for PRs (not in the brief; added because it is the PR rule) |
| `oss-contribution` | bundled with freeharness, see below |

## Safety of the install
- **Pinned.** The repo is cloned over https into a fresh `mktemp` directory and checked out at a full commit SHA
  (`agents_cmd.DEFAULT_REF`). HEAD must equal it afterwards or nothing runs. `--ref` takes only a full SHA, never a branch.
- **Scripts reviewed and checksummed.** For the default SHA, `convert.sh`, `install.sh` and `lib.sh` are compared with SHA-256
  values recorded after reading them in full. Findings of that review: no network access, no `sudo`; the only writes are
  `integrations/dsh/` inside the clone, a `mktemp` log in `$TMPDIR`, and the destination. The one trap: the destination is
  `--path`, else `$DSH_SKILLS_DIR`, else `$DSH_HOME/skills`, else `~/.dsh/skills`, so a stray `DSH_SKILLS_DIR` in your shell
  would redirect it. We therefore run with a scrubbed environment (`HOME` and `TMPDIR` inside the temp dir, `DSH_HOME` = our
  dsh home, no `DSH_SKILLS_DIR`) and pass `--path` explicitly. Only `--tool dsh` is ever used; `~/.dsh` and `~/.claude/agents`
  are never touched.
- **Other `--ref` or `--repo`.** An unreviewed commit needs `--yes` because its shell scripts then run on your machine.
- **Manifest.** `<data>/agents-installed.json` lists what was installed. `remove` and a later smaller `--set` delete only
  those exact folders; skills you wrote yourself are left alone (`list` marks anything else "not ours"). An existing
  `oss-contribution` that freeharness did not install is not overwritten.
- Updating the pin: read the new scripts, refresh `SCRIPT_SHA256`, `DEFAULT_REF` and `ALL_MEASURED` in `agents_cmd.py`.

## The `oss-contribution` skill
A project-independent, 14-step version of the open-source contribution routine for a weak model: fork and clone, read
CONTRIBUTING, review ONE component with the agency skills, duplicate check (issues and PRs, open and closed, 4+ queries),
write `reports/<repo>_issues.md`, vote gate (reproduced / not duplicate or fixed / maintainers would call it valid), post only
after human confirmation, one issue per branch off the upstream default, one-line commits without trailers, conventions and
tests, competing-PR check, PR with the repo template and `Fixes #N`, review fixes as NEW commits (never amend or force-push),
reply with the SHA, then resolve. `SKILL.md` stays under 3,500 characters; per-step detail is in `references/`.

It relies on the `gh_*` tools of the `ghgate` MCP server (gh_repo_info, gh_search, gh_get_file, gh_list_pr_files,
gh_pr_threads, gh_fork, gh_clone, gh_branch, gh_setup_env, gh_push, gh_issue_create, gh_pr_create, gh_pr_reply, gh_pr_resolve_thread). Those tools
enforce the hard rules; the skill tells the model that a refusal means stop and report, never work around it. The skill
cannot enforce anything itself: without `ghgate` it is only advice.

## Tests
`tests/test_agents_cmd.py`, offline: a local fixture git repo with a miniature Agency layout (built with `git fast-import`,
no git identity needed). Covers pinning, refusal of unknown/non-SHA/unreviewed refs and tampered scripts, HEAD mismatch,
idempotent reinstall, `--set all` needing `--yes`, switching sets, list, remove (including hostile manifest names), the leaked
`DSH_SKILLS_DIR`/`HOME` staying untouched, and, with the real dsh (skipped if missing) against `tests/fake_openai.py`, that the
installed skills are in the request. Not covered: the real network clone and the real upstream scripts (run once by hand).
