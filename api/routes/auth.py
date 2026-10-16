"""
Auth + agent credential endpoints - Milestone auth.

POST /api/v1/auth/register  - create a user account
POST /api/v1/auth/login     - exchange email/password for a bearer token
GET  /api/v1/auth/me        - return current user info (requires Bearer)

POST   /api/v1/agents       - register upstream email account, get agent credentials (shown once)
GET    /api/v1/agents       - list agent credentials for current user (no token field)
DELETE /api/v1/agents/{id}  - revoke an agent credential

Lane 3: human → REST API (bearer token)
Lane 2: AI agent → proxy (agent_username + agent_token as IMAP/SMTP password)
"""
from __future__ import annotations

import asyncio
import contextlib
import imaplib
import logging
import os
import secrets
import socket
import ssl
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from api.auth import (
    generate_token,
    get_current_user,
    hash_agent_token,
    hash_password,
    hash_token_for_storage,
    verify_password,
)
from api.limiter import limiter
from api.models import (
    AgentCreateRequest,
    AgentCreateResponse,
    AgentResponse,
    ChangePasswordRequest,
    ChangePasswordResponse,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    ResetPasswordBody,
    ResetPasswordResponse,
    ResetRequestBody,
    ResetRequestResponse,
    UserCreateRequest,
    UserResponse,
)
from api.routes.operations import get_db_path
from api.signup import current_signup_mode
from gateway.credentials import delete_credential, purge_agent_upstream_secrets, store_credential
from gateway.entitlements import entitlements
from gateway.security_controls import build_auth_abuse_protector
from gateway.state_db import get_db

logger = logging.getLogger(__name__)

router = APIRouter()
LOGIN_ABUSE_PROTECTOR = build_auth_abuse_protector("api_login")


class ImapValidationError(Exception):
    """Raised when upstream IMAP credential validation fails."""

    def __init__(self, error: str, detail: str):
        super().__init__(detail)
        self.error = error
        self.detail = detail


def _build_auth_failure_detail(upstream_host: str) -> str:
    host = upstream_host.lower()
    if any(k in host for k in ("gmail.com", "icloud.com", "me.com", "mac.com")):
        return (
            "IMAP authentication failed. Wrong username or password. "
            "For Gmail/iCloud, use an app-specific password."
        )
    return "IMAP authentication failed. Wrong username or password."


def _verify_imap_connection_sync(
    upstream_host: str,
    upstream_imap_port: int,
    upstream_user: str,
    upstream_password: str,
) -> None:
    client: imaplib.IMAP4_SSL | None = None
    try:
        # Verify the upstream certificate and hostname. Without an explicit
        # context, stdlib IMAP4_SSL skips both, and this probe sends the
        # user's real mailbox password. create_default_context() honours
        # SSL_CERT_FILE / SSL_CERT_DIR, so self-hosters with a private CA
        # point those at their CA bundle instead of turning verification off.
        client = imaplib.IMAP4_SSL(
            upstream_host,
            upstream_imap_port,
            ssl_context=ssl.create_default_context(),
            timeout=10,
        )
        client.login(upstream_user, upstream_password)
        status_, _ = client.select("INBOX")
        if status_.upper() != "OK":
            raise imaplib.IMAP4.error("Failed to select INBOX folder")
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.logout()


async def _verify_imap_connection(
    upstream_host: str,
    upstream_imap_port: int,
    upstream_user: str,
    upstream_password: str,
) -> None:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                _verify_imap_connection_sync,
                upstream_host,
                upstream_imap_port,
                upstream_user,
                upstream_password,
            ),
            timeout=10,
        )
    except TimeoutError as exc:
        raise ImapValidationError(
            "imap_timeout", "Timed out while verifying IMAP connection (10s limit)."
        ) from exc
    except ssl.SSLCertVerificationError as exc:
        # Must precede ssl.SSLError (its parent). A bad certificate is not a
        # wrong password and not a wrong port; say what actually failed.
        raise ImapValidationError(
            "imap_tls_verify_failed",
            f"TLS certificate verification failed for {upstream_host}. "
            "If this server uses a private CA, set SSL_CERT_FILE to its CA bundle.",
        ) from exc
    except ssl.SSLError as exc:
        raise ImapValidationError(
            "imap_ssl_error", "IMAP SSL/TLS handshake failed. Check host and IMAP SSL port."
        ) from exc
    except imaplib.IMAP4.abort as exc:
        raise ImapValidationError(
            "imap_connection_failed", "Unable to reach IMAP server. Check host and port."
        ) from exc
    except imaplib.IMAP4.error as exc:
        raise ImapValidationError("imap_auth_failed", _build_auth_failure_detail(upstream_host)) from exc
    except (socket.gaierror, ConnectionRefusedError, OSError) as exc:
        raise ImapValidationError(
            "imap_connection_failed", "Unable to reach IMAP server. Check host and port."
        ) from exc


# ---------------------------------------------------------------------------
# Account endpoints
# ---------------------------------------------------------------------------


@router.post("/auth/register", response_model=UserResponse, status_code=201)
async def register(
    body: UserCreateRequest,
    db_path: Path = Depends(get_db_path),
) -> UserResponse:
    """Create a new user account.

    Gated by the deployment's signup mode (NUVRAIL_SIGNUP_MODE):
      - closed  -> 403; accounts are provisioned only via scripts/manage_users.py
      - invite  -> requires a valid single-use invite_code (redeemed atomically
                   BEFORE the account is created; a bad/expired/spent/revoked
                   code -> 403 and no account)
      - open    -> allowed (deployment already acknowledged open signup at boot)

    Returns 409 if the email is already registered.

    Ordering note: the invite code is redeemed *before* the user INSERT so a
    caller cannot burn a valid code on a duplicate-email attempt — we check the
    email-exists case first, then spend the code, then insert. The redeem is a
    single atomic UPDATE, so it is also safe against concurrent double-spend.
    """
    mode = current_signup_mode()
    if mode == "closed":
        # Do not reveal whether registration would otherwise succeed; a private
        # instance simply has no public signup.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public registration is disabled on this deployment.",
        )

    now = int(time.time())
    hashed = hash_password(body.password)
    # Generate an API token immediately on registration so the client can
    # auto-login without a second round-trip.
    api_token = generate_token()

    async with get_db(db_path) as db:
        # Invite mode: the email-uniqueness check runs first (below) so a
        # duplicate email 409s without consuming a code; the code is then spent
        # atomically just before INSERT.
        if mode == "invite":
            raw_code = (body.invite_code or "").strip()
            if not raw_code:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="An invite code is required to register on this deployment.",
                )
        # Check for existing email
        async with db.execute(
            "SELECT id FROM users WHERE email = ?", (body.email,)
        ) as cur:
            existing = await cur.fetchone()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Email {body.email!r} is already registered",
            )

        # Invite mode: spend the code on THIS connection, in the same
        # transaction as the user INSERT, so the code and the account commit (or
        # fail) together — no spending a code without creating an account, and
        # no creating an account without spending a code. The conditional UPDATE
        # is the single-use gate: rowcount 1 = valid & now spent, 0 = reject.
        if mode == "invite":
            code_hash = hash_token_for_storage(raw_code)
            redeem_cur = await db.execute(
                "UPDATE invite_codes "
                "SET redeemed_at = ?, redeemed_by_email = ? "
                "WHERE code_hash = ? "
                "  AND redeemed_at IS NULL "
                "  AND revoked_at IS NULL "
                "  AND (expires_at IS NULL OR expires_at > ?)",
                (now, body.email, code_hash, now),
            )
            if redeem_cur.rowcount != 1:
                # Invalid / expired / already-used / revoked — uniform 403, no
                # detail about which, so the endpoint can't be used to probe
                # which codes exist.
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid or already-used invite code.",
                )

        cur2 = await db.execute(
            """
            INSERT INTO users (email, display_name, hashed_password, api_token,
                               api_token_created_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (body.email, body.display_name, hashed, hash_token_for_storage(api_token), now, now),
        )
        await db.commit()
        user_id = cur2.lastrowid

    return UserResponse(
        user_id=user_id,  # type: ignore[arg-type]
        email=body.email,
        display_name=body.display_name,
        created_at=now,
        token=api_token,  # plaintext - shown once, stored as hash
    )


@router.post("/auth/login", response_model=LoginResponse)
@limiter.limit("10/minute")
async def login(
    body: LoginRequest,
    request: Request,
    db_path: Path = Depends(get_db_path),
) -> LoginResponse:
    """Exchange email + password for a long-lived bearer token.

    Returns 401 on bad credentials (deliberately vague).
    """
    client_host = request.client.host if request.client and request.client.host else "unknown"
    decision = await LOGIN_ABUSE_PROTECTOR.start_attempt(ip=client_host, account=body.email)
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "Too many login attempts",
                "reason": decision.reason,
                "retry_after_seconds": decision.retry_after_seconds,
            },
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    async with get_db(db_path) as db, db.execute(
        "SELECT * FROM users WHERE email = ?", (body.email,)
    ) as cur:
        row = await cur.fetchone()

    if row is None or not verify_password(body.password, row["hashed_password"]):
        failure = await LOGIN_ABUSE_PROTECTOR.record_failure(ip=client_host, account=body.email)
        if failure.lockout_applied:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "Too many login attempts",
                    "reason": "temporary_lockout",
                    "retry_after_seconds": failure.retry_after_seconds,
                },
                headers={"Retry-After": str(failure.retry_after_seconds)},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await LOGIN_ABUSE_PROTECTOR.record_success(ip=client_host, account=body.email)
    user = dict(row)

    # Generate a fresh plaintext token for this session; store only its SHA-256
    # hash so the DB never holds a recoverable secret.  Each login invalidates
    # the previous token (only the latest hash is stored).
    fresh_token = generate_token()
    now_login = int(time.time())
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE users SET api_token = ?, api_token_created_at = ? WHERE id = ?",
            (hash_token_for_storage(fresh_token), now_login, user["id"]),
        )
        await db.commit()

    return LoginResponse(
        token=fresh_token,  # plaintext - shown once, not stored
        token_type="bearer",  # noqa: S106 — OAuth2 response field, not a secret
        user_id=user["id"],
        email=user["email"],
    )


@router.get("/auth/me", response_model=UserResponse)
async def me(
    current_user: dict = Depends(get_current_user),
) -> UserResponse:
    """Return the current user's profile."""
    return UserResponse(
        user_id=current_user["id"],
        email=current_user["email"],
        display_name=current_user.get("display_name"),
        created_at=current_user["created_at"],
    )


# ---------------------------------------------------------------------------
# Agent credential endpoints
# ---------------------------------------------------------------------------


@router.post("/agents", response_model=AgentCreateResponse, status_code=201)
@limiter.limit("5/minute")
async def create_agent(
    body: AgentCreateRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db_path: Path = Depends(get_db_path),
) -> AgentCreateResponse | JSONResponse:
    """Register upstream email credentials and generate an agent token.

    Supports two auth modes (mutually exclusive):
      - Password auth: provide upstream_password.
      - OAuth2/XOAUTH2: provide oauth2_provider, oauth2_client_id,
        oauth2_client_secret, and oauth2_refresh_token.

    The plaintext agent_token is returned ONCE in this response.
    It is NEVER stored and NEVER returned again - the caller must save it.
    """
    # --- Validate auth mode -------------------------------------------------
    using_oauth2 = bool(body.oauth2_provider)
    using_password = bool(body.upstream_password)

    if using_oauth2 and using_password:
        return JSONResponse(
            status_code=422,
            content={
                "error": "conflicting_auth",
                "detail": "Provide either upstream_password or oauth2_* fields, not both.",
            },
        )
    if not using_oauth2 and not using_password:
        return JSONResponse(
            status_code=422,
            content={
                "error": "missing_auth",
                "detail": "Provide either upstream_password or oauth2_provider + oauth2_client_id + oauth2_client_secret + oauth2_refresh_token.",
            },
        )
    if using_oauth2:
        missing = [
            f for f in ("oauth2_client_id", "oauth2_client_secret", "oauth2_refresh_token")
            if not getattr(body, f)
        ]
        if missing:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "incomplete_oauth2",
                    "detail": f"oauth2_provider set but missing required fields: {', '.join(missing)}",
                },
            )
        supported_providers = ("google",)
        if body.oauth2_provider not in supported_providers:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "unsupported_oauth2_provider",
                    "detail": f"Supported providers: {', '.join(supported_providers)}",
                },
            )

    # --- Enforce per-plan agent quota (no-op in open core) ------------------
    # The limit (if any) lives in the entitlements provider, not here, so the
    # public build never caps agents. The enterprise provider raises 402 when a
    # plan's agent limit is reached. Checked before the upstream IMAP probe so a
    # capped request fails fast.
    async with get_db(db_path) as db, db.execute(
        "SELECT COUNT(*) AS c FROM agent_credentials "
        "WHERE user_id = ? AND revoked_at IS NULL",
        (current_user["id"],),
    ) as cur:
        agent_count = (await cur.fetchone())["c"]
    await entitlements().assert_can_create_agent(current_user, agent_count)

    # --- Validate IMAP connectivity (password path only) --------------------
    if using_password:
        try:
            await _verify_imap_connection(
                body.upstream_host,
                body.upstream_imap_port,
                body.upstream_user,
                body.upstream_password,  # type: ignore[arg-type]
            )
        except ImapValidationError as exc:
            return JSONResponse(status_code=422, content={"error": exc.error, "detail": exc.detail})
    # OAuth2 path: connection validation deferred to first proxy use (issue #46).

    # --- Persist credentials ------------------------------------------------
    now = int(time.time())
    agent_username = "nuvrail_" + secrets.token_hex(8)
    agent_token_plain = generate_token()
    hashed = hash_agent_token(agent_token_plain)
    label = body.label or "default"

    # Persist secrets to the configured backend BEFORE opening the DB, so we
    # never hold the SQLite connection open across remote secret-manager calls.
    uid = current_user["id"]
    stored_password = (
        await store_credential(body.upstream_password, field="upstream_password", owner_user_id=uid)
        if body.upstream_password else None
    )
    stored_client_secret = (
        await store_credential(body.oauth2_client_secret, field="oauth2_client_secret", owner_user_id=uid)
        if body.oauth2_client_secret else None
    )
    stored_refresh_token = (
        await store_credential(body.oauth2_refresh_token, field="oauth2_refresh_token", owner_user_id=uid)
        if body.oauth2_refresh_token else None
    )
    created_refs = [r for r in (stored_password, stored_client_secret, stored_refresh_token) if r]

    try:
        async with get_db(db_path) as db:
            cur = await db.execute(
                """
                INSERT INTO agent_credentials
                    (user_id, label, agent_username, hashed_token,
                     upstream_host, upstream_smtp_host,
                     upstream_imap_port, upstream_smtp_port,
                     upstream_user, upstream_password,
                     oauth2_provider, oauth2_client_id,
                     oauth2_client_secret, oauth2_refresh_token,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    label,
                    agent_username,
                    hashed,
                    body.upstream_host,
                    body.upstream_smtp_host,  # NULL if not provided; proxy falls back to upstream_host
                    body.upstream_imap_port,
                    body.upstream_smtp_port,
                    body.upstream_user,
                    stored_password,
                    body.oauth2_provider,
                    body.oauth2_client_id,
                    stored_client_secret,
                    stored_refresh_token,
                    now,
                ),
            )
            await db.commit()
            cred_id = cur.lastrowid
    except Exception:
        # DB write failed after secrets were created externally — clean up the
        # orphans so they don't linger in the secret manager.
        for ref in created_refs:
            try:
                await delete_credential(ref)
            except Exception:
                logger.warning("[agents] Failed to clean up orphaned secret after insert error")
        raise

    return AgentCreateResponse(
        id=cred_id,  # type: ignore[arg-type]
        agent_username=agent_username,
        agent_token=agent_token_plain,  # SHOWN ONCE - never stored as plaintext
        label=label,
        upstream_host=body.upstream_host,
        upstream_user=body.upstream_user,
    )


@router.get("/agents", response_model=list[AgentResponse])
async def list_agents(
    current_user: dict = Depends(get_current_user),
    db_path: Path = Depends(get_db_path),
) -> list[AgentResponse]:
    """List agent credentials for the current user. Token is never returned."""
    async with get_db(db_path) as db, db.execute(
        """
            SELECT ac.id, ac.agent_username, ac.label,
                   ac.upstream_host, ac.upstream_user,
                   ac.created_at, ac.revoked_at,
                   (
                       SELECT MAX(so.created_at)
                       FROM staged_operations so
                       WHERE so.agent_id = ac.id
                   ) AS last_activity_at
            FROM agent_credentials ac
            WHERE ac.user_id = ?
            ORDER BY ac.created_at ASC
            """,
        (current_user["id"],),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    return [AgentResponse(**r) for r in rows]


@router.delete("/agents/{agent_id}", status_code=204)
async def revoke_agent(
    agent_id: int,
    current_user: dict = Depends(get_current_user),
    db_path: Path = Depends(get_db_path),
) -> None:
    """Revoke (disconnect) an agent credential.

    As promised in the Privacy Policy (§6 — credentials are removed when you
    disconnect an agent), this revokes any OAuth grant at the provider and
    deletes the agent's upstream secrets from the external secret store before
    nulling the secret columns. The row itself is retained (with ``revoked_at``
    set) for audit history; the proxy rejects the credential on its next auth
    attempt. There is no un-revoke flow, so retaining the secret would serve no
    purpose and would contradict the policy.
    """
    now = int(time.time())
    async with get_db(db_path) as db:
        async with db.execute(
            """
            SELECT id, user_id, oauth2_provider, oauth2_refresh_token,
                   oauth2_access_token, oauth2_client_secret, upstream_password
            FROM agent_credentials WHERE id = ?
            """,
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent credential {agent_id} not found",
            )
        if row["user_id"] != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not your credential",
            )

    # Best-effort provider revocation + external-secret deletion, BEFORE nulling
    # the reference columns (otherwise the secret is orphaned in the store —
    # same bug class as #72, here for the per-agent disconnect path). Done
    # outside the DB transaction so network / secret-store I/O holds no lock.
    await purge_agent_upstream_secrets(dict(row))

    async with get_db(db_path) as db:
        await db.execute(
            """
            UPDATE agent_credentials
            SET revoked_at = ?,
                upstream_password = NULL,
                oauth2_refresh_token = NULL,
                oauth2_access_token = NULL,
                oauth2_client_secret = NULL
            WHERE id = ?
            """,
            (now, agent_id),
        )
        await db.commit()



# ---------------------------------------------------------------------------
# Account maintenance endpoints (issue #16)
# ---------------------------------------------------------------------------

_RESET_TOKEN_TTL_SECONDS = 3600  # 1 hour


@router.put("/auth/password", response_model=ChangePasswordResponse)
async def change_password(
    body: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
    db_path: Path = Depends(get_db_path),
) -> ChangePasswordResponse:
    """Change the current user's password.

    Verifies current_password with bcrypt before accepting new_password.
    Returns 400 if current_password is wrong, 422 if new_password is too weak.
    """
    if len(body.new_password) < 12:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="New password must be at least 12 characters.",
        )
    if not verify_password(body.current_password, current_user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )

    new_hash = hash_password(body.new_password)
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE users SET hashed_password = ? WHERE id = ?",
            (new_hash, current_user["id"]),
        )
        await db.commit()

    return ChangePasswordResponse(ok=True)


@router.post("/auth/reset-request", response_model=ResetRequestResponse)
async def reset_request(
    body: ResetRequestBody,
    db_path: Path = Depends(get_db_path),
) -> ResetRequestResponse:
    """Initiate a password reset.

    Generates a short-lived signed reset token, stores its SHA-256 hash in
    the DB, and returns 200 regardless of whether the email is registered
    (no account enumeration).

    Token delivery: the plaintext reset token is NEVER written to the server
    log by default — logs are frequently shipped to third parties and a logged
    token is a full account-takeover primitive for anyone with log access
    (valid for the 1h TTL). Only the SHA-256 hash is persisted.

    For local development (no email delivery yet), set NUVRAIL_RESET_TOKEN_LOG=1
    to opt in to logging the full reset URL. This must never be enabled in
    production. Email delivery (Phase 2b) will replace the log entirely.
    """
    import hashlib as _hashlib
    import logging as _logging
    _log = _logging.getLogger(__name__)

    async with get_db(db_path) as db, db.execute(
        "SELECT id FROM users WHERE email = ?", (body.email,)
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        # Return 200 - no account enumeration
        return ResetRequestResponse(ok=True)

    user_id = row["id"]
    token_plain = generate_token()
    token_hash = _hashlib.sha256(token_plain.encode()).hexdigest()
    expires_at = int(time.time()) + _RESET_TOKEN_TTL_SECONDS

    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE users SET reset_token = ?, reset_token_expires_at = ? WHERE id = ?",
            (token_hash, expires_at, user_id),
        )
        await db.commit()

    if os.environ.get("NUVRAIL_RESET_TOKEN_LOG", "").strip().lower() in ("1", "true", "yes"):
        # Dev-only opt-in: surface the full reset URL for local testing.
        host = os.environ.get("NUVRAIL_HOST_URL", "http://localhost:8080")
        reset_url = f"{host}/#/reset?token={token_plain}"
        _log.warning(
            "[reset-request] NUVRAIL_RESET_TOKEN_LOG is enabled — logging reset "
            "URL for user %s: %s (do NOT enable in production)",
            body.email, reset_url,
        )
    else:
        # Production default: record that a reset was requested without the token.
        _log.info("[reset-request] Password reset requested for user %s", body.email)

    return ResetRequestResponse(ok=True)


@router.post("/auth/reset", response_model=ResetPasswordResponse)
async def reset_password(
    body: ResetPasswordBody,
    db_path: Path = Depends(get_db_path),
) -> ResetPasswordResponse:
    """Complete a password reset using a token from reset-request.

    Token must be present, unexpired, and not yet used. Invalidates the
    token on success. Returns 400 for invalid/expired tokens and 422 for
    weak passwords.
    """
    import hashlib as _hashlib

    if len(body.new_password) < 12:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="New password must be at least 12 characters.",
        )

    token_hash = _hashlib.sha256(body.token.encode()).hexdigest()
    now = int(time.time())

    async with get_db(db_path) as db:
        async with db.execute(
            """
            SELECT id FROM users
            WHERE reset_token = ?
              AND reset_token_expires_at IS NOT NULL
              AND reset_token_expires_at > ?
            """,
            (token_hash, now),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reset token is invalid or has expired.",
            )

        new_hash = hash_password(body.new_password)
        await db.execute(
            """
            UPDATE users
            SET hashed_password = ?,
                reset_token = NULL,
                reset_token_expires_at = NULL
            WHERE id = ?
            """,
            (new_hash, row["id"]),
        )
        await db.commit()

    return ResetPasswordResponse(ok=True)


@router.post("/auth/logout", response_model=LogoutResponse)
async def logout(
    current_user: dict = Depends(get_current_user),
    db_path: Path = Depends(get_db_path),
) -> LogoutResponse:
    """Revoke the current bearer token server-side.

    After this call, the token stored in the DB is nulled - future requests
    with the same token will receive 401. The client should also clear its
    local token storage.
    """
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE users SET api_token = NULL WHERE id = ?",
            (current_user["id"],),
        )
        await db.commit()
    return LogoutResponse(ok=True)
