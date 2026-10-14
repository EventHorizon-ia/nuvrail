"""Seed a THROWAWAY mailbox with 40 fake messages for the 3am demo.

Connects DIRECTLY to the test mailbox's own IMAP server (not through Nuvrail,
which would stage every APPEND for approval) and appends:

  38 read messages dated August 2026  -> what agent.py's SEARCH matches
   2 unread messages dated this month -> what it leaves alone

Use a mailbox you created for this. Never a real one.

    SEED_IMAP_HOST=imap.example.com SEED_IMAP_USER=demo@example.com \
    SEED_IMAP_PASSWORD=... python3 seed_mailbox.py
"""
import imaplib
import os
import sys
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

# The three subjects the demo narration points at, plus filler.
KEY_SUBJECTS = [
    "Acme onboarding — signed MSA attached",
    "Q4 pricing (DRAFT, do not send)",
    "Re: offer letter — Jordan",
]
FILLER = [
    "Weekly standup notes", "Your invoice is ready", "Re: lunch Thursday?",
    "Build #{n} passed", "Newsletter: this week in infra", "Re: design review",
]


def message(subject: str, when: datetime) -> bytes:
    msg = EmailMessage()
    msg["From"] = "colleague@example.com"
    msg["To"] = os.environ["SEED_IMAP_USER"]
    msg["Subject"] = subject
    msg["Date"] = when.strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(f"(fake demo message) {subject}\n")
    return msg.as_bytes()


def plan() -> list[tuple[str, datetime, str]]:
    """(subject, internal date, flags) for all 40 messages, oldest first."""
    old = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    rows = [(s, old + timedelta(days=i), r"(\Seen)") for i, s in enumerate(KEY_SUBJECTS)]
    for i in range(35):
        subject = FILLER[i % len(FILLER)].format(n=100 + i)
        rows.append((subject, old + timedelta(days=3 + i % 25, hours=i), r"(\Seen)"))
    recent = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
    rows += [("Re: can we move Friday's call?", recent, "()"),
             ("Contract redlines — v3", recent + timedelta(hours=2), "()")]
    return rows


def main() -> int:
    m = imaplib.IMAP4_SSL(os.environ["SEED_IMAP_HOST"], int(os.environ.get("SEED_IMAP_PORT", "993")))
    m.login(os.environ["SEED_IMAP_USER"], os.environ["SEED_IMAP_PASSWORD"])
    for subject, when, flags in plan():
        typ, _ = m.append("INBOX", flags, imaplib.Time2Internaldate(when), message(subject, when))
        if typ != "OK":
            print(f"APPEND failed for {subject!r}", file=sys.stderr)
            return 1
    print(f"seeded {len(plan())} messages into INBOX")
    m.logout()
    return 0


if __name__ == "__main__":
    sys.exit(main())
