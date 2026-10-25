"""
`freeharness login`: sign in once to the sites you want, in a plain Chrome window.

Why plain Chrome and not Playwright: Google refuses sign-in in an automated browser
("This browser or app may not be secure"). Why `--password-store=basic` (Linux) and
`--use-mock-keychain` (macOS): that is the cookie-encryption mode Playwright's headless
Chrome uses. Without it Chrome encrypts with the system keyring, headless cannot decrypt,
and Chrome then DELETES those cookies (measured: 152 -> 73). The window is closed by you;
afterwards the saved sessions are checked so a silent failure is reported, not discovered later.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from . import paths

SITES = {
    "gemini": "https://gemini.google.com/app", "chatgpt": "https://chatgpt.com/",
    "grok": "https://grok.com/", "qwen": "https://chat.qwen.ai/",
    "duck": "https://duck.ai/", "hf": "https://huggingface.co/chat/",
}
# cookie that exists only while signed in, per site (host suffix, cookie name)
SESSION_COOKIES = {
    "gemini": (".google.com", "SID"), "chatgpt": (".chatgpt.com", "__Secure-next-auth.session-token.0"),
    "grok": (".grok.com", "sso"), "qwen": (".qwen.ai", "refresh_token"), "hf": ("huggingface.co", "token"),
}


def find_chrome() -> str | None:
    env = os.environ.get("FREEHARNESS_CHROME")
    if env:
        return env
    if sys.platform == "darwin":
        c = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        return str(c) if c.exists() else None
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base and (Path(base) / "Google/Chrome/Application/chrome.exe").exists():
                return str(Path(base) / "Google/Chrome/Application/chrome.exe")
        return None
    for name in ("google-chrome", "google-chrome-stable", "chrome"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def cookie_db(profile: Path) -> Path | None:
    for rel in ("Default/Network/Cookies", "Default/Cookies"):
        if (profile / rel).exists():
            return profile / rel
    return None


def logged_in_sites(profile: Path | None = None) -> dict[str, bool]:
    """Which sites have a session cookie saved. Presence only, never the values."""
    db = cookie_db(profile or paths.chrome_profile())
    out = {k: False for k in SESSION_COOKIES}
    if not db:
        return out
    with tempfile.TemporaryDirectory() as tmp:              # Chrome may hold the file open
        copy = Path(tmp) / "c.db"
        try:
            shutil.copy2(db, copy)
            con = sqlite3.connect(copy)
            for site, (host, name) in SESSION_COOKIES.items():
                row = con.execute("select 1 from cookies where host_key like ? and name = ? limit 1",
                                  ("%" + host, name)).fetchone()
                out[site] = bool(row)
            con.close()
        except (OSError, sqlite3.Error):
            pass
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness login", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sites", nargs="*", choices=list(SITES), help=f"default: {' '.join(SITES)}")
    ap.add_argument("--check", action="store_true", help="only report which sites are signed in")
    args = ap.parse_args(argv)
    profile = paths.chrome_profile()

    if not args.check:
        chrome = find_chrome()
        if not chrome:
            print("Google Chrome not found. Install it, or set FREEHARNESS_CHROME to its path.", file=sys.stderr)
            return 1
        paths.private_dir(profile)
        flags = ["--password-store=basic"] if sys.platform.startswith("linux") else \
                ["--use-mock-keychain"] if sys.platform == "darwin" else []
        urls = [SITES[s] for s in (args.sites or SITES)]
        print("Sign in to the sites you want, then CLOSE the Chrome window (use its X) to save.")
        subprocess.run([chrome, f"--user-data-dir={profile}", "--profile-directory=Default", *flags,
                        "--no-first-run", "--no-default-browser-check", *urls])
    status = logged_in_sites(profile)
    for site, ok in status.items():
        print(f"  {site:8} {'signed in' if ok else 'not signed in'}")
    return 0 if any(status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
