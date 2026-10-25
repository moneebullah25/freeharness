#!/usr/bin/env python3
"""The eval's own correctness: every task's checker must REJECT the starting
workspace and ACCEPT a known solution, otherwise a task could pass (or fail)
without the agent doing anything. No model, no browser.

Run: .venv/bin/python tests/test_eval.py
"""


from freeharness.eval import run as ev  # noqa: E402
from freeharness.eval.tasks import BY_ID, TASKS  # noqa: E402
from freeharness.client.rpc import TurnResult  # noqa: E402

ANSWERS = {"read-answer": "The launch code is ORCHID-7731.", "count-todos": "3",
           "run-and-report": "It printed 5050."}
TOOLS = {"read-answer": "read", "write-file": "write", "fix-bug": "edit", "edit-config": "edit",
         "count-todos": "grep", "rename-function": "edit", "run-and-report": "bash",
         "write-and-run-test": "write"}


def result(text="", tools=(), ok=True):
    return TurnResult(ok, {"kind": "completed" if ok else "error"}, text, 1,
                      [(t, {}) for t in tools], 0, 1.0, [])


def fresh(task):
    return ev.materialize(task)


def test_every_task_has_a_unique_id_and_prompt():
    ids = [t.id for t in TASKS]
    assert len(ids) == len(set(ids)) == len(TASKS) >= 8
    assert all(t.prompt.strip() for t in TASKS)
    print("  task table .......... ok")


def test_checkers_reject_the_untouched_workspace():
    for t in TASKS:
        ws = fresh(t)
        # an agent that does nothing and says nothing useful
        ok, why = t.check(ws, result("I am done.", tools=[TOOLS[t.id]]))
        assert not ok, f"{t.id}: the untouched workspace passed ({why})"
    print("  reject do-nothing ... ok")


def test_checkers_accept_a_correct_solution():
    for t in TASKS:
        ws = fresh(t)
        t.solve(ws)
        ok, why = t.check(ws, result(ANSWERS.get(t.id, "done"), tools=[TOOLS[t.id]]))
        assert ok, f"{t.id}: a correct solution was rejected ({why})"
    print("  accept solution ..... ok")


def test_grade_requires_tool_use_and_a_completed_turn():
    t = BY_ID["read-answer"]
    ws = fresh(t)
    ok, why = ev.grade(t, ws, result("The launch code is ORCHID-7731.", tools=[]))
    assert not ok and why.startswith("no tool use"), "an answer without reading must fail"
    ok, why = ev.grade(t, ws, result("The launch code is ORCHID-7731.", tools=["read"], ok=False))
    assert not ok and "turn ended" in why
    ok, _ = ev.grade(t, ws, result("The launch code is ORCHID-7731.", tools=["read"]))
    assert ok
    print("  grade gates ......... ok")


def test_fix_bug_cannot_be_cheated_by_editing_the_test():
    t = BY_ID["fix-bug"]
    ws = fresh(t)
    (ws / "test_add.py").write_text('print("all tests passed")\n')      # neuter the test
    ok, why = t.check(ws, result("fixed", tools=["write"]))
    assert not ok and "cheating" in why
    print("  no test tampering ... ok")


def test_partial_and_overbroad_edits_fail():
    t = BY_ID["edit-config"]
    ws = fresh(t)
    (ws / "config.json").write_text('{"debug": true, "port": 9090, "name": "svc", "tags": ["a", "b"]}')
    assert not t.check(ws, result("", ["edit"]))[0], "changing debug as well must fail"
    t = BY_ID["rename-function"]
    ws = fresh(t)
    (ws / "mod.py").write_text("def compute_total(xs):\n    return sum(xs)\n")   # callers left broken
    assert not t.check(ws, result("", ["edit"]))[0], "a half-done rename must fail"
    t = BY_ID["count-todos"]
    assert not t.check(fresh(t), result("There are 3 or maybe 5 files."))[0], "hedged answers fail"
    print("  partial work fails .. ok")


def test_verdict_thresholds():
    ok = lambda b, d="": {"task": "x", "ok": b, "detail": d}
    assert ev.verdict([ok(True)] * 6 + [ok(False)] * 2)["eligible"], "6/8 = 75% meets the bar"
    assert not ev.verdict([ok(True)] * 5 + [ok(False)] * 3)["eligible"]
    v = ev.verdict([ok(True)] * 7 + [ok(False, "no tool use (needed one of ['read'])")])
    assert not v["eligible"] and v["answered_without_tools"], "answering without tools disqualifies"
    assert not ev.verdict([])["eligible"]
    # a task that could not run (provider rate-limited) is inconclusive, not a failure,
    # and a provider with unrun tasks is never declared eligible
    runs = [ok(True)] * 7 + [{"task": "y", "ok": False, "detail": "429 cooling down", "infra": True}]
    v = ev.verdict(runs)
    assert v["runs"] == 7 and v["passed"] == 7 and v["inconclusive"] == ["y"] and not v["eligible"]
    print("  verdict ............. ok")


def test_rate_limited_runs_are_waited_out_and_retried_once():
    calls = []
    limited = {"task": "t", "ok": False, "detail": "turn ended 429: every provider is cooling down"}
    good = {"task": "t", "ok": True, "detail": "fine"}
    assert ev.is_rate_limited(limited) and not ev.is_rate_limited(good)
    assert not ev.is_rate_limited({"task": "t", "ok": False, "detail": "wrong answer"}), \
        "a genuine failure must stay a failure"
    seq = iter([limited, good])
    orig = (ev.run_one, ev.wait_ready)
    ev.run_one = lambda *a, **k: (calls.append(1), next(seq))[1]
    ev.wait_ready = lambda *a, **k: True
    try:
        r = ev.run_with_patience(BY_ID["read-answer"], "gemini")
        assert r["ok"] and r["retried"] and len(calls) == 2 and not r.get("infra")
        seq = iter([limited, limited])
        calls.clear()
        r = ev.run_with_patience(BY_ID["read-answer"], "gemini")
        assert r.get("infra") and len(calls) == 2, "still limited after one retry => inconclusive"
    finally:
        ev.run_one, ev.wait_ready = orig
    print("  patience ............ ok")


def test_wait_ready_waits_for_a_cooldown_then_proceeds():
    states = iter([{"providers": {"gemini": {"ready": False, "cooldown_s": 5, "reason": "x"}}},
                   {"providers": {"gemini": {"ready": True, "cooldown_s": 0, "reason": ""}}}])
    orig = ev.shim_health
    ev.shim_health = lambda *a, **k: next(states)
    slept = []
    try:
        assert ev.wait_ready("gemini", sleep=slept.append, log=lambda m: None)
        assert slept == [7], slept
        ev.shim_health = lambda *a, **k: {"providers": {"gemini": {"ready": False, "cooldown_s": 900, "reason": "x"}}}
        assert not ev.wait_ready("gemini", cap=30, sleep=slept.append, log=lambda m: None), \
            "must give up instead of waiting past the cap"
    finally:
        ev.shim_health = orig
    print("  wait_ready .......... ok")


if __name__ == "__main__":
    print("eval self-check")
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("all passed")
