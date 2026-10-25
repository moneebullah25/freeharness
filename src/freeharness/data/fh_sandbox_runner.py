#!/usr/bin/env python3
"""
freeharness sandbox runner: the `runnerCommand` of dsh's sandbox-local (Linux).

dsh calls   <this> <bwrap profile args...> -- <command argv...>
and the profile is `--ro-bind / / --dev /dev --unshare-pid --proc /proc --die-with-parent`
(+ `--tmpfs /tmp --bind <ws> <ws>` in workspace-write). That profile makes the whole
host readable and leaves the network open. This wrapper keeps dsh's profile and adds:

  * no network      (--unshare-net: a new, empty network namespace; loopback is not the host's)
  * private places  hidden behind empty tmpfs mounts: $HOME, /root, /mnt, /media, /srv,
                    /tmp, /var/tmp, /run (docker.sock, the user bus), then the workspace is
                    bound back in. Secret dirs (~/.ssh ...) are re-hidden even when the
                    workspace contains $HOME.
  * secrets in env  tokens/keys/passwords are removed from the command's environment.

It fails CLOSED: no bwrap, or bwrap cannot build the sandbox, means the command does not run
(exit 126 with the signature `freeharness-sandbox:`, which dsh reports as a runner failure,
never as an unconfined run). Same-kernel confinement, not a container.

Extra read-only paths (a toolchain under $HOME): FREEHARNESS_SANDBOX_RO=/a:/b (set on dsh).
Standard toolchain: FH_TOOLCHAIN=<venv> (set on dsh by freeharness) is bound READ-ONLY and its bin/ goes first on
PATH, so `python`, `python3`, `pytest`, `ruff` ... exist in the sandbox. FH_SANDBOX_TOOLS=/a:/b are tool roots
(a Node install under $HOME, say): each is bound read-only and its bin/ is added to PATH. Neither exposes $HOME.

A SECOND mode exists for one caller only, gh_setup_env in mcp/ghgate.py:

    <this> --net-install <dir> [--hide P]... [--ro P]... [--setenv K V]... -- <command argv...>

It keeps the network (pip must reach the index) and takes away everything else: $HOME, /tmp, /run (but not the
DNS resolver config), the extra --hide paths, and a CLEARED environment; `<dir>` is the only writable place.
dsh never builds such an argv (its profile always starts with `--ro-bind / /`), and a command the model runs
inside the sandbox cannot reach this runner. Same fail-closed rules: no bwrap, no run.
Python 3.10+, standard library only.
"""
import os
import re
import shutil
import sys

SIGNATURE = "freeharness-sandbox: "
HIDE = ["/root", "/mnt", "/media", "/srv", "/var/tmp", "/run"]  # noqa: S108
SECRET_SUBDIRS = [".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config/gcloud", ".config/gh",
                  ".local/share/freeharness", ".config/freeharness", ".local/state/freeharness",
                  ".netrc", ".git-credentials", ".npmrc", ".pypirc"]
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|COOKIE|SESSION_ID|AUTH)", re.I)
KEEP_ENV = {"SSH_AUTH_SOCK_DISABLED"}


def die(msg: str, code: int = 126):
    sys.stderr.write(SIGNATURE + msg + "\n")
    sys.exit(code)


def _unsafe_root(p: str, home: str) -> bool:
    """A path that must never be bound into the sandbox: /, $HOME or one of its parents, /tmp."""
    p = os.path.realpath(p)
    if p in ("/", "/tmp", "/var/tmp") or p.startswith("/proc") or p.startswith("/dev"):  # noqa: S108
        return True
    h = os.path.realpath(home) if home else ""
    return bool(h) and os.path.commonpath([h, p]) == p


def tool_paths(environ: dict, home: str) -> tuple[list[str], list[str]]:
    """(read-only binds, PATH entries to put first) from FH_TOOLCHAIN and FH_SANDBOX_TOOLS."""
    binds: list[str] = []
    path: list[str] = []
    tc = environ.get("FH_TOOLCHAIN", "")
    if tc and os.path.isfile(os.path.join(tc, "pyvenv.cfg")) and not _unsafe_root(tc, home):
        binds.append(tc)
        path.append(os.path.join(tc, "bin"))
    for root in filter(None, environ.get("FH_SANDBOX_TOOLS", "").split(":")):
        if os.path.isdir(root) and not _unsafe_root(root, home):
            binds.append(root)
            if os.path.isdir(os.path.join(root, "bin")):
                path.append(os.path.join(root, "bin"))
    return binds, path


def project_env(workspace: str) -> tuple[str, str]:
    """(bin dir, source dir) of the per-project environment `gh_setup_env` built in the workspace
    (<workspace>/<repo>-venv with the clone at <workspace>/<repo>), or ("", "") when there is none."""
    try:
        cands = [d for d in os.scandir(workspace) if d.is_dir(follow_symlinks=False) and d.name.endswith("-venv")]
    except OSError:
        return "", ""
    for d in sorted(cands, key=lambda e: e.stat().st_mtime, reverse=True):
        src = os.path.join(workspace, d.name[:-len("-venv")])
        pybin = os.path.join(d.path, "bin")
        if os.path.isfile(os.path.join(pybin, "python")) and os.path.isdir(src):
            return pybin, src
    return "", ""


def build(argv: list[str], cwd: str, home: str, environ: dict, bwrap: str) -> list[str]:
    if "--" not in argv:
        die("malformed invocation (no -- separator); refusing to run unconfined")
    i = argv.index("--")
    profile, cmd = argv[:i], argv[i + 1:]
    if not cmd:
        die("no command")
    if profile[:3] != ["--ro-bind", "/", "/"]:
        die("unexpected sandbox profile from dsh; refusing to run unconfined")
    workspace = None
    for j, a in enumerate(profile):
        if a == "--bind" and j + 2 < len(profile):
            workspace = profile[j + 1]
    writable = workspace is not None
    workspace = os.path.realpath(workspace or cwd)

    out = [bwrap, "--unshare-net"]
    out += profile[:3]                                    # --ro-bind / /
    hide = [os.path.realpath(home)] if home else []
    hide += [h for h in HIDE if os.path.isdir(h)]
    hide += ["/tmp"] if not writable else []  # noqa: S108             # workspace-write already mounts a fresh /tmp
    for h in dict.fromkeys(hide):
        out += ["--tmpfs", h]
    rest = profile[3:]
    out += rest                                           # --dev --unshare-pid --proc ... --tmpfs /tmp --bind ws ws
    if not writable:
        out += ["--ro-bind", workspace, workspace]
    for extra in filter(None, environ.get("FREEHARNESS_SANDBOX_RO", "").split(":")):
        if os.path.exists(extra):
            out += ["--ro-bind", extra, extra]
    tc_binds, tc_path = tool_paths(environ, home)
    for b in dict.fromkeys(tc_binds):
        out += ["--ro-bind", b, b]
    proj_bin, proj_src = project_env(workspace)
    if proj_bin:                       # gh_setup_env built <workspace>/<repo>-venv: bare `pytest`/`python` use it
        tc_path = [proj_bin] + tc_path
        out += ["--setenv", "PYTHONPATH", proj_src]
    if tc_path:
        out += ["--setenv", "PATH", os.pathsep.join(tc_path + [environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")]),
                "--setenv", "PYTHONNOUSERSITE", "1"]
    # the workspace may contain $HOME (running in ~): re-hide the secret places inside it
    for rel in SECRET_SUBDIRS:
        p = os.path.join(os.path.realpath(home), rel) if home else ""
        if p and os.path.commonpath([p, workspace]) == workspace and os.path.exists(p):
            out += ["--tmpfs", p] if os.path.isdir(p) else ["--ro-bind", "/dev/null", p]
    for k in environ:
        if SECRET_ENV.search(k) and k not in KEEP_ENV:
            out += ["--unsetenv", k]
    return out + ["--chdir", cwd, "--"] + cmd


# resolver configuration lives under /run on systemd/NetworkManager hosts; /run is hidden, so these come back read-only
RESOLVER_DIRS = ["/run/systemd/resolve", "/run/NetworkManager", "/run/resolvconf", "/run/dnsmasq"]
NET_BASE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp/fh-home", "TMPDIR": "/tmp",  # noqa: S108
                "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1", "PIP_CONFIG_FILE": os.devnull,
                "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}


def build_net(argv: list[str], home: str, bwrap: str) -> list[str]:
    """The argv of the network-enabled install sandbox (see the module docstring). argv starts after --net-install."""
    if "--" not in argv:
        die("malformed --net-install invocation (no -- separator)")
    i = argv.index("--")
    opts, cmd = argv[:i], argv[i + 1:]
    if not opts or not cmd:
        die("--net-install needs a directory and a command")
    target, opts = opts[0], opts[1:]
    if not os.path.isabs(target) or not os.path.isdir(target) or os.path.realpath(target) != target \
            or _unsafe_root(target, home):
        die(f"--net-install directory {target!r} must be an absolute, real, existing directory outside $HOME's parents and /tmp itself")
    hide, ro, env = [], [], dict(NET_BASE_ENV)
    j = 0
    while j < len(opts):
        flag = opts[j]
        if flag in ("--hide", "--ro") and j + 1 < len(opts):
            p = os.path.realpath(opts[j + 1])
            if _unsafe_root(p, home) and flag == "--ro":
                die(f"refusing to bind {p} into the install sandbox")
            (hide if flag == "--hide" else ro).append(p)
            j += 2
        elif flag == "--setenv" and j + 2 < len(opts):
            env[opts[j + 1]] = opts[j + 2]
            j += 3
        else:
            die(f"unknown --net-install option {flag!r}")
    out = [bwrap, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
           "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]       # NO --unshare-net: pip needs the index
    private = ([os.path.realpath(home)] if home else []) + [h for h in HIDE if os.path.isdir(h)] + ["/tmp"]  # noqa: S108
    for h in dict.fromkeys(private):
        out += ["--tmpfs", h]
    for d in RESOLVER_DIRS:
        if os.path.isdir(d):
            out += ["--ro-bind", d, d]
    for h in dict.fromkeys(hide):
        out += ["--tmpfs", h]
    for r in dict.fromkeys(ro):
        if os.path.exists(r):
            out += ["--ro-bind", r, r]
    out += ["--bind", target, target, "--clearenv"]
    for k, v in env.items():
        out += ["--setenv", k, v]
    return out + ["--chdir", target, "--"] + cmd


def main():
    bwrap = shutil.which("bwrap")
    if not bwrap:
        die("bwrap (bubblewrap) is not installed; refusing to run unconfined. Install it (apt install bubblewrap).")
    if sys.argv[1:2] == ["--net-install"]:
        argv = build_net(sys.argv[2:], os.environ.get("HOME", ""), bwrap)
        try:
            os.execv(bwrap, argv)  # noqa: S606
        except OSError as e:
            die(f"cannot start bwrap: {e}")
    argv = build(sys.argv[1:], os.getcwd(), os.environ.get("HOME", ""), dict(os.environ), bwrap)
    try:
        os.execv(bwrap, argv)  # noqa: S606
    except OSError as e:
        die(f"cannot start bwrap: {e}")


if __name__ == "__main__":
    main()
