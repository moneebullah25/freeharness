"""The hunt stage asks for evidence that a candidate breaks what the PROJECT says (not misuse of its API)."""
from freeharness.eval import oss_stage as st


def sec(*lines):
    return {"text": "## Candidate 1: x\n" + "\n".join(lines) + "\n"}


def status(items):
    return {i["id"]: i["status"] for i in items}


def test_a_cited_file_in_the_clone_and_a_reason_pass(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    s = sec("- Documented: pkg/a.py:1 (the docstring says it accepts a list)",
            "- Valid input because: the signature and the docstring example both pass a list")
    assert status(st.contract_items(s, tmp_path)) == {"documented": "pass", "valid_input": "pass"}


def test_a_bare_file_name_is_found_and_a_url_is_accepted(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    s = sec("- Documented: a.py", "- Valid input because: it is a plain supported call here")
    assert status(st.contract_items(s, tmp_path))["documented"] == "pass"
    s = sec("- Documented: https://example.org/docs/flatten", "- Valid input because: the docs show this very call")
    assert status(st.contract_items(s, tmp_path))["documented"] == "pass"


def test_missing_lines_a_file_that_does_not_exist_and_a_one_word_reason_fail(tmp_path):
    assert status(st.contract_items(sec("- Where: a.py:1"), tmp_path)) == {"documented": "fail", "valid_input": "fail"}
    got = st.contract_items(sec("- Documented: nowhere/made_up.py:3", "- Valid input because: yes"), tmp_path)
    assert status(got) == {"documented": "fail", "valid_input": "fail"} and "no file that exists" in got[0]["evidence"]
    assert status(st.contract_items(None, None)) == {"documented": "fail", "valid_input": "fail"}


def test_the_hunt_prompt_asks_for_both_lines_and_calls_misuse_not_a_bug():
    from freeharness import oss_flow
    p = oss_flow.PROMPTS["hunt"]
    assert "`Documented:`" in p and "`Valid input because:`" in p and "misuse, not a bug" in p
