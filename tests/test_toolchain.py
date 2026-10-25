"""
The standard toolchain venv (freeharness.toolchain), its place in the bash sandbox (data/fh_sandbox_runner.py via
security.py) and the doctor row. Offline: the 'toolchain' is built from a local wheelhouse of tiny fake wheels, and the
real thing (pinned pytest, ruff, ... from PyPI) is only exercised by running `freeharness toolchain install` by hand.
"""
import json
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

from freeharness import cli, doctor, security, toolchain

from test_ghenv import needs_bwrap

RUNNER_DIR = str(security.RUNNER.parent)


def runner_module():
    sys.path.insert(0, RUNNER_DIR)
    try:
        import fh_sandbox_runner
        return fh_sandbox_runner
    finally:
        sys.path.pop(0)


@pytest.fixture
def small_toolchain(tmp_path, wheelhouse, monkeypatch):
    """A real venv built from the wheelhouse that plays the standard toolchain: one tool, `fhtool-cli`."""
    tc = tmp_path / "tc"
    toolchain.install(say=lambda *_: None, dest=tc, requirements=["fhtool==1.0"], pip_args=["--no-index", "--find-links", str(wheelhouse)])
    monkeypatch.setenv("FH_TOOLCHAIN", str(tc))
    monkeypatch.setattr(toolchain, "PINNED", [("fhtool", "1.0", None, None)])
    monkeypatch.setattr(toolchain, "COMMANDS", ["python", "python3", "pip", "fhtool-cli"])
    monkeypatch.setattr(toolchain, "RUN_CHECK", ("fhtool-cli",))
    return tc


# ============================================================================ pins and status

def test_pins_cover_every_tool_once_per_python_line_and_are_exact():
    for py in ((3, 10), (3, 11), (3, 12), (3, 14)):
        want = toolchain._expected(py)
        assert {"pip", "setuptools", "wheel", "build", "pytest", "coverage", "ruff", "flake8", "black", "isort", "mypy",
                "pre-commit"} <= set(want), py
    names_310 = [n for n, _, lo, hi in toolchain.PINNED if toolchain._applies(lo, hi, (3, 10))]
    assert len(names_310) == len(set(names_310)), "no tool is pinned twice for one Python"
    assert toolchain._expected((3, 10))["pre-commit"] != toolchain._expected((3, 12))["pre-commit"]
    lines = toolchain.requirement_lines()
    assert all("==" in ln for ln in lines) and any('python_version >= "3.11"' in ln for ln in lines)
    assert not any(c in "".join(lines) for c in "<>~") or all("python_version" in ln for ln in lines if any(c in ln for c in "<>~"))


def test_status_is_honest_about_missing_partial_and_full(tmp_path):
    assert toolchain.status(tmp_path / "nope")[0] == "missing"
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert toolchain.status(venv)[0] == "partial" and "did not finish" in toolchain.status(venv)[1]
    sp = venv / "lib" / "python3.12" / "site-packages"
    sp.mkdir(parents=True)
    (venv / toolchain.MANIFEST).write_text(json.dumps({"python": [3, 12]}))
    level, detail, _ = toolchain.status(venv)
    assert level == "partial" and "absent" in detail and "bin/pytest" in detail
    for n, v in toolchain._expected((3, 12)).items():
        (sp / f"{n.replace('-', '_')}-{v}.dist-info").mkdir()
    for c in toolchain.COMMANDS:
        (venv / "bin" / c).write_text("")
    level, detail, shown = toolchain.status(venv)
    assert level == "full" and shown["pytest"] == toolchain._expected((3, 12))["pytest"] and "pytest" in detail
    (sp / f"pytest-{toolchain._expected((3, 12))['pytest']}.dist-info").rename(sp / "pytest-1.0.dist-info")
    level, detail, _ = toolchain.status(venv)
    assert level == "partial" and "pytest 1.0" in detail, "a version that is not the pin is not 'full'"


def test_install_builds_a_venv_rebuilds_it_and_cleans_up_after_a_failure(tmp_path, wheelhouse):
    tc = tmp_path / "tc"
    args = ["--no-index", "--find-links", str(wheelhouse)]
    toolchain.install(say=lambda *_: None, dest=tc, requirements=["fhtool==1.0"], pip_args=args)
    meta = json.loads((tc / toolchain.MANIFEST).read_text())
    assert meta["python"] == list(sys.version_info[:2]) and meta["pinned"] == ["fhtool==1.0"]
    assert (tc / "bin" / "python").exists() and (tc / "bin" / "fhtool-cli").exists()
    (tc / "stale").write_text("x")
    toolchain.install(say=lambda *_: None, dest=tc, requirements=["fhtool==1.0"], pip_args=args)
    assert not (tc / "stale").exists()
    with pytest.raises(RuntimeError, match="pip install failed"):
        toolchain.install(say=lambda *_: None, dest=tc, requirements=["no-such-tool==9"], pip_args=args)
    assert not tc.exists(), "a failed install leaves no half-built toolchain"
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(RuntimeError, match="symlink"):
        toolchain.install(say=lambda *_: None, dest=link, requirements=["fhtool==1.0"], pip_args=args)
    assert tmp_path.exists()


def test_cli_dispatch_and_exit_codes(tmp_path, monkeypatch, capsys):
    assert "toolchain" in cli.SUBCOMMANDS
    monkeypatch.setenv("FH_TOOLCHAIN", str(tmp_path / "none"))
    assert cli.main(["toolchain", "status"]) == 1
    assert "missing" in capsys.readouterr().out
    assert cli.main(["toolchain", "bogus"]) == 2


# ============================================================================ the runner binds it read-only

def test_runner_binds_the_toolchain_read_only_and_puts_it_first_on_path(tmp_path):
    r = runner_module()
    tc = tmp_path / "tc"
    (tc / "bin").mkdir(parents=True)
    (tc / "pyvenv.cfg").write_text("x")
    node = tmp_path / "node"
    (node / "bin").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    prof = ["--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--die-with-parent", "--tmpfs", "/tmp",  # noqa: S108
            "--bind", str(ws), str(ws), "--", "bash", "-c", "true"]
    env = {"FH_TOOLCHAIN": str(tc), "FH_SANDBOX_TOOLS": str(node), "PATH": "/usr/bin:/bin"}
    argv = r.build(prof, str(ws), "/home/someone", env, "/usr/bin/bwrap")
    assert "--unshare-net" in argv, "still no network"
    assert any(argv[i:i + 3] == ["--ro-bind", str(tc), str(tc)] for i in range(len(argv)))
    assert any(argv[i:i + 3] == ["--ro-bind", str(node), str(node)] for i in range(len(argv)))
    path = argv[argv.index("PATH") + 1]
    assert path.split(":")[:3] == [f"{tc}/bin", f"{node}/bin", "/usr/bin"]
    assert argv.count("--bind") == 1, "the workspace is still the only writable bind"
    plain = r.build(prof, str(ws), "/home/someone", {"PATH": "/usr/bin"}, "/usr/bin/bwrap")
    assert "PATH" not in plain and "--ro-bind" in plain


def test_runner_never_binds_home_tmp_root_or_a_missing_dir(tmp_path):
    r = runner_module()
    home = str(Path.home())
    env = {"FH_TOOLCHAIN": home, "FH_SANDBOX_TOOLS": f"/:{home}:/tmp:{os.path.dirname(home)}:{tmp_path}/missing:/proc"}
    assert r.tool_paths(env, home) == ([], [])
    fake = tmp_path / "tc"
    fake.mkdir()
    assert r.tool_paths({"FH_TOOLCHAIN": str(fake)}, home) == ([], []), "a directory without pyvenv.cfg is not a toolchain"


def test_dsh_env_announces_the_toolchain_only_when_it_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("FH_TOOLCHAIN", str(tmp_path / "none"))
    assert "FH_TOOLCHAIN" not in security.dsh_env() and "FH_SANDBOX_RUNNER" in security.dsh_env()
    tc = tmp_path / "tc"
    tc.mkdir()
    (tc / "pyvenv.cfg").write_text("x")
    monkeypatch.setenv("FH_TOOLCHAIN", str(tc))
    assert security.dsh_env()["FH_TOOLCHAIN"] == str(tc)


@needs_bwrap
def test_the_sandbox_gets_python_and_the_tools_read_only_without_home_or_network(small_toolchain, tmp_path):
    if security.probe().level == "unavailable":
        pytest.skip("bubblewrap cannot create a sandbox here")
    tc = small_toolchain
    ws = tmp_path / "ws"
    ws.mkdir()
    canary = Path.home() / ".fh-toolchain-canary"
    canary.write_text("secret")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    try:
        r = security._run_confined(
            f"command -v python python3 fhtool-cli; python --version; fhtool-cli; python -c 'import fhtool; print(fhtool.VALUE)'; "
            f"cat {canary} 2>/dev/null && echo HOME-VISIBLE; echo x > {tc}/bin/evil 2>/dev/null && echo TC-WRITABLE; "
            f"exec 3<>/dev/tcp/127.0.0.1/{srv.getsockname()[1]} 2>/dev/null && echo NET-OPEN; echo done", ws)
    finally:
        canary.unlink()
        srv.close()
    out = r.stdout
    assert f"{tc}/bin/python\n" in out and f"{tc}/bin/python3\n" in out and f"{tc}/bin/fhtool-cli\n" in out
    assert "Python 3." in out and "fhtool 1.0" in out and "42" in out
    assert "HOME-VISIBLE" not in out and "TC-WRITABLE" not in out and "NET-OPEN" not in out and out.rstrip().endswith("done")


@needs_bwrap
def test_a_tool_root_under_home_is_exposed_read_only_and_nothing_else_of_home(tmp_path, monkeypatch):
    if security.probe().level == "unavailable":
        pytest.skip("bubblewrap cannot create a sandbox here")
    from freeharness.client import rpc
    root = Path(os.environ["HOME"]) / f".fh-tools-test-{os.getpid()}"
    (root / "node" / "bin").mkdir(parents=True)
    tool = root / "node" / "bin" / "mynode"
    tool.write_text("#!/bin/sh\necho mynode ok\n")
    tool.chmod(0o755)
    (root / "other").mkdir()
    (root / "other" / "secret").write_text("s")
    monkeypatch.setattr(rpc, "find_node_dir", lambda: str(root / "node" / "bin"))
    monkeypatch.setenv("FH_TOOLCHAIN", str(tmp_path / "none"))
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        assert security.tool_roots() == [str(root / "node")]
        r = security._run_confined(f"mynode; cat {root}/other/secret 2>/dev/null && echo LEAK; echo x > {root}/node/w 2>/dev/null && echo WRITABLE; echo end", ws)
    finally:
        shutil.rmtree(root)
    assert "mynode ok" in r.stdout and "LEAK" not in r.stdout and "WRITABLE" not in r.stdout and "end" in r.stdout


@needs_bwrap
def test_common_commands_in_the_bash_sandbox_are_the_ones_the_host_has(tmp_path):
    """Whatever lives on a system path stays available; this documents which of the usual ones the host provides."""
    if security.probe().level == "unavailable":
        pytest.skip("bubblewrap cannot create a sandbox here")
    ws = tmp_path / "ws"
    ws.mkdir()
    names = ["git", "grep", "sed", "awk", "find", "tree", "make", "jq", "curl", "bash", "sh", "diff", "patch", "tar", "gzip"]
    r = security._run_confined("for c in " + " ".join(names) + "; do command -v $c >/dev/null && echo has:$c; done", ws)
    have = {ln[4:] for ln in r.stdout.splitlines() if ln.startswith("has:")}
    on_host = {n for n in names if shutil.which(n, path="/usr/bin:/bin:/usr/local/bin")}
    assert have == on_host, (have, on_host)
    assert {"git", "grep", "sed", "awk", "find", "bash"} <= have


# ============================================================================ doctor

@needs_bwrap
def test_probe_and_doctor_report_missing_partial_and_full_from_real_runs(small_toolchain, tmp_path, monkeypatch):
    if security.probe().level == "unavailable":
        pytest.skip("bubblewrap cannot create a sandbox here")
    level, detail = security.probe_toolchain()
    assert level == "full" and "in the bash sandbox" in detail, detail
    row = [r for r in doctor.checks() if r[1].startswith("standard toolchain")][0]
    assert row[0] == "ok" and row[1] == "standard toolchain: full"
    monkeypatch.setattr(toolchain, "COMMANDS", ["python", "python3", "pip", "fhtool-cli", "ruff"])      # a tool that is not there
    level, detail = security.probe_toolchain()
    assert level == "partial" and "ruff" in detail
    row = [r for r in doctor.checks() if r[1].startswith("standard toolchain")][0]
    assert row[0] == "warn" and row[1] == "standard toolchain: partial"
    monkeypatch.setenv("FH_TOOLCHAIN", str(tmp_path / "gone"))
    assert security.probe_toolchain()[0] == "missing"
    row = [r for r in doctor.checks() if r[1].startswith("standard toolchain")][0]
    assert row[0] == "warn" and row[1] == "standard toolchain: missing" and "freeharness toolchain install" in row[2]


def test_runner_puts_the_project_environment_first_so_bare_pytest_finds_the_project_deps(tmp_path):
    r = runner_module()
    ws = tmp_path / "ws"
    (ws / "proj").mkdir(parents=True)
    (ws / "proj-venv" / "bin").mkdir(parents=True)
    (ws / "proj-venv" / "bin" / "python").write_text("")
    prof = ["--ro-bind", "/", "/", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(ws), str(ws), "--", "bash", "-c", "true"]  # noqa: S108
    argv = r.build(prof, str(ws), "/home/someone", {"PATH": "/usr/bin:/bin"}, "/usr/bin/bwrap")
    assert argv[argv.index("PATH") + 1].split(":")[0] == f"{ws}/proj-venv/bin"
    assert argv[argv.index("PYTHONPATH") + 1] == str(ws / "proj")
    bare = tmp_path / "bare"
    bare.mkdir()
    prof2 = ["--ro-bind", "/", "/", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(bare), str(bare), "--", "bash", "-c", "true"]  # noqa: S108
    none = r.build(prof2, str(bare), "/home/someone", {"PATH": "/usr/bin"}, "/usr/bin/bwrap")
    assert "PYTHONPATH" not in none
