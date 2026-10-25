"""
A stdio MCP server that lets the agent do open-source contribution work on GitHub with the user's
pipeline rules enforced IN CODE, not by prompt. Tools: gh_repo_info, gh_search, gh_get_file,
gh_list_pr_files, gh_pr_threads, gh_fork, gh_clone, gh_branch, gh_setup_env, gh_push, gh_issue_create,
gh_pr_create, gh_pr_reply, gh_pr_resolve_thread. Run by DeepSeek Harness (overlay row written by
`freeharness github enable`); see docs/GITHUB_GATE.md.

The agent's sandbox has no network and no HOME, so git and GitHub access happens only here, in a
process outside that sandbox. Every gate below is a code path the model cannot talk its way past:

  * writes (fork, issue, PR, comment, push) need a human: the draft is shown through the client's
    confirmation socket (FH_CONFIRM_SOCKET + FH_CONFIRM_TOKEN); no socket, no TTY, timeout or
    anything but an explicit approval = refused. "request changes" returns the user's note to the model.
  * FH_GH_DRYRUN (default ON; only the exact value "0" is live) validates every gate and returns what
    would be sent, with no write to GitHub.
  * GITHUB_TOKEN is read from the process environment (or the file named by FH_GH_ENVFILE) and is never
    printed, logged, returned or written; every output passes through redact().

Nothing but protocol messages goes to stdout.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .. import depfiles, paths, security, toolchain

# --------------------------------------------------------------------------- patterns

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/@-]{1,200}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
MARKERS = re.compile(r"claude|anthropic|co[-_ ]?authored|generated\s+with|ponytail", re.I)
TOKEN_PAT = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})")
ARTIFACT_EXT = {".db", ".sqlite", ".sqlite3", ".bin", ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".o", ".a",
                ".class", ".jar", ".war", ".zip", ".tar", ".gz", ".tgz", ".whl", ".egg", ".pickle", ".pkl", ".ds_store"}
MAX_FILE_BYTES = 1_000_000
MAX_TEXT = 30_000
MAX_OUT = 7000
MAX_DIFF = 5_000_000
EXTRA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$")
ENV_TOOLS = ("pytest", "ruff", "flake8", "black", "isort", "mypy", "pre_commit", "coverage")
PASS_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
PTH_WRITER = ("import pathlib,sys,sysconfig;p=pathlib.Path(sysconfig.get_paths()['purelib']);p.mkdir(parents=True,exist_ok=True);"
              "(p/'fh-toolchain.pth').write_text(sys.argv[1]+'\\n')")

CLA_RE = re.compile(r"\bCLA\b|contributor\s+licen[sc]e\s+agreement|copyright\s+assignment", re.I)
DCO_RE = re.compile(r"\bDCO\b|developer\s+certificate\s+of\s+origin|signed-off-by", re.I)
APPROVAL_RES = [re.compile(p, re.I) for p in (
    r"\b(ask|get|obtain|seek|request|need|require[sd]?)\b[^.\n]{0,40}\b(approval|permission|go-?ahead|green\s*light)\b",
    r"\b(wait|waiting)\s+(for|until)\b[^.\n]{0,60}\b(approv|assign|maintainer|triage|confirm|go-?ahead|label)",
    r"\b(assign(ed|ment)|claim)\b[^.\n]{0,60}\b(before|first|prior|required|must|need)",
    r"\bmust\s+be\s+(assigned|approved|triaged|accepted)\b",
    r"\b(only|unless)\b[^.\n]{0,60}\b(maintainers?|collaborators?|invited|approved|assigned)\b",
    r"\bcomment\b[^.\n]{0,40}\b(to\s+)?(claim|be assigned|take)\b",
    r"\b(do\s+not|don't|please\s+do\s+not|never)\b[^.\n]{0,40}\b(open|submit|send|file|create)\b[^.\n]{0,40}\b(without|unless|until|before)\b",
    r"\b(issues?|pull requests?|prs?|contributions?)\b[^.\n]{0,40}\b(by invitation|invite[- ]only|not accepted|not accepting|closed to)\b",
)]


def _normalize(text: str) -> str:
    """NFKC and drop invisible format characters so `cl​aude` cannot slip past the marker check."""
    t = unicodedata.normalize("NFKC", text or "")
    return "".join(c for c in t if unicodedata.category(c) != "Cf")


def find_markers(*texts: str) -> list[str]:
    hits = []
    for t in texts:
        for m in MARKERS.finditer(_normalize(t)):
            hits.append(m.group(0).lower())
    return sorted(set(hits))


# --------------------------------------------------------------------------- secrets never leave

_SECRETS: set[str] = set()


def register_secret(s: str) -> None:
    if s and len(s) >= 4:
        _SECRETS.add(s)


def redact(text) -> str:
    t = str(text)
    for s in sorted(_SECRETS, key=len, reverse=True):
        t = t.replace(s, "***")
    return TOKEN_PAT.sub("***", t)


class Refuse(Exception):
    """A gate said no. The message is shown to the model, so it says what to fix."""


class ChangesRequested(Exception):
    """The human asked for changes; the note goes back to the model as the tool result."""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(redact(f"HTTP {status}: {message}" if status else message))
        self.status = status


# --------------------------------------------------------------------------- configuration

def _read_envfile_token(path: str) -> str:
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?GITHUB_TOKEN\s*=\s*(.*?)\s*$", line)
        if m:
            v = m.group(1)
            if v[:1] in ("'", '"') and v.find(v[0], 1) > 0:
                return v[1:v.find(v[0], 1)]
            return v.split(" #")[0].strip()
    return ""


@dataclasses.dataclass
class Config:
    token: str = ""
    api: str = "https://api.github.com"
    dryrun: bool = True
    workdir: Path | None = None
    state: Path = dataclasses.field(default_factory=lambda: paths.state_dir() / "ghgate")
    evidence: Path | None = None
    evidence_explicit: bool = False
    confirm_sock: str = ""
    confirm_token: str = ""
    confirm_timeout: float = 240.0
    ssh_host: str = "github.com"
    git_url: str = ""                     # template with {host} and {repo}; tests point it at local bare repos
    git_name: str = ""
    git_email: str = ""
    pip_index: str = "https://pypi.org/simple"    # gh_setup_env; operator config (FH_GH_PIP_INDEX), never from the model
    pip_find_links: str = ""                      # a local wheelhouse instead of an index (FH_GH_PIP_FIND_LINKS; tests)
    env_timeout: float = 600.0
    run_id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex[:12])

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        token = env.get("GITHUB_TOKEN", "")
        if not token and env.get("FH_GH_ENVFILE"):
            token = _read_envfile_token(env["FH_GH_ENVFILE"])
        register_secret(token)
        state = Path(env["FH_GH_STATE"]).expanduser() if env.get("FH_GH_STATE") else paths.state_dir() / "ghgate"
        wd = env.get("FH_GH_WORKDIR") or env.get("FREEHARNESS_CWD")
        ev = env.get("FH_GH_EVIDENCE")
        try:
            timeout = float(env.get("FH_GH_CONFIRM_TIMEOUT", "240"))
        except ValueError:
            timeout = 240.0
        try:
            env_timeout = float(env.get("FH_GH_ENV_TIMEOUT", "600"))
        except ValueError:
            env_timeout = 600.0
        return cls(token=token, api=(env.get("FH_GH_API") or "https://api.github.com").rstrip("/"),
                   dryrun=env.get("FH_GH_DRYRUN", "1") != "0",
                   workdir=Path(wd).expanduser().resolve() if wd else None, state=state,
                   evidence=Path(ev).expanduser() if ev else state / "evidence.jsonl", evidence_explicit=bool(ev),
                   confirm_sock=env.get("FH_CONFIRM_SOCKET", ""), confirm_token=env.get("FH_CONFIRM_TOKEN", ""),
                   confirm_timeout=timeout, ssh_host=env.get("FH_GITHUB_SSH_HOST") or "github.com",
                   git_url=env.get("FH_GH_GIT_URL", ""), git_name=env.get("FH_GH_GIT_NAME", ""),
                   git_email=env.get("FH_GH_GIT_EMAIL", ""),
                   pip_index=env.get("FH_GH_PIP_INDEX") or "https://pypi.org/simple",
                   pip_find_links=env.get("FH_GH_PIP_FIND_LINKS", ""), env_timeout=env_timeout)


# --------------------------------------------------------------------------- GitHub REST

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):          # never forward the token to another place
        return None


class Api:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._opener = urllib.request.build_opener(_NoRedirect)

    def call(self, method: str, path: str, body=None, params=None):
        if not self.cfg.token:
            raise ApiError(0, "GITHUB_TOKEN is not set in the environment of this server")
        url = self.cfg.api + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={  # noqa: S310
            "Authorization": f"Bearer {self.cfg.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "freeharness-ghgate",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with self._opener.open(req, timeout=20) as r:  # noqa: S310  (base URL is operator config, http(s) only)
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(0, f"network error: {type(e).__name__}") from None
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        if status >= 400 or 300 <= status < 400:
            msg = parsed.get("message") if isinstance(parsed, dict) else None
            raise ApiError(status, (msg or raw[:200].decode("utf-8", "replace") or "error"))
        return parsed

    def get(self, path, params=None):
        return self.call("GET", path, params=params)

    def get_optional(self, path, params=None):
        try:
            return self.call("GET", path, params=params)
        except ApiError as e:
            if e.status == 404:
                return None
            raise

    def paged(self, path, params=None, max_pages=3, per_page=100):
        out = []
        for page in range(1, max_pages + 1):
            chunk = self.get(path, {**(params or {}), "per_page": per_page, "page": page})
            if not isinstance(chunk, list):
                raise ApiError(0, f"unexpected response from {path}")
            out += chunk
            if len(chunk) < per_page:
                break
        return out

    def graphql(self, query: str, variables: dict):
        d = self.call("POST", "/graphql", {"query": query, "variables": variables})
        if not isinstance(d, dict) or d.get("errors") or "data" not in d:
            msg = (d or {}).get("errors", [{}])[0].get("message", "graphql error") if isinstance(d, dict) else "graphql error"
            raise ApiError(0, str(msg))
        return d["data"]


# --------------------------------------------------------------------------- helpers

def _int(v, name: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Refuse(f"{name} must be a positive integer") from None
    if n < 1:
        raise Refuse(f"{name} must be a positive integer")
    return n


def _text(v, name: str, required=True) -> str:
    if not isinstance(v, str):
        raise Refuse(f"{name} must be text")
    if required and not v.strip():
        raise Refuse(f"{name} is empty")
    if len(v) > MAX_TEXT:
        raise Refuse(f"{name} is longer than {MAX_TEXT} characters")
    return v


def _clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "..."


def artifact_looking(name: str) -> str | None:
    p = name.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    low = base.lower()
    if low == ".env" or low.startswith(".env."):
        return "environment file"
    if low == ".ds_store":
        return "OS metadata file"
    if "__pycache__/" in p or p.startswith("__pycache__/"):
        return "python cache"
    if any(seg in ("node_modules", ".venv", "venv") for seg in p.split("/")[:-1]):
        return "dependency directory"
    ext = os.path.splitext(low)[1]
    if ext in ARTIFACT_EXT:
        return f"build artifact or binary ({ext})"
    return None


@dataclasses.dataclass
class Draft:
    action: str
    repo: str
    text: str             # exactly what the human sees (and what is sent)


# --------------------------------------------------------------------------- the gate

class Gate:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = Api(cfg)
        self._me: str | None = None

    # -- identity, repo --------------------------------------------------------------------------

    def me(self) -> str:
        if self._me is None:
            d = self.api.get("/user")
            login = d.get("login") if isinstance(d, dict) else None
            if not login:
                raise ApiError(0, "could not determine the authenticated user")
            self._me = str(login)
        return self._me

    @staticmethod
    def _repo(repo) -> str:
        repo = str(repo).strip()
        if not REPO_RE.match(repo) or any(set(p) == {"."} for p in repo.split("/")):
            raise Refuse("repo must look like owner/name")
        return repo

    def _info(self, repo: str) -> dict:
        d = self.api.get(f"/repos/{repo}")
        if not isinstance(d, dict) or not d.get("default_branch"):
            raise ApiError(0, "repo response has no default branch")
        return {"full_name": d.get("full_name") or repo, "has_issues": bool(d.get("has_issues")),
                "default_branch": str(d["default_branch"]), "archived": bool(d.get("archived")),
                "disabled": bool(d.get("disabled")), "fork": bool(d.get("fork")),
                "policy": d.get("pull_request_creation_policy")}

    def _community(self, repo: str) -> dict:
        """CONTRIBUTING / templates and what CONTRIBUTING says. Raises on API errors (callers fail closed)."""
        found = {"contributing": None, "pr_templates": [], "issue_templates": []}
        for d in ("", ".github", "docs"):
            listing = self.api.get_optional(f"/repos/{repo}/contents" + (f"/{d}" if d else ""))
            if not isinstance(listing, list):
                continue
            for it in listing:
                name, typ = str(it.get("name", "")), it.get("type")
                path = f"{d}/{name}".lstrip("/")
                low = name.lower()
                if typ == "file" and re.match(r"^contributing(\.[a-z]+)?$", low) and not found["contributing"]:
                    found["contributing"] = path
                elif re.match(r"^pull_request_template(\.[a-z]+)?$", low):
                    found["pr_templates"].append(path)
                elif low == "pull_request_template" and typ == "dir":
                    sub = self.api.get_optional(f"/repos/{repo}/contents/{path}")
                    found["pr_templates"] += [f"{path}/{x['name']}" for x in sub or [] if x.get("type") == "file"]
                elif low == "issue_template" and typ == "dir":
                    sub = self.api.get_optional(f"/repos/{repo}/contents/{path}")
                    found["issue_templates"] += [f"{path}/{x['name']}" for x in sub or [] if x.get("type") == "file"]
                elif low.startswith("issue_template") and typ == "file":
                    found["issue_templates"].append(path)
        text = ""
        if found["contributing"]:
            f = self.api.get(f"/repos/{repo}/contents/{urllib.parse.quote(found['contributing'])}")
            if isinstance(f, dict) and f.get("encoding") == "base64":
                text = base64.b64decode(f.get("content", "")).decode("utf-8", "replace")[:200_000]
        approval = []
        for rx in APPROVAL_RES:
            m = rx.search(text)
            if m:
                approval.append(_clip(" ".join(text[max(0, m.start() - 20): m.end() + 40].split()), 120))
        found.update(text=text, cla=bool(CLA_RE.search(text)), dco=bool(DCO_RE.search(text)),
                     approval=approval[:3])
        return found

    # -- logs, evidence, drafts, state -------------------------------------------------------------

    def _append(self, path: Path, rec: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (redact(json.dumps(rec, ensure_ascii=False)) + "\n").encode())
        finally:
            os.close(fd)

    def _actions_path(self) -> Path:
        ev = self.cfg.evidence
        return ev.with_name(ev.name + ".actions.jsonl") if self.cfg.evidence_explicit else self.cfg.state / "actions.jsonl"

    def _log(self, event: str, **kw) -> None:
        self._append(self._actions_path(), {"time": time.time(), "run": self.cfg.run_id, "event": event, **kw})

    def _evidence(self, repo: str) -> list[dict]:
        out = []
        try:
            lines = self.cfg.evidence.read_text(encoding="utf-8").splitlines()
        except OSError:
            return out
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("event") != "search" or str(r.get("repo", "")).lower() != repo.lower():
                continue
            if not self.cfg.evidence_explicit and r.get("run") != self.cfg.run_id:
                continue
            out.append(r)
        return out

    def _evidence_summary(self, repo: str) -> str:
        ev = self._evidence(repo)
        if not ev:
            return "searches run: none"
        qs = [f"{r['kind']}: {r['query']} ({r['count']})" for r in ev]
        return f"searches run ({len(ev)}): " + " | ".join(qs)

    def _load(self, name: str) -> dict:
        try:
            d = json.loads((self.cfg.state / name).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _store(self, name: str, data: dict) -> None:
        paths.private_dir(self.cfg.state)
        fd, tmp = tempfile.mkstemp(dir=str(self.cfg.state), prefix=name + ".")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(redact(json.dumps(data, indent=1)))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.cfg.state / name)

    def _save_draft(self, d: Draft) -> str:
        did = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        p = paths.private_dir(self.cfg.state / "drafts") / f"{did}.md"
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(redact(f"# {d.action} on {d.repo}\n\nmode: {'dry-run' if self.cfg.dryrun else 'live'}\n\n{d.text}\n"))
        return did

    # -- the human ---------------------------------------------------------------------------------

    def _review(self, d: Draft, draft_id: str) -> None:
        """Ask the person. Returns only on an explicit approval; otherwise raises Refuse / ChangesRequested."""
        c = self.cfg
        if not c.confirm_sock or not c.confirm_token:
            raise Refuse("no human confirmation channel (non-interactive run or no terminal): nothing was sent. "
                         f"The draft is saved as {draft_id}.")
        s = socket.socket(socket.AF_UNIX)
        try:
            s.settimeout(c.confirm_timeout)
            s.connect(c.confirm_sock)
            s.sendall((json.dumps({"type": "confirm", "token": c.confirm_token, "action": d.action, "repo": d.repo,
                                   "text": d.text, "draft": draft_id}) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            reply = json.loads(buf.split(b"\n", 1)[0])
        except (OSError, ValueError):
            raise Refuse("the confirmation request failed or timed out: nothing was sent") from None
        finally:
            s.close()
        outcome = reply.get("outcome") if isinstance(reply, dict) else None
        if outcome == "confirmed":
            return
        if outcome == "changes":
            note = _clip(str(reply.get("note") or "").strip(), 1500) or "(no note given)"
            self._log("changes_requested", action=d.action, repo=d.repo, note=note)
            raise ChangesRequested(f"The user reviewed the draft and requested changes. Nothing was sent. "
                                   f"Their note: {note}\nRevise and call {d.action} again.")
        self._log("denied", action=d.action, repo=d.repo)
        raise Refuse("the user did not approve: nothing was sent")

    def _write(self, d: Draft, do) -> str:
        """Save the draft; dry-run returns it, live asks the human and then performs `do()`."""
        did = self._save_draft(d)
        if self.cfg.dryrun:
            self._log("dryrun", action=d.action, repo=d.repo, draft=did)
            return (f"DRY-RUN: all gates passed, nothing was sent (draft {did}). Would send:\n{d.text}")
        try:
            self._review(d, did)
        except ChangesRequested as e:
            return str(e)
        result = do()
        self._log("write", action=d.action, repo=d.repo, draft=did)
        return result

    # -- git --------------------------------------------------------------------------------------

    def _git_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "GITHUB_TOKEN"}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_SSH_COMMAND"] = os.environ.get("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes"   # env beats a repo's core.sshCommand
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        return env

    def _git(self, repo_dir, *args, timeout=120, check=True, raw=False):
        cmd = ["git"] + (["-C", str(repo_dir)] if repo_dir else []) + [
            "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "credential.helper=",
            "-c", "protocol.ext.allow=never", "-c", "log.showSignature=false", "-c", "core.pager=cat", *args]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=self._git_env())
        except (OSError, subprocess.SubprocessError) as e:
            raise Refuse(f"git failed to run: {type(e).__name__}") from None
        out = p.stdout.decode("utf-8", "replace")
        if check and p.returncode != 0:
            raise Refuse("git " + args[0] + " failed: " + redact(_clip((p.stderr.decode("utf-8", "replace") or out).strip(), 400)))
        return (p.returncode, out, p.stderr.decode("utf-8", "replace")) if raw else out

    def _url(self, slug: str) -> str:
        tpl = self.cfg.git_url or "git@{host}:{repo}.git"
        return tpl.format(host=self.cfg.ssh_host, repo=slug)

    def _in_workdir(self, p) -> Path:
        wd = self.cfg.workdir
        if wd is None:
            raise Refuse("FH_GH_WORKDIR is not set: no folder is allowed for clones")
        path = Path(p).expanduser()
        path = (path if path.is_absolute() else wd / path).resolve()
        if path == wd or not path.is_relative_to(wd):
            raise Refuse(f"{p} is not inside the working folder {wd}")
        return path

    def _registered(self, repo_dir) -> tuple[Path, dict]:
        rd = self._in_workdir(repo_dir)
        reg = self._load("registry.json").get(str(rd))
        if not reg:
            raise Refuse("this folder was not created by gh_clone; clone with gh_clone first")
        gd = rd / ".git"
        if gd.is_symlink() or not gd.is_dir():
            raise Refuse(".git must be a real directory inside the clone")
        cfg = self._git(rd, "config", "--local", "--get-regexp", r"^(url\..*\.(pushinsteadof|insteadof)|include)", check=False)
        if cfg.strip():
            raise Refuse("the clone's git config rewrites URLs or includes other config; refusing to push from it")
        return rd, reg

    # ======================================================================== read-only tools

    def gh_repo_info(self, repo: str) -> str:
        repo = self._repo(repo)
        i, com = self._info(repo), self._community(repo)
        policy = i["policy"] if i["policy"] else "unset(all)"
        lines = [f"{i['full_name']} default={i['default_branch']} has_issues={i['has_issues']} archived={i['archived']} "
                 f"fork={i['fork']} pull_request_creation_policy={policy}",
                 f"CONTRIBUTING: {com['contributing'] or 'none'}; PR templates: {', '.join(com['pr_templates']) or 'none'}; "
                 f"issue templates: {', '.join(com['issue_templates']) or 'none'}",
                 f"mentions: CLA={com['cla']} assignment_or_approval={bool(com['approval'])} DCO={com['dco']}"]
        lines += [f"  approval hint: \"{h}\"" for h in com["approval"]]
        if com["text"]:
            lines += ["--- CONTRIBUTING (first 4000 chars) ---", com["text"][:4000]]
        return "\n".join(lines)

    def gh_search(self, repo: str = "", query: str = "", kind: str = "issues") -> str:
        if not repo:                      # GitHub's own syntax puts it in the query: "repo:owner/name words"
            m = re.search(r"(?i)(?:^|\s)repo:([\w.-]+/[\w.-]+)", _text(query, "query"))
            if not m:
                raise Refuse("repo is required (owner/name), e.g. repo=\"piccolo-orm/piccolo\"")
            repo = m.group(1)
        repo = self._repo(repo)
        if kind not in ("issues", "prs"):
            raise Refuse("kind must be issues or prs")
        clean = re.sub(r"(?i)(?:^|\s)-?(?:repo|org|user|is|state|type|archived|no):\S*", " ", _text(query, "query"))
        clean = " ".join(clean.split())[:200]
        if not clean:
            raise Refuse("query has no search words")
        q = f"repo:{repo} is:{'issue' if kind == 'issues' else 'pr'} {clean}"
        d = self.api.get("/search/issues", {"q": q, "per_page": 10, "sort": "updated", "order": "desc"})
        if not isinstance(d, dict) or "items" not in d:
            raise ApiError(0, "unexpected search response")
        total = int(d.get("total_count", len(d["items"])))
        self._append(self.cfg.evidence, {"event": "search", "run": self.cfg.run_id, "query": clean, "kind": kind,
                                         "repo": repo, "time": time.time(), "count": total})
        rows = []
        for it in d["items"][:10]:
            st = it.get("state", "?")
            if (it.get("pull_request") or {}).get("merged_at"):
                st = "merged"
            rows.append(f"#{it.get('number')} [{'pr' if it.get('pull_request') else 'issue'} {st}] "
                        f"{_clip(it.get('title', ''), 100)} (@{(it.get('user') or {}).get('login', '?')})")
        return f"{total} result(s) for {kind} {clean!r} (open and closed)\n" + "\n".join(rows)

    def gh_get_file(self, repo: str, path: str, ref: str = "") -> str:
        repo = self._repo(repo)
        path = _text(path, "path", required=False).strip("/")
        if ".." in path.split("/"):
            raise Refuse("path must not contain ..")
        params = {}
        if ref:
            if not REF_RE.match(ref):
                raise Refuse("bad ref")
            params["ref"] = ref
        d = self.api.get(f"/repos/{repo}/contents/{urllib.parse.quote(path)}", params)
        if isinstance(d, list):
            return "directory:\n" + "\n".join(f"{x.get('type', '?')[:1]} {x.get('name')}" for x in d[:100])
        if not isinstance(d, dict) or d.get("encoding") != "base64":
            raise ApiError(0, "file content unavailable (too large or not a file)")
        txt = base64.b64decode(d.get("content", "")).decode("utf-8", "replace")
        return txt if len(txt) <= 6000 else txt[:6000] + f"\n[... truncated, {len(txt) - 6000} more chars]"

    def gh_list_pr_files(self, repo: str, number: int) -> str:
        repo, n = self._repo(repo), _int(number, "number")
        files = self.api.paged(f"/repos/{repo}/pulls/{n}/files", max_pages=3)
        rows = [f"{f.get('status', '?')[:1]} {f.get('filename')} +{f.get('additions', 0)}/-{f.get('deletions', 0)}"
                for f in files]
        return f"{len(files)} file(s) in PR #{n}\n" + "\n".join(rows[:300])

    _THREADS_Q = ("query($o:String!,$n:String!,$num:Int!){repository(owner:$o,name:$n){pullRequest(number:$num){"
                  "author{login} reviewThreads(first:50){nodes{id isResolved path line comments(first:20){nodes{"
                  "databaseId author{login} body}}}}}}}")

    def _threads(self, repo: str, n: int) -> tuple[str, list]:
        o, name = repo.split("/")
        d = self.api.graphql(self._THREADS_Q, {"o": o, "n": name, "num": n})
        pr = ((d.get("repository") or {}).get("pullRequest")) or None
        if not pr:
            raise ApiError(0, f"PR #{n} not found")
        return (pr.get("author") or {}).get("login", ""), (pr.get("reviewThreads") or {}).get("nodes") or []

    def gh_pr_threads(self, repo: str, number: int) -> str:
        repo, n = self._repo(repo), _int(number, "number")
        _, threads = self._threads(repo, n)
        out = []
        for t in threads:
            cs = (t.get("comments") or {}).get("nodes") or []
            out.append(f"thread {t.get('id')} {'resolved' if t.get('isResolved') else 'OPEN'} {t.get('path')}:{t.get('line')}")
            out += [f"  @{(c.get('author') or {}).get('login', '?')}: {_clip(c.get('body', ''), 400)}" for c in cs[:20]]
        return f"{len(threads)} review thread(s) on PR #{n}\n" + "\n".join(out)

    # ======================================================================== fork, clone, push

    def _existing_fork(self, repo: str) -> str | None:
        d = self.api.get_optional(f"/repos/{self.me()}/{repo.split('/')[1]}")
        if isinstance(d, dict) and d.get("fork") and str((d.get("parent") or {}).get("full_name", "")).lower() == repo.lower():
            return d.get("full_name")
        return None

    def gh_fork(self, repo: str) -> str:
        repo = self._repo(repo)
        i = self._info(repo)
        if i["archived"] or i["disabled"]:
            raise Refuse(f"{repo} is archived or disabled")
        have = self._existing_fork(repo)
        if have:
            return f"already forked: {have} (nothing sent)"
        d = Draft("gh_fork", repo, f"FORK {repo} into the account {self.me()}")

        def do():
            r = self.api.call("POST", f"/repos/{repo}/forks", {})
            full = (r or {}).get("full_name") or f"{self.me()}/{repo.split('/')[1]}"
            for _ in range(20):
                if self.api.get_optional(f"/repos/{full}"):
                    break
                time.sleep(1)
            return f"forked: {full}"
        return self._write(d, do)

    def gh_clone(self, repo: str, dest: str) -> str:
        repo = self._repo(repo)
        i = self._info(repo)
        dest_p = self._in_workdir(_text(dest, "dest"))
        if dest_p.exists() and any(dest_p.iterdir()):
            raise Refuse("dest exists and is not empty")
        fork = self._existing_fork(repo)
        if not fork and not self.cfg.dryrun:
            raise Refuse("you have no fork of this repo yet: call gh_fork first")
        origin = fork or repo
        db = i["default_branch"]
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        self._git(None, "clone", "--origin", "origin", "--", self._url(origin), str(dest_p), timeout=300)
        self._git(dest_p, "remote", "add", "upstream", self._url(repo))
        self._git(dest_p, "config", "remote.upstream.fetch", f"+refs/heads/{db}:refs/remotes/upstream/{db}")
        self._git(dest_p, "config", "remote.upstream.pushurl", "DISABLED")       # nobody pushes to upstream by accident
        if not fork:
            self._git(dest_p, "config", "remote.origin.pushurl", "DISABLED")
        if self.cfg.git_name and self.cfg.git_email:                              # from the user's env, never invented
            self._git(dest_p, "config", "user.name", self.cfg.git_name)
            self._git(dest_p, "config", "user.email", self.cfg.git_email)
        self._git(dest_p, "fetch", "upstream", timeout=300)
        reg = self._load("registry.json")
        reg[str(dest_p)] = {"upstream": repo, "fork": fork, "default_branch": db, "time": time.time()}
        self._store("registry.json", reg)
        self._log("clone", repo=repo, dest=str(dest_p), fork=fork)
        return (f"cloned {origin} to {dest_p}; remotes: origin={origin}"
                f"{'' if fork else ' (READ-ONLY: no fork yet, dry-run)'}, upstream={repo}; default branch {db}.\n"
                f"Make a branch (never commit on {db}), commit normally, then gh_push.")

    # ======================================================================== branch, environment

    @staticmethod
    def _branch_name(raw, default: str) -> str:
        if not isinstance(raw, str) or not BRANCH_RE.match(raw):
            raise Refuse("invalid branch name: use letters, digits, . _ - and / (for example fix/123-empty-list)")
        segs = raw.split("/")
        if (".." in raw or "//" in raw or raw.endswith(("/", ".", ".lock")) or "@{" in raw
                or any(s.startswith(".") or s.endswith(".lock") for s in segs)
                or segs[0].lower() in ("refs", "heads", "remotes", "upstream", "origin")):
            raise Refuse("invalid branch name: no '..', '//', leading dots, '.lock', or names starting with refs/ heads/ remotes/ upstream/ origin/")
        if raw.lower() in (default.lower(), "head", "@"):
            raise Refuse(f"refusing to use the default branch name ({default}); pick a feature branch name such as fix/<issue>-<slug>")
        return raw

    def gh_branch(self, repo_dir: str, name: str) -> str:
        rd, reg = self._registered(repo_dir)
        db, up = reg["default_branch"], reg["upstream"]
        name = self._branch_name(name, db)
        if self._git(None, "check-ref-format", "--branch", name, check=False, raw=True)[0] != 0:
            raise Refuse("invalid branch name (git check-ref-format)")
        if self._git(rd, "config", "--local", "--get-regexp", r"^(filter\.|diff\..*\.(textconv|command))", check=False).strip():
            raise Refuse("the clone's git config defines filters or diff drivers (programs git would run on checkout); refusing")
        if self._git(rd, "rev-parse", "--verify", "-q", f"refs/heads/{name}", raw=True, check=False)[0] == 0:
            raise Refuse(f"a local branch named {name!r} already exists; choose another name")
        dirty = [ln for ln in self._git(rd, "status", "--porcelain", "--untracked-files=normal").splitlines() if ln.strip()]
        if dirty:
            raise Refuse(f"the working tree is not clean ({len(dirty)} change(s): {'; '.join(x.strip() for x in dirty[:5])}). "
                         "Commit them on their branch or discard them with git (restore / clean), then call gh_branch again.")
        # the same restricted fetch gh_push uses: URL from the registry (not the clone's config), one branch, no tags
        self._git(rd, "fetch", "--no-tags", "--no-recurse-submodules", self._url(up),
                  f"+refs/heads/{db}:refs/remotes/upstream/{db}", timeout=300)
        self._git(rd, "checkout", "--no-track", "-b", name, f"refs/remotes/upstream/{db}")
        sha = self._git(rd, "rev-parse", "HEAD").strip()
        self._log("branch", repo=up, repo_dir=str(rd), branch=name, base=f"upstream/{db}", sha=sha)
        return (f"on new branch {name} at {sha[:12]}, created from a fresh upstream/{db} ({sha}). Not pushed. "
                f"Next: gh_setup_env, make the fix, run tests and lint, commit, gh_push.")

    # -- gh_setup_env --------------------------------------------------------------------------------

    def _tree_reader(self, rd: Path, ref: str):
        def read(path: str) -> str | None:
            if not path or path.startswith(("/", "-")) or ".." in path.split("/"):
                return None
            size = self._git(rd, "cat-file", "-s", f"{ref}:{path}", check=False, raw=True)
            if size[0] != 0 or not size[1].strip().isdigit() or int(size[1]) > depfiles.MAX_FILE_BYTES:
                return None
            rc, out, _ = self._git(rd, "cat-file", "blob", f"{ref}:{path}", check=False, raw=True)
            return out if rc == 0 else None
        return read

    def _extras(self, extras) -> set[str]:
        if not isinstance(extras, str):
            raise Refuse("extras must be text: names separated by commas, for example 'test,lint'")
        names = [x for x in re.split(r"[,\s]+", extras.strip()) if x]
        if len(names) > 10 or any(not EXTRA_RE.match(x) for x in names):
            raise Refuse("extras: up to 10 names of letters, digits, . _ - (these select groups or requirements files the repo declares)")
        return {depfiles.norm(x) for x in names}

    def _run_capped(self, argv: list[str], timeout: float, env: dict, cap: int = 6000) -> tuple[int, str]:
        """Run with a hard timeout and bounded memory: keeps the first 1500 and the last `cap` bytes of output."""
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,  # noqa: S603
                                start_new_session=True)
        head, tail, total = bytearray(), bytearray(), [0]

        def pump():
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    return
                total[0] += len(chunk)
                room = 1500 - len(head)
                if room > 0:
                    head.extend(chunk[:room])
                    chunk = chunk[room:]
                tail.extend(chunk)
                if len(tail) > cap:
                    del tail[:len(tail) - cap]
        th = threading.Thread(target=pump, daemon=True)
        th.start()
        timed_out = False
        try:
            proc.wait(timeout=max(1.0, timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
        th.join(timeout=5)
        omitted = total[0] - len(head) - len(tail)
        text = head.decode("utf-8", "replace") + (f"\n[... {omitted} bytes of output omitted ...]\n" if omitted > 0 else "") \
            + tail.decode("utf-8", "replace")
        return (-9 if timed_out else proc.returncode), text

    def _net_argv(self, venv: Path, cmd: list[str], ro: list[str], hide: list[str]) -> list[str]:
        flags: list[str] = []
        for h in hide:
            flags += ["--hide", h]
        for r in ro:
            flags += ["--ro", r]
        for k in PASS_ENV:
            if os.environ.get(k):
                flags += ["--setenv", k, os.environ[k]]
        return [*security.runner_command(), "--net-install", str(venv), *flags, "--", *cmd]

    def _net_sandbox(self, venv: Path, cmd: list[str], deadline: float, ro: list[str], hide: list[str]) -> tuple[int, str]:
        """One command in the install sandbox: network yes; $HOME, /tmp, /run, other files and the environment no;
        `venv` is the only writable path. Only this tool builds this argv (see data/fh_sandbox_runner.py)."""
        left = deadline - time.monotonic()
        if left <= 1:
            return -9, "timed out"
        return self._run_capped(self._net_argv(venv, cmd, ro, hide), left,
                                {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "")})

    def _src_dirs(self, rd: Path) -> list[str]:
        src = rd / "src"
        if src.is_dir() and not src.is_symlink() and any(c.is_dir() and not c.name.startswith(".") for c in src.iterdir()):
            return [str(src), str(rd)]
        return [str(rd)]

    def _install_env(self, venv: Path, col, tc: Path | None, deadline: float) -> None:
        c = self.cfg
        if venv.exists():
            shutil.rmtree(venv)
        venv.mkdir(mode=0o755)
        py = str(tc / "bin" / "python") if tc else os.path.realpath(sys.executable)   # not the freeharness venv's own link
        ro: list[str] = []
        home = os.path.realpath(os.environ["HOME"]) if os.environ.get("HOME") else ""
        for base in {os.path.realpath(py), os.path.realpath(sys.base_prefix)}:
            if home and base.startswith(home + os.sep):          # a Python that lives under $HOME is hidden: bind its install
                ro.append(os.path.dirname(os.path.dirname(base)) if "/bin/" in base else base)
        if tc:
            ro.append(str(tc))
        pip = ["--isolated", "--no-input", "--no-cache-dir", "--disable-pip-version-check", "--progress-bar", "off",
               "--no-warn-script-location", "--prefer-binary"]
        if c.pip_find_links:
            pip += ["--no-index", "--find-links", c.pip_find_links]
            ro.append(c.pip_find_links)
        else:
            pip += ["--index-url", c.pip_index]
        steps = [("create the virtualenv", [py, "-m", "venv", str(venv)])]
        if tc:
            sp = toolchain._site_packages(tc)
            if sp:
                steps.append(("link the standard toolchain", [str(venv / "bin" / "python"), "-c", PTH_WRITER, str(sp)]))
        if col.reqs:
            fd = os.open(venv / ".fh-requirements.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(col.reqs) + "\n")
            steps.append(("pip install the declared dependencies",
                          [str(venv / "bin" / "python"), "-m", "pip", "install", *pip, "-r", str(venv / ".fh-requirements.txt")]))
        for what, cmd in steps:
            rc, out = self._net_sandbox(venv, cmd, deadline, ro, [str(c.workdir)])
            if rc != 0:
                shutil.rmtree(venv, ignore_errors=True)
                why = f"timed out after {c.env_timeout:.0f}s" if rc == -9 else f"exit {rc}"
                raise Refuse(f"could not {what} ({why}); nothing was left behind.\n{_clip(out.strip(), 1800)}")

    def gh_setup_env(self, repo_dir: str, extras: str = "") -> str:
        rd, reg = self._registered(repo_dir)
        if sys.platform != "linux" or not shutil.which("bwrap"):
            raise Refuse("installing dependencies needs the bubblewrap sandbox (bwrap, Linux) and it is not available here: "
                         "nothing was installed. Install bubblewrap, or run the project's tests by hand.")
        c = self.cfg
        wanted = self._extras(extras)
        up, db = reg["upstream"], reg["default_branch"]
        ref = f"refs/remotes/upstream/{db}"
        if self._git(rd, "rev-parse", "--verify", "-q", ref, raw=True, check=False)[0] != 0:
            raise Refuse(f"no upstream/{db} in the clone; gh_clone it again")
        top = [x for x in self._git(rd, "ls-tree", "--name-only", ref).split("\n") if x]
        sub = [x for x in self._git(rd, "ls-tree", "-r", "--name-only", ref, "--", "requirements/").split("\n") if x]
        # The install list comes from upstream's committed files, never from the working tree the agent can edit.
        col = depfiles.collect(top, sub, self._tree_reader(rd, ref), wanted)
        venv = c.workdir / f"{up.split('/')[1]}-venv"
        if venv.is_symlink() or (venv.exists() and not venv.is_dir()) or str(venv) in self._load("registry.json"):
            raise Refuse(f"{venv} exists and is not a virtualenv made by this tool; move or delete it")
        tc = toolchain.toolchain_dir()
        tstat = toolchain.status(tc)[0]
        inherit = tc if tstat != "missing" and toolchain._site_packages(tc) else None
        fp = hashlib.sha256(json.dumps([col.reqs, sys.version, str(inherit), tstat, c.pip_index, c.pip_find_links]).encode()).hexdigest()
        envs = self._load("envs.json")
        cached = envs.get(str(venv)) == fp and (venv / "bin" / "python").exists()
        if not cached:
            self._install_env(venv, col, inherit, time.monotonic() + c.env_timeout)
            envs[str(venv)] = fp
            self._store("envs.json", envs)
        py = str(venv / "bin" / "python")
        # does it work where the agent will use it? the same sandbox its bash tool runs in
        probe = (f"{shlex.quote(py)} -c 'import sys; print(\"python\", sys.version.split()[0])' && "
                 f"for m in {' '.join(ENV_TOOLS)}; do {shlex.quote(py)} -m $m --version >/dev/null 2>&1 && echo \"ok:$m\"; done")
        try:
            r = security._run_confined(probe, c.workdir, timeout=120)
            out, err = r.stdout, r.stderr
        except (OSError, subprocess.SubprocessError) as e:
            out, err = "", str(e)
        usable = [ln[3:].replace("_", "-") for ln in out.splitlines() if ln.startswith("ok:")]
        pp = ":".join(self._src_dirs(rd))
        prefix = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={pp} {py} -m"
        self._log("setup_env", repo=up, venv=str(venv), requirements=len(col.reqs), cached=cached, skipped=len(col.skipped),
                  toolchain=tstat)
        lines = [f"environment ready: {venv} ({'unchanged, reused' if cached else 'built'}; "
                 f"{len(col.reqs)} declared requirement(s) from: {', '.join(col.sources) or 'no declared files found'})"]
        if not out.startswith("python"):
            lines += ["WARNING: the venv's python does not start inside the bash sandbox: " + _clip(err.strip() or out.strip(), 300),
                      "(a Python installed under $HOME is hidden from the sandbox; list its install dir in FH_SANDBOX_TOOLS)"]
        else:
            lines.append(f"python in the sandbox: {py} ({out.splitlines()[0]}); `python` and `python3` also resolve there")
        lines.append("tools that run here: " + (", ".join(usable) or "none") +
                     ("" if usable else "  (none declared by the repo and no standard toolchain: python -m unittest still works)"))
        if tstat != "full":
            lines.append(f"standard toolchain is {tstat}: run `freeharness toolchain install` for pytest/ruff/flake8/black/mypy")
        lines += [f"COMMAND PREFIX (copy it into bash after `cd {rd} &&`):", f"  {prefix}",
                  "  then the tool, e.g.  <prefix> pytest -q -p no:cacheprovider   or   <prefix> ruff check .",
                  "The project itself is NOT installed (imports come from PYTHONPATH); tests that read installed metadata or "
                  "need a compiled extension may fail for that reason."]
        if col.skipped:
            lines.append(f"skipped {len(col.skipped)} unsafe or unsupported line(s): " + " | ".join(col.skipped[:6]))
        lines += [f"note: {n}" for n in col.notes[:4]]
        return "\n".join(lines)

    @staticmethod
    def _force_attempt(raw: str) -> bool:
        for tok in raw.split():
            if tok.startswith("+") or ":+" in tok:
                return True
            if re.match(r"^--force", tok) or re.match(r"^-[A-Za-z]*f[A-Za-z]*$", tok):
                return True
        return False

    def gh_push(self, repo_dir: str, branch: str) -> str:
        raw = str(branch)
        if self._force_attempt(raw):
            self._log("force_refused", repo_dir=str(repo_dir), branch=raw)
            raise Refuse("force pushes are never allowed (--force, -f, --force-with-lease, +refspec). "
                         "Make a new commit on top instead of amending or rewriting history.")
        rd, reg = self._registered(repo_dir)
        if not BRANCH_RE.match(raw) or ".." in raw or raw.endswith(("/", ".lock")) or "//" in raw:
            raise Refuse("invalid branch name")
        db, up = reg["default_branch"], reg["upstream"]
        if raw.lower() in (db.lower(), "head", "@"):
            raise Refuse(f"refusing to push the default branch ({db}); work on a feature branch")
        rc, sha, _ = self._git(rd, "rev-parse", "--verify", f"refs/heads/{raw}", raw=True, check=False)
        if rc != 0:
            raise Refuse(f"no local branch {raw!r}")
        sha = sha.strip()
        # Fresh truth from upstream, so a doctored local upstream/<default> cannot shrink the range we scan.
        self._git(rd, "fetch", self._url(up), f"+refs/heads/{db}:refs/remotes/upstream/{db}", timeout=300)
        rng = f"refs/remotes/upstream/{db}...refs/heads/{raw}"
        commits = self._git(rd, "log", "--no-merges", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%s%x1f%B%x1e",
                            f"refs/remotes/upstream/{db}..refs/heads/{raw}")
        recs = [c for c in commits.split("\x1e") if c.strip()]
        if not recs:
            raise Refuse("nothing to push: the branch has no commits beyond upstream/" + db)
        files = [f for f in self._git(rd, "diff", "--no-ext-diff", "--no-textconv", "--name-only", rng).split("\n") if f]
        diff = self._git(rd, "diff", "--no-ext-diff", "--no-textconv", "-U0", rng)
        if len(diff) > MAX_DIFF:
            raise Refuse("diff is too large to scan for markers; split the change")
        added = "\n".join(ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        hits = find_markers(commits, added, "\n".join(files))
        if hits:
            raise Refuse(f"tooling or attribution markers found in the commits or diff: {', '.join(hits)}. "
                         "Remove them with a new commit (no amending) and try again.")
        self._check_files(rd, sha, files)
        subjects = [r.split("\x1f")[5] for r in recs]
        fork = reg.get("fork")
        if fork:
            fi = self.api.get(f"/repos/{fork}")
            if not (fi.get("fork") and fi.get("owner", {}).get("login", "").lower() == self.me().lower()
                    and str((fi.get("parent") or {}).get("full_name", "")).lower() == up.lower()):
                raise Refuse("origin is not your own fork of the upstream repo")
        elif not self.cfg.dryrun:
            raise Refuse("no fork recorded for this clone: gh_fork, then gh_clone again")
        text = (f"PUSH branch {raw} ({sha[:10]}) to {fork or '(your fork, not yet created)'} via "
                f"{self._url(fork) if fork else self.cfg.ssh_host}\nupstream: {up}  base: {db}\n"
                f"commits ({len(recs)}):\n" + "\n".join(f"  {s}" for s in subjects[:30]) +
                f"\nfiles ({len(files)}):\n" + "\n".join(f"  {f}" for f in files[:100]) +
                f"\nnormal (non-force) push; markers: none; {self._evidence_summary(up)}")

        def do():
            rc2, out, err = self._git(rd, "push", "--no-verify", "--porcelain", self._url(fork),
                                      f"refs/heads/{raw}:refs/heads/{raw}", timeout=300, raw=True, check=False)
            if rc2 != 0:
                raise Refuse("push rejected: " + redact(_clip((err or out).strip(), 300)) +
                             " (never force: add a new commit or rebase-free fix)")
            self._record_push(rd, up, raw, sha, files, True)
            return f"pushed {raw} ({sha[:10]}) to {fork}"
        if self.cfg.dryrun:
            self._record_push(rd, up, raw, sha, files, False)
        return self._write(Draft("gh_push", up, text), do)

    def _check_files(self, rd: Path, sha: str, files: list[str]) -> None:
        bad = []
        for f in files:
            why = artifact_looking(f)
            if why:
                bad.append(f"{f} ({why})")
        if files:
            ls = self._git(rd, "ls-tree", "-r", "-l", sha, "--", *files)
            for ln in ls.splitlines():
                meta, _, name = ln.partition("\t")
                parts = meta.split()
                if len(parts) >= 4 and parts[3].isdigit() and int(parts[3]) > MAX_FILE_BYTES:
                    bad.append(f"{name} (over 1 MB)")
        if bad:
            raise Refuse("artifact-looking files in the change: " + "; ".join(bad[:10]) + ". Remove them with a new commit.")

    def _record_push(self, rd: Path, up: str, branch: str, sha: str, files: list[str], live: bool) -> None:
        d = self._load("pushes.json")
        d[f"{up}#{branch}"] = {"repo_dir": str(rd), "head_sha": sha, "files": sorted(files), "live": live, "time": time.time()}
        self._store("pushes.json", d)

    # ======================================================================== issue

    def gh_issue_create(self, repo: str, title: str, body: str) -> str:
        repo = self._repo(repo)
        title, body = _text(title, "title"), _text(body, "body")
        hits = find_markers(title, body)
        if hits:
            raise Refuse(f"tooling or attribution markers in the title/body: {', '.join(hits)}")
        i = self._info(repo)
        if i["archived"] or i["disabled"]:
            raise Refuse(f"{repo} is archived or disabled")
        if not i["has_issues"]:
            raise Refuse(f"{repo} has issues disabled")
        com = self._community(repo)
        if com["approval"]:
            raise Refuse("CONTRIBUTING seems to require approval, permission or assignment first: " +
                         "; ".join(com["approval"]) + ". Ask the user to handle it.")
        ev = self._evidence(repo)
        distinct = {r["query"].lower() for r in ev}
        kinds = {r["kind"] for r in ev}
        if len(distinct) < 4 or kinds < {"issues", "prs"}:
            raise Refuse(f"search evidence is not enough: {len(distinct)} distinct gh_search queries on {repo} in this run "
                         f"(kinds: {', '.join(sorted(kinds)) or 'none'}); need at least 4 distinct, covering both issues and prs. "
                         "Search open and closed issues and PRs for this problem first.")
        text = (f"NEW ISSUE on {repo}\nTitle: {title}\n\n{body}\n\n--\nissue templates: "
                f"{', '.join(com['issue_templates']) or 'none'}\n{self._evidence_summary(repo)}")

        def do():
            r = self.api.call("POST", f"/repos/{repo}/issues", {"title": title, "body": body})
            return f"issue created: #{(r or {}).get('number')} {(r or {}).get('html_url')}"
        return self._write(Draft("gh_issue_create", repo, text), do)

    # ======================================================================== PR

    def _competing_check(self, repo: str, issue: int, files: set[str]) -> tuple[list[str], list[str]]:
        me = self.me().lower()
        blockers: list[str] = []
        notes: list[str] = []
        iss = self.api.get(f"/repos/{repo}/issues/{issue}")
        if not isinstance(iss, dict):
            raise ApiError(0, "unexpected issue response")
        if iss.get("pull_request"):
            raise Refuse(f"#{issue} is a pull request, not an issue")
        if iss.get("state") != "open":
            blockers.append(f"issue #{issue} is {iss.get('state')}")
        for a in iss.get("assignees") or []:
            if str(a.get("login", "")).lower() != me:
                blockers.append(f"issue #{issue} is assigned to @{a.get('login')}")

        def judge(num, author, state, merged, how):
            if str(author).lower() == me:
                return
            if state == "open" or merged:
                blockers.append(f"{how}: PR #{num} by @{author} ({'merged' if merged else 'open'})")
            else:
                notes.append(f"{how}: PR #{num} by @{author} was closed unmerged")

        ref = re.compile(rf"(?<![\w/])#{issue}\b|/issues/{issue}\b")
        found = self.api.get("/search/issues", {"q": f"repo:{repo} is:pr {issue}", "per_page": 50})
        if not isinstance(found, dict) or "items" not in found:
            raise ApiError(0, "unexpected search response")
        for it in found["items"]:
            if ref.search((it.get("title") or "") + "\n" + (it.get("body") or "")):
                judge(it.get("number"), (it.get("user") or {}).get("login", "?"), it.get("state"),
                      bool((it.get("pull_request") or {}).get("merged_at")), "search by issue number")
        for ev in self.api.paged(f"/repos/{repo}/issues/{issue}/timeline", max_pages=3):
            kind = ev.get("event")
            if kind == "cross-referenced":
                src = (ev.get("source") or {}).get("issue") or {}
                if src.get("pull_request"):
                    judge(src.get("number"), (src.get("user") or {}).get("login", "?"), src.get("state"),
                          bool(src["pull_request"].get("merged_at")), "timeline cross-reference")
            elif kind == "closed" and ev.get("commit_id"):
                blockers.append(f"issue was closed by commit {str(ev['commit_id'])[:10]}")
            elif kind == "referenced" and str((ev.get("actor") or {}).get("login", "")).lower() != me:
                notes.append("a commit by someone else references the issue")
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

    def _push_record(self, repo: str, branch: str) -> dict:
        rec = self._load("pushes.json").get(f"{repo}#{branch}")
        if not rec:
            raise Refuse(f"no gh_push of branch {branch!r} to your fork was recorded in this setup: push first")
        if not rec.get("live") and not self.cfg.dryrun:
            raise Refuse("the recorded push was a dry-run; push for real first")
        return rec

    def gh_pr_create(self, repo: str, head: str, base: str, title: str, body: str, issue: int) -> str:
        try:
            draft, do = self._pr_gates(repo, head, base, title, body, issue)
        except ApiError as e:                       # any check that cannot complete is a refusal
            raise Refuse(f"a pull-request check could not complete ({e}); refusing (fail closed)") from None
        return self._write(draft, do)

    def _pr_gates(self, repo, head, base, title, body, issue):
        repo, n = self._repo(repo), _int(issue, "issue")
        title, body, head, base = _text(title, "title"), _text(body, "body"), _text(head, "head"), _text(base, "base")
        hits = find_markers(title, body)
        if hits:
            raise Refuse(f"tooling or attribution markers in the title/body: {', '.join(hits)}")
        if not re.search(rf"\bFixes\s+#{n}\b", body, re.I):
            raise Refuse(f"the PR body must contain 'Fixes #{n}'")
        i = self._info(repo)
        if i["archived"] or i["disabled"]:
            raise Refuse(f"{repo} is archived or disabled")
        if i["policy"] not in (None, "", "all"):
            raise Refuse(f"{repo} restricts who may open pull requests (pull_request_creation_policy={i['policy']})")
        if base != i["default_branch"]:
            raise Refuse(f"base must be the default branch ({i['default_branch']})")
        me = self.me()
        owner, _, branch = head.rpartition(":")
        if owner and owner.lower() != me.lower():
            raise Refuse("head must be a branch on your own fork")
        rec = self._push_record(repo, branch)
        pushed = set(rec["files"])
        # (e) the PR must contain exactly what was pushed, and nothing artifact-looking
        if self.cfg.dryrun and not rec.get("live"):
            rd = Path(rec["repo_dir"])
            reg = self._load("registry.json").get(str(rd)) or {}
            if not rd.is_dir() or not reg:
                raise Refuse("the dry-run clone is gone; push again")
            actual = {f for f in self._git(rd, "diff", "--no-ext-diff", "--no-textconv", "--name-only",
                      f"refs/remotes/upstream/{reg['default_branch']}...refs/heads/{branch}").split("\n") if f}
        else:
            fork = self._existing_fork(repo)
            if not fork:
                raise Refuse("no fork of this repo found under your account")
            br = self.api.get(f"/repos/{fork}/branches/{urllib.parse.quote(branch)}")
            if str(((br or {}).get("commit") or {}).get("sha", "")) != rec["head_sha"]:
                raise Refuse("the branch on your fork is not the commit that gh_push recorded; push again")
            cmp = self.api.get(f"/repos/{repo}/compare/{urllib.parse.quote(base)}...{me}:{urllib.parse.quote(branch)}")
            cfiles = (cmp or {}).get("files")
            if not isinstance(cfiles, list) or len(cfiles) >= 300:
                raise ApiError(0, "could not list the PR's files (missing or 300+); failing closed")
            actual = {f.get("filename") for f in cfiles}
        if actual != pushed:
            raise Refuse("the PR's file list differs from the pushed diff: "
                         f"extra {sorted(actual - pushed)[:5]}, missing {sorted(pushed - actual)[:5]}")
        bad = [f"{f} ({artifact_looking(f)})" for f in sorted(actual) if artifact_looking(f)]
        if bad:
            raise Refuse("artifact-looking files in the PR: " + "; ".join(bad[:10]))
        # (b) no one else is already on it; an incomplete check refuses
        try:
            blockers, notes = self._competing_check(repo, n, actual)
        except Refuse:
            raise
        except ApiError as e:
            raise Refuse(f"the competing-PR check could not complete ({e}); refusing (fail closed)") from None
        if blockers:
            raise Refuse("someone else is already on this issue: " + "; ".join(blockers[:6]))
        summary = f"competing-PR check: clean ({'; '.join(notes) if notes else 'no notes'})"
        text = (f"NEW PULL REQUEST on {repo}\nfrom {me}:{branch} -> {base}   closes #{n}\nTitle: {title}\n\n{body}\n\n--\n"
                f"files ({len(actual)}):\n" + "\n".join(f"  {f}" for f in sorted(actual)[:100]) +
                f"\n{self._evidence_summary(repo)}\n{summary}")

        def do():
            r = self.api.call("POST", f"/repos/{repo}/pulls", {"title": title, "head": f"{me}:{branch}", "base": base, "body": body})
            return f"PR created: #{(r or {}).get('number')} {(r or {}).get('html_url')}"
        return Draft("gh_pr_create", repo, text), do

    # ======================================================================== review replies

    def _my_thread(self, repo: str, n: int, thread: str) -> dict:
        author, threads = self._threads(repo, n)
        if author.lower() != self.me().lower():
            raise Refuse("replies are only allowed on your own pull requests")
        for t in threads:
            if t.get("id") == thread:
                return t
        raise Refuse("no such review thread on this PR (use gh_pr_threads for the ids)")

    def gh_pr_reply(self, repo: str, number: int, thread: str, body: str, sha: str) -> str:
        repo, n = self._repo(repo), _int(number, "number")
        thread, body, sha = _text(thread, "thread"), _text(body, "body"), _text(sha, "sha").strip()
        if not SHA_RE.match(sha):
            raise Refuse("sha must be a 7-40 character commit hash")
        if sha[:7].lower() not in body.lower():
            body = f"{body.rstrip()}\n\nAddressed in {sha[:10]}."
        hits = find_markers(body)
        if hits:
            raise Refuse(f"tooling or attribution markers in the reply: {', '.join(hits)}")
        self._my_thread(repo, n, thread)
        commits = self.api.paged(f"/repos/{repo}/pulls/{n}/commits", max_pages=3)
        if not any(str(c.get("sha", "")).lower().startswith(sha.lower()) for c in commits):
            raise Refuse("that commit is not in the pull request; push it first")
        text = f"REPLY on {repo} PR #{n}, thread {thread}\n\n{body}"

        def do():
            self.api.graphql("mutation($t:ID!,$b:String!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$t,body:$b}){comment{id}}}",
                             {"t": thread, "b": body})
            reps = self._load("replies.json")
            reps[f"{repo}#{n}#{thread}"] = {"sha": sha, "live": True, "time": time.time()}
            self._store("replies.json", reps)
            return "reply posted"
        out = self._write(Draft("gh_pr_reply", repo, text), do)
        if self.cfg.dryrun:
            reps = self._load("replies.json")
            reps[f"{repo}#{n}#{thread}"] = {"sha": sha, "live": False, "time": time.time()}
            self._store("replies.json", reps)
        return out

    def gh_pr_resolve_thread(self, repo: str, number: int, thread: str) -> str:
        repo, n, thread = self._repo(repo), _int(number, "number"), _text(thread, "thread")
        rep = self._load("replies.json").get(f"{repo}#{n}#{thread}")
        if not rep or (not rep.get("live") and not self.cfg.dryrun):
            raise Refuse("reply to this thread with a commit SHA (gh_pr_reply) before resolving it")
        t = self._my_thread(repo, n, thread)
        if t.get("isResolved"):
            return "thread is already resolved (nothing sent)"
        text = f"RESOLVE thread {thread} on {repo} PR #{n} (replied with {rep['sha'][:10]})"

        def do():
            self.api.graphql("mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{id isResolved}}}", {"t": thread})
            return "thread resolved"
        return self._write(Draft("gh_pr_resolve_thread", repo, text), do)


# --------------------------------------------------------------------------- MCP plumbing

def _tool(name, desc, props, required):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": {k: {"type": v} for k, v in props.items()}, "required": required}}


S, INT = "string", "integer"
TOOLS = [
    _tool("gh_repo_info", "Repo rules: issues on?, default branch, PR policy, CONTRIBUTING (CLA/approval flags).", {"repo": S}, ["repo"]),
    _tool("gh_search", "Search open+closed issues or prs in a repo (logged as evidence).", {"repo": S, "query": S, "kind": S}, ["query"]),
    _tool("gh_get_file", "Read a file or folder from a repo (optional ref).", {"repo": S, "path": S, "ref": S}, ["repo", "path"]),
    _tool("gh_list_pr_files", "Files changed in a PR.", {"repo": S, "number": INT}, ["repo", "number"]),
    _tool("gh_pr_threads", "Review threads on a PR.", {"repo": S, "number": INT}, ["repo", "number"]),
    _tool("gh_fork", "Fork a repo (user confirms).", {"repo": S}, ["repo"]),
    _tool("gh_clone", "Clone your fork into the work folder; adds upstream.", {"repo": S, "dest": S}, ["repo", "dest"]),
    _tool("gh_branch", "Fetch upstream and create a feature branch from its default branch.", {"repo_dir": S, "name": S}, ["repo_dir", "name"]),
    _tool("gh_setup_env", "Make a venv with the repo's declared test/lint deps; returns the command prefix to use.",
          {"repo_dir": S, "extras": S}, ["repo_dir"]),
    _tool("gh_push", "Normal push of a feature branch to your fork (user confirms; never force).", {"repo_dir": S, "branch": S}, ["repo_dir", "branch"]),
    _tool("gh_issue_create", "Draft an issue; the user reviews it, then it is posted.", {"repo": S, "title": S, "body": S}, ["repo", "title", "body"]),
    _tool("gh_pr_create", "Draft a PR (body needs 'Fixes #N'); the user reviews it, then it is posted.",
          {"repo": S, "head": S, "base": S, "title": S, "body": S, "issue": INT}, ["repo", "head", "base", "title", "body", "issue"]),
    _tool("gh_pr_reply", "Reply to a review thread on your PR with a commit sha.", {"repo": S, "number": INT, "thread": S, "body": S, "sha": S},
          ["repo", "number", "thread", "body", "sha"]),
    _tool("gh_pr_resolve_thread", "Resolve a review thread you already replied to.", {"repo": S, "number": INT, "thread": S}, ["repo", "number", "thread"]),
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def call_tool(gate: Gate, name: str, args: dict) -> dict:
    fn = getattr(gate, name)
    try:
        inspect.signature(fn).bind(**args)
    except TypeError as e:
        return {"content": [{"type": "text", "text": redact(f"bad arguments: {e}")}], "isError": True}
    try:
        text, err = fn(**args), False
    except ChangesRequested as e:
        text, err = str(e), False
    except Refuse as e:
        text, err = f"REFUSED: {e}", True
    except ApiError as e:
        text, err = f"GitHub API error: {e}", True
    except Exception as e:   # noqa: BLE001  never let a bug kill the server or print a traceback with secrets
        text, err = f"internal error: {type(e).__name__}: {e}", True
    text = redact(text)
    if len(text) > MAX_OUT:
        text = text[:MAX_OUT] + "\n[... output truncated]"
    return {"content": [{"type": "text", "text": text}], **({"isError": True} if err else {})}


def handle(gate: Gate, msg: dict) -> dict | None:
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                   "serverInfo": {"name": "freeharness-ghgate", "version": "0.1.0"},
                   "instructions": "GitHub contribution tools with enforced gates. Search before filing; writes need the user's review."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOL_NAMES:
            return err(-32602, f"unknown tool {name!r}")
        if not isinstance(args, dict):
            return ok({"content": [{"type": "text", "text": "bad arguments"}], "isError": True})
        return ok(call_tool(gate, name, args))
    return err(-32601, f"method not found: {method}")


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    gate = Gate(Config.from_env())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(gate, msg)
        if resp is not None:
            sys.stdout.write(redact(json.dumps(resp, ensure_ascii=False)) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
