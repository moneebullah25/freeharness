"""depfiles: declared dependency files -> a vetted flat requirement list, nothing from the repository is executed."""
import pytest

from freeharness import depfiles as d

PYPROJECT = '''\
[build-system]
requires = ["setuptools>=68"]          # never read

[project]
name = "My_Proj"
dynamic = ["version"]
dependencies = [
    "requests>=2.0",                   # a comment
    'rich >= 13',
    "colorama; sys_platform == 'win32'",
    "numpy>=1.20,<2 ; python_version >= \\"3.9\\"",
]

[project.optional-dependencies]
dev = ["my-proj[test,lint]", "pre-commit"]
test = ["pytest>=8", "pytest-cov"]
lint = [
    "ruff",
    "black==24.1.0",
]
docs = ["sphinx"]
postgres = ["asyncpg"]

[dependency-groups]
typing = ["mypy", {include-group = "test"}]
test = ["pytest-asyncio"]
cycle-a = [{include-group = "cycle-b"}]
cycle-b = [{include-group = "cycle-a"}, "cyc"]

[tool.ruff]
line-length = 100
'''


def collect(files: dict, extras=()):
    root = [f for f in files if "/" not in f]
    sub = [f for f in files if f.startswith("requirements/")]
    return d.collect(root, sub, files.get, set(extras))


def test_pyproject_default_groups_only_and_the_project_itself_is_never_installed():
    c = collect({"pyproject.toml": PYPROJECT})
    assert c.reqs == ["requests>=2.0", "rich >= 13", "colorama; sys_platform == 'win32'", 'numpy>=1.20,<2 ; python_version >= "3.9"',
                      "pytest>=8", "pytest-cov", "ruff", "black==24.1.0", "pre-commit", "mypy", "pytest-asyncio"]
    assert not any(r.startswith(("my-proj", "my_proj", "sphinx", "asyncpg", "setuptools")) for r in c.reqs)
    assert c.project == "my-proj"
    assert c.sources == ["pyproject.toml[dependencies, optional-dependencies.dev, optional-dependencies.test, "
                         "optional-dependencies.lint, dependency-groups.typing, dependency-groups.test]"]


def test_extras_select_more_groups_and_unknown_ones_are_reported():
    c = collect({"pyproject.toml": PYPROJECT}, ["docs", "Postgres", "nope"])
    assert "sphinx" in c.reqs and "asyncpg" in c.reqs
    assert any("nope" in n for n in c.notes) and not any("docs" in n for n in c.notes)


def test_include_group_cycles_terminate():
    c = collect({"pyproject.toml": PYPROJECT}, ["cycle-a"])
    assert c.reqs.count("cyc") == 1


@pytest.mark.parametrize("text", [PYPROJECT])
def test_the_small_toml_reader_agrees_with_tomllib_on_what_we_read(text):
    tomllib = pytest.importorskip("tomllib")
    full, mini = tomllib.loads(text), d._mini_toml(text)
    assert mini["project"]["name"] == full["project"]["name"]
    assert mini["project"]["dependencies"] == full["project"]["dependencies"]
    assert mini["project"]["optional-dependencies"] == full["project"]["optional-dependencies"]
    assert mini["dependency-groups"] == full["dependency-groups"]


def test_the_small_toml_reader_alone_gives_the_same_requirements(monkeypatch):
    monkeypatch.setattr(d, "load_toml", d._mini_toml)            # what Python 3.10 (no tomllib) does
    assert collect({"pyproject.toml": PYPROJECT}).reqs == ["requests>=2.0", "rich >= 13", "colorama; sys_platform == 'win32'",
                                                            'numpy>=1.20,<2 ; python_version >= "3.9"', "pytest>=8",
                                                            "pytest-cov", "ruff", "black==24.1.0", "pre-commit", "mypy", "pytest-asyncio"]


def test_dotted_keys_and_inline_tables_in_the_small_reader():
    got = d._mini_toml('[project]\nname = "x"\noptional-dependencies.dev = ["a", "b"]\n[tool.x]\nk = {a = 1, b = ["c"]}\n[[tool.y]]\nz = 1\n')
    assert got["project"]["optional-dependencies"]["dev"] == ["a", "b"] and got["tool"]["x"]["k"] == {"a": "1", "b": ["c"]}


def test_a_broken_pyproject_is_a_note_not_a_crash():
    c = collect({"pyproject.toml": "[project\nname = ="})
    assert c.reqs == [] and isinstance(c.notes, list)


def test_poetry_projects_are_reported_as_unsupported():
    c = collect({"pyproject.toml": '[tool.poetry]\nname = "p"\n[tool.poetry.dependencies]\nrequests = "^2"\n'})
    assert c.reqs == [] and any("Poetry" in n for n in c.notes)


@pytest.mark.parametrize("line,ok", [
    ("requests", True), ("requests>=2.0,<3", True), ("requests[security,socks]>=2", True), ("Flask_SQLAlchemy==3.0.*", True),
    ("pkg ; python_version < '3.9' and sys_platform == 'linux'", True), ("pkg (>=1.0)", True), ("pkg~=1.4.2", True),
    ("pkg @ https://files.pythonhosted.org/packages/ab/cd/pkg-1.0-py3-none-any.whl", True),
    ("pkg @ https://pypi.org/packages/pkg-1.0.tar.gz", True),
    ("pkg==1.0 --hash=sha256:abcdef", True),
    ("-e .", False), ("--editable ./x", False), ("-e git+https://github.com/a/b.git#egg=b", False), ("git+https://github.com/a/b.git", False),
    ("git+ssh://git@github.com/a/b.git", False), ("https://example.org/pkg.whl", False), ("http://pypi.org/x.whl", False),
    ("pkg @ http://pypi.org/x.whl", False), ("pkg @ https://evil.example/pkg.whl", False), ("pkg @ https://pypi.org.evil.example/x.whl", False),
    ("pkg @ https://user:pw@pypi.org/x.whl", False), ("pkg @ file:///etc/passwd", False),
    ("./local", False), ("../up", False), ("/abs/path", False), ("~/x", False), ("dist/pkg-1.0.tar.gz", False), ("pkg-1.0-py3-none-any.whl", False),
    ("--index-url https://pypi.example/simple", False), ("-i http://x/simple", False), ("--extra-index-url=https://x/simple", False),
    ("--find-links /tmp/wheels", False), ("-f https://x", False), ("--trusted-host x", False), ("--no-index", False), ("--pre", False),
    ("-c constraints.txt", False), ("--install-option=--prefix=/x", False),
    ("pkg; $(id)", False), ("pkg`id`", False), ("pkg && rm -rf x", False), ("pkg | tee x", False), ("a b c", False), ("pkg>=1 \\n x", False),
    ("x" * 400, False),
])
def test_vet_accepts_plain_pypi_requirements_and_skips_the_rest(line, ok):
    clean, why = d.vet(line)
    assert bool(clean) is ok, (line, clean, why)
    assert (why == "") == ok


def test_blank_and_comment_lines_are_silent():
    assert d.vet("") == (None, "") and d.vet("   # note") == (None, "") and d.vet("pkg  # trailing") == ("pkg", "")
    assert d.vet("pkg --hash=sha256:aa --hash=sha256:bb") == ("pkg", "")


def test_requirements_files_includes_selection_and_escapes():
    files = {
        "requirements.txt": "base1\nbase2>=1  # c\n",
        "requirements-dev.txt": "-r requirements.txt\n-r requirements/test.txt\n-r ../outside.txt\n-r /etc/passwd\n-r https://x/y.txt\n"
                                "devtool==1\nlong-one \\\n  ==2.0\n",
        "dev-requirements.in": "ignored-in-file\n",
        "requirements-docs.txt": "sphinx\n",
        "requirements/test.txt": "testtool\n-r ../requirements.txt\n",
        "requirements/lint.txt": "linttool\n",
        "requirements/prod.txt": "gunicorn\n",
        "requirements/extras/postgres.txt": "asyncpg\n",
        "requirements/extras/uvloop.txt": "uvloop\n",
        "README.md": "# not a requirements file\n",
    }
    c = collect(files)
    assert c.reqs == ["base1", "base2>=1", "testtool", "devtool==1", "long-one ==2.0", "linttool"]
    assert "requirements/prod.txt" not in c.sources and "requirements-docs.txt" not in c.sources
    assert not any("extras" in s for s in c.sources)
    skipped = " | ".join(c.skipped)
    assert "../outside.txt" in skipped and "/etc/passwd" in skipped and "https://x/y.txt" in skipped
    c2 = collect(files, ["postgres", "docs"])
    assert "asyncpg" in c2.reqs and "sphinx" in c2.reqs and "uvloop" not in c2.reqs and not any("not found" in n for n in c2.notes)


def test_include_loops_file_limit_and_requirement_cap_are_bounded():
    loop = {"requirements.txt": "-r requirements-a.txt\n", "requirements-a.txt": "-r requirements.txt\n-r requirements-dev.txt\na\n",
            "requirements-dev.txt": "-r requirements-a.txt\nb\n"}
    assert collect(loop).reqs == ["a", "b"]
    many = {"requirements.txt": "\n".join(f"pkg{i}" for i in range(d.MAX_REQS + 50))}
    c = collect(many)
    assert len(c.reqs) == d.MAX_REQS and any("more than" in n for n in c.notes)
    chain = {f"requirements-{i}.txt": f"-r requirements-{i + 1}.txt\np{i}\n" for i in range(40)}
    chain["requirements.txt"] = "-r requirements-0.txt\n"
    c = collect(chain)
    assert len(c.sources) <= d.MAX_FILES + 2 and any("limit" in n for n in c.notes)


def test_a_missing_or_unreadable_file_is_a_note():
    c = d.collect(["requirements.txt"], [], lambda p: None, set())
    assert c.reqs == [] and any("could not be read" in n for n in c.notes)
