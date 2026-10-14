"""
Nuvrail IMAP Proxy — Milestone 0.2: Parser-aware intercept.

Data flow (0.2):

  Client (plain TCP)               proxy.py                Upstream (SSL/TLS)
  ──────────────────               ────────                ──────────────────
       connect          ──────────────────────────►        SSL connect
                        ◄──────────────────────────        greeting
       ◄── greeting

       cmd ────────────────► parse_line()
                                   │
                          ┌────────┴──────────────────────┐
                          │  classify() → read             │  classify() → write/blocked
                          │                                │
                          ▼                                ▼
                  forward cmd to upstream          intercept & respond locally
                  ◄──── upstream response          send OK [STAGED] / OK Noted
                  ◄──── forward response

       (special case: sync literal {N})
       ◄── "+ Ready for literal data"
       literal bytes ────────────────                 (discarded — staged stub)
       ◄── "<tag> OK [STAGED] ..."

       ...              ◄──────────────────────────        ...
       disconnect ────────────────────────────────►

Sub-milestone 0.1 LOGIN intercept is preserved; 0.3 will translate to XOAUTH2.
Upstream→client direction is still a raw byte pump.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import ssl
from pathlib import Path

from dotenv import load_dotenv

from gateway.agent_auth import decode_sasl_plain, verify_agent_login
from gateway.batching import get_or_create_batch
from gateway.command_router import classify, filter_capabilities, nonsync_literal_size
from gateway.credentials import fetch_credential
from gateway.extensions import load_plugins
from gateway.imap_parser import ParsedCommand, parse_line
from gateway.imap_response_parser import (
    _HEADER_READ_LIMIT,
    _RE_FETCH_LITERAL,
    extract_headers_from_rfc822,
    parse_fetch_line,
    parse_list_folders,
    parse_select_response,
)
from gateway.intent import derive_intent, role_from_list_attributes
from gateway.operation_parser import (
    ParsedOperation,
    build_rich_description,
    parse_append,
    parse_copy,
    parse_move,
    parse_store,
)
from gateway.provider_profiles import (
    PendingCopyIntent,
    ProviderProfile,
    copy_archive_intent,
    detect_provider,
    should_suppress_append,
)
from gateway.security_controls import build_auth_abuse_protector
from gateway.staging import create_operation
from gateway.state_db import (
    DB_PATH,
    apply_optimistic_flag_update,
    get_message,
    get_message_metadata_by_uid_set,
    get_pending_flag_changes_for_uid,
    get_pending_move_uids_for_folder,
    get_pending_reverts,
    get_special_use_folders,
    get_trash_folder,
    init_db,
    mark_reverts_delivered,
    remove_messages_from_folder,
    snapshot_messages,
    update_folder_stats,
    upsert_folders_from_list,
    upsert_message,
    warn_if_loopback_bind_in_container,
)

load_dotenv()

logger = logging.getLogger(__name__)
IMAP_AUTH_ABUSE_PROTECTOR = build_auth_abuse_protector("imap_proxy_login")

_READ_CHUNK = 4096

# Largest APPEND body we persist (base64) for later replay on approval.
# Oversized messages are still consumed off the wire (to stay protocol-synced)
# but staged without a body — they will not be replayed upstream.
_MAX_APPEND_BYTES = int(os.environ.get("NUVRAIL_MAX_APPEND_BYTES", str(10 * 1024 * 1024)))

# Matches the literal size at end of an IMAP line: {N} or {N+}
_LITERAL_RE = re.compile(r"\{(\d+)\+?\}$")


def _build_parsed_op(parsed: ParsedCommand) -> ParsedOperation | None:
    """Convert a ParsedCommand into a ParsedOperation for staging.

    Returns None for commands that should use generic staging (CREATE, RENAME, etc.).
    """
    cmd = parsed.command
    args = parsed.args
    uid_mode = parsed.uid

    if cmd in ("STORE",):
        # STORE uid_set flags_op flags_list
        uid_set = args[0] if args else "?"
        flags_op = args[1] if len(args) > 1 else "+FLAGS"
        # flags may be in parentheses like (\Seen) or bare \Seen
        raw_flags = args[2] if len(args) > 2 else "()"
        if raw_flags.startswith("(") and raw_flags.endswith(")"):
            flags = raw_flags[1:-1].split()
        else:
            flags = [raw_flags]
        return parse_store(parsed.tag, uid_mode, uid_set, flags_op, flags)

    if cmd in ("MOVE",):
        uid_set = args[0] if args else "?"
        # Join remaining args: folder names may contain spaces (e.g. "[Gmail]/All Mail")
        # and agents may send them unquoted, causing the tokenizer to split them.
        destination = " ".join(args[1:]) if len(args) > 1 else "?"
        return parse_move(parsed.tag, uid_set, destination)

    if cmd in ("COPY",):
        uid_set = args[0] if args else "?"
        # Join remaining args: folder names may contain spaces (e.g. "[Gmail]/All Mail")
        # and agents may send them unquoted, causing the tokenizer to split them.
        destination = " ".join(args[1:]) if len(args) > 1 else "?"
        return parse_copy(parsed.tag, uid_set, destination)

    # APPEND is handled via the literal path; if it somehow reaches here, generic staging
    # CREATE, RENAME → return None (caller uses generic staging)
    return None


async def _load_special_use(session: dict, db_path: Path, peer: str) -> dict:
    """Return the tenant's {folder name: RFC 6154 role} mapping for intent derivation.

    The session cache is seeded at connection time (proactive discovery in
    handle_client + a DB load) and kept current by the LIST sync handler, so
    the common path is a dict lookup. The DB fallback only fires for sessions
    constructed without the cache (tests, future call sites). An empty dict
    simply means intent falls back to provider-profile / name-heuristic
    classification. Never raises — staging must not fail because a lookup did.
    """
    cached = session.get("special_use")
    if cached is not None:
        return cached
    try:
        loaded = await get_special_use_folders(
            user_id=session.get("user_id"), db_path=db_path
        )
        session["special_use"] = loaded
        return loaded
    except Exception as exc:
        logger.debug("[%s] special-use lookup failed (non-fatal): %s", peer, exc)
        return {}


async def _resolve_trash_folder(
    session: dict,
    db_path: Path,
    profile: ProviderProfile | None,
    peer: str,
) -> str | None:
    """Pick the Trash folder an approved ``trash`` op will be moved into (#168).

    Resolved at staging time so the human approves the action that will
    actually run, and so the destination is recorded in ``folder_to`` (undo
    moves it back from there). Resolution order:

      1. the server-declared RFC 6154 ``\\Trash`` folder (exact name), then
      2. the provider profile's ``trash_folder`` (Gmail/iCloud/Outlook).

    Folder-name heuristics are deliberately NOT used: a mis-guessed folder
    would send approved deletes somewhere the human didn't expect. None means
    "no Trash folder known" and the op is staged — and labelled — as a plain
    ``\\Deleted`` flag instead. Never raises.
    """
    try:
        declared = await get_trash_folder(user_id=session.get("user_id"), db_path=db_path)
    except Exception as exc:
        logger.debug("[%s] trash-folder lookup failed (non-fatal): %s", peer, exc)
        declared = None
    if declared:
        return declared
    if profile is not None and profile.trash_folder:
        return profile.trash_folder
    return None


# Budget for the proxy-issued SPECIAL-USE discovery exchange at connect time.
# The line cap is far above any realistic folder count; in practice it only
# fires on a misbehaving server.
_DISCOVERY_TIMEOUT_SECONDS = float(os.environ.get("NUVRAIL_SPECIAL_USE_DISCOVERY_TIMEOUT", "10"))
_DISCOVERY_MAX_LINES = 2000


async def _read_upstream_until_tagged(
    upstream_reader: asyncio.StreamReader,
    tag: bytes,
    peer: str,
) -> list[str] | None:
    """Collect upstream response lines until the tagged completion for ``tag``.

    Returns the untagged lines (decoded, CRLF-stripped), or None on timeout /
    line-cap overflow / EOF. A None return means the exchange did not complete
    cleanly and leftover response lines may still be in flight — callers must
    abort the discovery (the stray lines would then surface to the client as
    unsolicited responses, which IMAP clients are required to tolerate).
    """
    lines: list[str] = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _DISCOVERY_TIMEOUT_SECONDS
    tag_prefix = tag + b" "
    while len(lines) < _DISCOVERY_MAX_LINES:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            line = await asyncio.wait_for(upstream_reader.readline(), timeout=remaining)
        except TimeoutError:
            break
        if not line:
            break
        if line.startswith(tag_prefix):
            return lines
        lines.append(line.decode("utf-8", errors="replace").rstrip("\r\n"))
    logger.warning(
        "[%s] SPECIAL-USE discovery: no tagged completion for %r (%d lines read)",
        peer, tag.decode(), len(lines),
    )
    return None


async def _discover_special_use(
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    known_caps: str,
    *,
    user_id: int | None,
    db_path: Path,
    peer: str,
) -> None:
    """Proactively capture SPECIAL-USE folder roles with one proxy-issued LIST.

    Opportunistic capture (the LIST sync handler) only works if the agent
    happens to issue a LIST; an agent that goes straight to SELECT/MOVE would
    leave intent derivation on name heuristics. This runs once per connection,
    after upstream auth and BEFORE the byte pumps start, so the proxy has
    exclusive use of the upstream stream and can consume the responses without
    leaking them to the client.

    ``known_caps`` is any capability text already seen during the auth
    exchange; an explicit CAPABILITY round-trip is only made when it doesn't
    mention SPECIAL-USE. Servers that don't advertise SPECIAL-USE are skipped
    entirely. Failures are non-fatal — the connection proceeds either way.
    """
    caps = known_caps or ""
    if "SPECIAL-USE" not in caps.upper():
        upstream_writer.write(b"nuvcap0 CAPABILITY\r\n")
        await upstream_writer.drain()
        cap_lines = await _read_upstream_until_tagged(upstream_reader, b"nuvcap0", peer)
        if cap_lines is None:
            return
        caps = " ".join(cap_lines)
    if "SPECIAL-USE" not in caps.upper():
        logger.debug("[%s] upstream does not advertise SPECIAL-USE — discovery skipped", peer)
        return

    upstream_writer.write(b'nuvlist0 LIST "" "*" RETURN (SPECIAL-USE)\r\n')
    await upstream_writer.drain()
    list_lines = await _read_upstream_until_tagged(upstream_reader, b"nuvlist0", peer)
    if list_lines is None:
        return
    listed = parse_list_folders(
        [line for line in list_lines if line.upper().startswith("* LIST")]
    )
    roles: dict[str, str] = {}
    for f in listed:
        role = role_from_list_attributes(f.attributes)
        if role:
            roles[f.name] = role
    if listed:
        await upsert_folders_from_list(
            [f.name for f in listed],
            user_id=user_id,
            special_use=roles or None,
            db_path=db_path,
        )
    logger.info(
        "[%s] SPECIAL-USE discovery: %d folders listed, roles: %s",
        peer, len(listed), roles or "none",
    )


async def _inject_pending_reverts(
    client_writer: asyncio.StreamWriter,
    session: dict,
    db_path: Path,
    peer: str,
) -> None:
    """Inject unsolicited FETCH responses for any pending rejected operations.

    Called after the tagged OK for SELECT/NOOP/FETCH is forwarded to the client.
    The injected lines arrive after the tagged OK — valid per RFC 3501 §7 (unsolicited
    responses may be sent at any time). From the AI's perspective, another client
    modified the mailbox between commands — standard IMAP sync behavior.

    Injection format:
      * {seq_num} FETCH (UID {uid} FLAGS ({flags}))

    Errors are always swallowed — never propagated to the byte pump.
    """
    folder_id = session.get("folder_id")
    if folder_id is None:
        return
    try:
        reverts = await get_pending_reverts(folder_id, db_path=db_path)
        if not reverts:
            return
        lines: list[bytes] = []
        ids_to_mark: list[int] = []
        for r in reverts:
            uid = r["uid"]
            msg = await get_message(folder_id, uid, db_path=db_path)
            seq_num = msg["sequence_num"] if msg and msg.get("sequence_num") else 1
            true_flags = json.loads(r["true_flags"]) if r["true_flags"] else []
            flags_str = " ".join(true_flags)
            line = f"* {seq_num} FETCH (UID {uid} FLAGS ({flags_str}))\r\n"
            lines.append(line.encode())
            ids_to_mark.append(r["id"])
        # Write all injected lines in one go
        for line_bytes in lines:
            client_writer.write(line_bytes)
        await client_writer.drain()
        await mark_reverts_delivered(ids_to_mark, db_path=db_path)
        logger.info(
            "[%s] Injected %d unsolicited FETCH revert(s) for folder_id=%s",
            peer, len(lines), folder_id,
        )
    except Exception as exc:
        logger.warning("[%s] _inject_pending_reverts failed (non-fatal): %s", peer, exc)


async def _sync_upstream_line(
    raw_line: str,
    session: dict,
    db_path: Path,
    peer: str,
) -> None:
    """Inspect one upstream response line and update the local state DB.

    Session dict shape:
      {
        'folder':             str | None,   # currently selected folder name
        'folder_id':          int | None,   # cached DB id for current folder
        'select_lines':       list[str],    # untagged lines accumulated for SELECT
        'in_select':          bool,         # True while accumulating SELECT lines
        'revert_trigger_tag': str | None,   # tag of last SELECT/NOOP/FETCH sent by AI
      }

    Response handling:
      SELECT — accumulate untagged lines, flush when tagged OK [READ-WRITE/READ-ONLY]
      LIST   — parse folder name, upsert into folders table
      FETCH  — parse UID/flags/envelope, upsert into messages table

    Revert injection is handled by _upstream_to_client BEFORE forwarding the tagged
    OK line — so the AI receives [* N FETCH ...][tag OK] in the right order.

    Errors are logged at WARNING but never re-raised.
    """
    stripped = raw_line.rstrip("\r\n")

    # ------------------------------------------------------------------
    # SELECT accumulation
    # ------------------------------------------------------------------
    if stripped.startswith("* ") and session.get("in_select"):
        session["select_lines"].append(stripped)
        return

    # Untagged lines that signal we're in a SELECT response
    _select_untagged = re.compile(
        r"^\* (?:\d+ (?:EXISTS|RECENT)|OK \[(?:UIDVALIDITY|UIDNEXT|UNSEEN|PERMANENTFLAGS|FLAGS))",
        re.IGNORECASE,
    )
    if _select_untagged.match(stripped):
        session["select_lines"].append(stripped)
        session["in_select"] = True
        return

    # Tagged OK ending a SELECT (e.g. "A001 OK [READ-WRITE] SELECT completed")
    _select_ok = re.compile(r"^[A-Za-z0-9]+ OK \[READ-(?:WRITE|ONLY)\]", re.IGNORECASE)
    if _select_ok.match(stripped) and session.get("in_select") and session.get("folder"):
        lines = session.pop("select_lines", [])
        session["select_lines"] = []
        session["in_select"] = False
        try:
            info = parse_select_response(lines)
            folder_id = await update_folder_stats(
                session["folder"],
                user_id=session.get("user_id"),
                exists_count=info.exists,
                recent_count=info.recent,
                uidvalidity=info.uidvalidity,
                uidnext=info.uidnext,
                unseen_count=info.unseen,
                db_path=db_path,
            )
            session["folder_id"] = folder_id
            logger.debug(
                "[%s] Synced folder %r: exists=%s uidvalidity=%s",
                peer, session["folder"], info.exists, info.uidvalidity,
            )
        except Exception as exc:
            logger.warning("[%s] Failed to sync SELECT stats: %s", peer, exc)
        return

    # ------------------------------------------------------------------
    # LIST handling
    # ------------------------------------------------------------------
    if re.match(r"^\* LIST\b", stripped, re.IGNORECASE):
        try:
            listed = parse_list_folders([stripped])
            if listed:
                # Capture server-declared SPECIAL-USE roles (RFC 6154) so
                # intent derivation classifies folders with full confidence
                # even without a provider profile (e.g. localized names).
                roles = {
                    f.name: role
                    for f in listed
                    if (role := role_from_list_attributes(f.attributes))
                }
                await upsert_folders_from_list(
                    [f.name for f in listed],
                    user_id=session.get("user_id"),
                    special_use=roles or None,
                    db_path=db_path,
                )
                # Keep the session's intent-derivation cache current.
                if roles:
                    cache = session.get("special_use")
                    if cache is None:
                        cache = {}
                        session["special_use"] = cache
                    cache.update({name.lower(): role for name, role in roles.items()})
                logger.debug(
                    "[%s] LIST: upserted folders %s (special-use: %s)",
                    peer, [f.name for f in listed], roles or "none",
                )
        except Exception as exc:
            logger.warning("[%s] Failed to sync LIST line: %s", peer, exc)
        return

    # ------------------------------------------------------------------
    # FETCH handling — only single-line FETCH with FLAGS/UID/ENVELOPE
    # ------------------------------------------------------------------
    if re.match(r"^\* \d+ FETCH\b", stripped, re.IGNORECASE):
        # Skip if it looks like a literal continuation {N}
        if re.search(r"\{\d+\}$", stripped):
            return
        folder_id = session.get("folder_id")
        if folder_id is None:
            return
        try:
            info = parse_fetch_line(stripped)
            if info is None or info.uid is None:
                return
            await upsert_message(
                folder_id,
                info.uid,
                seq_num=info.seq_num,
                flags=info.flags,
                subject=info.subject,
                sender=info.sender,
                size=info.size,
                message_id=info.message_id,
                db_path=db_path,
            )
            logger.debug(
                "[%s] FETCH synced uid=%s flags=%s", peer, info.uid, info.flags
            )
        except Exception as exc:
            logger.warning("[%s] Failed to sync FETCH line: %s", peer, exc)


async def _discard_nonsync_literals(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    raw: str,
    peer: str,
) -> bool:
    """Consume the non-synchronizing literal(s) a refused command carries.

    Wire shape (RFC 7888) — the client does not wait for ``+``:

        C: a1 REPLACE 4 INBOX {310+}\r\n     ← raw (already read)
        C: <310 bytes>                        ← discarded here, never forwarded
        C: \r\n  (or " more args {12+}\r\n" → loop)

    Returns False when the connection should end: the client went away, or a
    literal is larger than ``_MAX_APPEND_BYTES`` (refused with ``* BYE`` rather
    than read, so a hostile size can't keep the proxy busy).
    """
    line = raw
    while (size := nonsync_literal_size(line)) is not None:
        if size > _MAX_APPEND_BYTES:
            logger.warning(
                "[%s] REJECTED non-sync literal of %d bytes (cap %d) — closing",
                peer, size, _MAX_APPEND_BYTES,
            )
            with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
                client_writer.write(b"* BYE Nuvrail: literal too large\r\n")
                await client_writer.drain()
            return False
        try:
            remaining = size
            while remaining > 0:
                chunk = await client_reader.readexactly(min(_READ_CHUNK, remaining))
                remaining -= len(chunk)
            nxt = await client_reader.readline()
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            return False
        if not nxt:
            return False
        line = nxt.decode("utf-8", errors="replace").rstrip("\r\n")
    return True


async def _client_to_upstream(
    client_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    client_writer: asyncio.StreamWriter,
    session: dict,
    peer: str,
    db_path: Path = DB_PATH,
) -> None:
    """Parse each client line and route: read→upstream, write/blocked→intercept.

    AUTH/LOGIN is handled upstream in handle_client before this coroutine starts.
    All commands arriving here are post-authentication.

    Handles sync literals ({N} at end of line) by consuming the literal body
    from the client and responding with OK [STAGED] without forwarding.

    Fails closed: a verb classify() does not know is answered locally with
    ``NO [CANNOT]`` and never forwarded, and so is any command carrying a
    non-synchronizing literal ({N+}/{N-}).
    """
    while True:
        try:
            line_bytes = await client_reader.readline()
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            break
        if not line_bytes:
            break

        raw = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")

        # --- Parse line -------------------------------------------------------
        parsed = parse_line(raw)

        if parsed is None:
            # Sync literal: {N} at end of line — this is typically an APPEND command.
            # Consume the literal bytes, stage the operation, respond with OK [STAGED].
            parts = raw.split()
            tag = parts[0] if parts else "?"
            m = _LITERAL_RE.search(raw)
            literal_size = int(m.group(1)) if m else 0

            # Extract APPEND parameters from the raw command line before the literal
            # Format: tag APPEND folder (flags) {size}
            append_folder = parts[2] if len(parts) > 2 else "INBOX"
            # Flags are in a parenthesised token — find it
            import re as _re
            flags_match = _re.search(r"\(([^)]*)\)", raw)
            append_flags = flags_match.group(1).split() if flags_match else []

            literal_data = b""
            try:
                client_writer.write(b"+ Ready for literal data\r\n")
                await client_writer.drain()
                if 0 < literal_size <= _MAX_APPEND_BYTES:
                    literal_data = await client_reader.readexactly(literal_size)
                elif literal_size > _MAX_APPEND_BYTES:
                    # Too large to persist — must still consume exactly the
                    # literal off the wire to stay protocol-synced, but in
                    # bounded chunks so a huge APPEND can't exhaust memory.
                    remaining = literal_size
                    while remaining > 0:
                        chunk = await client_reader.readexactly(min(_READ_CHUNK, remaining))
                        remaining -= len(chunk)
                    logger.warning(
                        "[%s] APPEND body %d bytes exceeds cap %d — staging without "
                        "body (will not replay upstream)",
                        peer, literal_size, _MAX_APPEND_BYTES,
                    )
                await client_reader.readline()  # consume trailing CRLF after literal
            except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
                break

            try:
                _profile: ProviderProfile = session.get("provider_profile")
                if _profile and should_suppress_append(append_folder, _profile):
                    # Provider auto-adds to sent folder after SMTP send;
                    # suppress APPEND to avoid duplicates. Respond OK so the
                    # agent doesn't see an error (it doesn't need to know).
                    resp = f"{tag} OK APPEND completed\r\n"
                    logger.info(
                        "[%s] SUPPRESSED APPEND to %r (sent-dedup: %s provider)",
                        peer, append_folder, _profile.name,
                    )
                else:
                    parsed_op = parse_append(tag, append_folder, append_flags, literal_size)
                    _append_intent, _append_conf = derive_intent(
                        parsed_op, _profile,
                        special_use=await _load_special_use(session, db_path, peer),
                    )
                    # Persist the raw message (base64) so the APPEND can actually
                    # be replayed upstream on approval. Oversized bodies were
                    # already dropped (logged) during the literal read above.
                    import base64 as _b64
                    _append_b64: str | None = (
                        _b64.b64encode(literal_data).decode("ascii") if literal_data else None
                    )
                    _append_batch_id = await get_or_create_batch(
                        folder=append_folder, protocol="imap",
                        agent_id=session.get("agent_id"),
                    )
                    op_id = await create_operation(
                        op_type=parsed_op.op_type,
                        protocol="imap",
                        agent_id=session.get("agent_id"),
                        description=parsed_op.description,
                        imap_command=parsed_op.imap_command,
                        folder_to=parsed_op.folder_to,
                        flags_add=parsed_op.flags_add,
                        append_message=_append_b64,
                        batch_id=_append_batch_id,
                        intent_label=_append_intent,
                        intent_confidence=_append_conf,
                        db_path=db_path,
                    )
                    resp = f"{tag} OK [STAGED] Operation queued — ID: {op_id}\r\n"
            except Exception as exc:
                logger.error("[%s] Failed to stage APPEND: %s", peer, exc)
                resp = f"{tag} OK [STAGED] Operation queued for approval\r\n"

            try:
                client_writer.write(resp.encode())
                await client_writer.drain()
            except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
                break
            logger.info("[%s] STAGED (literal APPEND): %s", peer, raw)
            continue

        if nonsync_literal_size(raw) is not None:
            # Non-synchronizing literal (RFC 7888). We strip LITERAL+/LITERAL-
            # from the capabilities we relay, so a compliant client never sends
            # one. Consume the bytes to stay in sync, then refuse the command.
            if not await _discard_nonsync_literals(client_reader, client_writer, raw, peer):
                break
            try:
                client_writer.write(
                    f"{parsed.tag} NO [CANNOT] Nuvrail: non-synchronizing literals"
                    f" are not supported, use {{N}}\r\n".encode()
                )
                await client_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                break
            # Log the verb only: the line may carry credentials (LOGIN) or data.
            logger.warning("[%s] REJECTED (non-sync literal): %s %s", peer, parsed.tag, parsed.command)
            continue

        action = classify(parsed)

        if action == "rejected":
            # Fail closed: never forward a verb we haven't classified. The
            # agent gets a definite NO instead of an unapproved upstream write.
            if not raw.strip():
                continue  # blank line: nothing to answer
            if parsed.command:
                resp = (
                    f"{parsed.tag} NO [CANNOT] Nuvrail: {parsed.command} is not"
                    f" permitted for agents\r\n"
                )
            else:
                resp = f"{parsed.tag} BAD Nuvrail: missing command\r\n"
            try:
                client_writer.write(resp.encode())
                await client_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                break
            # Log the verb only: a post-auth LOGIN/AUTHENTICATE carries credentials.
            logger.warning(
                "[%s] REJECTED (unclassified verb): %s %s", peer, parsed.tag, parsed.command
            )
            continue

        if action == "read":
            # Track SELECT so the u2c pump knows which folder we're in.
            if parsed.command.upper() in ("SELECT", "EXAMINE"):
                folder_name = parsed.args[0].strip('"') if parsed.args else None
                if folder_name:
                    session["folder"] = folder_name
                    session["folder_id"] = None
                    session["select_lines"] = []
                    session["in_select"] = False

            # Track which commands should trigger pending_reverts injection.
            # The u2c pump will call _inject_pending_reverts when it sees
            # the tagged OK for one of these commands.
            if parsed.command.upper() in ("SELECT", "EXAMINE", "NOOP", "FETCH"):
                session["revert_trigger_tag"] = parsed.tag.upper()

            # Filter the CAPABILITY response the u2c pump relays (see
            # filter_capabilities) — only while this command is outstanding.
            if parsed.command.upper() == "CAPABILITY":
                session["capability_tag"] = parsed.tag.upper()

            # Track whether the current SEARCH is UID-mode so the u2c pump
            # can filter pending-move UIDs from the SEARCH response.
            if parsed.command.upper() == "SEARCH":
                session["search_uid_mode"] = parsed.uid

            # Pass through to upstream; the u2c pump returns the response.
            try:
                upstream_writer.write(line_bytes)
                await upstream_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                break

        elif action == "write":
            # -----------------------------------------------------------------
            # Provider normalization: COPY+STORE(\Deleted) → UID MOVE
            #
            # When a provider profile is active, we track COPY commands to
            # known archive/trash folders and hold them instead of staging
            # immediately.  When STORE \Deleted follows for the same UIDs,
            # we rewrite the pair as a single UID MOVE operation (cleaner
            # for approval, correct for providers like Gmail that require MOVE).
            #
            # Flush the held COPY as a normal staged operation if any
            # non-matching write command arrives before the STORE \Deleted.
            # -----------------------------------------------------------------
            _profile: ProviderProfile = session.get("provider_profile")
            _pending: PendingCopyIntent | None = session.get("pending_copy_intent")
            _is_store_deleted = (
                parsed.command.upper() == "STORE"
                and len(parsed.args) >= 3
                and "deleted" in parsed.args[2].lower()
                and parsed.args[1].upper().replace(".SILENT", "") in ("+FLAGS", "+FLAGS.SILENT")
            )
            # Reconstruct folder name for COPY: agents may send unquoted folder
            # names with spaces (e.g. UID COPY 23 [Gmail]/All Mail) which the
            # tokenizer splits across multiple args. Join everything after args[0].
            _copy_folder = " ".join(parsed.args[1:]) if len(parsed.args) > 1 else ""
            _is_archive_copy = (
                parsed.command.upper() == "COPY"
                and parsed.uid  # UID COPY only — non-UID COPY rejected below
                and bool(_copy_folder)
                and (
                    # With profile: only hold known archive/trash destinations.
                    # Without profile: hold ALL UID COPY — if STORE \Deleted
                    # follows for the same UIDs we rewrite to MOVE; otherwise
                    # the held COPY is flushed as a normal staged COPY op.
                    # This covers naive agents that use COPY+STORE+EXPUNGE
                    # without a configured provider profile.
                    _profile is None
                    or copy_archive_intent(_copy_folder, _profile) is not None
                )
            )

            # If we're holding a COPY intent and the next write is NOT a
            # matching STORE \Deleted, flush the held COPY as staged before
            # processing the new command.
            if _pending is not None and not _is_store_deleted:
                try:
                    _flush_op = parse_copy(_pending.tag, _pending.uid_set, _pending.destination)
                    _flush_batch_id = await get_or_create_batch(
                        folder=session.get("folder") or "INBOX", protocol="imap",
                        agent_id=session.get("agent_id"),
                    )
                    _flush_op_id = await create_operation(
                        op_type=_flush_op.op_type,
                        protocol="imap",
                        agent_id=session.get("agent_id"),
                        description=_flush_op.description,
                        imap_command=_flush_op.imap_command,
                        message_ids=[_pending.uid_set],
                        folder_from=session.get("folder"),
                        folder_to=_flush_op.folder_to,
                        batch_id=_flush_batch_id,
                        db_path=db_path,
                    )
                    logger.info(
                        "[%s] FLUSHED held COPY to %r as staged op %s",
                        peer, _pending.destination, _flush_op_id,
                    )
                except Exception as _flush_exc:
                    logger.warning("[%s] Failed to flush held COPY (non-fatal): %s", peer, _flush_exc)
                finally:
                    session["pending_copy_intent"] = None

            # Hold archive-intent COPY; respond with synthetic OK (not STAGED).
            if _is_archive_copy:
                _copy_uid_set = parsed.args[0] if parsed.args else "?"
                _copy_dest = _copy_folder  # already reconstructed above (may contain spaces)
                session["pending_copy_intent"] = PendingCopyIntent(
                    tag=parsed.tag,
                    uid_set=_copy_uid_set,
                    destination=_copy_dest,
                )
                logger.info(
                    "[%s] HELD COPY to %r — waiting for STORE \\Deleted to rewrite as MOVE (%s)",
                    peer, _copy_dest, _profile.name,
                )
                try:
                    client_writer.write(f"{parsed.tag} OK COPY completed\r\n".encode())
                    await client_writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break
                continue  # don't stage yet

            # Reject non-UID write commands.
            # Sequence numbers are positional and shift whenever messages are
            # added/removed from a folder. Staged operations may be held
            # pending approval for up to 48 hours — a sequence number recorded
            # at staging time can point to a completely different message by
            # execution time. UID addressing is stable across sessions and
            # mailbox changes. All write commands must use UID prefix.
            if not parsed.uid and parsed.command.upper() in ("STORE", "COPY", "MOVE"):
                try:
                    client_writer.write(
                        f"{parsed.tag} NO [CLIENTBUG] Use UID {parsed.command} not "
                        f"{parsed.command} — sequence numbers shift when messages move "
                        f"and are unsafe for staged approval.\r\n".encode()
                    )
                    await client_writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break
                logger.warning("[%s] Rejected non-UID write: %s", peer, raw)
                continue

            # Intercept — don't forward, stage the operation, respond locally.
            try:
                # Provider normalization: COPY+STORE(\Deleted) → UID MOVE
                # If STORE \Deleted matches a held COPY intent, rewrite the pair
                # as a single UID MOVE to the COPY destination and clear the intent.
                _pending_now: PendingCopyIntent | None = session.get("pending_copy_intent")
                if (
                    _pending_now is not None
                    and _is_store_deleted
                    and parsed.args
                    and parsed.args[0] == _pending_now.uid_set
                ):
                    if _profile and _pending_now.destination == _profile.archive_folder:
                        _folder_label = "archive"
                    elif _profile and _pending_now.destination == _profile.junk_folder:
                        _folder_label = "spam"
                    else:
                        _folder_label = "delete"
                    # The human-facing description is rebuilt below via
                    # derive_intent + build_rich_description — the MOVE
                    # destination classifies as archive/delete/spam intent.
                    parsed_op = parse_move(
                        parsed.tag, _pending_now.uid_set, _pending_now.destination
                    )
                    session["pending_copy_intent"] = None
                    logger.info(
                        "[%s] REWRITE %s: COPY+STORE(\\Deleted) → UID MOVE %r for %s",
                        peer, _folder_label, _pending_now.destination,
                        (_profile.name if _profile else "?"),
                    )
                else:
                    parsed_op = _build_parsed_op(parsed)
                if parsed_op is not None:
                    # For flag operations (STORE), take a pre-op snapshot of
                    # affected messages and apply the optimistic update so the
                    # AI sees the proposed state immediately. The snapshot is
                    # stored in staged_operations for later revert on rejection.
                    op_snapshot: dict | None = None
                    folder_id = session.get("folder_id")
                    is_flag_op = parsed.command.upper() in ("STORE",) and (
                        parsed_op.flags_add or parsed_op.flags_remove
                    )
                    # If folder_id isn't cached yet (SELECT response still in flight),
                    # look it up synchronously so we can still take a snapshot.
                    if is_flag_op and folder_id is None and session.get("folder"):
                        try:
                            from gateway.state_db import get_or_create_folder
                            folder_id = await get_or_create_folder(
                                session["folder"],
                                user_id=session.get("user_id"),
                                db_path=db_path,
                            )
                            session["folder_id"] = folder_id
                        except Exception:  # noqa: S110 — best-effort folder-cache warm; op still staged
                            pass
                    if is_flag_op and folder_id is not None and parsed_op.message_ids:
                        uid_set_str = parsed_op.message_ids[0] if len(parsed_op.message_ids) == 1 else ",".join(parsed_op.message_ids)
                        try:
                            op_snapshot = await snapshot_messages(
                                folder_id, uid_set_str, db_path=db_path
                            )
                            # If the message isn't in the state DB yet (not yet FETCH'd
                            # through the proxy), synthesize a minimal snapshot using the
                            # known message_ids. For +FLAGS ops, pre-op state is "no flags"
                            # for unknown messages — this ensures revert can still fire.
                            if not op_snapshot:
                                for uid_str in parsed_op.message_ids:
                                    op_snapshot[uid_str] = {
                                        "flags": [],
                                        "seq_num": None,
                                        "folder_id": folder_id,
                                    }
                                logger.debug(
                                    "[%s] Messages not in state DB — synthesised snapshot for %s UIDs",
                                    peer, len(op_snapshot),
                                )
                            await apply_optimistic_flag_update(
                                folder_id,
                                uid_set_str,
                                flags_add=parsed_op.flags_add or [],
                                flags_remove=parsed_op.flags_remove or [],
                                db_path=db_path,
                            )
                            logger.debug(
                                "[%s] Snapshot captured + optimistic update applied for %s UIDs",
                                peer, len(op_snapshot),
                            )
                        except Exception as snap_exc:
                            logger.warning(
                                "[%s] Snapshot/optimistic update failed (non-fatal): %s",
                                peer, snap_exc,
                            )
                            op_snapshot = None

                    # Derive the semantic intent (archive/delete/mark_spam/...)
                    # from the op's folders + flags under the active provider
                    # profile, then enrich the description with sender/subject
                    # from the state DB. Must happen BEFORE the MOVE optimistic
                    # update below, which deletes message rows from the state
                    # DB — after that point the metadata is gone. Non-fatal if
                    # lookup fails.
                    # An approved trash op executes as UID MOVE into the Trash
                    # folder (#168). Resolve it now: it is recorded as
                    # folder_to (the executor's target, undo's source) and it
                    # decides the label — "Move to Trash" vs "Mark deleted (no
                    # Trash folder found)" — so the human approves what runs.
                    if parsed_op.op_type == "trash" and not parsed_op.folder_to:
                        parsed_op.folder_to = await _resolve_trash_folder(
                            session, db_path, _profile, peer
                        )

                    op_intent, op_intent_conf = derive_intent(
                        parsed_op, _profile,
                        folder_from=parsed_op.folder_from or session.get("folder"),
                        special_use=await _load_special_use(session, db_path, peer),
                    )
                    op_description = build_rich_description(parsed_op, [], op_intent)
                    if folder_id is not None and parsed_op.message_ids:
                        uid_set_for_meta = parsed_op.message_ids[0]
                        try:
                            msg_meta = await get_message_metadata_by_uid_set(
                                folder_id, uid_set_for_meta, db_path=db_path
                            )
                            op_description = build_rich_description(
                                parsed_op, msg_meta, op_intent
                            )
                        except Exception as meta_exc:
                            logger.debug(
                                "[%s] Metadata lookup for rich description failed (non-fatal): %s",
                                peer, meta_exc,
                            )

                    # MOVE optimistic update: remove message from source folder
                    # in local state DB so the agent sees a consistent view
                    # (message appears moved immediately, before human approves).
                    # The snapshot captured above enables full rollback on rejection.
                    # Check op_type (not parsed.command) so the COPY+STORE→MOVE
                    # rewrite path is also covered: in that case parsed.command
                    # is still "STORE" but parsed_op.op_type is "move".
                    is_move_op = parsed_op.op_type == "move" and parsed_op.message_ids
                    if is_move_op and folder_id is not None and parsed_op.message_ids:
                        uid_set_str = parsed_op.message_ids[0]
                        try:
                            # Snapshot first (if not already taken by flag-op path)
                            if op_snapshot is None:
                                op_snapshot = await snapshot_messages(
                                    folder_id, uid_set_str, db_path=db_path
                                )
                            await remove_messages_from_folder(
                                folder_id, uid_set_str, db_path=db_path
                            )
                            logger.debug(
                                "[%s] MOVE staged: removed UIDs %s from local state (folder_id=%s)",
                                peer, uid_set_str, folder_id,
                            )
                        except Exception as move_exc:
                            logger.warning(
                                "[%s] MOVE optimistic update failed (non-fatal): %s",
                                peer, move_exc,
                            )

                    _write_folder = parsed_op.folder_from or session.get("folder") or "INBOX"
                    _write_batch_id = await get_or_create_batch(
                        folder=_write_folder, protocol="imap",
                        agent_id=session.get("agent_id"),
                    )
                    op_id = await create_operation(
                        op_type=parsed_op.op_type,
                        protocol="imap",
                        agent_id=session.get("agent_id"),
                        description=op_description,
                        imap_command=parsed_op.imap_command,
                        message_ids=parsed_op.message_ids if parsed_op.message_ids else None,
                        # folder_from: use parsed value if set (COPY/MOVE
                        # parse_move doesn't know the current mailbox), fall
                        # back to the session's selected folder.
                        folder_from=parsed_op.folder_from or session.get("folder"),
                        folder_to=parsed_op.folder_to,
                        flags_add=parsed_op.flags_add if parsed_op.flags_add else None,
                        flags_remove=parsed_op.flags_remove if parsed_op.flags_remove else None,
                        snapshot=op_snapshot,
                        batch_id=_write_batch_id,
                        intent_label=op_intent,
                        intent_confidence=op_intent_conf,
                        db_path=db_path,
                    )
                    resp = f"{parsed.tag} OK [STAGED] Operation queued — ID: {op_id}\r\n"
                else:
                    # CREATE / RENAME or unknown write — stage generically (no snapshot)
                    op_id = await create_operation(
                        op_type=parsed.command.lower(),
                        protocol="imap",
                        agent_id=session.get("agent_id"),
                        description=f"{parsed.command} {' '.join(parsed.args)}".strip(),
                        imap_command=raw,
                        db_path=db_path,
                    )
                    resp = f"{parsed.tag} OK [STAGED] Operation queued — ID: {op_id}\r\n"
            except Exception as exc:
                logger.error("[%s] Failed to stage write command: %s", peer, exc)
                resp = f"{parsed.tag} OK [STAGED] Operation queued for approval\r\n"

            try:
                client_writer.write(resp.encode())
                await client_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                break
            logger.info("[%s] STAGED: %s", peer, raw)

        else:  # blocked (classify() returns exactly read/write/blocked/rejected)
            resp = f"{parsed.tag} OK Noted\r\n"
            try:
                client_writer.write(resp.encode())
                await client_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                break
            logger.info("[%s] BLOCKED: %s", peer, raw)


_REVERT_TRIGGER_OK_RE = re.compile(r"^([A-Za-z0-9]+)\s+OK\b", re.IGNORECASE)


async def _upstream_to_client(
    upstream_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    session: dict,
    db_path: Path,
    peer: str,
) -> None:
    """Forward upstream bytes to client, tapping each line for state sync.

    Data flow for most lines:
      line arrives → forwarded to client immediately → _sync_upstream_line (best-effort)

    Data flow for tagged OK on revert-trigger commands (SELECT/NOOP/FETCH):
      line arrives → _inject_pending_reverts (inject unsolicited FETCH lines first)
                   → tagged OK forwarded to client
                   → _sync_upstream_line for state sync

    This ensures injected unsolicited FETCH lines arrive BEFORE the tagged OK that
    the AI is waiting on. The AI sees: [* N FETCH ...][tag OK] — standard IMAP
    unsolicited response ordering, no extensions required.

    The line-buffering delay is minimal (~microseconds per line) because upstream
    IMAP responses are line-terminated and we read in 4KB chunks.
    """
    buffer = b""
    while True:
        try:
            chunk = await upstream_reader.read(_READ_CHUNK)
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            break
        if not chunk:
            break

        buffer += chunk

        while b"\r\n" in buffer:
            line_bytes, buffer = buffer.split(b"\r\n", 1)
            line = line_bytes.decode("utf-8", errors="replace")
            line_with_crlf = line_bytes + b"\r\n"

            # Strip capabilities the agent must not use (LITERAL+, COMPRESS, ...)
            # from the response to the agent's own CAPABILITY command. Gated on
            # the outstanding tag so a message body line is never rewritten.
            cap_tag = session.get("capability_tag")
            if cap_tag:
                if line.upper().startswith("* CAPABILITY "):
                    line = filter_capabilities(line)
                    line_with_crlf = line.encode() + b"\r\n"
                elif line.split(" ", 1)[0].upper() == cap_tag:
                    session["capability_tag"] = None

            # Check if this is a tagged OK for a revert-trigger command.
            # If so, inject pending reverts BEFORE forwarding the tagged OK,
            # so the AI receives: [* N FETCH (UID M FLAGS (...))] then [tag OK].
            revert_tag = session.get("revert_trigger_tag")
            if revert_tag:
                m = _REVERT_TRIGGER_OK_RE.match(line)
                if m and m.group(1).upper() == revert_tag:
                    session["revert_trigger_tag"] = None
                    await _inject_pending_reverts(client_writer, session, db_path, peer)

            # Adjust * N EXISTS count: subtract pending-move messages so the
            # agent sees the mailbox size consistent with its staged operations.
            _exists_m = re.match(r"^\* (\d+) EXISTS$", line, re.IGNORECASE)
            if _exists_m and session.get("folder"):
                try:
                    _pending = await get_pending_move_uids_for_folder(
                        session["folder"],
                        agent_id=session.get("agent_id"),
                        db_path=db_path,
                    )
                    if _pending:
                        _n = int(_exists_m.group(1))
                        _adjusted = max(0, _n - len(_pending))
                        if _adjusted != _n:
                            line = f"* {_adjusted} EXISTS"
                            line_with_crlf = line.encode() + b"\r\n"
                            logger.debug(
                                "[%s] EXISTS adjusted %d → %d (%d pending moves)",
                                peer, _n, _adjusted, len(_pending),
                            )
                except Exception as exc:
                    logger.warning("[%s] EXISTS adjustment failed (non-fatal): %s", peer, exc)

            # Filter * SEARCH results: strip UIDs that are pending MOVE from
            # the current folder. Only applied when the agent used UID SEARCH.
            elif re.match(r"^\* SEARCH\b", line, re.IGNORECASE) and session.get("search_uid_mode") and session.get("folder"):
                try:
                    _pending = await get_pending_move_uids_for_folder(
                        session["folder"],
                        agent_id=session.get("agent_id"),
                        db_path=db_path,
                    )
                    if _pending:
                        _parts = line.split()
                        _keyword = _parts[:2]  # ["*", "SEARCH"]
                        _uid_strs = _parts[2:]
                        _filtered = [
                            u for u in _uid_strs
                            if not (u.isdigit() and int(u) in _pending)
                        ]
                        line = " ".join(_keyword + _filtered)
                        line_with_crlf = line.encode() + b"\r\n"
                        logger.debug(
                            "[%s] SEARCH filtered %d pending-move UIDs from results",
                            peer, len(_uid_strs) - len(_filtered),
                        )
                except Exception as exc:
                    logger.warning("[%s] SEARCH filter failed (non-fatal): %s", peer, exc)

            # RFC822 literal FETCH: "* N FETCH (UID M RFC822 {size})"
            # The full message body follows as a literal block. We consume it
            # from the stream here, extract From/Subject headers, and forward
            # everything to the client unchanged. This is the only point where
            # we see message metadata for agents that fetch RFC822 directly
            # rather than requesting ENVELOPE.
            #
            # Flow:
            #   header line  ──► forward to client
            #                ──► consume literal bytes from stream
            #                ──► extract headers from first _HEADER_READ_LIMIT bytes
            #                ──► forward remaining bytes to client
            #                ──► upsert sender/subject into state DB
            #
            # Non-fatal: if anything fails, bytes are still forwarded correctly.
            _lit_m = _RE_FETCH_LITERAL.match(line)
            if _lit_m:
                _lit_uid = int(_lit_m.group(2))
                _lit_size = int(_lit_m.group(3))
                folder_id = session.get("folder_id")
                try:
                    # Forward the header line first.
                    client_writer.write(line_with_crlf)
                    await client_writer.drain()

                    # Read the full literal from stream (already partially in buffer).
                    _lit_remaining = _lit_size
                    _header_buf = b""
                    _header_done = False

                    # Drain whatever is already in buffer before reading more.
                    while _lit_remaining > 0:
                        if buffer:
                            _take = min(len(buffer), _lit_remaining)
                            _chunk = buffer[:_take]
                            buffer = buffer[_take:]
                        else:
                            _to_read = min(_READ_CHUNK, _lit_remaining)
                            _chunk = await upstream_reader.read(_to_read)
                            if not _chunk:
                                break

                        # Accumulate prefix for header extraction.
                        if not _header_done:
                            _header_buf += _chunk
                            if len(_header_buf) >= _HEADER_READ_LIMIT or _lit_remaining - len(_chunk) <= 0:
                                _header_done = True

                        # Forward bytes to client immediately.
                        client_writer.write(_chunk)
                        await client_writer.drain()
                        _lit_remaining -= len(_chunk)

                    # Extract and store headers (non-fatal).
                    if folder_id is not None and _header_buf:
                        try:
                            _sender, _subject, _message_id = extract_headers_from_rfc822(
                                _header_buf
                            )
                            if _sender or _subject or _message_id:
                                await upsert_message(
                                    folder_id,
                                    _lit_uid,
                                    sender=_sender,
                                    subject=_subject,
                                    message_id=_message_id,
                                    db_path=db_path,
                                )
                                logger.debug(
                                    "[%s] RFC822 literal: stored sender=%r subject=%r for uid=%s",
                                    peer, _sender, _subject, _lit_uid,
                                )
                        except Exception as _hdr_exc:
                            logger.debug(
                                "[%s] RFC822 header extraction failed (non-fatal): %s",
                                peer, _hdr_exc,
                            )
                except (ConnectionResetError, BrokenPipeError, OSError):
                    return
                except Exception as _lit_exc:
                    logger.warning("[%s] RFC822 literal handling error (non-fatal): %s", peer, _lit_exc)
                # Skip normal forward + sync for this line — already forwarded above.
                continue

            # Suppress FETCH lines for UIDs that are pending MOVE from the
            # current folder. This keeps the agent's view consistent with its
            # staged operations — the message appears already moved without
            # waiting for human approval.
            if re.match(r"^\* \d+ FETCH\b", line, re.IGNORECASE):
                folder_name = session.get("folder")
                if folder_name:
                    try:
                        pending_move_uids = await get_pending_move_uids_for_folder(
                            folder_name,
                            agent_id=session.get("agent_id"),
                            db_path=db_path,
                        )
                        if pending_move_uids:
                            fetch_info = parse_fetch_line(line)
                            if fetch_info and fetch_info.uid in pending_move_uids:
                                logger.debug(
                                    "[%s] Suppressing FETCH for pending-move UID %s",
                                    peer, fetch_info.uid,
                                )
                                # Still sync state but don't forward to client
                                with contextlib.suppress(Exception):
                                    await _sync_upstream_line(line, session, db_path, peer)
                                continue
                    except Exception as exc:
                        logger.warning("[%s] Pending-move filter error (non-fatal): %s", peer, exc)

            # Patch FLAGS in FETCH responses for pending STORE ops.
            #
            # If the agent staged a flag change (e.g. STORE +FLAGS \Deleted)
            # the upstream server hasn't executed it yet, so its FETCH response
            # reflects the pre-staged state.  We rewrite the FLAGS field inline
            # so the agent sees its own staged change immediately — consistent
            # with how pending-MOVE UIDs are suppressed entirely.
            #
            # Flow:
            #   upstream line  ──► parse UID + FLAGS
            #              ──► get_pending_flag_changes_for_uid
            #              ──► merge flags  ──► rewrite FLAGS(…) in line
            #              ──► forward patched line to agent
            if re.match(r"^\* \d+ FETCH\b", line, re.IGNORECASE):
                folder_name = session.get("folder")
                if folder_name:
                    try:
                        fetch_info = parse_fetch_line(line)
                        if fetch_info and fetch_info.uid is not None:
                            f_add, f_rem = await get_pending_flag_changes_for_uid(
                                folder_name,
                                fetch_info.uid,
                                agent_id=session.get("agent_id"),
                                db_path=db_path,
                            )
                            if f_add or f_rem:
                                # Merge: start from upstream flags, apply staged delta.
                                current = set(fetch_info.flags or [])
                                current.update(f_add)
                                current.difference_update(f_rem)
                                new_flags_str = " ".join(sorted(current))
                                patched = re.sub(
                                    r"\bFLAGS \([^)]*\)",
                                    f"FLAGS ({new_flags_str})",
                                    line,
                                    count=1,
                                    flags=re.IGNORECASE,
                                )
                                if patched != line:
                                    line = patched
                                    line_with_crlf = line.encode() + b"\r\n"
                                    logger.debug(
                                        "[%s] Patched FLAGS for pending STORE UID %s: %s",
                                        peer, fetch_info.uid, new_flags_str,
                                    )
                    except Exception as exc:
                        logger.warning("[%s] FETCH flag-patch error (non-fatal): %s", peer, exc)

            # Forward this line to the client.
            try:
                client_writer.write(line_with_crlf)
                await client_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                return

            # State sync (best-effort, errors never propagate).
            try:
                await _sync_upstream_line(line, session, db_path, peer)
            except Exception as exc:
                logger.warning("[%s] State sync error: %s", peer, exc)

        # If there are buffered bytes that don't form a complete line yet
        # (partial line from upstream), forward them immediately so the client
        # doesn't stall. These partial bytes will be re-buffered on the next chunk.
        if buffer:
            try:
                client_writer.write(buffer)
                await client_writer.drain()
                buffer = b""
            except (ConnectionResetError, BrokenPipeError, OSError):
                break


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    """
    Handle one client connection.

    Auth-before-upstream flow:

      Client connects
          │
          ▼
      Proxy sends synthetic greeting (no upstream yet)
          │
          ▼
      Wait for LOGIN command
          │  verify agent_username + token against agent_credentials
          ├─ fail → BYE + close
          │
          ▼
      Open SSL connection to upstream using credential.upstream_host/port
          │
          ▼
      Rewrite LOGIN with upstream_user + upstream_password → forward
          │
          ▼
      Bidirectional pump (c2u + u2c tasks)

    This means the upstream host/port comes from the per-agent credential row,
    not from a static env var — each agent can point to a different IMAP server.
    """
    peer = client_writer.get_extra_info("peername", ("?", 0))
    peer_str = f"{peer[0]}:{peer[1]}"
    logger.info("[%s] Client connected", peer_str)

    import gateway.state_db as _state_db_mod
    _db_path = _state_db_mod.DB_PATH

    # Safety net: init_db is idempotent.
    try:
        await init_db(_db_path)
    except Exception as exc:
        logger.warning("[%s] init_db safety call failed: %s", peer_str, exc)

    # --- Step 1: Send synthetic greeting to client (no upstream yet) ----------
    try:
        client_writer.write(b"* OK Nuvrail IMAP proxy ready\r\n")
        await client_writer.drain()
    except OSError as exc:
        logger.error("[%s] Failed to send greeting: %s", peer_str, exc)
        client_writer.close()
        return

    # --- Step 2: Wait for LOGIN and verify agent credentials ------------------
    credential: dict | None = None
    login_tag: str = "*"
    login_line_bytes: bytes = b""

    while credential is None:
        try:
            line_bytes = await client_reader.readline()
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            client_writer.close()
            return
        if not line_bytes:
            client_writer.close()
            return

        raw = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
        parts = raw.split()
        if not parts:
            continue

        cmd = parts[1].upper() if len(parts) > 1 else ""

        if cmd == "CAPABILITY":
            # Some clients issue CAPABILITY before LOGIN; respond locally.
            tag = parts[0]
            try:
                client_writer.write(
                    f"* CAPABILITY IMAP4rev1 AUTH=PLAIN AUTH=LOGIN\r\n"
                    f"{tag} OK CAPABILITY completed\r\n".encode()
                )
                await client_writer.drain()
            except OSError:
                client_writer.close()
                return
            continue

        if cmd == "LOGOUT":
            tag = parts[0]
            try:
                client_writer.write(
                    f"* BYE Logging out\r\n"
                    f"{tag} OK LOGOUT completed\r\n".encode()
                )
                await client_writer.drain()
            except OSError:
                pass
            client_writer.close()
            return

        if cmd not in ("LOGIN", "AUTHENTICATE"):
            # Reject any other command before authentication.
            # [CLIENTBUG] per RFC 5530: the client issued a command that is
            # not permitted in the non-authenticated state.
            tag = parts[0]
            try:
                client_writer.write(
                    f"{tag} NO [CLIENTBUG] {cmd} is not permitted before"
                    f" authentication — send: LOGIN <agent_user> <agent_token>\r\n".encode()
                )
                await client_writer.drain()
            except OSError:
                client_writer.close()
                return
            logger.warning(
                "[%s] Pre-auth command rejected: cmd=%s", peer_str, cmd
            )
            continue

        # LOGIN command: tag LOGIN userid password
        # AUTHENTICATE PLAIN command: tag AUTHENTICATE PLAIN [base64]
        # Clients may quote LOGIN arguments. Strip surrounding double-quotes.
        login_tag = parts[0]

        if cmd == "AUTHENTICATE":
            mech = parts[2].upper() if len(parts) > 2 else ""
            if mech != "PLAIN":
                try:
                    client_writer.write(
                        f"{login_tag} NO Unsupported auth mechanism — use LOGIN or AUTHENTICATE PLAIN\r\n".encode()
                    )
                    await client_writer.drain()
                except OSError:
                    client_writer.close()
                    return
                continue

            # AUTHENTICATE PLAIN may have inline base64 or use a challenge
            if len(parts) > 3:
                b64_payload = parts[3]
            else:
                # Send challenge, wait for client response
                try:
                    client_writer.write(b"+ \r\n")
                    await client_writer.drain()
                    resp_line = await client_reader.readline()
                    b64_payload = resp_line.decode("utf-8", errors="replace").strip()
                except (OSError, asyncio.IncompleteReadError):
                    client_writer.close()
                    return

            agent_user, agent_pass = decode_sasl_plain(b64_payload) or ("", "")

        else:
            # LOGIN command
            agent_user = parts[2].strip('"') if len(parts) > 2 else ""
            # Password may contain spaces if quoted — rejoin and strip quotes
            agent_pass = " ".join(parts[3:]).strip('"') if len(parts) > 3 else ""

        decision = await IMAP_AUTH_ABUSE_PROTECTOR.start_attempt(ip=str(peer[0]), account=agent_user or "<unknown>")
        if not decision.allowed:
            try:
                client_writer.write(
                    f"* BYE Too many authentication attempts. Retry in {decision.retry_after_seconds}s\r\n"
                    f"{login_tag} NO [LIMIT] Too many authentication attempts"
                    f" — retry in {decision.retry_after_seconds}s\r\n".encode()
                )
                await client_writer.drain()
            except OSError:
                pass
            logger.warning(
                "[%s] Agent auth blocked user=%s reason=%s retry_after=%ss",
                peer_str,
                agent_user,
                decision.reason,
                decision.retry_after_seconds,
            )
            client_writer.close()
            return

        credential = await verify_agent_login(agent_user, agent_pass, _db_path)
        if credential is None:
            failure = await IMAP_AUTH_ABUSE_PROTECTOR.record_failure(
                ip=str(peer[0]), account=agent_user or "<unknown>"
            )
            try:
                client_writer.write(
                    f"* BYE Authentication failed\r\n"
                    f"{login_tag} NO [AUTHENTICATIONFAILED] Authentication credentials invalid\r\n".encode()
                )
                await client_writer.drain()
            except OSError:
                pass
            if failure.lockout_applied:
                logger.warning(
                    "[%s] IMAP auth lockout triggered user=%s retry_after=%ss",
                    peer_str,
                    agent_user,
                    failure.retry_after_seconds,
                )
            logger.warning("[%s] Agent auth failed for user=%s", peer_str, agent_user)
            client_writer.close()
            return

        await IMAP_AUTH_ABUSE_PROTECTOR.record_success(
            ip=str(peer[0]), account=agent_user or "<unknown>"
        )
        logger.info(
            "[%s] Agent authenticated: %s → upstream %s:%d user=%s",
            peer_str, agent_user,
            credential["upstream_host"], credential["upstream_imap_port"],
            credential["upstream_user"],
        )

        # Build upstream auth command: XOAUTH2 if oauth2_provider is set,
        # otherwise fall back to plain LOGIN.
        upstream_user = credential["upstream_user"]
        if credential.get("oauth2_provider"):
            login_line_bytes = None  # determined after upstream connection (needs DB path)
            _use_xoauth2 = True
        else:
            upstream_password = await fetch_credential(credential["upstream_password"])
            login_line_bytes = (
                f"{login_tag} LOGIN {upstream_user} {upstream_password}\r\n".encode()
            )
            _use_xoauth2 = False

    # --- Step 3: Open upstream connection using per-agent host/port -----------
    upstream_host = credential["upstream_host"]
    upstream_port = int(credential["upstream_imap_port"])
    ssl_ctx = ssl.create_default_context()
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            upstream_host, upstream_port, ssl=ssl_ctx,
        )
    except Exception as exc:
        logger.error(
            "[%s] Failed to connect to upstream %s:%d — %s",
            peer_str, upstream_host, upstream_port, exc,
        )
        try:
            client_writer.write(
                f"{login_tag} NO [UNAVAILABLE] Upstream IMAP server temporarily"
                f" unreachable ({upstream_host}:{upstream_port}) — try again later\r\n".encode()
            )
            await client_writer.drain()
        except OSError:
            pass
        client_writer.close()
        return

    # --- Step 4: Consume upstream greeting (not forwarded — client already got ours)
    try:
        await upstream_reader.readline()
    except (OSError, asyncio.IncompleteReadError) as exc:
        logger.error("[%s] Failed to read upstream greeting: %s", peer_str, exc)
        client_writer.close()
        upstream_writer.close()
        return

    # --- Step 5: Authenticate to upstream (LOGIN or XOAUTH2) ----------------
    if _use_xoauth2:
        from gateway.oauth2_tokens import OAuth2Error, get_xoauth2_string
        try:
            xoauth2_str = await get_xoauth2_string(str(credential["id"]), _db_path)
        except OAuth2Error as exc:
            logger.error("[%s] XOAUTH2 token fetch failed: %s", peer_str, exc)
            try:
                client_writer.write(
                    f"{login_tag} NO [AUTHENTICATIONFAILED] Upstream OAuth2 token"
                    f" is invalid or expired — re-authorize the agent\r\n".encode()
                )
                await client_writer.drain()
            except OSError:
                pass
            client_writer.close()
            upstream_writer.close()
            return
        auth_bytes = (
            f"{login_tag} AUTHENTICATE XOAUTH2 {xoauth2_str}\r\n".encode()
        )
    else:
        auth_bytes = login_line_bytes

    try:
        upstream_writer.write(auth_bytes)
        await upstream_writer.drain()
    except (OSError, BrokenPipeError) as exc:
        logger.error("[%s] Failed to send upstream auth: %s", peer_str, exc)
        client_writer.close()
        upstream_writer.close()
        return

    # Read the upstream auth response and forward to client.
    #
    # XOAUTH2 SASL exchange on failure (RFC 4422 / Gmail-specific):
    #
    #   C: TAG AUTHENTICATE XOAUTH2 <b64token>
    #   S: + <b64 JSON error>          ← SASL challenge (status/scope only, safe to log)
    #   C: \r\n                         ← empty abort; REQUIRED to complete the exchange
    #   S: TAG NO [AUTHENTICATIONFAILED] ...
    #
    # On success Gmail sends untagged lines BEFORE the tagged OK:
    #
    #   S: * CAPABILITY IMAP4rev1 UNSELECT IDLE ...\r\n  ← untagged (forward to client)
    #   S: TAG OK user@example.com authenticated (Success)\r\n  ← tagged completion
    #
    # We must loop, forwarding untagged '*' lines to the client, until we
    # see the tagged completion or a SASL '+' challenge.  Reading only one
    # line mistakes the CAPABILITY response for an auth failure.
    _login_tag_prefix = (login_tag + " ").encode()
    _upstream_caps = ""  # capability text seen during the auth exchange (for SPECIAL-USE discovery)
    try:
        login_resp = b""
        while True:
            line = await upstream_reader.readline()
            if not line:
                break
            if line.startswith((b"+ ", _login_tag_prefix)):
                # SASL challenge or tagged completion — stop here.
                login_resp = line
                break
            if b"CAPABILITY" in line.upper():
                _upstream_caps += " " + line.decode("utf-8", errors="replace")
                line = (
                    filter_capabilities(line.decode("utf-8", errors="replace").rstrip("\r\n"))
                    .encode() + b"\r\n"
                )
            # Untagged response (e.g. * CAPABILITY) — forward to client and keep reading.
            try:
                client_writer.write(line)
                await client_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                upstream_writer.close()
                return

        if _use_xoauth2 and login_resp.startswith(b"+ "):
            # SASL challenge — auth has already failed on Gmail's side.
            # Decode the base64 JSON to surface the error code; it contains
            # only status/schemes/scope — no credential material.
            import base64 as _b64
            import json as _json
            try:
                challenge_json = _json.loads(_b64.b64decode(login_resp[2:].strip()))
                logger.error(
                    "[%s] Upstream XOAUTH2 rejected: status=%s scope=%s",
                    peer_str,
                    challenge_json.get("status", "?"),
                    challenge_json.get("scope", "?"),
                )
            except Exception:
                logger.error(
                    "[%s] Upstream XOAUTH2 rejected (challenge decode failed)",
                    peer_str,
                )

            # Send the empty abort to complete the SASL exchange.
            try:
                upstream_writer.write(b"\r\n")
                await upstream_writer.drain()
                # Read the final tagged NO.
                login_resp = await upstream_reader.readline()
            except (OSError, asyncio.IncompleteReadError) as exc:
                logger.error("[%s] Failed to read XOAUTH2 final NO: %s", peer_str, exc)
                client_writer.close()
                upstream_writer.close()
                return

            # Send a clean NO to the client and close.
            try:
                client_writer.write(
                    f"{login_tag} NO [AUTHENTICATIONFAILED] Upstream XOAUTH2"
                    f" authentication failed — token may be expired, re-authorize the agent\r\n".encode()
                )
                await client_writer.drain()
            except OSError:
                pass
            client_writer.close()
            upstream_writer.close()
            return

        elif _use_xoauth2 and b" OK " not in login_resp.upper():
            # Unexpected non-OK, non-challenge response — close both sides.
            logger.error(
                "[%s] Upstream XOAUTH2 authentication failed (unexpected response)",
                peer_str,
            )
            try:
                client_writer.write(
                    f"{login_tag} NO [AUTHENTICATIONFAILED] Upstream XOAUTH2"
                    f" authentication failed — token may be expired, re-authorize the agent\r\n".encode()
                )
                await client_writer.drain()
            except OSError:
                pass
            client_writer.close()
            upstream_writer.close()
            return

        else:
            if b"CAPABILITY" in login_resp.upper():
                login_resp = (
                    filter_capabilities(login_resp.decode("utf-8", errors="replace").rstrip("\r\n"))
                    .encode() + b"\r\n"
                )
            client_writer.write(login_resp)
            await client_writer.drain()
    except (OSError, asyncio.IncompleteReadError) as exc:
        logger.error("[%s] Failed to forward upstream auth response: %s", peer_str, exc)
        client_writer.close()
        upstream_writer.close()
        return

    # Detect provider profile from upstream hostname for normalization rules.
    # Used to suppress duplicate Sent Mail APPENDs, rewrite COPY+DELETE→MOVE, etc.
    provider_profile = detect_provider(upstream_host)
    logger.info(
        "[%s] Provider profile: %s (upstream=%s)",
        peer_str, provider_profile.name, upstream_host,
    )

    # Proactive SPECIAL-USE discovery (RFC 6154) — one proxy-issued LIST so
    # intent derivation has server-declared folder roles even if the agent
    # never sends a LIST itself. Must run before the byte pumps start (the
    # responses are consumed here, not forwarded). Only after successful auth;
    # the tagged OK may also carry [CAPABILITY ...], so include it in the
    # already-seen capability text.
    if b" OK " in login_resp.upper():
        try:
            await _discover_special_use(
                upstream_reader, upstream_writer,
                _upstream_caps + " " + login_resp.decode("utf-8", errors="replace"),
                user_id=credential["user_id"], db_path=_db_path, peer=peer_str,
            )
        except Exception as exc:
            logger.warning("[%s] SPECIAL-USE discovery failed (non-fatal): %s", peer_str, exc)

    # Seed the session's intent-derivation cache from the DB — includes roles
    # from the discovery above and from any previous connections.
    try:
        _session_special_use: dict | None = await get_special_use_folders(
            user_id=credential["user_id"], db_path=_db_path
        )
    except Exception:
        _session_special_use = None  # None → _load_special_use falls back to the DB

    # Per-connection state shared between c2u (tracks SELECT) and u2c (syncs responses).
    session: dict = {
        "folder": None,              # currently selected folder name
        "folder_id": None,           # cached DB id for current folder
        "select_lines": [],          # untagged lines accumulate during SELECT
        "in_select": False,          # True while accumulating SELECT response lines
        "revert_trigger_tag": None,  # tag of last SELECT/NOOP/FETCH (triggers revert injection)
        "agent_id": credential["id"],  # agent_credentials.id for staging
        "user_id": credential["user_id"],  # owning tenant; scopes the mailbox mirror (issue #73)
        "search_uid_mode": False,    # True if last SEARCH was UID SEARCH
        "capability_tag": None,      # tag of an outstanding agent CAPABILITY (u2c filters its reply)
        # Provider normalization
        "provider_profile": provider_profile,
        "pending_copy_intent": None,  # PendingCopyIntent | None — held COPY awaiting STORE \Deleted
        # {lower-cased folder name: RFC 6154 role} for intent derivation —
        # seeded above, kept current by the LIST sync handler.
        "special_use": _session_special_use,
    }
    c2u = asyncio.create_task(
        _client_to_upstream(client_reader, upstream_writer, client_writer, session, peer_str, _db_path)
    )
    u2c = asyncio.create_task(
        _upstream_to_client(upstream_reader, client_writer, session, _db_path, peer_str)
    )

    _done, pending = await asyncio.wait({c2u, u2c}, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    try:
        upstream_writer.close()
        await upstream_writer.wait_closed()
    except OSError:
        pass

    try:
        client_writer.close()
        await client_writer.wait_closed()
    except OSError:
        pass

    logger.info("[%s] Client disconnected", peer_str)


async def start_proxy(host: str, port: int) -> asyncio.AbstractServer:
    """Start the proxy server and return the Server object.

    Exposed separately from main() so tests can bind to port 0 and retrieve the
    actual ephemeral port via server.sockets[0].getsockname()[1].
    """
    await init_db(DB_PATH)
    server = await asyncio.start_server(handle_client, host, port)
    actual = server.sockets[0].getsockname()
    logger.info(
        "Nuvrail IMAP proxy listening on %s:%d (upstream host/port per agent credential)",
        actual[0], actual[1],
    )
    return server


async def main() -> None:
    """Entry point: read config from env and start the proxy."""
    # Load optional plugins in THIS process. The IMAP proxy stages operations
    # (create_operation -> run_auto_decision), so the auto-decision provider must
    # be registered here, not just in the API process. No-op in open core.
    load_plugins()

    host = os.environ.get("NUVRAIL_PROXY_HOST", "127.0.0.1")
    warn_if_loopback_bind_in_container(host)
    port = int(os.environ.get("NUVRAIL_PROXY_IMAP_PORT", "10143"))

    server = await start_proxy(host, port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
