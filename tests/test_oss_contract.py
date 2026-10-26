"""The hunt stage asks for evidence that a candidate breaks what the PROJECT says (not misuse of its API)."""
from freeharness.eval import oss_stage as st

PASSING = {"documented": "pass", "valid_input": "pass", "quote_real": "pass", "intent_checked": "pass"}


def sec(*lines):
    return {"text": "## Candidate 1: x\n" + "\n".join(lines) + "\n"}


def status(items):
    return {i["id"]: i["status"] for i in items}


def clone(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text('def f(xs):\n    """Return the mean of a list of numbers."""\n    return sum(xs) / len(xs)\n')
    return tmp_path


FULL = ("- Documented: pkg/a.py:2 (the docstring)", "- Quote: Return the mean of a list of numbers.",
        "- Intent checked: pkg/a.py, no comment says an empty list is deliberate",
        "- Valid input because: the signature and the docstring example both pass a list")


def test_a_cited_file_a_real_quote_an_intent_file_and_a_reason_pass(tmp_path):
    assert status(st.contract_items(sec(*FULL), clone(tmp_path))) == PASSING


def test_a_bare_file_name_is_found_and_a_url_is_accepted(tmp_path):
    c = clone(tmp_path)
    s = sec("- Documented: a.py", "- Valid input because: it is a plain supported call here")
    assert status(st.contract_items(s, c))["documented"] == "pass"
    s = sec("- Documented: https://example.org/docs/flatten", "- Valid input because: the docs show this very call")
    assert status(st.contract_items(s, c))["documented"] == "pass"


def test_missing_lines_a_file_that_does_not_exist_and_a_one_word_reason_fail(tmp_path):
    allfail = {"documented": "fail", "valid_input": "fail", "quote_real": "fail", "intent_checked": "fail"}
    assert status(st.contract_items(sec("- Where: a.py:1"), tmp_path)) == allfail
    got = st.contract_items(sec("- Documented: nowhere/made_up.py:3", "- Valid input because: yes"), tmp_path)
    assert status(got) == allfail and "no file that exists" in got[0]["evidence"]
    assert status(st.contract_items(None, None)) == allfail


def test_an_invented_quote_fails_but_one_found_in_a_tool_result_passes_for_a_url(tmp_path):
    c = clone(tmp_path)
    fake = sec("- Documented: pkg/a.py:2", "- Quote: Returns the median of any iterable, even empty ones.",
               "- Intent checked: pkg/a.py", "- Valid input because: the docstring says so, at least in my head")
    got = st.contract_items(fake, c)
    assert status(got)["quote_real"] == "fail" and "not in the cited file" in got[2]["evidence"]
    url = sec("- Documented: https://github.com/o/r/issues/7", "- Quote: crashes on an empty   list when nesting",
              "- Intent checked: pkg/a.py", "- Valid input because: the issue reporter does exactly this call")
    assert status(st.contract_items(url, c))["quote_real"] == "fail"
    seen = [{"text": "#7 [open] x\n--- body ---\nIt crashes on an empty list when nesting happens."}]
    assert status(st.contract_items(url, c, seen))["quote_real"] == "pass"
    short = sec("- Documented: pkg/a.py", "- Quote: mean", "- Intent checked: pkg/a.py", "- Valid input because: a plain list call")
    assert status(st.contract_items(short, c))["quote_real"] == "fail"


def test_intent_must_name_a_file_that_exists(tmp_path):
    c = clone(tmp_path)
    s = sec(FULL[0], FULL[1], "- Intent checked: I looked around and it seems fine", FULL[3])
    assert status(st.contract_items(s, c))["intent_checked"] == "fail"
    s = sec(FULL[0], FULL[1], "- Intent checked: nothing_here/zzz.py", FULL[3])
    assert status(st.contract_items(s, c))["intent_checked"] == "fail"


def test_issue_items_need_a_read_open_unassigned_issue():
    read = lambda n, text, err=False: {"name": "gh_issue_read", "args": {"number": n}, "text": text, "isError": err}  # noqa: E731
    good = [read(7, "#7 [open] Crash\nlabels: bug; assigned to: nobody; opened by @x")]
    assert status(st.issue_items(good, sec("- Issue: #7"))) == {"issue_chosen": "pass"}
    assert status(st.issue_items(good, sec("- Where: a.py"))) == {"issue_chosen": "fail"}
    assert "not read" in st.issue_items(good, sec("- Issue: #8"))[0]["evidence"]
    assert "assigned to someone" in st.issue_items([read(7, "#7 [open] C\nassigned to: @bob")], sec("- Issue: 7"))[0]["evidence"]
    assert "not open" in st.issue_items([read(7, "#7 [closed] C\nassigned to: nobody")], sec("- Issue: #7"))[0]["evidence"]
    assert status(st.issue_items([read(7, "REFUSED", err=True)], sec("- Issue: #7"))) == {"issue_chosen": "fail"}


def test_the_hunt_prompt_asks_for_the_lines_and_calls_misuse_not_a_bug():
    from freeharness import oss_flow
    p = oss_flow.PROMPTS["hunt"]
    for line in ("`Documented:`", "`Quote:`", "`Intent checked:`", "`Valid input because:`", "misuse, not a bug"):
        assert line in p, line
