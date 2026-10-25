"""
A read-only MCP server that gives the agent retrieval over a folder of markdown notes
(an Obsidian-style vault): `vault_search` and `vault_read`. Run by DeepSeek Harness as a
stdio MCP server; see docs/MEMORY.md.

WHY NOT EMBEDDINGS. The sites behind freeharness are slow (15-120 s per model step), so the
cost that matters is the NUMBER OF STEPS, not retrieval quality. Grep + read costs several
steps; one ranked search that returns the right notes, snippets and their [[links]] costs one.
For a vault of hundreds of linked notes, keyword ranking (BM25) plus the link graph finds what
a vector index would, with no model, no dependency, no re-indexing and nothing to go stale.

SECURITY. Read-only; every path is resolved and must stay inside the vault root (no `..`, no
symlink escape); only .md files; output is size-capped. It speaks newline-delimited JSON-RPC
on stdin/stdout, so nothing is written to stdout except protocol messages.

    python -m freeharness.mcp.vault --vault ~/path/to/vault
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".obsidian", "node_modules", ".trash"}
MAX_FILE = 1_000_000
MAX_RESULT_CHARS = 4000
WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
TOKEN = re.compile(r"[a-z0-9_]+")
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


class Note:
    def __init__(self, rel: str, text: str, mtime: float):
        self.rel, self.text, self.mtime = rel, text, mtime
        body = FRONTMATTER.sub("", text, count=1)
        m = re.search(r"^#\s+(.+)$", body, re.M)
        self.title = m.group(1).strip() if m else Path(rel).stem
        self.name = Path(rel).stem
        self.links = [x.strip() for x in WIKILINK.findall(text)]
        self.body_tokens = tokens(body)
        self.head_tokens = tokens(self.title + " " + self.name.replace("-", " "))
        self.lines = body.splitlines()


class Vault:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise SystemExit(f"vault not found: {self.root}")
        self._notes: dict[str, Note] = {}

    # -- indexing: re-read only files whose mtime changed -----------------------------------
    def refresh(self):
        seen = set()
        for p in self.root.rglob("*.md"):
            rel = p.relative_to(self.root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            try:
                real = p.resolve()                       # a symlink must not lead out of the vault
                if real != self.root and self.root not in real.parents:
                    continue
                st = p.stat()
                if st.st_size > MAX_FILE or not p.is_file():
                    continue
                key = rel.as_posix()
                seen.add(key)
                old = self._notes.get(key)
                if old is None or old.mtime != st.st_mtime:
                    self._notes[key] = Note(key, p.read_text(encoding="utf-8", errors="replace"), st.st_mtime)
            except OSError:
                continue
        for gone in set(self._notes) - seen:
            del self._notes[gone]

    def resolve(self, ref: str) -> Note | None:
        """A vault-relative path, or a bare note name as written in [[links]]."""
        self.refresh()
        ref = ref.strip().strip("[]").split("|")[0].split("#")[0]
        if not ref:
            return None
        cand = ref if ref.endswith(".md") else ref + ".md"
        if cand in self._notes:
            return self._notes[cand]
        low = Path(ref).stem.lower()
        hits = [n for n in self._notes.values() if n.name.lower() == low]
        return min(hits, key=lambda n: (n.rel.startswith("raw/"), len(n.rel))) if hits else None

    # -- ranking: BM25 over body with title/name boosted, wiki pages above raw captures -----
    def search(self, query: str, limit: int = 5) -> str:
        self.refresh()
        q = [t for t in dict.fromkeys(tokens(query)) if len(t) > 1]
        if not q or not self._notes:
            return "No query terms." if not q else "The vault has no notes."
        notes = list(self._notes.values())
        n = len(notes)
        avg = sum(len(x.body_tokens) for x in notes) / n or 1
        df = {t: sum(1 for x in notes if t in x.body_tokens or t in x.head_tokens) for t in q}
        scored = []
        for note in notes:
            score = 0.0
            for t in q:
                if not df[t]:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                tf = note.body_tokens.count(t)
                score += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * len(note.body_tokens) / avg))
                if t in note.head_tokens:
                    score += idf * 3.0
            if score <= 0:
                continue
            if note.rel.startswith("wiki/"):
                score *= 1.3
            elif note.rel.startswith("raw/"):
                score *= 0.8
            scored.append((score, note))
        scored.sort(key=lambda s: -s[0])
        if not scored:
            return f"No notes match {query!r}. Try fewer or different words, or read index.md."
        out, size = [], 0
        for rank, (score, note) in enumerate(scored[: max(1, min(limit, 10))], 1):
            qs = set(q)
            lines = [ln.strip() for ln in note.lines if qs & set(tokens(ln))]
            lines.sort(key=lambda ln: -len(qs & set(tokens(ln))))
            snippet = "\n".join("   > " + ln[:220] for ln in lines[:3])
            links = " ".join(f"[[{x}]]" for x in dict.fromkeys(note.links) if x != note.name)[:200]
            block = f"{rank}. {note.rel}  (score {score:.1f})  {note.title}\n{snippet}\n" + (f"   links: {links}\n" if links else "")
            if size + len(block) > MAX_RESULT_CHARS:
                break
            out.append(block)
            size += len(block)
        return "\n".join(out) + "\nRead a note with vault_read(path)."

    def read(self, ref: str, offset: int = 0, max_chars: int = 6000) -> str:
        note = self.resolve(ref)
        if note is None:
            return f"No note {ref!r}. Use vault_search to find the path."
        max_chars = max(200, min(int(max_chars), 20000))
        offset = max(0, int(offset))
        chunk = note.text[offset: offset + max_chars]
        more = len(note.text) - (offset + len(chunk))
        tail = f"\n\n[... {more} more chars: vault_read({note.rel!r}, offset={offset + len(chunk)})]" if more > 0 else ""
        return f"# {note.rel}\n\n{chunk}{tail}"


TOOLS = [
    {"name": "vault_search",
     "description": "Search the user's notes vault (their memory: decisions, project state, preferences). "
                    "Returns the best-matching notes with snippets and their [[links]]. Use this BEFORE answering "
                    "anything about the user's projects, setup or past decisions.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "keywords, e.g. 'qwen eval result'"},
         "limit": {"type": "integer", "description": "notes to return, 1-10 (default 5)"}},
         "required": ["query"]}},
    {"name": "vault_read",
     "description": "Read one note by vault-relative path or by its [[link]] name. Long notes are paged with offset.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}},
         "required": ["path"]}},
]


def handle(vault: Vault, msg: dict) -> dict | None:
    """One JSON-RPC message in, the response out (None for notifications)."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None
    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "freeharness-vault", "version": "0.1.0"},
                   "instructions": "Read-only retrieval over the user's notes vault. Search first, then read the best note."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            if name == "vault_search":
                text = vault.search(str(args.get("query", "")), int(args.get("limit", 5)))
            elif name == "vault_read":
                text = vault.read(str(args.get("path", "")), int(args.get("offset", 0)), int(args.get("max_chars", 6000)))
            else:
                return err(-32602, f"unknown tool {name!r}")
        except (ValueError, TypeError) as e:
            return ok({"content": [{"type": "text", "text": f"bad arguments: {e}"}], "isError": True})
        return ok({"content": [{"type": "text", "text": text}]})
    return err(-32601, f"method not found: {method}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", required=True, help="the notes folder (read-only)")
    args = ap.parse_args(argv)
    vault = Vault(Path(args.vault).expanduser())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(vault, msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
