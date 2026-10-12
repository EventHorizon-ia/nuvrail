# Nuvrail IMAP/SMTP Approval Gateway — Specification

**Status:** v7
**Date:** August 19, 2026
**Authors:** Martin Modahl, Jack (CEO)
**Changelog:**
- v2 — security/auth model added, SMTP moved to launch, failure modes defined, schema updated, deployment URLs added, open questions closed
- v3 — multi-provider support (Google, Microsoft, Apple, generic IMAP/SMTP) added to Milestone 2; provider abstraction layer specified
- v4 — open source scope defined (superseded in v6: license is AGPL-3.0, not MIT/Apache)
- v5 — updated to reflect actual built state as of 2026-03-19
- v6 — full reconciliation against the codebase as of 2026-06-12. Everything previously "Phase 1/2 planned" that has since shipped is now documented as built: secret-store credential handling, Gmail XOAUTH2, hash-chained audit log + verification, web push, batching, undo, SMTP 214 rejection notice, Sent-folder write-back, auto-approval rules (enterprise plugin), per-user tenancy, GDPR retention/erasure/export, send rate caps, loop health. Decisions log updated with supersessions (§19).
- v7 — open-source-first framing for the public launch. This repository **is** the product: a free, self-hostable core (AGPL-3.0). A hosted deployment, where operated, is simply one deployment of this same core, not a separate paid product; multi-user/tenancy and secret-manager sections describe capabilities of the core, not a commercial tier. No behavioral spec change from v6.

---

## 1. Overview

### Problem

AI agents that interact with email must be given raw IMAP credentials today — which grants unrestricted access. There is no mechanism for a human to review and approve proposed email operations before they execute, no audit trail, and no rollback path. This is the primary reason email access is the most dangerous thing to give an AI agent.

### Solution

An **IMAP/SMTP Approval Gateway** — a proxy server that sits between the AI agent and the real email provider. The AI agent believes it is talking to a normal IMAP/SMTP server. All read operations pass through transparently. All write and send operations are **staged** — queued for human approval — before executing against the real provider.

The result: the AI can propose email changes freely, but nothing reaches the real mailbox without an approval decision against a readable diff.

### Design Principles

1. **Propose before execute** — every write and send is staged, never immediate
2. **Readable diffs** — approvals show exactly what will change, in human language
3. **Tamper-evident audit log** — every action (proposed, approved, rejected, executed) is written to an append-only, SHA-256 hash-chained log. The log is immutable while retained; on account deletion it is anonymized immediately and hard-purged after a bounded retention window (GDPR storage limitation — see §10)
4. **No deletes** — permanent deletion (`EXPUNGE`) is disabled at the gateway layer; archive/trash-move only
5. **Standard IMAP/SMTP on both ends** — no special client or agent modifications required
6. **Human review by default; automation is opt-in and audited** — out of the box every write requires per-action human approval. Users may opt into auto-approval rules (enterprise feature) that decide matching operations without per-action review; every rule decision is attributed in the audit log, deferred sends are cancellable during a cool-down, and rules support a shadow (observe-only) mode

---

## 2. Deployment Environments

| Environment | Base URL | Purpose |
|---|---|---|
| **Self-hosted** | operator-defined | Open-source core, Docker Compose |
| **Staging** | fly.io staging app | Pre-prod; E2E smoke target |
| **Hosted example** | `mail.nuvrail.com` (gateway) / `app.nuvrail.com` (web) | A hosted deployment of the open-source core, where operated |

**Ports:**
- IMAP proxy: `993` (TLS, fly edge / nginx) external; `10143` internal
- SMTP proxy: `465` (SMTPS, fly edge / nginx) external; `10587` internal
- REST API: `443` (HTTPS) external; `8080` internal
- Upstream: IMAP `993` SSL; SMTP `587` STARTTLS (certificates CA-validated)

---

## 3. System Architecture **[BUILT]**

```
┌────────────────────────────────────────────────────────────────────┐
│                        Nuvrail Gateway                             │
│                                                                    │
│  ┌──────────────────────┐    ┌──────────────────────┐              │
│  │   IMAP Proxy Server  │    │   SMTP Proxy Server  │              │
│  │   (993 / 10143)      │    │   (465 / 10587)      │              │
│  └──────────┬───────────┘    └──────────┬───────────┘              │
│             │                            │                          │
│  ┌──────────▼────────────────────────────▼───────────┐             │
│  │              Staging Engine                        │             │
│  │  intercept writes → Operation records + snapshots  │             │
│  │  intent labels · batching window · auto-decision   │             │
│  │  provider seam (rules, via plugin) · web push      │             │
│  └──────────────────────┬────────────────────────────┘             │
│                         │                                           │
│  ┌──────────────────────▼────────────────────────────┐             │
│  │     Core Data Layer (SQLite via aiosqlite)        │             │
│  │  staged_operations │ audit_log │ folders/messages │             │
│  │  users │ agent_credentials │ pending_reverts │    │             │
│  │  push_subscriptions │ auto_approval_rules         │             │
│  └──────────────────────┬────────────────────────────┘             │
│                         │                                           │
│  ┌──────────────────────▼────────────────────────────┐             │
│  │  Approval REST API (FastAPI) + background loops:  │             │
│  │  expiry · body scrub · cool-down scheduler ·      │             │
│  │  retention purge · audit-chain verify             │             │
│  │  (each heartbeats into /health)                   │             │
│  └──────────────────────┬────────────────────────────┘             │
└───────────────────────── │ ──────────────────────────────────────── ┘
                           │ HTTPS + Bearer token
          ┌────────────────▼─────────────────────┐
          │   Web App (React PWA)                 │
          │   Pending (incl. batches) · Audit ·   │
          │   Agents · Rules · Account · Setup    │
          └───────────────────────────────────────┘
```

### Components

| Component | Role | Status |
|---|---|---|
| **IMAP Proxy Server** | Presents as IMAP to AI agent; intercepts writes; passes reads | **BUILT** |
| **SMTP Proxy Server** | Presents as SMTP to AI agent; stages all sends | **BUILT** |
| **Local State DB** | Per-user mailbox mirror (headers, flags, folders) | **BUILT** |
| **Staging Engine** | Parses intercepted commands → Operation records | **BUILT** |
| **Intent labelling** | Classifies ops (archive / delete / mark_spam / …) using RFC 6154 special-use folder discovery | **BUILT** |
| **Batching** | Sliding-window grouping of related ops + batch summaries | **BUILT** |
| **Rejection Revert** | Snapshot pre-op state, restore on reject, inject FETCH to AI | **BUILT** |
| **Undo** | Reverse an executed IMAP op within 24h | **BUILT** |
| **Operation Expiry** | 48h background job; expires pending ops, reverts state | **BUILT** |
| **Approval REST API** | Fetch/approve/reject/undo ops, single + batch | **BUILT** |
| **Audit Log** | Append-only, SHA-256 hash-chained, verified hourly | **BUILT** |
| **Web Push** | VAPID notifications on staging (no PII in payloads) | **BUILT** |
| **Auto-approval rules engine** | Auto-decision provider via plugin seam | **BUILT** (enterprise plugin) |
| **Retention & erasure** | Two-stage account erasure, body scrub, data export | **BUILT** |
| **Approval Web App** | React PWA (see §9) | **BUILT** |
| **iOS App** | Native approval app (APNs) | Not built |

---

## 4. Security & Authentication Model

Three distinct access lanes. No credential from one lane can be used in another.

### 4.1 Lane 1 — Upstream provider credentials **[BUILT]**

The gateway authenticates to real email providers using credentials captured at agent setup (validated live against the upstream IMAP server before the agent row is created).

**Storage — backend-selectable secret store (`NUVRAIL_SECRET_BACKEND`):**
- `gcp-sm` / `aws-sm` (hosted service): the cloud secret manager is the store of record; the application database holds only a **v2 reference envelope** (`{"v":2,"backend":…,"ref":…}`), never the secret. Plaintext is fetched on demand into a short-TTL in-process cache.
- `local` (self-hosted default): AES-256-GCM envelope (`{"v":1,"iv":…,"ct":…}`) under a 32-byte master key (`NUVRAIL_MASTER_KEY` env var or protected key file). Per-value random 96-bit nonce.
- Reads dispatch on the stored envelope's own version, so plaintext→v1→v2 migrations are safe online.

**OAuth2 (Gmail / Google Workspace) [BUILT]:** XOAUTH2 upstream auth. Refresh tokens and client secrets live in the secret store; **access tokens are cached in process memory only and never persisted**. Outlook (Azure AD) and Apple iCloud are planned.

**Teardown:** agent disconnect, account deletion, and the retention purge all (a) revoke the Google OAuth grant at the provider and (b) delete the external secrets, before nulling the DB columns — nothing is orphaned in the secret store. Credentials are never logged (redaction filter) and never exposed to agents.

### 4.2 Lane 2 — AI agent credentials (IMAP/SMTP access) **[BUILT]**

- **Username:** `nuvrail_<hex>`; **token** generated once, shown once, stored bcrypt-hashed (rounds=10)
- Verified on every IMAP LOGIN / SMTP AUTH; revoked or suspended credentials rejected immediately
- Scoped to a single upstream inbox; multiple agents per user supported (no agent quota in the open core; see `gateway/entitlements.py`)

### 4.3 Lane 3 — Human credentials (web app / REST API) **[BUILT]**

- Email + password (bcrypt rounds=12) → long-lived bearer token, stored **SHA-256-hashed** at rest, shown once; self-serve rotation (`POST /account/token/rotate`)
- Password change, reset-request/reset flow (token-based), logout
- Login rate-limited (10/min) with lockout escalation (auth abuse protector); reset 5/min
- Accounts can be administratively **suspended** (403 on all API use — ToS enforcement)
- Token sent as `Authorization: Bearer`; web app keeps it in localStorage

### 4.4 Personal client access (not through gateway)

Personal email clients connect directly to the provider — the gateway is the AI lane only.

---

## 5. IMAP Proxy Server **[BUILT]**

### 5.1 Connection handling

- TLS at the edge (fly/nginx); authenticates Lane 2 credentials; one upstream connection per session
- Upstream auth: plain LOGIN (generic IMAP) or XOAUTH2 (Gmail)

### 5.2 Read operations — pass-through

`SELECT`, `EXAMINE`, `FETCH`, `SEARCH`, `LIST`, `LSUB`, `STATUS`, `NOOP`, `CAPABILITY`, `ID`, `LOGOUT`, `CHECK`, `SUBSCRIBE`, `UNSUBSCRIBE`, `NAMESPACE`, `IDLE`, `UNSELECT`, plus the read-only extensions `ENABLE`, `SORT`, `THREAD`, `ESEARCH`, `GETQUOTA`, `GETQUOTAROOT`, `GETACL`, `MYRIGHTS`, `LISTRIGHTS`, `GETMETADATA`, `XLIST`, forwarded directly (allowlist: `READ_COMMANDS` in `gateway/command_router.py`).

**Fail closed.** Any verb not in the read allowlist, the staged-write set (§5.3) or the blocked set (§5.5) is answered locally with `NO [CANNOT]` and never forwarded. That covers write-capable extensions such as `REPLACE`, `SETACL`/`DELETEACL`, `SETMETADATA`, `SETQUOTA`, and framing changes (`COMPRESS`, `STARTTLS`). Commands carrying a non-synchronizing literal (`{N+}`/`{N-}`) are consumed and refused, and `LITERAL+`, `LITERAL-`, `COMPRESS=*`, `REPLACE`, `STARTTLS` and `IMAP4rev2` are stripped from capabilities relayed to the agent. The per-user local mirror is updated from FETCH/SELECT/LIST responses; special-use folder attributes (RFC 6154) are captured for intent classification. Upstream→client is line-buffered so pending reverts can be injected before tagged responses.

### 5.3 Write operations — staged

| IMAP Command | Op Type | Snapshot | Reverts on reject |
|---|---|---|---|
| `STORE +FLAGS \Deleted` | trash | ✅ | ✅ |
| `STORE +FLAGS \Seen` | mark_read | ✅ | ✅ |
| `STORE +FLAGS \Flagged` | flag | ✅ | ✅ |
| `STORE -FLAGS *` | unflag/mark_unread | ✅ | ✅ |
| `COPY` / `MOVE` | copy / move | — | — |
| `EXPUNGE` | **blocked permanently** | — | — |
| `APPEND` | append (body stored until decision; scrubbed after) | — | — |
| `CREATE` / `RENAME` | create / rename | — | — |

On interception: snapshot pre-op flags → optimistic local update → `staged_operations` record (with derived intent label + batching window assignment) → consult the auto-decision provider (§7.5) → `OK [STAGED]` to the agent → web push to the human (unless a rule already decided).

Provider profiles normalize execution upstream (e.g. prefer native `MOVE` over `COPY`+`EXPUNGE`).

### 5.4 Human-readable descriptions **[BUILT]** — unchanged (`operation_parser.py`).

### 5.5 EXPUNGE blocking and APPEND handling **[BUILT]** — unchanged: EXPUNGE never forwarded; `\Deleted` → staged trash; APPEND literals consumed and staged without touching upstream.

---

## 6. SMTP Proxy Server **[BUILT]**

- SMTPS 465 external (TLS at edge); AUTH LOGIN/PLAIN verified against Lane 2
- All sends staged: DATA intercepted, full body + envelope stored in the op record (required for relay), 200-char preview for the approval card
- Fail closed after AUTH: only `MAIL`, `RCPT`, `DATA` (staged), `QUIT`, `NOOP`, `RSET`, `HELP`, `VRFY` reach upstream; `EHLO`/`HELO`/`AUTH` are handled locally. Anything else, notably `BDAT` (RFC 3030 CHUNKING, which would submit a message without `DATA`), gets `502 5.5.1` and is never forwarded
- **On approval:** relayed via `aiosmtplib` (STARTTLS, CA-validated) — *after* passing the outbound send rate caps (§6.1) — then a copy is **appended to the account's Sent folder** (discovered via RFC 6154, cached per agent), mirroring normal client behaviour
- **On rejection [BUILT]:** on the agent's next SMTP session, right after AUTH succeeds, the proxy sends one informational line per unnotified rejected op, oldest first, up to 10 per session: `214 [NUVRAIL] REJECTED <op_id>: <op description>` (`gateway/smtp_proxy.py`; `rejection_notified` tracking, so each op is notified once). It is a `214`, not a `550`: the rejected message was already accepted with `250` at submit time, so the notice is out-of-band and the session continues normally. Agents should parse `214 [NUVRAIL] REJECTED` lines for op IDs they sent.
- **On expiry:** as IMAP — status `expired`, audit logged
- Bodies (and APPEND literals) are scrubbed 7 days after a terminal state (§10)

### 6.1 Outbound send rate caps **[BUILT]** — anti-spam, fail-closed

Counted durably from executed `smtp_send` audit rows (recipients, not operations; survives restarts):

| Scope | Default | Env |
|---|---|---|
| Per agent / hour | 100 | `NUVRAIL_SEND_MAX_PER_WINDOW` |
| Per account / hour (all agents) | 300 | `NUVRAIL_ACCOUNT_SEND_MAX_PER_WINDOW` |
| Per account / day | 1,000 | `NUVRAIL_ACCOUNT_SEND_MAX_PER_DAY` |

A refused send fails the op and writes a `send_rate_exceeded` audit row. Any count error refuses the send (a control that fails open is not a control). Applies to both human-approved and rule-approved sends — both flow through `execute_operation`.

---

## 7. Staging Queue **[BUILT]**

### 7.1 Operation schema (actual)

```sql
CREATE TABLE staged_operations (
    id              TEXT PRIMARY KEY,       -- op_XXXXXX
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,       -- created_at + 48h (NUVRAIL_EXPIRY_HOURS)
    status          TEXT NOT NULL,          -- pending|approved|rejected|executed|failed|expired|cancelled
    op_type         TEXT NOT NULL,
    protocol        TEXT NOT NULL,          -- imap|smtp
    imap_command    TEXT,
    smtp_envelope   TEXT,                   -- JSON {from,to,subject,body,body_preview}
    description     TEXT NOT NULL,
    agent_id        TEXT,                   -- owning agent → owning user (tenancy)
    message_ids     TEXT, folder_from TEXT, folder_to TEXT,
    flags_add       TEXT, flags_remove TEXT,
    snapshot        TEXT,                   -- pre-op state for revert
    append_message  TEXT,                   -- APPEND literal (scrubbed post-decision)
    is_urgent       INTEGER NOT NULL DEFAULT 0,
    intent_label    TEXT, intent_confidence REAL,
    batch_id        TEXT,                   -- sliding-window batch membership
    decided_at      INTEGER, decided_by TEXT,
    executed_at     INTEGER,
    undo_expires_at INTEGER,                -- 24h undo window for executed IMAP ops
    scheduled_execute_at INTEGER,           -- cool-down deadline (approve_after rules)
    rejection_notified INTEGER NOT NULL DEFAULT 0,
    body_scrubbed_at INTEGER,               -- set by the scrub job
    error           TEXT
);
```

### 7.2 Batching **[BUILT]**

Related operations arriving within a sliding time window share a `batch_id`; the API returns intent-aggregated `batch_summaries` (e.g. "Inbox triage — 5 archived, 2 marked read") and batch approve/reject endpoints act on up to 100 ops. Batches are in-memory windows (lost on restart — acceptable; a `batches` table is a Postgres-era refinement).

### 7.3 Undo **[BUILT]**

`POST /operations/{id}/undo` reverses an executed IMAP operation within `undo_expires_at` (default 24h) by executing the inverse upstream. Undo of a move/trash/archive addresses the message by the UID the server assigned in the destination (`COPYUID`, RFC 4315 UIDPLUS), recorded in the `executed` audit row; if that UID was not recorded, the destination's `UIDVALIDITY` changed, or the inverse `MOVE` moves nothing, undo refuses and the op stays `executed` (#170). SMTP sends are not undoable (mail cannot be recalled) — which is why sends get the strictest review defaults.

### 7.4 Pending reverts **[BUILT]** — unchanged (per-UID revert queue; proxy injects unsolicited FETCH).

### 7.5 Auto-decision provider seam **[BUILT]**

`gateway/extensions.py` exposes a `nuvrail.plugins` entry-point group. The staging path calls `run_auto_decision(op)`; with no provider registered (open core) every operation follows the manual-approval flow. The **enterprise plugin** registers the rules engine as provider, plus the `/rules` CRUD API, plan-tier entitlements (free = 1 agent; pro = 5 + multi-agent; enterprise = unlimited + rules), and its own idempotent schema migrations.

Those plan tiers exist only inside the plugin; they are not limits on the open build. The public build ships `OpenCoreEntitlements` (`gateway/entitlements.py`): no agent quota (`GET /api/v1/features` reports `max_agents: null`), multi-agent enabled, and `assert_can_create_agent` never refuses. The one capability it reports as unavailable is `auto_approval_rules`, because the rules engine's code ships in the plugin, not because the open build gates it.

Rule capabilities (enterprise): priority-ordered first-match; actions `approve`, `reject`, `approve_after` (cool-down `delay_seconds`; the scheduler loop executes at the deadline; cancellable until then), `hold`; predicates over op type, sender/recipient/subject/flag/intent patterns, folders, message-count bounds, minimum intent confidence, active-hours window with timezone, per-agent scoping; per-rule hourly budget for approving actions; guardrail rules; **shadow mode** (logs the would-be decision, never acts). Every rule decision is audit-attributed (`actor='auto_rule'` + rule id/description).

---

## 8. Approval REST API **[BUILT]**

Base: `/api/v1`. Bearer auth everywhere except register/login/reset; suspended accounts get 403.

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register` · `/auth/login` · `/auth/logout` | login rate-limited |
| PUT | `/auth/password` | change password |
| POST | `/auth/reset-request` · `/auth/reset` | token-based reset |
| GET | `/auth/me` | profile |
| POST/GET/DELETE | `/agents` · `/agents/{id}` | create (live IMAP validation; token shown once) / list / disconnect (purges secrets + revokes OAuth grant) |
| GET | `/oauth2/google/start` · `/callback` · `/result` | Gmail OAuth connect flow |
| GET | `/operations` | list, filters, `batch_summaries` |
| GET | `/operations/{id}` | detail incl. message previews (tenant-scoped) |
| POST | `/operations/{id}/approve` · `/reject` · `/undo` | single-op decisions |
| POST | `/operations/batch/approve` · `/batch/reject` | up to 100 ids |
| GET | `/audit` · `/audit/{id}` · `/audit/export` | user-scoped, filterable (event/actor/agent/op_type/intent/time/search) |
| GET | `/agents/{id}/audit` | per-agent trail |
| GET | `/audit/verify` | on-demand chain verification (summary + chain head) |
| GET | `/push/vapid-key` · POST/DELETE `/push/subscribe` | web push |
| GET | `/account/token` · POST `/account/token/rotate` | token management |
| GET | `/account/export` | full GDPR export (see §10) |
| DELETE | `/account` | self-serve deletion (password-confirmed) |
| GET | `/features` | plan entitlements for the UI |
| * | `/rules` … | enterprise plugin (CRUD + shadow stats) |

All operations/audit/rules queries are scoped to the authenticated user's agents (tenancy — see §14).

---

## 9. Approval App (Web) **[BUILT]**

React 18 + Vite + TypeScript + Tailwind PWA (installable; service worker). Views: **Login**, **Setup** (account + unified agent-add wizard incl. Google OAuth), **Pending** (live-refresh cards, batch UI, urgency, undo), **Audit** (filters, inline expansion, export), **Agents** (list/disconnect/per-agent audit), **Rules** (enterprise; predicates + intent + shadow), **Account** (export, deletion, token rotate, password change), **OAuth callback**, **Reset request/confirm**. Web push subscription management; cookie-consent banner gating Plausible analytics; AGPL §13 "source available" footer link.

---

## 10. Audit Log, Retention & Erasure **[BUILT]**

**Hash chain:** every `audit_log` row carries `prev_hash` + `entry_hash` (SHA-256 over all fields + previous hash; genesis row anchors the chain; insertions serialized). The application never issues UPDATE/DELETE against the table.

**Verification:** an hourly background loop walks the chain and logs at ERROR on any break (the alerting hook); `GET /audit/verify` runs it on demand (summary only); the current chain head ships in every data export so users can independently detect later rewrites.

**Events:** `staged`, `approved`, `rejected`, `executed`, `execution_failed`, `expired`, `body_scrubbed`, `shadow`, `send_rate_exceeded`, `export_requested`, `account_deleted`. Actors: `ai_agent` / `human` / `system` / `auto_rule`. Rows carry user/agent attribution + intent labels; all queries user-scoped.

**Retention & erasure (GDPR-aligned):**
- Message bodies (SMTP + APPEND) scrubbed **7 days** after a terminal decision (`NUVRAIL_BODY_SCRUB_DAYS`, hourly job, audit-logged)
- Account deletion is **two-stage**: (1) immediate — pseudonymize the user row, null credentials (and delete them from the external secret store + revoke the OAuth grant), revoke agents, cancel pending ops, drop push subscriptions; audit log retained but no longer linkable; (2) after `NUVRAIL_RETENTION_DAYS` (default **365**) a daily sweep hard-deletes every remaining row for the account, including its mailbox-mirror rows
- **Export** (`GET /account/export`): account, agents (never credential values), operations, audit rows incl. hash-chain fields + the chain head, rules, mailbox-mirror envelope metadata, push endpoints (no key material)

The log is therefore *immutable while retained*, not retained forever — public copy must match this wording.

---

## 11. Rejection Handling — IMAP Mechanics **[BUILT]** — unchanged (snapshot restore → pending_reverts → unsolicited FETCH before tagged OK; no IMAP extensions required). SMTP rejections additionally surface a `214 [NUVRAIL] REJECTED <op_id>: <op description>` notice right after AUTH on the agent's next session (§6).

---

## 12. Failure Modes

| Failure | Behavior |
|---|---|
| Upstream IMAP/SMTP connection fails | `* BYE` / `421` to client |
| Approved op fails upstream | status `failed`; `execution_failed` audit event |
| Operation expires (48h) | status `expired`, state reverted, audited |
| Send-cap count query fails | **send refused** (fail closed) |
| Snapshot revert fails on reject | non-fatal; rejection succeeds, warning logged |
| Background loop dies or its work errors | per-loop heartbeats on `GET /health` (`loops_ok`, per-loop staleness = 3× its interval, 120s floor); probe stays 200 — alerting keys on the body |
| `NUVRAIL_CORS_ORIGINS` unset in production | **API refuses to start** (no silent wildcard CORS); dev/test keep permissive default |
| Secret-store fetch fails | auth/execution fails closed; deletion paths are best-effort with the retention sweep as backstop |

---

## 13. Tech Stack (actual)

| Component | Technology |
|---|---|
| IMAP / SMTP proxies | Python 3.11 + asyncio (custom IMAP4rev1 state machine; single-coroutine SMTP) |
| Upstream connections | aioimaplib / aiosmtplib, TLS CA-validated; XOAUTH2 (Gmail) via httpx |
| Credential storage | GCP/AWS Secret Manager (hosted; lazy SDK extras) or AES-256-GCM local |
| Data layer | SQLite via aiosqlite; idempotent startup migrations (Postgres planned) |
| REST API | FastAPI + uvicorn + slowapi (rate limits) |
| Background jobs | asyncio tasks in FastAPI lifespan + loop-health heartbeats |
| Web push | VAPID (pywebpush) |
| Web app | React 18 + Vite + TypeScript + Tailwind, PWA, @tanstack/react-query |
| Auth | bcrypt (12 human / 10 agent); SHA-256-hashed bearer tokens |
| Testing / CI | pytest + pytest-asyncio + httpx — **~780 tests**; ruff; gitleaks; staged deploys (CI → manual dispatch → staging → E2E smoke → prod) |
| License | **AGPL-3.0 (core)**; enterprise plugin proprietary |

---

## 14. Database Schema Summary (actual)

| Table | Purpose | Notes |
|---|---|---|
| `users` | Human accounts | + `suspended_at`, `deleted_at` (tombstone), reset tokens |
| `agent_credentials` | Agent tokens + upstream config | secret-store reference envelopes; OAuth2 fields; cached `sent_folder` |
| `staged_operations` | Pending/decided operations | see §7.1 |
| `audit_log` | Append-only, hash-chained event log | + `user_id`, `prev_hash`, `entry_hash`, `intent_label` |
| `pending_reverts` | Per-UID revert queue | unchanged |
| `folders` / `messages` | Local mailbox mirror | **per-user** (`folders.user_id`, `UNIQUE(user_id, name)`) — names are unique per tenant, never globally; purged with the account |
| `push_subscriptions` | Web Push registrations | user-scoped |
| `auto_approval_rules` | Rule definitions | table in core; predicates/actions/guardrail columns added by the enterprise plugin migration; evaluation is enterprise-only |

**Tenancy:** any multi-user deployment (hosted or self-hosted) runs on a shared database. Every read/write path — operations, audit, rules, mailbox mirror, message previews, push — is scoped to the owning user. Folder lookups resolve by `(user_id, name)`, never name alone.

---

## 15. Milestones

### Shipped (Phases 0–2a)

Proxies, staging, reverts, expiry, REST API, web PWA, audit API — plus everything v5 listed as planned: Gmail XOAUTH2, credential secret store, web push (VAPID), batch approve/reject (API + UI), undo (API + UI), SMTP 214 rejection notice, urgency, per-agent audit, intent labelling + special-use discovery, provider profiles, auto-approval rules engine (enterprise plugin: predicates, cool-down, guardrails, shadow), plan entitlements, account maintenance (export / delete / token rotate / password reset), hash-chained + verified audit log, two-stage GDPR erasure + body scrub, send rate caps, per-user tenancy, loop health, CORS fail-closed, unified agent-add wizard.

### Next

| Item | Notes |
|---|---|
| Outlook (Azure AD) OAuth2 | Phase 2 provider work |
| Apple iCloud (app-specific password flow) | Phase 2 provider work |
| iOS app (SwiftUI + APNs) | proprietary |
| Postgres migration | Phase 3 (with durable batches table) |
| EU hosting region | fly.io region expansion |

---

## 16. Open Source Boundary

- **Core — AGPL-3.0** (supersedes v4's MIT/Apache decision): IMAP/SMTP proxies, staging engine, audit log + verification, retention/erasure, REST API, web PWA, Docker Compose. Public repo: `github.com/agens-field/nuvrail`. The hosted app's footer links the corresponding source (AGPL §13).
- **Proprietary**: the enterprise plugin (`nuvrail-enterprise` — rules engine, plan entitlements, admin API), deploy tooling, iOS app, Nuvrail Cloud operations. The plugin attaches through the `nuvrail.plugins` entry-point seam (§7.5); the public build has no providers registered and is fully functional with manual approval. Plan tiers and agent quotas are plugin policy; the public build enforces neither (§7.5).

---

## 17. Implementation Notes (architecture decisions)

**Single-coroutine SMTP vs. two-task IMAP** — unchanged from v5 (IMAP needs unsolicited server pushes; SMTP DATA needs synchronous control of both sides).

**Revert injection timing** — unchanged: unsolicited FETCH injected before the tagged OK (valid per RFC 3501).

**Secret store "Pattern B" (store-of-record):** long-lived secrets live in the cloud secret manager; the DB stores only a reference. This keeps the DB breach-blast-radius to references, makes deletion provable (delete the secret, not just the pointer), and leaves self-hosting dependency-free via the AES local backend. Short-lived OAuth access tokens deliberately stay in process memory to avoid secret-version churn.

**Durable send caps from the audit log:** the rate limiter counts executed sends from `audit_log` rather than an in-memory counter, so the ceiling survives deploys/restarts and needs no new write path. Fail closed.

**Tamper-evidence must be exercised:** a hash chain nobody verifies is decorative. Verification runs hourly, is exposed on demand, and the chain head is handed to users in their export.

**Plugin seam over forks:** enterprise capability registers into core extension points (auto-decision provider, migrations, entitlements, routers) so core stays self-contained and the public build's behavior is exactly "no provider → manual approval."

**DB path injection for testing** and **bcrypt round split (12 human / 10 agent)** — unchanged from v5.

---

## 18. Production Readiness Checklist

The v5 demo-blocker list is fully resolved: CORS is origin-allowlisted and fails closed in prod; upstream credentials are secret-store/encrypted (plaintext Phase 0 long gone); login and send paths are rate-limited; TLS terminates at the fly edge on all listeners.

End-to-end flow for a new user: register → connect mailbox (manual IMAP/SMTP or Google OAuth; live validation; token shown once) → point the agent at the proxy → agent proposes; ops stage with readable descriptions, intents, batches → push notification → approve/reject/undo from the PWA (or rules decide, audited) → full trail in Audit, exportable and chain-verifiable.

---

## 19. Decisions Log

| # | Question | Decision |
|---|---|---|
| 1 | SMTP proxy? | Yes — IMAP and SMTP together |
| 2 | Multi-agent support? | Multiple agent credential sets per user from day 1 (no quota in the open core) |
| 3 | Calendar scope? | Deferred — email only |
| 4 | Personal clients bypass gateway? | Yes — AI lane only |
| 5 | APPEND to Sent folder? | ~~Blocked at IMAP layer~~ **Superseded:** on an approved send, the gateway itself appends a copy to the discovered Sent folder (RFC 6154), mirroring normal client behaviour. Agent-initiated APPENDs remain staged writes. |
| 6 | APPEND to Drafts? | Staged as normal write |
| 7 | Single vs. multi-account? | Multiple agents per user; multi-upstream supported |
| 8 | Auto-approval for sends? | ~~Never~~ **Superseded:** opt-in enterprise rules may auto-approve sends, with safeguards — off by default, cool-down (`approve_after`) cancellable, shadow mode, per-rule hourly budgets, account/agent send caps, full audit attribution. Default behaviour is still per-action human approval. |
| 9 | Operation expiry? | 48h; `expired` distinct from `rejected` |
| 10 | Gmail vs. generic IMAP first? | Generic first; Gmail XOAUTH2 shipped |
| 11 | Auth: JWT vs. long-lived token? | Long-lived bearer token, SHA-256-hashed at rest, self-serve rotation |
| 12 | Agent token storage? | bcrypt hash only; plaintext shown once |
| 13 | Upstream cred encryption? | ~~Plaintext Phase 0~~ **Superseded:** secret-manager store-of-record (hosted) / AES-256-GCM (local); see §4.1 |
| 14 | Rejection revert timing? | Inject FETCH before tagged OK |
| 15 | Expiry distinct from rejection? | Yes |
| 16 | Open source scope? | ~~MIT/Apache~~ **Superseded:** core AGPL-3.0; enterprise plugin proprietary (§16) |
| 17 | Audit retained forever? | **No — superseded by GDPR alignment:** immutable while retained; two-stage erasure (anonymize at deletion, hard-purge after 365 days) |
| 18 | Tenancy on the hosted service? | Multi-user on a shared DB with strict per-user scoping everywhere, incl. the mailbox mirror (`UNIQUE(user_id, name)`) |
| 19 | Outbound abuse control? | Fail-closed send caps: 100/hr/agent, 300/hr/account, 1,000/day/account, counted durably from the audit log |
| 20 | Worker observability? | Per-loop heartbeats on `/health`; liveness stays 200, alerting keys on `loops_ok` |
