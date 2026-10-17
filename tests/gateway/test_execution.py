"""
Tests for gateway/execution.py — upstream replay of staged operations.

Focus: APPEND operations now carry the raw message body (base64 in
append_message) and are actually replayed upstream on approval, instead of
being silent no-ops. The upstream IMAP client is mocked — no network.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.credentials import encrypt_credential
from gateway.execution import (
    ExecutionError,
    ImapCredentials,
    _execute_imap_upstream,
    authenticate_upstream_imap,
    execute_operation,
    parse_copyuid,
    parse_uidvalidity,
    resolve_imap_credentials,
)
from gateway.staging import create_operation, get_operation
from gateway.state_db import decode_json_list, get_db, init_db


@pytest.fixture()
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "exec_test.db"
    await init_db(path)
    return path


async def _seed_agent(db_path: Path) -> int:
    async with get_db(db_path) as db:
        # Seed the owning user (active) so the user-state JOIN in
        # get_agent_credential (issue #65) resolves on the execution path.
        await db.execute(
            """INSERT INTO users (id, email, display_name, hashed_password, created_at)
               VALUES (1, 'exec@example.com', 'T', 'x', 0)"""
        )
        cur = await db.execute(
            """INSERT INTO agent_credentials
               (user_id, label, agent_username, hashed_token,
                upstream_host, upstream_imap_port, upstream_smtp_port,
                upstream_user, upstream_password, created_at)
               VALUES (1, 'test', 'nuvrail_exec', 'x', 'imap.example.com', 993, 587,
                       'u@example.com', ?, 0)""",
            (encrypt_credential("hunter2"),),
        )
        await db.commit()
        return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Shared helpers: decode_json_list / resolve_imap_credentials
# ---------------------------------------------------------------------------


def test_decode_json_list_variants() -> None:
    assert decode_json_list('["1","2"]') == ["1", "2"]   # JSON string
    assert decode_json_list(["a"]) == ["a"]               # already a list
    assert decode_json_list(None) == []                   # NULL column
    assert decode_json_list("") == []                     # empty string is falsy


async def test_resolve_imap_credentials_from_agent(db_path: Path) -> None:
    """Agent row is used and the stored password is decrypted."""
    agent_id = await _seed_agent(db_path)  # password "hunter2", no oauth2
    row = {"agent_id": agent_id, "id": "op_x"}
    creds = await resolve_imap_credentials(row, db_path)
    assert creds.host == "imap.example.com"
    assert creds.user == "u@example.com"
    assert creds.password == "hunter2"
    assert creds.oauth2_provider is None
    assert creds.cred is not None


async def test_resolve_imap_credentials_env_fallback(db_path: Path, monkeypatch) -> None:
    """With no agent, the NUVRAIL_TEST_IMAP_* env vars are used."""
    monkeypatch.setenv("NUVRAIL_TEST_IMAP_HOST", "fallback.example.com")
    monkeypatch.setenv("NUVRAIL_TEST_IMAP_USER", "envuser@example.com")
    monkeypatch.setenv("NUVRAIL_TEST_IMAP_PASS", "envpass")
    creds = await resolve_imap_credentials({"agent_id": None, "id": "op_y"}, db_path)
    assert creds.host == "fallback.example.com"
    assert creds.user == "envuser@example.com"
    assert creds.password == "envpass"
    assert creds.cred is None


async def test_resolve_imap_credentials_missing_raises(db_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NUVRAIL_TEST_IMAP_HOST", raising=False)
    with pytest.raises(RuntimeError, match="no agent_id and no fallback"):
        await resolve_imap_credentials({"agent_id": None, "id": "op_z"}, db_path)


class _FakeIMAP:
    """Minimal aioimaplib.IMAP4_SSL stand-in recording append() calls."""

    def __init__(self, *args, **kwargs) -> None:
        self.appends: list[tuple[bytes, object, object]] = []
        self.logged_in = False

    async def wait_hello_from_server(self) -> None:
        return None

    async def login(self, user: str, password: str):
        self.logged_in = True
        return "OK", [b"LOGIN completed"]

    async def append(self, message_bytes, mailbox=None, flags=None):
        self.appends.append((message_bytes, mailbox, flags))
        return "OK", [b"APPEND completed"]

    async def logout(self):
        return "OK", [b"BYE"]


async def test_append_op_replayed_upstream(db_path: Path) -> None:
    """An APPEND op with a stored body is appended to the target folder."""
    agent_id = await _seed_agent(db_path)
    raw = b"From: a@b.com\r\nSubject: hi\r\n\r\nbody"
    op_id = await create_operation(
        op_type="append",
        protocol="imap",
        description="Save to Sent",
        agent_id=agent_id,
        folder_to="Sent",
        flags_add=["\\Seen"],
        append_message=base64.b64encode(raw).decode("ascii"),
        db_path=db_path,
    )

    # Round-trip: the body is persisted.
    row = await get_operation(op_id, db_path=db_path)
    assert row["append_message"] == base64.b64encode(raw).decode("ascii")

    fake = _FakeIMAP()
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        await _execute_imap_upstream(row, db_path)

    assert len(fake.appends) == 1
    sent_bytes, mailbox, flags = fake.appends[0]
    assert sent_bytes == raw, "exact original bytes must be appended"
    assert "Sent" in str(mailbox)
    assert flags == "(\\Seen)"


async def test_append_op_without_body_is_skipped(db_path: Path) -> None:
    """A legacy APPEND op with no stored body is a no-op — no upstream connection."""
    agent_id = await _seed_agent(db_path)
    op_id = await create_operation(
        op_type="append",
        protocol="imap",
        description="Legacy append (no body)",
        agent_id=agent_id,
        folder_to="Sent",
        append_message=None,
        db_path=db_path,
    )
    row = await get_operation(op_id, db_path=db_path)

    with patch("gateway.execution.aioimaplib.IMAP4_SSL") as ctor:
        await _execute_imap_upstream(row, db_path)
        ctor.assert_not_called()


async def test_append_upstream_failure_raises(db_path: Path) -> None:
    """A non-OK APPEND response raises so the caller can mark the op failed."""
    agent_id = await _seed_agent(db_path)
    op_id = await create_operation(
        op_type="append",
        protocol="imap",
        description="Save to Sent",
        agent_id=agent_id,
        folder_to="Sent",
        append_message=base64.b64encode(b"raw").decode("ascii"),
        db_path=db_path,
    )
    row = await get_operation(op_id, db_path=db_path)

    class _FailingIMAP(_FakeIMAP):
        async def append(self, message_bytes, mailbox=None, flags=None):
            return "NO", [b"over quota"]

    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=_FailingIMAP()):
        with pytest.raises(RuntimeError, match="APPEND"):
            await _execute_imap_upstream(row, db_path)


# ---------------------------------------------------------------------------
# Trash ops (#168): approved "Move to Trash" is a UID MOVE, not STORE \Deleted
#
#   folder_to set + MOVE cap  → SELECT src; UID MOVE uid "Trash"
#   folder_to unset           → SELECT src; UID STORE +FLAGS (\Deleted)
#   folder_to set, no MOVE    → STORE fallback, folder_to cleared, audited
#   (never EXPUNGE in any branch)
# ---------------------------------------------------------------------------


class _FakeMailboxIMAP(_FakeIMAP):
    """Records select/uid calls; MOVE capability is configurable."""

    def __init__(
        self, *args, move: bool = True, uid_status: str = "OK",
        uid_lines: list | None = None, **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple] = []
        self._move = move
        self._uid_status = uid_status
        self._uid_lines = uid_lines if uid_lines is not None else [b"done"]

    def has_capability(self, capability: str) -> bool:
        return capability.upper() == "MOVE" and self._move

    async def select(self, mailbox):
        self.calls.append(("select", mailbox))
        return "OK", [b"SELECT completed"]

    async def uid(self, *args):
        self.calls.append(("uid", *args))
        return self._uid_status, list(self._uid_lines)

    async def expunge(self):  # pragma: no cover - must never be called
        raise AssertionError("the gateway must never EXPUNGE")


async def _stage_trash(db_path: Path, agent_id: int, *, folder_to: str | None) -> dict:
    op_id = await create_operation(
        op_type="trash",
        protocol="imap",
        description="Move to Trash: 42",
        agent_id=agent_id,
        message_ids=["42"],
        folder_from="INBOX",
        folder_to=folder_to,
        flags_add=["\\Deleted"],
        db_path=db_path,
    )
    return await get_operation(op_id, db_path=db_path)


async def test_trash_with_trash_folder_executes_as_uid_move(db_path: Path) -> None:
    agent_id = await _seed_agent(db_path)
    row = await _stage_trash(db_path, agent_id, folder_to="Deleted Items")

    fake = _FakeMailboxIMAP(move=True)
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        detail = await _execute_imap_upstream(row, db_path)

    assert detail is None
    assert fake.calls == [
        ("select", '"INBOX"'),
        ("uid", "move", "42", '"Deleted Items"'),  # quoted: the name has a space
    ]


async def test_trash_without_trash_folder_falls_back_to_store_deleted(db_path: Path) -> None:
    """No Trash folder known at staging → the flag the agent asked for."""
    agent_id = await _seed_agent(db_path)
    row = await _stage_trash(db_path, agent_id, folder_to=None)

    fake = _FakeMailboxIMAP(move=True)
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        detail = await _execute_imap_upstream(row, db_path)

    assert detail is None  # staged + labelled as "Mark deleted" already
    assert fake.calls == [
        ("select", '"INBOX"'),
        ("uid", "store", "42", "+FLAGS", "(\\Deleted)"),
    ]


async def test_trash_without_move_capability_falls_back_and_clears_folder_to(
    db_path: Path,
) -> None:
    agent_id = await _seed_agent(db_path)
    row = await _stage_trash(db_path, agent_id, folder_to="Trash")

    fake = _FakeMailboxIMAP(move=False)
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        detail = await _execute_imap_upstream(row, db_path)

    assert fake.calls == [
        ("select", '"INBOX"'),
        ("uid", "store", "42", "+FLAGS", "(\\Deleted)"),
    ]
    assert detail == {"trash_fallback": "no_move_capability", "trash_folder": "Trash"}
    # Nothing went to Trash, so undo must not try to move anything back out.
    assert (await get_operation(row["id"], db_path=db_path))["folder_to"] is None


async def test_trash_move_failure_raises(db_path: Path) -> None:
    """A NO on the MOVE (e.g. [TRYCREATE] for a wrong folder) fails the op
    rather than silently falling back to a flag the human didn't approve."""
    agent_id = await _seed_agent(db_path)
    row = await _stage_trash(db_path, agent_id, folder_to="Trash")

    fake = _FakeMailboxIMAP(move=True, uid_status="NO")
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        with pytest.raises(RuntimeError, match="UID MOVE to Trash"):
            await _execute_imap_upstream(row, db_path)
    assert [c for c in fake.calls if c[0] == "uid" and c[1] == "store"] == []


async def test_trash_fallback_is_recorded_in_executed_audit_row(db_path: Path) -> None:
    agent_id = await _seed_agent(db_path)
    row = await _stage_trash(db_path, agent_id, folder_to="Trash")

    fake = _FakeMailboxIMAP(move=False)
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        await execute_operation(row["id"], row, db_path)

    async with get_db(db_path) as db, db.execute(
        "SELECT detail FROM audit_log WHERE operation_id = ? AND event = 'executed'",
        (row["id"],),
    ) as cur:
        audit = await cur.fetchone()
    assert audit is not None
    assert json.loads(audit["detail"]) == {
        "trash_fallback": "no_move_capability",
        "trash_folder": "Trash",
    }


# ---------------------------------------------------------------------------
# Destination UIDs (#170): a UID MOVE records where the messages landed
#
#   UID MOVE 42 "Archive"
#     S: * OK [COPYUID 1706723021 42 7] Moved UIDs.     ← untagged (Dovecot)
#     S: * 1 EXPUNGE
#     S: A1 OK Move completed
#   aioimaplib strips "* ", so Response.lines holds b"OK [COPYUID ...] ...".
#   Some servers put COPYUID on the tagged OK instead (RFC 4315 example):
#     S: A3 OK [COPYUID 38505 304,319:320 3956:3958] Done
# ---------------------------------------------------------------------------

DOVECOT_MOVE_LINES = [
    b"OK [COPYUID 1706723021 42 7] Moved UIDs.",
    b"1 EXPUNGE",
    b"Move completed (0.002 + 0.000 secs).",
]


def test_parse_copyuid_untagged_dovecot_form() -> None:
    assert parse_copyuid(DOVECOT_MOVE_LINES) == (1706723021, "7")


def test_parse_copyuid_tagged_rfc4315_form_with_ranges() -> None:
    assert parse_copyuid([b"[COPYUID 38505 304,319:320 3956:3958] Done"]) == (
        38505, "3956:3958",
    )


def test_parse_copyuid_joins_split_responses() -> None:
    """RFC 6851 lets a server report one MOVE as several COPYUID codes."""
    lines = [
        b"OK [COPYUID 99 10 500] Moved",
        b"OK [COPYUID 99 11:12 501:502] Moved",
        b"Done",
    ]
    assert parse_copyuid(lines) == (99, "500,501:502")


def test_parse_copyuid_absent_or_inconsistent_returns_none() -> None:
    assert parse_copyuid([b"done"]) is None                     # no UIDPLUS
    assert parse_copyuid([]) is None
    assert parse_copyuid(None) is None
    assert parse_copyuid([                                      # UIDVALIDITY mismatch
        b"OK [COPYUID 1 10 500] Moved", b"OK [COPYUID 2 11 501] Moved",
    ]) is None


def test_parse_copyuid_accepts_str_lines() -> None:
    assert parse_copyuid(["OK [copyuid 5 1 2] moved"]) == (5, "2")


def test_parse_uidvalidity_from_select_response() -> None:
    lines = [
        b"3 EXISTS",
        b"0 RECENT",
        b"OK [UIDVALIDITY 3857529045] UIDs valid",
        b"OK [UIDNEXT 4392] Predicted next UID",
        b"[READ-WRITE] Select completed.",
    ]
    assert parse_uidvalidity(lines) == 3857529045
    assert parse_uidvalidity([b"[READ-WRITE] Select completed."]) is None


async def _stage_move(db_path: Path, agent_id: int) -> dict:
    op_id = await create_operation(
        op_type="move", protocol="imap", description="Move 42 to Archive",
        agent_id=agent_id, message_ids=["42"], folder_from="INBOX",
        folder_to="Archive", db_path=db_path,
    )
    return await get_operation(op_id, db_path=db_path)


async def test_move_returns_destination_uids_from_copyuid(db_path: Path) -> None:
    agent_id = await _seed_agent(db_path)
    row = await _stage_move(db_path, agent_id)

    fake = _FakeMailboxIMAP(uid_lines=DOVECOT_MOVE_LINES)
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        detail = await _execute_imap_upstream(row, db_path)

    assert fake.calls == [("select", '"INBOX"'), ("uid", "move", "42", '"Archive"')]
    assert detail == {"dest_uidvalidity": 1706723021, "dest_uids": "7"}


async def test_move_without_copyuid_records_no_destination(db_path: Path) -> None:
    """No UIDPLUS → nothing to record; undo will refuse rather than guess."""
    agent_id = await _seed_agent(db_path)
    row = await _stage_move(db_path, agent_id)

    fake = _FakeMailboxIMAP(uid_lines=[b"Move completed"])
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        assert await _execute_imap_upstream(row, db_path) is None


async def test_trash_move_returns_destination_uids(db_path: Path) -> None:
    agent_id = await _seed_agent(db_path)
    row = await _stage_trash(db_path, agent_id, folder_to="Deleted Items")

    fake = _FakeMailboxIMAP(uid_lines=[b"[COPYUID 77 42 9001] Done"])
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        detail = await _execute_imap_upstream(row, db_path)

    assert detail == {"dest_uidvalidity": 77, "dest_uids": "9001"}


async def test_move_destination_is_recorded_in_executed_audit_row(db_path: Path) -> None:
    agent_id = await _seed_agent(db_path)
    row = await _stage_move(db_path, agent_id)

    fake = _FakeMailboxIMAP(uid_lines=DOVECOT_MOVE_LINES)
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        await execute_operation(row["id"], row, db_path)

    async with get_db(db_path) as db, db.execute(
        "SELECT detail FROM audit_log WHERE operation_id = ? AND event = 'executed'",
        (row["id"],),
    ) as cur:
        audit = await cur.fetchone()
    assert json.loads(audit["detail"]) == {"dest_uidvalidity": 1706723021, "dest_uids": "7"}


# ---------------------------------------------------------------------------
# Unknown op_type (#174): fail closed. An op_type with no executor branch
# must end 'failed' with an execution_failed audit row, never 'executed'.
# ---------------------------------------------------------------------------


async def test_unknown_op_type_fails_closed(db_path: Path) -> None:
    agent_id = await _seed_agent(db_path)
    op_id = await create_operation(
        op_type="archive",
        protocol="imap",
        description="Archive 42",
        agent_id=agent_id,
        message_ids=["42"],
        folder_from="INBOX",
        folder_to="Archive",
        db_path=db_path,
    )
    row = await get_operation(op_id, db_path=db_path)

    fake = _FakeMailboxIMAP()
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake):
        with pytest.raises(ExecutionError, match="Unsupported op_type 'archive'"):
            await execute_operation(op_id, row, db_path)

    # Nothing mutating was sent upstream.
    assert [c for c in fake.calls if c[0] == "uid"] == []
    assert (await get_operation(op_id, db_path=db_path))["status"] == "failed"
    async with get_db(db_path) as db, db.execute(
        "SELECT event FROM audit_log WHERE operation_id = ? ORDER BY id",
        (op_id,),
    ) as cur:
        events = [r["event"] for r in await cur.fetchall()]
    assert "execution_failed" in events
    assert "executed" not in events


# ---------------------------------------------------------------------------
# authenticate_upstream_imap — the one upstream login, shared with undo
# ---------------------------------------------------------------------------


class _AuthFakeIMAP:
    """Records the auth calls; ``xoauth2_lines`` is the AUTHENTICATE reply."""

    def __init__(self, *, xoauth2_result: str = "OK", xoauth2_lines: list | None = None) -> None:
        self.calls: list[tuple] = []
        self._xoauth2_result = xoauth2_result
        self._xoauth2_lines = (
            xoauth2_lines if xoauth2_lines is not None
            else [b"CAPABILITY IMAP4rev1 UIDPLUS MOVE", b"u@example.com authenticated (Success)"]
        )
        self.protocol = SimpleNamespace(capabilities=set())

    async def wait_hello_from_server(self) -> None:
        self.calls.append(("hello",))

    async def login(self, user, password):
        self.calls.append(("login", user))
        return "OK", [b"ok"]

    async def xoauth2(self, user, token):
        self.calls.append(("xoauth2", user, token))

        return SimpleNamespace(result=self._xoauth2_result, lines=self._xoauth2_lines)

    async def capability(self):
        self.calls.append(("capability",))
        self.protocol.capabilities = {"IMAP4rev1", "MOVE"}
        return "OK", [b"ok"]


def _creds(*, oauth2: bool, password: str | None) -> ImapCredentials:
    return ImapCredentials(
        host="imap.example.com", port=993, user="u@example.com", password=password,
        oauth2_provider="google" if oauth2 else None,
        cred={"id": 9} if oauth2 or password else None,
    )


async def test_auth_password_agent_uses_login(db_path: Path) -> None:
    fake = _AuthFakeIMAP()
    await authenticate_upstream_imap(fake, _creds(oauth2=False, password="pw"), db_path)
    assert fake.calls == [("hello",), ("login", "u@example.com")]


async def test_auth_oauth2_agent_uses_xoauth2_and_seeds_capabilities(db_path: Path) -> None:
    fake = _AuthFakeIMAP()
    with patch(
        "gateway.oauth2_tokens.get_access_token", return_value=("u@example.com", "tok")
    ) as get_token:
        await authenticate_upstream_imap(fake, _creds(oauth2=True, password=None), db_path)
    get_token.assert_awaited_once_with("9", db_path)
    assert fake.calls == [("hello",), ("xoauth2", "u@example.com", "tok")]
    # Seeded from the AUTHENTICATE reply, so UID MOVE is allowed without a round trip.
    assert "MOVE" in fake.protocol.capabilities


async def test_auth_oauth2_without_capability_line_fetches_it(db_path: Path) -> None:
    fake = _AuthFakeIMAP(xoauth2_lines=[b"authenticated (Success)"])
    with patch("gateway.oauth2_tokens.get_access_token", return_value=("u@example.com", "t")):
        await authenticate_upstream_imap(fake, _creds(oauth2=True, password=None), db_path)
    assert fake.calls[-1] == ("capability",)
    assert "MOVE" in fake.protocol.capabilities


async def test_auth_oauth2_token_failure_raises_before_authenticate(db_path: Path) -> None:
    from gateway.oauth2_tokens import OAuth2Error

    fake = _AuthFakeIMAP()
    with patch(
        "gateway.oauth2_tokens.get_access_token",
        side_effect=OAuth2Error("Google token refresh failed: HTTP 400 error='invalid_grant'"),
    ), pytest.raises(RuntimeError, match="OAuth2 token fetch failed"):
        await authenticate_upstream_imap(fake, _creds(oauth2=True, password=None), db_path)
    assert [c[0] for c in fake.calls] == ["hello"]


async def test_auth_oauth2_rejected_raises(db_path: Path) -> None:
    fake = _AuthFakeIMAP(xoauth2_result="NO", xoauth2_lines=[b"Invalid credentials"])
    with patch(
        "gateway.oauth2_tokens.get_access_token", return_value=("u@example.com", "t")
    ), pytest.raises(RuntimeError, match="XOAUTH2 authentication failed"):
        await authenticate_upstream_imap(fake, _creds(oauth2=True, password=None), db_path)


async def test_auth_without_password_or_oauth2_fails_closed(db_path: Path) -> None:
    """No password and no OAuth2 provider: refuse without sending LOGIN."""
    fake = _AuthFakeIMAP()
    with pytest.raises(RuntimeError, match="needs a password"):
        await authenticate_upstream_imap(fake, _creds(oauth2=False, password=""), db_path)
    assert ("login", "u@example.com") not in fake.calls


async def test_execute_oauth2_agent_moves_via_xoauth2(db_path: Path) -> None:
    """The forward executor still authenticates OAuth2 agents through the helper."""
    async with get_db(db_path) as db:
        await db.execute(
            """INSERT INTO users (id, email, display_name, hashed_password, created_at)
               VALUES (1, 'o@example.com', 'T', 'x', 0)"""
        )
        cur = await db.execute(
            """INSERT INTO agent_credentials
               (user_id, label, agent_username, hashed_token,
                upstream_host, upstream_imap_port, upstream_smtp_port,
                upstream_user, upstream_password, oauth2_provider, created_at)
               VALUES (1, 'l', 'nuvrail_o', 'x', 'imap.gmail.com', 993, 587,
                       'o@example.com', NULL, 'google', 0)"""
        )
        await db.commit()
        agent_id = int(cur.lastrowid)

    class _MoveFake(_AuthFakeIMAP):
        def has_capability(self, cap):
            return cap in self.protocol.capabilities

        async def select(self, folder):
            self.calls.append(("select", folder))
            return "OK", [b"OK [UIDVALIDITY 77] ok"]

        async def uid(self, *args):
            self.calls.append(("uid", *args))
            return "OK", [b"OK [COPYUID 77 42 9] Moved."]

        async def logout(self):
            self.calls.append(("logout",))
            return "OK", [b"bye"]

    fake = _MoveFake()
    row = {
        "id": "op_oauth", "agent_id": agent_id, "op_type": "move",
        "folder_from": "INBOX", "folder_to": "Archive", "message_ids": '["42"]',
    }
    with patch("gateway.execution.aioimaplib.IMAP4_SSL", return_value=fake), patch(
        "gateway.oauth2_tokens.get_access_token", return_value=("o@example.com", "tok")
    ):
        result = await _execute_imap_upstream(row, db_path)

    assert ("xoauth2", "o@example.com", "tok") in fake.calls
    assert not any(c[0] == "login" for c in fake.calls)
    assert result == {"dest_uidvalidity": 77, "dest_uids": "9"}
    assert fake.calls[-1] == ("logout",)
