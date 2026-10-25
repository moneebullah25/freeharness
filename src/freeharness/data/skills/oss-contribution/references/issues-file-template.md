# reports/<repo>_issues.md: layout to copy

One `## Candidate` section per finding. Keep it short and factual.

```markdown
# <owner>/<repo>: candidate issues

Component reviewed: <path or package>
Upstream default branch: <name>   Commit reviewed: <short sha>
Contribution rules read: CONTRIBUTING yes/no, PR template yes/no, issue template yes/no

## Candidate 1: <short title>
- Where: <file>:<line>
- What is wrong: <one or two sentences>
- Reproduce: `<exact command>`
- Expected: <...>
- Actual: <...> (paste the real output)
- Duplicate search (issues + PRs, open + closed):
  | query | where | hits |
  |---|---|---|
  | "<exact error>" | issues open/closed | 0 |
  | "<function name>" | PRs open/closed | 0 |
  | "<symptom>" | issues open/closed | 1 (#123, unrelated: <why>) |
  | "<file path>" | PRs open/closed | 0 |
- Still present on default branch: yes (read <file> via gh_get_file)
- Vote: reproduced yes/no, duplicate-or-fixed no/yes, maintainer validity yes/no
- Decision: post / drop (reason)

## Dropped
- <title>: <reason>
```
