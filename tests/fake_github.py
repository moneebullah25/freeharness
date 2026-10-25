"""
A fake GitHub REST + GraphQL server for offline tests (free port, never 8766, no real network).
State is plain dicts the tests edit; every write is recorded in `.writes`.
"""
import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse


class FakeGitHub:
    def __init__(self, me="me"):
        self.me = me
        self.repos: dict = {}            # full_name -> repo json (+ "_files": {path: text})
        self.items: dict = {}            # repo -> [ {number, kind issue|pr, title, body, state, user, merged, assignees} ]
        self.pr_files: dict = {}         # (repo, n) -> [filenames]
        self.pr_commits: dict = {}       # (repo, n) -> [shas]
        self.timeline: dict = {}         # (repo, n) -> [events]
        self.branches: dict = {}         # (repo, branch) -> sha
        self.compare: dict = {}          # repo -> [filenames]
        self.threads: dict = {}          # (repo, n) -> {"author": login, "threads": [...]}
        self.writes: list = []
        self.requests: list = []         # (method, path)
        self.auth: set = set()
        self.fail: dict = {}             # path substring -> (status, body dict)
        self.searches: list = []
        self.srv = None

    # -- setup helpers ------------------------------------------------------------------------------

    def add_repo(self, full, default_branch="main", has_issues=True, archived=False, policy=None, files=None,
                 fork_of=None, owner=None):
        self.repos[full] = {"full_name": full, "default_branch": default_branch, "has_issues": has_issues,
                            "archived": archived, "disabled": False, "fork": bool(fork_of),
                            "owner": {"login": owner or full.split("/")[0]},
                            "_files": dict(files or {}), "_policy": policy,
                            **({"parent": {"full_name": fork_of}} if fork_of else {})}
        self.items.setdefault(full, [])

    def add_item(self, repo, number, kind="issue", title="", body="", state="open", user="someone", merged=False, assignees=()):
        self.items[repo].append({"number": number, "kind": kind, "title": title, "body": body, "state": state,
                                 "user": user, "merged": merged, "assignees": list(assignees)})

    @property
    def url(self):
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                b = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _go(self, method):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                fake.requests.append((method, u.path))
                fake.auth.add(self.headers.get("Authorization", ""))
                for sub, (status, obj) in fake.fail.items():
                    if sub in u.path:
                        return self._send(status, obj)
                try:
                    status, obj = fake.route(method, unquote(u.path), q, body)
                except KeyError:
                    status, obj = 404, {"message": "Not Found"}
                self._send(status, obj)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        assert self.srv.server_address[1] != 8766
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    # -- routing ------------------------------------------------------------------------------------

    @staticmethod
    def _page(lst, q):
        per, page = int(q.get("per_page", 30)), int(q.get("page", 1))
        return lst[(page - 1) * per: page * per]

    def _issue_json(self, repo, it):
        d = {"number": it["number"], "title": it["title"], "body": it["body"], "state": it["state"],
             "user": {"login": it["user"]}, "assignees": [{"login": a} for a in it["assignees"]]}
        if it["kind"] == "pr":
            d["pull_request"] = {"merged_at": "2026-01-01T00:00:00Z" if it["merged"] else None}
        return d

    def route(self, method, path, q, body):
        m = re.match(r"^/repos/([^/]+/[^/]+)(/.*)?$", path)
        if method == "GET" and path == "/user":
            return 200, {"login": self.me}
        if method == "GET" and path == "/search/issues":
            return self._search(q)
        if method == "POST" and path == "/graphql":
            return self._graphql(body)
        if not m:
            raise KeyError
        repo, rest = m.group(1), m.group(2) or ""
        if method == "GET" and rest == "":
            r = self.repos[repo]
            out = {k: v for k, v in r.items() if not k.startswith("_")}
            if r["_policy"]:
                out["pull_request_creation_policy"] = r["_policy"]
            return 200, out
        if method == "GET" and rest.startswith("/contents"):
            return self._contents(repo, rest[len("/contents"):].strip("/"))
        if method == "GET" and (mm := re.match(r"^/issues/(\d+)$", rest)):
            it = next(i for i in self.items[repo] if i["number"] == int(mm.group(1)))
            return 200, self._issue_json(repo, it)
        if method == "GET" and (mm := re.match(r"^/issues/(\d+)/timeline$", rest)):
            return 200, self._page(self.timeline.get((repo, int(mm.group(1))), []), q)
        if method == "GET" and rest == "/pulls":
            prs = [i for i in self.items[repo] if i["kind"] == "pr" and i["state"] == q.get("state", "open")]
            return 200, self._page([{"number": p["number"], "user": {"login": p["user"]}} for p in prs], q)
        if method == "GET" and (mm := re.match(r"^/pulls/(\d+)/files$", rest)):
            return 200, self._page([{"filename": f, "status": "modified", "additions": 1, "deletions": 0}
                                    for f in self.pr_files[(repo, int(mm.group(1)))]], q)
        if method == "GET" and (mm := re.match(r"^/pulls/(\d+)/commits$", rest)):
            return 200, self._page([{"sha": s} for s in self.pr_commits.get((repo, int(mm.group(1))), [])], q)
        if method == "GET" and (mm := re.match(r"^/branches/(.+)$", rest)):
            return 200, {"commit": {"sha": self.branches[(repo, mm.group(1))]}}
        if method == "GET" and rest.startswith("/compare/"):
            return 200, {"files": [{"filename": f} for f in self.compare[repo]]}
        if method == "POST" and rest == "/forks":
            self.writes.append((method, path, body))
            name = repo.split("/")[1]
            self.add_repo(f"{self.me}/{name}", fork_of=repo, files=self.repos[repo]["_files"])
            return 202, {"full_name": f"{self.me}/{name}"}
        if method == "POST" and rest in ("/issues", "/pulls"):
            self.writes.append((method, path, body))
            return 201, {"number": 77, "html_url": f"https://github.example/{repo}/{rest[1:]}/77"}
        raise KeyError

    def _contents(self, repo, sub):
        files = self.repos[repo]["_files"]
        if sub in files:
            return 200, {"type": "file", "name": sub.rsplit("/", 1)[-1], "encoding": "base64",
                         "content": base64.b64encode(files[sub].encode()).decode()}
        prefix = (sub + "/") if sub else ""
        names, dirs = {}, set()
        for p in files:
            if p.startswith(prefix):
                head = p[len(prefix):].split("/", 1)
                if len(head) == 1:
                    names[head[0]] = "file"
                else:
                    dirs.add(head[0])
        for d in dirs:
            names[d] = "dir"
        if not names and sub:
            raise KeyError
        return 200, [{"name": n, "type": t} for n, t in sorted(names.items())]

    def _search(self, q):
        query = q.get("q", "")
        self.searches.append(query)
        repo = re.search(r"repo:(\S+)", query).group(1)
        kind = "pr" if "is:pr" in query else "issue"
        rest = re.sub(r"(repo|is):\S+", " ", query).lower().split()
        hits = []
        for it in self.items.get(repo, []):
            text = (it["title"] + " " + it["body"]).lower()
            if it["kind"] == kind and all(t in text for t in rest):
                hits.append(it)
        return 200, {"total_count": len(hits), "items": [self._issue_json(repo, i) for i in hits][: int(q.get("per_page", 30))]}

    def _graphql(self, body):
        query, v = body["query"], body["variables"]
        if "reviewThreads" in query:
            repo = f"{v['o']}/{v['n']}"
            t = self.threads[(repo, v["num"])]
            nodes = [{"id": x["id"], "isResolved": x["isResolved"], "path": x.get("path"), "line": x.get("line"),
                      "comments": {"nodes": [{"databaseId": 1, "author": {"login": a}, "body": b} for a, b in x["comments"]]}}
                     for x in t["threads"]]
            return 200, {"data": {"repository": {"pullRequest": {"author": {"login": t["author"]},
                                                                  "reviewThreads": {"nodes": nodes}}}}}
        if "addPullRequestReviewThreadReply" in query:
            self.writes.append(("POST", "/graphql", body))
            for t in self.threads.values():
                for x in t["threads"]:
                    if x["id"] == v["t"]:
                        x["comments"].append((self.me, v["b"]))
            return 200, {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "C1"}}}}
        if "resolveReviewThread" in query:
            self.writes.append(("POST", "/graphql", body))
            for t in self.threads.values():
                for x in t["threads"]:
                    if x["id"] == v["t"]:
                        x["isResolved"] = True
            return 200, {"data": {"resolveReviewThread": {"thread": {"id": v["t"], "isResolved": True}}}}
        return 200, {"errors": [{"message": "unknown operation"}]}
