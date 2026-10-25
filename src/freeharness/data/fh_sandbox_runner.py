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
    # the workspace may contain $HOME (running in ~): re-hide the secret places inside it
    for rel in SECRET_SUBDIRS:
        p = os.path.join(os.path.realpath(home), rel) if home else ""
        if p and os.path.commonpath([p, workspace]) == workspace and os.path.exists(p):
            out += ["--tmpfs", p] if os.path.isdir(p) else ["--ro-bind", "/dev/null", p]
    for k in environ:
        if SECRET_ENV.search(k) and k not in KEEP_ENV:
            out += ["--unsetenv", k]
    return out + ["--chdir", cwd, "--"] + cmd


def main():
    bwrap = shutil.which("bwrap")
    if not bwrap:
        die("bwrap (bubblewrap) is not installed; refusing to run unconfined. Install it (apt install bubblewrap).")
    argv = build(sys.argv[1:], os.getcwd(), os.environ.get("HOME", ""), dict(os.environ), bwrap)
    try:
        os.execv(bwrap, argv)  # noqa: S606
    except OSError as e:
        die(f"cannot start bwrap: {e}")


if __name__ == "__main__":
    main()
