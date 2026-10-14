"""
Local SQLite state DB.

Mirrors mailbox state (folders, messages, flags) from the upstream server.
This is the source of truth for what the AI agent believes the mailbox looks like.

State transitions:
  upstream FETCH response → upsert messages table
  write interception      → optimistic update (proposed state)
  rejection               → revert to snapshot

Tenancy (issue #73) — the mirror is shared across ALL tenants on the hosted
gateway (one app, one volume, one SQLite file). Folders are therefore scoped
by owning user; messages inherit that scope through folder_id:

     users(id)
        │ 1
        │        UNIQUE(user_id, name)  ← two tenants may both own "INBOX"
        ▼ *
     folders(id, user_id, name, …)
        │ 1
        │        UNIQUE(folder_id, uid)
        ▼ *
     messages(id, folder_id, uid, subject, sender, …)

  get_or_create_folder(name, user_id=…)  ← the single scope chokepoint; every
  folder lookup/insert passes the connected agent's owning user_id, so a row
  (and all messages keyed off it) can never collide or leak across tenants.
  Pending-op lookups against staged_operations are scoped by agent_id for the
  same reason (folder name alone is no longer globally unique).

Sub-milestone: 0.4 (mailbox mirror tables)
Sub-milestone: 1.0 (staged_operations + audit_log tables)
"""
import json
import logging
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiosqlite

_DATA_DIR = os.environ.get("NUVRAIL_DATA_DIR", str(Path.home() / ".nuvrail"))
DB_PATH = Path(_DATA_DIR).expanduser() / "nuvrail.db"

# How long a connection waits for a held write lock before giving up with
# 'database is locked'. The gateway runs the IMAP proxy, the SMTP proxy, the
# REST API, and two background loops as SEPARATE processes sharing this one
# SQLite file, so concurrent writers are normal — they must wait, not error.
_SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("NUVRAIL_SQLITE_BUSY_TIMEOUT_MS", "5000"))

_logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _running_in_container() -> bool:
    """Best-effort container detection.

    The docker-entrypoint sets ``NUVRAIL_IN_CONTAINER=1``; ``/.dockerenv`` is a
    Docker-created marker file. Either signal is sufficient.
    """
    if os.environ.get("NUVRAIL_IN_CONTAINER"):
        return True
    return Path("/.dockerenv").exists()


def warn_if_loopback_bind_in_container(host: str) -> None:
    """Loud-but-non-fatal guard for a common self-host footgun.

    Under the documented Docker Compose deploy, Compose publishes the proxy
    ports to the *host* loopback (``127.0.0.1:10143`` etc.) and forwards them
    to the container's ``0.0.0.0``. If the proxy inside the container is bound
    to ``127.0.0.1`` instead, the forwarded traffic lands on an address
    nothing is listening on: the TCP handshake completes at the docker-proxy
    layer (so TLS/openssl appears to connect) but the application never sees
    the connection and never sends its IMAP/SMTP greeting. The symptom is a
    silent proxy that is maddening to debug.

    We warn rather than crash: binding to loopback is legitimate for a
    non-containerised run, so this is a footgun signal, not an error.
    """
    if host in _LOOPBACK_HOSTS and _running_in_container():
        _logger.warning(
            "NUVRAIL_PROXY_HOST=%s binds the proxy to loopback INSIDE the "
            "container. Under Docker, published ports forward to the "
            "container's 0.0.0.0, so connections will complete TLS but never "
            "reach this process (silent proxy, no greeting). Set "
            "NUVRAIL_PROXY_HOST=0.0.0.0 for the containerised deploy.",
            host,
        )


def decode_json_list(value: object) -> list:
    """Return a list from a JSON-array column (message_ids, flags, …).

    Columns are stored as a JSON string but may already be a Python list when
    passed in-process. Returns [] for None or an empty/blank string. Centralised
    so every reader coerces these columns the same way.
    """
    if isinstance(value, str):
        return json.loads(value) if value.strip() else []
    return value or []


async def _apply_connection_pragmas(db: "aiosqlite.Connection") -> None:
    """Apply concurrency/durability PRAGMAs to a freshly opened connection.

    Applied on every connection (cheap, idempotent) so behaviour is identical
    no matter which of the gateway's processes opened it:

    - ``busy_timeout``: block up to N ms waiting for another process's write
      lock instead of immediately raising ``database is locked``.
    - ``journal_mode=WAL``: Write-Ahead Logging lets many readers run
      concurrently with a single writer across processes — the default
      rollback journal serialises all access and is the root cause of lock
      errors under our multi-process layout. WAL is a persistent property of
      the DB file; re-asserting it per connection is a no-op once set.
    - ``synchronous=NORMAL``: the recommended, durable-enough setting under
      WAL (a crash can lose only the last transaction, never corrupt the DB),
      and noticeably faster than FULL.

    ``foreign_keys`` is intentionally left at its OFF default: the schema
    declares REFERENCES for documentation but the app never relied on
    enforcement, and turning it on now could reject existing rows.
    """
    await db.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA synchronous = NORMAL")

# Mailbox mirror schema — implemented in sub-milestone 0.4 (left intact)
SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER REFERENCES users(id),  -- owning user; the mailbox mirror is per-tenant (issue #73)
    name         TEXT NOT NULL,
    uidvalidity  INTEGER,
    uidnext      INTEGER,
    exists_count INTEGER,
    recent_count INTEGER,
    unseen_count INTEGER,
    last_synced  INTEGER,
    special_use  TEXT,  -- RFC 6154 role from the server's LIST attributes: 'archive'|'trash'|'junk'|'drafts'|'sent' (NULL = none declared)
    -- Folder names are unique *within a tenant*, not globally. Two users each
    -- owning an "INBOX" must get distinct folder rows (and therefore distinct
    -- message rows, which key off folder_id) so their mailbox metadata can
    -- never collide or leak across the shared hosted SQLite DB. See issue #73.
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id    INTEGER REFERENCES folders(id),
    uid          INTEGER NOT NULL,
    sequence_num INTEGER,
    message_id   TEXT,
    subject      TEXT,
    sender       TEXT,
    date_sent    INTEGER,
    size         INTEGER,
    flags        TEXT NOT NULL DEFAULT '[]',
    last_updated INTEGER,
    UNIQUE(folder_id, uid)
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id),  -- owning user; notifications go only to their own subscriptions
    endpoint   TEXT NOT NULL UNIQUE,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_approval_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER REFERENCES users(id),  -- owning user; rules apply only to that user's operations
    enabled        INTEGER NOT NULL DEFAULT 1,
    priority       INTEGER NOT NULL DEFAULT 0,
    op_type        TEXT,
    sender_pattern TEXT,
    folder_from    TEXT,
    action         TEXT NOT NULL,
    description    TEXT NOT NULL,
    created_at     INTEGER NOT NULL
);
"""

# Staging + audit schema — implemented in sub-milestone 1.0
_STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS staged_operations (
    id              TEXT PRIMARY KEY,       -- op_XXXXXX (6 random alphanum chars)
    created_at      INTEGER NOT NULL,       -- unix timestamp
    expires_at      INTEGER NOT NULL,       -- created_at + 48h
    status          TEXT NOT NULL,          -- 'pending'|'approved'|'rejected'|'executed'|'failed'|'expired'
    op_type         TEXT NOT NULL,          -- 'store'|'move'|'copy'|'append'|'create'|'rename'|'smtp_send'
    protocol        TEXT NOT NULL,          -- 'imap'|'smtp'
    imap_command    TEXT,                   -- raw IMAP command (null for smtp ops)
    smtp_envelope   TEXT,                   -- JSON {from, to, subject, body_preview} (null for imap)
    description     TEXT NOT NULL,          -- human-readable
    agent_id        TEXT,                   -- null for Phase 0
    message_ids     TEXT,                   -- JSON array of affected UIDs
    folder_from     TEXT,
    folder_to       TEXT,
    flags_add       TEXT,                   -- JSON array
    flags_remove    TEXT,                   -- JSON array
    snapshot        TEXT,                   -- JSON {uid: {flags, seq_num, folder_id}} pre-op state
    append_message  TEXT,                   -- base64 of the raw RFC822 body for APPEND ops (NULL otherwise)
    is_urgent       INTEGER NOT NULL DEFAULT 0,  -- 1 for smtp_send and trash ops (always show at top)
    intent_label    TEXT,                   -- semantic intent: 'archive'|'delete'|'mark_spam'|... (see gateway.intent); NULL = op_type says it all
    intent_confidence REAL,                 -- 1.0 = provider-profile/mechanical match, 0.8 = folder-name heuristic
    decided_at      INTEGER,
    decided_by      TEXT,
    executed_at     INTEGER,
    undo_expires_at INTEGER,
    scheduled_execute_at INTEGER,  -- cool-down deadline; the scheduler auto-executes a pending op once this passes (NULL = no deferral)
    rejection_notified INTEGER NOT NULL DEFAULT 0,  -- 1 once SMTP 214 REJECTED notice sent to agent
    error           TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    INTEGER NOT NULL,
    operation_id TEXT REFERENCES staged_operations(id),
    event        TEXT NOT NULL,             -- 'staged'|'approved'|'rejected'|'executed'|'execution_failed'
    actor        TEXT,                      -- 'ai_agent'|'human'|'system'
    agent_id     TEXT,
    intent_label TEXT,                      -- denormalized semantic intent (gateway.intent); NOT part of entry_hash
    detail       TEXT                       -- JSON
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE,
    display_name    TEXT,
    hashed_password TEXT NOT NULL,       -- bcrypt hash of human password
    api_token       TEXT UNIQUE,         -- SHA-256 hex digest of bearer token (never plaintext)
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_credentials (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    label              TEXT NOT NULL DEFAULT 'default',
    agent_username     TEXT NOT NULL UNIQUE,  -- e.g. "nuvrail_<hex8>"
    hashed_token       TEXT NOT NULL,          -- bcrypt hash of the agent token
    upstream_host      TEXT NOT NULL,
    upstream_smtp_host TEXT,                   -- NULL → falls back to upstream_host (e.g. smtp.gmail.com vs imap.gmail.com)
    upstream_imap_port INTEGER NOT NULL DEFAULT 993,
    upstream_smtp_port INTEGER NOT NULL DEFAULT 587,
    upstream_user      TEXT NOT NULL,
    upstream_password  TEXT,                   -- AES-256-GCM encrypted; NULL for OAuth2 agents
    -- OAuth2 fields (NULL for password-auth agents)
    oauth2_provider              TEXT,         -- 'google' | NULL
    oauth2_refresh_token         TEXT,         -- encrypted at rest (AES-256-GCM)
    oauth2_client_id             TEXT,         -- GCP OAuth2 client ID (not a secret)
    oauth2_client_secret         TEXT,         -- encrypted at rest (AES-256-GCM)
    oauth2_access_token          TEXT,         -- cached access token — encrypted
    oauth2_access_token_expires_at INTEGER,    -- unix timestamp; NULL = not yet cached
    created_at         INTEGER NOT NULL,
    revoked_at         INTEGER                 -- NULL = active
);

CREATE TABLE IF NOT EXISTS pending_reverts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES staged_operations(id),
    folder_id    INTEGER NOT NULL,
    uid          INTEGER NOT NULL,
    true_flags   TEXT NOT NULL,             -- JSON array: actual flags after revert
    created_at   INTEGER NOT NULL,
    delivered_at INTEGER                    -- NULL until proxy injects the unsolicited FETCH
);

-- Invite codes for NUVRAIL_SIGNUP_MODE=invite. A code is single-use: it is
-- "spent" atomically at redemption (redeemed_at set in the same UPDATE...WHERE
-- redeemed_at IS NULL that gates the register), so two concurrent registrations
-- cannot both consume one code. Codes are stored as a SHA-256 hex digest, never
-- plaintext (same discipline as api_token) — the raw code is shown once at mint
-- time on the admin CLI and never persisted.
--
--   mint (CLI)          redeem (/auth/register, invite mode)
--   ----------          -------------------------------------
--   INSERT row     ->   UPDATE ... SET redeemed_at=?, redeemed_by_email=?
--   (hash only)         WHERE code_hash=? AND redeemed_at IS NULL
--                         AND revoked_at IS NULL
--                         AND (expires_at IS NULL OR expires_at > now)
--                       rowcount==1 => valid & now spent; 0 => reject (403)
CREATE TABLE IF NOT EXISTS invite_codes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash         TEXT NOT NULL UNIQUE,  -- SHA-256 hex of the raw code; never plaintext
    created_at        INTEGER NOT NULL,
    expires_at        INTEGER,               -- NULL = no expiry (opt-in TTL)
    note              TEXT,                  -- optional admin label (e.g. "for alice")
    redeemed_at       INTEGER,               -- NULL until spent; single-use
    redeemed_by_email TEXT,                  -- who consumed it (audit)
    revoked_at        INTEGER                 -- NULL unless an admin killed it early
);
"""


async def init_db(path: Path = DB_PATH) -> None:
    """Initialize the database, creating tables if they don't exist."""
    import hashlib
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        # Switch the DB file to WAL (persistent) and set per-connection pragmas
        # before any schema work, so the very first process to touch the file
        # establishes WAL for everyone.
        await _apply_connection_pragmas(db)
        await db.executescript(SCHEMA)
        await db.executescript(_STAGING_SCHEMA)

        # Migration: re-hash any api_token values that are not already
        # SHA-256 hex digests (64 lowercase hex chars).  This handles
        # existing plaintext tokens from before the security hardening.
        async with db.execute(
            "SELECT id, api_token FROM users WHERE api_token IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            token = row["api_token"]
            if len(token) != 64 or not all(c in "0123456789abcdef" for c in token):
                hashed = hashlib.sha256(token.encode()).hexdigest()
                await db.execute(
                    "UPDATE users SET api_token = ? WHERE id = ?",
                    (hashed, row["id"]),
                )

        # Migration: add OAuth2 columns to agent_credentials if not present.
        # SQLite supports ADD COLUMN but not IF NOT EXISTS; we check the
        # schema directly to stay idempotent across repeated startups.
        async with db.execute(
            "PRAGMA table_info(agent_credentials)"
        ) as cur:
            col_rows = await cur.fetchall()
            existing_cols = {row["name"] for row in col_rows}
            # Also capture whether upstream_password still has NOT NULL
            password_col = next(
                (row for row in col_rows if row["name"] == "upstream_password"), None
            )
            password_is_not_null = password_col is not None and password_col["notnull"] == 1

        # Migration: add rejection_notified column to staged_operations if absent.
        # (existing_cols above is from agent_credentials — query staged_operations separately)
        async with db.execute("PRAGMA table_info(staged_operations)") as cur:
            staged_cols = {row["name"] for row in await cur.fetchall()}
        if "rejection_notified" not in staged_cols:
            await db.execute(
                "ALTER TABLE staged_operations "
                "ADD COLUMN rejection_notified INTEGER NOT NULL DEFAULT 0"
            )
        if "batch_id" not in staged_cols:
            await db.execute(
                "ALTER TABLE staged_operations ADD COLUMN batch_id TEXT"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_staged_ops_batch_id"
                " ON staged_operations(batch_id)"
            )
        # Migration: add body_scrubbed_at column to staged_operations (issue #29).
        if "body_scrubbed_at" not in staged_cols:
            await db.execute(
                "ALTER TABLE staged_operations ADD COLUMN body_scrubbed_at INTEGER"
            )
        # Migration: add append_message column — stores the base64 RFC822 body
        # of an APPEND op so it can be replayed upstream on approval.
        if "append_message" not in staged_cols:
            await db.execute(
                "ALTER TABLE staged_operations ADD COLUMN append_message TEXT"
            )
        # Migration: semantic intent labels (gateway.intent) derived at staging time.
        if "intent_label" not in staged_cols:
            await db.execute(
                "ALTER TABLE staged_operations ADD COLUMN intent_label TEXT"
            )
        if "intent_confidence" not in staged_cols:
            await db.execute(
                "ALTER TABLE staged_operations ADD COLUMN intent_confidence REAL"
            )

        new_columns = [
            ("oauth2_provider",              "TEXT"),
            ("oauth2_refresh_token",         "TEXT"),
            ("oauth2_client_id",             "TEXT"),
            ("oauth2_client_secret",         "TEXT"),
            ("oauth2_access_token",          "TEXT"),
            ("oauth2_access_token_expires_at", "INTEGER"),
            ("upstream_smtp_host",           "TEXT"),  # NULL → falls back to upstream_host
            ("sent_folder",                  "TEXT"),  # cached Sent folder (discovered via LIST)
        ]
        for col_name, col_type in new_columns:
            if col_name not in existing_cols:
                await db.execute(
                    f"ALTER TABLE agent_credentials ADD COLUMN {col_name} {col_type}"
                )

        # Migration: add reset_token columns to users if absent (issue #16),
        # and token-tracking columns (issue #28), and deleted_at (issue #26).
        async with db.execute("PRAGMA table_info(users)") as cur:
            user_cols = {row["name"] for row in await cur.fetchall()}
        for col_name, col_type in [
            ("reset_token", "TEXT"),
            ("reset_token_expires_at", "INTEGER"),
            ("api_token_created_at", "INTEGER"),
            ("api_token_last_used_at", "INTEGER"),
            ("deleted_at", "INTEGER"),
            # Account suspension (ToS §4/§13 enforcement, issue #65). NULL = active.
            # Distinct from deleted_at: suspension is reversible (un-suspend by
            # setting back to NULL) and retains the account + its data; deletion
            # is terminal. Enforced at API bearer auth, proxy agent auth, and the
            # execution path so a suspended account can neither stage nor send.
            ("suspended_at", "INTEGER"),
        ]:
            if col_name not in user_cols:
                await db.execute(
                    f"ALTER TABLE users ADD COLUMN {col_name} {col_type}"
                )

        # Migration: add per-agent columns to audit_log (issue #5 — Phase 2a).
        async with db.execute("PRAGMA table_info(audit_log)") as cur:
            audit_cols = {row["name"] for row in await cur.fetchall()}
        for col_name, col_type in [
            ("user_id",    "INTEGER"),   # direct user scope — avoids join on hot path
            ("op_type",    "TEXT"),       # denormalized from staged_operations
            ("prev_hash",  "TEXT"),       # hash chain: entry_hash of preceding row
            ("entry_hash", "TEXT"),       # SHA-256 of this row's fields
            # Denormalized like op_type so audit history keeps its semantics
            # after the operation row is retention-purged. NOT part of
            # entry_hash — see gateway/audit.py.
            ("intent_label", "TEXT"),
        ]:
            if col_name not in audit_cols:
                await db.execute(
                    f"ALTER TABLE audit_log ADD COLUMN {col_name} {col_type}"
                )
        # Idempotent index for per-agent queries.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_agent_id ON audit_log(agent_id)"
        )

        # Migration: add scheduled_execute_at to staged_operations for cool-down
        # auto-approvals. NULL on pre-existing rows means "no deferral", so the
        # scheduler ignores them. Indexed for the loop's due-op query.
        async with db.execute("PRAGMA table_info(staged_operations)") as cur:
            op_cols = {row["name"] for row in await cur.fetchall()}
        if "scheduled_execute_at" not in op_cols:
            await db.execute(
                "ALTER TABLE staged_operations ADD COLUMN scheduled_execute_at INTEGER"
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_staged_ops_scheduled"
            " ON staged_operations(scheduled_execute_at)"
        )

        # Migration: add user_id to auto_approval_rules so rules are tenant-scoped.
        # Rules predating this column have NULL user_id and intentionally match no
        # operation (fail closed) — a global rule must never act on another user's
        # mail. The owner must re-create such rules under their account.
        async with db.execute("PRAGMA table_info(auto_approval_rules)") as cur:
            rule_cols = {row["name"] for row in await cur.fetchall()}
        if "user_id" not in rule_cols:
            await db.execute(
                "ALTER TABLE auto_approval_rules ADD COLUMN user_id INTEGER"
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_auto_rules_user_id"
            " ON auto_approval_rules(user_id)"
        )

        # Migration: add user_id to push_subscriptions so notifications are
        # tenant-scoped. Subscriptions predating this column have NULL user_id
        # and receive nothing (fail closed) — a notification for one user's
        # operation must never be pushed to another user's device. The browser
        # re-subscribes on next load, repopulating user_id.
        async with db.execute("PRAGMA table_info(push_subscriptions)") as cur:
            push_cols = {row["name"] for row in await cur.fetchall()}
        if "user_id" not in push_cols:
            await db.execute(
                "ALTER TABLE push_subscriptions ADD COLUMN user_id INTEGER"
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_push_subs_user_id"
            " ON push_subscriptions(user_id)"
        )

        # Migration: scope the mailbox mirror per-user (issue #73).
        #
        # The legacy `folders` table had `name TEXT NOT NULL UNIQUE` and no
        # owner column. On a shared hosted DB with open registration that is a
        # cross-tenant leak: two users each connecting an "INBOX" collide on the
        # global UNIQUE(name) and intermingle the message metadata that keys off
        # folder_id. We rebuild the table with `user_id` + `UNIQUE(user_id, name)`.
        #
        #   detect old shape ──► rename ──► CREATE new ──► (wipe) ──► drop old
        #
        # We deliberately do NOT backfill a user_id onto pre-existing rows: the
        # mirror is a *cache* that the proxy repopulates on the next SELECT/LIST
        # per connection, and the old rows have no trustworthy owner (a globally
        # shared "INBOX" row cannot be safely attributed to one tenant). Wiping
        # the un-owned mirror is the only fail-closed option — a wrong
        # attribution would re-create the very leak we are closing. The orphaned
        # message rows are removed with it so no row outlives its folder.
        async with db.execute("PRAGMA table_info(folders)") as cur:
            folder_cols = {row["name"] for row in await cur.fetchall()}
        if "user_id" not in folder_cols:
            await db.execute("ALTER TABLE folders RENAME TO folders_legacy_unscoped")
            await db.execute("""
                CREATE TABLE folders (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER REFERENCES users(id),
                    name         TEXT NOT NULL,
                    uidvalidity  INTEGER,
                    uidnext      INTEGER,
                    exists_count INTEGER,
                    recent_count INTEGER,
                    unseen_count INTEGER,
                    last_synced  INTEGER,
                    UNIQUE(user_id, name)
                )
            """)
            # Drop the legacy table and any message rows that referenced it. The
            # mirror re-syncs per-user on next connection; nothing is lost that
            # the upstream server is not the system of record for.
            await db.execute("DROP TABLE folders_legacy_unscoped")
            await db.execute("DELETE FROM messages")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_folders_user_id ON folders(user_id)"
        )
        # Reply/forward intent lookups (find_message_by_message_id) run on every
        # staged SMTP send carrying In-Reply-To/References — without this index
        # each one is a full scan of the messages mirror.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_message_id"
            " ON messages(message_id)"
        )

        # Migration: add special_use to folders — the server-declared RFC 6154
        # role captured from LIST attributes, used for intent derivation.
        # Re-query table_info: the per-user rebuild above may have just
        # recreated the table (without this column).
        async with db.execute("PRAGMA table_info(folders)") as cur:
            folder_cols_now = {row["name"] for row in await cur.fetchall()}
        if "special_use" not in folder_cols_now:
            await db.execute("ALTER TABLE folders ADD COLUMN special_use TEXT")

        # Migration: drop NOT NULL from upstream_password to support OAuth2 agents.
        # SQLite has no ALTER COLUMN — we must rename, recreate, copy, and drop.
        # This is idempotent: the PRAGMA notnull flag is checked first.
        if password_is_not_null:
            await db.execute(
                "ALTER TABLE agent_credentials RENAME TO agent_credentials_old"
            )
            await db.execute("""
                CREATE TABLE agent_credentials (
                    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id                      INTEGER NOT NULL REFERENCES users(id),
                    label                        TEXT NOT NULL DEFAULT 'default',
                    agent_username               TEXT NOT NULL UNIQUE,
                    hashed_token                 TEXT NOT NULL,
                    upstream_host                TEXT NOT NULL,
                    upstream_imap_port           INTEGER NOT NULL DEFAULT 993,
                    upstream_smtp_port           INTEGER NOT NULL DEFAULT 587,
                    upstream_user                TEXT NOT NULL,
                    upstream_password            TEXT,
                    oauth2_provider              TEXT,
                    oauth2_refresh_token         TEXT,
                    oauth2_client_id             TEXT,
                    oauth2_client_secret         TEXT,
                    oauth2_access_token          TEXT,
                    oauth2_access_token_expires_at INTEGER,
                    created_at                   INTEGER NOT NULL,
                    revoked_at                   INTEGER
                )
            """)
            await db.execute("""
                INSERT INTO agent_credentials
                    SELECT id, user_id, label, agent_username, hashed_token,
                           upstream_host, upstream_imap_port, upstream_smtp_port,
                           upstream_user, upstream_password,
                           oauth2_provider, oauth2_refresh_token, oauth2_client_id,
                           oauth2_client_secret, oauth2_access_token,
                           oauth2_access_token_expires_at,
                           created_at, revoked_at
                    FROM agent_credentials_old
            """)
            await db.execute("DROP TABLE agent_credentials_old")

        # Run migrations registered by plugins (e.g. the enterprise plan_tier
        # column). No-op in the public build where nothing is registered.
        from gateway.extensions import get_migrations
        for migrate in get_migrations():
            await migrate(db)

        await db.commit()


@asynccontextmanager
async def get_db(path: Path = DB_PATH) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager yielding an aiosqlite.Connection.

    Sets the row factory and applies the WAL / busy_timeout / synchronous
    PRAGMAs required for safe concurrent access across the gateway's separate
    processes (see _apply_connection_pragmas).
    """
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await _apply_connection_pragmas(db)
        yield db


# ---------------------------------------------------------------------------
# Folder sync helpers
# ---------------------------------------------------------------------------


async def get_or_create_folder(
    name: str,
    *,
    user_id: "int | None",
    db_path: Path = DB_PATH,
) -> int:
    """Return folder_id for (user_id, name), creating a row if it doesn't exist.

    ``user_id`` is the owning tenant and is **required** (keyword-only) so the
    mailbox mirror is always scoped — see issue #73. It is passed explicitly
    rather than defaulted for the same reason ``db_path`` is: a forgotten scope
    is a cross-tenant leak, so call sites must supply it deliberately. It may be
    None only in genuinely single-tenant/legacy callers (e.g. tests), in which
    case all such rows share the NULL-owner namespace.

    Lookup and insert are both scoped by user_id, so two tenants each owning a
    folder named "INBOX" get distinct rows (and distinct message rows, which
    key off folder_id).
    """
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT id FROM folders WHERE user_id IS ? AND name = ?",
            (user_id, name),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return int(row["id"])
        cur = await db.execute(
            "INSERT INTO folders (user_id, name, last_synced) VALUES (?, ?, ?)",
            (user_id, name, int(time.time())),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def update_folder_stats(
    name: str,
    *,
    user_id: "int | None",
    exists_count: "int | None" = None,
    recent_count: "int | None" = None,
    uidvalidity: "int | None" = None,
    uidnext: "int | None" = None,
    unseen_count: "int | None" = None,
    db_path: Path = DB_PATH,
) -> int:
    """Upsert folder stats for the owning tenant. Returns folder_id.

    ``user_id`` is required (keyword-only) so stats land on the caller's own
    folder row and never the wrong tenant's — see issue #73.
    """
    folder_id = await get_or_create_folder(name, user_id=user_id, db_path=db_path)

    # Build UPDATE only for non-None fields
    updates: list[tuple[str, object]] = [("last_synced", int(time.time()))]
    if exists_count is not None:
        updates.append(("exists_count", exists_count))
    if recent_count is not None:
        updates.append(("recent_count", recent_count))
    if uidvalidity is not None:
        updates.append(("uidvalidity", uidvalidity))
    if uidnext is not None:
        updates.append(("uidnext", uidnext))
    if unseen_count is not None:
        updates.append(("unseen_count", unseen_count))

    set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
    values = [v for _, v in updates]
    values.append(folder_id)

    async with get_db(db_path) as db:
        await db.execute(
            f"UPDATE folders SET {set_clause} WHERE id = ?",  # noqa: S608
            values,
        )
        await db.commit()

    return folder_id


async def upsert_folders_from_list(
    folder_names: "list[str]",
    *,
    user_id: "int | None",
    special_use: "dict[str, str] | None" = None,
    db_path: Path = DB_PATH,
) -> None:
    """Create folder rows for all LIST names under the owning tenant. Idempotent.

    ``user_id`` is required (keyword-only) so a LIST never materialises folders
    under the wrong tenant — see issue #73.

    ``special_use`` maps folder name → RFC 6154 role ('archive'|'trash'|...)
    for folders whose LIST attributes declared one. A declared role is stored;
    a folder *absent* from the mapping keeps any previously stored role —
    plain LIST responses without SPECIAL-USE attributes must not erase
    knowledge captured from an earlier extended LIST.
    """
    for name in folder_names:
        folder_id = await get_or_create_folder(name, user_id=user_id, db_path=db_path)
        role = (special_use or {}).get(name)
        if role:
            async with get_db(db_path) as db:
                await db.execute(
                    "UPDATE folders SET special_use = ? WHERE id = ?",
                    (role, folder_id),
                )
                await db.commit()


async def find_message_by_message_id(
    message_id: str,
    *,
    user_id: "int | None",
    db_path: Path = DB_PATH,
) -> "dict | None":
    """Find a mirrored message by its RFC 5322 Message-ID, tenant-scoped.

    Used to match an outgoing reply/forward (via its In-Reply-To/References
    headers) back to the original message so the approval card can say
    'Reply to "Invoice #1234" from billing@acme.com'.

    Stored message_id values may or may not carry the surrounding angle
    brackets depending on which sync path captured them, so both forms are
    matched. Returns {uid, subject, sender, folder_name} or None.
    """
    if not message_id:
        return None
    bare = message_id.strip().strip("<>")
    if not bare:
        return None
    bracketed = f"<{bare}>"
    async with get_db(db_path) as db, db.execute(
        """
            SELECT m.uid, m.subject, m.sender, f.name AS folder_name
            FROM messages m
            JOIN folders f ON m.folder_id = f.id
            WHERE f.user_id IS ? AND m.message_id IN (?, ?)
            ORDER BY m.last_updated DESC
            LIMIT 1
            """,
        (user_id, bare, bracketed),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row is not None else None


async def get_special_use_folders(
    *,
    user_id: "int | None",
    db_path: Path = DB_PATH,
) -> "dict[str, str]":
    """Return {lower-cased folder name: role} for the tenant's special-use folders.

    The keys are lower-cased so the mapping can be passed directly to
    gateway.intent.classify_folder / derive_intent.
    """
    async with get_db(db_path) as db, db.execute(
        "SELECT name, special_use FROM folders "
        "WHERE user_id IS ? AND special_use IS NOT NULL",
        (user_id,),
    ) as cur:
        rows = await cur.fetchall()
    return {row["name"].lower(): row["special_use"] for row in rows}


async def get_trash_folder(
    *,
    user_id: "int | None",
    db_path: Path = DB_PATH,
) -> "str | None":
    """Return the tenant's server-declared ``\\Trash`` folder, case preserved.

    Unlike get_special_use_folders (lower-cased keys, for classification),
    this returns the exact name the server advertised, because it is used
    as a real IMAP mailbox argument (UID MOVE target) and mailbox names
    other than INBOX are case-sensitive. None when no folder carries the
    RFC 6154 \\Trash attribute (or discovery hasn't run for this tenant).
    If a server ever declares more than one, the lowest name wins so the
    choice is deterministic.
    """
    async with get_db(db_path) as db, db.execute(
        "SELECT name FROM folders "
        "WHERE user_id IS ? AND special_use = 'trash' ORDER BY name LIMIT 1",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    return row["name"] if row is not None else None


# ---------------------------------------------------------------------------
# Message sync helpers
# ---------------------------------------------------------------------------


async def upsert_message(
    folder_id: int,
    uid: int,
    *,
    seq_num: "int | None" = None,
    flags: "list[str] | None" = None,
    subject: "str | None" = None,
    sender: "str | None" = None,
    date_sent: "int | None" = None,
    size: "int | None" = None,
    message_id: "str | None" = None,
    db_path: Path = DB_PATH,
) -> None:
    """Insert or update a message row. Only updates non-None fields."""
    now = int(time.time())
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT id FROM messages WHERE folder_id = ? AND uid = ?",
            (folder_id, uid),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            # INSERT with whatever we have
            await db.execute(
                """
                INSERT INTO messages
                    (folder_id, uid, sequence_num, flags, subject, sender,
                     date_sent, size, message_id, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    folder_id,
                    uid,
                    seq_num,
                    json.dumps(flags if flags is not None else []),
                    subject,
                    sender,
                    date_sent,
                    size,
                    message_id,
                    now,
                ),
            )
        else:
            # UPDATE only non-None fields
            updates: list[tuple[str, object]] = [("last_updated", now)]
            if seq_num is not None:
                updates.append(("sequence_num", seq_num))
            if flags is not None:
                updates.append(("flags", json.dumps(flags)))
            if subject is not None:
                updates.append(("subject", subject))
            if sender is not None:
                updates.append(("sender", sender))
            if date_sent is not None:
                updates.append(("date_sent", date_sent))
            if size is not None:
                updates.append(("size", size))
            if message_id is not None:
                updates.append(("message_id", message_id))

            set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
            values: list[object] = [v for _, v in updates]
            values.append(folder_id)
            values.append(uid)
            await db.execute(
                f"UPDATE messages SET {set_clause} WHERE folder_id = ? AND uid = ?",  # noqa: S608
                values,
            )

        await db.commit()


async def get_message(
    folder_id: int,
    uid: int,
    db_path: Path = DB_PATH,
) -> "dict | None":
    """Fetch a single message row by UID. Returns dict or None."""
    async with get_db(db_path) as db, db.execute(
        "SELECT * FROM messages WHERE folder_id = ? AND uid = ?",
        (folder_id, uid),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return dict(row)


def _expand_uid_set(uid_set_str: str, known_uids: "list[int]") -> "list[int]":
    """Expand an IMAP UID set string into a list of matching UIDs.

    UID set grammar (simplified):
      uid-set = uid-range *("," uid-range)
      uid-range = uid / uid ":" uid
      uid = nz-number / "*"

    "*" means the highest known UID in the folder.
    """
    if not known_uids:
        return []

    max_uid = max(known_uids)
    known_set = set(known_uids)
    result: set[int] = set()

    for part in uid_set_str.split(","):
        part = part.strip()  # noqa: PLW2901 — intentional normalize-in-place of the loop token
        if ":" in part:
            low_str, high_str = part.split(":", 1)
            low = max_uid if low_str == "*" else int(low_str)
            high = max_uid if high_str == "*" else int(high_str)
            if low > high:
                low, high = high, low
            result.update(u for u in known_set if low <= u <= high)
        elif part == "*":
            result.add(max_uid)
        else:
            uid = int(part)
            if uid in known_set:
                result.add(uid)

    return sorted(result)


async def get_messages_by_uid_set(
    folder_id: int,
    uid_set_str: str,
    db_path: Path = DB_PATH,
) -> "list[dict]":
    """Resolve a UID set string to message rows.

    uid_set_str formats:
      "42"          — single UID
      "1:10"        — range
      "1,3,5"       — comma list
      "1:5,10,20"   — mixed
      "*"           — highest known UID in folder
      "1:*"         — all from 1 to highest known UID

    Pulls all known UIDs for the folder from DB, then filters to the set.
    Returns [] if no messages match.
    """
    async with get_db(db_path) as db, db.execute(
        "SELECT uid FROM messages WHERE folder_id = ? ORDER BY uid",
        (folder_id,),
    ) as cur:
        rows = await cur.fetchall()

    known_uids = [int(r["uid"]) for r in rows]
    target_uids = _expand_uid_set(uid_set_str, known_uids)

    if not target_uids:
        return []

    placeholders = ",".join("?" * len(target_uids))
    async with get_db(db_path) as db, db.execute(
        f"SELECT * FROM messages WHERE folder_id = ? AND uid IN ({placeholders})"  # noqa: S608
        " ORDER BY uid",
        [folder_id, *target_uids],
    ) as cur:
        rows2 = await cur.fetchall()

    return [dict(r) for r in rows2]


async def get_message_metadata_by_uid_set(
    folder_id: int,
    uid_set_str: str,
    db_path: Path = DB_PATH,
) -> "list[dict]":
    """Return lightweight sender/subject rows for a UID set.

    Used at staging time to build human-readable operation descriptions
    without fetching full message rows. Returns a list of dicts with keys
    ``uid``, ``sender``, ``subject`` for each message found in the state DB.
    Messages not yet FETCH'd through the proxy will simply be absent.
    """
    rows = await get_messages_by_uid_set(folder_id, uid_set_str, db_path=db_path)
    return [
        {"uid": r["uid"], "sender": r.get("sender"), "subject": r.get("subject")}
        for r in rows
    ]


async def get_pending_move_uids_for_folder(
    folder_name: str,
    *,
    agent_id: "str | int | None",
    db_path: Path = DB_PATH,
) -> "set[int]":
    """Return UIDs that are pending MOVE out of the given folder, for one agent.

    Used by the u2c pump to filter FETCH responses — if a UID is pending
    move from the current folder, the proxy suppresses that FETCH line so
    the agent sees a consistent view without waiting for human approval.

    ``agent_id`` scopes the query to the connected agent's own staged ops so a
    pending move in one tenant's identically-named folder can never suppress
    another tenant's FETCH lines (issue #73). Folder names are not globally
    unique once the mirror is per-user, so name alone is not a safe key.
    """
    async with get_db(db_path) as db, db.execute(
        """SELECT message_ids FROM staged_operations
               WHERE status = 'pending' AND op_type = 'move'
                 AND folder_from = ? AND agent_id IS ?""",
        (folder_name, agent_id),
    ) as cur:
        rows = await cur.fetchall()

    pending: set[int] = set()
    for row in rows:
        raw = row["message_ids"] if isinstance(row, dict) else row[0]
        if not raw:
            continue
        try:
            ids = json.loads(raw)
        except Exception:  # noqa: S112 — skip a malformed stored row; not a control-flow secret
            continue
        for uid_str in ids:
            # message_ids stores raw uid_set strings like "377" or "1:5"
            # For simple single UIDs, parse directly; ranges handled below
            s = str(uid_str).strip()
            if s.isdigit():
                pending.add(int(s))
            elif ":" in s:
                # Expand simple range N:M
                parts = s.split(":")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    lo, hi = int(parts[0]), int(parts[1])
                    pending.update(range(lo, hi + 1))
    return pending


async def get_pending_flag_changes_for_uid(
    folder_name: str,
    uid: int,
    *,
    agent_id: "str | int | None",
    db_path: Path = DB_PATH,
) -> "tuple[list[str], list[str]]":
    """Return (flags_to_add, flags_to_remove) aggregated from all pending STORE ops
    that affect the given UID in folder_name, for one agent.

    Used by the u2c pump to patch FLAGS in upstream FETCH responses so the agent
    sees the staged flag state immediately without waiting for human approval.
    Only considers ops whose op_type indicates a flag mutation (store, trash,
    mark_read, mark_unread, flag, unflag).

    ``agent_id`` scopes the query to the connected agent's own staged ops so one
    tenant's pending flag change can never bleed into another tenant's FETCH
    view of an identically-named folder (issue #73).
    """
    FLAG_OP_TYPES = ("store", "trash", "mark_read", "mark_unread", "flag", "unflag")
    placeholders = ",".join("?" * len(FLAG_OP_TYPES))
    async with get_db(db_path) as db, db.execute(
        f"SELECT message_ids, flags_add, flags_remove FROM staged_operations "  # noqa: S608
        f"WHERE status = 'pending' AND op_type IN ({placeholders}) "
        f"AND folder_from = ? AND agent_id IS ?",
        (*FLAG_OP_TYPES, folder_name, agent_id),
    ) as cur:
        rows = await cur.fetchall()

    flags_to_add: list[str] = []
    flags_to_remove: list[str] = []

    def _uid_in_set(uid_set_raw: "list[str]") -> bool:
        for entry in uid_set_raw:
            s = str(entry).strip()
            if s.isdigit() and int(s) == uid:
                return True
            if ":" in s:
                parts = s.split(":")
                if (
                    len(parts) == 2
                    and parts[0].isdigit()
                    and parts[1].isdigit()
                    and int(parts[0]) <= uid <= int(parts[1])
                ):
                    return True
        return False

    for row in rows:
        raw_ids = row["message_ids"] if isinstance(row, dict) else row[0]
        raw_add = row["flags_add"] if isinstance(row, dict) else row[1]
        raw_remove = row["flags_remove"] if isinstance(row, dict) else row[2]
        if not raw_ids:
            continue
        try:
            ids = json.loads(raw_ids)
        except Exception:  # noqa: S112 — skip a malformed stored row; not a control-flow secret
            continue
        if not _uid_in_set(ids):
            continue
        if raw_add:
            with suppress(Exception):
                flags_to_add.extend(json.loads(raw_add))
        if raw_remove:
            with suppress(Exception):
                flags_to_remove.extend(json.loads(raw_remove))

    return flags_to_add, flags_to_remove


async def remove_messages_from_folder(
    folder_id: int,
    uid_set_str: str,
    db_path: Path = DB_PATH,
) -> None:
    """Delete message rows for the given UID set from the local state mirror.

    Used when staging a MOVE operation — the message is optimistically removed
    from the source folder so the agent sees a consistent view without waiting
    for human approval. Rollback restores the row via restore_from_snapshot.
    """
    rows = await get_messages_by_uid_set(folder_id, uid_set_str, db_path=db_path)
    if not rows:
        return
    uids = [r["uid"] for r in rows]
    placeholders = ",".join("?" * len(uids))
    async with get_db(db_path) as db:
        await db.execute(
            f"DELETE FROM messages WHERE folder_id = ? AND uid IN ({placeholders})",  # noqa: S608
            [folder_id, *uids],
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Snapshot, optimistic update, and revert (milestone 1.2 — rejection revert)
# ---------------------------------------------------------------------------


async def snapshot_messages(
    folder_id: int,
    uid_set_str: str,
    db_path: Path = DB_PATH,
) -> dict:
    """Capture current state of messages matching uid_set_str.

    Returns a dict keyed by str(uid) containing the pre-operation state
    needed to revert the messages table on rejection:
      {"42": {"flags": ["\\\\Seen"], "seq_num": 5, "folder_id": 1}, ...}

    If no messages match (uid not yet in state DB — message hasn't been
    FETCH'd through the proxy yet), the snapshot will be empty for that
    uid. Revert will be a no-op for unknown UIDs.
    """
    rows = await get_messages_by_uid_set(folder_id, uid_set_str, db_path=db_path)
    snapshot: dict = {}
    for row in rows:
        uid_str = str(row["uid"])
        flags = decode_json_list(row.get("flags", "[]"))
        snapshot[uid_str] = {
            "flags": flags,
            "seq_num": row.get("sequence_num"),
            "folder_id": folder_id,
            # sender/subject captured so MOVE rollback can fully re-insert the row
            "sender": row.get("sender"),
            "subject": row.get("subject"),
        }
    return snapshot


async def apply_optimistic_flag_update(
    folder_id: int,
    uid_set_str: str,
    flags_add: "list[str]",
    flags_remove: "list[str]",
    db_path: Path = DB_PATH,
) -> None:
    """Apply a proposed flag change to the messages table optimistically.

    Called immediately after staging a STORE operation so the AI sees
    the proposed state without waiting for approval. Does not touch upstream.

    Flag merge logic:
      - flags_add:    add each flag to existing set (dedup)
      - flags_remove: remove each flag from existing set
      Both lists can be non-empty simultaneously (e.g. FLAGS.SILENT with add/remove).

    Messages not yet in the state DB (uid unknown) are silently skipped —
    the snapshot will also be empty for them, so there is nothing to revert.
    """
    rows = await get_messages_by_uid_set(folder_id, uid_set_str, db_path=db_path)
    if not rows:
        return

    now = int(time.time())
    async with get_db(db_path) as db:
        for row in rows:
            uid = row["uid"]
            current = set(decode_json_list(row.get("flags", "[]")))
            current.update(flags_add)
            current.difference_update(flags_remove)
            new_flags = json.dumps(sorted(current))
            await db.execute(
                "UPDATE messages SET flags = ?, last_updated = ? WHERE folder_id = ? AND uid = ?",
                (new_flags, now, folder_id, uid),
            )
        await db.commit()


async def restore_from_snapshot(
    operation_id: str,
    db_path: Path = DB_PATH,
) -> "list[dict]":
    """Read snapshot from staged_operations, restore messages table.

    For each UID in the snapshot:
      - Restores the flags column in the messages table to the pre-op value.
      - Returns a list of {operation_id, folder_id, uid, true_flags} dicts
        for insertion into pending_reverts.

    If snapshot is NULL or empty (operation had no snapshot — e.g. CREATE,
    RENAME, or APPEND), returns an empty list (no-op revert).
    """
    async with get_db(db_path) as db, db.execute(
        "SELECT snapshot FROM staged_operations WHERE id = ?",
        (operation_id,),
    ) as cur:
        row = await cur.fetchone()

    if row is None or row["snapshot"] is None:
        return []

    snapshot: dict = json.loads(row["snapshot"])
    if not snapshot:
        return []

    now = int(time.time())
    reverts: list[dict] = []

    async with get_db(db_path) as db:
        for uid_str, state in snapshot.items():
            uid = int(uid_str)
            folder_id: int = state["folder_id"]
            true_flags: list = state.get("flags", [])
            true_flags_json = json.dumps(true_flags)

            # Check if the row still exists (STORE revert) or was deleted (MOVE revert).
            async with db.execute(
                "SELECT id FROM messages WHERE folder_id = ? AND uid = ?",
                (folder_id, uid),
            ) as chk:
                exists = await chk.fetchone()

            if exists:
                # STORE revert: row exists, just restore flags
                await db.execute(
                    "UPDATE messages SET flags = ?, last_updated = ? WHERE folder_id = ? AND uid = ?",
                    (true_flags_json, now, folder_id, uid),
                )
            else:
                # MOVE revert: row was deleted at staging time — re-insert it
                seq_num = state.get("seq_num")
                sender = state.get("sender")
                subject = state.get("subject")
                await db.execute(
                    """INSERT OR IGNORE INTO messages
                       (folder_id, uid, sequence_num, flags, sender, subject, last_updated)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (folder_id, uid, seq_num, true_flags_json, sender, subject, now),
                )
            reverts.append({
                "operation_id": operation_id,
                "folder_id": folder_id,
                "uid": uid,
                "true_flags": true_flags_json,
            })
        await db.commit()

    return reverts


async def insert_pending_reverts(
    operation_id: str,
    reverts: "list[dict]",
    db_path: Path = DB_PATH,
) -> None:
    """Insert pending_reverts rows (one per affected UID).

    Each row will be picked up by the proxy on the AI's next SELECT/NOOP/FETCH
    and injected as an unsolicited FETCH response so the AI re-syncs to true state.

    reverts: list of {operation_id, folder_id, uid, true_flags} dicts as
    returned by restore_from_snapshot().
    """
    if not reverts:
        return

    now = int(time.time())
    async with get_db(db_path) as db:
        for r in reverts:
            await db.execute(
                """
                INSERT INTO pending_reverts
                    (operation_id, folder_id, uid, true_flags, created_at, delivered_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (r["operation_id"], r["folder_id"], r["uid"], r["true_flags"], now),
            )
        await db.commit()


async def get_pending_reverts(
    folder_id: int,
    db_path: Path = DB_PATH,
) -> "list[dict]":
    """Return undelivered pending_reverts rows for a folder.

    Only returns rows where delivered_at IS NULL.
    Ordered by creation time so the proxy injects them in staging order.
    """
    async with get_db(db_path) as db, db.execute(
        """
            SELECT id, operation_id, folder_id, uid, true_flags, created_at, delivered_at
            FROM pending_reverts
            WHERE folder_id = ? AND delivered_at IS NULL
            ORDER BY created_at ASC
            """,
        (folder_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_reverts_delivered(
    revert_ids: "list[int]",
    db_path: Path = DB_PATH,
) -> None:
    """Set delivered_at = now for the given pending_reverts ids."""
    if not revert_ids:
        return

    now = int(time.time())
    placeholders = ",".join("?" * len(revert_ids))
    async with get_db(db_path) as db:
        await db.execute(
            f"UPDATE pending_reverts SET delivered_at = ? WHERE id IN ({placeholders})",  # noqa: S608
            [now, *revert_ids],
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Invite codes (NUVRAIL_SIGNUP_MODE=invite)
#
# Single-use contract: redeem_invite_code() spends a code in ONE atomic
# UPDATE...WHERE redeemed_at IS NULL. SQLite serializes writes, so two
# concurrent registrations racing on the same code cannot both see rowcount==1
# — exactly one wins, the other gets 0 and is rejected. This is why redemption
# is a conditional UPDATE and not SELECT-then-UPDATE.
# ---------------------------------------------------------------------------


async def insert_invite_code(
    code_hash: str,
    expires_at: "int | None" = None,
    note: "str | None" = None,
    db_path: Path = DB_PATH,
) -> int:
    """Store a new invite code (hash only) and return its row id."""
    now = int(time.time())
    async with get_db(db_path) as db:
        cur = await db.execute(
            "INSERT INTO invite_codes (code_hash, created_at, expires_at, note) "
            "VALUES (?, ?, ?, ?)",
            (code_hash, now, expires_at, note),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def redeem_invite_code(
    code_hash: str,
    redeemed_by_email: str,
    db_path: Path = DB_PATH,
) -> bool:
    """Atomically spend a single-use invite code.

    Returns True iff a currently-valid code (unspent, unrevoked, unexpired) was
    consumed by this call. The validity check and the spend happen in a single
    conditional UPDATE so concurrent redemptions cannot double-spend one code.
    """
    now = int(time.time())
    async with get_db(db_path) as db:
        cur = await db.execute(
            "UPDATE invite_codes "
            "SET redeemed_at = ?, redeemed_by_email = ? "
            "WHERE code_hash = ? "
            "  AND redeemed_at IS NULL "
            "  AND revoked_at IS NULL "
            "  AND (expires_at IS NULL OR expires_at > ?)",
            (now, redeemed_by_email, code_hash, now),
        )
        await db.commit()
        return cur.rowcount == 1


async def revoke_invite_code(code_hash: str, db_path: Path = DB_PATH) -> bool:
    """Revoke an unspent invite code. Returns True iff a code was revoked.

    A code that is already redeemed or already revoked is left untouched
    (rowcount 0 -> False): redemption is terminal, and re-revoking is a no-op.
    """
    now = int(time.time())
    async with get_db(db_path) as db:
        cur = await db.execute(
            "UPDATE invite_codes SET revoked_at = ? "
            "WHERE code_hash = ? AND redeemed_at IS NULL AND revoked_at IS NULL",
            (now, code_hash),
        )
        await db.commit()
        return cur.rowcount == 1


async def list_invite_codes(db_path: Path = DB_PATH) -> "list[dict]":
    """Return all invite codes (hashes, not raw codes) with their status."""
    async with get_db(db_path) as db, db.execute(
        "SELECT id, code_hash, created_at, expires_at, note, "
        "redeemed_at, redeemed_by_email, revoked_at "
        "FROM invite_codes ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]

