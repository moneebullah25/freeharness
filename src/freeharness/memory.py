"""
Long-term memory: typed facts in a local sqlite3 file (stdlib), searched with FTS5 when the
sqlite build has it and with a BM25 ranking in Python otherwise (same idea as the notes-vault
server: no embeddings, nothing to rebuild).

A fact has a type (user | feedback | project | reference), a scope (global, or project = the
working directory it was learned in, which also applies to sub-folders), the session it came
from, timestamps and a `pinned` flag. Pinned facts are what `memory_cmd.render_block` puts in
the always-on AGENTS.md block. Saving something that is already known (same normalised text,
or a near-duplicate of the same type and scope) UPDATES that fact instead of adding a copy.

Nothing here touches the network. See docs/SESSIONS_AND_MEMORY.md and docs/PRIVACY.md.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import time
from pathlib import Path

from . import paths

TYPES = ("user", "feedback", "project", "reference")
MAX_TEXT = 600
NEAR_DUPLICATE = 0.75          # token Jaccard above which a save updates the existing fact
TOKEN = re.compile(r"[a-z0-9_]+")


def db_path() -> Path:
    return paths.data_dir() / "memory" / "memory.sqlite3"


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def _norm(text: str) -> str:
    return " ".join(tokens(text))


def _jaccard(a: str, b: str) -> float:
    x, y = set(tokens(a)), set(tokens(b))
    return len(x & y) / len(x | y) if x | y else 0.0


SECRET = re.compile(
    r"(?i)(?:\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abprs]|glpat|AKIA|AIza|ya29)[-_A-Za-z0-9]{10,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:password|passwd|pwd|secret|api[_ -]?key|token|bearer|auth(?:orization)?)\b\s*(?:is|=|:)?\s*[\"']?[^\s\"']{6,}"
    r"|\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9+/_-]{40,}={0,2}(?=\s|$))")


def looks_secret(text: str) -> bool:
    """Heuristic: token/key/password shaped text must never be written to memory (it is re-sent to chat sites)."""
    return bool(SECRET.search(text))


def norm_project(cwd) -> str:
    return str(Path(cwd).expanduser().resolve())


class MemoryStore:
    def __init__(self, path: Path | None = None, fts: bool | None = None):
        self.path = Path(path) if path else db_path()
        paths.private_dir(self.path.parent)
        new = not self.path.exists()
        if new:                                           # create 0600 before sqlite writes anything
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        self.db = sqlite3.connect(str(self.path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, type TEXT NOT NULL, scope TEXT NOT NULL, "
            "project TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0, "
            "source_session TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL)")
        self.fts = False
        if fts is not False:
            try:
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text)")
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
        self.db.commit()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(str(self.path) + suffix, 0o600)
            except OSError:
                pass

    def close(self):
        self.db.close()

    # -- writes ------------------------------------------------------------

    def _index(self, fid: int, text: str):
        if self.fts:
            self.db.execute("DELETE FROM facts_fts WHERE rowid=?", (fid,))
            self.db.execute("INSERT INTO facts_fts(rowid, text) VALUES (?, ?)", (fid, " ".join(tokens(text))))

    def save(self, text: str, type: str = "feedback", scope: str = "global", cwd=None,
             session: str = "", pinned: bool | None = None, now: float | None = None) -> dict:
        """Add a fact, or update the matching one. Returns the fact plus "action": added | updated."""
        text = re.sub(r"\s+", " ", str(text)).strip()
        if not text:
            raise ValueError("empty fact")
        if type not in TYPES:
            raise ValueError(f"type must be one of {', '.join(TYPES)}")
        if scope not in ("global", "project"):
            raise ValueError("scope must be global or project")
        if scope == "project" and cwd is None:
            raise ValueError("project scope needs a working directory")
        text = text[:MAX_TEXT]
        if looks_secret(text):
            raise ValueError("refused: this looks like a secret (token, key or password). Memory is sent to the chat "
                             "site in prompts, so keep secrets out of it.")
        project = norm_project(cwd) if scope == "project" else ""
        now = now or time.time()
        if pinned is None:
            pinned = type in ("feedback", "user")         # conventions and preferences are always-on by default
        dup = None
        for r in self.db.execute("SELECT * FROM facts WHERE scope=? AND project=?", (scope, project)):
            if _norm(r["text"]) == _norm(text) or (r["type"] == type and _jaccard(r["text"], text) >= NEAR_DUPLICATE):
                dup = r
                break
        if dup:
            self.db.execute("UPDATE facts SET text=?, type=?, pinned=?, updated=?, source_session=? WHERE id=?",
                            (text, type, int(bool(pinned) or bool(dup["pinned"])), now, session or dup["source_session"], dup["id"]))
            fid, action = dup["id"], "updated"
        else:
            cur = self.db.execute(
                "INSERT INTO facts(type, scope, project, text, pinned, source_session, created, updated) "
                "VALUES (?,?,?,?,?,?,?,?)", (type, scope, project, text, int(bool(pinned)), session, now, now))
            fid, action = cur.lastrowid, "added"
        self._index(fid, text)
        self.db.commit()
        out = self.get(fid)
        out["action"] = action
        return out

    def forget(self, ref, cwd=None) -> list[dict]:
        """Delete by id, or by text query when it matches exactly one visible fact. Returns what was removed."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            victims = [f for f in [self.get(int(ref))] if f]
        else:
            hits = self.search(str(ref), cwd=cwd, limit=2)
            victims = hits if len(hits) == 1 else []
            if len(hits) > 1:
                raise ValueError("several facts match; forget by id: " + ", ".join(str(h["id"]) for h in hits))
        for f in victims:
            self.db.execute("DELETE FROM facts WHERE id=?", (f["id"],))
            if self.fts:
                self.db.execute("DELETE FROM facts_fts WHERE rowid=?", (f["id"],))
        self.db.commit()
        return victims

    def wipe(self) -> int:
        n = self.db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.db.execute("DELETE FROM facts")
        if self.fts:
            self.db.execute("DELETE FROM facts_fts")
        self.db.commit()
        self.db.execute("VACUUM")
        return n

    # -- reads -------------------------------------------------------------

    def get(self, fid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
        return dict(r) if r else None

    @staticmethod
    def _visible(f: dict, cwd) -> bool:
        if f["scope"] == "global" or cwd is None:
            return True
        c = norm_project(cwd)
        return c == f["project"] or c.startswith(f["project"].rstrip("/") + "/")

    def list(self, cwd=None, type: str | None = None, pinned_only: bool = False) -> list[dict]:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM facts ORDER BY pinned DESC, updated DESC")]
        return [f for f in rows if self._visible(f, cwd) and (not type or f["type"] == type)
                and (f["pinned"] or not pinned_only)]

    def search(self, query: str, cwd=None, type: str | None = None, limit: int = 8) -> list[dict]:
        terms = list(dict.fromkeys(tokens(query)))
        if not terms:
            return []
        cands = self.list(cwd, type)
        if not cands:
            return []
        if self.fts:
            try:
                q = " OR ".join('"' + t + '"' for t in terms)
                ranked = [r[0] for r in self.db.execute(
                    "SELECT rowid FROM facts_fts WHERE facts_fts MATCH ? ORDER BY bm25(facts_fts)", (q,))]
                byid = {f["id"]: f for f in cands}
                return [byid[i] for i in ranked if i in byid][:limit]
            except sqlite3.OperationalError:
                pass
        return _bm25(cands, terms)[:limit]

    def export(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM facts ORDER BY id")]


def _bm25(facts: list[dict], terms: list[str], k1: float = 1.5, b: float = 0.75) -> list[dict]:
    docs = [tokens(f["text"] + " " + f["type"]) for f in facts]
    n = len(docs)
    avg = (sum(len(d) for d in docs) / n) or 1.0
    df = {t: sum(1 for d in docs if t in d) for t in terms}
    scored = []
    for f, d in zip(facts, docs, strict=False):
        s = 0.0
        for t in terms:
            tf = d.count(t)
            if tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg))
        if s > 0:
            scored.append((s, f))
    return [f for _, f in sorted(scored, key=lambda x: (-x[0], -x[1]["updated"]))]


def format_fact(f: dict) -> str:
    where = "global" if f["scope"] == "global" else "project"
    pin = " pinned" if f["pinned"] else ""
    return f"[{f['id']}] ({f['type']}, {where}{pin}) {f['text']}"
