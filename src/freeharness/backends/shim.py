"""
shim — an OpenAI-compatible /v1/chat/completions endpoint backed by free chat
sites driven through bridge.Session. Point an agent harness (DeepSeek Harness's
`llm-pi-ai` custom provider, or anything that speaks Chat Completions) at it.

    freeharness serve                       # gemini then qwen, headless
    freeharness serve --providers gemini,qwen
    python -m freeharness.backends.shim check   # does each site answer right now?

WHY THIS EXISTS. A chat site returns text; a harness wants structured
`tool_calls`. So each request is rendered into a prompt (tool list, a reply
protocol, the conversation), sent to a chat, and the reply is parsed back into
`tool_calls`. The harness's own log is the source of truth, which is what makes
switching sites mid-task safe. By default ONE chat is kept per task and only the
new messages are sent (Router, "sticky"); a new chat with a compact history is
started only on the site's own "full" signals, a history mismatch or an error.
Prompt-size limits are never assumed: they are learned from the site (limits.py).

TOOL-CALL PROTOCOL. The model is told to emit each call as a fenced code block
holding one JSON object. Code, not prose, because chat UIs render markdown:
measured on Gemini, `**bold**` inside rendered text comes back as `bold`, which
would silently corrupt a tool argument. A code block's textContent is exact
(bridge.Session.code_blocks). The text fallback exists for sites that flatten it.

FAILOVER. `model: "auto"` walks the providers in order; one that hits a quota,
captcha, login wall or timeout is put on cooldown and the next is tried. That
only happens before any output has gone to the client. `model: "<key>"` pins one.
Binds to loopback only and requires a bearer token (see auth.py): it proxies logged-in sessions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import select
import signal
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .. import auth, paths
from .limits import NO_LIMIT, Limits
from .limits import default_path as limits_path
from .providers import ELIGIBLE, PROVIDERS, Provider, resolve

MAX_BODY = 8_000_000
ORPHAN_SECONDS = 1800      # how long a dropped request's chat stays adoptable
PING_SECONDS = 15          # a request still waiting for its reply gets its HTTP headers and an SSE comment this often
COOL_WAIT = 300            # a cooldown this short (seconds) is waited out in place when the request has a chat to resume

# ------------------------------------------------------------------ prompting

PROTOCOL = """\
You are the reasoning engine of a coding agent. A harness runs tools for you and
shows you the results; you cannot run anything yourself.

# How to reply
Either answer in plain text, or call tools.
To call a tool, reply with ONLY fenced code blocks tagged `tool_call`, one block
per call, each holding a single JSON object:

```tool_call
{"name": "<tool name>", "arguments": {"<argument>": <value>}}
```

Rules: use the exact tool and argument names listed below. Put nothing before,
between or after the blocks. Never write a tool's result yourself and never say
you ran something you did not call a tool for. When you have the answer, reply
in plain text with no blocks. After your calls, the results come back and you
are asked again."""

_SENTENCE = re.compile(r"(?<=[.!?])\s")
TOOL_DESC_CAP = 400      # chars of description per tool
PARAM_DESC_CAP = 130     # chars of description per argument
# dsh's sandbox-escalation retry arguments: optional, and a web model never needs them
SKIP_ARGS = {"sandbox_permissions", "justification"}


def _short(text: str, cap: int) -> str:
    text = " ".join((text or "").split())
    first = _SENTENCE.split(text, 1)[0]
    out = first if len(first) <= cap else first[: cap - 1] + "…"
    return out


def _brief(text: str, cap: int) -> str:
    """Whole sentences, in order, while they fit in `cap` chars; the first is always kept
    (clipped with an ellipsis if it alone is too long). Keeps the rules that matter
    ("old_string must appear exactly once", "output is truncated to its tail") in the prompt."""
    text = " ".join((text or "").split())
    out = ""
    for s in _SENTENCE.split(text):
        cand = f"{out} {s}" if out else s
        if len(cand) > cap:
            return out or (s[: cap - 1] + "…")
        out = cand
    return out


def _type_of(schema: dict) -> str:
    if not isinstance(schema, dict):
        return "any"
    if "enum" in schema:
        return "|".join(json.dumps(v) for v in schema["enum"])
    t = schema.get("type", "any")
    if isinstance(t, list):
        t = "|".join(map(str, t))
    if t == "array":
        return f"array<{_type_of(schema.get('items', {}))}>"
    return str(t)


def render_tool(tool: dict) -> str:
    fn = tool.get("function", tool)
    params = fn.get("parameters") or {}
    required = set(params.get("required") or [])
    lines = [f"- {fn['name']}: {_brief(fn.get('description', ''), TOOL_DESC_CAP)}"]
    for name, schema in (params.get("properties") or {}).items():
        if name in SKIP_ARGS and name not in required:
            continue
        req = "required" if name in required else "optional"
        desc = _brief(schema.get("description", ""), PARAM_DESC_CAP) if isinstance(schema, dict) else ""
        lines.append(f"    {name} ({_type_of(schema)}, {req})" + (f": {desc}" if desc else ""))
    return "\n".join(lines)


def _text(content) -> str:
    """Message content is a string, or a list of typed parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") in (None, "text", "input_text"))


def render_call(call: dict) -> str:
    fn = call["function"]
    args = fn.get("arguments") or "{}"
    try:
        args = json.loads(args) if isinstance(args, str) else args
    except json.JSONDecodeError:
        pass
    body = json.dumps({"name": fn["name"], "arguments": args}, ensure_ascii=False)
    return f"```tool_call\n{body}\n```"


def render_prompt(messages: list[dict], tools: list[dict] | None) -> str:
    system = "\n\n".join(_text(m.get("content")) for m in messages
                         if m.get("role") in ("system", "developer")).strip()
    parts = [PROTOCOL if tools else
             "You are the reasoning engine of a coding agent. Reply in plain text."]
    if tools:
        parts.append("# Tools\n" + "\n".join(render_tool(t) for t in tools))
    if system:
        parts.append("# Instructions from the harness\n" + system)

    names = {}                                   # tool_call id -> tool name
    convo = []
    for m in messages:
        role = m.get("role")
        if role in ("system", "developer"):
            continue
        if role == "assistant":
            bits = [_text(m.get("content")).strip()]
            for c in m.get("tool_calls") or []:
                names[c["id"]] = c["function"]["name"]
                bits.append(render_call(c))
            convo.append("[assistant]\n" + "\n".join(b for b in bits if b))
        elif role == "tool":
            nm = names.get(m.get("tool_call_id"), "tool")
            convo.append(f"[tool_result {nm}]\n{_text(m.get('content')).strip()}")
        else:
            convo.append(f"[user]\n{_text(m.get('content')).strip()}")
    parts.append("# Conversation\n" + "\n\n".join(convo))
    parts.append("Reply now as the assistant, answering the MOST RECENT [user] message "
                 "(earlier requests in the conversation are context, not new work). "
                 + ("The tools listed above are real and you may use them: if the task still "
                    "needs a file read, a command or an edit, call the tool with a `tool_call` "
                    "block instead of saying you cannot. Otherwise answer in plain text."
                    if tools else "Answer in plain text."))
    return "\n\n".join(parts)


# --- trimming a history for a FRESH chat (see Router: only used when a chat must be (re)started)
TRIM_HEAD = 400            # chars kept from the start of an older tool result ...
TRIM_TAIL = 200            # ... and from its end; the middle becomes a marker with the original length
PROTECT_RESULTS = 5        # the newest tool results are kept verbatim ...
PROTECT_CAP = 40_000       # ... up to this many chars each (a bigger one keeps its first 27k and last 13k)
OMIT_TAG = "chars omitted ("            # appears in every shortened result (idempotence, tests)
DROPPED = "[earlier conversation omitted to fit the provider's prompt limit]"


def _shorten(text: str, head: int, tail: int) -> str:
    """`text` with its middle replaced by a marker that states how much was cut and how long it was.
    Returned unchanged when shortening would not make it smaller."""
    t = text.strip()
    if OMIT_TAG in t[head:len(t) - tail]:
        return text
    omitted = len(t) - head - tail
    note = f"\n[… {omitted:,} chars omitted (of {len(t):,}); re-run the command if you need it]\n"
    if omitted <= len(note) + 50:
        return text
    return t[:head] + note + t[len(t) - tail:]


def compact_messages(messages: list[dict]) -> list[dict]:
    """The history a FRESH chat is sent, whatever the provider's limits: every tool result except the newest
    PROTECT_RESULTS (and any after the last assistant message) keeps its first TRIM_HEAD and last TRIM_TAIL chars
    with an "N chars omitted (of M)" marker; the newest stay verbatim up to PROTECT_CAP chars each. Nothing is
    dropped and nothing else is touched. Returns `messages` itself when nothing needed shortening."""
    last_asst = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
    results = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    protected = {i for i in results if i > last_asst} | set(results[-PROTECT_RESULTS:])
    out, changed = list(messages), False
    for i in results:
        old = _text(messages[i].get("content"))
        if i in protected:
            new = _shorten(old, PROTECT_CAP * 2 // 3, PROTECT_CAP // 3) if len(old.strip()) > PROTECT_CAP else old
        else:
            new = _shorten(old, TRIM_HEAD, TRIM_TAIL)
        if new != old:
            out[i] = {**messages[i], "content": new}
            changed = True
    return out if changed else messages


DIGEST_TARGET = int(os.environ.get("FREEHARNESS_DIGEST_TARGET", "48000"))   # a fresh chat's history is digested above this
FRESH_KEEP = 40            # ... keeping the newest messages verbatim
DIGEST_LINE = 170          # chars per digested step
DIGEST_MAX = 18_000        # chars for the whole digest (older steps are only counted beyond this)


def _digest_line(m: dict, names: dict) -> str:
    role = m.get("role")
    if role == "assistant":
        calls = [f"{c['function']['name']} {_one_line(c['function'].get('arguments'), 90)}" for c in m.get("tool_calls") or []]
        return "assistant: " + ("; ".join(calls) or _one_line(_text(m.get("content")), DIGEST_LINE))
    if role == "tool":
        return "  -> " + _one_line(_text(m.get("content")), 100)
    return "user: " + _one_line(_text(m.get("content")), DIGEST_LINE)


def _one_line(value, n: int) -> str:
    t = " ".join((value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).split())
    return t if len(t) <= n else t[:n - 1] + "…"


def digest_history(messages: list[dict], tools: list[dict] | None, target: int | None = None) -> list[dict]:
    """A FRESH chat cannot take hundreds of steps: when the compacted history is still over `target` chars, every
    message between the first user message and the newest FRESH_KEEP is replaced by ONE user message listing what
    was done (a line per call and result). Nothing in the newest messages changes. Returns `messages` itself when
    it already fits or is too short to digest."""
    if len(render_prompt(messages, tools)) <= (DIGEST_TARGET if target is None else target):
        return messages
    first = next((i for i, m in enumerate(messages) if m.get("role") == "user"), -1)
    cut = len(messages) - FRESH_KEEP
    while cut > first + 1 and messages[cut].get("role") == "tool":       # never start the kept part on an orphan result
        cut -= 1
    if first < 0 or cut <= first + 1:
        return messages
    lines = [_digest_line(m, {}) for m in messages[first + 1:cut] if m.get("role") in ("assistant", "tool", "user")]
    omitted = 0
    while sum(len(x) + 1 for x in lines) > DIGEST_MAX and len(lines) > 2:
        lines.pop(0)
        omitted += 1
    head = ("[Earlier steps of this session, summarized by the harness because this is a new chat; the results are "
            "cut short, so re-run a command if you need its full output."
            + (f" {omitted} older lines are left out." if omitted else "") + "]\n")
    kept = [m for i, m in enumerate(messages) if i <= first or i >= cut or m.get("role") in ("system", "developer")]
    pos = next(i for i, m in enumerate(kept) if m is messages[first])
    kept.insert(pos + 1, {"role": "user", "content": head + "\n".join(lines)})
    return kept


def fit_messages(messages: list[dict], tools: list[dict] | None, budget: int | None) -> list[dict]:
    """Shrink an over-long history so render_prompt() fits `budget` chars (None = no limit).

    Never touches system/developer messages or the latest user message. The order is a promise to
    the model that it does not lose what it just read:
      1. OLDER tool results (not the newest PROTECT_RESULTS, not any after the last assistant message)
         keep their first TRIM_HEAD and last TRIM_TAIL chars; the middle becomes
         "[… N chars omitted (of M); re-run the command if you need it]". Oldest first, stopping as
         soon as it fits.
      2. Only if that is not enough: a protected result bigger than PROTECT_CAP is cut the same way.
      3. Only if that is not enough: the oldest WHOLE messages go (one marker is left behind).
    Returns `messages` itself when it already fits; may still be too large (the router then reports
    context_length_exceeded so the harness can condense)."""
    if budget is None:
        return messages
    size = len(render_prompt(messages, tools))
    if size <= budget:
        return messages
    ms = [dict(m) for m in messages]
    last_user = max((i for i, m in enumerate(ms) if m.get("role") == "user"), default=-1)
    last_asst = max((i for i, m in enumerate(ms) if m.get("role") == "assistant"), default=-1)
    results = [i for i, m in enumerate(ms) if m.get("role") == "tool"]
    protected = {i for i in results if i > last_asst} | set(results[-PROTECT_RESULTS:])

    def cut(i, head, tail):
        nonlocal size
        old = _text(ms[i].get("content"))
        new = _shorten(old, head, tail)
        if new != old:
            size += len(new.strip()) - len(old.strip())
            ms[i]["content"] = new

    for i in results:
        if i not in protected:
            cut(i, TRIM_HEAD, TRIM_TAIL)
            if size <= budget:
                return ms
    for i in results:
        if i in protected and len(_text(ms[i].get("content")).strip()) > PROTECT_CAP:
            cut(i, PROTECT_CAP * 2 // 3, PROTECT_CAP // 3)
            if size <= budget:
                return ms
    if last_user > 0:
        keep = [m for m in ms[:last_user] if m.get("role") in ("system", "developer")]
        rest = [m for m in ms[:last_user] if m.get("role") not in ("system", "developer")]
        tail = ms[last_user:]

        def build(k):
            return keep + [{"role": "user", "content": DROPPED}] + rest[k:] + tail
        lo, hi = 1, len(rest)               # smallest number of oldest messages to drop that fits
        if rest and len(render_prompt(build(hi), tools)) > budget:
            return build(hi)
        while lo < hi:
            mid = (lo + hi) // 2
            if len(render_prompt(build(mid), tools)) <= budget:
                hi = mid
            else:
                lo = mid + 1
        return build(lo) if rest else ms
    return ms


# ------------------------------------------------------- sticky threads (see Router)

# Safety nets only, NOT the normal way a chat ends: the normal ways are the site's own "full" notice, a
# history that no longer matches (compaction / edit), a changed page or session, or an error.
STICKY_MAX_STEPS = 400         # a chat that has taken this many requests is replaced by a fresh one
MAX_NEW_CHATS_PER_HOUR = int(os.environ.get("FREEHARNESS_MAX_NEW_CHATS_PER_HOUR", "6"))   # per provider; 0 = no cap
STICKY_MAX_CHARS = 600_000     # ... or has been sent this many characters in total
FRESH_TARGET = 0.5             # a fresh chat's prompt aims at this fraction of the limit (when one is known)
FULL_REPLY_MAX = 1_500         # a "chat is full" notice is short; a longer reply is an answer
LENGTH_WORDS = ("too long", "context length", "context window", "maximum context", "exceeds the maximum",
                "token limit", "length limit", "too large", "prompt is too", "message is too")


def _norm_calls(calls) -> list:
    out = []
    for c in calls or []:
        fn = c.get("function", {})
        args = fn.get("arguments") or "{}"
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except json.JSONDecodeError:
            pass
        out.append([fn.get("name"), json.dumps(args, sort_keys=True, ensure_ascii=False)])
    return out


def reply_fingerprint(content, calls) -> str:
    """What the site was answered with, in a form the harness's recorded copy reproduces."""
    blob = json.dumps([(content or "").strip(), _norm_calls(calls)], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def history_fingerprint(messages: list[dict], tools: list[dict] | None) -> str:
    """Identity of a conversation prefix. Tools are included: a changed tool list invalidates it."""
    norm = [[m.get("role"), _text(m.get("content")).strip(), _norm_calls(m.get("tool_calls")),
             m.get("tool_call_id")] for m in messages]
    names = sorted(t.get("function", t).get("name", "") for t in tools or [])
    blob = json.dumps([norm, names], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def render_delta(new_messages: list[dict], tools: list[dict] | None, names: dict) -> str:
    """Only the messages the site's chat has not seen, plus a one-line protocol reminder
    (models drift from the tool-call format over a long chat; the full rules were sent once)."""
    convo = []
    for m in new_messages:
        role = m.get("role")
        if role == "tool":
            nm = names.get(m.get("tool_call_id"), "tool")
            convo.append(f"[tool_result {nm}]\n{_text(m.get('content')).strip()}")
        elif role == "user":
            convo.append(f"[user]\n{_text(m.get('content')).strip()}")
    remind = ("Continue as the assistant, answering the MOST RECENT message. If the task still needs a file read, "
              "a command or an edit, reply with ONLY fenced `tool_call` blocks (one JSON object each, exactly as "
              "described at the start of this chat, nothing around them); otherwise answer in plain text."
              if tools else "Continue as the assistant and answer in plain text.")
    return "# New messages\n" + "\n\n".join(convo) + "\n\n" + remind


def framed(prov, text: str, fresh: bool, tools) -> str:
    """What is typed into the site: `text` with the provider's own note on where the tools live (only when tools are
    offered and the provider has one). The router's bookkeeping keeps using `text`, so the sticky prefix is unchanged."""
    note = (prov.tool_preamble if fresh else prov.tool_reminder) if tools else ""
    return f"{note}\n\n{text}" if note else text


def tool_names(messages: list[dict]) -> dict:
    return {c["id"]: c["function"]["name"] for m in messages if m.get("role") == "assistant"
            for c in m.get("tool_calls") or []}


# -------------------------------------------------------------------- parsing

def _pairs(pairs):
    """object_pairs_hook: a repeated key keeps its FIRST value; later ones are kept as "_dup_<key>". Models that
    write call arguments beside the tool name collide on "name" (the tool, then a branch/skill name)."""
    out = {}
    for k, v in pairs:
        if k in out:
            out["_dup_" + k] = v
        else:
            out[k] = v
    return out


def _json_objects(text: str):
    """Yield every balanced top-level {...} in text that parses as JSON."""
    i = 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return
        depth, in_str, esc = 0, False, False
        for j in range(i, len(text)):
            ch = text[j]
            if in_str:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        yield json.loads(text[i:j + 1], strict=False, object_pairs_hook=_pairs), j + 1   # raw newlines inside strings are common
                    except json.JSONDecodeError:
                        pass
                    i = j + 1
                    break
        else:
            i += 1          # unbalanced (a truncated copy): later copies may be whole


def _as_call(obj, names: dict):
    """A {"name", "arguments"} object naming a real tool -> (name, args), else None. Models also write the tool under
    "tool", "tool_name" or "function" (ChatGPT: {"tool": "read", "file_path": ...}); that counts when it names a real tool."""
    if isinstance(obj, dict) and "name" not in obj:
        for alias in ("tool", "tool_name", "function"):
            if isinstance(obj.get(alias), str) and obj[alias].lower() in names:
                obj = {"name": obj[alias], **{k: v for k, v in obj.items() if k != alias}}
                break
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    name = names.get(str(obj["name"]).lower())
    if not name:
        return None
    if any(k in obj for k in ("arguments", "parameters", "input")):
        args = obj.get("arguments", obj.get("parameters", obj.get("input", {})))
    else:
        # {"name": "bash"} alone is a mention (a list of tools), never a call with empty arguments; but
        # {"name": "gh_clone", "repo": "x"} is a call whose arguments sit beside the name (a common slip).
        args = {k: v for k, v in obj.items() if k not in ("name", "type", "id")}
        for k in [k for k in args if k.startswith("_dup_")]:        # the colliding second "name", "id", ...
            args[k[5:]] = args.pop(k)
        if not args:
            return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    return (name, args) if isinstance(args, dict) else None


_DSML_INVOKE = re.compile(r'invoke\s+name="([^"]+)"\s*>')
_DSML_PARAM = re.compile(r'parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?\s*>(.*?)(?=<[^<>]*DSML|\Z)', re.S)


def _dsml_calls(text: str, names: dict):
    """DeepSeek writes its OWN call markup when it ignores ours: `<｜DSML｜invoke name="bash">
    <｜DSML｜parameter name="arguments" string="false">{json}</｜DSML｜parameter>...` (the bar and spacing vary
    with how the page renders it). Returns [(name, args)] for tools that exist."""
    if "DSML" not in text:
        return []
    out = []
    heads = list(_DSML_INVOKE.finditer(text))
    for k, m in enumerate(heads):
        name = names.get(m.group(1).lower())
        if not name:
            continue
        end = heads[k + 1].start() if k + 1 < len(heads) else len(text)
        args = {}
        for pm in _DSML_PARAM.finditer(text[m.end():end]):
            key, is_str, raw = pm.group(1), pm.group(2), pm.group(3).strip()
            raw = re.sub(r"<[^<>]*DSML[^<>]*>\s*$", "", raw).strip()
            if key == "arguments":
                try:
                    val = json.loads(raw, strict=False)
                except json.JSONDecodeError:
                    continue
                if isinstance(val, dict):
                    args.update(val)
                continue
            if is_str == "false":
                try:
                    raw = json.loads(raw, strict=False)
                except json.JSONDecodeError:
                    pass
            args[key] = raw
        out.append((name, args))
    return out


def parse_reply(text: str, code_blocks: list[str], tools: list[dict] | None):
    """(content, tool_calls). Calls come from code blocks first (exact text),
    then from JSON found in the flattened text. Unknown tool names are NOT calls:
    a model explaining some JSON must not trigger a tool."""
    if not tools:
        return text.strip(), []
    names = {t.get("function", t)["name"].lower(): t.get("function", t)["name"] for t in tools}
    # Qwen's code viewer swaps every space for U+00A0, which json rejects, and
    # renders the block twice (plain + nbsp copy). Plain spaces are what the model wrote.
    text = text.replace("\xa0", " ")
    code_blocks = [b.replace("\xa0", " ") for b in code_blocks]

    calls = []
    for block in code_blocks:
        for obj, _ in _json_objects(block):
            c = _as_call(obj, names)
            if c:
                calls.append(c)
    if not calls:
        for obj, _ in _json_objects(text):
            c = _as_call(obj, names)
            if c:
                calls.append(c)
    if not calls:
        calls.extend(_dsml_calls(text, names))
    if not calls:
        # A fenced block in the exact call shape that names a tool we do not have (a model calling a
        # SKILL name as a tool, say) is a failed attempt, not prose: hand it to dsh, whose "Unknown
        # tool" error result lets the model correct itself instead of the turn ending silently.
        for block in code_blocks:
            for obj, _ in _json_objects(block):
                if (isinstance(obj, dict) and isinstance(obj.get("name"), str) and obj["name"].strip()
                        and isinstance(obj.get("arguments"), dict) and set(obj) <= {"name", "arguments"}):
                    calls.append((obj["name"].strip(), obj["arguments"]))
    seen, unique = set(), []
    for n, a in calls:                   # the same call shown twice is ONE call
        k = (n, json.dumps(a, sort_keys=True))
        if k not in seen:
            seen.add(k)
            unique.append((n, a))
    calls = unique
    if calls:
        return "", [{"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                     "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                    for n, a in calls]
    return text.strip(), []


# --------------------------------------------------------------------- router

class AllCoolingDown(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"every provider is cooling down; next one frees up in {retry_after:.0f}s")
        self.retry_after = retry_after


class PromptTooLarge(Exception):
    pass


class ProvidersFailed(Exception):
    pass


# seconds a provider sits out after each kind of failure
COOLDOWN = {"quota": 900, "login": 1800, "captcha": 1800, "broken": 1800, "timeout": 120, "error": 120}


def classify(exc: Exception) -> str:
    msg = str(exc).lower()
    if isinstance(exc, LookupError):
        return "broken"                   # selector no longer matches: the UI changed
    if "challeng" in msg or "captcha" in msg or "verification" in msg:
        return "captcha"
    if "sign up" in msg or "sign in" in msg or "log in" in msg:
        return "login"
    if any(k in msg for k in ("limit", "too many", "quota", "rate")):
        return "quota"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "error"


def code_fingerprint() -> str:
    """Hash of the source files that decide what the shim does, so a client can tell a stale shim
    (started before an upgrade or fix) from the current code."""
    h = hashlib.sha256()
    here = Path(__file__).parent
    for name in ("shim.py", "providers.py", "bridge.py"):
        try:
            h.update((here / name).read_bytes())
        except OSError:
            h.update(name.encode())
    return h.hexdigest()[:16]


def parse_fault(spec):
    """FREEHARNESS_FAULT="gemini:3" makes gemini's 3rd request fail with a quota error, so failover
    can be exercised live without waiting for a real limit. Unset in normal use."""
    out = {}
    for part in (spec or "").split(","):
        key, _, n = part.partition(":")
        if key.strip() and n.strip().isdigit():
            out[key.strip()] = int(n)
    return out


class SiteFull(RuntimeError):
    """The site answered with its own "this chat is full / the message is too long" notice."""


class Router:
    """Owns one lazily-created Session per provider and the cooldown ledger.

    Chat policy (see docs/MEMORY.md): with `sticky`, ONE chat per task is kept and only the new messages
    are sent. A chat ends only when the SITE says it is full (provider.full_markers on the page or the
    reply, or a length/context error), the history no longer matches what the chat has seen, the page or
    session object changed, an error happened, or a safety net (STICKY_MAX_*) trips. The next request then
    starts a NEW chat whose prompt is the compact history (fit_messages). Prompt-size limits are never
    assumed: they are learned from failures (limits.Limits)."""

    def __init__(self, keys, make_session, clock=time.monotonic, log=None, sticky=(), limits=None,
                 debug_prompts=None, chats_path=None):
        self.keys = list(keys)
        self.make_session = make_session      # (Provider) -> Session-like
        self.clock = clock
        self.log = log or (lambda m: sys.stderr.write(m + "\n"))
        self.sessions: dict[str, object] = {}
        self.until: dict[str, float] = {}
        self.why: dict[str, str] = {}
        # Providers allowed to continue ONE chat across requests (send only what is new).
        self.sticky = set(sticky)
        self.threads: dict[str, dict] = {}     # provider -> what its current chat has seen
        self.wait = time.sleep                 # injectable: tests advance a fake clock instead
        self.orphans: dict[str, dict] = {}     # provider -> a request the client dropped while the site was still answering
        self.limits = limits if limits is not None else Limits()
        self.fault = parse_fault(os.environ.get("FREEHARNESS_FAULT"))   # test hook: {provider: Nth request fails}
        self.requests: dict[str, int] = {}
        self.new_chats: dict[str, list[float]] = {}     # when each provider's chats were started (the last hour)
        self.chats_path = chats_path                    # if set, that count survives a shim restart (wall-clock times)
        self.wall = time.time
        self.debug_prompts = debug_prompts if debug_prompts is not None else os.environ.get("SHIM_DEBUG_PROMPTS")
        self._debug_last: list[str] = []

    def status(self) -> dict:
        now = self.clock()
        return {k: {"ready": self.until.get(k, 0) <= now,
                    "cooldown_s": max(0, round(self.until.get(k, 0) - now)),
                    "reason": self.why.get(k, ""),
                    "sticky": k in self.sticky,
                    "limits": self.limits.snapshot(k)} for k in self.keys}

    def _session(self, key):
        if key not in self.sessions:
            self.sessions[key] = self.make_session(PROVIDERS[key])
        return self.sessions[key]

    def _cool(self, key, kind, exc):
        self.threads.pop(key, None)
        self.until[key] = self.clock() + COOLDOWN[kind]
        self.why[key] = f"{kind}: {str(exc)[:120]}"
        # a wedged browser must not be reused after a hard failure
        if kind in ("captcha", "broken", "error", "timeout"):
            sess = self.sessions.pop(key, None)
            if sess is not None:
                try:
                    sess.close()
                except Exception:        # noqa: BLE001, S110  best-effort cleanup
                    pass

    def candidates(self, model: str) -> list[str]:
        if model in ("auto", "", None):
            return self.keys
        if model in self.keys:
            return [model]
        raise KeyError(model)

    # -- sizes: learned, never assumed ----------------------------------------

    def hard_limit(self, key) -> int:
        """Largest message this provider is believed to take: its confirmed learned ceiling, else the
        (huge) safety value. A prompt above it cannot be sent to this provider."""
        c = self.limits.ceiling(key)
        mp = PROVIDERS[key].max_prompt
        return min(mp, c) if c else mp

    def fresh_budget(self, key) -> int | None:
        """Chars a FRESH chat's prompt should aim at, or None (= send the whole history)."""
        c = self.limits.ceiling(key)
        if c:
            return int(c * FRESH_TARGET)
        probe = self.limits.probe_budget(key)       # failed once, not yet confirmed: retry smaller
        if probe:
            return probe
        mp = PROVIDERS[key].max_prompt
        return int(mp * FRESH_TARGET) if mp < NO_LIMIT else None

    @staticmethod
    def _length_looking(prov: Provider, exc) -> bool:
        if isinstance(exc, SiteFull):
            return True
        msg = str(exc).lower()
        return any(m in msg for m in (*prov.full_markers, *prov.size_errors, *LENGTH_WORDS))

    @staticmethod
    def _only_generic_error(prov: Provider, exc) -> bool:
        """The error matches only the site's vague size-looking message ("I encountered an error..."), never an explicit
        "conversation is too long / start a new chat" wording: worth one retry in the same chat before leaving it."""
        if isinstance(exc, SiteFull):
            return False
        msg = str(exc).lower()
        if any(m in msg for m in (*prov.full_markers, *LENGTH_WORDS)):
            return False
        return any(m in msg for m in prov.size_errors)

    @staticmethod
    def _reply_says_full(prov: Provider, text: str, blocks) -> bool:
        t = (text or "").strip()
        if not t or len(t) > FULL_REPLY_MAX or blocks:
            return False
        low = t.lower()
        return any(m in low for m in prov.full_markers)

    @staticmethod
    def _page_full(sess) -> bool:
        probe = getattr(sess, "chat_full", None)
        if not probe:
            return False
        try:
            return bool(probe())
        except Exception:                # noqa: BLE001  a flaky probe must not fail a good answer
            return False

    # -- sticky planning ------------------------------------------------------

    def _plan(self, key, sess, fresh_prompt, messages, tools, force_fresh=False):
        """(text to send, fresh?, request fingerprint) for one provider.

        Continue the provider's existing chat only if EVERYTHING still lines up: same session
        object, same page, the history we sent is an unchanged prefix of the new request, the next
        message is the very reply we returned, and no safety net tripped. Otherwise a fresh chat gets
        `fresh_prompt()` (the compact history), which is exactly the non-sticky behaviour."""
        if key not in self.sticky or messages is None:
            return fresh_prompt(), True, None
        th = self.threads.get(key)
        if force_fresh:
            self.orphans.pop(key, None)
            return fresh_prompt(), True, {"fresh": True}
        orp = self.orphans.pop(key, None)
        if orp:
            # The client dropped this very request (dsh gave up waiting) while the site kept answering in the chat. If the
            # same request is back and the page is still on that chat, take over the answer instead of opening a new chat.
            same = (orp["session"] is sess and self.clock() - orp["t"] < ORPHAN_SECONDS
                    and history_fingerprint(messages, tools) == orp["fp"]
                    and getattr(sess, "current_url", lambda: None)() == orp["url"])
            if same and hasattr(sess, "adopt") and sess.adoptable(orp["before"]):
                return "", False, {"adopt": orp}
            if same and orp["th"] is not None:
                th = self.threads[key] = orp["th"]          # nothing arrived on the page: say it again in the SAME chat
                self.log(f"shim: {key} resuming the previous chat; the dropped message left no answer, sending it again")
        url = getattr(sess, "current_url", lambda: None)()
        k = th["k"] if th else 0
        checks = [
            ("no chat yet", lambda: bool(th)),
            ("session object changed", lambda: th["session"] is sess),
            ("no reply fingerprint", lambda: bool(th["reply_fp"])),
            ("page address changed", lambda: bool(url) and url == th["url"]),
            ("step cap", lambda: th["steps"] < STICKY_MAX_STEPS),
            ("char cap", lambda: th["chars"] < STICKY_MAX_CHARS),
            ("no new messages", lambda: len(messages) > k + 1),
            ("next message is not our reply", lambda: messages[k].get("role") == "assistant"),
            ("unseen assistant message", lambda: all(m.get("role") in ("tool", "user") for m in messages[k + 1:])),
            ("our reply was edited", lambda: reply_fingerprint(
                _text(messages[k].get("content")), messages[k].get("tool_calls")) == th["reply_fp"]),
            ("history/tools changed", lambda: history_fingerprint(messages[:k], tools) == th["prefix_fp"]),
        ]
        why = next((name for name, test in checks if not test()), None)
        delta = ""
        if why is None:
            delta = render_delta(messages[k + 1:], tools, tool_names(messages[:k + 1]))
            if len(delta) > self.hard_limit(key) * 0.9:
                why = "new messages bigger than the provider's limit"
        if why is not None:
            if th:
                self.log(f"shim: {key} chat not continued ({why}); starting a new one with a compact history")
            return fresh_prompt(), True, {"fresh": True}
        return delta, False, {"fresh": False, "steps": th["steps"], "chars": th["chars"]}

    def _orphan(self, key, sess, messages, tools, send_chars, plan, th_before, orp):
        """The client dropped a sticky request after the message went in: remember the chat so the same request,
        when it comes back, takes over the answer on the page instead of starting a new chat."""
        before = orp["before"] if orp else getattr(sess, "last_before", 0)
        self.orphans[key] = {"session": sess, "url": getattr(sess, "current_url", lambda: None)(), "before": before,
                             "fp": history_fingerprint(messages, tools), "send_chars": send_chars, "plan": plan,
                             "th": th_before, "t": self.clock()}
        self.log(f"shim: {key} request dropped by the client; its chat is kept so a retry resumes it (no new chat)")

    def _remember(self, key, sess, messages, tools, sent_chars, plan):
        """Record what this chat has seen (the reply fingerprint arrives later via note_reply)."""
        carried = plan if plan and not plan["fresh"] else {}     # counts of the chat being continued
        self.threads[key] = {
            "session": sess, "url": getattr(sess, "current_url", lambda: None)(), "k": len(messages),
            "prefix_fp": history_fingerprint(messages, tools), "reply_fp": None,
            "steps": carried.get("steps", 0) + 1, "chars": carried.get("chars", 0) + sent_chars}

    def note_reply(self, key, content, calls):
        """Called once the reply is parsed; lets the NEXT request prove it follows this one."""
        th = self.threads.get(key)
        if th:
            th["reply_fp"] = reply_fingerprint(content, calls)

    def _debug_prompt(self, key, fresh, send):
        """SHIM_DEBUG_PROMPTS=<file>: <file> holds the FULL text of the last 3 prompts sent (rewritten each
        time, owner-only); <file>.index gets one summary line per prompt (provider, fresh/sticky, size,
        first and last 300 chars). Prompts contain whatever the agent read; the token is never part of
        a prompt and is never written."""
        path = self.debug_prompts
        if not path:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        mode = "fresh" if fresh else "sticky"
        self._debug_last = (self._debug_last + [
            f"===== {stamp} provider={key} mode={mode} chars={len(send)} =====\n{send}\n"])[-3:]
        try:
            p = Path(path)
            tmp = p.with_name(p.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("".join(self._debug_last))
            os.replace(tmp, p)
            idx = p.with_name(p.name + ".index")
            fd = os.open(idx, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps({"time": stamp, "provider": key, "mode": mode, "chars": len(send),
                                    "first": send[:300], "last": send[-300:]}) + "\n")
        except OSError as e:
            self.log(f"shim: cannot write SHIM_DEBUG_PROMPTS ({e})")

    def run(self, model: str, prompt: str, should_stop=None, *, messages=None, tools=None):
        """Generator of ("provider", key) once, then ("t", text) events, then
        ("done", code_blocks). Fails over only until the first text is yielded.
        `should_stop()` lets the caller abandon the generation (client left).

        `prompt` is the whole history rendered (used as is when `messages` is None or the provider has no
        known limit); with `messages` the router renders a compact history itself whenever a fresh chat is
        needed, so the harness's own messages are never pre-trimmed (that would break the sticky prefix)."""
        keys = self.candidates(model)
        now = self.clock()
        cache: dict = {}

        def fresh_for(key):
            if messages is None:
                return prompt
            budget = self.fresh_budget(key)           # None until a limit has been learned from the site
            if budget not in cache:
                compact = digest_history(compact_messages(messages), tools)   # every fresh chat gets a bounded history
                cache[budget] = render_prompt(fit_messages(compact, tools, budget), tools)
            return cache[budget]

        fits = [k for k in keys if len(fresh_for(k)) <= self.hard_limit(k)]
        if not fits:
            raise PromptTooLarge(
                f"prompt is {min(len(fresh_for(k)) for k in keys):,} chars even after trimming; "
                f"the largest limit among {keys} is {max(self.hard_limit(k) for k in keys):,}")
        ready = [k for k in fits if self.until.get(k, 0) <= now]
        if not ready:
            wait = min(self.until.get(k, now) for k in fits) - now
            if wait > COOL_WAIT or not any(k in self.orphans for k in fits):
                raise AllCoolingDown(wait)
            # dsh retries at once and gives up after two tries; this request has a chat to resume, so wait the cooldown
            # out here (dsh allows 900s of silence) instead of failing the stage and opening a new chat
            self.log(f"shim: every provider is cooling down; waiting {wait:.0f}s to resume the chat that is already open")
            end = self.clock() + wait
            while self.clock() < end:
                if should_stop and should_stop():
                    return
                self.wait(min(1.0, end - self.clock()))
            now = self.clock()
            ready = [k for k in fits if self.until.get(k, 0) <= now]
            if not ready:
                raise AllCoolingDown(min(self.until.get(k, now) for k in fits) - now)

        last = None
        length_failures = 0
        for key in ready:
            prov = PROVIDERS[key]
            started = False
            sess = None
            force_fresh = False
            fresh, send, plan = True, "", None
            same_retry = False
            for attempt in (0, 1, 2):
                asked = adopting = hold = False
                orp, send_chars = None, 0
                try:
                    sess = self._session(key)
                    send, fresh, plan = self._plan(key, sess, lambda k=key: fresh_for(k), messages, tools,
                                                   force_fresh)
                    adopting = bool(plan and plan.get("adopt"))
                    orp = plan["adopt"] if adopting else None
                    if fresh and not self._may_start_chat(key):
                        last = RuntimeError(f"{prov.label}: {MAX_NEW_CHATS_PER_HOUR} new chats were started in the last "
                                            "hour; not opening another (an account that makes many chats gets suspended)")
                        self.log(f"shim: {key} skipped: {last}")
                        break
                    if adopting:
                        send, send_chars, plan = "", orp["send_chars"], orp["plan"]
                        self.log(f"shim: {key} resuming the previous chat: taking over the answer already on its page "
                                 "(the client dropped the request) instead of opening a new chat")
                    else:
                        send_chars = len(send)
                        self.log(f"shim: -> {key} ({len(send):,} chars{'' if fresh else ', same chat'})")
                        self._debug_prompt(key, fresh, framed(prov, send, fresh, tools))     # exactly what is typed into the site
                    th_before = orp["th"] if adopting else self.threads.get(key)
                    self.threads.pop(key, None)            # unknown until this request finishes cleanly
                    yield ("provider", key)
                    self.requests[key] = self.requests.get(key, 0) + 1
                    if self.fault.get(key) == self.requests[key]:
                        raise RuntimeError("You've reached your limit (injected quota fault)")
                    hold = key in self.sticky and messages is not None    # a "chat is full" reply is discarded
                    held, text = [], ""
                    sess.keep_generating = hold          # a dropped sticky request leaves the site's answer to be adopted
                    asked = True
                    answer = (sess.adopt(orp["before"], should_stop) if adopting else
                              sess.ask(framed(prov, send, fresh, tools), fresh=fresh, should_stop=should_stop))
                    for ev in answer:
                        text = text + ev["t"] if "t" in ev else ev["reset"]
                        if hold:
                            held.append(ev)
                        else:
                            started = True
                            yield ("t", ev["t"]) if "t" in ev else ("reset", ev["reset"])
                    if hold and should_stop and should_stop():       # the client left: keep the chat for its retry
                        self._orphan(key, sess, messages, tools, send_chars, plan, th_before, orp)
                        return
                    blocks = sess.code_blocks()
                    if self._reply_says_full(prov, text, blocks):
                        raise SiteFull(f"{prov.label} says the chat is full or the message too long: "
                                       f"{text.strip()[:120]!r}")
                    self.limits.good(key, send_chars)
                    page_full = key in self.sticky and self._page_full(sess)
                    for ev in held:
                        started = True
                        yield ("t", ev["t"]) if "t" in ev else ("reset", ev["reset"])
                    if key in self.sticky and messages is not None:
                        if page_full:
                            self.log(f"shim: {key} shows its chat-full notice; the next request starts a new chat")
                        else:
                            self._remember(key, sess, messages, tools, send_chars, plan)
                    yield ("done", blocks)
                    return
                except GeneratorExit:
                    self.threads.pop(key, None)            # partial reply: that chat can no longer be trusted ...
                    if asked and hold:                     # ... as a continuation, but a retry of this request can adopt it
                        self._orphan(key, sess, messages, tools, send_chars, plan, th_before, orp)
                        raise
                    stop = getattr(sess, "stop_generation", None)
                    if stop:                       # the consumer left: stop burning free-tier quota
                        try:
                            stop()
                        except Exception:          # noqa: BLE001, S110  best-effort cleanup
                            pass
                    raise
                except Exception as e:           # noqa: BLE001
                    if started:
                        raise                      # output already left: cannot fail over
                    self.threads.pop(key, None)
                    if asked and hold and isinstance(e, TimeoutError) and "no reply started" in str(e):
                        # the message is in the chat and the site is just slow: the retry waits for that answer
                        self._orphan(key, sess, messages, tools, send_chars, plan, th_before, orp)
                    site_full = sess is not None and self._page_full(sess)
                    length = self._length_looking(prov, e) or site_full
                    if (not fresh and not same_retry and th_before is not None and not length
                            and any(m in str(e).lower() for m in prov.transient_errors)):
                        # "something went wrong, try again": try again in the SAME chat, once; never a new chat for it
                        same_retry = True
                        self.threads[key] = th_before
                        self.log(f"shim: {key} errored in a continued chat ({str(e)[:80]}); trying the same chat once more")
                        continue
                    if (length and not fresh and not site_full and not same_retry and th_before is not None
                            and self._only_generic_error(prov, e)):
                        # a generic error ("I encountered an error...") is not the site saying the chat is full: say it once
                        # more in the SAME chat before giving it up (a new chat is a risk to the account)
                        same_retry = True
                        self.threads[key] = th_before
                        self.log(f"shim: {key} errored in a continued chat ({str(e)[:80]}); trying the same chat once more")
                        continue
                    if length and attempt < 2:
                        if not fresh:
                            # the chat we were continuing is full: same site, NEW chat, compact history
                            self.log(f"shim: {key} chat is full ({str(e)[:100]}); starting a new one with a compact history")
                            force_fresh = True
                            continue
                        if self.limits.failed_length(key, len(send)):
                            smaller = fresh_for(key)
                            if len(smaller) < 0.9 * len(send):
                                self.log(f"shim: {key} rejected a {len(send):,}-char prompt as too long; "
                                         f"retrying in a new chat with {len(smaller):,}")
                                force_fresh = True
                                continue
                    kind = "error" if length else classify(e)
                    length_failures += 1 if length else 0
                    self.log(f"shim: {key} failed ({kind}): {str(e)[:160]}")
                    self._cool(key, kind, e)
                    last = e
                    break
        if length_failures and length_failures == len(ready):
            raise PromptTooLarge(f"every provider says the prompt/chat is too long: {last}")
        raise ProvidersFailed(f"all providers failed; last error: {last}")

    def _may_start_chat(self, key: str) -> bool:
        """Room for one more NEW chat on this site this hour? Counts it when there is. A site that sees dozens of
        chats an hour from one account can suspend it, so the cap holds even when a request could otherwise go,
        and (with `chats_path`) across a restart of the shim."""
        if not MAX_NEW_CHATS_PER_HOUR:
            return True
        if self.chats_path:
            now, saved = self.wall(), {}
            try:
                saved = json.loads(Path(self.chats_path).read_text())
            except (OSError, ValueError):
                pass
            recent = [t for t in saved.get(key, []) if isinstance(t, (int, float)) and 0 <= now - t < 3600]
            if len(recent) >= MAX_NEW_CHATS_PER_HOUR:
                return False
            saved[key] = recent + [now]
            if recent:
                self.log(f"shim: warning: {key} is getting its {len(recent) + 1}th new chat in the last hour "
                         f"(cap {MAX_NEW_CHATS_PER_HOUR}); one chat per task is the rule")
            try:
                Path(self.chats_path).parent.mkdir(parents=True, exist_ok=True)
                Path(self.chats_path).write_text(json.dumps(saved))
            except OSError:
                pass
            return True
        now = self.clock()
        recent = [t for t in self.new_chats.get(key, []) if now - t < 3600]
        if len(recent) >= MAX_NEW_CHATS_PER_HOUR:
            self.new_chats[key] = recent
            return False
        recent.append(now)
        self.new_chats[key] = recent
        return True

    def close(self):
        for s in self.sessions.values():
            try:
                s.close()
            except Exception:                # noqa: BLE001, S110  best-effort cleanup
                pass


# --------------------------------------------------------------- worker thread

class Worker:
    """Runs browser jobs one at a time on ONE thread. Playwright's sync API is
    bound to the thread that started it, so HTTP handlers (one thread per
    connection, so /health stays answerable mid-generation) never touch it; they
    queue a job here and read its events back."""

    def __init__(self):
        self.jobs: queue.Queue = queue.Queue()

    def run_forever(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            job()

    def stop(self):
        self.jobs.put(None)


def _client_gone(sock) -> bool:
    """True once the peer has closed its end (readable + zero bytes on peek)."""
    try:
        r, _, _ = select.select([sock], [], [], 0)
        return bool(r) and sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def stream_from_worker(worker: Worker, make_gen, sock=None, ping=None):
    """Iterate a generator that runs on the worker thread. Leaving early, or the
    client disconnecting, sets `cancel`, which the generator is polling. With `ping`, a ("ping", None) event is
    yielded whenever that many seconds pass with nothing to deliver (the caller keeps the connection alive)."""
    out: queue.Queue = queue.Queue()
    cancel = threading.Event()

    def job():
        gen = None
        try:
            gen = make_gen(cancel.is_set)
            for ev in gen:
                if cancel.is_set():
                    break
                out.put(("ev", ev))
            out.put(("end", None))
        except BaseException as e:             # noqa: BLE001 -- handed to the consumer
            out.put(("err", e))
        finally:
            if gen is not None:
                gen.close()

    worker.jobs.put(job)
    quiet = time.monotonic()
    try:
        while True:
            try:
                kind, val = out.get(timeout=0.5)
            except queue.Empty:
                if sock is not None and _client_gone(sock):
                    raise BrokenPipeError("client went away") from None
                if ping and time.monotonic() - quiet >= ping:
                    quiet = time.monotonic()
                    yield ("ping", None)
                continue
            quiet = time.monotonic()
            if kind == "ev":
                yield val
            elif kind == "end":
                return
            else:
                raise val
    finally:
        cancel.set()


# ----------------------------------------------------------------- HTTP layer

def is_title_request(messages, tools) -> bool:
    if tools or not messages:
        return False
    first = _text(messages[0].get("content"))
    return first.lstrip().lower().startswith("create a concise title")


def local_title(messages) -> str:
    """The harness asks a model to title the session. That costs a free-tier
    message per run for nothing, so answer it ourselves from the first prompt."""
    body = _text(messages[-1].get("content"))
    m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
    words = (json.loads(f'"{m.group(1)}"') if m else body).split()
    return " ".join(words[:6]) or "Session"


def _chunk(model, delta, finish=None, usage=None):
    c = {"id": "chatcmpl-shim", "object": "chat.completion.chunk",
         "created": int(time.time()), "model": model,
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        c["usage"] = usage
    return c


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    router: Router = None
    worker: Worker = None
    token: str | None = None       # None disables auth (unit tests only; serve() always sets it)

    def _allowed(self, public=False) -> bool:
        """Host + bearer check. `public` endpoints still enforce the Host check."""
        denied = auth.check_request(self.headers.get("Host"), self.headers.get("Authorization"),
                                    None if public else self.token)
        if denied:
            self._error(denied[0], "invalid_request_error", denied[1])
            return False
        return True

    def log_message(self, fmt, *a):
        sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

    def _send_json(self, obj, code=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, kind, message, error_code=None, headers=None):
        self._send_json({"error": {"message": message, "type": kind, "code": error_code}},
                        code, headers)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/health" and self.token and not self._authed():
            if self._allowed(public=True):         # liveness only: no provider detail
                self._send_json({"ok": True})
            return
        if not self._allowed():
            return
        if path == "/v1/models":
            ids = ["auto"] + self.router.keys
            self._send_json({"object": "list", "data": [
                {"id": i, "object": "model", "owned_by": "shim"} for i in ids]})
        elif path == "/health":
            self._send_json({"ok": True, "providers": self.router.status(), "pid": os.getpid(), "code": code_fingerprint()})
        else:
            self.send_error(404)

    def _authed(self) -> bool:
        return auth.check_request(self.headers.get("Host"), self.headers.get("Authorization"),
                                  self.token) is None

    def do_POST(self):
        if not self._allowed():
            return
        if urlparse(self.path).path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._error(400, "invalid_request_error", "bad Content-Length")
            return
        if length > MAX_BODY:
            remaining = min(length, 2 * MAX_BODY)      # so the client sees 413, not a reset
            while remaining > 0:
                remaining -= len(self.rfile.read(min(65536, remaining)) or b"")
            self.close_connection = True
            self._error(413, "invalid_request_error", "request body too large")
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            messages = body["messages"]
        except (ValueError, KeyError, json.JSONDecodeError):
            self._error(400, "invalid_request_error", "bad request body")
            return

        tools = body.get("tools") or None
        model = body.get("model", "auto")
        stream = bool(body.get("stream"))
        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        if is_title_request(messages, tools):
            self._reply(model, stream, want_usage, local_title(messages), [], 0, "title")
            return

        # The history is NOT trimmed here: the router sends only new messages to a chat it continues, and
        # trims (fit_messages) only when it must start a fresh chat. Trimming here would change the
        # already-seen prefix on every request and defeat sticky chats.
        prompt = render_prompt(messages, tools)
        try:
            events = stream_from_worker(
                self.worker, lambda stop: self.router.run(model, prompt, should_stop=stop,
                                                          messages=messages, tools=tools),
                self.connection, ping=PING_SECONDS if stream else None)
            self._drive(model, stream, want_usage, prompt, tools, events)
        except KeyError:
            self._error(404, "invalid_request_error", f"unknown model {model!r}", "model_not_found")
        except PromptTooLarge as e:
            self._error(400, "invalid_request_error", str(e), "context_length_exceeded")
        except AllCoolingDown as e:
            self._error(429, "rate_limit_error", str(e), "rate_limit_exceeded",
                        {"Retry-After": str(int(e.retry_after) + 1)})
        except ProvidersFailed as e:
            self._error(503, "server_error", str(e), "provider_unavailable")
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- output ------------------------------------------------------------

    def _drive(self, model, stream, want_usage, prompt, tools, events):
        """Run the router. Without tools, text streams live; with tools the
        reply is held until it can be classified as text or tool calls."""
        try:
            self._consume(events, model, stream, tools, st := {})
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:                 # noqa: BLE001
            if not st.get("streaming") and not st.get("began"):
                raise                          # nothing sent yet: do_POST maps it to a status
            self.wfile.write(_sse({"error": {"message": str(e), "type": "server_error"}}))
            self.wfile.flush()
            return
        provider, text, blocks, streaming = (st.get(k) for k in
                                             ("provider", "text", "blocks", "streaming"))
        text = text or ""
        content, calls = parse_reply(text, blocks or [], tools)
        if tools and not calls and "tool_call" in text:
            self.router.log(f"shim: WARNING {provider}'s reply mentions tool_call but no call could be read from it: "
                            f"{text.strip()[:200]!r}")
        if provider:
            self.router.note_reply(provider, content, calls)
        if os.environ.get("SHIM_DEBUG"):         # raw replies, for diagnosing a site's markup
            with open(os.environ["SHIM_DEBUG"], "a") as f:
                f.write(f"--- {provider} calls={len(calls)}\ntext={text!r}\nblocks={blocks!r}\n")
        used = (len(prompt) // 4, len(text) // 4)
        if streaming:
            self._finish_sse(model, "stop", want_usage, used)
        else:
            self._reply(model, stream, want_usage, content, calls, used, provider)

    def _consume(self, events, model, stream, tools, st):
        st.update(provider=None, text="", blocks=[], streaming=False)
        for kind, val in events:
            if kind == "provider":
                st["provider"] = val
            elif kind in ("t", "reset"):
                st["text"] = st["text"] + val if kind == "t" else val
                if stream and not tools:
                    if not st["streaming"]:
                        self._begin_sse()
                        self.wfile.write(_sse(_chunk(model, {"role": "assistant", "content": ""})))
                        st["streaming"] = True
                    if kind == "t":
                        self.wfile.write(_sse(_chunk(model, {"content": val})))
                        self.wfile.flush()
            elif kind == "done":
                st["blocks"] = val
            elif kind == "ping":
                # A Node HTTP client gives up on a response whose headers have not come after 300s (MEASURED 2026-10-28:
                # the drop came at 301s), and a held reply sends none. Send them now, then a comment line (ignored by SSE
                # parsers) so the connection is never silent.
                self._begin_sse()
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                st["began"] = True

    def _begin_sse(self):
        if getattr(self, "_sse_begun", False):
            return
        self._sse_begun = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _finish_sse(self, model, reason, want_usage, used):
        self.wfile.write(_sse(_chunk(model, {}, reason)))
        if want_usage:
            self.wfile.write(_sse({**_chunk(model, {}, None, {
                "prompt_tokens": used[0], "completion_tokens": used[1],
                "total_tokens": used[0] + used[1]}), "choices": []}))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _reply(self, model, stream, want_usage, content, calls, used, provider):
        used = used if isinstance(used, tuple) else (0, len(content) // 4)
        reason = "tool_calls" if calls else "stop"
        if not stream:
            msg = {"role": "assistant", "content": content or None}
            if calls:
                msg["tool_calls"] = calls
            self._send_json({
                "id": "chatcmpl-shim", "object": "chat.completion", "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": msg, "finish_reason": reason}],
                "usage": {"prompt_tokens": used[0], "completion_tokens": used[1],
                          "total_tokens": used[0] + used[1]}})
            return
        self._begin_sse()
        w = self.wfile.write
        w(_sse(_chunk(model, {"role": "assistant", "content": ""})))
        if content:
            w(_sse(_chunk(model, {"content": content})))
        for i, c in enumerate(calls):
            w(_sse(_chunk(model, {"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                                                  "function": {"name": c["function"]["name"],
                                                               "arguments": ""}}]})))
            w(_sse(_chunk(model, {"tool_calls": [{"index": i, "function": {
                "arguments": c["function"]["arguments"]}}]})))
        self._finish_sse(model, reason, want_usage, used)


def _sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


# ------------------------------------------------------------------------ CLI

def _sticky_keys(spec: str, keys) -> list[str]:
    spec = (spec or "auto").strip().lower()
    if spec in ("none", "off", "no"):
        return []
    if spec == "auto":
        return list(keys)              # sticky is the default for EVERY provider; STICKY_VERIFIED only says which were measured live
    chosen = list(keys) if spec == "all" else [k.strip() for k in spec.split(",") if k.strip()]
    bad = [k for k in chosen if k not in keys]
    if bad:
        sys.exit(f"--sticky names {bad}, which are not in --providers {list(keys)}")
    return chosen


def _alive(session) -> bool:
    try:
        _ = session.ctx.pages
        return not session.ctx.browser or session.ctx.browser.is_connected()
    except Exception:                    # noqa: BLE001
        return False


def serve(host, port, keys, headless, sticky=()):
    if host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit(f"refusing to bind {host}: this endpoint proxies logged-in browser "
                 f"sessions and must stay on loopback.")
    from .bridge import Session            # imported late: tests never need Playwright
    from playwright.sync_api import sync_playwright

    # One driver for every provider's browser: a thread can host only one.
    pw = sync_playwright().start()

    shared = {}     # the one logged-in browser; `shared` providers are tabs of it

    def make(prov: Provider):
        s = _make(prov)
        s.shrink_prompts = False       # prompts are sized by the router from what the site reports, never cut silently
        return s

    def _make(prov: Provider):
        if not prov.shared:
            if prov.channel and not prov.profile.is_dir():
                raise RuntimeError(f"{prov.key} needs you to log in: run `freeharness login {prov.key}` first")
            # A site that blocks headless Chrome gets a visible window of its own (own profile).
            return Session(headless=headless and prov.headless_ok, provider=prov, pw=pw,
                           channel=prov.channel or None)
        if not paths.chrome_profile().is_dir():
            raise RuntimeError(f"{prov.key} needs you to log in: run `freeharness login` first")
        root = shared.get("root")
        if root is None or not _alive(root):
            # The browser is NOT any provider's session: a provider that fails gets
            # its tab closed and rebuilt, never the browser the other tabs live in.
            root = shared["root"] = Session(headless=headless, provider=prov, pw=pw,
                                            user_data_dir=paths.chrome_profile(), channel="chrome")
        return root.tab(prov)

    Handler.router = Router(keys, make, sticky=sticky, limits=Limits(limits_path()),
                            chats_path=paths.state_dir() / "chats.json")
    Handler.token = None if os.environ.get("FREEHARNESS_NO_AUTH") else auth.load_or_create_token()
    Handler.worker = Worker()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    # `kill <pid>` must run the finally below, or the browsers outlive the shim.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f"shim listening on http://{host}:{port}/v1  providers={keys}  headless={headless}")
    # HTTP runs on threads; THIS thread owns Playwright, so it runs the browser jobs.
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        Handler.worker.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        Handler.router.close()
        if shared.get("root"):
            try:
                shared["root"].close()
            except Exception:            # noqa: BLE001, S110  best-effort cleanup
                pass
        pw.stop()


def check(keys, headless):
    """Ask each provider a question with a known answer; print what works."""
    from .bridge import Session
    ok = {}
    for k in keys:
        prov = PROVIDERS[k]
        try:
            s = Session(headless=headless and prov.headless_ok, provider=prov)
            try:
                text = "".join(e.get("t", e.get("reset", ""))
                               for e in s.ask("What is 17*23? Reply with just the number."))
            finally:
                s.close()
            ok[k] = "391" in text
            print(f"{k:11} {'OK' if ok[k] else 'WRONG ANSWER'}  {text.strip()[:60]!r}")
        except Exception as e:             # noqa: BLE001
            ok[k] = False
            print(f"{k:11} FAIL [{classify(e)}] {str(e)[:100]}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "check"):
        sp = sub.add_parser(name)
        # Only providers that pass the tool-use eval belong here (docs/PROVIDERS.md): a
        # fallback that confidently refuses to use tools is worse than no fallback.
        sp.add_argument("--providers", default="gemini,qwen",
                        help="comma-separated in rotation order, or 'all' (every registered site that can run, "
                             "including ones that failed the eval). Default: %(default)s")
        sp.add_argument("--headed", action="store_true", help="show the browser windows")
        sp.add_argument("--sticky", default=os.environ.get("FREEHARNESS_STICKY", "auto"),
                        help="providers (comma-separated, or 'all') that continue ONE chat per task and send only "
                             "what is new instead of a fresh chat with the whole history on every step. Default 'auto' "
                             "= every provider in --providers (only Gemini is live-verified); 'none' turns it off")
        if name == "serve":
            sp.add_argument("--host", default="127.0.0.1")
            sp.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    try:
        keys = resolve(args.providers, args.headed)
    except ValueError as e:
        sys.exit(str(e))
    risky = [k for k in keys if k not in ELIGIBLE]
    if risky:
        print(f"warning: {risky} have not passed the tool-use eval; a turn that lands on one may "
              f"answer without using tools. See docs/PROVIDERS.md.", file=sys.stderr)
    bad = [k for k in keys if k not in PROVIDERS]
    if bad:
        sys.exit(f"unknown provider(s) {bad}; choose from {sorted(PROVIDERS)}")
    if args.cmd == "serve":
        serve(args.host, args.port, keys, headless=not args.headed, sticky=_sticky_keys(args.sticky, keys))
    else:
        sys.exit(0 if any(check(keys, headless=not args.headed).values()) else 1)


if __name__ == "__main__":
    main()
