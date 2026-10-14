"""
IMAP command classifier (Milestone 0.2).

Determines whether a parsed command is:
  - "read"    → pass through to upstream
  - "write"   → intercept and stage
  - "blocked" → return OK but never forward (EXPUNGE, DELETE folder, CLOSE)

Sub-milestone: 0.2

Core invariant
--------------
The proxy's headline promise is: **an agent can never cause an EXPUNGE to
reach the real mailbox.** Deletion is always staged for human approval; it is
never applied upstream by an agent command. Every command that can *expunge*
mail as a side effect must therefore land in ``BLOCKED_COMMANDS``, not "read".

RFC commands that expunge \\Deleted messages upstream:

    ┌───────────────┬──────────────────────────────────────────────┬──────────┐
    │ Command       │ Expunge behaviour                            │ Class    │
    ├───────────────┼──────────────────────────────────────────────┼──────────┤
    │ EXPUNGE       │ Expunges all \\Deleted in selected mailbox    │ blocked  │
    │ UID EXPUNGE   │ Expunges the given \\Deleted UIDs (RFC 4315)  │ blocked* │
    │ CLOSE         │ Implicitly expunges, THEN unselects (RFC3501)│ blocked  │
    │ UNSELECT      │ Unselects WITHOUT expunging (RFC 3691)        │ read     │
    └───────────────┴──────────────────────────────────────────────┴──────────┘

    * ``UID EXPUNGE`` is parsed with ``command == "EXPUNGE"`` (the parser strips
      the UID prefix), so it is already covered by the EXPUNGE entry.

``CLOSE`` is the subtle one: clients issue it routinely to leave a mailbox, but
per RFC 3501 §6.4.2 it *first* permanently expunges every \\Deleted message,
then returns to the authenticated state. Forwarding it upstream is a covert
EXPUNGE path — it breaks the core invariant whenever the real mailbox already
has \\Deleted messages set by the human's other clients. We block it: the
client receives ``OK`` (its local view is "mailbox closed") and nothing reaches
upstream, so no mail is expunged. ``UNSELECT`` is the safe way to leave a
mailbox (no expunge), so it passes through as a read.

Fail closed
-----------
The broader promise is **every write is staged**. Enumerating the expunge
verbs is not enough for that: IMAP extensions add write-capable verbs that are
neither "read" nor in ``WRITE_COMMANDS`` — ``REPLACE`` (RFC 8508: APPEND +
EXPUNGE of the old message), ``SETACL``/``DELETEACL`` (RFC 4314: share the
mailbox with another account), ``SETMETADATA`` (RFC 5464), ``SETQUOTA``
(RFC 9208), and ``COMPRESS`` (RFC 4978: after it is negotiated the line
classifier would see deflate bytes). An earlier version defaulted unknown verbs
to "read" and forwarded them. It now fails closed:

    verb ──► BLOCKED_COMMANDS? ──yes──► "blocked"  (local OK, never forwarded)
      │
      no
      ▼
    WRITE_COMMANDS?   ──yes──► "write"    (staged for human approval)
      │
      no
      ▼
    READ_COMMANDS?    ──yes──► "read"     (forwarded upstream)
      │
      no
      ▼
    bare "DONE" line? ──yes──► "read"     (ends an IDLE the proxy forwarded)
      │
      no
      ▼
    "rejected"  (local ``NO [CANNOT]``, never forwarded)

A verb belongs in ``READ_COMMANDS`` only if forwarding it cannot change mail,
flags, folders, sharing, or the byte framing of the connection. Adding a verb
there is a security decision; ``tests/gateway/test_command_router.py`` pins the
class of every RFC verb we know of so the change shows up in review.

The proxy also refuses non-synchronizing literals (``{N+}``, RFC 7888) before
classification, and strips ``LITERAL+``/``LITERAL-``/``COMPRESS=*``/``REPLACE``/
``STARTTLS``/``IMAP4rev2`` from capabilities it relays to the agent (see
``filter_capabilities``).
"""

import re

from gateway.imap_parser import ParsedCommand

READ_COMMANDS = {
    "SELECT", "EXAMINE", "FETCH", "SEARCH", "LIST", "LSUB",
    "STATUS", "NOOP", "CAPABILITY", "ID", "LOGOUT", "CHECK",
    "SUBSCRIBE", "UNSUBSCRIBE", "NAMESPACE", "IDLE",
    "UNSELECT",  # RFC 3691: close mailbox WITHOUT expunging — safe to forward.
    # Read-only extension verbs, each checked against its RFC:
    "ENABLE",        # RFC 5161: turns on response extensions (CONDSTORE etc.); no mailbox change.
    "SORT",          # RFC 5256: sorted SEARCH.
    "THREAD",        # RFC 5256: threaded SEARCH.
    "ESEARCH",       # RFC 7377: multi-mailbox SEARCH.
    "GETQUOTA",      # RFC 9208: read quota.
    "GETQUOTAROOT",  # RFC 9208: read quota roots.
    "GETACL",        # RFC 4314: read ACL (SETACL/DELETEACL are rejected).
    "MYRIGHTS",      # RFC 4314: read own rights.
    "LISTRIGHTS",    # RFC 4314: read grantable rights.
    "GETMETADATA",   # RFC 5464: read annotations (SETMETADATA is rejected).
    "XLIST",         # Legacy Gmail LIST variant with folder roles.
}

WRITE_COMMANDS = {
    "STORE", "COPY", "MOVE", "APPEND", "CREATE", "RENAME",
}

BLOCKED_COMMANDS = {
    "EXPUNGE",  # Never forwarded; \Deleted → staged trash move. Covers UID EXPUNGE.
    "DELETE",   # Folder deletion blocked
    "CLOSE",    # RFC 3501: side-effect-expunges \Deleted, THEN unselects → covert EXPUNGE.
}


# Capability atoms the proxy never relays to the agent. Each one either names a
# verb the proxy rejects (so advertising it would only mislead), or changes the
# wire framing in a way the line-based proxy cannot inspect:
#   LITERAL+ / LITERAL-  non-synchronizing literals (RFC 7888), refused by the proxy
#   COMPRESS=*           RFC 4978 deflate, rejected (classifier would see garbage)
#   REPLACE              RFC 8508 APPEND+EXPUNGE, rejected
#   STARTTLS             TLS upgrade after login, rejected
#   IMAP4rev2            RFC 9051 makes LITERAL- implicit; the proxy speaks
#                        IMAP4rev1 to the agent, so a rev2 client must not assume it
_HIDDEN_CAPABILITIES = {"LITERAL+", "LITERAL-", "REPLACE", "STARTTLS", "IMAP4REV2"}
_HIDDEN_CAPABILITY_PREFIXES = ("COMPRESS=",)

# "* CAPABILITY a b c" (untagged) or "... [CAPABILITY a b c] ..." (response code).
_CAPABILITY_UNTAGGED_RE = re.compile(r"^(\* CAPABILITY )(.*)$", re.IGNORECASE)
_CAPABILITY_CODE_RE = re.compile(r"(\[CAPABILITY )([^\]]*)(\])", re.IGNORECASE)

# A non-synchronizing literal marker (RFC 7888): {N+} (LITERAL+), {N-} (LITERAL-),
# optionally ~-prefixed (RFC 6855 UTF-8 literal). It must end the line.
_NONSYNC_LITERAL_TAIL_RE = re.compile(r"~?\{(\d+)[+-]\}$")


def _keep_capability(atom: str) -> bool:
    upper = atom.upper()
    if upper in _HIDDEN_CAPABILITIES:
        return False
    return not upper.startswith(_HIDDEN_CAPABILITY_PREFIXES)


def _filter_atoms(atoms: str) -> str:
    return " ".join(a for a in atoms.split() if _keep_capability(a))


def filter_capabilities(line: str) -> str:
    """Strip capabilities the agent must not use from one upstream response line.

    Handles both the untagged ``* CAPABILITY ...`` response and the
    ``[CAPABILITY ...]`` response code (e.g. on a tagged LOGIN OK). Any other
    line is returned unchanged. ``line`` has no trailing CRLF.
    """
    m = _CAPABILITY_UNTAGGED_RE.match(line)
    if m:
        return m.group(1) + _filter_atoms(m.group(2))
    return _CAPABILITY_CODE_RE.sub(
        lambda cm: cm.group(1) + _filter_atoms(cm.group(2)) + cm.group(3), line
    )


def nonsync_literal_size(line: str) -> int | None:
    """Byte count of a non-synchronizing literal ending *line*, else None.

    With a non-synchronizing literal the client sends the literal bytes right
    after the CRLF without waiting for a ``+`` continuation, so the proxy must
    consume them to stay in sync. It refuses the command rather than forward
    it: the bytes could be a message body (``REPLACE``/``APPEND``) and the rest
    of the command continues on the line after them.
    """
    m = _NONSYNC_LITERAL_TAIL_RE.search(line.rstrip())
    return int(m.group(1)) if m else None


def classify(cmd: ParsedCommand) -> str:
    """Return 'read', 'write', 'blocked', or 'rejected' for a parsed command.

    Fails closed: a verb that is not explicitly enumerated is 'rejected'. The
    proxy answers it locally with ``NO [CANNOT]`` and never forwards it. See the
    module docstring for the decision tree and why.
    """
    if cmd.command in BLOCKED_COMMANDS:
        return "blocked"
    if cmd.command in WRITE_COMMANDS:
        return "write"
    if cmd.command in READ_COMMANDS:
        return "read"
    # A bare "DONE" line ends IDLE (RFC 2177). parse_line() reads it as a tag
    # with no command. Forwarding a stray DONE is harmless: upstream answers BAD.
    if cmd.command == "" and cmd.tag.upper() == "DONE":
        return "read"
    # Everything else, including LOGIN/AUTHENTICATE after authentication
    # (handled in handle_client before this point), COMPRESS, STARTTLS,
    # REPLACE, SETACL, SETMETADATA, SETQUOTA, and unknown/custom extensions.
    return "rejected"
