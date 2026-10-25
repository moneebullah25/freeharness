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
    "deepseek": "https://chat.deepseek.com/sign_in",
    "grok": "https://grok.com/", "qwen": "https://chat.qwen.ai/",
    "duck": "https://duck.ai/", "hf": "https://huggingface.co/chat/",
}
# Sites that block headless Chrome run in a visible window with their OWN profile (Chromium locks a
# profile to one browser, and the shared one is headless), so they are logged in separately.
OWN_PROFILE = ("chatgpt", "deepseek")

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


def logged_in_sites(profile: Path | None = None, only: str | None = None) -> dict[str, bool]:
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
                if only and site != only:
                    continue
                row = con.execute("select 1 from cookies where host_key like ? and name = ? limit 1",
                                  ("%" + host, name)).fetchone()
                out[site] = bool(row)
            con.close()
        except (OSError, sqlite3.Error):
            pass
    return out


def _open_chrome(chrome: str, profile: Path, urls: list[str]):
    paths.private_dir(profile)
    flags = ["--password-store=basic"] if sys.platform.startswith("linux") else \
            ["--use-mock-keychain"] if sys.platform == "darwin" else []
    subprocess.run([chrome, f"--user-data-dir={profile}", "--profile-directory=Default", *flags,
                    "--no-first-run", "--no-default-browser-check", *urls])


def status_by_site() -> dict[str, bool | None]:
    """site -> signed in (True/False), or None when it cannot be read from cookies."""
    shared = logged_in_sites()
    out: dict[str, bool | None] = {k: v for k, v in shared.items() if k not in OWN_PROFILE}
    for site in OWN_PROFILE:
        if site in SESSION_COOKIES:
            out[site] = logged_in_sites(paths.provider_profile(site), only=site)[site]
        else:
            out[site] = None
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freeharness login", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sites", nargs="*", choices=list(SITES), help=f"default: {' '.join(SITES)}")
    ap.add_argument("--check", action="store_true", help="only report which sites are signed in")
    args = ap.parse_args(argv)

    if not args.check:
        chrome = find_chrome()
        if not chrome:
            print("Google Chrome not found. Install it, or set FREEHARNESS_CHROME to its path.", file=sys.stderr)
            return 1
        wanted = args.sites or list(SITES)
        shared = [s for s in wanted if s not in OWN_PROFILE]
        if shared:
            print("Sign in to the sites you want, then CLOSE the Chrome window (use its X) to save.")
            _open_chrome(chrome, paths.chrome_profile(), [SITES[s] for s in shared])
        for site in (s for s in wanted if s in OWN_PROFILE):
            print(f"{site} runs in its own visible window; sign in there, then close it with its X.")
            _open_chrome(chrome, paths.provider_profile(site), [SITES[site]])
    status = status_by_site()
    for site, ok in status.items():
        print(f"  {site:8} {'signed in' if ok else 'not signed in' if ok is False else 'unknown (check with the eval)'}")
    return 0 if any(v for v in status.values() if v is not None) else 1


if __name__ == "__main__":
    sys.exit(main())
