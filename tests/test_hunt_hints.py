"""hunt_hints: deterministic starting points from a tiny local git repo; the hunt prompt line; `oss hints`."""
import subprocess

from freeharness import hunt_hints as hh
from freeharness import oss_cmd, oss_flow
from freeharness.eval import oss_judge as oj

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
       "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent"}


def git(d, *a):
    subprocess.run(["git", "-C", str(d), "-c", "commit.gpgsign=false", *a], check=True, capture_output=True, env=ENV)


def commit(d, files, msg):
    for name, text in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(d, "add", "-A")
    git(d, "commit", "-m", msg)


def build(tmp_path):
    git(tmp_path, "init", "-q")
    commit(tmp_path, {"pkg/core.py": "x=1\n" * 50, "pkg/util.py": "y=1\n", "tests/test_util.py": "import util\n",
                      "poetry.lock": "a", "migrations/0001.py": "a=1\n", "docs/conf.py": "a=1\n",
                      "api/handler.py": "z=1\n" * 20}, "init")
    for i in range(3):
        commit(tmp_path, {"pkg/core.py": f"x={i}\n" * 50, "poetry.lock": str(i), "migrations/0001.py": f"a={i}\n"}, f"tweak {i}")
    commit(tmp_path, {"api/handler.py": "z=2\n" * 20}, "Fix crash in handler")
    return tmp_path


def test_is_source_excludes_tests_docs_vendor_generated_migrations_lockfiles():
    for f in ("tests/test_a.py", "docs/conf.py", "vendor/x.py", "migrations/0001.py", "poetry.lock", "a/b_pb2.py",
              "web/app.min.js", "README.md", "conftest.py"):
        assert not hh.is_source(f), f
    assert hh.is_source("src/pkg/core.py")


def test_hints_rank_churn_testgap_and_bugfix_deterministically(tmp_path):
    d = build(tmp_path)
    hints = hh.hints_for_clone(str(d))
    paths = [h["path"] for h in hints]
    assert paths[0] == "pkg/core.py" and "4 commits" in hints[0]["reason"]
    assert "api/handler.py" in paths and "bug-fix" in next(h for h in hints if h["path"] == "api/handler.py")["reason"]
    assert not any(p.startswith(("migrations", "docs", "tests")) or p.endswith(".lock") for p in paths)
    assert hints == hh.hints_for_clone(str(d)) and len(hints) <= 5


def test_open_prs_drop_directories_with_two_or_more(tmp_path):
    d = build(tmp_path)
    prs = [{"title": "a", "files": ["pkg/other.py"]}, {"title": "b", "files": ["pkg/more.py"]}]
    paths = [h["path"] for h in hh.hints_for_clone(str(d), prs)]
    assert not any(p.startswith("pkg/") for p in paths) and "api/handler.py" in paths
    one = [h["path"] for h in hh.hints_for_clone(str(d), prs[:1])]
    assert "pkg/core.py" in one


def test_limit_and_non_git_dir(tmp_path):
    assert hh.safe_hints(str(tmp_path)) == []
    files = [f"m{i}/mod{i}.py" for i in range(9)]
    out = hh.compute_hints([], [], files, {f: 10 for f in files}, {})
    assert len(out) == 5


def test_prompt_line_text_and_hunt_prompt(tmp_path):
    line = hh.prompt_line([{"path": "pkg/core.py", "reason": "4 commits"}])
    assert line == "Suggested starting points (pick one or ignore them): `pkg/core.py` (4 commits)."
    assert hh.prompt_line([]) == ""
    rd = oj.RunDir(tmp_path / "run")
    rd.workspace.mkdir(parents=True, exist_ok=True)
    state = oss_flow.new_state(rd, "up/proj", "", False)
    state["hints"] = [{"path": "pkg/core.py", "reason": "4 commits"}]
    f = oss_flow.Flow(rd, state, None, None, ["auto"], 100, 10.0, None, lambda s: None, {})
    assert line in f.render(oss_flow.BY_ID["hunt"])
    state["hints"] = []
    assert "Suggested starting points" not in f.render(oss_flow.BY_ID["hunt"])


def test_cli_needs_clone_and_prints(tmp_path, capsys):
    assert oss_cmd.main(["hints", "o/n"]) == 2
    assert "--clone" in capsys.readouterr().err
    d = build(tmp_path)
    assert oss_cmd.main(["hints", "o/n", "--clone", str(d)]) == 0
    assert "`pkg/core.py`" in capsys.readouterr().out
