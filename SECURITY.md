# Security

## Reporting a vulnerability
Open a private security advisory on the GitHub repository (Security -> Report a vulnerability).
Do not post exploit details in a public issue. Expect an acknowledgement within a week.

Supported versions: the latest release only (0.x).

## Threat model
freeharness drives browsers that are signed in to **your** chat accounts and exposes them to a local
agent through an HTTP endpoint on `127.0.0.1:8766`. The assets are therefore (1) your account sessions
(`chrome-profile/`), (2) your chat quota, (3) your files, which the agent can read and modify.

| Risk | Control |
|---|---|
| Another local user or process drives your accounts | Bearer token (random, per install, mode 0600) required on every route except a liveness `/health`; profile and token directories are 0700/0600 |
| A web page in your browser reaches the shim (DNS rebinding) | Host header must be a loopback name; the server binds loopback only and refuses other hosts at startup |
| Agent damages your files | DSH runs in `workspace-write` sandbox mode; approvals are denied over the SDK; run it in a project directory, not `~` |
| Secrets in logs | Logs record sizes and outcomes only, never prompts, replies or file contents. `SHIM_DEBUG` (opt-in, off by default) writes raw replies and must not be shared |
| Malicious dependency install scripts | `npm ci --ignore-scripts`; lockfile committed; Node tarball checksum-verified |
| Running as root | `freeharness doctor` warns; do not |

## Known, accepted risk: npm advisories in the harness
`npm audit` on the pinned DeepSeek Harness reports advisories in four packages
(`@modelcontextprotocol/client`, `fflate`, `undici`, `zod`; moderate/high, all denial-of-service or
MCP-HTTP-transport issues; no critical) plus ~190 packages that merely depend on them. They cannot be
patched without `npm audit fix --force`, which breaks the harness. freeharness does not use MCP servers,
unzips nothing from untrusted sources, and the harness only talks to the loopback shim. CI fails on
**critical** advisories and the pin is reviewed on each harness release.

## Not a hosted service
Never expose the shim beyond loopback or share a profile directory: that hands out your logged-in
accounts. Automating a consumer chat site may breach that site's terms; this tool is for personal use.
