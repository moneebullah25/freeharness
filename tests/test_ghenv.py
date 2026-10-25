"""
gh_branch and gh_setup_env (freeharness.mcp.ghgate), offline: local bare repos, the fake GitHub of test_ghgate,
and a LOCAL WHEELHOUSE (`pip --no-index --find-links`) instead of PyPI. Nothing here touches github.com or pypi.org.

What is NOT tested offline: pip talking to a real index. The network-enabled sandbox is tested for what it does and
refuses (a loopback listener on the host stands in for "the network is reachable"), and the pip command line is
checked, but a download from pypi.org is only exercised by `freeharness toolchain install` run by hand.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from freeharness import depfiles, security, toolchain
from freeharness.mcp import ghgate as g

from test_ghgate import World, git, refused  # noqa: F401  (helpers shared with the gate tests)

RUNNER = str(security.RUNNER)
needs_bwrap = pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="needs bubblewrap on Linux")

PYPROJECT = """\
[project]
name = "proj"
version = "0.1"
requires-python = ">=3.10"
dependencies = [
  "fhdep>=1.0",
  "fhnever; python_version < '3.0'",   # a marker that is false here: pip skips it
]

[project.optional-dependencies]
test = ["fhtest==1.0", "proj[extra1]"]
extra1 = ["fhextra"]
docs = ["fhdocs-must-not-install"]

[dependency-groups]
lint = ["fhlint"]
ci = [{include-group = "lint"}, "fhci"]
"""
REQS_DEV = """\
# development requirements
fhdev==1.0 --hash=sha256:abc123
-e .
-e git+https://github.com/x/y.git#egg=y
git+https://example.org/z.git
--index-url http://evil.example/simple
--extra-index-url https://pypi.example.net/simple
pkg @ https://evil.example/pkg.whl
./local-dir
../escape
-r ../outside.txt
-c constraints.txt
"""


@pytest.fixture
def toolvenv(tmp_path, wheelhouse, monkeypatch):
    """A real (tiny) 'standard toolchain' venv built from the wheelhouse, found through FH_TOOLCHAIN."""
    tc = tmp_path / "tc"
    toolchain.install(say=lambda *_: None, dest=tc, requirements=["fhtool==1.0"], pip_args=["--no-index", "--find-links", str(wheelhouse)])
    monkeypatch.setenv("FH_TOOLCHAIN", str(tc))
    return tc


@pytest.fixture
def w(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "fh"))
    world = World(tmp_path)
    yield world
    world.close()


def seed(w, files: dict, msg="add project files"):
    """Commit `files` to the upstream project (and the fork), before anyone clones."""
    work = w.tmp / f"seed-{time.monotonic_ns()}"
    git("clone", str(w.base / "up/proj.git"), str(work))
    for name, text in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git("add", "-A", cwd=work)
    git("commit", "-m", msg, cwd=work)
    git("push", "origin", "main", cwd=work)
    git("push", str(w.base / "me/proj.git"), "main", cwd=work)
    return work


def project_files():
    return {"pyproject.toml": PYPROJECT, "requirements-dev.txt": REQS_DEV, "requirements/docs.txt": "fhdocs-must-not-install\n",
            "src/proj/__init__.py": "X = 1\n", "tests/test_x.py": "def test_x():\n    assert True\n"}


# ============================================================================ gh_branch

def test_branch_creates_a_branch_from_a_fresh_upstream_default(w):
    gate = w.gate()
    clone = w.cloned(gate)
    seed(w, {"src/new.py": "NEW = 1\n"}, "upstream moves on")            # upstream advances after the clone
    up_tip = git("rev-parse", "main", cwd=w.base / "up/proj.git").strip()
    out = gate.gh_branch(str(clone), "fix/12-empty-list")
    assert "fix/12-empty-list" in out and up_tip[:12] in out
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone).strip() == "fix/12-empty-list"
    assert git("rev-parse", "HEAD", cwd=clone).strip() == up_tip, "the branch starts at upstream's CURRENT tip, not the clone-time one"
    assert (clone / "src/new.py").exists()
    acts = [json.loads(ln) for ln in w.gate()._actions_path().read_text().splitlines()]
    assert any(a["event"] == "branch" and a["branch"] == "fix/12-empty-list" for a in acts)


def test_branch_is_not_a_github_write_so_it_needs_no_human_and_works_in_dry_run(w):
    gate = w.gate(dryrun=True, confirm_sock="", confirm_token="")
    clone = w.cloned(w.gate())
    assert "on new branch fix/a" in gate.gh_branch(str(clone), "fix/a")
    assert w.fake.writes == []


@pytest.mark.parametrize("name,why", [
    ("main", "default branch"), ("MAIN", "default branch"), ("HEAD", "default branch"), ("@", "invalid"),
    ("", "invalid"), ("-x", "invalid"), ("a b", "invalid"), ("a;rm -rf x", "invalid"), ("a..b", "invalid"), ("a//b", "invalid"),
    ("a/", "invalid"), ("x.lock", "invalid"), ("fix/.hidden", "invalid"), ("refs/heads/x", "invalid"), ("upstream/main", "invalid"),
    ("origin/x", "invalid"), ("a@{1}", "invalid"), ("a\nb", "invalid"), ("x" * 300, "invalid"), ("é", "invalid"),
])
def test_branch_refuses_the_default_branch_and_unsafe_names(w, name, why):
    gate = w.gate()
    clone = w.cloned(gate)
    assert why in refused(gate.gh_branch, str(clone), name)
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone).strip() == "main", "nothing was checked out"


def test_branch_refuses_a_dirty_tree_an_existing_branch_and_foreign_folders(w, tmp_path):
    gate = w.gate()
    clone = w.cloned(gate)
    (clone / "README.md").write_text("changed\n")
    assert "not clean" in refused(gate.gh_branch, str(clone), "fix/b")
    git("checkout", "--", "README.md", cwd=clone)
    (clone / "untracked.txt").write_text("x")
    assert "not clean" in refused(gate.gh_branch, str(clone), "fix/b")
    (clone / "untracked.txt").unlink()
    gate.gh_branch(str(clone), "fix/b")
    git("checkout", "main", cwd=clone)
    assert "already exists" in refused(gate.gh_branch, str(clone), "fix/b")
    other = w.ws / "plain"
    other.mkdir()
    assert "not created by gh_clone" in refused(gate.gh_branch, str(other), "fix/c")
    assert "not inside" in refused(gate.gh_branch, "/etc", "fix/c")


def test_branch_refuses_a_clone_whose_config_defines_filters(w):
    gate = w.gate()
    clone = w.cloned(gate)
    git("config", "filter.evil.smudge", "touch /tmp/should-never-exist", cwd=clone)
    assert "filters" in refused(gate.gh_branch, str(clone), "fix/d")


def test_branch_runs_no_hooks_and_ignores_the_clones_ssh_command(w, tmp_path):
    gate = w.gate()
    clone = w.cloned(gate)
    marker = tmp_path / "hook-ran"
    for hook in ("post-checkout", "reference-transaction", "pre-auto-gc"):
        h = clone / ".git" / "hooks" / hook
        h.write_text(f"#!/bin/sh\ntouch {marker}\n")
        h.chmod(0o755)
    git("config", "core.sshCommand", f"touch {marker}; false", cwd=clone)
    git("config", "core.fsmonitor", f"touch {marker}", cwd=clone)
    gate.gh_branch(str(clone), "fix/e")
    assert not marker.exists()


def test_branch_fetches_only_the_default_branch_from_the_registered_url_not_the_clones_config(w):
    gate = w.gate()
    clone = w.cloned(gate)
    git("remote", "set-url", "upstream", "/nonexistent/evil.git", cwd=clone)         # a doctored remote
    seed(w, {"x.txt": "1\n"})
    tip = git("rev-parse", "main", cwd=w.base / "up/proj.git").strip()
    gate.gh_branch(str(clone), "fix/f")
    assert git("rev-parse", "HEAD", cwd=clone).strip() == tip


# ============================================================================ the install sandbox (real bwrap, no PyPI)

def run_net(args, env=None, timeout=60):
    return subprocess.run([sys.executable, RUNNER, "--net-install", *args], capture_output=True, text=True, timeout=timeout,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", ""), **(env or {})})


def test_net_mode_argv_keeps_the_network_and_removes_everything_else(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(RUNNER).parent))
    try:
        import fh_sandbox_runner as r
    finally:
        sys.path.pop(0)
    venv = tmp_path / "v"
    venv.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    wh = tmp_path / "wh"
    wh.mkdir()
    argv = r.build_net([str(venv), "--hide", str(ws), "--ro", str(wh), "--setenv", "HTTPS_PROXY", "http://p:3128", "--", "pip", "install"],
                       "/home/someone", "/usr/bin/bwrap")
    assert "--unshare-net" not in argv, "this mode exists to reach the index"
    assert "--clearenv" in argv and "--die-with-parent" in argv and "--new-session" in argv
    assert argv[argv.index("--bind") + 1:argv.index("--bind") + 3] == [str(venv), str(venv)]
    assert argv.count("--bind") == 1, "the install directory is the ONLY writable bind"
    assert any(argv[i:i + 3] == ["--ro-bind", str(wh), str(wh)] for i in range(len(argv)))
    tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert "/tmp" in tmpfs and str(ws) in tmpfs  # noqa: S108
    assert not os.path.isdir("/run") or "/run" in tmpfs
    assert any(argv[i:i + 3] == ["--setenv", "HTTPS_PROXY", "http://p:3128"] for i in range(len(argv)))
    assert argv[-2:] == ["pip", "install"]
    std = r.build(["--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--die-with-parent", "--",
                   "bash", "-c", "true"], str(ws), "/home/someone", {}, "/usr/bin/bwrap")
    assert "--unshare-net" in std, "the normal bash sandbox is unchanged: no network"


UNSAFE_DIRS = [["/"], ["relative/dir"], ["/nonexistent-dir-xyz"], ["/tmp"],  # noqa: S108
               [os.path.expanduser("~")], [os.path.dirname(os.path.expanduser("~"))]]


@pytest.mark.parametrize("bad", UNSAFE_DIRS)
def test_net_mode_refuses_unsafe_install_directories(bad):
    p = run_net([*bad, "--", "true"])
    assert p.returncode != 0 and "freeharness-sandbox:" in p.stderr


def test_net_mode_refuses_a_symlinked_directory_binding_home_and_unknown_options(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert "freeharness-sandbox:" in run_net([str(link), "--", "true"]).stderr
    assert "refusing to bind" in run_net([str(real), "--ro", "/", "--", "true"]).stderr
    assert "unknown --net-install option" in run_net([str(real), "--bogus", "--", "true"]).stderr
    assert "no -- separator" in run_net([str(real)]).stderr


def test_the_runner_fails_closed_without_bwrap(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    for argv in (["--net-install", str(real), "--", "true"],
                 ["--ro-bind", "/", "/", "--", "true"]):
        p = subprocess.run([sys.executable, RUNNER, *argv], capture_output=True, text=True,
                           env={"PATH": str(tmp_path / "empty"), "HOME": str(tmp_path)})
        assert p.returncode == 126 and "freeharness-sandbox:" in p.stderr and "refusing to run unconfined" in p.stderr


@needs_bwrap
def test_net_sandbox_really_has_the_network_but_nothing_else(tmp_path, monkeypatch):
    import socket
    import threading
    if security.probe().level == "unavailable":
        pytest.skip("bubblewrap cannot create a sandbox here")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    hits = []

    def accept():
        srv.settimeout(10)
        try:
            while True:
                c, _ = srv.accept()
                hits.append(1)
                c.close()
        except OSError:
            return
    threading.Thread(target=accept, daemon=True).start()
    port = srv.getsockname()[1]
    venv = tmp_path / "ws" / "v"
    venv.mkdir(parents=True)
    clone = tmp_path / "ws" / "clone"
    clone.mkdir()
    (clone / "secret.txt").write_text("clone-secret")
    home = Path(os.environ["HOME"])
    canary = home / ".fh-net-canary"
    canary.write_text("home-secret")
    try:
        script = (f"exec 3<>/dev/tcp/127.0.0.1/{port} && echo NET=yes; "
                  f"cat {clone}/secret.txt 2>/dev/null && echo CLONE=visible; "
                  f"cat {canary} 2>/dev/null && echo HOME=visible; "
                  f"echo x > /usr/fh-write 2>/dev/null && echo USR=writable; "
                  f"echo hi > {venv}/ok.txt && echo VENV=writable; "
                  f"echo $SECRET_TOKEN-$GITHUB_TOKEN; ls /tmp | wc -l; echo HOME=$HOME")
        p = run_net([str(venv), "--hide", str(tmp_path / "ws"), "--", "bash", "-c", script],
                    env={"SECRET_TOKEN": "s3cr3t", "GITHUB_TOKEN": "ghp_x"})
    finally:
        canary.unlink()
        srv.close()
    out = p.stdout
    assert "NET=yes" in out and hits, p.stderr
    assert "CLONE=visible" not in out and "clone-secret" not in out
    assert "HOME=visible" not in out and "home-secret" not in out
    assert "USR=writable" not in out
    assert "VENV=writable" in out and (venv / "ok.txt").read_text() == "hi\n"
    assert "s3cr3t" not in out and "ghp_x" not in out, "the environment is cleared"
    assert "HOME=/tmp/fh-home" in out


@needs_bwrap
def test_the_normal_bash_sandbox_still_has_no_network(tmp_path):
    import socket
    if security.probe().level == "unavailable":
        pytest.skip("bubblewrap cannot create a sandbox here")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        r = security._run_confined(f"exec 3<>/dev/tcp/127.0.0.1/{srv.getsockname()[1]}", ws)
    finally:
        srv.close()
    assert r.returncode != 0


def test_run_capped_kills_on_timeout_and_bounds_the_output(w):
    gate = w.gate()
    t0 = time.monotonic()
    rc, text = gate._run_capped([sys.executable, "-c", "import time; print('start', flush=True); time.sleep(60)"], 1.5, dict(os.environ))
    assert rc == -9 and "start" in text and time.monotonic() - t0 < 15
    rc, text = gate._run_capped([sys.executable, "-c", "import sys; sys.stdout.write('x' * 3_000_000); print('THE-END')"], 30, dict(os.environ))
    assert rc == 0 and len(text) < 9000 and "THE-END" in text and "bytes of output omitted" in text


# ============================================================================ gh_setup_env

def cloned_project(w, **cfg):
    seed(w, project_files())
    gate = w.gate(**cfg)
    return gate, w.cloned(gate)


def test_setup_env_fails_closed_without_bwrap_and_runs_nothing(w, monkeypatch):
    gate, clone = cloned_project(w, pip_find_links=str(w.tmp / "none"))
    monkeypatch.setattr(g.shutil, "which", lambda *a, **k: None)
    spawned = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: (spawned.append(cmd), real_popen(cmd, *a, **k))[1])
    msg = refused(gate.gh_setup_env, str(clone))
    assert "bubblewrap" in msg and "nothing was installed" in msg
    assert all(c[0] == "git" for c in spawned), spawned            # only git ran: no runner, no venv, no pip
    assert not (w.ws / "proj-venv").exists()
    monkeypatch.setattr(sys, "platform", "darwin")
    assert "bubblewrap" in refused(gate.gh_setup_env, str(clone))


def test_setup_env_refuses_unregistered_folders_and_bad_extras(w):
    gate, clone = cloned_project(w)
    other = w.ws / "plain"
    other.mkdir()
    assert "not created by gh_clone" in refused(gate.gh_setup_env, str(other))
    for bad in ("a;b", "../x", "x" * 80, ",".join(f"e{i}" for i in range(11)), "$(id)"):
        assert "extras" in refused(gate.gh_setup_env, str(clone), bad), bad


def test_setup_env_install_list_comes_from_upstream_files_and_skips_unsafe_lines(w, wheelhouse, monkeypatch):
    gate, clone = cloned_project(w, pip_find_links=str(wheelhouse))
    (clone / "requirements-dev.txt").write_text("evil-package-added-by-the-agent\n")      # the working tree is not the source
    (clone / "pyproject.toml").write_text("[project]\nname='x'\ndependencies=['evil-too']\n")
    top = [x for x in git("ls-tree", "--name-only", "refs/remotes/upstream/main", cwd=clone).split("\n") if x]
    sub = [x for x in git("ls-tree", "-r", "--name-only", "refs/remotes/upstream/main", "--", "requirements/", cwd=clone).split("\n") if x]
    col = depfiles.collect(top, sub, gate._tree_reader(clone, "refs/remotes/upstream/main"), set())
    assert sorted(col.reqs) == sorted(["fhdep>=1.0", "fhnever; python_version < '3.0'", "fhtest==1.0", "fhextra", "fhlint", "fhci",
                                       "fhdev==1.0"])
    assert not any("evil" in r or "docs" in r for r in col.reqs)
    blob = " | ".join(col.skipped)
    for must in ("-e .", "git+https://github.com/x/y.git", "git+https://example.org/z.git", "--index-url http://evil.example/simple",
                 "--extra-index-url https://pypi.example.net/simple", "pkg @ https://evil.example/pkg.whl", "./local-dir", "../escape",
                 "-r ../outside.txt", "-c constraints.txt"):
        assert must in blob, (must, blob)


@needs_bwrap
def test_setup_env_builds_a_working_venv_through_the_install_sandbox(w, wheelhouse, toolvenv):
    if security.probe().level == "unavailable":
        pytest.skip("bubblewrap cannot create a sandbox here")
    gate, clone = cloned_project(w, pip_find_links=str(wheelhouse), env_timeout=240)
    out = gate.gh_setup_env(str(clone), "")
    venv = w.ws / "proj-venv"
    assert venv.is_dir() and not venv.is_symlink()
    assert "environment ready" in out and "built" in out and f"{venv}/bin/python" in out
    prefix = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={clone}/src:{clone} {venv}/bin/python -m"
    assert prefix in out, out
    assert "skipped" in out and "-e ." in out and "http://evil.example" in out
    req = (venv / ".fh-requirements.txt").read_text()
    assert "fhdep>=1.0" in req and "fhdev==1.0" in req and "evil" not in req and "docs" not in req and "git+" not in req
    # what the model will actually do: run it in its own (no network, no HOME) bash sandbox
    py = f"{venv}/bin/python"
    r = security._run_confined(f"cd {clone} && {prefix} fhdep && PYTHONPATH={clone}/src {py} -c 'import fhdep, fhtool, proj; print(fhdep.VALUE, proj.X)'", w.ws)
    assert r.returncode == 0, r.stderr
    assert "main of __main__" in r.stdout and "42 1" in r.stdout, "project deps, the standard toolchain (via .pth) and the clone on PYTHONPATH"
    r2 = security._run_confined("command -v python python3; python --version", w.ws)
    assert r2.returncode == 0 and str(toolvenv / "bin") in r2.stdout, "the sandbox has `python` from the toolchain"
    # the second call changes nothing
    stamp = (venv / ".fh-requirements.txt").stat().st_mtime_ns
    assert "unchanged, reused" in gate.gh_setup_env(str(clone), "")
    assert (venv / ".fh-requirements.txt").stat().st_mtime_ns == stamp
    # the docs group is only installed when asked for
    out2 = gate.gh_setup_env(str(clone), "docs")
    assert "built" in out2 and "fhdocs-must-not-install" in (venv / ".fh-requirements.txt").read_text()


@needs_bwrap
def test_setup_env_failure_leaves_nothing_behind_and_times_out(w, wheelhouse, toolvenv):
    if security.probe().level == "unavailable":
        pytest.skip("bubblewrap cannot create a sandbox here")
    seed(w, {"requirements.txt": "this-package-does-not-exist==9.9\n"})
    gate = w.gate(pip_find_links=str(wheelhouse), env_timeout=240)
    clone = w.cloned(gate)
    msg = refused(gate.gh_setup_env, str(clone))
    assert "pip install" in msg and "nothing was left behind" in msg
    assert not (w.ws / "proj-venv").exists()
    # a hung install is killed at the deadline
    slow = w.gate(pip_find_links=str(wheelhouse), env_timeout=3)
    t0 = time.monotonic()
    rc, _ = slow._net_sandbox(w.ws, ["sleep", "30"], time.monotonic() + 2, [], [])
    assert rc == -9 and time.monotonic() - t0 < 20


def test_setup_env_will_not_touch_a_symlink_or_a_registered_clone(w, wheelhouse):
    gate, clone = cloned_project(w, pip_find_links=str(wheelhouse))
    (w.ws / "proj-venv").symlink_to(w.tmp)
    assert "not a virtualenv made by this tool" in refused(gate.gh_setup_env, str(clone))
    assert w.tmp.exists()


def test_the_pip_command_never_takes_an_index_from_the_repo(w, wheelhouse, monkeypatch):
    gate, clone = cloned_project(w, pip_index="https://pypi.org/simple")
    seen = []
    monkeypatch.setattr(gate, "_net_sandbox", lambda venv, cmd, deadline, ro, hide: (seen.append((cmd, ro, hide)), (0, ""))[1])
    monkeypatch.setattr(g.shutil, "which", lambda *a, **k: "/usr/bin/bwrap")
    monkeypatch.setattr(g.security, "_run_confined", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "no sandbox in this test"))
    try:
        gate.gh_setup_env(str(clone))
    except g.Refuse:
        pass
    pip = [c for c, _, _ in seen if "pip" in c][0]
    assert pip[pip.index("--index-url") + 1] == "https://pypi.org/simple"
    assert "--isolated" in pip and "--no-cache-dir" in pip and "--prefer-binary" in pip and "-e" not in pip
    assert all("evil" not in a and "git+" not in a for a in pip)
    assert pip.count("-r") == 1 and pip[pip.index("-r") + 1].endswith(".fh-requirements.txt")
    assert seen[0][2] == [str(w.ws)], "the whole work folder (clone included) is hidden from the install sandbox"


# ============================================================================ tool list and protocol

def test_the_two_new_tools_are_listed_with_short_descriptions():
    names = [t["name"] for t in g.TOOLS]
    assert names[:8] == ["gh_repo_info", "gh_search", "gh_get_file", "gh_list_pr_files", "gh_pr_threads", "gh_fork", "gh_clone", "gh_branch"]
    assert "gh_setup_env" in names and len(names) == 14
    for t in g.TOOLS:
        assert len(t["description"]) <= 100, t["name"]
    assert len(json.dumps(g.TOOLS)) < 5000
    props = {t["name"]: t["inputSchema"] for t in g.TOOLS}
    assert props["gh_branch"]["required"] == ["repo_dir", "name"] and props["gh_setup_env"]["required"] == ["repo_dir"]


def test_call_tool_reports_refusals_as_tool_errors(w):
    gate = w.gate()
    res = g.call_tool(gate, "gh_branch", {"repo_dir": "/etc", "name": "x"})
    assert res["isError"] and "REFUSED" in res["content"][0]["text"]
    res = g.call_tool(gate, "gh_setup_env", {"repo_dir": "/etc"})
    assert res["isError"]
    assert g.call_tool(gate, "gh_branch", {"repo_dir": "x"})["isError"]            # missing argument
