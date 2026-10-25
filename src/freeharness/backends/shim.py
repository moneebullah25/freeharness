"""
shim — an OpenAI-compatible /v1/chat/completions endpoint backed by free chat
sites driven through bridge.Session. Point an agent harness (DeepSeek Harness's
`llm-pi-ai` custom provider, or anything that speaks Chat Completions) at it.

    freeharness serve                       # gemini then qwen, headless
    freeharness serve --providers gemini,qwen
    python -m freeharness.backends.shim check   # does each site answer right now?

WHY THIS EXISTS. A chat site returns text; a harness wants structured
`tool_calls`. So each request is rendered into ONE prompt (tool list, a reply
protocol, the whole conversation), sent to a fresh thread, and the reply is
parsed back into `tool_calls`. The site never holds state: the harness's own
log is the source of truth, which is what makes switching sites mid-task safe.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .. import auth, paths
from .providers import ELIGIBLE, PROVIDERS, STICKY_VERIFIED, Provider, resolve

MAX_BODY = 8_000_000

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


# ------------------------------------------------------- sticky threads (see Router)

STICKY_MAX_STEPS = 20          # a chat that has taken this many requests is replaced by a fresh one
STICKY_MAX_CHARS = 60_000      # ... or has been sent this many characters in total


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


def tool_names(messages: list[dict]) -> dict:
    return {c["id"]: c["function"]["name"] for m in messages if m.get("role") == "assistant"
            for c in m.get("tool_calls") or []}


# -------------------------------------------------------------------- parsing

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
                        yield json.loads(text[i:j + 1]), j + 1
                    except json.JSONDecodeError:
                        pass
                    i = j + 1
                    break
        else:
            i += 1          # unbalanced (a truncated copy): later copies may be whole


def _as_call(obj, names: dict):
    """A {"name", "arguments"} object naming a real tool -> (name, args), else None."""
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    name = names.get(str(obj["name"]).lower())
    if not name:
        return None
    args = obj.get("arguments", obj.get("parameters", obj.get("input", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    return (name, args) if isinstance(args, dict) else None


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


def parse_fault(spec):
    """FREEHARNESS_FAULT="gemini:3" makes gemini's 3rd request fail with a quota error, so failover
    can be exercised live without waiting for a real limit. Unset in normal use."""
    out = {}
    for part in (spec or "").split(","):
        key, _, n = part.partition(":")
        if key.strip() and n.strip().isdigit():
            out[key.strip()] = int(n)
    return out


class Router:
    """Owns one lazily-created Session per provider and the cooldown ledger."""

    def __init__(self, keys, make_session, clock=time.monotonic, log=None, sticky=()):
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
        self.fault = parse_fault(os.environ.get("FREEHARNESS_FAULT"))   # test hook: {provider: Nth request fails}
        self.requests: dict[str, int] = {}

    def status(self) -> dict:
        now = self.clock()
        return {k: {"ready": self.until.get(k, 0) <= now,
                    "cooldown_s": max(0, round(self.until.get(k, 0) - now)),
                    "reason": self.why.get(k, "")} for k in self.keys}

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

    def _plan(self, key, sess, prompt, messages, tools):
        """(text to send, fresh?, request fingerprint) for one provider.

        Continue the provider's existing chat only if EVERYTHING still lines up: same session
        object, same page, the history we sent is an unchanged prefix of the new request, the next
        message is the very reply we returned, and the chat is not too long. Otherwise a fresh
        chat gets the full prompt, which is exactly the non-sticky behaviour."""
        if key not in self.sticky or messages is None:
            return prompt, True, None
        th = self.threads.get(key)
        url = getattr(sess, "current_url", lambda: None)()
        k = th["k"] if th else 0
        ok = bool(
            th and th["session"] is sess and th["reply_fp"] and url and url == th["url"]
            and th["steps"] < STICKY_MAX_STEPS and th["chars"] < STICKY_MAX_CHARS
            and len(messages) > k + 1 and messages[k].get("role") == "assistant"
            and all(m.get("role") in ("tool", "user") for m in messages[k + 1:])
            and reply_fingerprint(_text(messages[k].get("content")), messages[k].get("tool_calls")) == th["reply_fp"]
            and history_fingerprint(messages[:k], tools) == th["prefix_fp"])
        if not ok:
            if th:
                self.log(f"shim: {key} chat not continued (history changed or chat is full); starting a new one")
            return prompt, True, {"fresh": True}
        return (render_delta(messages[k + 1:], tools, tool_names(messages[:k + 1])), False,
                {"fresh": False, "steps": th["steps"], "chars": th["chars"]})

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

    def run(self, model: str, prompt: str, should_stop=None, *, messages=None, tools=None):
        """Generator of ("provider", key) once, then ("t", text) events, then
        ("done", code_blocks). Fails over only until the first text is yielded.
        `should_stop()` lets the caller abandon the generation (client left)."""
        keys = self.candidates(model)
        now = self.clock()
        fits = [k for k in keys if len(prompt) <= PROVIDERS[k].max_prompt]
        if not fits:
            raise PromptTooLarge(
                f"prompt is {len(prompt):,} chars; the largest budget among {keys} is "
                f"{max(PROVIDERS[k].max_prompt for k in keys):,}")
        ready = [k for k in fits if self.until.get(k, 0) <= now]
        if not ready:
            raise AllCoolingDown(min(self.until.get(k, now) for k in fits) - now)

        last = None
        for key in ready:
            started = False
            sess = None
            try:
                sess = self._session(key)
                send, fresh, plan = self._plan(key, sess, prompt, messages, tools)
                self.log(f"shim: -> {key} ({len(send):,} chars{'' if fresh else ', same chat'})")
                self.threads.pop(key, None)            # unknown until this request finishes cleanly
                yield ("provider", key)
                self.requests[key] = self.requests.get(key, 0) + 1
                if self.fault.get(key) == self.requests[key]:
                    raise RuntimeError("You've reached your limit (injected quota fault)")
                for ev in sess.ask(send, fresh=fresh, should_stop=should_stop):
                    if "t" in ev:
                        started = True
                        yield ("t", ev["t"])
                    else:
                        started = True
                        yield ("reset", ev["reset"])
                if key in self.sticky and messages is not None:
                    self._remember(key, sess, messages, tools, len(send), plan)
                yield ("done", sess.code_blocks())
                return
            except GeneratorExit:
                self.threads.pop(key, None)            # partial reply: that chat can no longer be trusted
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
                kind = classify(e)
                self.log(f"shim: {key} failed ({kind}): {str(e)[:160]}")
                self._cool(key, kind, e)
                last = e
        raise ProvidersFailed(f"all providers failed; last error: {last}")

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


def stream_from_worker(worker: Worker, make_gen, sock=None):
    """Iterate a generator that runs on the worker thread. Leaving early, or the
    client disconnecting, sets `cancel`, which the generator is polling."""
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
    try:
        while True:
            try:
                kind, val = out.get(timeout=0.5)
            except queue.Empty:
                if sock is not None and _client_gone(sock):
                    raise BrokenPipeError("client went away") from None
                continue
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
            self._send_json({"ok": True, "providers": self.router.status()})
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

        prompt = render_prompt(messages, tools)
        try:
            events = stream_from_worker(
                self.worker, lambda stop: self.router.run(model, prompt, should_stop=stop,
                                                          messages=messages, tools=tools),
                self.connection)
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
            if not st.get("streaming"):
                raise                          # nothing sent yet: do_POST maps it to a status
            self.wfile.write(_sse({"error": {"message": str(e), "type": "server_error"}}))
            self.wfile.flush()
            return
        provider, text, blocks, streaming = (st.get(k) for k in
                                             ("provider", "text", "blocks", "streaming"))
        text = text or ""
        content, calls = parse_reply(text, blocks or [], tools)
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

    def _begin_sse(self):
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
        return [k for k in keys if k in STICKY_VERIFIED]
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

    Handler.router = Router(keys, make, sticky=sticky)
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
                             "what is new instead of a fresh chat with the whole history on every step: 'auto' (verified by the eval), 'all', 'none' or a list")
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
