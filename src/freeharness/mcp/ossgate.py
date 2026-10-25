"""
The GitHub gate as run by `freeharness oss run`: exactly `freeharness.mcp.ghgate`, plus ONE dry-run affordance.

In a dry run nothing is posted, so the issue the agent drafted never gets a number, and the pull-request gates
(which look the issue up and fail closed when they cannot) could never pass: a dry run could never produce a
validated PR draft. Here `gh_issue_create` in dry-run mode hands out a placeholder number (900001, 900002, ...)
and records it in `<FH_GH_STATE>/virtual_issues.json`; `gh_pr_create` accepts such a number in dry-run mode only,
skipping the checks that need a real issue (state, assignees, search by number, timeline) but still running the
open-PR file-overlap scan and every other gate. A placeholder can never satisfy a live run: live mode never
reads the file, and a placeholder number does not exist on GitHub.

Everything else is inherited unchanged from ghgate.Gate (draft review, markers, evidence, push rules, ...).
"""
from __future__ import annotations

import re
import sys

from . import ghgate
from .ghgate import Gate

VIRTUAL_BASE = 900000


class OssGate(Gate):
    def _virtual(self) -> dict:
        return self._load("virtual_issues.json")

    def gh_issue_create(self, repo: str, title: str, body: str) -> str:
        out = super().gh_issue_create(repo, title, body)
        if self.cfg.dryrun and out.startswith("DRY-RUN"):
            m = re.search(r"\(draft ([0-9A-Za-z-]+)\)", out)
            v = self._virtual()
            number = VIRTUAL_BASE + len(v) + 1
            v[str(number)] = {"repo": repo, "title": title, "draft": m.group(1) if m else ""}
            self._store("virtual_issues.json", v)
            out += (f"\n\nDRY-RUN issue number for this run: #{number} (a placeholder: nothing exists on GitHub). "
                    f"Use it as issue={number} and `Fixes #{number}` in gh_pr_create, and in the branch name.")
        return out

    def _competing_check(self, repo: str, issue: int, files: set[str]):
        if not (self.cfg.dryrun and str(issue) in self._virtual()):
            return super()._competing_check(repo, issue, files)
        me = self.me().lower()
        blockers: list[str] = []
        notes = [f"dry-run: #{issue} is a placeholder, issue/timeline/search-by-number checks skipped"]
        opens = self.api.paged(f"/repos/{repo}/pulls", {"state": "open", "sort": "updated", "direction": "desc"}, max_pages=2)
        others = [p for p in opens if str((p.get("user") or {}).get("login", "")).lower() != me]
        for p in others[:40]:
            theirs = {f.get("filename") for f in self.api.paged(f"/repos/{repo}/pulls/{p.get('number')}/files", max_pages=1)}
            both = sorted(files & theirs)
            if both:
                blockers.append(f"open PR #{p.get('number')} by @{(p.get('user') or {}).get('login')} touches the same files: "
                                + ", ".join(both[:5]))
        if len(others) > 40:
            notes.append(f"only the 40 most recently updated of {len(others)} other open PRs were compared by file")
        return blockers, notes


def main(argv=None) -> int:
    ghgate.Gate = OssGate            # ghgate.main() builds `Gate(...)` from its module namespace
    return ghgate.main(argv)


if __name__ == "__main__":
    sys.exit(main())
