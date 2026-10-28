"""Chat policy of the shim: sticky for every provider, the site's own "chat is full" signals, learned (never
assumed) prompt limits, compact history for a new chat, SHIM_DEBUG_PROMPTS. Offline: scripted fake sessions."""
import json
import stat

import pytest

from freeharness.backends import shim
from freeharness.backends.limits import NO_LIMIT, Limits
from freeharness.backends.providers import PROVIDERS
from test_shim import BLOCK, TOOLS, TOOLS_S, Clock, FakeSession, _long_history, convo

CALL_TEXT = "```tool_call\n" + BLOCK + "\n```"


class ScriptSession(FakeSession):
    """Each ask() plays the next script entry: an Exception to raise, or (text, code_blocks)."""

    def __init__(self, script=()):
        super().__init__()
        self.script = list(script)
        self.blocks = []
        self.full = False                      # what chat_full() reports

    def ask(self, prompt, fresh=True, should_stop=None):
        self.prompts.append(prompt)
        self.fresh_flags.append(fresh)
        entry = self.script.pop(0) if self.script else (CALL_TEXT, [BLOCK])
        if isinstance(entry, Exception):
            raise entry
        text, self.blocks = entry
        yield {"t": text}

    def chat_full(self):
        return self.full


def make_router(key, sess, sticky=True, **kw):
    return shim.Router([key], lambda prov: sess, clock=Clock(), log=lambda m: None,
                       sticky=[key] if sticky else [], **kw)


def step(r, key, messages, tools=TOOLS_S):
    """One request, then the handler's parse + note_reply. Returns (events, tool_calls)."""
    prompt = shim.render_prompt(messages, tools)
    events = list(r.run(key, prompt, messages=messages, tools=tools))
    text = "".join(v for k, v in events if k == "t")
    blocks = next((v for k, v in events if k == "done"), [])
    content, calls = shim.parse_reply(text, blocks, tools)
    r.note_reply(key, content, calls)
    return events, calls


def big_convo(rounds, size=3000):
    msgs = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "list files"}]
    for i in range(rounds):
        msgs += [{"role": "assistant", "content": None, "tool_calls": [
                     {"id": f"call_{i}", "type": "function",
                      "function": {"name": "bash", "arguments": '{"command": "ls"}'}}]},
                 {"role": "tool", "tool_call_id": f"call_{i}", "content": f"OUT-{i}-" + chr(97 + i % 26) * size + f"-END{i}"}]
    return msgs


# ------------------------------------------------------------ sticky is the default

ALL = ("gemini", "qwen", "deepseek", "chatgpt", "grok", "perplexity")


def test_auto_means_sticky_for_every_provider_and_none_switches_it_off():
    assert shim._sticky_keys("auto", list(ALL)) == list(ALL)
    assert shim._sticky_keys("none", list(ALL)) == []
    assert set(ALL) == set(PROVIDERS)


@pytest.mark.parametrize("key", ALL)
def test_every_provider_continues_one_chat_and_sends_only_the_new_messages(key):
    s = ScriptSession()
    r = make_router(key, s)
    for n in range(6):
        step(r, key, convo(n))
    assert s.fresh_flags == [True] + [False] * 5, "one chat for the whole task"
    assert all("# Tools" not in p for p in s.prompts[1:]) and "list files" not in s.prompts[5]


def test_sticky_none_keeps_a_fresh_chat_per_step():
    s = ScriptSession()
    r = make_router("deepseek", s, sticky=False)
    for n in range(3):
        step(r, "deepseek", convo(n))
    assert s.fresh_flags == [True, True, True]


def test_a_growing_session_alone_never_starts_a_new_chat():
    s = ScriptSession()
    r = make_router("deepseek", s)
    for n in range(60):
        step(r, "deepseek", big_convo(n, size=2000))
    assert s.fresh_flags.count(True) == 1 and len(s.prompts) == 60
    assert max(len(p) for p in s.prompts[1:]) < 4_000, "only what is new is sent, however long the session is"


def test_safety_nets_are_generous_and_still_trip(monkeypatch):
    assert shim.STICKY_MAX_STEPS >= 400 and shim.STICKY_MAX_CHARS >= 600_000
    monkeypatch.setattr(shim, "STICKY_MAX_STEPS", 5)
    s = ScriptSession()
    r = make_router("qwen", s)
    for n in range(8):
        step(r, "qwen", convo(n))
    assert s.fresh_flags == [True, False, False, False, False, True, False, False]
    monkeypatch.setattr(shim, "STICKY_MAX_STEPS", 400)
    monkeypatch.setattr(shim, "STICKY_MAX_CHARS", 2_000)
    s2 = ScriptSession()
    r2 = make_router("qwen", s2)
    for n in range(4):
        step(r2, "qwen", big_convo(n, size=900))
    assert s2.fresh_flags.count(True) >= 2, "the character cap must also trip"


# ------------------------------------------------- the site says the chat is full

@pytest.mark.parametrize("key,notice", [
    ("chatgpt", "You've reached the maximum length for this conversation, but you can keep talking by starting a new chat."),
    ("deepseek", "The current conversation has reached its length limit. Please start a new chat."),
    ("gemini", "This conversation is too long. Start a new chat to continue."),
    ("qwen", "The conversation is too long, please open a new chat."),
    ("grok", "This conversation is too long. Start a new conversation."),
    ("perplexity", "This thread is too long. Start a new thread."),
])
def test_a_full_notice_in_the_reply_starts_a_new_chat_with_a_compact_history_then_sticks_to_it(key, notice):
    s = ScriptSession([(CALL_TEXT, [BLOCK]), (notice, []), (CALL_TEXT, [BLOCK]), (CALL_TEXT, [BLOCK])])
    r = make_router(key, s)
    step(r, key, big_convo(0))
    events, calls = step(r, key, big_convo(1))              # the chat is full: the notice is NOT the answer
    assert s.fresh_flags == [True, False, True], s.fresh_flags
    assert calls and calls[0]["function"]["name"] == "bash"
    assert notice not in "".join(v for k, v in events if k == "t"), "the notice must not reach the harness"
    new_chat_prompt = s.prompts[2]
    assert "# Tools" in new_chat_prompt and "list files" in new_chat_prompt and "OUT-0-" in new_chat_prompt
    step(r, key, big_convo(2))                              # ... and the new chat continues sticky
    assert s.fresh_flags == [True, False, True, False]
    assert "OUT-1-" in s.prompts[-1] and "# Tools" not in s.prompts[-1] and "OUT-0-" not in s.prompts[-1]


def test_a_full_notice_is_checked_case_insensitively_and_only_on_short_replies_without_tool_calls():
    s = ScriptSession([(CALL_TEXT, [BLOCK]), ("Done. " + "x " * 1000 + "start a new chat", []), (CALL_TEXT, [BLOCK])])
    r = make_router("chatgpt", s)
    step(r, "chatgpt", convo(0))
    step(r, "chatgpt", convo(1))                            # long answer mentioning the words: a real answer
    assert s.fresh_flags == [True, False]
    s2 = ScriptSession([(CALL_TEXT, [BLOCK]), ("I will START A NEW CHAT", [BLOCK]), (CALL_TEXT, [BLOCK])])
    r2 = make_router("chatgpt", s2)
    step(r2, "chatgpt", convo(0))
    step(r2, "chatgpt", convo(1))                           # has a tool-call block: not a notice
    assert s2.fresh_flags == [True, False]


def test_a_full_signal_from_a_failed_ask_starts_a_new_chat_in_the_same_request():
    s = ScriptSession([(CALL_TEXT, [BLOCK]), RuntimeError("DeepSeek returned an error page: conversation is too long"),
                       (CALL_TEXT, [BLOCK])])
    r = make_router("deepseek", s)
    step(r, "deepseek", convo(0))
    _, calls = step(r, "deepseek", convo(1))
    assert calls and s.fresh_flags == [True, False, True]
    assert "deepseek" not in r.until, "a full chat is not a provider failure: no cooldown"


def test_a_length_error_with_other_wording_is_also_treated_as_full():
    s = ScriptSession([(CALL_TEXT, [BLOCK]), RuntimeError("request exceeds the maximum context length"), (CALL_TEXT, [BLOCK])])
    r = make_router("grok", s)
    step(r, "grok", convo(0))
    step(r, "grok", convo(1))
    assert s.fresh_flags == [True, False, True]


def test_a_full_banner_on_the_page_after_a_good_answer_makes_the_NEXT_request_fresh():
    s = ScriptSession()
    r = make_router("chatgpt", s)
    step(r, "chatgpt", convo(0))
    s.full = True                                           # the page now shows the notice
    step(r, "chatgpt", convo(1))                            # this answer was fine and is used
    s.full = False
    step(r, "chatgpt", convo(2))
    step(r, "chatgpt", convo(3))
    assert s.fresh_flags == [True, False, True, False]


def test_unrelated_errors_still_cool_the_provider_down():
    s = ScriptSession([(CALL_TEXT, [BLOCK]), RuntimeError("Perplexity returned an error page: sign up and repeat")])
    r = make_router("perplexity", s)
    step(r, "perplexity", convo(0))
    with pytest.raises(shim.ProvidersFailed):
        step(r, "perplexity", convo(1))
    assert r.status()["perplexity"]["ready"] is False


@pytest.mark.parametrize("mutate", [
    lambda m: m.__setitem__(1, {"role": "user", "content": "a DIFFERENT first message"}),   # dsh compaction / edit
    lambda m: m.insert(1, {"role": "user", "content": "inserted into the seen history"}),
])
def test_history_mismatch_starts_a_new_chat(mutate):
    s = ScriptSession()
    r = make_router("deepseek", s)
    step(r, "deepseek", convo(0))
    msgs = convo(1)
    mutate(msgs)
    step(r, "deepseek", msgs)
    assert s.fresh_flags == [True, True] and "# Tools" in s.prompts[1]


def test_session_object_or_page_change_starts_a_new_chat():
    s = ScriptSession()
    r = make_router("qwen", s)
    step(r, "qwen", convo(0))
    s.url = "https://site.test/c/OTHER"
    step(r, "qwen", convo(1))
    r.threads["qwen"]["session"] = object()
    step(r, "qwen", convo(2))
    assert s.fresh_flags == [True, True, True]


# --------------------------------------------------- limits are learned, never assumed

def test_providers_have_no_assumed_prompt_ceiling():
    assert all(p.max_prompt == NO_LIMIT for p in PROVIDERS.values())
    assert all(p.full_markers for p in PROVIDERS.values())


@pytest.fixture(autouse=True)
def _no_digest_unless_asked(monkeypatch):
    """The older tests look at compaction alone; the digest tests set DIGEST_TARGET themselves."""
    monkeypatch.setattr(shim, "DIGEST_TARGET", 10**9)


def _assert_compact(prompt, msgs):
    """`prompt` is the compact rendering of `msgs`: older results head+tail with a marker, newest verbatim."""
    tool = [m["content"] for m in msgs if m["role"] == "tool"]
    for i, body in enumerate(tool):
        if i >= len(tool) - shim.PROTECT_RESULTS:
            assert body in prompt, f"result {i} is among the newest and must be verbatim"
        else:
            assert body not in prompt and body[:shim.TRIM_HEAD] in prompt and body[-shim.TRIM_TAIL:] in prompt
            assert f"of {len(body):,}" in prompt
    assert "SYSTEM-RULES" in prompt and "LATEST-QUESTION" in prompt and "first ask" in prompt


def test_every_fresh_chat_gets_the_compact_history_even_when_no_limit_is_known():
    """Live: resuming a long session sent a 303,797-char first prompt to a fresh chat."""
    s = ScriptSession()
    r = make_router("deepseek", s, sticky=False)
    msgs = _long_history(n=60, size=5000)                   # ~300k chars of raw history
    full = shim.render_prompt(msgs, TOOLS)
    assert len(full) > 290_000
    list(r.run("deepseek", full, messages=msgs, tools=TOOLS))
    assert len(s.prompts[0]) < 80_000 and len(s.prompts[0]) < len(full) * 0.3, len(s.prompts[0])
    _assert_compact(s.prompts[0], msgs)
    assert s.prompts[0] == shim.render_prompt(shim.compact_messages(msgs), TOOLS)


def test_compact_messages_leaves_a_short_history_alone_and_does_not_mutate_the_input():
    small = _long_history(n=3, size=300)
    assert shim.compact_messages(small) is small
    big = _long_history(n=20, size=5000)
    before = json.dumps(big)
    out = shim.compact_messages(big)
    assert json.dumps(big) == before and len(out) == len(big) and out is not big


def test_sticky_first_prompt_resume_mismatch_and_full_signal_all_use_the_compact_history():
    msgs = _long_history(n=60, size=5000)
    full = shim.render_prompt(msgs, TOOLS)
    for key in ("gemini", "qwen", "deepseek", "chatgpt"):       # dsh resume: a router with no chat yet
        s = ScriptSession()
        list(make_router(key, s).run(key, full, messages=msgs, tools=TOOLS))
        assert s.fresh_flags == [True] and len(s.prompts[0]) < 80_000
        _assert_compact(s.prompts[0], msgs)
    # history-prefix mismatch (dsh compaction / edit) in a running sticky chat
    s = ScriptSession()
    r = make_router("deepseek", s)
    step(r, "deepseek", big_convo(1))
    edited = big_convo(40, size=5000)
    edited[1] = {"role": "user", "content": "a DIFFERENT first message"}
    step(r, "deepseek", edited)
    assert s.fresh_flags == [True, True] and len(s.prompts[1]) < 0.35 * len(shim.render_prompt(edited, TOOLS_S))
    # a "chat is full" signal in a long session
    s = ScriptSession([(CALL_TEXT, [BLOCK]), ("The conversation is too long, start a new chat", []), (CALL_TEXT, [BLOCK])])
    r = make_router("deepseek", s)
    step(r, "deepseek", big_convo(40, size=5000))
    long_conv = big_convo(41, size=5000)
    step(r, "deepseek", long_conv)
    assert s.fresh_flags == [True, False, True]
    assert len(s.prompts[2]) < 0.35 * len(shim.render_prompt(long_conv, TOOLS_S))
    assert long_conv[-1]["content"] in s.prompts[2], "the newest result is verbatim"


def test_failover_to_another_provider_gets_the_compact_history_not_the_raw_one():
    a, b = ScriptSession([RuntimeError("You've reached your limit (quota)")]), ScriptSession()
    sess = {"gemini": a, "qwen": b}
    r = shim.Router(["gemini", "qwen"], lambda prov: sess[prov.key], clock=Clock(), log=lambda m: None,
                    sticky=["gemini", "qwen"])
    msgs = _long_history(n=60, size=5000)
    full = shim.render_prompt(msgs, TOOLS)
    events = list(r.run("auto", full, messages=msgs, tools=TOOLS))
    assert ("provider", "qwen") in events and b.fresh_flags == [True]
    assert len(b.prompts[0]) < 80_000 and len(a.prompts[0]) < 80_000
    _assert_compact(b.prompts[0], msgs)


def test_length_failure_on_a_fresh_chat_is_recorded_and_retried_smaller_then_confirmed_and_persisted(tmp_path):
    path = tmp_path / "limits.json"
    lim = Limits(path)
    s = ScriptSession([RuntimeError("DeepSeek returned an error page: the message is too long"), (CALL_TEXT, [BLOCK])])
    r = make_router("deepseek", s, sticky=False, limits=lim)
    msgs = _long_history(n=40, size=5000)
    full = shim.render_prompt(msgs, TOOLS)
    events = list(r.run("deepseek", full, messages=msgs, tools=TOOLS))
    assert ("done", [BLOCK]) in events
    first, second = len(s.prompts[0]), len(s.prompts[1])
    assert first == len(shim.render_prompt(shim.compact_messages(msgs), TOOLS)) < len(full)
    assert second <= first * 0.5 + 1, (first, second)
    assert "LATEST-QUESTION" in s.prompts[1] and "SYSTEM-RULES" in s.prompts[1] and (shim.OMIT_TAG in s.prompts[1] or shim.DROPPED in s.prompts[1])
    snap = lim.snapshot("deepseek")
    assert snap["suspect_max_chars"] == first and snap["confirmed"] is True
    assert snap["known_good_chars"] == second and snap["ceiling_chars"] == int(first * 0.8)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = Limits(path)                                    # survives a restart
    assert again.snapshot("deepseek") == snap
    assert r.status()["deepseek"]["limits"] == snap, "exposed in /health"
    # the next fresh chat targets half of the confirmed ceiling
    assert r.fresh_budget("deepseek") == int(first * 0.8 * shim.FRESH_TARGET)


def test_a_failure_at_or_below_a_size_that_worked_is_not_blamed_on_size():
    lim = Limits()
    lim.good("deepseek", 200_000)
    s = ScriptSession([RuntimeError("DeepSeek returned an error page: the message is too long")])
    r = make_router("deepseek", s, sticky=False, limits=lim)
    msgs = _long_history(n=10, size=5000)
    with pytest.raises(shim.PromptTooLarge):                # the site says too long and nothing can be shrunk
        list(r.run("deepseek", shim.render_prompt(msgs, TOOLS), messages=msgs, tools=TOOLS))
    assert lim.snapshot("deepseek")["suspect_max_chars"] is None


def test_a_continuing_chat_that_fails_never_poisons_the_learned_limit():
    lim = Limits()
    s = ScriptSession([(CALL_TEXT, [BLOCK]), RuntimeError("conversation is too long"), (CALL_TEXT, [BLOCK])])
    r = make_router("deepseek", s, limits=lim)
    step(r, "deepseek", convo(0))
    step(r, "deepseek", convo(1))
    assert lim.snapshot("deepseek")["suspect_max_chars"] is None


def test_limits_state_machine():
    lim = Limits()
    assert lim.ceiling("x") is None and lim.probe_budget("x") is None
    assert lim.failed_length("x", 50_000) is True
    assert lim.ceiling("x") is None, "one failure is only a suspicion"
    assert lim.probe_budget("x") == 25_000
    lim.good("x", 20_000)
    assert lim.ceiling("x") == 40_000, "failure at N then a smaller success: confirmed, with a safety margin"
    lim.good("x", 60_000)                                   # worked above the suspect: the suspicion was wrong
    assert lim.ceiling("x") is None and lim.snapshot("x")["suspect_max_chars"] is None


# ------------------------------------------------------------ debug prompts

def test_debug_prompts_keep_the_last_three_full_prompts_owner_only_plus_an_index(tmp_path):
    out = tmp_path / "prompts.txt"
    s = ScriptSession()
    r = make_router("gemini", s, sticky=False, debug_prompts=str(out))
    for n in range(5):
        step(r, "gemini", convo(n))
    text = out.read_text()
    assert text.count("===== ") == 3
    assert shim.render_prompt(convo(4), TOOLS_S) in text and shim.render_prompt(convo(0), TOOLS_S) not in text
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    rows = [json.loads(x) for x in out.with_name(out.name + ".index").read_text().splitlines()]
    assert len(rows) == 5 and rows[0]["provider"] == "gemini" and rows[0]["mode"] == "fresh"
    assert rows[1]["chars"] == len(s.prompts[1]) and len(rows[1]["first"]) <= 300 and len(rows[1]["last"]) <= 300


def test_debug_prompts_mark_sticky_deltas(tmp_path):
    out = tmp_path / "p.txt"
    s = ScriptSession()
    r = make_router("gemini", s, debug_prompts=str(out))
    step(r, "gemini", convo(0))
    step(r, "gemini", convo(1))
    rows = [json.loads(x) for x in out.with_name("p.txt.index").read_text().splitlines()]
    assert [x["mode"] for x in rows] == ["fresh", "sticky"]


def test_debug_prompts_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SHIM_DEBUG_PROMPTS", raising=False)
    s = ScriptSession()
    r = make_router("gemini", s)
    step(r, "gemini", convo(0))
    assert list(tmp_path.iterdir()) == []


# ------------------------------------- do tool results reach the model intact?

def test_newest_tool_results_reach_the_site_verbatim_in_every_delta_and_old_ones_are_shortened_by_the_rule_only():
    s = ScriptSession()
    r = make_router("deepseek", s)
    for n in range(1, 41):
        msgs = big_convo(n, size=3000)
        step(r, "deepseek", msgs)
        newest = msgs[-1]["content"]
        assert s.fresh_flags[-1] is (n == 1)
        assert newest in s.prompts[-1], f"step {n}: the newest tool result must arrive verbatim"
    # a fresh chat on the same history (what a new chat is sent): older results shortened by the documented rule
    msgs = big_convo(40, size=3000)
    full = len(shim.render_prompt(msgs, TOOLS_S))
    out = shim.fit_messages(msgs, TOOLS_S, full // 2)
    tool_in = [m["content"] for m in msgs if m["role"] == "tool"]
    tool_out = [m["content"] for m in out if m["role"] == "tool"]
    assert len(tool_out) == len(tool_in), "no result may disappear while shortening is enough"
    assert tool_out[-shim.PROTECT_RESULTS:] == tool_in[-shim.PROTECT_RESULTS:]
    shortened = 0
    for a, b in zip(tool_in, tool_out):
        if a == b:
            continue
        shortened += 1
        assert b.startswith(a[:shim.TRIM_HEAD]) and b.endswith(a[-shim.TRIM_TAIL:])
        assert f"of {len(a):,}" in b and "chars omitted" in b and "re-run the command" in b
    assert shortened > 0
    prompt = shim.render_prompt(out, TOOLS_S)
    assert len(prompt) <= full // 2
    assert tool_in[-1] in prompt


def test_render_delta_has_every_new_result_verbatim_even_when_huge():
    huge = "H" * 150_000
    new = [{"role": "tool", "tool_call_id": "a", "content": huge}, {"role": "tool", "tool_call_id": "b", "content": "small"}]
    d = shim.render_delta(new, TOOLS_S, {"a": "read", "b": "bash"})
    assert huge in d and "[tool_result read]" in d and "[tool_result bash]\nsmall" in d


# --------------------------------------------------------------- fit_messages

def test_fit_messages_shortens_old_tool_output_oldest_first_and_keeps_the_newest_verbatim():
    ms = _long_history(n=12, size=5000)
    full = len(shim.render_prompt(ms, TOOLS))
    assert shim.fit_messages(ms, TOOLS, full) is ms and shim.fit_messages(ms, TOOLS, None) is ms
    budget = full - 20_000
    out = shim.fit_messages(ms, TOOLS, budget)
    p = shim.render_prompt(out, TOOLS)
    assert len(p) <= budget
    assert "SYSTEM-RULES" in p and "LATEST-QUESTION" in p and "first ask" in p, "shortening is enough: nothing dropped"
    tool = [m["content"] for m in out if m["role"] == "tool"]
    orig = [m["content"] for m in ms if m["role"] == "tool"]
    assert len(tool) == 12 and tool[-shim.PROTECT_RESULTS:] == orig[-shim.PROTECT_RESULTS:]
    assert tool[0].startswith("BODY0" + "z" * 100) and tool[0].endswith("z" * 200)
    assert f"of {len(orig[0]):,}" in tool[0] and "re-run the command" in tool[0] and len(tool[0]) < 1000
    assert len(ms[3]["content"]) > 4000, "input not mutated"
    out2 = shim.fit_messages(ms, TOOLS, full - 3_000)       # a small saving touches only the oldest
    assert [i for i, (a, b) in enumerate(zip(ms, out2)) if a != b] == [3]
    tiny = shim.fit_messages(ms, TOOLS, len(shim.render_prompt([ms[0], ms[-1]], TOOLS)) + 200)
    p = shim.render_prompt(tiny, TOOLS)
    assert "SYSTEM-RULES" in p and "LATEST-QUESTION" in p and shim.DROPPED in p and "first ask" not in p


def test_fit_messages_long_session_of_small_results_keeps_the_newest_verbatim():
    """Regression for the loop seen live: results the model read last were blanked, so it asked again (x200)."""
    ms = [{"role": "system", "content": "SYSTEM-RULES"}, {"role": "user", "content": "task"}]
    for i in range(120):
        ms.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "bash", "arguments": '{"command": "x"}'}}]})
        ms.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"RESULT-{i}-" + "r" * 3000})
    ms.append({"role": "user", "content": "go on"})
    full = len(shim.render_prompt(ms, TOOLS))
    out = shim.fit_messages(ms, TOOLS, int(full * 0.5))
    assert len(shim.render_prompt(out, TOOLS)) <= int(full * 0.5)
    tools = [m["content"] for m in out if m["role"] == "tool"]
    assert len(tools) == 120, "shortening was enough: no message may be dropped"
    for i in range(115, 120):
        assert tools[i] == f"RESULT-{i}-" + "r" * 3000
    assert shim.OMIT_TAG in tools[0] and tools[0].startswith("RESULT-0-")
    # every result after the last assistant message is protected, even beyond the last 5
    ms2 = ms[:-1] + [{"role": "tool", "tool_call_id": f"d{k}", "content": f"FRESH-{k}-" + "f" * 3000} for k in range(8)] + [ms[-1]]
    out2 = shim.fit_messages(ms2, TOOLS, int(len(shim.render_prompt(ms2, TOOLS)) * 0.5))
    assert [m["content"] for m in out2 if m["role"] == "tool"][-8:] == [m["content"] for m in ms2 if m["role"] == "tool"][-8:]


def test_fit_messages_caps_a_protected_result_only_beyond_the_generous_size():
    ms = [{"role": "system", "content": "S"}, {"role": "user", "content": "task"},
          {"role": "assistant", "content": "", "tool_calls": [
              {"id": "c0", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
          {"role": "tool", "tool_call_id": "c0", "content": "HEAD" + "q" * 200_000 + "TAIL"},
          {"role": "user", "content": "go"}]
    res = shim.fit_messages(ms, TOOLS, 80_000)[3]["content"]
    assert res.startswith("HEAD") and res.endswith("TAIL") and shim.OMIT_TAG in res and len(res) < shim.PROTECT_CAP + 200
    mid = shim.fit_messages(ms, TOOLS, 250_000)
    assert mid is ms, "it already fits: untouched"


# ------------------------------------------------------------- HTTP end to end

def test_http_handler_does_not_pretrim_the_history(tmp_path):
    """A pre-trimmed history would change the already-seen prefix on every request and defeat sticky."""
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    s = ScriptSession()
    shim.Handler.router = make_router("gemini", s)
    shim.Handler.worker = shim.Worker()
    threading.Thread(target=shim.Handler.worker.run_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), shim.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"
    try:
        msgs = _long_history(n=30, size=5000)
        req = urllib.request.Request(base, json.dumps({"model": "gemini", "tools": TOOLS, "messages": msgs}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        assert s.prompts[0] == shim.render_prompt(shim.compact_messages(msgs), TOOLS)
    finally:
        srv.shutdown()
        shim.Handler.router = None


def _steps(n, result_chars=3000):
    msgs = [{"role": "system", "content": "be careful"}, {"role": "user", "content": "do the task"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": f"ls dir{i}"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i} " + "x" * result_chars})
    msgs.append({"role": "user", "content": "keep going"})
    return msgs


def test_a_fresh_chat_gets_a_digest_of_hundreds_of_steps_and_the_newest_messages_verbatim():
    msgs = _steps(200)
    out = shim.digest_history(shim.compact_messages(msgs), TOOLS, 48_000)
    assert len(shim.render_prompt(msgs, TOOLS)) > 500_000
    assert len(shim.render_prompt(out, TOOLS)) < 90_000
    assert out[0] == msgs[0] and out[1] == msgs[1] and out[-1] == msgs[-1]           # system, task and latest user message
    digest = out[2]["content"]
    assert digest.startswith("[Earlier steps") and "assistant: bash" in digest and "ls dir5" in digest or "older lines" in digest
    assert out[-shim.FRESH_KEEP:] == shim.compact_messages(msgs)[-shim.FRESH_KEEP:]
    firsts = [m for m in out[3:] if m["role"] != "system"]
    assert firsts[0]["role"] != "tool"                                               # no orphan tool result
    assert shim.digest_history(out, TOOLS, 48_000) is out                                    # idempotent once it fits


def test_a_short_history_is_not_digested():
    msgs = _steps(5, result_chars=100)
    assert shim.digest_history(msgs, TOOLS, 48_000) is msgs


def test_a_fresh_chat_on_resume_sends_the_digest_not_hundreds_of_messages(monkeypatch):
    monkeypatch.setattr(shim, "DIGEST_TARGET", 48_000)
    sess = ScriptSession([("fine", [])])
    router = make_router("gemini", sess)
    list(router.run("gemini", "", messages=_steps(150), tools=TOOLS))
    assert len(sess.prompts[0]) < 90_000 and "[Earlier steps" in sess.prompts[0]


def test_new_chats_per_hour_are_capped_so_an_account_is_never_flooded(monkeypatch):
    monkeypatch.setattr(shim, "MAX_NEW_CHATS_PER_HOUR", 3)
    s = ScriptSession()
    r = make_router("gemini", s, sticky=False)           # every request opens a chat
    for i in range(3):
        step(r, "gemini", convo(i))
    with pytest.raises(shim.ProvidersFailed, match="new chats were started"):
        step(r, "gemini", convo(3))
    assert len(s.prompts) == 3
    r.clock.t += 3601                                    # an hour later there is room again
    step(r, "gemini", convo(4))
    assert len(s.prompts) == 4


def test_a_stale_shim_that_ignores_sigterm_is_still_replaced(monkeypatch):
    """ensure_shim(restart_stale=True) must not give up when the old shim is stuck in a browser call."""
    import subprocess
    import sys
    import time

    from freeharness.client import rpc
    code = "import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);print('up',flush=True);time.sleep(60)"
    p = subprocess.Popen([sys.executable, "-c", code, "freeharness.backends.shim"], stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline().strip() == b"up"
        monkeypatch.setattr(rpc, "shim_health", lambda timeout=1.0: {"pid": p.pid} if p.poll() is None else None)
        real_sleep = time.sleep
        monkeypatch.setattr(rpc.time, "sleep", lambda s: real_sleep(0.01))
        assert rpc._stop_stale_shim({"pid": p.pid}) is True
        assert p.wait(5) < 0                             # killed by a signal
    finally:
        p.kill()
        p.wait()


def test_the_new_chat_count_survives_a_shim_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(shim, "MAX_NEW_CHATS_PER_HOUR", 2)
    path = tmp_path / "chats.json"
    for _ in range(2):                                   # two shim processes, one chat each
        r = make_router("gemini", ScriptSession(), sticky=False, chats_path=path)
        step(r, "gemini", convo(0))
    r = make_router("gemini", ScriptSession(), sticky=False, chats_path=path)
    with pytest.raises(shim.ProvidersFailed, match="new chats were started"):
        step(r, "gemini", convo(1))
    r.wall = lambda: __import__("time").time() + 3700
    step(r, "gemini", convo(2))                          # an hour later it is allowed again


def test_a_vague_error_in_a_continued_chat_is_retried_in_the_same_chat_before_a_new_one_is_opened():
    vague = RuntimeError("Gemini returned an error page: I encountered an error doing what you asked. Could you try again?")
    s = ScriptSession([(CALL_TEXT, [BLOCK]), vague, (CALL_TEXT, [BLOCK])])
    r = make_router("gemini", s)
    step(r, "gemini", convo(0))
    step(r, "gemini", convo(1))                                   # errors once, then goes through in the SAME chat
    assert s.fresh_flags == [True, False, False] and len(s.prompts) == 3


def test_the_other_gemini_wording_is_an_error_too_not_an_answer():
    vague = RuntimeError("Gemini returned an error page: I seem to be encountering an error. Can I try something else for you?")
    s = ScriptSession([(CALL_TEXT, [BLOCK]), vague, (CALL_TEXT, [BLOCK])])
    r = make_router("gemini", s)
    step(r, "gemini", convo(0))
    step(r, "gemini", convo(1))
    assert s.fresh_flags == [True, False, False]
    from freeharness.backends import providers
    assert any("i seem to be encountering an error" in m for m in providers.GEMINI.errors)


def test_a_transient_error_is_retried_once_in_the_same_chat_and_never_opens_a_new_one():
    oops = RuntimeError("Gemini returned an error page: Sorry, something went wrong. Please try your request again.")
    s = ScriptSession([(CALL_TEXT, [BLOCK]), oops, (CALL_TEXT, [BLOCK])])
    r = make_router("gemini", s)
    step(r, "gemini", convo(0))
    step(r, "gemini", convo(1))
    assert s.fresh_flags == [True, False, False]
    s2 = ScriptSession([(CALL_TEXT, [BLOCK]), oops, oops, (CALL_TEXT, [BLOCK])])
    r2 = make_router("gemini", s2)
    step(r2, "gemini", convo(0))
    with pytest.raises(shim.ProvidersFailed):
        step(r2, "gemini", convo(1))
    assert s2.fresh_flags == [True, False, False]                 # two tries in the chat, no new chat


def test_a_vague_error_twice_then_opens_a_new_chat_with_the_compact_history():
    vague = RuntimeError("Gemini returned an error page: I encountered an error doing what you asked.")
    s = ScriptSession([(CALL_TEXT, [BLOCK]), vague, vague, (CALL_TEXT, [BLOCK])])
    r = make_router("gemini", s)
    step(r, "gemini", convo(0))
    step(r, "gemini", convo(1))
    assert s.fresh_flags == [True, False, False, True]


def test_chatgpt_is_told_where_its_tools_live_and_the_other_sites_are_not():
    """MEASURED live: ChatGPT said 'the gh_* tools aren't exposed in the current tool interface' and called nothing."""
    pre, rem = PROVIDERS["chatgpt"].tool_preamble, PROVIDERS["chatgpt"].tool_reminder
    assert "user's own computer" in pre and "tool_call" in pre and "Do not say that you lack access" in pre and rem
    sess = ScriptSession()
    r = shim.Router(["chatgpt"], lambda prov: sess, clock=Clock(), log=lambda m: None, sticky=["chatgpt"])
    step(r, "chatgpt", convo(0))
    step(r, "chatgpt", convo(1))
    assert sess.prompts[0].startswith(pre) and sess.fresh_flags[0] is True        # the first message of the chat
    assert sess.prompts[1].startswith(rem) and sess.fresh_flags[1] is False       # a continued one: the short reminder
    assert not sess.prompts[1].startswith(pre)
    g = ScriptSession()
    step(make_router("gemini", g), "gemini", convo(0))
    assert not g.prompts[0].startswith("IMPORTANT CONTEXT") and "user's own computer" not in g.prompts[0]
    assert shim.framed(PROVIDERS["chatgpt"], "x", True, None) == "x"             # no tools offered: nothing added


class AdoptSession(ScriptSession):
    """A page that kept answering after the client left: adopt() hands over what is already there."""
    last_before = 3

    def __init__(self, script=()):
        super().__init__(script)
        self.adopted, self.can_adopt = [], True

    def adoptable(self, before):
        return self.can_adopt

    def adopt(self, before, should_stop=None):
        self.adopted.append(before)
        self.blocks = [BLOCK]
        yield {"t": CALL_TEXT}


def _dropped_request(r, messages):
    """A request whose client leaves (dsh's idle timeout) while the site is still answering."""
    prompt = shim.render_prompt(messages, TOOLS_S)
    return list(r.run("gemini", prompt, should_stop=lambda: True, messages=messages, tools=TOOLS_S))


def test_a_dropped_request_is_adopted_by_its_retry_in_the_same_chat_and_never_opens_a_new_one():
    """MEASURED live 2026-10-28: dsh cut a request after 300s of silence; the retry opened a new ChatGPT chat."""
    s = AdoptSession()
    r = make_router("gemini", s)
    step(r, "gemini", convo(0))                                      # the chat exists
    assert _dropped_request(r, convo(1)) == [("provider", "gemini")]  # nothing was delivered to the client
    assert "gemini" in r.orphans and "gemini" not in r.threads
    events, calls = step(r, "gemini", convo(1))                      # dsh retries the same request
    assert s.adopted == [3] and len(s.prompts) == 2 and s.fresh_flags == [True, False]   # nothing was typed again
    assert calls and "gemini" in r.threads and not r.orphans         # the chat carries on
    events, calls = step(r, "gemini", convo(2))                      # and the next step is a normal continued message
    assert s.fresh_flags == [True, False, False]


def test_a_dropped_request_with_no_answer_on_the_page_is_sent_again_in_the_same_chat():
    s = AdoptSession()
    r = make_router("gemini", s)
    step(r, "gemini", convo(0))
    _dropped_request(r, convo(1))
    s.can_adopt = False
    step(r, "gemini", convo(1))
    assert s.adopted == [] and s.fresh_flags == [True, False, False]   # sent once more, still the same chat


def test_a_different_request_after_a_dropped_one_does_not_adopt_it():
    s = AdoptSession()
    r = make_router("gemini", s)
    step(r, "gemini", convo(0))
    _dropped_request(r, convo(1))
    step(r, "gemini", convo(0))                                      # not the dropped request
    assert s.adopted == [] and not r.orphans


def test_a_reply_that_never_starts_is_waited_for_in_the_same_chat_after_the_cooldown():
    """MEASURED live 2026-10-28: ChatGPT gave no reply start in 240s; dsh retried at once, got 'cooling down' and failed the
    stage. Now the message stays in its chat and the retry waits out the short cooldown, then takes over the answer."""
    s = AdoptSession()
    r = make_router("gemini", s)
    waited = []
    r.wait = lambda sec: (waited.append(sec), setattr(r.clock, "t", r.clock.t + sec))
    step(r, "gemini", convo(0))
    s.script = [TimeoutError("no reply started within 240s. Page state: url=https://site.test/c/1; composer holds 0 chars")]
    with pytest.raises(shim.ProvidersFailed):
        step(r, "gemini", convo(1))
    assert "gemini" in r.orphans
    events, calls = step(r, "gemini", convo(1))                       # dsh retries at once
    assert sum(waited) > 60 and s.adopted == [3]                      # it waited out the cooldown, then adopted
    assert len(s.prompts) == 2 and s.fresh_flags == [True, False] and calls   # nothing was typed again, no new chat


def test_without_a_chat_to_resume_a_cooling_provider_still_fails_at_once():
    s = ScriptSession()
    r = make_router("gemini", s)
    r.wait = lambda sec: pytest.fail("must not wait")
    r.until["gemini"] = r.clock() + 100
    with pytest.raises(shim.AllCoolingDown):
        step(r, "gemini", convo(0))
