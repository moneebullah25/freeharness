from freeharness import preflight as pf

NOW = 1_000_000.0


class SB:
    def __init__(self, level, detail="d"):
        self.level, self.detail = level, detail


def levels(rows):
    return [r[0] for r in rows]


def test_a_sandbox_that_is_not_full_refuses_the_run():
    rows = pf.check(["qwen"], sandbox=SB("unavailable", "bwrap: uid map"), chats={}, status={"qwen": True}, now=NOW)
    bad = pf.failures(rows)
    assert len(bad) == 1 and "bash sandbox: unavailable" in bad[0][1] and "AppArmor" in bad[0][2]
    assert not pf.failures(pf.check(["qwen"], sandbox=SB("full"), chats={}, status={"qwen": True}, now=NOW))


def test_chat_budget_warns_on_one_provider_at_the_cap_and_fails_only_when_all_are():
    chats = {"gemini": [NOW - 100 * i for i in range(1, 7)], "qwen": [NOW - 50]}
    rows = pf.chat_budget(["gemini", "qwen"], chats, NOW, 6)
    assert pf.failures(rows) == [] and any("gemini: new-chat cap reached" in r[1] for r in rows)
    rows = pf.chat_budget(["gemini"], chats, NOW, 6)
    assert len(pf.failures(rows)) == 1
    old = {"gemini": [NOW - 4000] * 6}                        # older than an hour: no longer counted
    assert pf.failures(pf.chat_budget(["gemini"], old, NOW, 6)) == [] and pf.chat_budget(["gemini"], old, NOW, 6)[0][2] == "0/6"


def test_cap_zero_means_no_cap_and_auto_is_not_a_provider():
    assert pf.chat_budget(["gemini"], {"gemini": [NOW - 1] * 50}, NOW, 0)[0][0] == "ok"
    assert pf.chat_budget(["auto"], {}, NOW, 6) == []


def test_unsigned_models_are_a_warning_not_a_stop():
    rows = pf.sign_ins(["qwen", "gemini", "auto"], {"qwen": False, "gemini": True})
    assert levels(rows) == ["warn"] and "qwen" in rows[0][1]
    assert pf.sign_ins(["qwen"], {"qwen": None}) == []          # unreadable cookie store: no claim either way


def test_render_has_one_line_per_row():
    out = pf.render([("ok", "a", "x"), ("FAIL", "b", "")])
    assert out.splitlines() == ["  [ok  ] a: x", "  [FAIL] b"]


# ------------------------------------------------------------------ the import probe of gh_setup_env

import types  # noqa: E402

from freeharness import security  # noqa: E402
from freeharness.mcp import ghgate  # noqa: E402


def _gate(tmp_path, pyproject):
    cls = next(v for v in vars(ghgate).values() if isinstance(v, type) and hasattr(v, "_import_probe"))
    fake = types.SimpleNamespace(_tree_reader=lambda rd, ref: (lambda p: pyproject), _log=lambda *a, **k: None,
                                 cfg=types.SimpleNamespace(workdir=tmp_path))
    return cls, fake


def test_import_probe_reports_a_failing_import_as_an_environment_problem(tmp_path, monkeypatch):
    (tmp_path / "mylib").mkdir()
    (tmp_path / "mylib" / "__init__.py").write_text("")
    cls, fake = _gate(tmp_path, '[project]\nname = "mylib"\n')
    seen = {}

    def run(cmd, cwd, timeout=0):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback\nModuleNotFoundError: No module named 'sqlalchemy'")
    monkeypatch.setattr(security, "_run_confined", run)
    out = cls._import_probe(fake, tmp_path, "ref", "/v/bin/python", [str(tmp_path)])
    assert "import mylib" in seen["cmd"] and out.startswith("WARNING: `import mylib` FAILS") and "sqlalchemy" in out
    assert "do NOT edit the project" in out


def test_import_probe_says_ok_and_stays_quiet_without_a_package(tmp_path, monkeypatch):
    (tmp_path / "mylib").mkdir()
    (tmp_path / "mylib" / "__init__.py").write_text("")
    cls, fake = _gate(tmp_path, '[project]\nname = "mylib"\n')
    monkeypatch.setattr(security, "_run_confined", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert cls._import_probe(fake, tmp_path, "ref", "/v/bin/python", [str(tmp_path)]).startswith("import check: `import mylib` works")
    empty = tmp_path / "e"
    empty.mkdir()
    assert cls._import_probe(fake, empty, "ref", "/v/bin/python", [str(empty)]) == ""
