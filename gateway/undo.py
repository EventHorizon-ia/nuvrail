"""
Undo of approved+executed operations — the inverse of gateway.execution.

Reverses an executed IMAP operation within the undo window (default 24h).
Lives next to the forward executor (gateway.execution) and shares its
credential-resolution, upstream-login and JSON-field helpers, so the forward and inverse
mailbox-mutating commands sit side by side in one layer.

Data flow:
  POST /api/v1/operations/{id}/undo
       │
       ├─ load operation row from staged_operations
       │   ├─ status must be 'executed'
       │   ├─ undo_expires_at must be > now
       │   └─ op_type must be in UNDOABLE_OP_TYPES
       │
       ├─ move/trash only (#170): load the destination UIDs +
       │   UIDVALIDITY the forward MOVE recorded (COPYUID) from the
       │   'executed' audit row; missing → refuse (never guess a UID)
       │
       ├─ resolve_imap_credentials() (shared with the forward executor)
       │
       ├─ authenticate_upstream_imap() (shared with the forward executor:
       │   XOAUTH2 for OAuth2 agents, LOGIN otherwise)
       │
       ├─ execute the inverse IMAP command
       │
       └─ mark operation status → 'reverted', insert audit_log event

Inverse strategies:
  move      → SELECT folder_to; UIDVALIDITY must match the recorded one;
              UID MOVE <recorded dest UIDs> → folder_from. The undo MOVE
              must itself return COPYUID, else nothing moved → refuse.
  trash     → same as move, out of folder_to (Trash)
  mark_read → UID STORE -FLAGS (\\Seen)   [was +FLAGS]
  mark_unread→UID STORE +FLAGS (\\Seen)   [was -FLAGS]
  flag      → UID STORE -FLAGS (\\Flagged)[was +FLAGS]
  unflag    → UID STORE +FLAGS (\\Flagged)[was -FLAGS]

  The parser emits flag/unflag for \\Flagged (#173); it has never emitted
  star/unstar ("Star" is only the display label). Archive rides on
  op_type='move' + intent_label, so 'archive' is not an op_type (#174).

  Why not reuse message_ids: IMAP UIDs are per-mailbox. message_ids are the
  SOURCE folder's UIDs; in folder_to the message has a new UID, and the old
  number may belong to an unrelated message (or to nothing, which used to
  report a false 'reverted').

Operations NOT undoable in Phase 2:
  smtp_send — message already sent (or not yet; reverting is a cancel, not an undo)
  copy      — we don't track which UID was created in folder_to
  append    — body not stored in DB
  create    — folder DELETE is destructive; Phase 3
  rename    — requires re-rename; Phase 3
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import aioimaplib
from aioimaplib import quoted as imap_quoted

from gateway.audit import record_audit_event
from gateway.execution import (
    ImapCredentials,
    authenticate_upstream_imap,
    parse_copyuid,
    parse_uidvalidity,
    resolve_imap_credentials,
)
from gateway.staging import update_operation_status
from gateway.state_db import decode_json_list, get_db

UNDO_WINDOW_HOURS = int(os.getenv("UNDO_WINDOW_HOURS", "24"))

UNDOABLE_OP_TYPES = frozenset({
    "move",
    "trash",
    "mark_read",
    "mark_unread",
    "flag",
    "unflag",
})


MOVE_OP_TYPES = frozenset({"move", "trash"})

_UID_SET_RE = re.compile(r"^\d+(:\d+)?(,\d+(:\d+)?)*$")


class UndoError(Exception):
    """Raised when an undo cannot proceed. Message is safe to surface to the client."""


def count_uid_set(uid_set: str) -> int:
    """Number of UIDs in a COPYUID-style set such as ``7,9:11`` (→ 4)."""
    total = 0
    for part in uid_set.split(","):
        lo, _, hi = part.partition(":")
        total += abs(int(hi) - int(lo)) + 1 if hi else 1
    return total


async def _load_move_destination(operation_id: str, db_path: Path) -> tuple[str, int] | None:
    """Destination ``(uid_set, uidvalidity)`` recorded when the MOVE executed.

    Written by gateway.execution into the 'executed' audit row's detail from
    the server's COPYUID. None when absent (server lacks UIDPLUS, or the op
    executed before #170) or malformed.
    """
    async with get_db(db_path) as db, db.execute(
        "SELECT detail FROM audit_log WHERE operation_id = ? AND event = 'executed' "
        "ORDER BY id DESC LIMIT 1",
        (operation_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None or not row["detail"]:
        return None
    try:
        detail = json.loads(row["detail"])
    except (TypeError, ValueError):
        return None
    if not isinstance(detail, dict):
        return None
    dest_uids = detail.get("dest_uids")
    uidvalidity = detail.get("dest_uidvalidity")
    if (
        not isinstance(dest_uids, str)
        or not _UID_SET_RE.match(dest_uids)
        or isinstance(uidvalidity, bool)
        or not isinstance(uidvalidity, int)
    ):
        return None
    return dest_uids, uidvalidity


async def undo_operation(operation_id: str, db_path: Path) -> dict[str, Any]:
    """Reverse an executed operation and mark it reverted.

    Returns a summary dict with op_id, op_type, and what was reversed.
    Raises UndoError with a human-readable message on any validation or
    execution failure.
    """
    now = int(time.time())

    async with get_db(db_path) as db, db.execute(
        "SELECT * FROM staged_operations WHERE id = ?", (operation_id,)
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        raise UndoError(f"Operation {operation_id!r} not found.")

    row = dict(row)
    status = row.get("status")
    op_type = row.get("op_type", "")
    undo_expires_at = row.get("undo_expires_at") or 0

    if status != "executed":
        raise UndoError(
            f"Operation {operation_id!r} cannot be undone: status is {status!r}, "
            f"expected 'executed'."
        )
    if op_type not in UNDOABLE_OP_TYPES:
        raise UndoError(
            f"op_type {op_type!r} is not undoable. "
            f"Undoable types: {', '.join(sorted(UNDOABLE_OP_TYPES))}."
        )
    if undo_expires_at and now > undo_expires_at:
        raise UndoError(
            f"Operation {operation_id!r} undo window has expired "
            f"(window: {UNDO_WINDOW_HOURS}h, expired at {undo_expires_at})."
        )

    # --- Move-type ops: where did the messages go? (#170) ----------------
    folder_to = row.get("folder_to") or ""
    dest_uids: str | None = None
    dest_uidvalidity: int | None = None
    if op_type in MOVE_OP_TYPES:
        if not folder_to:
            # For trash this also covers the STORE \\Deleted fallback
            # (no Trash folder / no MOVE, #168): nothing was moved.
            raise UndoError(f"Cannot undo {op_type!r}: original folder_to not recorded.")
        destination = await _load_move_destination(operation_id, db_path)
        if destination is None:
            raise UndoError(
                f"Cannot undo {op_type!r}: the message's UID in {folder_to!r} was not "
                "recorded when it was moved (the server sent no COPYUID, or the "
                "operation predates this check). Undo refuses rather than guess — "
                f"move the message back from {folder_to!r} in your mail client."
            )
        dest_uids, dest_uidvalidity = destination

    # --- Resolve credentials (shared with the forward executor) ----------
    try:
        creds = await resolve_imap_credentials(row, db_path)
    except RuntimeError as exc:
        raise UndoError(
            f"Operation {operation_id!r} has no agent_id and no fallback "
            "IMAP env vars are set."
        ) from exc

    # --- Deserialize fields ----------------------------------------------
    folder_from = row.get("folder_from") or "INBOX"
    message_ids = decode_json_list(row.get("message_ids"))
    uid_set = ",".join(message_ids) if message_ids else "1"
    flags_add = decode_json_list(row.get("flags_add"))
    flags_remove = decode_json_list(row.get("flags_remove"))

    # --- Execute inverse -------------------------------------------------
    description = await _execute_undo_imap(
        op_type=op_type,
        uid_set=uid_set,
        folder_from=folder_from,
        folder_to=folder_to,
        dest_uids=dest_uids,
        dest_uidvalidity=dest_uidvalidity,
        flags_add=flags_add,
        flags_remove=flags_remove,
        creds=creds,
        db_path=db_path,
    )

    # --- Mark reverted in DB ---------------------------------------------
    await update_operation_status(operation_id, "reverted", db_path=db_path)
    agent_id = row.get("agent_id")
    await record_audit_event(
        db_path, timestamp=now, event='reverted', actor='human',
        operation_id=operation_id,
        agent_id=str(agent_id) if agent_id else None,
        op_type=op_type,
        intent_label=row.get('intent_label'),
        detail=json.dumps({'description': description}),
    )

    return {
        "operation_id": operation_id,
        "op_type": op_type,
        "reverted": description,
    }


async def _execute_undo_imap(
    *,
    op_type: str,
    uid_set: str,
    folder_from: str,
    folder_to: str,
    dest_uids: str | None,
    dest_uidvalidity: int | None,
    flags_add: list[str],
    flags_remove: list[str],
    creds: ImapCredentials,
    db_path: Path,
) -> str:
    """Execute the inverse IMAP command. Returns a human-readable description."""
    client = aioimaplib.IMAP4_SSL(host=creds.host, port=creds.port)
    try:
        try:
            # Same login as the forward executor: XOAUTH2 for OAuth2 agents,
            # LOGIN otherwise. Fails before any mailbox command, so a failed
            # token refresh leaves the operation 'executed' and nothing moved.
            await authenticate_upstream_imap(client, creds, db_path)
        except RuntimeError as exc:
            raise UndoError(f"IMAP authentication failed while attempting undo: {exc}") from exc

        if op_type in MOVE_OP_TYPES:
            # Original: UID MOVE folder_from → folder_to; the server assigned
            # new UIDs in folder_to (dest_uids, from COPYUID).
            # Reverse:  move those back folder_to → folder_from.
            if not dest_uids or dest_uidvalidity is None:
                # undo_operation refuses before connecting; defensive only.
                raise UndoError(f"Cannot undo {op_type!r}: destination UIDs not recorded.")
            # Quote mailbox names exactly as the forward executor does:
            # "Deleted Items" / "Deleted Messages" contain spaces and would
            # otherwise be sent as two IMAP arguments.
            status, data = await client.select(imap_quoted(folder_to))
            if status != "OK":
                raise UndoError(f"IMAP SELECT {folder_to!r} failed during undo: {data}")
            current_uidvalidity = parse_uidvalidity(data)
            if current_uidvalidity != dest_uidvalidity:
                raise UndoError(
                    f"Cannot undo {op_type!r}: {folder_to!r} UIDVALIDITY is "
                    f"{current_uidvalidity} but was {dest_uidvalidity} when the message "
                    "was moved, so the recorded UIDs no longer identify it. "
                    f"Move it back from {folder_to!r} in your mail client."
                )
            status, data = await client.uid("move", dest_uids, imap_quoted(folder_from))
            if status != "OK":
                raise UndoError(f"IMAP UID MOVE (undo) failed: {data}")
            # A UID MOVE naming UIDs that no longer exist is still an OK with
            # nothing moved; only a COPYUID proves messages came back.
            moved = parse_copyuid(data)
            if moved is None:
                raise UndoError(
                    f"Undo moved nothing: UIDs {dest_uids} are no longer in "
                    f"{folder_to!r} (moved or deleted since). The operation stays "
                    "'executed'."
                )
            expected, got = count_uid_set(dest_uids), count_uid_set(moved[1])
            if got < expected:
                return (
                    f"Moved {got} of {expected} messages (UIDs {dest_uids}) back from "
                    f"{folder_to!r} to {folder_from!r}; the rest were no longer in "
                    f"{folder_to!r}"
                )
            return f"Moved UIDs {dest_uids} back from {folder_to!r} to {folder_from!r}"

        elif op_type in ("mark_read", "flag"):
            # Original added flags — reverse by removing them
            if not flags_add:
                raise UndoError(f"Cannot undo {op_type!r}: no flags_add recorded.")
            status, data = await client.select(imap_quoted(folder_from))
            if status != "OK":
                raise UndoError(f"IMAP SELECT {folder_from!r} failed during undo: {data}")
            flag_str = " ".join(flags_add)
            status, data = await client.uid("store", uid_set, "-FLAGS", f"({flag_str})")
            if status != "OK":
                raise UndoError(f"IMAP UID STORE -FLAGS (undo) failed: {data}")
            return f"Removed flags {flags_add} from UIDs {uid_set} in {folder_from!r}"

        elif op_type in ("mark_unread", "unflag"):
            # Original removed flags — reverse by adding them back
            if not flags_remove:
                raise UndoError(f"Cannot undo {op_type!r}: no flags_remove recorded.")
            status, data = await client.select(imap_quoted(folder_from))
            if status != "OK":
                raise UndoError(f"IMAP SELECT {folder_from!r} failed during undo: {data}")
            flag_str = " ".join(flags_remove)
            status, data = await client.uid("store", uid_set, "+FLAGS", f"({flag_str})")
            if status != "OK":
                raise UndoError(f"IMAP UID STORE +FLAGS (undo) failed: {data}")
            return f"Restored flags {flags_remove} on UIDs {uid_set} in {folder_from!r}"

        else:
            raise UndoError(f"No undo strategy defined for op_type {op_type!r}.")

    finally:
        with contextlib.suppress(Exception):
            await client.logout()
