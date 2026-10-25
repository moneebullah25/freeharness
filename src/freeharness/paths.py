"""
Where freeharness keeps things, in one place.

Nothing is stored next to the code or in the install directory: an installed
package may be read-only, and a login profile must survive an upgrade.

    data    browser profiles (logins!), the DSH home with saved sessions, the harness install
    config  the shim's access token
    state   logs

Override the root with FREEHARNESS_HOME (everything under one directory), which
is also how the tests and a portable setup relocate it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _base(kind: str) -> Path:
    override = os.environ.get("FREEHARNESS_HOME")
    if override:
        return Path(override).expanduser().resolve() / kind
    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library" / "Application Support" / "freeharness"
        return root if kind == "data" else root / kind
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "freeharness"
        return root if kind == "data" else root / kind
    xdg = {"data": ("XDG_DATA_HOME", ".local/share"),
           "config": ("XDG_CONFIG_HOME", ".config"),
           "state": ("XDG_STATE_HOME", ".local/state")}[kind]
    return Path(os.environ.get(xdg[0], home / xdg[1])) / "freeharness"


def data_dir() -> Path:
    return _base("data")


def config_dir() -> Path:
    return _base("config")


def state_dir() -> Path:
    return _base("state")


def private_dir(path: Path) -> Path:
    """Create `path` readable by this user only. Profiles hold live logins."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:                       # e.g. a filesystem without POSIX modes
        pass
    return path


def chrome_profile() -> Path:
    """The one logged-in Chrome profile shared by every `shared` provider."""
    return data_dir() / "chrome-profile"


def provider_profile(key: str) -> Path:
    """A throwaway browser profile for a provider that needs no login."""
    return data_dir() / "profiles" / key


def dsh_home() -> Path:
    return data_dir() / "dsh-home"


def harness_dir() -> Path:
    """Where `freeharness setup` installs the pinned DeepSeek Harness (npm)."""
    return data_dir() / "harness"


def log_dir() -> Path:
    return state_dir() / "logs"


def token_file() -> Path:
    return config_dir() / "shim-token"
