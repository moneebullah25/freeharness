"""
The shim proxies logged-in browser sessions, so it must not answer just anyone
who can reach 127.0.0.1: another local user, a browser tab doing DNS rebinding
or a stray script would otherwise spend your accounts' quotas and read your chats.

Two checks, both in `check_request`:
  * Host must be a loopback name (defeats DNS rebinding: an attacker's page can
    reach the port, but its requests carry the attacker's Host).
  * Authorization: Bearer <token>, a random per-install secret kept 0600.
"""
from __future__ import annotations

import hmac
import os
import secrets

from . import paths

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def load_or_create_token() -> str:
    f = paths.token_file()
    try:
        tok = f.read_text().strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    paths.private_dir(f.parent)
    tok = secrets.token_urlsafe(32)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok


def host_ok(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if not host_header.endswith("]") else host_header
    if host_header.startswith("["):                      # [::1]:8766
        host = host_header.split("]")[0] + "]"
    return host in LOOPBACK_HOSTS


def check_request(host_header: str | None, auth_header: str | None, token: str | None):
    """None when allowed, else (status, message)."""
    if not host_ok(host_header):
        return 403, "forbidden host"
    if token is None:
        return None
    given = (auth_header or "")
    if given.lower().startswith("bearer "):
        given = given[7:].strip()
    else:
        given = ""
    if not hmac.compare_digest(given.encode(), token.encode()):
        return 401, "missing or invalid bearer token"
    return None
