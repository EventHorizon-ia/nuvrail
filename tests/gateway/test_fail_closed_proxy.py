"""
Proxy-level tests for the fail-closed command path.

Drives _client_to_upstream / _upstream_to_client directly with hand-fed
asyncio.StreamReaders and fake writers — no real upstream needed.

    agent bytes ──► [StreamReader] ──► _client_to_upstream ──┬─► upstream FakeWriter
                                                             └─► client FakeWriter (local replies)

The invariant under test: a verb the classifier does not know, and any command
carrying a non-synchronizing literal, never produces a single byte upstream.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gateway.proxy import _MAX_APPEND_BYTES, _client_to_upstream, _upstream_to_client
from gateway.state_db import init_db


class FakeWriter:
    """Collects written bytes; satisfies the write/drain surface the code uses."""

    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass


def _reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _session() -> dict:
    return {
        "folder": None,
        "folder_id": None,
        "select_lines": [],
        "in_select": False,
        "revert_trigger_tag": None,
        "agent_id": 1,
        "user_id": 1,
        "search_uid_mode": False,
        "capability_tag": None,
        "provider_profile": None,
        "pending_copy_intent": None,
        "special_use": None,
    }


@pytest.fixture()
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    await init_db(path)
    return path


async def _run_c2u(data: bytes, db_path: Path) -> tuple[bytes, bytes, dict]:
    upstream, client, session = FakeWriter(), FakeWriter(), _session()
    await _client_to_upstream(
        _reader_with(data), upstream, client, session, peer="test", db_path=db_path
    )
    return upstream.written, client.written, session


@pytest.mark.parametrize("line", [
    b"a1 SETACL INBOX attacker@example.com lrswipkxtea\r\n",
    b"a1 DELETEACL INBOX owner@example.com\r\n",
    b"a1 SETMETADATA INBOX (/private/comment \"x\")\r\n",
    b"a1 SETQUOTA \"\" (STORAGE 1)\r\n",
    b"a1 COMPRESS DEFLATE\r\n",
    b"a1 STARTTLS\r\n",
    b"a1 LOGIN user pass\r\n",
    b"a1 XSOMETHING arg\r\n",
    b"a1 UID REPLACE 4 INBOX x\r\n",
])
async def test_unclassified_verb_never_reaches_upstream(line: bytes, db_path: Path) -> None:
    upstream, client, _ = await _run_c2u(line, db_path)
    assert upstream == b""
    assert client.startswith(b"a1 NO [CANNOT] Nuvrail: ")


async def test_nonsync_literal_is_consumed_and_refused(db_path: Path) -> None:
    """REPLACE with LITERAL+ = APPEND + EXPUNGE upstream. Nothing may be forwarded,
    and the literal body must not be parsed as commands (a body line that looks
    like a read would otherwise be forwarded)."""
    body = b"a9 FETCH 1 (FLAGS)\r\nSubject: hi\r\n\r\nbody\r\n"
    data = (
        b"a1 REPLACE 4 INBOX {" + str(len(body)).encode() + b"+}\r\n" + body + b"\r\n"
        b"a2 NOOP\r\n"
    )
    upstream, client, _ = await _run_c2u(data, db_path)
    # Only the NOOP after the refused command is forwarded.
    assert upstream == b"a2 NOOP\r\n"
    assert client == (
        b"a1 NO [CANNOT] Nuvrail: non-synchronizing literals are not supported, use {N}\r\n"
    )


async def test_chained_nonsync_literals_are_all_consumed(db_path: Path) -> None:
    data = (
        b"a1 APPEND INBOX {3+}\r\nabc (\\Seen) {4-}\r\ndefg\r\n"
        b"a2 NOOP\r\n"
    )
    upstream, client, _ = await _run_c2u(data, db_path)
    assert upstream == b"a2 NOOP\r\n"
    assert client.startswith(b"a1 NO [CANNOT]")


async def test_oversized_nonsync_literal_closes_connection(db_path: Path) -> None:
    data = b"a1 APPEND INBOX {" + str(_MAX_APPEND_BYTES + 1).encode() + b"+}\r\na2 NOOP\r\n"
    upstream, client, _ = await _run_c2u(data, db_path)
    assert upstream == b""
    assert client == b"* BYE Nuvrail: literal too large\r\n"


async def test_reads_and_idle_done_still_forwarded(db_path: Path) -> None:
    data = b"a1 IDLE\r\nDONE\r\na2 UID FETCH 1:* (FLAGS)\r\na3 ENABLE CONDSTORE\r\n"
    upstream, client, _ = await _run_c2u(data, db_path)
    assert upstream == data
    assert client == b""


async def test_blank_line_is_ignored_and_bare_tag_gets_bad(db_path: Path) -> None:
    upstream, client, _ = await _run_c2u(b"\r\na7\r\n", db_path)
    assert upstream == b""
    assert client == b"a7 BAD Nuvrail: missing command\r\n"


async def test_capability_reply_is_filtered_only_while_outstanding(db_path: Path) -> None:
    """The agent's own CAPABILITY reply loses LITERAL+/COMPRESS; once its tagged
    OK has passed, a body line that happens to look like a capability line is
    relayed byte-for-byte."""
    _, _, session = await _run_c2u(b"c1 CAPABILITY\r\n", db_path)
    assert session["capability_tag"] == "C1"

    upstream = _reader_with(
        b"* CAPABILITY IMAP4rev1 LITERAL+ IDLE COMPRESS=DEFLATE\r\n"
        b"c1 OK done\r\n"
        b"* CAPABILITY IMAP4rev1 LITERAL+\r\n"
    )
    client = FakeWriter()
    await _upstream_to_client(upstream, client, session, db_path, peer="test")
    assert client.written == (
        b"* CAPABILITY IMAP4rev1 IDLE\r\n"
        b"c1 OK done\r\n"
        b"* CAPABILITY IMAP4rev1 LITERAL+\r\n"
    )
    assert session["capability_tag"] is None
