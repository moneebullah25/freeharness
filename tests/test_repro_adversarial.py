"""Every fake or broken repro a free model produced in a real run, as a fixture: the hunt's repro check must keep refusing each one
and keep accepting a genuine repro. (Runs: ormar 20261101-*, sqlparse/Python-Markdown 20261029-*.)"""
import pytest

from freeharness.eval import oss_stage as st

TB = "Traceback (most recent call last):\n  File \"r.py\", line 4, in <module>\n{exc}\n[exit code: 1]"
SEC = {"text": "Expected: works\nActual: AssertionError: FAIL"}


@pytest.fixture
def clone(tmp_path):
    (tmp_path / "mylib").mkdir()
    (tmp_path / "mylib" / "__init__.py").write_text("")
    return tmp_path


def w(content, path="scratch/r.py", n=1):
    return {"n": n, "name": "write", "args": {"file_path": path, "content": content}}


def bash(n, cmd, out, exit_=1):
    return {"n": n, "name": "bash", "args": {"command": cmd}, "text": out, "exit": exit_, "isError": False}


CASES = {
    "print_only_placeholder": ([bash(1, "python3 -c 'print(\"repro run executed\")'", "repro run executed\n", 0)], False),
    "echo": ([bash(1, "echo hello", "hello\n", 0)], False),
    "script_raises_its_own_assertion": (
        [w("raise AssertionError('FAIL: x')"), bash(2, "python3 scratch/r.py", TB.format(exc="AssertionError: FAIL: x"))], False),
    "verbatim_copy_of_the_function": (
        [w('"""copied verbatim from\nmylib/utils.py lines 1-9"""\ndef f(x):\n    return x[0]\nf([])'),
         bash(2, "python3 scratch/r.py", TB.format(exc="IndexError: list index out of range"))], False),
    "prose_that_says_from_package": (
        [bash(1, "python3 - <<'E'\n# verbatim from\nmylib/x.py\nraise ValueError('FAIL')\nE", TB.format(exc="ValueError: FAIL"))], False),
    "environment_not_built": (
        [w("import mylib"), bash(2, "python3 scratch/r.py", TB.format(exc="ModuleNotFoundError: No module named 'sqlalchemy'"))], False),
    "api_misuse": (
        [w("import mylib"), bash(2, "python3 scratch/r.py", TB.format(exc="TypeError: f() got an unexpected keyword argument 'x'"))], False),
    "genuine_repro": (
        [w("import mylib\nmylib.f([])"), bash(2, "python3 scratch/r.py", TB.format(exc="IndexError: list index out of range"))], True),
    "genuine_inline_repro": (
        [bash(1, "python3 -c 'import mylib; mylib.f([])'", TB.format(exc="IndexError: list index out of range"))], True),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_repro_check_on_a_real_failure_pattern(name, clone):
    calls, accepted = CASES[name]
    sec = {"text": "Expected: a value\nActual: IndexError: list index out of range"} if accepted else SEC
    items, repro = st.find_repro(calls, sec, clone)
    assert (repro is not None) is accepted, (name, [(i["id"], i["evidence"]) for i in items])
