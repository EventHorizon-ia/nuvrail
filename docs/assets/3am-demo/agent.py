"""The "3am agent" from the Nuvrail demo (docs/assets/3am-demo/README.md).

It is a plain imaplib script, not an LLM, so anyone can reproduce the demo.
Its logic is the kind of thing a real agent does when told to tidy an inbox:
find old read mail, flag it Deleted, then EXPUNGE.

    agent.py --(IMAP, :10143)--> Nuvrail proxy --(only what you approve)--> mailbox
      UID SEARCH ............... read    -> forwarded
      UID STORE +FLAGS \\Deleted  write   -> staged, "OK [STAGED] ... ID: op_..."
      EXPUNGE .................. blocked -> "OK Noted", never forwarded

WARNING: pointed straight at a mail server (no Nuvrail in between), this
really does delete mail. It refuses to run against anything but the local
proxy unless you set DEMO_ALLOW_ANY_SERVER=1.
"""
# task: clean up my inbox, archive anything resolved
import imaplib
import os
import sys

LOCAL_PROXY = {("localhost", 10143), ("127.0.0.1", 10143)}


class TapIMAP(imaplib.IMAP4):
    """imaplib drops the text of a tagged OK; keep the last raw line so we can print it verbatim."""

    def _get_line(self) -> bytes:
        self.last_line = super()._get_line()
        return self.last_line


def uid_set(uids: list[bytes]) -> str:
    """'1:38' for a contiguous run, else '3,7,9' (both are valid IMAP UID sets)."""
    nums = [int(u) for u in uids]
    if nums == list(range(nums[0], nums[-1] + 1)):
        return f"{nums[0]}:{nums[-1]}"
    return ",".join(str(n) for n in nums)


def main() -> int:
    host = os.environ.get("NUVRAIL_HOST", "localhost")
    port = int(os.environ.get("NUVRAIL_PORT", "10143"))
    if (host, port) not in LOCAL_PROXY and os.environ.get("DEMO_ALLOW_ANY_SERVER") != "1":
        print(f"refusing to run against {host}:{port}: not the local Nuvrail proxy", file=sys.stderr)
        return 2
    m = TapIMAP(host, port)
    m.login(os.environ["NUVRAIL_AGENT_USER"], os.environ["NUVRAIL_AGENT_TOKEN"])
    m.select("INBOX")
    _, data = m.uid("SEARCH", None, "SEEN", "BEFORE", "01-Sep-2026")
    uids = data[0].split()
    print(f'agent> SEARCH SEEN BEFORE 01-Sep-2026 -> {len(uids)} messages "resolved"')
    if uids:
        s = uid_set(uids)
        m.uid("STORE", s, "+FLAGS", r"(\Deleted)")
        print(f"agent> UID STORE {s} +FLAGS (\\Deleted)\n  <- {m.last_line.decode()}")
        m.expunge()
        print(f"agent> EXPUNGE\n  <- {m.last_line.decode()}")
    print("agent> inbox cleaned up. Moving on.")
    m.logout()
    return 0


if __name__ == "__main__":
    sys.exit(main())
