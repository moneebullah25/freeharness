#!/usr/bin/env python3
"""Self-check: the delta protocol and the SSE server, no browser needed.

    python test_bridge.py

The browser half can only be checked against the live site (`bridge.py ask`).
Everything below it is deterministic, so it gets a real test.
"""
import json
import urllib.request
import time

import pytest

from freeharness.backends import bridge


def test_delta_event():
    d = bridge.delta_event
    assert d("", "") is None
    assert d("abc", "abc") is None
    assert d("", "Hello") == {"t": "Hello"}
    assert d("Hello", "Hello world") == {"t": " world"}
    # a re-render that only truncates is a mid-paint artefact, not new content
    assert d("Hello world", "Hello") is None
    # a re-render that rewrites earlier text must not be sent as a suffix
    assert d("Hello wrld", "Hello world") == {"reset": "Hello world"}
    assert d("aaa", "bbb") == {"reset": "bbb"}
    # streaming a full answer one character at a time reconstructs it exactly
    target, sent, out = "The quick brown fox.", "", ""
    for i in range(len(target) + 1):
        ev = d(sent, target[:i])
        if ev and "t" in ev:
            out += ev["t"]
        elif ev:
            out = ev["reset"]
        sent = target[:i]
    assert out == target, out
    print("  delta_event ......... ok")


class FakeSession:
    """Stands in for the browser: replays a scripted DOM-text sequence.

    This reuses delta_event rather than re-deriving it, so these tests cover the
    HTTP/SSE layer, not Session.ask's polling loop -- that half needs the live
    site (`bridge.py ask`). delta_event itself is unit-tested above.
    """

    def __init__(self, frames):
        self.frames = frames
        self.stopped = False

    def ask(self, prompt, fresh=True):
        assert prompt, "handler must reject empty prompts before reaching here"
        sent = ""
        for text in self.frames:
            ev = bridge.delta_event(sent, text)
            if ev:
                yield ev
            sent = text

    def stop_generation(self):
        self.stopped = True




def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode()


def test_render_event():
    """The `ask` CLI path — it shipped broken because nothing covered it."""
    r = bridge.render_event
    assert r({"t": "Hello"}, "") == ("Hello", "Hello")
    assert r({"t": " world"}, "Hello") == (" world", "Hello world")
    # a reset that merely extends should print only the new tail
    assert r({"reset": "Hello world"}, "Hello") == (" world", "Hello world")
    # a reset that rewrites must be visibly flagged, not silently appended
    out, shown = r({"reset": "Hello there"}, "Hello world")
    assert out.startswith("\n--- re-rendered ---\n") and shown == "Hello there"
    print("  render_event ........ ok")


def _notes(n=3):
    topics = [("ESG RAG", "Qdrant chosen over pgvector for metadata filtering. "),
              ("DB Sync", "MS SQL to Snowflake replication without CDC. "),
              ("Eye Tracking", "Vectorised the I-DT fixation classifier. ")]
    return "\n".join(f"### Project {i} — {t}\n{body * 40}\n"
                     for i, (t, body) in enumerate(topics[:n], 1))


def test_shrink_single_question_prompt():
    """answerViaRelay shape: notes + question. Notes retrieve, question stays."""
    q = "Why did you choose Qdrant over pgvector?"
    prompt = f"earpiece rules\n\n{bridge.NOTES_HEAD}\n{_notes()}\n\nQuestion:\n{q}"
    out, note = bridge.shrink_prompt(prompt, budget=2_000)
    assert q in out, "the question must survive"
    assert "Qdrant" in out, "the relevant section must be the one kept"
    assert "Eye Tracking" not in out, "irrelevant sections should be dropped"
    assert "own +" in note, note

    small = "short prompt"
    assert bridge.shrink_prompt(small, budget=2_000) == (small, "")
    print("  shrink: 1-question .. ok")


def test_reference_sections_cannot_crowd_out_own_experience():
    """The whole point of the REF split: 200 textbook sections that mention the
    same terms must not displace the one section describing what they built."""
    own = ("### Project 1 - ESG RAG\nBuilt retrieval over ESG reports. "
           "Chose Qdrant over pgvector for metadata filtering at 2M vectors.\n")
    # a reference corpus that says the same words far more often
    ref = "\n".join(
        f"### REF: Vector Topic {i}\n\n**Keywords:** qdrant, pgvector, vector database\n\n"
        f"**Answer:** {'Qdrant pgvector vector database metadata filtering. ' * 8}\n"
        for i in range(200))
    notes = own + "\n" + ref
    q = "Why did you choose Qdrant over pgvector?"
    prompt = f"rules\n\n{bridge.NOTES_HEAD}\n{notes}\n\nQuestion:\n{q}"

    out, note = bridge.shrink_prompt(prompt, budget=4_000)
    assert "Project 1 - ESG RAG" in out, \
        f"their own experience was crowded out by reference sections: {note}"
    assert "own +" in note, note
    # Whether reference tags along is the relevance floor's call, not this
    # test's: here the short personal section outscores 200 verbose reference
    # ones on BM25 length normalisation, so dropping them is correct.
    print("  ref vs own pools .... ok")


def test_irrelevant_reference_pool_is_skipped():
    """A question with no discriminating terms scored near-noise against the
    reference pool, which then filled 40% of the budget with arbitrary textbook
    sections. Below RELEVANCE_FLOOR the pool is dropped entirely."""
    own = "\n".join(
        f"### Project {i} - War Story {i}\nWe shipped a thing and it broke and we fixed it.\n"
        for i in range(1, 6))
    ref = "\n".join(
        f"### REF: Attention Topic {i}\n\n**Keywords:** softmax, qkv, transformer\n\n"
        f"**Answer:** Attention computes weighted sums over value vectors.\n"
        for i in range(30))
    notes = own + "\n" + ref
    mk = lambda q: f"rules\n\n{bridge.NOTES_HEAD}\n{notes}\n\nQuestion:\n{q}"

    # nothing in the reference pool relates to a project war story
    out, note = bridge.shrink_prompt(mk("Tell me about a project you worked on."), budget=1_500)
    assert "+ 0 ref" in note, note
    assert "REF:" not in out, "irrelevant reference sections should not be retrieved"

    # but a question that genuinely matches the reference pool still gets it
    out, _ = bridge.shrink_prompt(mk("How does softmax attention with QKV work?"), budget=1_500)
    assert "REF:" in out, "relevant reference sections must still come through"
    print("  noise ref skipped ... ok")


def test_shrink_session_prompt_keeps_question_and_recent_talk():
    """Session shape: notes + transcript + question, all three handled."""
    tr = "\n".join(f"Interviewer: point number {i} about systems." for i in range(300))
    q = "How would you shard that table?"
    prompt = (f"You are assisting a candidate.\n\n{bridge.NOTES_HEAD}\n{_notes()}\n\n"
              f"Conversation so far:\n{tr}\n\nQuestion to answer:\n{q}")
    out, note = bridge.shrink_prompt(prompt, budget=4_000)

    assert q in out, "the question must survive"
    assert "You are assisting a candidate." in out, "the rules must survive"
    # the RECENT end of the transcript is what the question refers to
    assert "point number 299" in out and "point number 0 " not in out
    assert "recent transcript" in note and "own +" in note, note
    print("  shrink: session Q ... ok")


def test_shrink_session_action_prompt_has_no_question_block():
    """Assist/Answer/Summary end with Transcript: and no question at all."""
    tr = "\n".join(f"You: sentence {i}." for i in range(300))
    prompt = (f"You are assisting a candidate.\n\n{bridge.NOTES_HEAD}\n{_notes()}\n\n"
              f"Transcript:\n{tr}")
    out, note = bridge.shrink_prompt(prompt, budget=4_000)
    assert "You are assisting a candidate." in out
    assert "sentence 299" in out, "the latest speech must survive"
    assert len(out) < len(prompt)
    print("  shrink: session act . ok")


def test_shrink_unknown_shape_keeps_both_ends():
    out, note = bridge.shrink_prompt("HEAD" + "x" * 5_000 + "TAIL", budget=1_000)
    assert out.startswith("HEAD") and out.endswith("TAIL"), "never drop the end"
    assert "kept first+last" in note, note
    print("  shrink: unknown ..... ok")


def test_site_error_not_passed_off_as_answer():
    assert bridge.looks_like_site_error("Something went wrong. If this persists")
    assert bridge.looks_like_site_error("You've reached our limit of messages")
    assert bridge.looks_like_site_error("  TOO MANY REQUESTS, try later")
    # a real answer that merely mentions failure must not trip it
    assert not bridge.looks_like_site_error(
        "When the sync went wrong, I added a checkpoint file so retries were safe.")
    assert not bridge.looks_like_site_error("I chose Qdrant for metadata filtering.")
    print("  site error guard .... ok")


def test_navigation_retries_before_giving_up():
    """A single slow Cloudflare check left the tab on about:blank past the 45s
    timeout and killed a task that had real work committed."""
    import inspect
    src = inspect.getsource(bridge.Session.goto)
    assert "NAV_ATTEMPTS" in src, "navigation does not retry"
    assert "retrying" in src, "a retry is not reported to the user"
    assert "TimeoutError" in src, "a final failure must be the type the loop handles"
    assert bridge.NAV_ATTEMPTS >= 2
    # EVERY navigation must go through the retrying helper. Fixing open_chat
    # alone left new_thread with a bare goto that died the identical death.
    whole = inspect.getsource(bridge)
    bare = [l for l in whole.splitlines()
            if "page.goto(" in l and "def goto" not in l]
    assert len(bare) == 1, f"bare page.goto outside the helper: {bare}"
    print("  nav retry ........... ok")


def test_reply_timeout_reports_page_state_not_a_guessed_cause():
    """The old message said "(rate limited?)" without checking, which sent a
    real debugging session after a quota problem that did not exist."""
    import inspect
    src = inspect.getsource(bridge.Session.ask)
    assert "rate limited" not in src, "the error still guesses a cause"
    assert "page_state()" in src, "the error does not report what it observed"
    state = inspect.getsource(bridge.Session.page_state)
    # each of these distinguishes a genuinely different cause
    for signal in ["is_closed", "_challenged", "_streaming", "composer holds",
                   "assistant turn"]:
        assert signal in state, f"page_state does not check {signal}"
    assert bridge.REPLY_TIMEOUT >= 60, "30s is shorter than an observed TTFT"
    print("  timeout diagnostics . ok")


def test_disabled_send_button_raises_a_clear_error():
    """A rate-limited composer leaves send visible but aria-disabled. A plain
    click waits 45s then throws a raw Playwright traceback; the agent's loop
    only handles TimeoutError, so it must arrive as one with a readable reason."""
    import inspect
    src = inspect.getsource(bridge.Session.ask)
    assert "aria-disabled" in src, "the disabled state is not checked"
    assert "free-tier" in src, "the reason is not explained"
    assert "TimeoutError" in src, "it must raise the type the agent handles"
    # and a raw Playwright error is translated rather than escaping
    assert "could not send the prompt" in src
    print("  disabled send ....... ok")













class _FakePage:
    def __init__(self, clipboard, clicked=True):
        self.clipboard, self.clicked, self.calls = clipboard, clicked, []

    def evaluate(self, js, *a):
        self.calls.append(js)
        return self.clicked if "click()" in js else self.clipboard

    def wait_for_timeout(self, ms):
        pass


class _FakeCtx:
    def grant_permissions(self, perms, origin=None):
        self.granted = (perms, origin)


def test_code_blocks_from_clipboard_markdown():
    # Qwen: the DOM holds no code (Monaco), the Copy button's clipboard holds the markdown.
    s = object.__new__(bridge.Session)
    s.p = bridge.PROVIDERS["qwen"]
    s.ctx = _FakeCtx()
    md = '```tool_call\n{"name": "bash", "arguments": {"command": "echo one"}}\n```\n```tool_call\n{"name": "bash"}\n```'
    s.page = _FakePage(md)
    assert s.code_blocks() == ['{"name": "bash", "arguments": {"command": "echo one"}}', '{"name": "bash"}']
    assert s.ctx.granted == (["clipboard-read", "clipboard-write"], "https://chat.qwen.ai")
    s.page = _FakePage("", clicked=False)         # no copy button yet: fall back, never raise
    s.page.evaluate = lambda js, *a: [] if "querySelectorAll(css)" in js and "click()" not in js else False
    assert s.code_blocks() == []
    print("  clipboard blocks .... ok")


def test_verification_page_is_detected_by_url_not_waited_on():
    s = object.__new__(bridge.Session)
    s.p = bridge.PROVIDERS["qwen"]

    class Page:
        url = "https://chat.qwen.ai//api/v2/chat/completions/_____tmd_____/punish?x5secdata=abc"

        def title(self):
            return "Qwen"

    s.page = Page()
    assert s._challenged() is True
    Page.url = "https://chat.qwen.ai/c/1234"
    Page.locator = lambda self, css: type("L", (), {"count": lambda self: 0})()
    assert s._challenged() is False
    from freeharness.backends.shim import classify
    assert classify(RuntimeError("Qwen is challenging this browser with a human-verification page")) == "captcha"
    print("  verification url .... ok")


def test_fresh_install_creates_the_nested_private_profile_directory(tmp_path, monkeypatch):
    # Regression: Session did mkdir(exist_ok=True) without parents, so on a fresh install
    # (no profiles/ yet) every launch raised FileNotFoundError and the provider was cooled down.
    import stat
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "fresh"))
    seen = {}

    class Chromium:
        def launch_persistent_context(self, user_data_dir, **kw):
            seen["dir"] = user_data_dir
            seen["exists"] = __import__("pathlib").Path(user_data_dir).is_dir()

            class Ctx:
                pages = []

                def new_page(self):
                    class P:
                        def set_default_timeout(self, ms):
                            pass
                    return P()
            return Ctx()

    class PW:
        chromium = Chromium()

    bridge.Session(headless=True, provider=bridge.PROVIDERS["gemini"], pw=PW())
    assert seen["exists"] and seen["dir"].endswith("profiles/gemini"), seen
    from pathlib import Path
    assert stat.S_IMODE(Path(seen["dir"]).stat().st_mode) == 0o700
    print("  fresh-install profile ok")


if __name__ == "__main__":
    print("chatgpt-bridge self-check")
    test_delta_event()
    test_render_event()
    test_shrink_single_question_prompt()
    test_reference_sections_cannot_crowd_out_own_experience()
    test_irrelevant_reference_pool_is_skipped()
    test_shrink_session_prompt_keeps_question_and_recent_talk()
    test_shrink_session_action_prompt_has_no_question_block()
    test_shrink_unknown_shape_keeps_both_ends()
    test_site_error_not_passed_off_as_answer()
    test_navigation_retries_before_giving_up()
    test_reply_timeout_reports_page_state_not_a_guessed_cause()
    test_disabled_send_button_raises_a_clear_error()
    test_code_blocks_from_clipboard_markdown()
    test_verification_page_is_detected_by_url_not_waited_on()
    print("all passed")


def test_landing_on_the_sign_in_page_is_a_login_error_not_a_changed_ui():
    from freeharness.backends import bridge, providers, shim

    class P:
        url = "https://chat.deepseek.com/sign_in"

    s = bridge.Session.__new__(bridge.Session)
    s.p, s.page = providers.PROVIDERS["deepseek"], P()
    import pytest
    with pytest.raises(RuntimeError) as e:
        s._raise_if_logged_out()
    assert shim.classify(e.value) == "login" and "freeharness login deepseek" in str(e.value)
    s.page.url = "https://chat.deepseek.com/a/chat/s/1"
    s._raise_if_logged_out()                                   # a normal chat page: nothing raised


def test_a_too_long_notice_on_the_page_ends_the_wait_for_a_reply_at_once():
    """MEASURED live 2026-10-28: ChatGPT showed 'message too long' and never replied; the shim waited the full 600s."""
    class Loc:
        def count(self):
            return 0
        last = first = property(lambda self: self)

    class Page:
        def locator(self, css):
            return Loc()
    s = object.__new__(bridge.Session)
    s.p = bridge.CHATGPT
    s.page = Page()
    s._challenged = lambda: False
    s.chat_full = lambda: True
    s._streaming = lambda: False
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="too long or the chat is full"):
        list(s._collect(0))
    assert time.monotonic() - t0 < 10


def test_a_limit_notice_on_the_page_ends_the_wait_for_a_reply_with_the_notice_in_the_error():
    class Loc:
        def count(self):
            return 0
        last = first = property(lambda self: self)

    class Page:
        def locator(self, css):
            return Loc()
    s = object.__new__(bridge.Session)
    s.p = bridge.CHATGPT
    s.page = Page()
    s._challenged = lambda: False
    s.chat_full = lambda: False
    s.page_notice = lambda markers: "you've reached our limit"
    s._streaming = lambda: False
    with pytest.raises(RuntimeError, match="shows a notice and no reply started: \"you've reached our limit\""):
        list(s._collect(0))
