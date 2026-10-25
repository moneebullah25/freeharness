#!/usr/bin/env python3
"""Self-check for shim.py. No browser: sessions are scripted fakes.

Run: .venv/bin/python test_shim.py
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from freeharness import auth
from freeharness.backends import shim

TOOLS = [
    {"type": "function", "function": {
        "name": "read", "description": "Read a UTF-8 text file. Returns numbered lines.",
        "parameters": {"type": "object", "required": ["file_path"], "properties": {
            "file_path": {"type": "string", "description": "Path to read."},
            "limit": {"type": "number"}}}}},
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "required": ["command"],
                       "properties": {"command": {"type": "string"}}}}},
]


class FakeSession:
    """Stands in for bridge.Session at its public boundary (ask / code_blocks / close)."""

    def __init__(self, reply="ok", blocks=None, error=None, chunks=None):
        self.reply, self.blocks, self.error = reply, blocks or [], error
        self.chunks = chunks
        self.prompts, self.closed = [], False

    def ask(self, prompt, fresh=True, should_stop=None):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        for c in (self.chunks or [self.reply]):
            if should_stop and should_stop():
                self.stopped = True
                return
            yield {"t": c}

    stopped = False

    def stop_generation(self):
        self.stopped = True

    def code_blocks(self):
        return self.blocks

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def router(sessions, keys=("gemini", "perplexity"), clock=None):
    return shim.Router(keys, lambda prov: sessions[prov.key],
                       clock=clock or Clock(), log=lambda m: None)


def collect(r, model, prompt="hi"):
    return list(r.run(model, prompt))


# ------------------------------------------------------------------ prompting

def test_prompt_lists_tools_protocol_and_history_in_order():
    msgs = [
        {"role": "system", "content": "Be careful."},
        {"role": "user", "content": "read a.txt"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read", "arguments": '{"file_path":"a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "1: hello"},
    ]
    p = shim.render_prompt(msgs, TOOLS)
    assert "```tool_call" in p, "the reply protocol is missing"
    assert "- read: Read a UTF-8 text file." in p and "file_path (string, required)" in p
    assert "limit (number, optional)" in p
    assert "Be careful." in p
    i_user, i_call, i_res = p.index("[user]"), p.index('"name": "read"'), p.index("[tool_result read]")
    assert i_user < i_call < i_res, "history is out of order"
    assert "1: hello" in p and p.rstrip().endswith("plain text.")
    assert "MOST RECENT [user] message" in p, "the reply must target the latest request"
    print("  prompt render ....... ok")


def test_prompt_without_tools_has_no_protocol():
    p = shim.render_prompt([{"role": "user", "content": "hi"}], None)
    assert "tool_call" not in p and "# Tools" not in p
    print("  prompt no tools ..... ok")


def test_content_parts_are_flattened():
    p = shim.render_prompt([{"role": "user", "content": [
        {"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}]}], None)
    assert "part one\npart two" in p
    print("  content parts ....... ok")


# -------------------------------------------------------------------- parsing

def test_parse_code_block_is_preferred_and_exact():
    # the case that motivated code blocks: markdown would eat the ** in rendered text
    block = '{"name": "bash", "arguments": {"command": "echo **hi** a_b_c"}}'
    content, calls = shim.parse_reply("flattened text without it", [block], TOOLS)
    assert content == "" and len(calls) == 1
    args = json.loads(calls[0]["function"]["arguments"])
    assert args == {"command": "echo **hi** a_b_c"}, args
    assert calls[0]["id"].startswith("call_") and calls[0]["type"] == "function"
    print("  parse code block .... ok")


def test_parse_falls_back_to_flattened_text():
    text = 'Sure.\ntool_call\n{"name":"read","arguments":{"file_path":"x.py"}}\n'
    content, calls = shim.parse_reply(text, [], TOOLS)
    assert [json.loads(c["function"]["arguments"]) for c in calls] == [{"file_path": "x.py"}]
    print("  parse text fallback . ok")


def test_parse_multiple_calls_and_case_insensitive_names():
    blocks = ['{"name":"READ","arguments":{"file_path":"a"}}',
              '{"name":"bash","arguments":{"command":"ls"}}']
    _, calls = shim.parse_reply("", blocks, TOOLS)
    assert [c["function"]["name"] for c in calls] == ["read", "bash"], "names not canonicalised"
    print("  parse multi call .... ok")


def test_qwen_nbsp_and_double_rendered_block_is_one_call():
    # Measured on Qwen: the viewer shows the block twice, the second copy with U+00A0
    # for every space, which json cannot parse.
    plain = '{"name": "bash", "arguments": {"command": "echo hi there"}}'
    nbsp = plain.replace(" ", "\xa0")
    for text in (f"tool_call\n{plain}\n1\n{nbsp}", f"tool_call\n1\n{nbsp}"):
        _, calls = shim.parse_reply(text, [], TOOLS)
        assert [json.loads(c["function"]["arguments"]) for c in calls] == [{"command": "echo hi there"}]
    print("  qwen nbsp + double render . ok")


def test_truncated_first_copy_does_not_hide_the_whole_one():
    # Measured on Qwen: copy 1 was cut mid-string, copy 2 (nbsp-spaced) was complete.
    cut = 'tool_call\n{"name": "bash", "arguments": {"command": "echo hi'
    whole = '{"name":\xa0"bash",\xa0"arguments":\xa0{"command":\xa0"echo\xa0hi"}}'
    _, calls = shim.parse_reply(cut + "\n1\n" + whole, [], TOOLS)
    assert [json.loads(c["function"]["arguments"]) for c in calls] == [{"command": "echo hi"}]
    print("  truncated copy first .. ok")


def test_provider_spec_all_and_unknown():
    from freeharness.backends import providers
    assert providers.resolve("gemini, qwen") == ["gemini", "qwen"]
    allk = providers.resolve("all")
    assert allk[:2] == ["gemini", "qwen"], "eligible providers must come first"
    assert "chatgpt" in allk, "sites that block headless Chrome run in their own visible window"
    assert allk == list(providers.EVERYTHING) and providers.resolve("ALL") == allk
    assert not providers.PROVIDERS["chatgpt"].headless_ok and providers.PROVIDERS["chatgpt"].channel == "chrome"
    assert allk[-1] == "deepseek" and not providers.PROVIDERS["deepseek"].headless_ok
    try:
        providers.resolve("gemini,nope")
        raise AssertionError("unknown provider accepted")
    except ValueError as e:
        assert "nope" in str(e)
    print("  providers=all ....... ok")


def test_json_about_something_else_is_not_a_tool_call():
    """A model explaining some JSON must not trigger a tool."""
    text = 'An example record is {"name": "Alice", "arguments": {"x": 1}} as you see.'
    content, calls = shim.parse_reply(text, ['{"name": "Alice"}'], TOOLS)
    assert calls == [] and "Alice" in content
    print("  parse non-tool json . ok")


def test_arguments_as_json_string_and_nested_braces():
    block = '{"name":"bash","arguments":"{\\"command\\": \\"echo {a}\\"}"}'
    _, calls = shim.parse_reply("", [block], TOOLS)
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "echo {a}"}
    print("  parse string args ... ok")


def test_plain_answer_passes_through_and_no_tools_means_no_parsing():
    c, calls = shim.parse_reply("  The answer is 391.\n", [], TOOLS)
    assert (c, calls) == ("The answer is 391.", [])
    raw = '{"name":"read","arguments":{}}'
    c, calls = shim.parse_reply(raw, [raw], None)
    assert calls == [] and c == raw, "without tools nothing may become a call"
    print("  parse plain ......... ok")


# --------------------------------------------------------------------- router

def test_failover_to_next_provider_and_cooldown():
    g = FakeSession(error=RuntimeError("Gemini returned an error page: you've reached your limit"))
    p = FakeSession(reply="from perplexity")
    clock = Clock()
    r = router({"gemini": g, "perplexity": p}, clock=clock)
    evs = collect(r, "auto")
    assert ("t", "from perplexity") in evs and ("provider", "perplexity") in evs
    st = r.status()
    assert not st["gemini"]["ready"] and st["gemini"]["reason"].startswith("quota"), st
    assert not g.closed, "a quota error says nothing about the browser: keep it"
    g2 = FakeSession(reply="gemini is back")
    r.make_session = lambda prov: {"gemini": g2, "perplexity": p}[prov.key]
    r.sessions["gemini"] = g2
    assert ("provider", "perplexity") in collect(r, "auto"), "still cooling: must skip gemini"
    clock.t += shim.COOLDOWN["quota"] + 1
    assert ("provider", "gemini") in collect(r, "auto"), "cooldown never expired"
    print("  failover + cooldown . ok")


def test_hard_failure_discards_the_browser():
    g = FakeSession(error=TimeoutError("no reply started within 90s"))
    r = router({"gemini": g, "perplexity": FakeSession(reply="p")})
    collect(r, "auto")
    assert g.closed and "gemini" not in r.sessions, "a wedged browser must not be reused"
    assert r.status()["gemini"]["reason"].startswith("timeout")
    print("  hard failure ........ ok")


def test_all_cooling_down_reports_retry_after():
    clock = Clock()
    r = router({"gemini": FakeSession(error=RuntimeError("limit")),
                "perplexity": FakeSession(error=RuntimeError("limit"))}, clock=clock)
    try:
        collect(r, "auto")
        assert False, "should have failed"
    except shim.ProvidersFailed:
        pass
    try:
        collect(r, "auto")
        assert False, "should be cooling"
    except shim.AllCoolingDown as e:
        assert 0 < e.retry_after <= shim.COOLDOWN["quota"], e.retry_after
    print("  all cooling ......... ok")


def test_pinned_model_never_fails_over():
    g = FakeSession(error=RuntimeError("limit"))
    p = FakeSession(reply="should not be used")
    r = router({"gemini": g, "perplexity": p})
    try:
        collect(r, "gemini")
        assert False
    except shim.ProvidersFailed:
        pass
    assert p.prompts == [], "a pinned model must not touch another provider"
    try:
        r.candidates("nope")
        assert False
    except KeyError:
        pass
    print("  pinned model ........ ok")


def test_no_failover_after_output_has_started():
    class Dies(FakeSession):
        def ask(self, prompt, fresh=True, should_stop=None):
            yield {"t": "partial"}
            raise TimeoutError("answer did not finish")
    p = FakeSession(reply="x")
    r = router({"gemini": Dies(), "perplexity": p})
    got, raised = [], None
    try:
        for ev in r.run("auto", "hi"):
            got.append(ev)
    except TimeoutError as e:
        raised = e
    assert raised and ("t", "partial") in got and p.prompts == [], \
        "once text has left, switching would splice two answers"
    print("  no mid-stream swap .. ok")


def test_oversize_prompt_skips_small_providers_then_errors():
    small = shim.PROVIDERS["perplexity"]
    r = router({"gemini": FakeSession(reply="big ok"), "perplexity": FakeSession()})
    big = "x" * (small.max_prompt + 1)
    if shim.PROVIDERS["gemini"].max_prompt > small.max_prompt:
        assert ("provider", "gemini") in collect(r, "auto", big)
    try:
        collect(r, "auto", "x" * 10_000_000)
        assert False
    except shim.PromptTooLarge:
        pass
    print("  oversize prompt ..... ok")


def test_classify():
    assert shim.classify(RuntimeError("Gemini returned an error page: you've reached your limit")) == "quota"
    assert shim.classify(RuntimeError("Perplexity returned an error page: sign up and repeat")) == "login"
    assert shim.classify(RuntimeError("X is challenging this browser")) == "captcha"
    assert shim.classify(TimeoutError("no reply started within 90s")) == "timeout"
    assert shim.classify(LookupError("no selector matched")) == "broken"
    assert shim.classify(ValueError("boom")) == "error"
    print("  classify ............ ok")


# ----------------------------------------------------------------------- http

def serve_with(sessions, keys=("gemini", "perplexity")):
    shim.Handler.router = router(sessions, keys)
    shim.Handler.worker = shim.Worker()
    threading.Thread(target=shim.Handler.worker.run_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), shim.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def sse_events(body):
    return [json.loads(l[6:]) for l in body.splitlines()
            if l.startswith("data: ") and l != "data: [DONE]"]


def test_http_tool_call_streams_in_openai_shape():
    blk = '{"name":"read","arguments":{"file_path":"hello.txt"}}'
    srv, base = serve_with({"gemini": FakeSession(blocks=[blk]), "perplexity": FakeSession()})
    try:
        status, body, _ = post(base + "/v1/chat/completions", {
            "model": "auto", "stream": True, "stream_options": {"include_usage": True},
            "tools": TOOLS, "messages": [{"role": "user", "content": "read hello.txt"}]})
        assert status == 200 and body.endswith("data: [DONE]\n\n")
        ev = sse_events(body)
        calls = [c for e in ev for ch in e["choices"] for c in ch["delta"].get("tool_calls", [])]
        assert calls[0]["function"]["name"] == "read" and calls[0]["id"].startswith("call_")
        args = "".join(c["function"].get("arguments", "") for c in calls)
        assert json.loads(args) == {"file_path": "hello.txt"}, args
        assert [ch["finish_reason"] for e in ev for ch in e["choices"] if ch["finish_reason"]] == ["tool_calls"]
        assert ev[-1]["choices"] == [] and ev[-1]["usage"]["prompt_tokens"] > 0
        print("  http tool call ...... ok")
    finally:
        srv.shutdown()


def test_http_text_streams_live_and_non_stream_json():
    s = FakeSession(chunks=["Hel", "lo ", "there"])
    srv, base = serve_with({"gemini": s, "perplexity": FakeSession()})
    try:
        _, body, _ = post(base + "/v1/chat/completions", {
            "model": "gemini", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        deltas = [ch["delta"].get("content") for e in sse_events(body) for ch in e["choices"]]
        assert "".join(d for d in deltas if d) == "Hello there"
        assert deltas.count("Hel") == 1, "text without tools must stream chunk by chunk"
        _, body, _ = post(base + "/v1/chat/completions", {
            "model": "gemini", "messages": [{"role": "user", "content": "hi"}]})
        msg = json.loads(body)["choices"][0]
        assert msg["message"]["content"] == "Hello there" and msg["finish_reason"] == "stop"
        print("  http text ........... ok")
    finally:
        srv.shutdown()


def test_http_errors_map_to_statuses():
    srv, base = serve_with({"gemini": FakeSession(error=RuntimeError("limit hit")),
                            "perplexity": FakeSession(error=RuntimeError("limit hit"))})
    try:
        url = base + "/v1/chat/completions"
        msgs = [{"role": "user", "content": "hi"}]
        code, body, _ = post(url, {"model": "auto", "messages": msgs})
        assert code == 503 and json.loads(body)["error"]["code"] == "provider_unavailable"
        code, body, hdr = post(url, {"model": "auto", "messages": msgs})
        assert code == 429 and int(hdr["Retry-After"]) > 0, (code, body)
        code, body, _ = post(url, {"model": "auto",
                                   "messages": [{"role": "user", "content": "x" * 500_000}]})
        assert code == 400 and json.loads(body)["error"]["code"] == "context_length_exceeded"
        assert post(url, {"model": "auto", "messages": [
            {"role": "user", "content": "x" * (shim.MAX_BODY + 1)}]})[0] == 413
        assert post(url, {"model": "nope", "messages": msgs})[0] == 404
        assert post(url, {"model": "auto"})[0] == 400
        print("  http errors ......... ok")
    finally:
        srv.shutdown()


def test_title_request_is_answered_locally():
    s = FakeSession(reply="SHOULD NOT BE CALLED")
    srv, base = serve_with({"gemini": s, "perplexity": FakeSession()})
    try:
        _, body, _ = post(base + "/v1/chat/completions", {"model": "auto", "messages": [
            {"role": "system", "content": "Create a concise title for an AI coding-assistant session."},
            {"role": "user", "content": 'Generate the title:\n[{"seq":8,"text":"fix the login bug in auth.py now please"}]'}]})
        assert s.prompts == [], "a title must not spend a free-tier message"
        assert json.loads(body)["choices"][0]["message"]["content"] == "fix the login bug in auth.py"
        print("  local title ......... ok")
    finally:
        srv.shutdown()


def test_models_and_health():
    srv, base = serve_with({"gemini": FakeSession(), "perplexity": FakeSession()})
    try:
        with urllib.request.urlopen(base + "/v1/models") as r:
            ids = [m["id"] for m in json.load(r)["data"]]
        assert ids == ["auto", "gemini", "perplexity"], ids
        with urllib.request.urlopen(base + "/health") as r:
            assert json.load(r)["providers"]["gemini"]["ready"] is True
        print("  models + health ..... ok")
    finally:
        srv.shutdown()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def test_auth_token_and_host_checks():
    srv, base = serve_with({"gemini": FakeSession(), "perplexity": FakeSession()})
    shim.Handler.token = "s3cret-" + "x" * 32
    good = {"Authorization": "Bearer " + shim.Handler.token}
    try:
        assert _get(base + "/v1/models")[0] == 401
        assert _get(base + "/v1/models", {"Authorization": "Bearer nope"})[0] == 401
        assert _get(base + "/v1/models", {"Authorization": shim.Handler.token})[0] == 401   # no scheme
        assert _get(base + "/v1/models", good)[0] == 200
        code, body = _get(base + "/health")                 # liveness leaks no provider detail
        assert code == 200 and body == {"ok": True}, (code, body)
        assert "providers" in _get(base + "/health", good)[1]
        # DNS rebinding: a page on evil.example reaches 127.0.0.1 but cannot forge Host
        assert _get(base + "/v1/models", {**good, "Host": "evil.example"})[0] == 403
        assert _get(base + "/health", {"Host": "evil.example"})[0] == 403
        req = urllib.request.Request(base + "/v1/chat/completions", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req)
            raise AssertionError("POST without a token must be refused")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        print("  auth token + host ... ok")
    finally:
        shim.Handler.token = None
        srv.shutdown()


def test_host_ok_and_token_file(tmp=None):
    assert auth.host_ok("127.0.0.1:8766") and auth.host_ok("localhost") and auth.host_ok("[::1]:8766")
    assert not auth.host_ok("evil.example:8766") and not auth.host_ok(None) and not auth.host_ok("127.0.0.1.evil.example")
    import os, tempfile
    with tempfile.TemporaryDirectory() as d:
        os.environ["FREEHARNESS_HOME"] = d
        try:
            a, b = auth.load_or_create_token(), auth.load_or_create_token()
            assert a == b and len(a) >= 32
            from freeharness import paths
            assert oct(paths.token_file().stat().st_mode & 0o777) == "0o600"
        finally:
            del os.environ["FREEHARNESS_HOME"]
    print("  host check + token file ok")


class SlowSession(FakeSession):
    """Yields one chunk, then keeps the 'browser' busy until told to stop."""

    def ask(self, prompt, fresh=True, should_stop=None):
        self.prompts.append(prompt)
        yield {"t": "first "}
        for _ in range(100):                       # up to ~10s
            if should_stop and should_stop():
                self.stopped = True
                return
            time.sleep(0.1)
        yield {"t": "late"}


def test_health_answers_while_a_generation_is_running():
    """After an abandoned request the harness restarts and asks /v1/models. A
    single-threaded server made that wait for the whole generation."""
    slow = SlowSession()
    srv, base = serve_with({"gemini": slow, "perplexity": FakeSession()})
    try:
        t = threading.Thread(target=lambda: post(base + "/v1/chat/completions", {
            "model": "gemini", "stream": True, "messages": [{"role": "user", "content": "hi"}]}),
            daemon=True)
        t.start()
        time.sleep(0.5)
        t0 = time.monotonic()
        with urllib.request.urlopen(base + "/health", timeout=3) as r:
            assert json.load(r)["ok"]
        with urllib.request.urlopen(base + "/v1/models", timeout=3) as r:
            assert r.status == 200
        assert time.monotonic() - t0 < 2, "health was blocked behind the generation"
        slow.stopped = True
        print("  health mid-generation ok")
    finally:
        srv.shutdown()


def test_client_disconnect_cancels_the_generation():
    slow = SlowSession()
    srv, base = serve_with({"gemini": slow, "perplexity": FakeSession()})
    try:
        body = json.dumps({"model": "gemini", "tools": TOOLS,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        sock = socket.create_connection(("127.0.0.1", srv.server_port))
        sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                     b"Content-Length: %d\r\n\r\n" % len(body) + body)
        time.sleep(0.8)                            # generation underway, reply buffered (tools)
        sock.close()                               # the client gives up
        deadline = time.monotonic() + 5
        while not slow.stopped and time.monotonic() < deadline:
            time.sleep(0.1)
        assert slow.stopped, "the site kept generating after the client left"
        print("  disconnect cancels .. ok")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    print("shim self-check")
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("all passed")
