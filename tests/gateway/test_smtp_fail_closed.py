"""
SMTP proxy fails closed after authentication.

Only enumerated session verbs reach upstream. BDAT (RFC 3030 CHUNKING) is the
one that matters most: it submits a message without DATA, so forwarding it
would skip send staging entirely (``BDAT 0 LAST`` after MAIL/RCPT delivers an
empty message to arbitrary recipients on a CHUNKING-capable upstream).

Harness: handle_smtp_client driven with a hand-fed client reader. Agent auth,
the upstream STARTTLS connect, and the credential fetch are monkeypatched so
the "upstream" is a pair of in-memory streams we can inspect.

    agent ──► [client reader] ──► handle_smtp_client ──► [upstream FakeWriter]
                                        ▲
                     [upstream reader: canned 250/235 replies]
"""
from __future__ import annotations

import asyncio
import base64

import pytest

import gateway.smtp_proxy as smtp_proxy


class FakeWriter:
    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def get_extra_info(self, name: str, default=None):
        return ("127.0.0.1", 40000) if name == "peername" else default

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def _run(agent_lines: bytes, upstream_replies: bytes, monkeypatch) -> tuple[bytes, bytes]:
    upstream_reader = _reader_with(
        b"250 upstream ehlo\r\n"  # post-STARTTLS EHLO, drained by the proxy
        b"235 2.7.0 ok\r\n"       # upstream AUTH PLAIN
        + upstream_replies
    )
    upstream_writer = FakeWriter()

    async def fake_connect(host, port):
        return upstream_reader, upstream_writer, None

    async def fake_verify(user, password, db_path):
        return {
            "id": 1,
            "user_id": 1,
            "upstream_host": "mail.example.com",
            "upstream_smtp_host": "smtp.example.com",
            "upstream_smtp_port": 587,
            "upstream_user": "owner@example.com",
            "upstream_password": "enc",
            "oauth2_provider": None,
        }

    async def fake_fetch(_):
        return "pw"

    async def no_notices(*_args, **_kwargs):
        return None

    monkeypatch.setattr(smtp_proxy, "_connect_upstream_starttls", fake_connect)
    monkeypatch.setattr(smtp_proxy, "verify_agent_login", fake_verify)
    monkeypatch.setattr(smtp_proxy, "fetch_credential", fake_fetch)
    monkeypatch.setattr(smtp_proxy, "_send_smtp_rejection_notices", no_notices)

    auth = base64.b64encode(b"\x00agent\x00token").decode()
    client = FakeWriter()
    await smtp_proxy.handle_smtp_client(
        _reader_with(f"AUTH PLAIN {auth}\r\n".encode() + agent_lines), client
    )
    # Drop the proxy's own upstream AUTH line; keep what the agent caused.
    sent = upstream_writer.written.split(b"\r\n", 1)[1]
    return sent, client.written


@pytest.mark.parametrize("line", [
    b"BDAT 0 LAST",
    b"BDAT 12",
    b"ETRN example.com",
    b"XCLIENT ADDR=1.2.3.4",
    b"STARTTLS",
    b"XUNKNOWN",
])
async def test_unlisted_smtp_verb_never_reaches_upstream(line: bytes, monkeypatch) -> None:
    sent, client = await _run(line + b"\r\n", b"", monkeypatch)
    assert sent == b""
    verb = line.split()[0].decode()
    assert f"502 5.5.1 Nuvrail: {verb} is not permitted for agents".encode() in client


async def test_session_verbs_still_forwarded(monkeypatch) -> None:
    sent, client = await _run(
        b"NOOP\r\nRSET\r\n", b"250 2.0.0 ok\r\n250 2.0.0 reset\r\n", monkeypatch
    )
    assert sent == b"NOOP\r\nRSET\r\n"
    assert client.endswith(b"250 2.0.0 ok\r\n250 2.0.0 reset\r\n")
