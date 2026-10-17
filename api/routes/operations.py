"""
/api/v1/operations endpoints — Milestone 1.0.

GET    /operations              — list (optional ?status= filter)
GET    /operations/{op_id}      — single operation detail
POST   /operations/batch/approve — approve multiple operations
POST   /operations/batch/reject  — reject multiple operations
POST   /operations/{op_id}/approve  — approve + execute against upstream
POST   /operations/{op_id}/reject   — reject

Approve logic:
  - SMTP ops: relay via aiosmtplib to upstream SMTP server (STARTTLS)
  - IMAP ops: open a fresh aioimaplib connection to upstream, replay the
    stored raw IMAP command, close connection. Supports: STORE, UID STORE,
    MOVE, UID MOVE, COPY, UID COPY, CREATE, RENAME. APPEND is skipped
    (message body not stored in staging DB — deferred to later milestone).

Execution flow for IMAP:

  approve request
      │
      ▼
  open fresh IMAP4_SSL → mail.example.com:993
      │  LOGIN (plain credentials from env)
      │
      ▼
  SELECT folder_from  (for STORE/MOVE/COPY — folder context required)
      │
      ▼
  replay command (uid store / uid move / uid copy / create / rename)
      │
      ▼
  LOGOUT → close connection
      │
      ▼
  update staged_operations status → 'executed'
  insert audit_log event='executed'

Route ordering note: batch routes (/operations/batch/approve and
/operations/batch/reject) are registered BEFORE the parameterised route
/operations/{op_id}/approve so FastAPI does not treat 'batch' as an op_id.

Sub-milestone: 1.0 (IMAP execution added)
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import get_current_user
from api.models import (
    ApproveResponse,
    BatchApproveRequest,
    BatchApproveResponse,
    BatchApproveResult,
    BatchRejectRequest,
    BatchRejectResponse,
    BatchRejectResult,
    MessagePreview,
    OperationListResponse,
    OperationResponse,
    RejectResponse,
    UndoResponse,
)
from gateway.audit import record_audit_event
from gateway.batch_summary import build_batch_summary
from gateway.execution import ExecutionError, execute_operation
from gateway.staging import get_operation, list_operations, update_operation_status
from gateway.state_db import (
    DB_PATH,
    decode_json_list,
    insert_pending_reverts,
    restore_from_snapshot,
)
from gateway.undo import UndoError, undo_operation

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# DB path dependency — overridable in tests via app.dependency_overrides
# ---------------------------------------------------------------------------


def get_db_path() -> Path:
    """Return the DB path. Override in tests via dependency injection."""
    return DB_PATH


def _previews_from_snapshot(row: dict, max_messages: int = 3) -> list[MessagePreview]:
    """Extract MessagePreview objects from the operation's snapshot field.

    Fallback used when the messages table has no rows (e.g. after MOVE staging
    deletes them, or when folder_from is not set on older operations).
    Returns [] if snapshot is absent or has no sender/subject data.
    """
    snap_raw = row.get("snapshot")
    if not snap_raw:
        return []
    try:
        snap = json.loads(snap_raw) if isinstance(snap_raw, str) else snap_raw
        previews: list[MessagePreview] = []
        for uid_str, state in (snap or {}).items():
            if len(previews) >= max_messages:
                break
            sender = state.get("sender")
            subject = state.get("subject")
            if sender or subject:  # only include if we have something to show
                previews.append(MessagePreview(
                    uid=int(uid_str),
                    sender=sender,
                    subject=subject,
                    date_sent=None,
                ))
        return previews
    except Exception:
        return []


async def _fetch_message_previews(
    row: dict, db_path: Path, max_messages: int = 3
) -> list[MessagePreview]:
    """Look up sender/subject for IMAP operation's affected messages.

    Primary path: joins folder_from -> folders table -> messages table.
    Fallback: reads sender/subject from the operation's snapshot field
    (used when rows were deleted during MOVE staging, or folder_from is null).
    SMTP ops return an empty list.
    """
    from gateway.state_db import get_db as _get_db

    if row.get("protocol") != "imap":
        return []
    folder_name = row.get("folder_from")
    if not folder_name:
        # No folder context — go straight to snapshot fallback
        return _previews_from_snapshot(row, max_messages)
    raw_ids = row.get("message_ids")
    if not raw_ids:
        return []
    try:
        uid_list = decode_json_list(raw_ids)
    except Exception:
        return []
    if not uid_list:
        return []
    # message_ids is stored as ["<uid_set_str>"] — e.g. ["42"] or ["1:5"]
    uid_set_str = uid_list[0] if isinstance(uid_list[0], str) else str(uid_list[0])

    try:
        async with _get_db(db_path) as db:
            # Resolve folder name to folder_id — scoped to THIS operation's owning
            # user. Folder names are unique only within a tenant (UNIQUE(user_id,
            # name)), so resolving by name alone could match another tenant's
            # same-named folder (e.g. "INBOX") and leak their message previews,
            # especially since UIDs overlap heavily across mailboxes. Derive the
            # owner from the operation's agent rather than trusting the name.
            async with db.execute(
                """
                SELECT id FROM folders
                WHERE name = ?
                  AND user_id = (
                      SELECT user_id FROM agent_credentials WHERE id = ?
                  )
                """,
                (folder_name, row.get("agent_id")),
            ) as cur:
                folder_row = await cur.fetchone()
            if folder_row is None:
                return []
            folder_id = folder_row[0]

            # Expand the UID set and fetch sender/subject for up to max_messages
            # We use a simple expansion: if it's a plain UID or comma list, split;
            # if it's a range (e.g. 1:5), let the DB do the work via BETWEEN.
            previews: list[MessagePreview] = []
            if ":" in uid_set_str and uid_set_str != "1:*":
                # Range like "41:71" — fetch ordered by uid, limit to max_messages
                parts = uid_set_str.split(":")
                lo, hi = int(parts[0]), int(parts[1])
                async with db.execute(
                    "SELECT uid, sender, subject, date_sent FROM messages "
                    "WHERE folder_id = ? AND uid BETWEEN ? AND ? ORDER BY uid LIMIT ?",
                    (folder_id, lo, hi, max_messages),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                # Individual UIDs (comma-separated) or single UID
                uids = [int(u.strip()) for u in uid_set_str.split(",") if u.strip().isdigit()]
                if not uids:
                    return []
                uids = uids[:max_messages]
                placeholders = ",".join("?" for _ in uids)
                async with db.execute(
                    f"SELECT uid, sender, subject, date_sent FROM messages "
                    f"WHERE folder_id = ? AND uid IN ({placeholders}) ORDER BY uid",
                    [folder_id, *uids],
                ) as cur:
                    rows = await cur.fetchall()

            for r in rows:
                previews.append(MessagePreview(
                    uid=r[0],
                    sender=r[1],
                    subject=r[2],
                    date_sent=r[3],
                ))
            # If messages table has no rows (e.g. deleted during MOVE staging),
            # fall back to snapshot which captures sender/subject at staging time.
            if not previews:
                return _previews_from_snapshot(row, max_messages)

            return previews
    except Exception:
        # Non-fatal: fall back to snapshot rather than breaking the API
        return _previews_from_snapshot(row, max_messages)


def _row_to_response(row: dict) -> OperationResponse:
    """Convert a raw DB dict to an OperationResponse, deserializing JSON fields."""
    return OperationResponse(**row)


async def _do_approve(op_id: str, row: dict, db_path: Path) -> ApproveResponse:
    """Approve a single operation by executing it upstream.

    Delegates to the shared gateway.execution.execute_operation (the same code
    path the auto-approval rules use), translating an upstream failure into a
    500 for the HTTP caller.

    Args:
        op_id: The operation ID.
        row: The raw DB row dict (must have status='pending').
        db_path: Path to the SQLite DB.

    Returns:
        ApproveResponse on success.

    Raises:
        HTTPException(500) if upstream execution fails.
    """
    try:
        result = await execute_operation(op_id, row, db_path, actor="human")
    except ExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ApproveResponse(
        id=op_id,
        status="executed",
        executed_at=result.get("executed_at"),
    )


async def _do_reject(op_id: str, row: dict, db_path: Path) -> RejectResponse:
    """Core reject logic for a single operation.

    Updates status to 'rejected', inserts audit log entry, attempts snapshot
    revert (non-fatal).

    Args:
        op_id: The operation ID.
        row: The raw DB row dict (must have status='pending').
        db_path: Path to the SQLite DB.

    Returns:
        RejectResponse on success.
    """
    await update_operation_status(op_id, "rejected", db_path=db_path)
    await record_audit_event(
        db_path, timestamp=int(time.time()), event='rejected', actor='human',
        operation_id=op_id, agent_id=row.get('agent_id'), op_type=row.get('op_type'),
        intent_label=row.get('intent_label'),
    )

    # Restore local state DB from snapshot and queue unsolicited FETCH responses.
    # Non-fatal: if revert fails (e.g. no snapshot), rejection still succeeds.
    try:
        reverts = await restore_from_snapshot(op_id, db_path=db_path)
        await insert_pending_reverts(op_id, reverts, db_path=db_path)
        if reverts:
            logger.info(
                "[reject] Restored snapshot and queued %d pending_reverts for op %s",
                len(reverts),
                op_id,
            )
    except Exception as exc:
        logger.warning("[reject] Snapshot revert failed for op %s (non-fatal): %s", op_id, exc)

    return RejectResponse(id=op_id, status="rejected")


# ---------------------------------------------------------------------------
# Endpoints — batch routes FIRST (before parameterised /{op_id} routes)
# ---------------------------------------------------------------------------


@router.get("/operations", response_model=OperationListResponse)
async def list_ops(
    status: str | None = None,
    agent_id: int | None = Query(default=None, ge=1),
    db_path: Path = Depends(get_db_path),
    current_user: dict = Depends(get_current_user),
) -> OperationListResponse:
    """List staged operations, optionally filtered by status.

    Scoped to the authenticated user's own agents — operations belonging to
    other users are never returned.
    """
    rows = await list_operations(
        status=status, agent_id=agent_id, db_path=db_path, user_id=current_user["id"]
    )
    ops = []
    for r in rows:
        op = _row_to_response(r)
        op.message_previews = await _fetch_message_previews(r, db_path)
        ops.append(op)

    # One-line summaries for multi-op batches — the batch card's headline.
    by_batch: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("batch_id"):
            by_batch.setdefault(r["batch_id"], []).append(r)
    batch_summaries = {
        batch_id: build_batch_summary(batch_rows)
        for batch_id, batch_rows in by_batch.items()
        if len(batch_rows) > 1
    }

    return OperationListResponse(
        operations=ops, total=len(ops), batch_summaries=batch_summaries
    )


@router.post("/operations/batch/approve", response_model=BatchApproveResponse)
async def batch_approve(
    body: BatchApproveRequest,
    db_path: Path = Depends(get_db_path),
    current_user: dict = Depends(get_current_user),
) -> BatchApproveResponse:
    """Approve multiple operations by ID.

    Processing rules:
    - Operations are approved in the order provided.
    - SMTP sends are approved individually in sequence (no special handling needed).
    - If an op is not found: added to skipped list.
    - If an op is not 'pending': added to skipped list (already decided).
    - If approve execution fails: added to failed list with error; processing continues.
    - Empty list: returns 400.
    - More than 100 IDs: returns 400.
    """
    if not body.operation_ids:
        raise HTTPException(status_code=400, detail="operation_ids must not be empty")
    if len(body.operation_ids) > 100:
        raise HTTPException(
            status_code=400,
            detail=f"Too many operation IDs: {len(body.operation_ids)} (max 100)",
        )

    approved: list[BatchApproveResult] = []
    failed: list[BatchApproveResult] = []
    skipped: list[BatchApproveResult] = []

    for op_id in body.operation_ids:
        row = await get_operation(op_id, db_path=db_path, user_id=current_user["id"])
        if row is None:
            skipped.append(BatchApproveResult(id=op_id, status="skipped", error="not found"))
            continue
        if row["status"] != "pending":
            skipped.append(
                BatchApproveResult(
                    id=op_id,
                    status="skipped",
                    error=f"already in status '{row['status']}'",
                )
            )
            continue
        try:
            result = await _do_approve(op_id, row, db_path)
            approved.append(
                BatchApproveResult(
                    id=op_id,
                    status="executed",
                    executed_at=result.executed_at,
                )
            )
        except Exception as exc:
            error_msg = str(exc)
            logger.error("[batch_approve] Failed to approve op %s: %s", op_id, error_msg)
            failed.append(BatchApproveResult(id=op_id, status="failed", error=error_msg))

    return BatchApproveResponse(
        approved=approved,
        failed=failed,
        skipped=skipped,
        total=len(body.operation_ids),
    )


@router.post("/operations/batch/reject", response_model=BatchRejectResponse)
async def batch_reject(
    body: BatchRejectRequest,
    db_path: Path = Depends(get_db_path),
    current_user: dict = Depends(get_current_user),
) -> BatchRejectResponse:
    """Reject multiple operations by ID.

    Processing rules:
    - Operations are rejected in the order provided.
    - If an op is not found: added to skipped list.
    - If an op is not 'pending': added to skipped list (already decided).
    - If reject execution fails: added to failed list with error; processing continues.
    - Empty list: returns 400.
    - More than 100 IDs: returns 400.
    """
    if not body.operation_ids:
        raise HTTPException(status_code=400, detail="operation_ids must not be empty")
    if len(body.operation_ids) > 100:
        raise HTTPException(
            status_code=400,
            detail=f"Too many operation IDs: {len(body.operation_ids)} (max 100)",
        )

    rejected: list[BatchRejectResult] = []
    failed: list[BatchRejectResult] = []
    skipped: list[BatchRejectResult] = []

    for op_id in body.operation_ids:
        row = await get_operation(op_id, db_path=db_path, user_id=current_user["id"])
        if row is None:
            skipped.append(BatchRejectResult(id=op_id, status="skipped", error="not found"))
            continue
        if row["status"] != "pending":
            skipped.append(
                BatchRejectResult(
                    id=op_id,
                    status="skipped",
                    error=f"already in status '{row['status']}'",
                )
            )
            continue
        try:
            await _do_reject(op_id, row, db_path)
            rejected.append(BatchRejectResult(id=op_id, status="rejected"))
        except Exception as exc:
            error_msg = str(exc)
            logger.error("[batch_reject] Failed to reject op %s: %s", op_id, error_msg)
            failed.append(BatchRejectResult(id=op_id, status="failed", error=error_msg))

    return BatchRejectResponse(
        rejected=rejected,
        failed=failed,
        skipped=skipped,
        total=len(body.operation_ids),
    )


# ---------------------------------------------------------------------------
# Single-op endpoints — AFTER batch routes to avoid /batch matching as op_id
# ---------------------------------------------------------------------------


@router.get("/operations/{op_id}", response_model=OperationResponse)
async def get_op(
    op_id: str,
    db_path: Path = Depends(get_db_path),
    current_user: dict = Depends(get_current_user),
) -> OperationResponse:
    """Retrieve a single operation by ID (scoped to the authenticated user)."""
    row = await get_operation(op_id, db_path=db_path, user_id=current_user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail=f"Operation {op_id!r} not found")
    op = _row_to_response(row)
    op.message_previews = await _fetch_message_previews(row, db_path)
    return op


@router.post("/operations/{op_id}/approve", response_model=ApproveResponse)
async def approve_op(
    op_id: str,
    db_path: Path = Depends(get_db_path),
    current_user: dict = Depends(get_current_user),
) -> ApproveResponse:
    """Approve and execute an operation.

    - SMTP ops: relays the message to the upstream SMTP server via aiosmtplib.
    - IMAP ops: replays the stored command against the upstream IMAP server.
    """
    row = await get_operation(op_id, db_path=db_path, user_id=current_user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail=f"Operation {op_id!r} not found")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Operation {op_id!r} is already in status '{row['status']}'",
        )
    return await _do_approve(op_id, row, db_path)


@router.post("/operations/{op_id}/reject", response_model=RejectResponse)
async def reject_op(
    op_id: str,
    db_path: Path = Depends(get_db_path),
    current_user: dict = Depends(get_current_user),
) -> RejectResponse:
    """Reject a pending operation (scoped to the authenticated user)."""
    row = await get_operation(op_id, db_path=db_path, user_id=current_user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail=f"Operation {op_id!r} not found")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Operation {op_id!r} is already in status '{row['status']}'",
        )
    return await _do_reject(op_id, row, db_path)


@router.post("/operations/{op_id}/undo", response_model=UndoResponse)
async def undo_op(
    op_id: str,
    db_path: Path = Depends(get_db_path),
    current_user: dict = Depends(get_current_user),
) -> UndoResponse:
    """Undo an executed operation within the undo window.

    Reverses the IMAP command that was applied when the operation was approved.
    Only operations with status 'executed' and op_type in the undoable set
    (move, trash, mark_read, mark_unread, flag, unflag) can be undone, for
    password and OAuth2 (XOAUTH2) agents alike.
    The undo window defaults to 24h and is configurable via UNDO_WINDOW_HOURS.
    """
    # Tenant isolation: only the owning user may undo their own operation.
    # Return 404 (not 403) so foreign op IDs are indistinguishable from
    # non-existent ones.
    owned = await get_operation(op_id, db_path=db_path, user_id=current_user["id"])
    if owned is None:
        raise HTTPException(status_code=404, detail=f"Operation {op_id!r} not found")
    try:
        result = await undo_operation(op_id, db_path=db_path)
    except UndoError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return UndoResponse(
        id=op_id,
        status="reverted",
        op_type=result["op_type"],
        reverted=result["reverted"],
    )
