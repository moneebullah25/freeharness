# Release checklist (adapted to freeharness)

freeharness is a **Python package (wheel/sdist) that drives Node (DeepSeek Harness) and the user's own Chrome**.
The generic "frozen executable" checklist is therefore adapted as follows: no PyInstaller/Nuitka (a frozen
binary would still need Chrome, a downloaded Node and the user's browser profile, and would only hide the
code), a pure-Python wheel installed with pipx, and a per-OS Node fetched and checksum-verified by
`freeharness setup`. It is a local developer tool, **not a SaaS**: it operates your own logged-in browser
sessions, which cannot be shared or hosted.

Legend: **done** = implemented and verified; **CI** = enforced by `.github/workflows`; **n/a** = does not apply to this
architecture (reason given); **open** = not done, tracked below; **manual** = needs a human on that platform.

## 1. Build and packaging (P0)
| Item | Status |
|---|---|
| Packaging tool | **done**: setuptools -> sdist + pure-Python wheel; pipx for installation (`scripts/install.sh`) |
| Reproducible release build from a clean environment | **done/CI**: `scripts/release.sh` refuses a dirty tree; CI `package` job builds and smoke-tests the wheel in a fresh venv |
| Bundle runtime and dependencies | **n/a** for Python (declared deps, pip resolves); **done** for Node: `setup` installs a pinned, checksum-verified Node and `npm ci` from the committed lockfile |
| Artifact per OS/arch | **done**: wheel is `py3-none-any`; Node tarball is chosen per OS/arch at setup (linux/darwin x64/arm64) |
| Required modules/assets/defaults bundled | **done**: tests assert `data/dsh.patch.yml` and the harness lockfile are in the package; wheel inspected |
| Test on a machine without Python | **n/a**: Python >= 3.10 is a documented prerequisite (`install.sh` checks it) |
| Paths with spaces and non-ASCII | **done**: `tests/test_portable.py`, and a real harness turn in a `wörk dir 日本` workspace |
| Launch from any folder, including removable drives | **done**: all state is under per-user dirs or `FREEHARNESS_HOME`; relocation tested |
| Remove debug flags, dev files, test credentials | **done**: `SHIM_DEBUG` is opt-in and documented in SECURITY.md; no credentials in the repo (`.gitignore` covers profiles/tokens/logs) |

## 2. Cross-platform compatibility (P0)
| Item | Status |
|---|---|
| Supported OS versions | **Linux x86-64 (Ubuntu 24.04): tested.** macOS 14 (arm64): runs in CI, **manual** end-to-end login not verified. Windows: **not supported** (see below) |
| Test on each supported OS | Linux **done**; macOS unit tests in CI only; **manual** for live sites |
| CPU architectures | x86-64 tested; arm64 **open/manual** (Node tarball selection handles it) |
| Platform paths, permissions, file ops | **done**: XDG/Application Support/LOCALAPPDATA via `paths.py`; 0700/0600 modes |
| Native libs / OS binaries | Chrome required (`doctor` checks); Playwright's Python package ships its driver; no other native deps |
| macOS Gatekeeper/quarantine | **n/a**: no downloaded executables are launched; Node tarball is fetched with urllib (no quarantine flag) and checksum-verified |
| Linux shared-library requirements | Chrome's own; Node official glibc build |
| Windows Defender/AV | **n/a**: Windows unsupported. Reason: Chrome's app-bound cookie encryption cannot be shared between a plain-Chrome login and Playwright, and the process model assumes POSIX |

## 3. Application reliability (P0)
| Item | Status |
|---|---|
| Core workflows end to end | **done**: eval suite (8 tasks) per provider; unit tests; real-harness tests against a stub model |
| Invalid inputs / missing files | **done**: shim returns 400/404/413 (tested); clear errors for missing harness/Chrome/login |
| Permission errors, disk full, unavailable paths | **partial**: unwritable log dir falls back to stderr (tested); other write failures surface the OS error |
| Corrupt/malformed input | **done**: malformed request bodies, truncated and duplicated tool-call blocks (tested) |
| Prevent data loss on failure | **done**: the agent runs in DSH's `workspace-write` sandbox; sessions are persisted by the harness; no freeharness code rewrites user files |
| Interrupted operations / unexpected shutdown | **done**: Ctrl-C restarts the runtime and resumes the session (tested); SIGTERM closes browsers |
| Temp files cleaned up | **done**: eval workspaces are removed; the Node download uses a TemporaryDirectory |
| Duplicate instances | **done**: the shim port is the lock (second start reuses the running shim); Chromium locks the profile |
| Clean start/stop, repeated launches | **done**: tested repeatedly during development |
| Known critical/high defects | **none open**; see Known limitations |

## 4. Security and privacy (P0)
| Item | Status |
|---|---|
| Dependency vulnerability audit | **CI**: `pip-audit` (clean); `npm audit` gate at *critical*. Moderate/high advisories in the harness tree are documented and accepted in SECURITY.md |
| No embedded keys/credentials | **done**: none; the shim token is generated per install |
| Untrusted inputs | **done**: model output is treated as untrusted (tool names whitelisted; JSON only); HTTP bodies size-limited |
| Unsafe deserialization | **done**: JSON only; no pickle/yaml-load of untrusted data |
| Safe subprocess use | **done**: argument lists, no `shell=True`; Node tarball extracted with `filter="data"` (traversal test) |
| Restrict filesystem operations | **done**: harness sandbox `workspace-write`; our writes only under the data/config/state dirs |
| Not as root | **done**: `doctor` warns |
| No secrets in logs | **done**: logs hold sizes/outcomes only (tested for diagnostics) |
| Third-party licenses | **done**: `scripts/licenses.py` -> THIRD_PARTY_LICENSES.md, fails on unreviewed non-permissive licenses |
| Network exposure | **done**: loopback only, Host check, bearer token (tests) |

## 5. Portable data and configuration (P0)
All **done** and tested in `tests/test_portable.py`: pathlib everywhere; per-user OS directories; the install directory is never written to; user data (profiles, sessions, token) is separate from package resources; nothing persistent in temp dirs; data survives upgrades (it is outside the package); read-only log dir falls back; the data folder can be moved (`FREEHARNESS_HOME`). Settings stay on the machine (docs/PRIVACY.md).

## 6. Testing and release quality (P0)
| Item | Status |
|---|---|
| Unit tests for critical logic | **done**: parser, router, cooldowns, auth, paths, setup, eval gate |
| Integration tests (filesystem, external tools) | **done**: real DSH runtime against a stub OpenAI server, including interrupt/resume and a pty REPL |
| Regression tests for fixed bugs | **done**: nbsp/duplicate/truncated tool-call blocks, Monaco clipboard, shared-browser tab handling, restart race |
| Automated tests in CI | **CI** (Linux + macOS, Python 3.10 and 3.12) |
| Test the packaged artifact | **CI/done**: wheel installed in a clean venv, `version` and `doctor --report` run |
| Clean VM / physical machine | **manual**: developed on Ubuntu 24.04 desktop; a clean-VM pass is still **open** |
| Large/unusual inputs, resource limits | **done**: prompt budget -> `context_length_exceeded`; 413 for oversized bodies |
| Smoke test per release artifact | **CI** |
| Known limitations recorded | **done**: README "Limitations" |

## 7. Logging and diagnostics (P1)
**done**: readable errors; rotating logs (`freeharness.log`, 1 MB x 3; `shim.log` rotated at each launch above 5 MB, **not** while running); version/OS/arch in `doctor --report`; unexpected exceptions logged without content; log location printed by `doctor`; no telemetry.

## 8. Updates and version management (P1)
| Item | Status |
|---|---|
| Versioning | **done**: SemVer, `CHANGELOG.md` (Keep a Changelog) |
| Update process | Manual by design: `pipx upgrade` / re-run `scripts/install.sh`; no self-updater (a self-updating tool that drives logged-in sessions is an unnecessary attack surface) |
| Checksums and signing | **CI**: `SHA256SUMS` + GitHub build-provenance attestation (Sigstore) on tagged releases |
| Verify authenticity before execution | verify with `sha256sum -c` and `gh attestation verify` |
| Replace older release with newer | **done by design**: data lives outside the package |
| Recovery path | `pipx install freeharness==<previous>` from the release assets; data untouched |
| Document security fixes / supported versions | SECURITY.md, CHANGELOG |

## 9. User experience and documentation (P1)
**done**: README quick-start, support matrix, first-run steps, troubleshooting (docs/TROUBLESHOOTING.md), privacy (docs/PRIVACY.md), license, issue reporting via GitHub. Name/icon/version metadata: version via `freeharness version`; icon **n/a** (CLI).

## 10. Distribution and launch (P0)
| Item | Status |
|---|---|
| Archive per platform | **n/a**: one wheel + sdist |
| Clear filenames | **done**: `freeharness-<version>-py3-none-any.whl` |
| Verify archives extract | **CI** wheel install |
| Test downloads from distribution location | **manual** at first tagged release |
| Checksums vs published artifacts | **CI/done** (`SHA256SUMS`) |
| Sign binaries | **n/a** (no binaries); provenance attestation instead |
| Redistribution rights | **done**: MIT harness; license review in THIRD_PARTY_LICENSES.md |
| Rollback download | previous releases stay on the Releases page |
| Final checklist + post-release smoke test | this file + CI `package` job |

## Known limitations
- Only providers that pass the eval are in rotation: Gemini and Qwen today (docs/PROVIDERS.md). Free tiers are small and Qwen may show a verification page after heavy use; the router skips a blocked provider, but if both are blocked or out of quota the agent waits (HTTP 429 + Retry-After).
- Web UIs change without notice; selectors are the part that rots (`providers.py`), and `doctor`/eval exist to catch it.
- Each model step takes 15-120 s (browser round trip); long edits are slow.
- Linux x86-64 is the only platform verified end to end.
- Automating consumer chat sites may violate their terms; personal use at your own risk.
