"""
`freeharness agents install | list | remove`: put open-source agent skills into the dsh home.

Source: "The Agency" (https://github.com/msitarzewski/agency-agents, MIT), which converts its ~280 agent
personas into DeepSeek Harness skills (`<dsh home>/skills/agency-<slug>/SKILL.md`). We run ITS OWN
`scripts/convert.sh --tool dsh` and `scripts/install.sh --tool dsh`, with these guard rails:

  * the repository is cloned (git over https) into a fresh temp directory and pinned to a full commit SHA;
    HEAD must equal that SHA after checkout. For the SHA built into this file the three shell scripts are also
    checked against SHA-256 values recorded after reading them (the same idea as setup's Node checksum);
  * scripts run with a scrubbed environment: HOME and TMPDIR point into the temp directory, DSH_HOME is our dsh
    home, DSH_SKILLS_DIR is never inherited and `--path` pins the destination, so the user's real ~/.dsh and
    ~/.claude/agents are never touched; only `--tool dsh` is ever run;
  * the default set `oss` installs a handful of agents, because skill names and descriptions are re-sent on
    every model step and the prompt budget is small. `--set all` needs `--yes`.

The bundled `oss-contribution` skill (src/freeharness/data/skills) is installed alongside. `remove` deletes
exactly what the manifest says we installed. See docs/AGENTS_SKILLS.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import paths

REPO_URL = "https://github.com/msitarzewski/agency-agents.git"
# main HEAD on 2026-10-25. Bump deliberately: read the scripts again, then refresh SCRIPT_SHA256.
DEFAULT_REF = "b37b5bb33b78f2c1d6567d98e25ca818e3259219"
SCRIPT_SHA256 = {
    "scripts/convert.sh": "c370a6e9bbea4fbc5b684a614ef9edf540010cf8c9be102a386678b8e8656a8f",
    "scripts/install.sh": "ce64d7ed6e3969d549da0ebb1e52db6852a9f33540460f15e89852d15aeafaf5",
    "scripts/lib.sh": "51bb6fb36c4727d2764f770649ab18630f5b91e6bd7c9ef96bb5ed2084f30b44",
}
PREFIX = "agency-"

# (install slug, why). The slug is the agent's `name:` slugified; the skill is PREFIX + slug.
OSS_AGENTS = (
    ("code-reviewer", "reviews a component for correctness and security, the main bug-hunting lens"),
    ("reality-checker", "defaults to NEEDS WORK; the skeptic behind the vote gate"),
    ("application-security-engineer", "secure code review of the same component"),
    ("software-architect", "design-level problems and trade-offs"),
    ("backend-architect", "APIs, data and server-side code, where most OSS bugs live"),
    ("evidence-collector", "QA that reports only reproducible issues; marks untested scope"),
    ("technical-writer", "README, docs and issue/PR text"),
    ("git-workflow-master", "branching, commit hygiene, rebase and conflict questions"),
    ("minimal-change-engineer", "keeps a fix to the smallest diff; no scope creep in PRs"),
)
OWN_SKILLS = ("oss-contribution",)
SETS = ("oss", "all")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
TOKENS_PER_CHAR = 0.25        # rough: 4 characters per token
# dsh 0.2.1-rc.2 renders skills as one message: a fixed frame, then "- `name`: description\n" per skill.
# Measured on the real request (tests/fake_openai.py): frame 693 chars; 10 skills = 2798 chars; 284 skills = 74270.
ENTRY_OVERHEAD = 6
LISTING_FRAME = 693
ALL_MEASURED = (284, 74270)   # skills (283 agents + oss-contribution), characters, at DEFAULT_REF


class AgentsError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------- locations

def skills_dir() -> Path:
    return paths.dsh_home() / "skills"


def manifest_path() -> Path:
    return paths.data_dir() / "agents-installed.json"


def bundled_skills_dir() -> Path:
    return Path(__file__).resolve().parent / "data" / "skills"


def read_manifest() -> dict | None:
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_manifest(data: dict) -> None:
    p = manifest_path()
    paths.private_dir(p.parent)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------------- git

def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "HOME")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https:file",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return env


def _git(*args: str, cwd: Path | None = None, timeout: int = 600) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, env=_git_env(), capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        raise AgentsError("git is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise AgentsError(f"git {args[0]} timed out") from None
    if r.returncode:
        raise AgentsError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-600:]}")
    return r.stdout.strip()


def fetch_pinned(url: str, ref: str, dest: Path) -> None:
    """Clone `url` into the (new, empty) `dest` and check out exactly `ref`; refuse unless HEAD == ref."""
    if not _SHA_RE.match(ref):
        raise AgentsError(f"--ref must be a full 40-character commit SHA, got {ref!r} (branches and tags move)")
    if url.startswith("-"):
        raise AgentsError("invalid repository URL")
    _git("clone", "--quiet", "--no-checkout", "--", url, str(dest))
    try:
        _git("checkout", "--quiet", "--detach", ref, cwd=dest)
    except AgentsError as e:
        raise AgentsError(f"commit {ref} is not in {url}: {e}") from None
    verify_head(dest, ref)


def verify_head(repo: Path, ref: str) -> None:
    head = _git("rev-parse", "HEAD", cwd=repo)
    if head != ref:
        raise AgentsError(f"pin mismatch: HEAD is {head}, expected {ref}; refusing to run anything from it")


def verify_scripts(repo: Path) -> None:
    import hashlib
    for rel, want in SCRIPT_SHA256.items():
        f = repo / rel
        got = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "missing"
        if got != want:
            raise AgentsError(f"{rel} does not match the reviewed checksum (got {got}); refusing to run it")


# ---------------------------------------------------------------------------------- running the upstream scripts

def _script_env(tmp: Path) -> dict:
    home, tdir = tmp / "home", tmp / "tmp"
    home.mkdir(exist_ok=True)
    tdir.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "TMPDIR": str(tdir),
           "NO_COLOR": "1", "TERM": "dumb", "DSH_HOME": str(paths.dsh_home())}
    for k in ("LANG", "LC_ALL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env            # DSH_SKILLS_DIR, CLAUDE_CONFIG_DIR and friends are deliberately not inherited


def _bash(args: list[str], repo: Path, env: dict, say) -> str:
    bash = shutil.which("bash", path=env["PATH"])
    if not bash:
        raise AgentsError("bash not found; the Agency installer is a bash script (Linux, macOS, WSL, Git Bash)")
    try:
        r = subprocess.run([bash, *args], cwd=repo, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise AgentsError(f"{args[0]} timed out") from None
    if r.returncode:
        raise AgentsError(f"{args[0]} failed (exit {r.returncode}):\n{(r.stderr or r.stdout)[-1500:]}")
    return r.stdout


def converted_names(repo: Path) -> list[str]:
    root = repo / "integrations" / "dsh"
    return sorted(d.name for d in root.iterdir() if (d / "SKILL.md").is_file()) if root.is_dir() else []


# ---------------------------------------------------------------------------------- skill files

def parse_frontmatter(text: str) -> dict:
    """name / description of a SKILL.md (plain, 'single' or "double" quoted scalars; no folding needed here)."""
    lines = text.splitlines()
    out: dict = {}
    if not lines or lines[0].strip() != "---":
        return out
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        m = re.match(r"^(name|description):\s*(.*)$", ln)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] == "'":
                v = v[1:-1].replace("''", "'")
            elif len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1].replace('\\"', '"')
            out[m.group(1)] = v
    return out


def scan_skills(root: Path | None = None) -> list[dict]:
    """Every <name>/SKILL.md under the dsh skills dir: name, description, size of its listing entry."""
    root = root or skills_dir()
    rows = []
    if not root.is_dir():
        return rows
    for d in sorted(root.iterdir()):
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        fm = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        name, desc = fm.get("name", d.name), fm.get("description", "")
        rows.append({"dir": d.name, "name": name, "description": desc, "chars": len(name) + len(desc) + ENTRY_OVERHEAD})
    return rows


def listing_size(rows: list[dict]) -> tuple[int, int]:
    """(characters, tokens) of the skills message dsh adds to the first request; 0 when there are no skills."""
    chars = (LISTING_FRAME + sum(r["chars"] for r in rows)) if rows else 0
    return chars, round(chars * TOKENS_PER_CHAR)


def _safe_remove_skill(name: str) -> bool:
    """Delete <skills>/<name> if it is a plain skill folder we manage. Exact path, never a glob."""
    if not _NAME_RE.fullmatch(name):
        return False
    root = skills_dir()
    p = root / name
    if p.parent != root:
        return False
    if p.is_symlink():
        p.unlink()
        return True
    if p.is_dir() and (p / "SKILL.md").is_file():
        shutil.rmtree(p)
        return True
    return False


def install_own_skills(owned_before: set[str], say=print) -> list[str]:
    done = []
    for name in OWN_SKILLS:
        src = bundled_skills_dir() / name
        if not (src / "SKILL.md").is_file():
            raise AgentsError(f"bundled skill {name} is missing from the package")
        dest = skills_dir() / name
        if dest.exists() and name not in owned_before:
            say(f"skipping {name}: {dest} exists and was not installed by freeharness (remove it to let us manage it)")
            continue
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        done.append(name)
    return done


# ---------------------------------------------------------------------------------- commands

ALL_WARNING = """WARNING: --set all installs the whole Agency roster ({n} skills).
Every skill's name and description is sent to the model on EVERY step. Measured with the real harness that is
{chars} characters (~{tok} tokens) per step, more than the whole 16k-token prompt budget: it would crowd out the
task, the tool list and the history. Use the default `--set oss` unless you know why you want all of them.
Re-run with --yes to install anyway."""


def estimate_all() -> tuple[int, int, int]:
    """(skills, chars, tokens) measured from the roster at DEFAULT_REF; update on a bump."""
    n, chars = ALL_MEASURED
    return n, chars, round(chars * TOKENS_PER_CHAR)


def install(set_name: str = "oss", ref: str = DEFAULT_REF, url: str = REPO_URL, yes: bool = False,
            say=print) -> int:
    if set_name not in SETS:
        raise AgentsError(f"unknown set {set_name!r}; choose from {', '.join(SETS)}")
    if set_name == "all" and not yes:
        n, chars, tok = estimate_all()
        print(ALL_WARNING.format(n=n, chars=chars, tok=tok), file=sys.stderr)
        return 2
    if ref != DEFAULT_REF and not yes:
        print(f"refusing: {ref} is not the commit whose scripts freeharness reviewed ({DEFAULT_REF}).\n"
              "Its convert.sh/install.sh will run on your machine. Read them first, then re-run with --yes.",
              file=sys.stderr)
        return 2
    wanted = [PREFIX + slug for slug, _ in OSS_AGENTS] if set_name == "oss" else None
    old = read_manifest() or {}
    owned_before = set(old.get("installed", [])) | set(old.get("own", []))

    paths.private_dir(paths.dsh_home())
    tmp = Path(tempfile.mkdtemp(prefix="freeharness-agents-"))
    try:
        repo = tmp / "repo"
        say(f"cloning {url} at {ref[:12]} …")
        fetch_pinned(url, ref, repo)
        say("HEAD verified")
        if ref == DEFAULT_REF:
            verify_scripts(repo)
            say("convert.sh, install.sh, lib.sh match the reviewed checksums")
        env = _script_env(tmp)
        _bash(["scripts/convert.sh", "--tool", "dsh"], repo, env, say)
        available = converted_names(repo)
        if not available:
            raise AgentsError("convert.sh produced no dsh skills")
        if wanted is None:
            wanted = available
        missing = [w for w in wanted if w not in available]
        if missing:
            raise AgentsError(f"not in this commit of the Agency repo: {', '.join(missing)}")
        cmd = ["scripts/install.sh", "--tool", "dsh", "--no-convert", "--no-interactive",
               "--path", str(skills_dir())]
        if set_name == "oss":
            cmd += ["--agent", ",".join(w[len(PREFIX):] for w in wanted)]
        _bash(cmd, repo, env, say)
        absent = [w for w in wanted if not (skills_dir() / w / "SKILL.md").is_file()]
        if absent:
            raise AgentsError(f"install.sh did not produce: {', '.join(absent)}")
        own = install_own_skills(owned_before, say)
    except AgentsError as e:
        print(f"agents install failed: {e}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    stale = sorted(set(old.get("installed", [])) - set(wanted))
    gone = [s for s in stale if _safe_remove_skill(s)]
    _write_manifest({"repo": url, "ref": ref, "set": set_name, "installed": sorted(wanted),
                     "own": sorted(own), "format": 1})
    say(f"installed {len(wanted)} Agency skills + {len(own)} bundled ({', '.join(own) or 'none'}) -> {skills_dir()}")
    if gone:
        say(f"removed {len(gone)} skills from an earlier, larger set")
    rows = scan_skills()
    chars, tok = listing_size(rows)
    say(f"skills now visible to dsh: {len(rows)}, listing about {chars} characters (~{tok} tokens) per step")
    say("restart a running freeharness to see them.")
    return 0


def list_cmd(say=print) -> int:
    m = read_manifest()
    rows = scan_skills()
    mine = set((m or {}).get("installed", [])) | set((m or {}).get("own", []))
    if m:
        say(f"installed from {m.get('repo')} @ {str(m.get('ref'))[:12]}, set: {m.get('set')}")
    else:
        say("nothing installed by `freeharness agents install` yet. The default set (`--set oss`) would add:")
        for slug, why in OSS_AGENTS:
            say(f"  {PREFIX}{slug:32} {why}")
        say(f"  {OWN_SKILLS[0]:{len(PREFIX) + 32}} the OSS contribution pipeline (bundled)")
    if rows:
        say(f"\nskills in {skills_dir()}:")
        for r in rows:
            tag = "" if r["dir"] in mine else "  (not ours)"
            say(f"  {r['name']:42} {r['chars']:5} chars{tag}")
        chars, tok = listing_size(rows)
        say(f"total listing sent every step: {chars} characters, about {tok} tokens (budget 16k)")
    return 0


def remove(say=print) -> int:
    m = read_manifest()
    if not m:
        say("nothing to remove (no manifest); skills you added yourself are never touched")
        return 0
    names = list(m.get("installed", [])) + list(m.get("own", []))
    n = sum(_safe_remove_skill(x) for x in names)
    manifest_path().unlink(missing_ok=True)
    say(f"removed {n} skills ({len(names) - n} were already gone)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness agents", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("install", help="install the open-source Agency skills + the oss-contribution skill")
    pi.add_argument("--set", dest="set_name", choices=SETS, default="oss",
                    help="oss: a curated handful (default); all: the whole roster (prompt cost, needs --yes)")
    pi.add_argument("--ref", default=DEFAULT_REF, help="full commit SHA to pin (default: the reviewed one)")
    pi.add_argument("--repo", default=REPO_URL, help="git URL of a mirror (for tests and forks)")
    pi.add_argument("--yes", action="store_true", help="confirm --set all or a non-reviewed --ref")
    sub.add_parser("list", help="show installed skills and the size of their listing")
    sub.add_parser("remove", help="remove what `agents install` installed (nothing else)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "install":
            return install(args.set_name, args.ref, args.repo, args.yes)
        if args.cmd == "list":
            return list_cmd()
        return remove()
    except (AgentsError, OSError) as e:
        print(f"agents {args.cmd} failed: {e}", file=sys.stderr)
        return 1
