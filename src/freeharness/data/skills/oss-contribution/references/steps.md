# oss-contribution: details per step

Numbers match SKILL.md. Read only the step you are on.

## Rules that never change
- The gh_* tools are the only way to touch GitHub. They enforce: no posting without the user's confirmation, no push to
  upstream, no force-push, no commit trailers, one issue per branch. A refusal is the answer. Report the exact text and stop.
  Do not try another route (shell git push, `gh` CLI, curl, a different token, editing the tool config).
- Work in the fork clone only. Never edit files outside it.
- You do not decide that a post is allowed. The user does.

## 1 Fork and clone
- gh_repo_info(owner/repo): default branch, license, whether issues and PRs are enabled, archived or not.
  If archived, issues disabled, or no license that allows contributions: stop and tell the user.
- gh_fork, then gh_clone. Do all work in that clone.

## 2 Read the rules of the project
- gh_get_file for CONTRIBUTING.md, .github/PULL_REQUEST_TEMPLATE.md, .github/ISSUE_TEMPLATE/*, CODE_OF_CONDUCT.md.
- Write down: how to run tests, how to lint/format, commit message style, branch naming, whether an issue must exist first,
  whether a CLA or sign-off (DCO) is required. If a sign-off is required, tell the user: it is a trailer and you do not add trailers.

## 3 Hunt ONE component
- List the top-level directories, pick one small component (one module or package). Do not skim the whole repo.
- Read its files fully, then call the review skills one at a time. Ask each for findings with file:line and a reason.
- A finding is a candidate only if you can name file:line and describe a concrete input that goes wrong.
- Not candidates: style opinions, missing features, speculative performance, anything the docs say is intended.

## 4 Duplicate check
- gh_search for issues and for PRs, with state open AND closed (search each separately). At least 4 variants, for example:
  1. the exact error message or wrong output
  2. the function or class name
  3. the symptom in plain words
  4. the file path or module name
  5. (optional) the likely fix wording: "handle empty", "off by one", "null"
- A closed issue marked fixed or a merged PR that touches the same lines means "already fixed": check the default branch before you drop it.
- Record queries and hit counts in the report. "No results" is a result.

## 5 Report
- reports/<repo>_issues.md in the working folder (not inside the clone). Template: issues-file-template.md.

## 6 Vote gate (all three must be yes)
- Reproduce: run the code, a test or a script and see the wrong result yourself. Save the command and the output.
- Duplicate and fixed: nothing open or merged covers it, and the default branch still has the bug (read the file with gh_get_file).
- Maintainer validity: the docs, tests and CONTRIBUTING agree that this is a bug. If maintainers could reasonably say "works as designed", drop it.

## 7 Post
- Show the report. Wait for the user's explicit yes for each issue.
- Post only if the project accepts issues from anyone and needs no invitation, label or permission.
- Use the repo's issue template. Title: short and specific. Body: steps to reproduce, expected, actual, version or commit, file:line.

## 8 to 11 Branch, fix, conventions, tests
- Branch from the upstream default branch (not from your fork's stale copy): update first, then branch fix/<issue-number>-<short-slug>.
- Change only what the issue needs. No drive-by cleanups, no renames, no formatting of untouched lines.
- Commit message: one line, imperative mood, under 72 characters, match the style of `git log --oneline`. No body trailers,
  no Co-Authored-By, no Signed-off-by unless the user says so, no mention of AI or tools.
- Run the project's own lint and formatter on changed files and fix what they report. Match nearby code.
- Add a test that fails before the fix and passes after. Run the whole test file or suite the project documents.
  Paste the real output in your report to the user. If tests cannot run here, say so.

## 12 Competing PRs
- gh_search PRs (open and closed) with the issue number and with key words. gh_list_pr_files on any hit that looks related.
- An open PR that fixes the same issue means stop. Tell the user, do not open a second one.

## 13 Pull request
- gh_push your branch to the fork. gh_pr_create from the fork branch to the upstream default branch.
- Body = the repo's PR template, filled in. Include `Fixes #N`. A checklist box is ticked only if you did that thing. Leave the rest unticked.
- Title: same style as the commit line.

## 14 Review comments
- gh_pr_threads lists open threads. For each one: make the change as a NEW commit (one line message, no trailers).
- Never amend, rebase, squash or force-push. History stays append-only; the maintainer squashes.
- gh_pr_reply with the short commit SHA ("Fixed in abc1234"), then gh_pr_resolve_thread. Reply first, resolve second.
- If you disagree with a comment, say why in the reply and do not resolve it. Ask the user.
