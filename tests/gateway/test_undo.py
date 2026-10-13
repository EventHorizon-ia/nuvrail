"""
Tests for gateway/undo.py — reversing an executed operation.

Validation paths run without a network; the inverse IMAP replay is exercised
with a mocked client (no live server).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.audit import record_audit_event
from gateway.credentials import encrypt_credential
from gateway.staging import create_operation
from gateway.state_db import get_db, init_db
from gateway.undo import UndoError, count_uid_set, undo_operation

# What the forward MOVE recorded (from COPYUID) for a seeded executed op:
# message 42 in INBOX became UID 7 in the destination, UIDVALIDITY 1234.
DEST_UIDVALIDITY = 1234
DEFAULT_DEST = ("7", DEST_UIDVALIDITY)


@pytest.fixture()
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "undo_test.db"
    await init_db(path)
    return path


async def _seed_agent(db_path: Path, *, oauth2: bool = False, with_password: bool = True) -> int:
    async with get_db(db_path) as db:
        # Seed the owning user (active) so the user-state JOIN in
        # get_agent_credential (issue #65) resolves on the execution path.
        await db.execute(
            """INSERT INTO users (id, email, display_name, hashed_password, created_at)
               VALUES (1, 'undo@example.com', 'T', 'x', 0)"""
        )
        cur = await db.execute(
            """INSERT INTO agent_credentials
               (user_id, label, agent_username, hashed_token,
                upstream_host, upstream_imap_port, upstream_smtp_port,
                upstream_user, upstream_password, oauth2_provider, created_at)
               VALUES (1, 'l', 'nuvrail_u', 'x', 'imap.example.com', 993, 587,
                       'u@example.com', ?, ?, 0)""",
            (
                encrypt_credential("pw") if with_password else None,
                "google" if oauth2 else None,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def _seed_executed_op(
    db_path: Path, *, agent_id: int, op_type: str = "move",
    folder_from: str = "INBOX", folder_to: str = "Archive",
    message_ids: list | None = None, undo_offset: int = 3600,
    dest: tuple[str, int] | None = DEFAULT_DEST, executed_detail: str | None = None,
    flags_add: list | None = None, flags_remove: list | None = None,
) -> str:
    """Seed an 'executed' op plus its 'executed' audit row.

    For move-type ops the audit detail carries the destination UIDs the
    forward MOVE recorded (``dest``); pass ``dest=None`` for an op executed
    without COPYUID (or before #170), or ``executed_detail`` to write a raw
    detail string.
    """
    op_id = await create_operation(
        op_type=op_type,
        protocol="imap",
        description=f"{op_type} op",
        agent_id=agent_id,
        folder_from=folder_from,
        folder_to=folder_to,
        message_ids=message_ids if message_ids is not None else ["42"],
        flags_add=flags_add,
        flags_remove=flags_remove,
        db_path=db_path,
    )
    now = int(time.time())
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE staged_operations SET status = 'executed', undo_expires_at = ? WHERE id = ?",
            (now + undo_offset, op_id),
        )
        await db.commit()
    if executed_detail is None and dest is not None and op_type in ("move", "trash"):
        executed_detail = json.dumps({"dest_uids": dest[0], "dest_uidvalidity": dest[1]})
    await record_audit_event(
        db_path, timestamp=now, event="executed", actor="human",
        operation_id=op_id, agent_id=str(agent_id), op_type=op_type,
        detail=executed_detail,
    )
    return op_id


class _FakeIMAP:
    """Records the IMAP commands undo issues; always replies OK.

    SELECT reports ``uidvalidity``; UID MOVE replies with ``move_lines``
    (default: a COPYUID proving one message moved back into INBOX).
    """

    def __init__(
        self, *args, uidvalidity: int | None = DEST_UIDVALIDITY,
        move_lines: list | None = None, **kwargs,
    ) -> None:
        self.calls: list[tuple] = []
        # aioimaplib's client.protocol; XOAUTH2 auth seeds capabilities here.
        self.protocol = SimpleNamespace(capabilities=set())
        self._uidvalidity = uidvalidity
        self._move_lines = (
            move_lines if move_lines is not None
            else [b"OK [COPYUID 555 7 1001] Moved UIDs.", b"Move completed."]
        )

    async def wait_hello_from_server(self) -> None:
        return None

    async def login(self, user, password):
        self.calls.append(("login", user))
        return "OK", [b"ok"]

    async def xoauth2(self, user, token):
        self.calls.append(("xoauth2", user))

        return SimpleNamespace(
            result="OK",
            lines=[b"CAPABILITY IMAP4rev1 UIDPLUS MOVE", b"authenticated (Success)"],
        )

    async def select(self, folder):
        self.calls.append(("select", folder))
        lines = [b"2 EXISTS"]
        if self._uidvalidity is not None:
            lines.append(f"OK [UIDVALIDITY {self._uidvalidity}] UIDs valid".encode())
        return "OK", [*lines, b"[READ-WRITE] Select completed."]

    async def uid(self, *args):
        self.calls.append(("uid", *args))
        if args and args[0] == "move":
            return "OK", list(self._move_lines)
        return "OK", [b"ok"]

    async def logout(self):
        self.calls.append(("logout",))
        return "OK", [b"bye"]


# ---------------------------------------------------------------------------
# Inverse replay
# ---------------------------------------------------------------------------


async def test_undo_move_reverses_direction(db_path: Path) -> None:
    """Undoing a move issues UID MOVE back from folder_to to folder_from,
    addressing the message by its DESTINATION UID (#170), not the source's."""
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(
        db_path, agent_id=agent_id, op_type="move",
        folder_from="INBOX", folder_to="Archive", message_ids=["42"],
    )

    fake = _FakeIMAP()
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake):
        result = await undo_operation(op_id, db_path)

    assert result["op_type"] == "move"
    assert ("select", "\"Archive\"") in fake.calls
    assert ("uid", "move", "7", "\"INBOX\"") in fake.calls
    # 42 is INBOX's UID; in Archive it may be an unrelated message.
    assert not [c for c in fake.calls if c[:3] == ("uid", "move", "42")]

    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT status FROM staged_operations WHERE id = ?", (op_id,)
        ) as cur:
            assert (await cur.fetchone())["status"] == "reverted"
        async with db.execute(
            "SELECT event, actor FROM audit_log WHERE operation_id = ? AND event = 'reverted'",
            (op_id,),
        ) as cur:
            audit = await cur.fetchone()
    assert audit is not None and audit["actor"] == "human"


async def test_undo_trash_moves_back_out_of_trash(db_path: Path) -> None:
    """#168: an executed trash op moved the message into folder_to (Trash);
    undo moves it back. Mailbox names are quoted like the forward executor."""
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(
        db_path, agent_id=agent_id, op_type="trash",
        folder_from="INBOX", folder_to="Deleted Items", message_ids=["42"],
    )

    fake = _FakeIMAP()
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake):
        result = await undo_operation(op_id, db_path)

    assert result["op_type"] == "trash"
    assert ("select", '"Deleted Items"') in fake.calls
    assert ("uid", "move", "7", '"INBOX"') in fake.calls


async def test_undo_trash_store_fallback_refuses(db_path: Path) -> None:
    """A trash op that fell back to STORE \\Deleted has no folder_to: nothing
    was moved, so undo refuses instead of moving something out of Trash."""
    agent_id = await _seed_agent(db_path)
    op_id = await create_operation(
        op_type="trash", protocol="imap", description="Mark deleted",
        agent_id=agent_id, folder_from="INBOX", folder_to=None,
        message_ids=["42"], db_path=db_path,
    )
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE staged_operations SET status = 'executed', undo_expires_at = ? WHERE id = ?",
            (int(time.time()) + 3600, op_id),
        )
        await db.commit()

    fake = _FakeIMAP()
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake):
        with pytest.raises(UndoError, match="folder_to not recorded"):
            await undo_operation(op_id, db_path)
    assert not [c for c in fake.calls if c[0] == "uid"]


# ---------------------------------------------------------------------------
# Destination UIDs (#170): refuse rather than guess
#
#   executed audit detail has dest UIDs? ──no──► refuse, no IMAP connection
#        │ yes
#        ▼
#   SELECT folder_to: UIDVALIDITY matches? ──no──► refuse
#        │ yes
#        ▼
#   UID MOVE <dest UIDs> → folder_from: COPYUID back? ──no──► refuse (nothing moved)
#        │ yes (all / some)
#        ▼
#   'reverted' (description says "N of M" when partial)
# ---------------------------------------------------------------------------


async def _status_and_reverted_rows(db_path: Path, op_id: str) -> tuple[str, int]:
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT status FROM staged_operations WHERE id = ?", (op_id,)
        ) as cur:
            status = (await cur.fetchone())["status"]
        async with db.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE operation_id = ? AND event = 'reverted'",
            (op_id,),
        ) as cur:
            reverted = (await cur.fetchone())["n"]
    return status, reverted


@pytest.mark.parametrize("op_type", ["move", "trash"])
async def test_undo_refuses_when_destination_uid_unknown(db_path: Path, op_type: str) -> None:
    """No COPYUID at execution (no UIDPLUS, or op predates #170): refuse
    before connecting, and leave the op 'executed'."""
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(db_path, agent_id=agent_id, op_type=op_type, dest=None)

    with patch("gateway.undo.aioimaplib.IMAP4_SSL") as ctor:
        with pytest.raises(UndoError, match="was not recorded"):
            await undo_operation(op_id, db_path)
        ctor.assert_not_called()
    assert await _status_and_reverted_rows(db_path, op_id) == ("executed", 0)


@pytest.mark.parametrize("detail", [
    json.dumps({"dest_uids": "7 INBOX", "dest_uidvalidity": 1234}),   # not a UID set
    json.dumps({"dest_uids": "7", "dest_uidvalidity": "1234"}),      # wrong type
    json.dumps({"dest_uids": "7", "dest_uidvalidity": True}),        # bool is not an int
    json.dumps(["7", 1234]),                                         # not an object
    "not json",
])
async def test_undo_refuses_on_malformed_destination_detail(db_path: Path, detail: str) -> None:
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(db_path, agent_id=agent_id, executed_detail=detail)

    with patch("gateway.undo.aioimaplib.IMAP4_SSL") as ctor:
        with pytest.raises(UndoError, match="was not recorded"):
            await undo_operation(op_id, db_path)
        ctor.assert_not_called()


async def test_undo_refuses_when_uidvalidity_changed(db_path: Path) -> None:
    """The folder was recreated/reset since the move: the recorded UID may
    now name a different message, so nothing is moved."""
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(db_path, agent_id=agent_id)

    fake = _FakeIMAP(uidvalidity=DEST_UIDVALIDITY + 1)
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake):
        with pytest.raises(UndoError, match="UIDVALIDITY"):
            await undo_operation(op_id, db_path)
    assert not [c for c in fake.calls if c[0] == "uid"]
    assert await _status_and_reverted_rows(db_path, op_id) == ("executed", 0)


async def test_undo_refuses_when_select_reports_no_uidvalidity(db_path: Path) -> None:
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(db_path, agent_id=agent_id)

    fake = _FakeIMAP(uidvalidity=None)
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake):
        with pytest.raises(UndoError, match="UIDVALIDITY"):
            await undo_operation(op_id, db_path)
    assert not [c for c in fake.calls if c[0] == "uid"]


async def test_undo_that_moves_nothing_is_not_reverted(db_path: Path) -> None:
    """The regression in #170: UID MOVE of UIDs no longer in the folder is an
    OK with no COPYUID. That must not be recorded as 'reverted'."""
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(db_path, agent_id=agent_id)

    fake = _FakeIMAP(move_lines=[b"No messages found."])
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake):
        with pytest.raises(UndoError, match="moved nothing"):
            await undo_operation(op_id, db_path)
    assert ("uid", "move", "7", '"INBOX"') in fake.calls
    assert await _status_and_reverted_rows(db_path, op_id) == ("executed", 0)


async def test_undo_partial_move_back_says_so(db_path: Path) -> None:
    """Two messages moved; one has since left Archive. The one that came back
    is reverted and the audit description says 1 of 2."""
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(
        db_path, agent_id=agent_id, message_ids=["41", "42"], dest=("7:8", DEST_UIDVALIDITY),
    )

    fake = _FakeIMAP(move_lines=[b"OK [COPYUID 555 8 1002] Moved UIDs.", b"Done"])
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake):
        result = await undo_operation(op_id, db_path)

    assert ("uid", "move", "7:8", '"INBOX"') in fake.calls
    assert result["reverted"].startswith("Moved 1 of 2 messages")
    assert await _status_and_reverted_rows(db_path, op_id) == ("reverted", 1)


async def test_execute_then_undo_round_trip_uses_copyuid(db_path: Path) -> None:
    """Forward MOVE via gateway.execution records COPYUID; undo consumes it."""
    from gateway.execution import execute_operation
    from gateway.staging import get_operation

    agent_id = await _seed_agent(db_path)
    op_id = await create_operation(
        op_type="move", protocol="imap", description="m", agent_id=agent_id,
        folder_from="INBOX", folder_to="Archive", message_ids=["42"], db_path=db_path,
    )

    forward = _FakeIMAP(move_lines=[b"OK [COPYUID 1234 42 7] Moved UIDs.", b"Done"])
    row = await get_operation(op_id, db_path=db_path)
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=forward):
        await execute_operation(op_id, row, db_path)
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE staged_operations SET undo_expires_at = ? WHERE id = ?",
            (int(time.time()) + 3600, op_id),
        )
        await db.commit()

    back = _FakeIMAP()
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=back):
        await undo_operation(op_id, db_path)
    assert ("uid", "move", "7", '"INBOX"') in back.calls


def test_count_uid_set() -> None:
    assert count_uid_set("7") == 1
    assert count_uid_set("7,9:11") == 4
    assert count_uid_set("3956:3958") == 3
    assert count_uid_set("11:9") == 3   # RFC 3501 allows reversed ranges


# ---------------------------------------------------------------------------
# Flag ops (#173): the parser emits flag/unflag for \\Flagged, and undo
# reverses them with the opposite STORE on the same (source-folder) UIDs.
#
#   flag   (+FLAGS \\Flagged) → undo: UID STORE 42 -FLAGS (\\Flagged)
#   unflag (-FLAGS \\Flagged) → undo: UID STORE 42 +FLAGS (\\Flagged)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_type", "flags", "inverse"),
    [
        ("flag", {"flags_add": ["\\Flagged"]}, "-FLAGS"),
        ("unflag", {"flags_remove": ["\\Flagged"]}, "+FLAGS"),
    ],
)
async def test_undo_flag_ops_store_the_inverse(
    db_path: Path, op_type: str, flags: dict, inverse: str
) -> None:
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(
        db_path, agent_id=agent_id, op_type=op_type, folder_to=None, **flags,
    )

    fake = _FakeIMAP()
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake):
        result = await undo_operation(op_id, db_path)

    assert result["op_type"] == op_type
    assert ("uid", "store", "42", inverse, "(\\Flagged)") in fake.calls
    assert await _status_and_reverted_rows(db_path, op_id) == ("reverted", 1)


# ---------------------------------------------------------------------------
# Validation (no network)
# ---------------------------------------------------------------------------


async def test_undo_unknown_op_raises(db_path: Path) -> None:
    with pytest.raises(UndoError, match="not found"):
        await undo_operation("op_missing", db_path)


async def test_undo_non_executed_raises(db_path: Path) -> None:
    agent_id = await _seed_agent(db_path)
    op_id = await create_operation(
        op_type="move", protocol="imap", description="m", agent_id=agent_id,
        folder_to="Archive", message_ids=["1"], db_path=db_path,
    )  # still 'pending'
    with pytest.raises(UndoError, match="cannot be undone"):
        await undo_operation(op_id, db_path)


@pytest.mark.parametrize("op_type", ["copy", "archive", "star", "unstar"])
async def test_undo_non_undoable_op_type_raises(db_path: Path, op_type: str) -> None:
    """archive/star/unstar are not op_types the parser emits (#173, #174);
    they are refused like copy, before any connection."""
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(db_path, agent_id=agent_id, op_type=op_type)
    with patch("gateway.undo.aioimaplib.IMAP4_SSL") as ctor:
        with pytest.raises(UndoError, match="not undoable"):
            await undo_operation(op_id, db_path)
        ctor.assert_not_called()


async def test_undo_expired_window_raises(db_path: Path) -> None:
    agent_id = await _seed_agent(db_path)
    op_id = await _seed_executed_op(db_path, agent_id=agent_id, undo_offset=-60)
    with pytest.raises(UndoError, match="window has expired"):
        await undo_operation(op_id, db_path)


# ---------------------------------------------------------------------------
# OAuth2 agents (Gmail/Outlook XOAUTH2) — undo logs in the way execution does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_type", "kwargs", "inverse"),
    [
        ("move", {}, ("uid", "move", "7", '"INBOX"')),
        ("trash", {"folder_to": "[Gmail]/Trash"}, ("uid", "move", "7", '"INBOX"')),
        ("flag", {"folder_to": None, "flags_add": ["\\Flagged"]},
         ("uid", "store", "42", "-FLAGS", "(\\Flagged)")),
    ],
)
async def test_undo_oauth2_agent_uses_xoauth2(
    db_path: Path, op_type: str, kwargs: dict, inverse: tuple
) -> None:
    """An OAuth2 agent (no stored password) undoes via AUTHENTICATE XOAUTH2."""
    agent_id = await _seed_agent(db_path, oauth2=True, with_password=False)
    op_id = await _seed_executed_op(db_path, agent_id=agent_id, op_type=op_type, **kwargs)

    fake = _FakeIMAP()
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake), patch(
        "gateway.oauth2_tokens.get_access_token", return_value=("u@example.com", "tok")
    ):
        result = await undo_operation(op_id, db_path)

    assert result["op_type"] == op_type
    assert ("xoauth2", "u@example.com") in fake.calls
    assert not any(c[0] == "login" for c in fake.calls)
    assert inverse in fake.calls
    assert await _status_and_reverted_rows(db_path, op_id) == ("reverted", 1)


async def test_undo_oauth2_token_refresh_failure_moves_nothing(db_path: Path) -> None:
    """A failed token refresh refuses the undo before any mailbox command."""
    from gateway.oauth2_tokens import OAuth2Error

    agent_id = await _seed_agent(db_path, oauth2=True, with_password=False)
    op_id = await _seed_executed_op(db_path, agent_id=agent_id)

    fake = _FakeIMAP()
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake), patch(
        "gateway.oauth2_tokens.get_access_token",
        side_effect=OAuth2Error("Google token refresh failed: HTTP 400 error='invalid_grant'"),
    ), pytest.raises(UndoError, match="authentication failed while attempting undo"):
        await undo_operation(op_id, db_path)

    assert not any(c[0] in ("select", "uid", "xoauth2") for c in fake.calls)
    assert fake.calls[-1] == ("logout",)
    assert await _status_and_reverted_rows(db_path, op_id) == ("executed", 0)


async def test_undo_without_password_or_oauth2_refuses(db_path: Path) -> None:
    """No stored password and no OAuth2 provider: fail closed, no LOGIN sent."""
    agent_id = await _seed_agent(db_path, oauth2=False, with_password=False)
    op_id = await _seed_executed_op(db_path, agent_id=agent_id)

    fake = _FakeIMAP()
    with patch("gateway.undo.aioimaplib.IMAP4_SSL", return_value=fake), pytest.raises(
        UndoError, match="needs a password"
    ):
        await undo_operation(op_id, db_path)

    assert not any(c[0] in ("login", "select", "uid") for c in fake.calls)
    assert await _status_and_reverted_rows(db_path, op_id) == ("executed", 0)
