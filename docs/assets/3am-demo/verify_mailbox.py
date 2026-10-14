"""Server-side proof for the 3am demo: list the mailbox straight from the
mail server, bypassing Nuvrail, and check nothing was deleted or flagged.

    verify_mailbox.py --expect-total 40 --expect-deleted 0

Prints one line per message (UID, flags, subject) so a recording or a CI log
shows the mailbox as the server sees it, then exits non-zero if the counts
don't match. Stands in for "open the provider's web UI" in the README steps.
Reads the same SEED_IMAP_* variables as seed_mailbox.py.
"""
import argparse
import email
import imaplib
import os
import re
import sys
from email.header import decode_header, make_header


def connect() -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(os.environ["SEED_IMAP_HOST"], int(os.environ.get("SEED_IMAP_PORT", "993")))
    m.login(os.environ["SEED_IMAP_USER"], os.environ["SEED_IMAP_PASSWORD"])
    return m


def listing(m: imaplib.IMAP4_SSL) -> list[tuple[int, str, str]]:
    """(uid, flags, subject) for every message in INBOX, read-only."""
    m.select("INBOX", readonly=True)
    _, data = m.uid("SEARCH", None, "ALL")
    uids = data[0].split()
    if not uids:
        return []
    _, parts = m.uid("FETCH", b",".join(uids).decode(), "(UID FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
    rows = []
    for part in parts:
        if not isinstance(part, tuple):
            continue
        meta = part[0].decode()
        uid = int(re.search(r"UID (\d+)", meta).group(1))
        flags = re.search(r"FLAGS \(([^)]*)\)", meta).group(1)
        subject = str(make_header(decode_header(email.message_from_bytes(part[1]).get("Subject", ""))))
        rows.append((uid, flags, subject))
    return sorted(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-total", type=int, required=True)
    ap.add_argument("--expect-deleted", type=int, default=0)
    args = ap.parse_args()

    m = connect()
    rows = listing(m)
    m.logout()
    for uid, flags, subject in rows:
        print(f"  uid {uid:>3}  [{flags or '-'}]  {subject}")
    deleted = sum(1 for _, flags, _ in rows if "\\Deleted" in flags)
    print(f"mailbox: {len(rows)} messages, {deleted} flagged \\Deleted")
    if len(rows) != args.expect_total or deleted != args.expect_deleted:
        print(f"FAIL: expected {args.expect_total} messages, {args.expect_deleted} \\Deleted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
