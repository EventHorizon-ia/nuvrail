"""Lock the 3am demo (docs/assets/3am-demo/) to what the gateway really does.

The demo video and README GIF make three claims about agent.py's traffic.
These tests replay the script against a fake IMAP server, capture the exact
bytes imaplib puts on the wire, and push each line through the REAL
gateway parser + classifier:

    agent.py ──wire──► FakeIMAP (records lines, answers like the proxy)
                          │
                          └─► gateway.imap_parser.parse_line
                                 └─► gateway.command_router.classify
                                        UID SEARCH  -> read
                                        UID STORE   -> write  (staged)
                                        EXPUNGE     -> blocked (never forwarded)

If someone changes the script or the classifier so the demo stops being true,
this fails instead of the video quietly lying.
"""
from __future__ import annotations

import importlib.util
import socketserver
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from gateway.command_router import classify
from gateway.imap_parser import parse_line

DEMO_DIR = Path(__file__).resolve().parents[2] / "docs" / "assets" / "3am-demo"
STAGED_LINE = "OK [STAGED] Operation queued — ID: op_demo123"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"demo_{name}", DEMO_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProxy(socketserver.StreamRequestHandler):
    """Answers the handful of commands agent.py sends, the way the proxy would."""

    received: ClassVar[list[str]] = []

    def _send(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8") + b"\r\n")

    def handle(self) -> None:
        self._send("* OK [CAPABILITY IMAP4rev1 UIDPLUS] fake nuvrail")
        for raw in self.rfile:
            line = raw.decode("utf-8").rstrip("\r\n")
            _FakeProxy.received.append(line)
            tag, _, rest = line.partition(" ")
            verb = rest.upper()
            if verb.startswith("CAPABILITY"):
                self._send("* CAPABILITY IMAP4rev1 UIDPLUS")
                self._send(f"{tag} OK done")
            elif verb.startswith("LOGIN"):
                self._send(f"{tag} OK logged in")
            elif verb.startswith("SELECT"):
                self._send("* 40 EXISTS")
                self._send(f"{tag} OK [READ-WRITE] selected")
            elif verb.startswith("UID SEARCH"):
                self._send("* SEARCH " + " ".join(str(n) for n in range(1, 39)))
                self._send(f"{tag} OK search done")
            elif verb.startswith("UID STORE"):
                self._send(f"{tag} {STAGED_LINE}")
            elif verb.startswith("EXPUNGE"):
                self._send(f"{tag} OK Noted")
            elif verb.startswith("LOGOUT"):
                self._send("* BYE")
                self._send(f"{tag} OK bye")
                return
            else:
                self._send(f"{tag} BAD unexpected in demo test")


@pytest.fixture
def fake_proxy(monkeypatch):
    _FakeProxy.received = []
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeProxy)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("NUVRAIL_HOST", "127.0.0.1")
    monkeypatch.setenv("NUVRAIL_PORT", str(server.server_address[1]))
    monkeypatch.setenv("DEMO_ALLOW_ANY_SERVER", "1")  # test port is not 10143
    monkeypatch.setenv("NUVRAIL_AGENT_USER", "nuvrail_demo")
    monkeypatch.setenv("NUVRAIL_AGENT_TOKEN", "not-a-real-token")
    yield _FakeProxy.received
    server.shutdown()
    server.server_close()


def _classified(lines: list[str]) -> dict[str, str]:
    """Map 'UID STORE' / 'EXPUNGE' / ... to the gateway's class for that wire line."""
    out = {}
    for line in lines:
        cmd = parse_line(line)
        assert cmd is not None, f"gateway parser rejected demo line: {line!r}"
        key = f"UID {cmd.command}" if cmd.uid else cmd.command
        out[key] = classify(cmd)
    return out


def test_agent_wire_traffic_is_classified_as_the_demo_claims(fake_proxy, capsys):
    agent = _load("agent")
    assert agent.main() == 0

    classes = _classified(fake_proxy)
    assert classes["UID SEARCH"] == "read"
    assert classes["UID STORE"] == "write"  # staged for approval
    assert classes["EXPUNGE"] == "blocked"  # answered locally, never forwarded
    # The proxy rejects non-UID STORE; the demo must not rely on it.
    assert "STORE" not in classes

    store = next(ln for ln in fake_proxy if " UID STORE " in ln.upper())
    assert store.endswith("UID STORE 1:38 +FLAGS (\\Deleted)")

    out = capsys.readouterr().out
    assert '38 messages "resolved"' in out
    assert STAGED_LINE in out  # printed verbatim, op id included
    assert "OK Noted" in out


def test_agent_refuses_non_proxy_server_without_override(monkeypatch, capsys):
    monkeypatch.setenv("NUVRAIL_HOST", "imap.example.com")
    monkeypatch.setenv("NUVRAIL_PORT", "993")
    monkeypatch.delenv("DEMO_ALLOW_ANY_SERVER", raising=False)
    assert _load("agent").main() == 2
    assert "refusing" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("uids", "expected"),
    [([b"1", b"2", b"3"], "1:3"), ([b"7"], "7:7"), ([b"3", b"7", b"9"], "3,7,9")],
)
def test_uid_set(uids, expected):
    assert _load("agent").uid_set(uids) == expected


def test_seed_plan_matches_the_agent_search(monkeypatch):
    """38 of 40 seeded messages match SEEN BEFORE 01-Sep-2026, incl. the 3 the VO names."""
    monkeypatch.setenv("SEED_IMAP_USER", "demo@example.com")
    seed = _load("seed_mailbox")
    rows = seed.plan()
    cutoff = datetime(2026, 9, 1, tzinfo=UTC)
    matched = [s for s, when, flags in rows if "\\Seen" in flags and when < cutoff]
    assert len(rows) == 40
    assert len(matched) == 38
    assert set(seed.KEY_SUBJECTS) <= set(matched)
    assert b"Subject:" in seed.message(rows[0][0], rows[0][1])
