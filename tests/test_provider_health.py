"""Error classes (provider_error vs too_long), provider_health.json, `providers status`, and failover preference.
Offline: fake sessions, fake clocks, tmp dirs only."""
import json

import pytest

from freeharness import providers_cmd
from freeharness.backends import shim
from freeharness.backends.health import Health
from freeharness.backends.limits import Limits
from freeharness.backends.providers import PROVIDERS
from test_shim import BLOCK, TOOLS_S, Clock, convo
from test_shim_chats import CALL_TEXT, ScriptSession, step

PAGE = RuntimeError("Gemini returned an error page: I encountered an error doing what you asked. Could you try again?")
HARD = RuntimeError("Gemini returned an error page: I'm having a hard time fulfilling your request.")


def run_all(r, model, messages):
    return list(r.run(model, shim.render_prompt(messages, TOOLS_S), messages=messages, tools=TOOLS_S))


@pytest.mark.parametrize("err", [PAGE, HARD])
def test_error_page_on_a_small_prompt_is_a_provider_error_and_leaves_limits_json_alone(tmp_path, err):
    lp = tmp_path / "limits.json"
    s = ScriptSession([err, err, err])
    h = Health(tmp_path / "provider_health.json")
    r = shim.Router(["gemini"], lambda p: s, clock=Clock(), log=lambda m: None, sticky=["gemini"],
                    limits=Limits(lp), health=h, chats_path=tmp_path / "chats.json")
    with pytest.raises(shim.ProvidersFailed) as ei:
        run_all(r, "gemini", convo(0))
    assert "too long" not in str(ei.value)                      # not the size-limit message
    assert not lp.exists()                                      # nothing learned, nothing written
    assert r.limits.snapshot("gemini")["suspect_max_chars"] is None
    assert h.data["gemini"]["last_error_class"] == "provider_error"
    assert json.loads((tmp_path / "provider_health.json").read_text())["gemini"]["last_error_class"] == "provider_error"


def test_a_big_fresh_prompt_with_the_same_page_is_still_too_long(tmp_path):
    s = ScriptSession([PAGE, PAGE, PAGE])
    r = shim.Router(["gemini"], lambda p: s, clock=Clock(), log=lambda m: None, limits=Limits(tmp_path / "l.json"),
                    chats_path=tmp_path / "c.json")
    prov = PROVIDERS["gemini"]
    assert r.error_class(prov, PAGE, 5_000, True) == "provider_error"
    assert r.error_class(prov, PAGE, 200_000, True) == "too_long"
    r.limits.good("gemini", 300_000)                            # answered a bigger one before: size is not the cause
    assert r.error_class(prov, PAGE, 200_000, True) == "provider_error"
    assert r.error_class(prov, RuntimeError("conversation is too long"), 100, True) == "too_long"


def test_health_file_is_written_atomically_and_tracks_success_stall_and_outcomes(tmp_path):
    path = tmp_path / "sub" / "provider_health.json"
    t = [100.0]
    h = Health(path, wall=lambda: t[0])
    h.success("qwen")
    h.outcome("qwen", "t")
    h.outcome("qwen", "n")
    h.error("qwen", "stalled", 1800)
    data = json.loads(path.read_text())["qwen"]
    assert data["last_success"] == 100.0 and data["stalled_count"] == 1 and data["cooldown_until"] == 1900.0
    assert data["tool_replies"] == 1 and data["nudged_replies"] == 1
    assert not list(path.parent.glob("*.tmp"))
    assert Health(path).score("qwen") == 0.5 and Health(path).score("unknown") == 0.5


def test_router_records_tool_replies_nudges_and_stalls(tmp_path):
    prose = ("I will now look at the files.", [])
    s = ScriptSession([(CALL_TEXT, [BLOCK]), prose, (CALL_TEXT, [BLOCK]), prose, prose])
    h = Health()
    r = shim.Router(["chatgpt"], lambda p: s, clock=Clock(), log=lambda m: None, sticky=["chatgpt"], health=h)
    step(r, "chatgpt", convo(0))
    step(r, "chatgpt", convo(1))                                   # prose, nudge, then a call
    assert h.data["chatgpt"]["tool_replies"] == 1 and h.data["chatgpt"]["nudged_replies"] == 1
    with pytest.raises(shim.ProvidersFailed):
        step(r, "chatgpt", convo(2))                               # prose twice: Stalled
    assert h.data["chatgpt"]["stalled_replies"] == 1 and h.data["chatgpt"]["last_error_class"] == "stalled"


def test_auto_failover_prefers_the_better_record_but_an_explicit_order_is_kept():
    boom = RuntimeError("something broke")
    sess = {"gemini": ScriptSession([boom]), "qwen": ScriptSession(), "chatgpt": ScriptSession()}
    h = Health()
    for _ in range(4):
        h.outcome("chatgpt", "t")
        h.outcome("qwen", "s")
    r = shim.Router(["gemini", "qwen", "chatgpt"], lambda p: sess[p.key], clock=Clock(), log=lambda m: None, health=h)
    events = run_all(r, "auto", convo(0))
    assert [v for k, v in events if k == "provider"] == ["gemini", "chatgpt"]     # chatgpt jumped ahead of qwen
    with pytest.raises(shim.ProvidersFailed):                                      # explicit: gemini alone, no failover
        run_all(shim.Router(["gemini", "qwen", "chatgpt"], lambda p: ScriptSession([boom]), clock=Clock(),
                            log=lambda m: None, health=h), "gemini", convo(0))
    assert r.candidates("gemini") == ["gemini"]


def test_providers_status_reports_budget_cooldown_error_and_stall(tmp_path):
    now = 10_000.0
    (tmp_path / "chats.json").write_text(json.dumps({"gemini": [now - 10, now - 100, now - 5000], "qwen": []}))
    (tmp_path / "provider_health.json").write_text(json.dumps({
        "gemini": {"last_error_class": "provider_error", "cooldown_until": now + 90, "tool_replies": 3,
                   "nudged_replies": 1, "stalled_replies": 1, "stalled_count": 1, "last_stalled_at": now - 60,
                   "recent": ["t", "t", "t", "n", "s"]}}))
    rows = {r["provider"]: r for r in providers_cmd.collect(tmp_path, now, 6)}
    g = rows["gemini"]
    assert (g["chats_used"], g["chat_cap"], g["cooldown_s"], g["last_error_class"]) == (2, 6, 90, "provider_error")
    assert g["stalled_recently"] and g["tool_replies"] == 3 and g["nudged_or_stalled"] == 2 and g["score"] == 0.6
    assert rows["qwen"]["last_error_class"] == "-" and not rows["qwen"]["stalled_recently"]
    assert "deepseek" not in rows
    lines = []
    assert providers_cmd.main(["status"], state_dir=tmp_path, now=now, out=lines.append) == 0
    assert "gemini" in lines[0] and "2/6" in lines[0] and "provider_error" in lines[0]
    assert providers_cmd.main([]) == 2
