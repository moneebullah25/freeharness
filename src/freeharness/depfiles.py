"""
Turn a repository's OWN declared dependency files into a flat, vetted list of pip requirements, without running
anything from the repository: no setup.py, no build backend, no `pip install .`, no `-e`.

Used by the gh_setup_env tool (mcp/ghgate.py). Input is text (read from git objects by the caller); output is
requirement strings that match a strict PEP 508 shape, plus an honest list of every line that was skipped and why.

Read:    pyproject.toml  [project].dependencies, [project.optional-dependencies] and [dependency-groups] whose name
         is a test/lint/dev kind (or is asked for), requirements*.txt at the top level, requirements/*.txt
Skipped: -e/--editable, local paths, VCS and URL requirements (anything not https://pypi.org or
         https://files.pythonhosted.org), index/find-links/trusted-host/pre/hash options, constraint files, anything that
         is not a plain `name[extras] spec ; marker` line. The project itself is never installed.
"""
from __future__ import annotations

import posixpath
import re
from typing import Callable

DEFAULT_GROUPS = {"dev", "develop", "development", "test", "tests", "testing", "lint", "linting", "ci", "check", "checks",
                  "style", "typing", "types", "quality", "qa"}
BASE_STEMS = {"base", "common", "main", "default", "runtime", "core"}
EXCLUDE_TOKENS = {"doc", "docs", "documentation", "prod", "production", "deploy", "release", "build", "example",
                  "examples", "windows", "mac", "macos", "gpu", "cuda"}
ALLOWED_URL_HOSTS = {"pypi.org", "files.pythonhosted.org"}
MAX_FILES, MAX_FILE_BYTES, MAX_REQS, MAX_DEPTH = 30, 200_000, 400, 6

NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
HEAD_RE = re.compile(rf"^({NAME})\s*(\[\s*{NAME}(?:\s*,\s*{NAME})*\s*\])?\s*([<>=!~(][A-Za-z0-9.,*+!<>=~\s()-]*)?$")
MARKER_RE = re.compile(r"^[A-Za-z0-9._\s\"'<>=!~(),*+-]*$")
URL_REQ_RE = re.compile(rf"^({NAME})\s*(?:\[[^\]]*\])?\s*@\s*(\S+)\s*(?:;.*)?$")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------- pyproject.toml

def _strip_comment(line: str) -> str:
    q = ""
    for i, c in enumerate(line):
        if q:
            if c == "\\" and q == '"':
                continue
            if c == q and not (q == '"' and line[i - 1] == "\\"):
                q = ""
        elif c in "\"'":
            q = c
        elif c == "#":
            return line[:i]
    return line


def _depth(s: str) -> int:
    d, q = 0, ""
    for i, c in enumerate(s):
        if q:
            if c == q and not (q == '"' and s[i - 1] == "\\"):
                q = ""
        elif c in "\"'":
            q = c
        elif c in "[{":
            d += 1
        elif c in "]}":
            d -= 1
    return d


class _Val:
    """A tiny parser for the TOML value forms dependency tables use: strings, arrays, inline tables, scalars."""

    def __init__(self, s: str):
        self.s, self.i = s, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n,":
            self.i += 1

    def value(self):
        self.ws()
        c = self.s[self.i]
        if c == "[":
            self.i += 1
            out = []
            while True:
                self.ws()
                if self.s[self.i] == "]":
                    self.i += 1
                    return out
                out.append(self.value())
        if c == "{":
            self.i += 1
            out = {}
            while True:
                self.ws()
                if self.s[self.i] == "}":
                    self.i += 1
                    return out
                k = self.key()
                self.ws()
                if self.s[self.i] != "=":
                    raise ValueError("bad inline table")
                self.i += 1
                out[k] = self.value()
        if c in "\"'":
            return self.string()
        j = self.i
        while j < len(self.s) and self.s[j] not in ",]}\n":
            j += 1
        raw, self.i = self.s[self.i:j].strip(), j
        return {"true": True, "false": False}.get(raw, raw)

    def key(self) -> str:
        self.ws()
        if self.s[self.i] in "\"'":
            return self.string()
        j = self.i
        while j < len(self.s) and re.match(r"[A-Za-z0-9_.-]", self.s[j]):
            j += 1
        k, self.i = self.s[self.i:j], j
        return k

    def string(self) -> str:
        q = self.s[self.i]
        self.i += 1
        out = []
        while self.s[self.i] != q:
            c = self.s[self.i]
            if c == "\\" and q == '"':
                self.i += 1
                c = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(self.s[self.i], "\\" + self.s[self.i])
            out.append(c)
            self.i += 1
        self.i += 1
        return "".join(out)


def _mini_toml(text: str) -> dict:
    """Enough TOML for pyproject dependency tables on Python 3.10 (no tomllib there). Unknown syntax is skipped."""
    root: dict = {}
    cur = root
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = _strip_comment(lines[i]).strip()
        i += 1
        if not line:
            continue
        if line.startswith("[["):
            cur = {}                                           # arrays of tables are not needed
            continue
        if line.startswith("[") and line.endswith("]") and "=" not in line.split("]")[0]:
            cur = root
            try:
                for part in _split_dotted(line[1:-1]):
                    cur = cur.setdefault(part, {})
                    if not isinstance(cur, dict):
                        cur = {}
            except (ValueError, IndexError):
                cur = {}
            continue
        m = re.match(r"^((?:[A-Za-z0-9_-]+|\"[^\"]*\"|'[^']*')(?:\s*\.\s*(?:[A-Za-z0-9_-]+|\"[^\"]*\"|'[^']*'))*)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        while _depth(val) > 0 and i < len(lines):
            val += "\n" + _strip_comment(lines[i])
            i += 1
        try:
            parsed = _Val(val.strip()).value()
        except (ValueError, IndexError):
            continue
        tgt = cur
        parts = _split_dotted(key)
        for part in parts[:-1]:
            tgt = tgt.setdefault(part, {})
            if not isinstance(tgt, dict):
                break
        else:
            tgt[parts[-1]] = parsed
    return root


def _split_dotted(key: str) -> list[str]:
    return [p.strip().strip("\"'") for p in re.findall(r"\"[^\"]*\"|'[^']*'|[^.]+", key) if p.strip()]


def load_toml(text: str) -> dict:
    try:
        import tomllib as tl                                  # Python 3.11+
    except ImportError:
        try:
            import tomli as tl                                # type: ignore[no-redef]
        except ImportError:
            return _mini_toml(text)
    return tl.loads(text)


# --------------------------------------------------------------------------- one requirement line

def vet(line: str) -> tuple[str | None, str]:
    """(clean requirement, "") or (None, reason it was skipped). Blank and comment-only lines give (None, "")."""
    s = re.sub(r"(^|\s)#.*$", "", line.strip()).strip()
    if not s:
        return None, ""
    s = re.sub(r"\s+--hash[= ]\S+", "", s).strip()
    s = " ".join(s.split())           # hashes are not enforced: the pins are what they are
    if len(s) > 300:
        return None, f"{s[:40]}... (line too long)"
    if s.startswith("-"):
        opt = s.split()[0].split("=")[0]
        why = ("editable installs run the project's own build backend" if opt in ("-e", "--editable") else
               "constraint files are not applied" if opt in ("-c", "--constraint") else
               "index settings come from freeharness, not from repo files" if opt in
               ("-i", "--index-url", "--extra-index-url", "-f", "--find-links", "--trusted-host", "--no-index") else
               "pip options in repo files are ignored")
        return None, f"{s[:80]} ({why})"
    m = URL_REQ_RE.match(s)
    if m:
        url = m.group(2)
        host = re.match(r"^https://([^/:@]+)(?::\d+)?/", url + "/")
        if host and host.group(1).lower() in ALLOWED_URL_HOSTS:
            return s, ""
        return None, f"{s[:80]} (direct URL outside pypi.org)"
    if "://" in s or s.startswith((".", "/", "~")) or re.search(r"\.(whl|zip|tar\.gz|tgz|tar\.bz2)\b", s):
        return None, f"{s[:80]} (URL, VCS or local path requirement)"
    head, _, marker = s.partition(";")
    if not HEAD_RE.match(head.strip()) or (marker and not MARKER_RE.match(marker)):
        return None, f"{s[:80]} (not a plain `name[extras] version ; marker` requirement)"
    return s, ""


# --------------------------------------------------------------------------- collecting

class Collected:
    def __init__(self):
        self.reqs: list[str] = []
        self.sources: list[str] = []
        self.skipped: list[str] = []
        self.notes: list[str] = []
        self.project = ""
        self.found: set[str] = set()      # asked-for extras that matched something

    def add(self, line: str) -> None:
        clean, why = vet(line)
        if why:
            self.skipped.append(why)
        elif clean and clean not in self.reqs and norm(re.split(r"[\[\s;<>=!~(@]", clean, maxsplit=1)[0]) != self.project:
            if len(self.reqs) >= MAX_REQS:
                self.notes.append(f"more than {MAX_REQS} requirements: the rest were not read")
                return
            self.reqs.append(clean)


def _expand(col: Collected, items, tables: dict, seen: set, depth: int = 0, opt: dict | None = None) -> None:
    """Add requirement strings; follow {include-group} entries (in `tables`) and self-references like `proj[a,b]`
    (in the optional-dependencies `opt`)."""
    opt = tables if opt is None else opt
    for it in items or []:
        if isinstance(it, dict) and "include-group" in it:
            g = norm(str(it["include-group"]))
            if g not in seen and depth < MAX_DEPTH and g in tables:
                seen.add(g)
                _expand(col, tables[g], tables, seen, depth + 1, opt)
            continue
        if not isinstance(it, str):
            col.skipped.append(f"{str(it)[:60]} (not a string requirement)")
            continue
        m = re.match(rf"^\s*({NAME})\s*\[([^\]]*)\]", it)
        if m and norm(m.group(1)) == col.project:               # the project's own extras: expand them, do not install it
            for extra in (norm(x) for x in m.group(2).split(",") if x.strip()):
                if extra not in seen and depth < MAX_DEPTH and extra in opt:
                    seen.add(extra)
                    _expand(col, opt[extra], tables, seen, depth + 1, opt)
            continue
        col.add(it)


def _wanted_group(name: str, extras: set[str]) -> bool:
    n = norm(name)
    return n in DEFAULT_GROUPS or n in extras


def _from_pyproject(col: Collected, text: str, extras: set[str]) -> None:
    try:
        data = load_toml(text)
    except ValueError as e:
        col.notes.append(f"pyproject.toml could not be parsed ({str(e)[:60]})")
        return
    proj = data.get("project") if isinstance(data.get("project"), dict) else {}
    col.project = norm(str(proj.get("name", "")))
    opt = {norm(k): v for k, v in (proj.get("optional-dependencies") or {}).items() if isinstance(v, list)}
    groups = {norm(k): v for k, v in (data.get("dependency-groups") or {}).items() if isinstance(v, list)}
    used = []
    if proj.get("dependencies"):
        _expand(col, proj["dependencies"], opt, set(), 0, opt)
        used.append("dependencies")
    for kind, table in (("optional-dependencies", opt), ("dependency-groups", groups)):
        for name in table:
            if _wanted_group(name, extras):
                _expand(col, table[name], table, {name}, 0, opt)
                used.append(f"{kind}.{name}")
                col.found.add(name)
    if (data.get("tool") or {}).get("poetry") and not proj:
        col.notes.append("pyproject uses Poetry's own dependency tables, which are not read")
    if used:
        col.sources.append("pyproject.toml[" + ", ".join(used) + "]")


def _logical_lines(text: str) -> list[str]:
    out, buf = [], ""
    for ln in text.splitlines():
        if ln.rstrip().endswith("\\"):
            buf += ln.rstrip()[:-1] + " "
            continue
        out.append(buf + ln)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _stem_wanted(path: str, extras: set[str]) -> bool:
    stem = posixpath.basename(path).rsplit(".", 1)[0].lower()
    toks = [t for t in re.split(r"[-_.]+", stem) if t]
    if norm(stem) in extras or any(norm(t) in extras for t in toks):
        return True
    if "extras" in path.lower().split("/"):
        return False
    if set(toks) & EXCLUDE_TOKENS:
        return False
    return toks == ["requirements"] or bool(set(toks) & (DEFAULT_GROUPS | BASE_STEMS))


def _from_requirements(col: Collected, path: str, read: Callable[[str], str | None], budget: list, seen: set,
                       depth: int = 0) -> None:
    if path in seen:
        return
    seen.add(path)
    if budget[0] <= 0 or depth > MAX_DEPTH:
        col.notes.append(f"{path}: file limit ({MAX_FILES}) or include depth reached")
        return
    budget[0] -= 1
    text = read(path)
    if text is None:
        col.notes.append(f"{path} could not be read")
        return
    col.sources.append(path)
    for ln in _logical_lines(text):
        m = re.match(r"^\s*(?:-r|--requirement)(?:\s+|=)?(\S+)", ln)
        if m:
            tgt = m.group(1)
            full = posixpath.normpath(posixpath.join(posixpath.dirname(path), tgt))
            if "://" in tgt or full.startswith(("..", "/")):
                col.skipped.append(f"{ln.strip()[:80]} (include outside the repository)")
            else:
                _from_requirements(col, full, read, budget, seen, depth + 1)
            continue
        col.add(ln)


def collect(root_files: list[str], requirements_files: list[str], read: Callable[[str], str | None],
            extras: set[str] | None = None) -> Collected:
    """root_files: names at the top of the repo; requirements_files: paths under requirements/ (any depth);
    read(path) -> text or None. `extras` are group/file names the caller asked for (already validated)."""
    extras = {norm(e) for e in (extras or set())}
    col = Collected()
    if "pyproject.toml" in root_files:
        text = read("pyproject.toml")
        if text is not None:
            _from_pyproject(col, text, extras)
    chosen = [f for f in sorted(root_files) if re.fullmatch(r"[\w.-]*requirements[\w.-]*\.txt", f) and _stem_wanted(f, extras)]
    chosen += [f for f in sorted(requirements_files) if f.endswith(".txt") and _stem_wanted(f, extras)]
    budget, seen = [MAX_FILES], set()
    for f in chosen:
        _from_requirements(col, f, read, budget, seen)
        stem = posixpath.basename(f).rsplit(".", 1)[0].lower()
        col.found |= {e for e in extras if e == norm(stem) or e in {norm(t) for t in re.split(r"[-_.]+", stem)}}
    if extras - col.found:
        col.notes.append("extras not found in any declared file: " + ", ".join(sorted(extras - col.found)))
    return col
