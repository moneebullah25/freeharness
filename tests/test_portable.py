"""Where data lives, and that it survives odd paths, relocation and read-only homes."""
import importlib
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from freeharness import auth, logs, paths


@pytest.fixture
def home(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="fh ünï 日本 "))
    monkeypatch.setenv("FREEHARNESS_HOME", str(base))
    yield base
    shutil.rmtree(base, ignore_errors=True)


def test_everything_lives_under_one_overridable_root(home):
    root = home.resolve()
    for p in (paths.data_dir(), paths.config_dir(), paths.state_dir(), paths.chrome_profile(),
              paths.dsh_home(), paths.harness_dir(), paths.log_dir(), paths.token_file()):
        assert root in p.parents or p == root, p


def test_paths_with_spaces_and_non_ascii_work(home):
    tok = auth.load_or_create_token()
    assert auth.load_or_create_token() == tok
    d = paths.private_dir(paths.chrome_profile())
    assert d.is_dir() and stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.token_file().stat().st_mode) == 0o600


def test_moving_the_data_directory_keeps_the_login_and_token(home):
    tok = auth.load_or_create_token()
    (paths.private_dir(paths.chrome_profile()) / "marker").write_text("session")
    new = Path(tempfile.mkdtemp(prefix="moved "))
    try:
        shutil.rmtree(new)
        shutil.move(str(home), str(new))
        os.environ["FREEHARNESS_HOME"] = str(new)
        assert auth.load_or_create_token() == tok
        assert (paths.chrome_profile() / "marker").read_text() == "session"
    finally:
        shutil.rmtree(new, ignore_errors=True)


def test_defaults_follow_xdg_on_linux(monkeypatch, tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("linux only")
    monkeypatch.delenv("FREEHARNESS_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    assert paths.data_dir() == tmp_path / "d" / "freeharness"
    assert paths.token_file() == tmp_path / "c" / "freeharness" / "shim-token"
    assert paths.log_dir() == tmp_path / "s" / "freeharness" / "logs"


@pytest.mark.skipif(os.geteuid() == 0 if hasattr(os, "geteuid") else True, reason="root ignores modes")
def test_unwritable_state_dir_falls_back_to_stderr_instead_of_crashing(home):
    ro = home / "state"
    ro.mkdir(parents=True)
    ro.chmod(0o500)
    try:
        import logging
        logging.getLogger("fh-ro").handlers.clear()
        log = logs.get_logger("fh-ro")
        log.info("still works")              # must not raise
    finally:
        ro.chmod(0o700)


def test_diagnostics_contain_no_home_paths_or_tokens(home):
    tok = auth.load_or_create_token()
    env = logs.environment()
    blob = repr(env)
    assert str(Path.home()) not in blob and tok not in blob
    assert set(env) == {"freeharness", "python", "os", "arch"}


def test_package_data_is_present():
    root = Path(importlib.import_module("freeharness").__file__).parent
    assert (root / "data" / "dsh.patch.yml").is_file()
    assert (root / "data" / "harness" / "package-lock.json").is_file()
