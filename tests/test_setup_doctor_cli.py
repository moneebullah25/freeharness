import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from freeharness import cli, doctor, paths, setup_cmd


def _tarball(dest: Path, name: str, extra: dict | None = None):
    top = name[: -len(".tar.xz")]
    with tarfile.open(dest, "w:xz") as t:
        for rel, data in {f"{top}/bin/node": b"#!/bin/sh\necho v24.22.1\n", **(extra or {})}.items():
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            ti.mode = 0o755
            t.addfile(ti, io.BytesIO(data))


def _fake_download(monkeypatch, sha_ok: bool, extra=None):
    name = f"node-v{setup_cmd.NODE_VERSION}-{setup_cmd._OS[__import__('platform').system()]}-" \
           f"{setup_cmd._ARCH[__import__('platform').machine().lower()]}.tar.xz"

    def dl(url, dest):
        if url.endswith("SHASUMS256.txt"):
            tb = dest.parent / name
            _tarball(tb, name, extra)
            digest = hashlib.sha256(tb.read_bytes()).hexdigest() if sha_ok else "0" * 64
            dest.write_text(f"{digest}  {name}\n")
        else:
            _tarball(dest, name, extra)
    monkeypatch.setattr(setup_cmd, "_download", dl)


def test_node_install_verifies_checksum_and_installs(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path))
    _fake_download(monkeypatch, sha_ok=True)
    bindir = setup_cmd.install_node(say=lambda *_: None)
    assert (bindir / "node").is_file() and bindir == paths.data_dir() / "node" / "bin"


def test_node_install_refuses_a_tampered_download(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path))
    _fake_download(monkeypatch, sha_ok=False)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        setup_cmd.install_node(say=lambda *_: None)
    assert not (paths.data_dir() / "node").exists(), "nothing may be installed on a bad checksum"


def test_node_install_rejects_path_traversal_members(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path / "home"))
    _fake_download(monkeypatch, sha_ok=True, extra={"../../escape.txt": b"x"})
    with pytest.raises(Exception):  # noqa: B017  (tarfile.FilterError subclasses; any refusal is correct)
        setup_cmd.install_node(say=lambda *_: None)
    assert not (tmp_path / "escape.txt").exists()


def test_doctor_runs_and_reports_are_safe_to_share(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FREEHARNESS_HOME", str(tmp_path))
    rc = doctor.main(["--report"])
    out = capsys.readouterr().out
    assert rc == 0 and "freeharness diagnostics" in out
    assert str(Path.home()) not in out.replace(str(paths.log_dir()), "")
    rows = doctor.checks()
    assert all(level in ("ok", "warn", "FAIL") for level, _, _ in rows)


def test_cli_dispatch(capsys):
    assert cli.main(["version"]) == 0
    assert "freeharness " in capsys.readouterr().out
    assert set(cli.SUBCOMMANDS) >= {"setup", "login", "doctor", "serve", "eval", "version"}
