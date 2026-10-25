# Troubleshooting

Start with `freeharness doctor`. To report a problem, paste `freeharness doctor --report` (safe to share:
versions and pass/fail only) and, if asked, `freeharness.log` / `shim.log` from the log directory it prints.

| Symptom | Cause / fix |
|---|---|
| `DeepSeek Harness installed: FAIL` | Run `freeharness setup`. |
| `node >= 24.9: FAIL` | `freeharness setup` installs a checksum-verified Node under the data dir (Linux/macOS). |
| `Google Chrome not found` | Install Chrome, or set `FREEHARNESS_CHROME=/path/to/chrome`. |
| Google says "This browser or app may not be secure" | You signed in through an automated browser. Use `freeharness login`, which opens plain Chrome. |
| `login` says a site is "not signed in" after you signed in | Close the window with its X (not by killing it) so cookies are flushed; sign in again. |
| Logged in once, then logged out in headless | The profile was created without `--password-store=basic`; headless Chrome cannot decrypt it and **deletes** the cookies. Re-run `freeharness login` (never open the profile with a normal Chrome launch). |
| `Opening in existing browser session` / profile in use | Only one process can use the profile. Stop the shim (`pkill -f freeharness.backends.shim`) or close the login window. |
| `429 ... all providers cooling down` | Each provider rests after hitting its free-tier limit (15-30 min); see `/providers`. |
| A turn hangs, then `no reply started within Ns` | The site is thinking or blocked; the shim reports what the page showed. Retry or `/model gemini`. |
| Qwen/Grok asks for your age, a CAPTCHA, or a new terms dialog | Answer it once in `freeharness login <site>`. CAPTCHAs are never bypassed; a site that shows one on every message is excluded from rotation. |
| `port 8766 in use` | Another shim is running. `freeharness doctor` says whether it answers. |
| Agent edited the wrong directory | It works in the directory you launched from (`--cwd`). Launch from the project, not `~`. |

## Where things are
`freeharness doctor` prints the log directory. Data/config/state paths are listed in docs/PRIVACY.md.
