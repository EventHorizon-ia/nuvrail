# Changelog

All notable changes to Nuvrail are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`0.1.0` is the first tagged release. Each `## [X.Y.Z] - YYYY-MM-DD` section
is published verbatim as that version's GitHub Release notes by
`.github/workflows/release.yml` (via `scripts/release_notes.py`), so keep the
heading format exact and add new work under `[Unreleased]`.

## [Unreleased]

## [0.1.2] - 2026-10-09

Security patch. The IMAP and SMTP proxies now fail closed on commands they do
not classify (details below); before this release, write-capable IMAP
extensions and SMTP `BDAT` could reach the real server without approval on
servers that support them. Upgrading from 0.1.1 is recommended. The web UI
also stops loading fonts from Google, so opening it makes no third-party
request by default.

### Security
- The IMAP and SMTP proxies now fail closed. Previously the IMAP proxy
  forwarded any command verb it did not recognise as a read, so write-capable
  IMAP extensions (`REPLACE`, which appends and expunges; `SETACL`/`DELETEACL`,
  which share a mailbox; `SETMETADATA`; `SETQUOTA`) and `COMPRESS` could reach
  the real mailbox without approval on servers that support them (typically
  Dovecot/Cyrus, not Gmail). The SMTP proxy likewise forwarded unknown verbs
  after AUTH, including `BDAT`, which on CHUNKING-capable servers could
  complete a message transaction without going through `DATA` staging. Now only an explicit allowlist is
  forwarded; everything else is refused locally (`NO [CANNOT]` / `502 5.5.1`).
  Non-synchronizing IMAP literals are refused and `LITERAL+`/`LITERAL-`/
  `COMPRESS`/`REPLACE`/`STARTTLS`/`IMAP4rev2` are no longer advertised to the
  agent. Upgrading is recommended.

### Changed
- The web UI no longer loads fonts from Google Fonts
  ([#184](https://github.com/agens-field/nuvrail/issues/184)). Inter and
  Montserrat (SIL OFL 1.1) are now served from the web container itself, so
  opening the approval UI makes no third-party request by default, and the CSP
  drops `fonts.googleapis.com` / `fonts.gstatic.com`. A CI test now fails if
  `index.html` or the stylesheet references another origin.
- The API reports its version from the installed package metadata instead of
  a hardcoded string, so it cannot drift from the release
  ([#177](https://github.com/agens-field/nuvrail/issues/177)).

### Fixed
- The audit view now offers Undo for flag/unflag operations, and its op-type
  filter lists the types the gateway actually records (`flag`, `unflag`,
  `store` added; `archive`/`star`/`unstar`, which never matched anything,
  removed). Archived messages are still filtered via the intent filter
  ([#178](https://github.com/agens-field/nuvrail/issues/178)).
- SPEC.md documents the SMTP rejection notice as `214`, which is what the
  proxy sends, not `550`
  ([#177](https://github.com/agens-field/nuvrail/issues/177)).

## [0.1.1] - 2026-10-01

Patch release: four approval-path correctness fixes in the gateway. Upgrading
from 0.1.0 is recommended. One behaviour change: a move, archive or trash
executed before you upgrade cannot be undone afterwards, because undo now needs
the destination UID that 0.1.0 did not record (see #170 below).

### Fixed
- An approved "Move to Trash" now actually moves the message to Trash
  ([#168](https://github.com/agens-field/nuvrail/issues/168)). Previously the
  approved op ran as `UID STORE +FLAGS (\Deleted)` on the real server: Gmail
  archived the message, and on Dovecot/Fastmail it stayed flagged until the
  human's own client expunged it, with no Trash copy. Now the Trash folder is
  resolved when the op is staged (the server's RFC 6154 `\Trash` folder, else
  the provider profile's) and the approved op runs as `UID MOVE` into it. If no
  Trash folder is known, the op is staged and labelled "Mark deleted (no Trash
  folder found)" and runs as the `\Deleted` flag. If the server lacks `MOVE`,
  it falls back to the flag and the fallback is recorded in the `executed`
  audit row. The gateway still never expunges. Undo of a trash op moves it back
  from Trash.
- Undo of a move, archive or trash no longer targets the wrong message
  ([#170](https://github.com/agens-field/nuvrail/issues/170)). IMAP UIDs are
  per-mailbox, but undo reused the message's source-folder UID in the
  destination folder, so it could move an unrelated message back, or move
  nothing and still report the op `reverted`. The executor now records the
  destination UID and `UIDVALIDITY` from the server's `COPYUID` response in the
  `executed` audit row, and undo moves exactly that UID back. Undo refuses, and
  the op stays `executed`, when that UID was not recorded (the server lacks
  UIDPLUS, or the op ran before this fix), when the folder's `UIDVALIDITY`
  changed, or when the move back finds nothing. A partial move back says
  "N of M" in the audit row.
- Undo now quotes mailbox names, so undoing a move out of a folder whose name
  has a space (Outlook `Deleted Items`, iCloud `Deleted Messages`) no longer
  sends a malformed `SELECT`.
- Undo of a flag or unflag (star/unstar in the UI) now works
  ([#173](https://github.com/agens-field/nuvrail/issues/173)). The undo
  allowlist named `star`/`unstar`, but staged ops carry `flag`/`unflag`, so
  every such undo was refused as "not undoable".
- An approved op with an op type the executor has no handler for now fails
  instead of reporting success
  ([#174](https://github.com/agens-field/nuvrail/issues/174)). It used to be
  logged, skipped, and marked `executed` with nothing done upstream. It is now
  marked `failed` with an `execution_failed` audit row. No op type the proxy
  stages today hits this path; archive is staged as a move.

## [0.1.0] - 2026-09-23

First tagged release of Nuvrail: a self-hostable approval gateway for AI
agents' email. It is an IMAP/SMTP proxy that stages every write (move, delete,
flag, outbound send) for human approval while letting reads pass through
instantly. `EXPUNGE` is blocked at the gateway, so nothing is permanently
deleted without sign-off. Changes since the 2026-06-15 feature-complete build
are listed below; the full feature set is summarised under
[Pre-release history](https://github.com/agens-field/nuvrail/blob/v0.1.0/CHANGELOG.md#pre-release-history-untagged)
in `CHANGELOG.md`.

### Added
- Unauthenticated `GET /api/v1/config` returning the deployment's `signup_mode`
  (`closed` | `invite` | `open`), so the web app can hide — not merely reject —
  the account-creation UI on closed/invite deployments. Closed shows an
  "unavailable" notice; invite surfaces a required invite-code field; open is
  unchanged. The server-side 403 on `/auth/register` remains the sole source of
  truth; the client mode is UX only and fails closed. (GH #139)
- Community & contributor docs for the open-source launch: `CONTRIBUTING.md`
  (build/test/PR workflow, AGPL-3.0/DCO stance) and `SECURITY.md` (coordinated
  disclosure policy).
- Deployment knobs in `.env` so a self-host box needs no file edits:
  `NUVRAIL_HOST_WEB_PORT` / `_API_PORT` / `_IMAP_PORT` / `_SMTP_PORT` remap host
  ports when one is already taken, `NUVRAIL_BIND_ADDR` (default `127.0.0.1`)
  serves the app to other machines, `NUVRAIL_PROXY_HOST` sets the hostname the
  Agents screen hands an agent, and `NUVRAIL_API_URL` supplies an absolute API
  URL for split deployments where the gateway is not on the compose network.

### Fixed
- Web client now distinguishes a network/wiring failure from an HTTP error
  response. A `fetch()` that never reaches the API (server down, wrong
  `VITE_API_URL`/baked-in localhost, mixed content, CORS) previously surfaced
  the browser's bare "Load failed" / "Failed to fetch", indistinguishable from a
  server rejection. It now throws a typed `NetworkError` naming the target URL
  and the likely cause; HTTP 4xx/5xx errors are left exactly as-is. (GH #140)
- Self-hosted web app now calls the API on its **own origin**. The web container
  proxies `/api/` to the gateway over the compose network, restoring the block
  removed in `2830931` — routed through a variable this time, so nginx still
  boots where the `gateway` host does not resolve (fly.io runs web and gateway
  as separate apps, which is why it was dropped). Previously an absolute
  `VITE_API_URL` was baked into the bundle, making the API a second origin and
  tying three files to one value: the compose port, the CSP `connect-src`
  allowlist, and `NUVRAIL_CORS_ORIGINS`. No localhost origin was ever in
  `connect-src`, so the documented local quickstart was CSP-blocked, and any
  port remap or remote-host deployment failed with an opaque "Couldn't reach the
  Nuvrail API at ...". Same-origin drops CORS from the picture entirely and takes
  the host and port out of the bundle, so remapping a port no longer needs a
  rebuild. The production nginx already routed `/api/` this way, so TLS
  deployments are unaffected.
- `NetworkError` now names the page's own origin when the client is built for
  same-origin use, instead of reporting an empty target URL.
- Removed a stray bottom "MIT License" block in the README that contradicted the
  project's AGPL-3.0 license declaration.

### Security
- The API keys its per-IP login lockout and rate limits on the client address,
  which the new `/api/` proxy would have collapsed into a single global bucket —
  one attacker could throttle or lock out every user. uvicorn now honours
  `X-Forwarded-For`, but only when `NUVRAIL_FORWARDED_ALLOW_IPS` names the peers
  allowed to set it (compose sets `*`; the API port is published on loopback
  only), so a directly exposed gateway keeps using the real socket peer.
- Both the web container and the documented TLS front end now **set** rather than
  append `X-Forwarded-For`. uvicorn trusts the first entry, so an appended header
  would have preserved a caller-supplied address and let anyone forge the client
  IP used by the login lockout.

## Pre-release history (untagged)

Backfilled history of notable milestones before the first version tag,
reconstructed from the commit log. Dates are when the work landed on `main`.
These are milestones, not versions: none was ever published as a release.

### 2026-06-15 — Feature-complete self-host build

First self-hostable, feature-complete build of the approval gateway: an
IMAP/SMTP proxy that stages every write (move, delete, flag, outbound send) for
human approval while letting reads pass through instantly. `EXPUNGE` is blocked
at the gateway — nothing is ever permanently deleted without sign-off.

#### Added
- **Multi-provider mail support** — Gmail/Google via OAuth2 (XOAUTH2 SASL) with
  a web connect flow from the Agents UI, plus separate IMAP/SMTP host support;
  generic IMAP/SMTP via credential passthrough.
- **Auto-approval rules hook** — the auto-approval rules engine, its `/rules`
  API, and the Rules screen ship in the separate `nuvrail-enterprise` plugin, not
  in this open-source build. Core keeps a plugin hook, the `auto_approval_rules`
  table (so account data export still works), and hides the Rules screen unless
  the plugin reports the feature. Without the plugin, every write waits for a
  human.
- **Operation intent labels** — human-readable summaries of what an agent wants
  to do (move, reply/forward, special-use folder actions), including batch
  intent summaries for review at a glance.
- **Web PWA approval app** — React + Vite progressive web app with Web Push
  notifications so approvals can happen from a phone or browser.
- **Audit chain verification** — tamper-evident audit log with chain
  verification, plus export completeness checks.
- **Staging-loop health monitoring** — end-to-end staging monitor and
  credentialed proxy health checks; external uptime monitoring config and an
  on-call monitoring runbook.
- **Send-rate controls** — per-account and daily send caps, send rate limiting,
  and agent suspension enforcement.

#### Security
- **Encryption at rest** — AES-256-GCM encryption of upstream mail credentials,
  wired end-to-end, with a key-rotation script.
- **Web hardening** — Content-Security-Policy headers, removal of
  `unsafe-inline`, and CORS that fails closed in production.
- **Secret hygiene** — upstream secrets deleted on account disconnect/deletion;
  hardened `.gitignore`; fixed a `~`-expansion bug in path handling.
- **Tenancy isolation** — per-user mailbox mirror scoping and tenant-scoped
  operation previews so users cannot see each other's data.

#### Legal / Compliance
- **GDPR erasure path** — right-to-erasure support that anonymizes and purges
  user data.
- **AGPL-3.0 source-availability footer** — in-app link to source, satisfying
  the AGPL network-use obligation.

### 2026-03-22 — Pre-MVP staged-write system

Pre-MVP: the proxy became a real staged-write system with a database, an API,
and a web app, and credentials were encrypted at rest.

#### Added
- Staging engine backed by SQLite with a minimal REST API.
- Optimistic MOVE — messages leave the source folder in local state at staging
  time for a responsive review experience.
- Nuvrail Approval Web App (React + Vite PWA) and Web Push notifications.
- End-to-end test suite covering the staged approve/reject flow.

#### Security
- AES-256-GCM credential encryption at rest.

### 2026-03-18 — Earliest working proxy

Earliest working proxy: raw asyncio TCP with LOGIN passthrough, then SMTP with
STARTTLS upstream and DATA staging.

#### Added
- Raw asyncio IMAP TCP proxy with LOGIN passthrough.
- SMTP proxy with STARTTLS to the upstream server and outbound DATA staging.

[Unreleased]: https://github.com/agens-field/nuvrail/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/agens-field/nuvrail/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/agens-field/nuvrail/releases/tag/v0.1.0
