"""
Nuvrail SMTP Proxy — Milestone 0.5: Staging stub.

Data flow:

  Client (plain TCP)          smtp_proxy.py           Upstream (STARTTLS)
  ──────────────────          ─────────────           ───────────────────
       connect       ──────────────────────────►      connect + STARTTLS
                     ◄──────────────────────────      220 greeting
       ◄── greeting

       EHLO          ──────────────────────────►
                     ◄──────────────────────────      250 capabilities
       ◄── 250 (STARTTLS stripped)

       AUTH          ──────────────────────────►      (log user, redact creds)
                     ◄──────────────────────────      235 OK
       ◄── 235

       MAIL FROM     ──────────────────────────►      (track sender)
       RCPT TO       ──────────────────────────►      (track recipients)
       DATA          ──────────────────────────►
                     ◄──────────────────────────      354 go ahead
       message body  →  (consumed, NOT forwarded)
       .             →  (end of body — NOT forwarded)
       ◄── 250 OK [STAGED] Send queued for approval — ID: pending

       QUIT          ──────────────────────────►
       ◄── 221

Sub-milestone 0.5: staging stub only. Full relay lands in milestone 1.0.
TODO 0.3: translate AUTH to XOAUTH2 for OAuth2 providers.

Architectural note — single coroutine vs. two-task design
──────────────────────────────────────────────────────────
The IMAP proxy uses two concurrent asyncio tasks (one for client→upstream,
one for upstream→client) because IMAP is full-duplex: the server may send
unsolicited push responses (EXISTS, EXPUNGE, FETCH) at any time, independent
of client commands. The two directions can be pumped independently with no
synchronisation required.

SMTP is strictly request/response. Every command the client sends elicits
exactly one response from the server before the next command is valid. More
critically, DATA interception requires tight synchronisation across BOTH
directions:

  1. Forward DATA → upstream
  2. Read upstream's 354 (don't forward to client yet)
  3. Drain the client body until the terminating "." line
  4. Return 250 OK [STAGED] to client

Doing this with two independent tasks would require a shared state machine,
condition variables, and careful locking — far more complex than necessary.
A single coroutine that reads one client line at a time and drives upstream
responses inline is both simpler and correct for the SMTP protocol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl

from dotenv import load_dotenv

from gateway.agent_auth import decode_sasl_plain, verify_agent_login
from gateway.credentials import fetch_credential
from gateway.extensions import load_plugins
from gateway.intent import derive_send_intent, extract_message_ids, strip_subject_prefixes
from gateway.mime_render import render_body
from gateway.security_controls import build_auth_abuse_protector
from gateway.staging import create_operation
from gateway.state_db import (
    find_message_by_message_id,
    get_db,
    warn_if_loopback_bind_in_container,
)
from logging_config import redact_protocol_line

load_dotenv()

logger = logging.getLogger(__name__)
SMTP_AUTH_ABUSE_PROTECTOR = build_auth_abuse_protector("smtp_proxy_auth")

# Regex helpers for envelope extraction
_MAIL_FROM_RE = re.compile(r"MAIL FROM:\s*<([^>]*)>", re.IGNORECASE)
_RCPT_TO_RE = re.compile(r"RCPT TO:\s*<([^>]*)>", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Low-level SMTP response helpers
# ---------------------------------------------------------------------------


# Post-auth verbs forwarded upstream unchanged. MAIL, RCPT, DATA, QUIT, AUTH,
# EHLO and HELO have their own branches; every other verb is refused (502).
# Adding one here is a security decision: it must not be able to submit,
# relay, or change the transport of a message.
_SMTP_PASSTHROUGH_COMMANDS = frozenset({"NOOP", "RSET", "HELP", "VRFY"})


async def _read_smtp_response(reader: asyncio.StreamReader) -> list[bytes]:
    """Read a complete (possibly multi-line) SMTP response.

    Multi-line responses use the format:
        CODE-text\\r\\n   ← continuation line
        CODE text\\r\\n   ← final line (space instead of dash)
    Returns all lines including the final one.
    """
    lines: list[bytes] = []
    while True:
        line = await reader.readline()
        if not line:
            break
        lines.append(line)
        # Final line has a space (or no char) at position 3; continuation has '-'
        if len(line) < 4 or line[3:4] != b"-":
            break
    return lines


def _strip_starttls(lines: list[bytes]) -> list[bytes]:
    """Remove STARTTLS capability from EHLO response and fix continuation markers.

    When STARTTLS is the last capability listed, the line before it becomes the
    new last line and must use a space (not a dash) at position 3.
    """
    filtered = [ln for ln in lines if b"STARTTLS" not in ln.upper()]
    if not filtered:
        return lines  # safety fallback

    # Ensure the final line uses a space at position 3 (not a dash)
    last = filtered[-1]
    if len(last) >= 4 and last[3:4] == b"-":
        filtered[-1] = last[:3] + b" " + last[4:]
    return filtered


# Headers extracted from an outgoing message for staging/intent purposes.
_SEND_HEADERS = ("subject", "in-reply-to", "references")


def _extract_send_headers(body_lines: list[bytes]) -> dict[str, str]:
    """Extract Subject / In-Reply-To / References from outgoing message lines.

    Scans only the header block (stops at the first blank line) and unfolds
    continuation lines (leading whitespace) so multi-line References headers
    come back whole. Keys are lower-cased; absent headers are absent keys.
    """
    headers: dict[str, str] = {}
    current_key: str | None = None
    for raw in body_lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line.strip():
            break  # end of headers
        if line[:1] in (" ", "\t"):
            if current_key is not None:
                headers[current_key] += " " + line.strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            current_key = None
            continue
        key_lower = key.strip().lower()
        if key_lower in _SEND_HEADERS and key_lower not in headers:
            headers[key_lower] = value.strip()
            current_key = key_lower
        else:
            current_key = None
    return headers


def _extract_subject(body_lines: list[bytes]) -> str:
    """Extract the Subject header value from a list of message body lines."""
    return _extract_send_headers(body_lines).get("subject") or "<no subject>"


def _build_send_description(
    intent_label: str | None,
    subject: str,
    recipients: list[str],
    original: dict | None,
) -> str:
    """Human description for a staged send, intent-aware.

    Reply with the original found in the mirror:
      'Reply to "Invoice #1234" from billing@acme.com'
    Reply known only from the outgoing headers:
      'Reply to "Invoice #1234"'  (subject with Re:/Fwd: prefixes stripped)
    Forward:
      'Forward "Invoice #1234" to bob@example.com'
    Ordinary send (unchanged from before intent existed):
      'Send email to bob@example.com — Subject: "Hello"'
    """
    to_list = ", ".join(recipients or ["<unknown>"])
    base_subject = strip_subject_prefixes(subject)

    if intent_label == "reply":
        orig_subject = (original or {}).get("subject")
        orig_sender = (original or {}).get("sender")
        parts: list[str] = []
        if orig_subject:
            parts.append(f'"{orig_subject}"')
        elif base_subject:
            parts.append(f'"{base_subject}"')
        if orig_sender:
            parts.append(f"from {orig_sender}")
        if parts:
            return f"Reply to {' '.join(parts)}"
        return f"Reply to {to_list}"

    if intent_label == "forward":
        quoted = f'"{base_subject}"' if base_subject else f'"{subject}"'
        return f"Forward {quoted} to {to_list}"

    return f'Send email to {to_list} — Subject: "{subject}"'


# ---------------------------------------------------------------------------
# Upstream STARTTLS setup
# ---------------------------------------------------------------------------


async def _connect_upstream_starttls(
    host: str,
    port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    """Open a plain TCP connection to upstream, negotiate STARTTLS, and return
    the upgraded (TLS) reader/writer pair plus the raw 220 greeting bytes.

    Raises on connection failure or unexpected responses.
    """
    reader, writer = await asyncio.open_connection(host, port)

    # Step 1: Read 220 greeting (save it to forward to client later)
    greeting_lines = await _read_smtp_response(reader)
    greeting_bytes = b"".join(greeting_lines)

    # Step 2: Send EHLO pre-STARTTLS (required to discover STARTTLS capability)
    writer.write(b"EHLO nuvrail-proxy\r\n")
    await writer.drain()
    await _read_smtp_response(reader)  # discard pre-TLS capabilities

    # Step 3: Send STARTTLS command
    writer.write(b"STARTTLS\r\n")
    await writer.drain()
    starttls_resp = await _read_smtp_response(reader)
    if not starttls_resp or not starttls_resp[0].startswith(b"220"):
        raise RuntimeError(f"Unexpected STARTTLS response: {starttls_resp!r}")

    # Step 4: Upgrade the transport to TLS.
    # Python 3.9 does not have StreamWriter.start_tls() (added in 3.11), so
    # we call loop.start_tls() directly, passing the underlying transport and
    # protocol, then patch the writer's _transport reference so subsequent
    # writer.write() calls go over the TLS channel.
    ssl_ctx = ssl.create_default_context()
    loop = asyncio.get_running_loop()
    tls_transport = await loop.start_tls(
        writer.transport,
        writer._protocol,  # type: ignore[attr-defined]
        ssl_ctx,
        server_side=False,
        server_hostname=host,
    )
    writer._transport = tls_transport  # type: ignore[attr-defined]

    # Step 5: EHLO again — required by RFC 3207 after STARTTLS
    writer.write(b"EHLO nuvrail-proxy\r\n")
    await writer.drain()
    # (Caller will read this response in the command loop as needed)

    return reader, writer, greeting_bytes


# ---------------------------------------------------------------------------
# Main connection handler
# ---------------------------------------------------------------------------


async def _send_smtp_rejection_notices(
    writer: asyncio.StreamWriter,
    agent_id: str,
    db_path: object,
) -> None:
    """After successful auth, send 214 notices for any unnotified rejected SMTP ops.

    SMTP has no out-of-band push mechanism, so rejections are delivered
    in-band as RFC 2821 § 4.2.2 informational 214 responses immediately
    after the 235 auth success, before the agent's first command.

    Format:
      214 [NUVRAIL] REJECTED op_id: <human reason or 'human decision'>

    The agent should parse 214 lines for op IDs it sent previously.
    After delivery, the ops are marked rejection_notified=1 in the DB
    so they are not re-sent on the next connection.

    Phase 2 note: relies on a rejection_notified column added by migration.
    If the column is absent (pre-migration DB), notices are skipped silently.
    """
    from pathlib import Path

    if not isinstance(db_path, Path):
        db_path = Path(str(db_path))

    async with get_db(db_path) as db:
        # Check for the rejection_notified column (idempotent guard)
        async with db.execute("PRAGMA table_info(staged_operations)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if "rejection_notified" not in cols:
            return  # pre-migration DB — skip silently

        async with db.execute(
            """
            SELECT id, description, decided_at
            FROM staged_operations
            WHERE protocol = 'smtp'
              AND status = 'rejected'
              AND agent_id = ?
              AND (rejection_notified IS NULL OR rejection_notified = 0)
            ORDER BY decided_at ASC
            LIMIT 10
            """,
            (agent_id,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        if not rows:
            return

        for row in rows:
            notice = (
                f"214 [NUVRAIL] REJECTED {row['id']}: "
                f"{row.get('description', 'send rejected by approver')}"
                f"\r\n"
            ).encode()
            writer.write(notice)

        await writer.drain()

        # Mark all as notified
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" * len(ids))
        await db.execute(
            f"UPDATE staged_operations SET rejection_notified = 1 WHERE id IN ({placeholders})",  # noqa: S608
            ids,
        )
        await db.commit()


async def handle_smtp_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    """
    Handle one SMTP client connection.

    Auth-before-upstream flow:

      Client connects
          │
          ▼
      Proxy sends synthetic 220 greeting (no upstream yet)
          │
          ▼
      Relay EHLO locally with synthetic capabilities
          │
          ▼
      Wait for AUTH — verify agent credentials
          ├─ fail → 535 + close
          │
          ▼
      Open upstream using credential.upstream_host/upstream_smtp_port
      Negotiate STARTTLS, authenticate to upstream with real credentials
          │
          ▼
      Continue command loop (MAIL FROM, RCPT TO, DATA, QUIT)

    Each agent's upstream host/port comes from agent_credentials row.
    """
    peer = client_writer.get_extra_info("peername", ("?", 0))
    peer_str = f"{peer[0]}:{peer[1]}"
    logger.info("[%s] SMTP client connected", peer_str)

    # Resolve the DB path once at connection start. Read from the module
    # attribute (not the import-time DB_PATH) so integration tests that patch
    # gateway.state_db.DB_PATH before connecting are honoured.
    import gateway.state_db as _state_db_mod
    _db_path = _state_db_mod.DB_PATH

    # --- Step 1: Send synthetic greeting — no upstream connection yet --------
    try:
        client_writer.write(b"220 Nuvrail SMTP proxy ready\r\n")
        await client_writer.drain()
    except OSError as exc:
        logger.error("[%s] Failed to send greeting: %s", peer_str, exc)
        client_writer.close()
        return

    upstream_reader: asyncio.StreamReader | None = None
    upstream_writer: asyncio.StreamWriter | None = None
    upstream_credential: dict | None = None

    # --- Envelope tracking -------------------------------------------------
    sender: str | None = None
    recipients: list[str] = []

    # --- Command loop ------------------------------------------------------
    try:
        while True:
            try:
                line_bytes = await client_reader.readline()
            except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
                break
            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            cmd = line.split()[0].upper() if line.split() else ""

            logger.debug("[%s] C→P: %s", peer_str, redact_protocol_line(line))

            # ----------------------------------------------------------------
            # EHLO / HELO — respond locally before upstream is connected
            # ----------------------------------------------------------------
            if cmd in ("EHLO", "HELO"):
                client_writer.write(
                    b"250-Nuvrail proxy\r\n"
                    b"250-AUTH PLAIN LOGIN\r\n"
                    b"250-SIZE 52428800\r\n"
                    b"250 8BITMIME\r\n"
                )
                await client_writer.drain()
                continue

            # ----------------------------------------------------------------
            # AUTH — verify agent credentials, then open upstream connection
            # ----------------------------------------------------------------
            if cmd == "AUTH":
                parts = line.split()
                mech = parts[1].upper() if len(parts) > 1 else ""
                cred: dict | None = None

                if mech == "PLAIN" and len(parts) >= 3:
                    agent_user, agent_pass_plain = decode_sasl_plain(parts[2]) or ("", "")
                    decision = await SMTP_AUTH_ABUSE_PROTECTOR.start_attempt(
                        ip=str(peer[0]), account=agent_user or "<unknown>"
                    )
                    if not decision.allowed:
                        client_writer.write(
                            f"454 4.7.0 Too many auth attempts. Retry in {decision.retry_after_seconds}s\r\n".encode()
                        )
                        await client_writer.drain()
                        logger.warning(
                            "[%s] SMTP auth blocked user=%s reason=%s retry_after=%ss",
                            peer_str,
                            agent_user,
                            decision.reason,
                            decision.retry_after_seconds,
                        )
                        break
                    cred = await verify_agent_login(agent_user, agent_pass_plain, _db_path)
                    if cred:
                        await SMTP_AUTH_ABUSE_PROTECTOR.record_success(
                            ip=str(peer[0]), account=agent_user or "<unknown>"
                        )
                        logger.info("[%s] SMTP agent authenticated: %s", peer_str, agent_user)
                    else:
                        failure = await SMTP_AUTH_ABUSE_PROTECTOR.record_failure(
                            ip=str(peer[0]), account=agent_user or "<unknown>"
                        )
                        if failure.lockout_applied:
                            logger.warning(
                                "[%s] SMTP auth lockout triggered user=%s retry_after=%ss",
                                peer_str,
                                agent_user,
                                failure.retry_after_seconds,
                            )
                        logger.warning("[%s] SMTP AUTH PLAIN failed for user=%s", peer_str, agent_user)

                elif mech == "PLAIN" and len(parts) < 3:
                    # AUTH PLAIN without inline credentials — send challenge
                    client_writer.write(b"334 \r\n")
                    await client_writer.drain()
                    cred_line = await client_reader.readline()
                    agent_user, agent_pass_plain = decode_sasl_plain(cred_line.strip()) or ("", "")
                    decision = await SMTP_AUTH_ABUSE_PROTECTOR.start_attempt(
                        ip=str(peer[0]), account=agent_user or "<unknown>"
                    )
                    if not decision.allowed:
                        client_writer.write(
                            f"454 4.7.0 Too many auth attempts. Retry in {decision.retry_after_seconds}s\r\n".encode()
                        )
                        await client_writer.drain()
                        logger.warning(
                            "[%s] SMTP auth blocked user=%s reason=%s retry_after=%ss",
                            peer_str,
                            agent_user,
                            decision.reason,
                            decision.retry_after_seconds,
                        )
                        break
                    cred = await verify_agent_login(agent_user, agent_pass_plain, _db_path)
                    if cred:
                        await SMTP_AUTH_ABUSE_PROTECTOR.record_success(
                            ip=str(peer[0]), account=agent_user or "<unknown>"
                        )
                    else:
                        await SMTP_AUTH_ABUSE_PROTECTOR.record_failure(
                            ip=str(peer[0]), account=agent_user or "<unknown>"
                        )

                elif mech == "LOGIN":
                    # AUTH LOGIN multi-step
                    import base64 as _b64l
                    client_writer.write(b"334 " + _b64l.b64encode(b"Username:") + b"\r\n")
                    await client_writer.drain()
                    user_b64 = await client_reader.readline()
                    client_writer.write(b"334 " + _b64l.b64encode(b"Password:") + b"\r\n")
                    await client_writer.drain()
                    pass_b64 = await client_reader.readline()
                    try:
                        agent_user = _b64l.b64decode(user_b64.strip()).decode("utf-8", errors="replace")
                        agent_pass_plain = _b64l.b64decode(pass_b64.strip()).decode("utf-8", errors="replace")
                    except Exception:
                        agent_user, agent_pass_plain = "", ""
                    decision = await SMTP_AUTH_ABUSE_PROTECTOR.start_attempt(
                        ip=str(peer[0]), account=agent_user or "<unknown>"
                    )
                    if not decision.allowed:
                        client_writer.write(
                            f"454 4.7.0 Too many auth attempts. Retry in {decision.retry_after_seconds}s\r\n".encode()
                        )
                        await client_writer.drain()
                        logger.warning(
                            "[%s] SMTP auth blocked user=%s reason=%s retry_after=%ss",
                            peer_str,
                            agent_user,
                            decision.reason,
                            decision.retry_after_seconds,
                        )
                        break
                    cred = await verify_agent_login(agent_user, agent_pass_plain, _db_path)
                    if cred:
                        await SMTP_AUTH_ABUSE_PROTECTOR.record_success(
                            ip=str(peer[0]), account=agent_user or "<unknown>"
                        )
                    else:
                        await SMTP_AUTH_ABUSE_PROTECTOR.record_failure(
                            ip=str(peer[0]), account=agent_user or "<unknown>"
                        )

                else:
                    client_writer.write(
                        "504 5.5.4 Authentication mechanism not supported"
                        " — try AUTH PLAIN\r\n".encode()
                    )
                    await client_writer.drain()
                    logger.warning(
                        "[%s] SMTP unsupported auth mechanism: %s",
                        peer_str, mech,
                    )
                    continue

                if cred is None:
                    client_writer.write(b"535 5.7.8 Authentication credentials invalid\r\n")
                    await client_writer.drain()
                    logger.warning("[%s] SMTP auth failed — closing", peer_str)
                    break

                # Auth succeeded — open upstream using per-agent host/port
                upstream_host = cred["upstream_smtp_host"] or cred["upstream_host"]
                upstream_port = int(cred["upstream_smtp_port"])
                up_user = cred["upstream_user"]

                try:
                    upstream_reader, upstream_writer, _ = await _connect_upstream_starttls(
                        upstream_host, upstream_port
                    )
                except Exception as exc:
                    logger.error(
                        "[%s] Failed to connect to upstream %s:%d — %s",
                        peer_str, upstream_host, upstream_port, exc,
                    )
                    client_writer.write(
                        f"421 4.4.1 Upstream SMTP server temporarily"
                        f" unreachable ({upstream_host}:{upstream_port})"
                        f" — try again later\r\n".encode()
                    )
                    await client_writer.drain()
                    break

                # Drain the post-STARTTLS EHLO response
                await _read_smtp_response(upstream_reader)

                # Authenticate to upstream: XOAUTH2 if oauth2_provider is set,
                # else AUTH PLAIN with real credentials.
                if cred.get("oauth2_provider"):
                    import gateway.state_db as _state_db_mod
                    from gateway.oauth2_tokens import (
                        OAuth2Error,
                        get_xoauth2_string,
                    )
                    try:
                        xoauth2_str = await get_xoauth2_string(str(cred["id"]), _state_db_mod.DB_PATH)
                    except OAuth2Error as exc:
                        logger.error("[%s] SMTP XOAUTH2 token fetch failed: %s", peer_str, exc)
                        client_writer.write(b"535 5.7.8 Upstream OAuth2 authentication failed\r\n")
                        await client_writer.drain()
                        break
                    upstream_writer.write(f"AUTH XOAUTH2 {xoauth2_str}\r\n".encode())
                    await upstream_writer.drain()
                    upstream_auth_resp = await _read_smtp_response(upstream_reader)
                    # 334 = challenge/error frame from Google; 235 = success
                    if not upstream_auth_resp or not upstream_auth_resp[0].startswith(b"235"):
                        logger.error(
                            "[%s] Upstream SMTP XOAUTH2 AUTH failed (response redacted)",
                            peer_str,
                        )
                        client_writer.write(b"535 5.7.8 Upstream authentication failed\r\n")
                        await client_writer.drain()
                        break
                else:
                    import base64 as _b64u
                    up_pass = await fetch_credential(cred["upstream_password"])
                    rewritten_b64 = _b64u.b64encode(
                        f"\x00{up_user}\x00{up_pass}".encode()
                    ).decode()
                    upstream_writer.write(f"AUTH PLAIN {rewritten_b64}\r\n".encode())
                    await upstream_writer.drain()
                    upstream_auth_resp = await _read_smtp_response(upstream_reader)
                    if not upstream_auth_resp or not upstream_auth_resp[0].startswith(b"235"):
                        logger.error("[%s] Upstream AUTH failed: %r", peer_str, upstream_auth_resp)
                        client_writer.write(b"535 5.7.8 Upstream authentication failed\r\n")
                        await client_writer.drain()
                        break

                upstream_credential = cred
                client_writer.write(b"235 2.7.0 Authentication succeeded\r\n")
                await client_writer.drain()
                logger.info(
                    "[%s] Upstream authenticated: %s:%d user=%s",
                    peer_str, upstream_host, upstream_port, up_user,
                )

                # Notify agent of any previously-rejected SMTP sends.
                # SMTP has no unsolicited push; we deliver rejection notices
                # as informational 214 lines immediately after auth success.
                # The agent sees them before its first command is accepted.
                try:
                    import gateway.state_db as _state_db_mod2
                    await _send_smtp_rejection_notices(
                        client_writer, str(cred["id"]),
                        _state_db_mod2.DB_PATH,
                    )
                except Exception as _notice_exc:
                    logger.warning(
                        "[%s] Failed to send rejection notices: %s",
                        peer_str, _notice_exc,
                    )
                continue

            # ----------------------------------------------------------------
            # Require auth before any envelope commands
            # ----------------------------------------------------------------
            if upstream_writer is None:
                client_writer.write(b"530 5.7.0 Authentication required\r\n")
                await client_writer.drain()
                continue

            # ----------------------------------------------------------------
            # MAIL FROM — track sender, pass through
            # ----------------------------------------------------------------
            if cmd == "MAIL":
                m = _MAIL_FROM_RE.search(line)
                if m:
                    sender = m.group(1)
                    logger.debug("[%s] MAIL FROM: %s", peer_str, sender)
                # Rewrite MAIL FROM with the upstream user's real address.
                # The agent may send any From address (e.g. nuvrail_xxx@nuvrail.example.com);
                # upstream will reject it unless it matches the authenticated account.
                if upstream_credential is not None:
                    real_from = upstream_credential["upstream_user"]
                    rewritten_mail = f"MAIL FROM:<{real_from}>\r\n"
                    upstream_writer.write(rewritten_mail.encode())
                    logger.debug("[%s] MAIL FROM rewritten: %s → %s", peer_str, sender, real_from)
                else:
                    upstream_writer.write(line_bytes)
                await upstream_writer.drain()
                resp_lines = await _read_smtp_response(upstream_reader)
                client_writer.write(b"".join(resp_lines))
                await client_writer.drain()

            # ----------------------------------------------------------------
            # RCPT TO — accumulate recipients, pass through
            # ----------------------------------------------------------------
            elif cmd == "RCPT":
                m = _RCPT_TO_RE.search(line)
                if m:
                    recipients.append(m.group(1))
                    logger.debug("[%s] RCPT TO: %s", peer_str, m.group(1))
                upstream_writer.write(line_bytes)
                await upstream_writer.drain()
                resp_lines = await _read_smtp_response(upstream_reader)
                client_writer.write(b"".join(resp_lines))
                await client_writer.drain()

            # ----------------------------------------------------------------
            # DATA — intercept: consume body, do NOT forward, return STAGED
            # ----------------------------------------------------------------
            elif cmd == "DATA":
                # Respond 354 directly to client without touching upstream.
                #
                # We do NOT forward DATA upstream here.  The prior approach
                # forwarded DATA to upstream for a pre-check (354), read and
                # staged the body without sending it, then left upstream
                # hanging in DATA state.  When the agent sent QUIT, upstream
                # was still waiting for the body terminator (\.\r\n), so
                # _read_smtp_response() blocked forever.
                #
                # The upstream transaction (MAIL FROM + RCPT TO already
                # forwarded) is cancelled via RSET after staging (step 9).
                # Relay-side validation happens at approve time.
                client_writer.write(b"354 Start mail input; end with <CRLF>.<CRLF>\r\n")
                await client_writer.drain()

                # 4. Consume message body from client until terminator line ".\r\n"
                body_lines: list[bytes] = []
                byte_count = 0
                while True:
                    body_line = await client_reader.readline()
                    if not body_line:
                        break
                    if body_line in (b".\r\n", b".\n"):
                        break
                    body_lines.append(body_line)
                    byte_count += len(body_line)

                # 5. Extract headers for staging + reply/forward intent
                send_headers = _extract_send_headers(body_lines)
                subject = send_headers.get("subject") or "<no subject>"
                in_reply_to = send_headers.get("in-reply-to")
                references = send_headers.get("references")
                logger.info(
                    "[%s] STAGED DATA: from=%s to=%s subject=%r bytes=%d",
                    peer_str,
                    sender or "<unknown>",
                    recipients,
                    subject,
                    byte_count,
                )

                # 6. Do NOT forward body or terminating "." to upstream

                # 7. Create Operation record in staging engine.
                #
                # Reply/forward intent: the parent msg-id (In-Reply-To, or the
                # last References entry — the direct parent per RFC 5322) is
                # looked up in the mailbox mirror so the approval card can name
                # the original message. A miss only lowers confidence; the
                # intent still derives from the outgoing headers.
                parent_ids = extract_message_ids(in_reply_to) or extract_message_ids(references)
                original: dict | None = None
                if parent_ids and upstream_credential is not None:
                    try:
                        original = await find_message_by_message_id(
                            parent_ids[-1],
                            user_id=upstream_credential["user_id"],
                            db_path=_db_path,
                        )
                    except Exception as _lookup_exc:
                        logger.debug(
                            "[%s] Original-message lookup failed (non-fatal): %s",
                            peer_str, _lookup_exc,
                        )
                send_intent, send_intent_conf = derive_send_intent(
                    subject, in_reply_to, references, original is not None
                )
                description = _build_send_description(
                    send_intent, subject, recipients, original
                )

                body_text = b"".join(body_lines).decode("utf-8", errors="replace")
                # body_rendered is a DISPLAY-ONLY decode of the raw payload:
                # reverses Content-Transfer-Encoding (base64 / quoted-printable)
                # and reduces HTML to rough text so a reviewer can actually read
                # what will be sent (issue #154). It is NEVER used for relay —
                # execution.py replays the untouched raw `body` verbatim.
                body_rendered = render_body(body_text)
                # Preview the *rendered* text, not the raw payload, so the
                # collapsed one-liner is readable for encoded bodies too.
                body_preview = body_rendered[:200]  # quick-scan display only
                envelope = {
                    "from": sender or "",
                    "to": recipients,
                    "subject": subject,
                    "body": body_text,       # RAW full body — relayed verbatim on approval
                    "body_rendered": body_rendered,  # decoded, display-only (issue #154)
                    "body_preview": body_preview,  # truncated rendered text for quick-scan
                }
                if in_reply_to:
                    envelope["in_reply_to"] = in_reply_to
                if original is not None:
                    # Original-message context for the approval card.
                    envelope["original"] = {
                        "subject": original.get("subject"),
                        "sender": original.get("sender"),
                    }
                try:
                    op_id = await create_operation(
                        op_type="smtp_send",
                        protocol="smtp",
                        agent_id=upstream_credential["id"] if upstream_credential else None,
                        description=description,
                        smtp_envelope=envelope,
                        intent_label=send_intent,
                        intent_confidence=send_intent_conf,
                        db_path=_db_path,
                    )
                    staged_resp = (
                        f"250 OK [STAGED] Send queued for approval — ID: {op_id}\r\n"
                    )
                except Exception as exc:
                    logger.error("[%s] Failed to stage SMTP operation: %s", peer_str, exc)
                    staged_resp = "250 OK [STAGED] Send queued for approval — ID: pending\r\n"

                # 8. Return STAGED response to client
                client_writer.write(staged_resp.encode())
                await client_writer.drain()

                # 9. RSET upstream to cancel the open MAIL FROM/RCPT TO
                # transaction.  Upstream never saw DATA, so it has a pending
                # envelope; RSET clears it and leaves the connection ready
                # for the next command (including QUIT).
                try:
                    upstream_writer.write(b"RSET\r\n")
                    await upstream_writer.drain()
                    await _read_smtp_response(upstream_reader)
                except Exception as rset_exc:
                    logger.warning("[%s] RSET after staging failed (non-fatal): %s", peer_str, rset_exc)

                # 10. Reset envelope tracking for potential next message
                sender = None
                recipients = []

            # ----------------------------------------------------------------
            # QUIT — pass through, then close both sides
            # ----------------------------------------------------------------
            elif cmd == "QUIT":
                upstream_writer.write(line_bytes)
                await upstream_writer.drain()
                resp_lines = await _read_smtp_response(upstream_reader)
                client_writer.write(b"".join(resp_lines))
                await client_writer.drain()
                break

            # ----------------------------------------------------------------
            # Fail closed: only enumerated session verbs reach upstream.
            # Anything else (BDAT from CHUNKING, ETRN, XCLIENT, STARTTLS,
            # unknown extensions) is refused locally. BDAT in particular is a
            # second way to submit a message that would skip DATA staging.
            # ----------------------------------------------------------------
            elif cmd not in _SMTP_PASSTHROUGH_COMMANDS:
                client_writer.write(
                    f"502 5.5.1 Nuvrail: {cmd or 'empty command'} is not"
                    f" permitted for agents\r\n".encode()
                )
                await client_writer.drain()
                logger.warning(
                    "[%s] REJECTED SMTP command: %s", peer_str, redact_protocol_line(line)
                )

            # ----------------------------------------------------------------
            # Session verbs (NOOP, RSET, HELP, VRFY) — pass through
            # ----------------------------------------------------------------
            else:
                upstream_writer.write(line_bytes)
                await upstream_writer.drain()
                resp_lines = await _read_smtp_response(upstream_reader)
                resp_bytes = b"".join(resp_lines)

                # Strip STARTTLS from EHLO capabilities — client is already plain TCP;
                # advertising STARTTLS would confuse clients into attempting an upgrade
                # over a channel that is already plaintext on the client side.
                if cmd in ("EHLO", "HELO"):
                    resp_bytes = b"".join(_strip_starttls(list(resp_lines)))

                client_writer.write(resp_bytes)
                await client_writer.drain()

    except (ConnectionResetError, BrokenPipeError, OSError) as exc:
        logger.warning("[%s] Connection error: %s", peer_str, exc)
    finally:
        for w in (upstream_writer, client_writer):
            if w is None:
                continue
            try:
                w.close()
                await w.wait_closed()
            except OSError:
                pass
        logger.info("[%s] SMTP client disconnected", peer_str)


# ---------------------------------------------------------------------------
# Server entry points
# ---------------------------------------------------------------------------


async def start_smtp_proxy(host: str, port: int) -> asyncio.AbstractServer:
    """Start the SMTP proxy and return the Server object.

    Separated from main() so tests can bind to port 0 and retrieve the
    ephemeral port via server.sockets[0].getsockname()[1].
    """
    server = await asyncio.start_server(handle_smtp_client, host, port)
    actual = server.sockets[0].getsockname()
    logger.info(
        "Nuvrail SMTP proxy listening on %s:%d (upstream host/port per agent credential)",
        actual[0],
        actual[1],
    )
    return server


async def main() -> None:
    """Entry point: read config from env and start the proxy."""
    # Load optional plugins in THIS process. The SMTP proxy stages send
    # operations (create_operation -> run_auto_decision), so the auto-decision
    # provider must be registered here too. No-op in open core.
    load_plugins()

    host = os.environ.get("NUVRAIL_PROXY_HOST", "127.0.0.1")
    warn_if_loopback_bind_in_container(host)
    port = int(os.environ.get("NUVRAIL_PROXY_SMTP_PORT", "10587"))

    server = await start_smtp_proxy(host, port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
