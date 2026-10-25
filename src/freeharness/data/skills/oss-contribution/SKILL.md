---
name: oss-contribution
description: Contribute to an open-source repo through the gh_* tools. Find ONE real bug, check duplicates, then open one issue and one small PR. Use for any OSS issue or PR task.
---
# OSS contribution pipeline

The gh_* tools (gh_repo_info, gh_search, gh_get_file, gh_list_pr_files, gh_pr_threads, gh_fork, gh_clone, gh_branch, gh_setup_env, gh_push, gh_issue_create, gh_pr_create, gh_pr_reply, gh_pr_resolve_thread) enforce the hard rules. If a tool refuses, STOP and report the refusal text to the user. Never work around it (no raw git push, gh CLI, curl, other tokens).
One repo, one component, one issue per branch. Details per step: references/steps.md (next to this file).

1. Fork and clone: gh_repo_info, gh_fork, gh_clone. Note the upstream default branch.
2. Read CONTRIBUTING, the PR template and issue templates (gh_get_file). Note the test and lint commands and the commit style.
3. Pick ONE component. Review it with the agency skills (agency-code-reviewer, agency-application-security-engineer, agency-backend-architect). A candidate is a bug you can point to by file and line.
4. Duplicate check with gh_search: issues AND PRs, open AND closed, at least 4 query variants (error text, function name, symptom, file path). Keep every hit.
5. Write reports/<repo>_issues.md, one section per candidate (template: references/issues-file-template.md).
6. Vote gate. Keep a candidate only if ALL are true: (a) you reproduced it by running code; (b) it is not a duplicate and not already fixed on the default branch; (c) maintainers would call it valid (not documented behaviour, not a feature request). Otherwise drop it.
7. Show the report to the user. Post with gh_issue_create ONLY after the user says yes, and only if no special permission is needed. If unsure, ask.
8. One issue = one branch: gh_branch(repo_dir, name) fetches upstream and creates it from the default branch (bash has no network). Then gh_setup_env(repo_dir) once: it returns a COMMAND PREFIX.
9. Smallest possible fix. Commit message: one line, imperative, no trailers, no Co-Authored-By, no tool or AI mention.
10. Conventions: run the project's own lint and format commands through the prefix (`cd <clone> && <prefix> ruff check .`). Imitate the neighbouring code (names, errors, comments).
11. Add or update a test. Run the tests through the prefix (`<prefix> pytest -q -p no:cacheprovider`). Report the real output. Plain `python`, `pytest`, `ruff` also exist in bash.
12. Competing PR check: gh_search PRs for the issue number and gh_list_pr_files for the same files. If someone already has a fix open, stop and tell the user.
13. gh_push, then gh_pr_create with the repo's PR template and `Fixes #N` in the body. Tick a checklist box only if it is true.
14. Review comments: gh_pr_threads. Fix each one in a NEW commit. Never amend, rebase or force-push. Then gh_pr_reply with the commit SHA, then gh_pr_resolve_thread.

Unclear step, missing permission, or a refusal: stop and ask the user.
