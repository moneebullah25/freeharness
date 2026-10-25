"""`freeharness agents`: offline. A local fixture git repo stands in for agency-agents (no network).

The fixture has the Agency layout in miniature: engineering/ and testing/ agent files, a fake scripts/convert.sh
and scripts/install.sh that behave like the real ones for `--tool dsh` (including the DSH_SKILLS_DIR > DSH_HOME
rule that makes the real installer dangerous when the environment leaks), and nothing else.
The last test drives the real dsh against tests/fake_openai.py and checks the installed skills reach the request.
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from freeharness import agents_cmd, paths
from freeharness.client import rpc

HERE = Path(__file__).resolve().parent

CONVERT = r"""#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$1" = "--tool" ] && [ "$2" = "dsh" ] || { echo "only dsh" >&2; exit 1; }
rm -rf integrations/dsh
for f in engineering/*.md testing/*.md; do
  name=$(sed -n 's/^name: //p' "$f" | head -1)
  desc=$(sed -n 's/^description: //p' "$f" | head -1)
  slug="agency-$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g')"
  mkdir -p "integrations/dsh/$slug"
  printf -- "---\nname: '%s'\ndescription: '%s'\n---\nBody of %s\n" "$slug" "$desc" "$name" > "integrations/dsh/$slug/SKILL.md"
done
echo "converted"
"""

INSTALL = r"""#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
tool=""; path=""; agents=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tool) tool="$2"; shift 2;;
    --path) path="$2"; shift 2;;
    --agent) agents="$2"; shift 2;;
    --no-convert|--no-interactive) shift;;
    *) echo "unknown $1" >&2; exit 1;;
  esac
done
[ "$tool" = "dsh" ] || { echo "only dsh" >&2; exit 1; }
dest="${path:-${DSH_SKILLS_DIR:-${DSH_HOME:-$HOME/.dsh}/skills}}"
touch "$HOME/sentinel"
for d in integrations/dsh/*/; do
  n=$(basename "$d"); slug=${n#agency-}
  if [ -n "$agents" ]; then
    case ",$agents," in *",$slug,"*) ;; *) continue;; esac
  fi
  mkdir -p "$dest/$n"; cp "$d/SKILL.md" "$dest/$n/SKILL.md"
done
echo "installed"
"""


def agent_md(name, desc):
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\nPersona text.\n"


def fixture_files():
    files = {"scripts/convert.sh": (CONVERT, True), "scripts/install.sh": (INSTALL, True),
             "scripts/lib.sh": ("# lib\n", False)}
    names = {"code-reviewer": "Code Reviewer", "reality-checker": "Reality Checker",
             "application-security-engineer": "Application Security Engineer",
             "software-architect": "Software Architect", "backend-architect": "Backend Architect",
             "evidence-collector": "Evidence Collector", "technical-writer": "Technical Writer",
             "git-workflow-master": "Git Workflow Master", "minimal-change-engineer": "Minimal Change Engineer",
             "frontend-developer": "Frontend Developer", "sre": "SRE", "rapid-prototyper": "Rapid Prototyper"}
    for i, (slug, name) in enumerate(names.items()):
        d = "testing" if slug in ("reality-checker", "evidence-collector") else "engineering"
        files[f"{d}/{d}-{slug}.md"] = (agent_md(name, f"DESC-{slug} does one thing well."), False)
    return files


def commit(repo: Path, files: dict, parent: str | None = None, msg="fixture", deletes=()) -> str:
    """Create a commit with git fast-import: no user.name/email anywhere, explicit committer in the stream."""
    out = [f"commit refs/heads/main\ncommitter Fixture <fixture@example.invalid> 0 +0000\ndata {len(msg)}\n{msg}\n"]
    if parent:
        out.append(f"from {parent}\n")
    for path, (content, exe) in files.items():
        b = content.encode()
        out.append(f"M {'100755' if exe else '100644'} inline {path}\ndata {len(b)}\n")
        out.append(b.decode() + "\n")
    for path in deletes:
        out.append(f"D {path}\n")
    out.append("done\n")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "fast-import", "--quiet", "--done"], cwd=repo, input="".join(out).encode(), env=env,
                   check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=repo, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Fixture repo + isolated freeharness home + decoy 'real' dirs that must stay untouched."""
    repo = tmp_path / "agency-fixture"
    repo.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=repo, check=True, env=env)
    files = fixture_files()
    sha = commit(repo, files)
    fh = tmp_path / "fh-home"
    realhome = tmp_path / "realhome"
    decoy = tmp_path / "decoy-skills"
    for d in (realhome, decoy):
        d.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("FREEHARNESS_HOME", str(fh))
    monkeypatch.setenv("HOME", str(realhome))
    monkeypatch.setenv("DSH_SKILLS_DIR", str(decoy))           # the real install.sh would write here
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(realhome / ".claude"))
    monkeypatch.setattr(agents_cmd, "DEFAULT_REF", sha)
    monkeypatch.setattr(agents_cmd, "REPO_URL", str(repo))
    monkeypatch.setattr(agents_cmd, "SCRIPT_SHA256", {
        k: hashlib.sha256(files[k][0].encode()).hexdigest() for k in agents_cmd.SCRIPT_SHA256})
    return {"repo": repo, "sha": sha, "files": files, "realhome": realhome, "decoy": decoy, "tmp": tmp_path, "scratch": scratch}


def run(*argv):
    return agents_cmd.main(list(argv))


def installed_names():
    return sorted(p.name for p in agents_cmd.skills_dir().iterdir()) if agents_cmd.skills_dir().is_dir() else []


OSS_NAMES = sorted(["agency-" + s for s, _ in agents_cmd.OSS_AGENTS] + ["oss-contribution"])


def test_default_install_is_the_small_oss_set_and_touches_nothing_else(world, capsys):
    assert run("install", "--repo", str(world["repo"])) == 0
    assert installed_names() == OSS_NAMES
    assert "agency-frontend-developer" not in installed_names()
    # the leaked DSH_SKILLS_DIR / fake HOME were not used; the temp clone is gone
    assert list(world["decoy"].iterdir()) == []
    assert list(world["realhome"].iterdir()) == []
    assert list(world["scratch"].iterdir()) == []                # the temp clone was deleted
    skill = (agents_cmd.skills_dir() / "oss-contribution" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: oss-contribution\n")
    assert (agents_cmd.skills_dir() / "oss-contribution" / "references" / "steps.md").is_file()
    m = agents_cmd.read_manifest()
    assert m["ref"] == world["sha"] and m["set"] == "oss" and len(m["installed"]) == len(agents_cmd.OSS_AGENTS)
    assert "listing about" in capsys.readouterr().out


def test_reinstall_is_idempotent(world):
    assert run("install") == 0
    before = {p.relative_to(agents_cmd.skills_dir()): p.read_bytes()
              for p in agents_cmd.skills_dir().rglob("*") if p.is_file()}
    assert run("install") == 0
    after = {p.relative_to(agents_cmd.skills_dir()): p.read_bytes()
             for p in agents_cmd.skills_dir().rglob("*") if p.is_file()}
    assert before == after and installed_names() == OSS_NAMES


def test_all_needs_yes_and_warns_about_prompt_cost(world, capsys):
    assert run("install", "--set", "all") == 2
    err = capsys.readouterr().err
    assert "WARNING" in err and "EVERY step" in err and "--yes" in err
    assert installed_names() == []                      # refused before any clone or write
    assert run("install", "--set", "all", "--yes") == 0
    assert "agency-frontend-developer" in installed_names() and "agency-sre" in installed_names()
    assert "oss-contribution" in installed_names()


def test_switching_from_all_back_to_oss_removes_the_extras_only(world):
    mine = agents_cmd.skills_dir() / "my-own-skill"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: my-own-skill\ndescription: mine\n---\n")
    assert run("install", "--set", "all", "--yes") == 0
    assert run("install") == 0
    assert installed_names() == sorted(OSS_NAMES + ["my-own-skill"])


def test_unknown_commit_is_refused_and_nothing_is_installed(world, capsys):
    bad = "0" * 40
    assert run("install", "--ref", bad, "--yes") == 1
    assert "not in" in capsys.readouterr().err
    assert installed_names() == [] and agents_cmd.read_manifest() is None


def test_non_sha_ref_is_refused(world, capsys):
    assert run("install", "--ref", "main", "--yes") == 1
    assert "40-character" in capsys.readouterr().err
    assert installed_names() == []


def test_unreviewed_ref_needs_yes(world, capsys):
    sha2 = commit(world["repo"], world["files"], parent=world["sha"], msg="second")
    assert sha2 != world["sha"]
    assert run("install", "--ref", sha2) == 2
    assert "reviewed" in capsys.readouterr().err
    assert installed_names() == []
    assert run("install", "--ref", sha2, "--yes") == 0                # explicit opt-in works
    assert agents_cmd.read_manifest()["ref"] == sha2


def test_head_mismatch_is_refused(world, tmp_path):
    clone = tmp_path / "c"
    agents_cmd.fetch_pinned(str(world["repo"]), world["sha"], clone)
    other = "1" * 40
    with pytest.raises(agents_cmd.AgentsError, match="pin mismatch"):
        agents_cmd.verify_head(clone, other)


def test_tampered_scripts_are_refused(world, capsys, monkeypatch):
    files = dict(world["files"])
    files["scripts/install.sh"] = (INSTALL + "echo pwned > \"$HOME/pwned\"\n", True)
    sha2 = commit(world["repo"], files, parent=world["sha"], msg="tamper")
    # the pinned default now points at the tampered commit but the recorded checksums are the old ones
    monkeypatch.setattr(agents_cmd, "DEFAULT_REF", sha2)
    assert run("install") == 1
    assert "reviewed checksum" in capsys.readouterr().err
    assert installed_names() == [] and list(world["realhome"].iterdir()) == []


def test_missing_agent_in_the_pinned_commit_is_reported(world, capsys):
    sha2 = commit(world["repo"], {}, parent=world["sha"], msg="drop", deletes=["testing/testing-evidence-collector.md"])
    assert run("install", "--ref", sha2, "--yes") == 1
    assert "agency-evidence-collector" in capsys.readouterr().err
    assert installed_names() == []


def test_list_shows_installed_and_listing_size(world, capsys):
    assert run("list") == 0
    out = capsys.readouterr().out
    assert "nothing installed" in out and "agency-code-reviewer" in out
    assert run("install") == 0
    capsys.readouterr()
    assert run("list") == 0
    out = capsys.readouterr().out
    assert "agency-code-reviewer" in out and "oss-contribution" in out and "total listing" in out
    assert "not ours" not in out


def test_remove_deletes_only_what_we_installed(world, capsys):
    mine = agents_cmd.skills_dir() / "my-own-skill"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: my-own-skill\ndescription: mine\n---\n")
    assert run("install") == 0
    assert run("remove") == 0
    assert installed_names() == ["my-own-skill"]
    assert agents_cmd.read_manifest() is None
    assert run("remove") == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_manifest_with_hostile_names_cannot_delete_outside_skills(world):
    (world["tmp"] / "victim").mkdir()
    (world["tmp"] / "victim" / "SKILL.md").write_text("x")
    agents_cmd._write_manifest({"installed": ["../../victim", "/etc", ".."], "own": []})
    assert run("remove") == 0
    assert (world["tmp"] / "victim" / "SKILL.md").exists()


def test_user_made_oss_contribution_is_not_overwritten(world, capsys):
    mine = agents_cmd.skills_dir() / "oss-contribution"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: oss-contribution\ndescription: mine\n---\nMINE\n")
    assert run("install") == 0
    assert "MINE" in (mine / "SKILL.md").read_text()
    assert "skipping oss-contribution" in capsys.readouterr().out
    assert "oss-contribution" not in agents_cmd.read_manifest()["own"]


def test_bundled_skill_is_small_and_complete():
    d = agents_cmd.bundled_skills_dir() / "oss-contribution"
    text = (d / "SKILL.md").read_text()
    assert len(text) < 3500
    fm = agents_cmd.parse_frontmatter(text)
    assert fm["name"] == "oss-contribution" and len(fm["description"]) < 200
    assert ": " not in fm["description"] and " #" not in fm["description"]       # a plain YAML scalar: dsh drops the skill otherwise
    for tool in ("gh_repo_info", "gh_search", "gh_get_file", "gh_list_pr_files", "gh_pr_threads", "gh_fork",
                 "gh_clone", "gh_push", "gh_issue_create", "gh_pr_create", "gh_pr_reply", "gh_pr_resolve_thread"):
        assert tool in text, tool
    for must in ("STOP", "Never work around", "Fixes #N", "NEW commit", "force-push", "4 query variants",
                 "Vote gate", "reports/<repo>_issues.md"):
        assert must in text, must
    assert (d / "references" / "steps.md").is_file()


def test_cli_dispatch(world, capsys):
    from freeharness import cli
    assert "agents" in cli.SUBCOMMANDS
    assert cli.main(["agents", "list"]) == 0
    assert "oss-contribution" in capsys.readouterr().out


# --- real dsh: do the installed skills reach the model? -------------------------------------------------

try:
    rpc.find_node_dir()
    HAVE_DSH = rpc.DSH_BIN.exists()
except Exception:  # noqa: BLE001
    HAVE_DSH = False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not HAVE_DSH, reason="node / dsh harness not installed")
def test_real_dsh_offers_the_installed_skills(world, monkeypatch):
    tmp = world["tmp"]
    port, log = free_port(), tmp / "log"
    log.mkdir()
    p = subprocess.Popen([sys.executable, str(HERE / "fake_openai.py")], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=dict(os.environ, FAKE_OPENAI_PORT=str(port),
                                                             FAKE_OPENAI_LOG=str(log)))
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        patch = tmp / "patch.yml"
        patch.write_text(rpc.PATCH.read_text().replace("127.0.0.1:8766", f"127.0.0.1:{port}"))
        monkeypatch.setattr(rpc.Runtime, "overlays", staticmethod(lambda: []))
        proj = tmp / "proj"
        proj.mkdir()
        assert run("install") == 0
        rt = rpc.Runtime(proj, home=paths.dsh_home(), patch=patch)
        try:
            rt.start()
            r = rt.run_turn(f"agents-{uuid.uuid4().hex[:8]}", "hello", timeout=120)
            assert r.ok, r.reason
        finally:
            rt.close()
        first = json.loads((log / "freeharness-fake-requests.jsonl").read_text().splitlines()[0])
        text = json.dumps(first)
        for name in OSS_NAMES:
            assert name in text, f"{name} not offered to the model"
        assert "DESC-code-reviewer" in text
        assert "agency-frontend-developer" not in text and "agency-sre" not in text
    finally:
        p.terminate()
        p.wait(5)
