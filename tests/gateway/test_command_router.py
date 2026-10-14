"""
Unit tests for IMAP command classifier (Milestone 0.2).

Covers: every READ, WRITE, BLOCKED command; fail-closed rejection of
AUTHENTICATE/LOGIN (post-auth) and unknown/extension verbs; the IDLE "DONE"
line; UID prefix does not affect classification; capability filtering and
non-synchronizing literal detection.
"""

import pytest

from gateway.command_router import (
    BLOCKED_COMMANDS,
    READ_COMMANDS,
    WRITE_COMMANDS,
    classify,
    filter_capabilities,
    nonsync_literal_size,
)
from gateway.imap_parser import ParsedCommand


def _cmd(command: str, uid: bool = False) -> ParsedCommand:
    """Helper: build a minimal ParsedCommand for classification testing."""
    return ParsedCommand(tag="A001", command=command, uid=uid, args=[], raw="")


# ---------------------------------------------------------------------------
# All READ_COMMANDS → "read"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", sorted(READ_COMMANDS))
def test_read_commands(command: str) -> None:
    assert classify(_cmd(command)) == "read", f"Expected 'read' for {command!r}"


# ---------------------------------------------------------------------------
# All WRITE_COMMANDS → "write"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", sorted(WRITE_COMMANDS))
def test_write_commands(command: str) -> None:
    assert classify(_cmd(command)) == "write", f"Expected 'write' for {command!r}"


# ---------------------------------------------------------------------------
# All BLOCKED_COMMANDS → "blocked"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", sorted(BLOCKED_COMMANDS))
def test_blocked_commands(command: str) -> None:
    assert classify(_cmd(command)) == "blocked", f"Expected 'blocked' for {command!r}"


# ---------------------------------------------------------------------------
# Fail closed: anything not enumerated is "rejected", never forwarded
# ---------------------------------------------------------------------------
#
# Every RFC verb we know of has a pinned class here. Moving one (especially
# into "read") is a security decision and must show up as a diff to this
# table. The write-capable extension verbs are the reason the classifier
# fails closed: forwarding any of them changes the real mailbox without
# human approval.

KNOWN_VERB_CLASSES: list[tuple[str, str, str]] = [
    # (verb, expected class, why)
    ("REPLACE", "rejected", "RFC 8508: APPEND + EXPUNGE of the old message"),
    ("SETACL", "rejected", "RFC 4314: shares the mailbox with another account"),
    ("DELETEACL", "rejected", "RFC 4314: changes mailbox sharing"),
    ("SETMETADATA", "rejected", "RFC 5464: writes mailbox/server annotations"),
    ("SETQUOTA", "rejected", "RFC 9208: changes quota"),
    ("COMPRESS", "rejected", "RFC 4978: deflate framing the classifier can't read"),
    ("STARTTLS", "rejected", "TLS upgrade after login"),
    ("AUTHENTICATE", "rejected", "handled before the pumps; never forwarded after"),
    ("LOGIN", "rejected", "handled before the pumps; never forwarded after"),
    ("NOTIFY", "rejected", "RFC 5465: not reviewed"),
    ("URLFETCH", "rejected", "RFC 4467: not reviewed"),
    ("RESETKEY", "rejected", "RFC 4467: not reviewed"),
    ("GENURLAUTH", "rejected", "RFC 4467: not reviewed"),
    ("XALERT", "rejected", "fictional extension"),
    ("UNKNOWN_CMD", "rejected", "unknown"),
    ("ENABLE", "read", "RFC 5161: response extensions only"),
    ("SORT", "read", "RFC 5256"),
    ("THREAD", "read", "RFC 5256"),
    ("ESEARCH", "read", "RFC 7377"),
    ("GETQUOTA", "read", "RFC 9208"),
    ("GETQUOTAROOT", "read", "RFC 9208"),
    ("GETACL", "read", "RFC 4314"),
    ("MYRIGHTS", "read", "RFC 4314"),
    ("LISTRIGHTS", "read", "RFC 4314"),
    ("GETMETADATA", "read", "RFC 5464"),
    ("XLIST", "read", "legacy Gmail LIST"),
]


@pytest.mark.parametrize("command,expected,why", KNOWN_VERB_CLASSES)
def test_known_verb_classes(command: str, expected: str, why: str) -> None:
    assert classify(_cmd(command)) == expected, f"{command} ({why})"


@pytest.mark.parametrize("command", ["REPLACE", "SETACL", "COMPRESS", "XALERT"])
def test_uid_prefix_does_not_unlock_rejected_verbs(command: str) -> None:
    assert classify(_cmd(command, uid=True)) == "rejected"


def test_empty_command_is_rejected() -> None:
    """A tag with no verb (malformed) is not forwarded."""
    assert classify(ParsedCommand(tag="A001", command="", raw="A001")) == "rejected"


@pytest.mark.parametrize("line", ["DONE", "done", "Done"])
def test_idle_done_line_is_forwarded(line: str) -> None:
    """RFC 2177: the client ends IDLE with a bare DONE; it must reach upstream."""
    assert classify(ParsedCommand(tag=line, command="", raw=line)) == "read"


def test_read_set_has_no_write_capable_verbs() -> None:
    """Backstop: none of the write-capable extension verbs may be allowlisted."""
    write_capable = {
        "REPLACE", "SETACL", "DELETEACL", "SETMETADATA", "SETQUOTA",
        "COMPRESS", "STARTTLS", "APPEND", "STORE", "COPY", "MOVE",
        "CREATE", "RENAME", "DELETE", "EXPUNGE", "CLOSE",
    }
    assert not (READ_COMMANDS & write_capable)


# ---------------------------------------------------------------------------
# filter_capabilities: hide what the agent must not use
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected", [
    (
        "* CAPABILITY IMAP4rev1 LITERAL+ IDLE COMPRESS=DEFLATE MOVE REPLACE",
        "* CAPABILITY IMAP4rev1 IDLE MOVE",
    ),
    (
        "* capability IMAP4rev1 literal- STARTTLS UIDPLUS",
        "* capability IMAP4rev1 UIDPLUS",
    ),
    (
        "* CAPABILITY IMAP4rev1 IMAP4rev2 IDLE",
        "* CAPABILITY IMAP4rev1 IDLE",
    ),
    (
        "a1 OK [CAPABILITY IMAP4rev1 LITERAL+ SPECIAL-USE COMPRESS=DEFLATE] Logged in",
        "a1 OK [CAPABILITY IMAP4rev1 SPECIAL-USE] Logged in",
    ),
    ("* OK [CAPABILITY IMAP4rev1 IDLE] ready", "* OK [CAPABILITY IMAP4rev1 IDLE] ready"),
    ("* 3 EXISTS", "* 3 EXISTS"),
    ("* CAPABILITY IMAP4rev1 ACL QUOTA", "* CAPABILITY IMAP4rev1 ACL QUOTA"),
])
def test_filter_capabilities(line: str, expected: str) -> None:
    assert filter_capabilities(line) == expected


# ---------------------------------------------------------------------------
# nonsync_literal_size: detect {N+} / {N-} at end of line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("a1 REPLACE 4 INBOX {310+}", 310),
    ("a1 APPEND INBOX {12-}", 12),
    ("a1 APPEND INBOX UTF8 (~{7+}", 7),
    ("a1 APPEND INBOX {12}", None),       # sync literal: handled by the APPEND path
    ("a1 FETCH 1 (FLAGS)", None),
    ("a1 SEARCH TEXT {5+} extra", None),  # not at end of line: not a literal
])
def test_nonsync_literal_size(line: str, expected: int | None) -> None:
    assert nonsync_literal_size(line) == expected


# ---------------------------------------------------------------------------
# UID prefix does not change classification
# ---------------------------------------------------------------------------

UID_CLASSIFICATION_CASES: list[tuple[str, str]] = [
    # (command, expected_classification)
    ("FETCH", "read"),
    ("SEARCH", "read"),
    ("STORE", "write"),
    ("MOVE", "write"),
    ("COPY", "write"),
    ("EXPUNGE", "blocked"),
]


@pytest.mark.parametrize("command,expected", UID_CLASSIFICATION_CASES)
def test_uid_prefix_does_not_change_classification(command: str, expected: str) -> None:
    """UID FETCH is still 'read', UID STORE is still 'write', etc."""
    result = classify(_cmd(command, uid=True))
    assert result == expected, (
        f"UID {command}: expected {expected!r}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Explicit spot-checks for a few important commands
# ---------------------------------------------------------------------------

def test_select_is_read() -> None:
    assert classify(_cmd("SELECT")) == "read"


def test_examine_is_read() -> None:
    assert classify(_cmd("EXAMINE")) == "read"


def test_logout_is_read() -> None:
    assert classify(_cmd("LOGOUT")) == "read"


def test_store_is_write() -> None:
    assert classify(_cmd("STORE")) == "write"


def test_append_is_write() -> None:
    assert classify(_cmd("APPEND")) == "write"


def test_expunge_is_blocked() -> None:
    assert classify(_cmd("EXPUNGE")) == "blocked"


def test_delete_is_blocked() -> None:
    assert classify(_cmd("DELETE")) == "blocked"


# ---------------------------------------------------------------------------
# Regression: CLOSE must be BLOCKED, not forwarded (covert-EXPUNGE guard)
# ---------------------------------------------------------------------------
#
# RFC 3501 §6.4.2: CLOSE permanently expunges every \Deleted message in the
# selected mailbox, THEN returns to the authenticated state. If CLOSE is
# forwarded upstream it silently expunges mail the human's other clients
# flagged \Deleted — a covert EXPUNGE path that breaks the core invariant
# "EXPUNGE never reaches upstream." It must classify as 'blocked' so the proxy
# answers OK locally and never forwards it.

def test_close_is_blocked() -> None:
    """CLOSE side-effect-expunges (RFC 3501) — it must never be forwarded."""
    assert classify(_cmd("CLOSE")) == "blocked", (
        "CLOSE expunges \\Deleted messages upstream per RFC 3501 §6.4.2; "
        "classifying it as anything but 'blocked' is a covert EXPUNGE path."
    )


def test_close_is_in_blocked_set() -> None:
    """Guard the enumeration itself, not just the classify() result."""
    assert "CLOSE" in BLOCKED_COMMANDS


def test_uid_close_is_blocked() -> None:
    """UID prefix must not let CLOSE slip through to 'read'."""
    assert classify(_cmd("CLOSE", uid=True)) == "blocked"


# ---------------------------------------------------------------------------
# UNSELECT (RFC 3691) is the SAFE way to leave a mailbox — no expunge → read
# ---------------------------------------------------------------------------

def test_unselect_is_read() -> None:
    """UNSELECT closes the mailbox WITHOUT expunging (RFC 3691) — safe to forward."""
    assert classify(_cmd("UNSELECT")) == "read"


# ---------------------------------------------------------------------------
# Invariant guard: no expunge-capable verb may be classified 'read'
# ---------------------------------------------------------------------------
#
# Any RFC IMAP command that can expunge \Deleted mail as a side effect must be
# in BLOCKED_COMMANDS. This test is the backstop against a future edit
# accidentally moving one of these into READ_COMMANDS.

@pytest.mark.parametrize("command", ["EXPUNGE", "CLOSE"])
def test_expunge_capable_commands_never_read(command: str) -> None:
    assert command not in READ_COMMANDS
    assert command not in WRITE_COMMANDS
    assert classify(_cmd(command)) == "blocked", (
        f"{command} can expunge upstream and must be 'blocked', not forwarded."
    )
