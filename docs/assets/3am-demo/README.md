# The 3am demo: reproduce it yourself

An agent is told to "clean up my inbox". It decides 38 old threads are
resolved, flags them `\Deleted`, and sends `EXPUNGE`. Through Nuvrail, the
flagging is **staged for your approval**, and the `EXPUNGE` is **answered
locally and never forwarded**. Reject the staged operation and the mailbox is
untouched.

The "agent" here is a short script, so you can reproduce it; any IMAP client
behaves the same through the proxy.

```
seed_mailbox.py ──IMAPS──► throwaway mailbox          (setup, bypasses Nuvrail)

agent.py ──IMAP :10143──► Nuvrail proxy ──► throwaway mailbox
   UID SEARCH SEEN BEFORE 01-Sep-2026    read     -> forwarded
   UID STORE 1:38 +FLAGS (\Deleted)      write    -> "OK [STAGED] Operation queued — ID: op_…"
   EXPUNGE                               blocked  -> "OK Noted", nothing sent upstream
                                  │
                                  └─► approval UI (localhost:3000): Approve / Reject
```

## Steps

1. **Make a throwaway mailbox** (a fresh account at any IMAP provider). Never
   use a real one.
2. **Seed it** with 40 fake messages, connecting to the provider directly:
   ```bash
   SEED_IMAP_HOST=imap.example.com SEED_IMAP_USER=demo@example.com \
   SEED_IMAP_PASSWORD='app-password' python3 seed_mailbox.py
   ```
   This makes 38 read messages from August 2026 (including "Acme onboarding —
   signed MSA attached", "Q4 pricing (DRAFT, do not send)" and "Re: offer
   letter — Jordan") and 2 unread ones from late September.
3. **Run Nuvrail** (`docker compose up`, see the main README) and connect the
   throwaway mailbox in the UI. Copy the agent username and token it gives you.
4. **Run the agent** against the local proxy:
   ```bash
   NUVRAIL_AGENT_USER=nuvrail_xxx NUVRAIL_AGENT_TOKEN='…' python3 agent.py
   ```
   Expected output (imaplib picks a random tag prefix, and the op id will differ):
   ```
   agent> SEARCH SEEN BEFORE 01-Sep-2026 -> 38 messages "resolved"
   agent> UID STORE 1:38 +FLAGS (\Deleted)
     <- KDEH4 OK [STAGED] Operation queued — ID: op_…
   agent> EXPUNGE
     <- KDEH5 OK Noted
   agent> inbox cleaned up. Moving on.
   ```
5. **Open the approval UI** and reject the staged operation.
6. **Check the mailbox in the provider's own web UI.** All 40 messages are
   still there, none flagged.

`agent.py` refuses to connect to anything other than `localhost:10143`,
because pointed straight at a mail server it really would delete the 38
messages. `DEMO_ALLOW_ANY_SERVER=1` overrides that; only use it with a
throwaway mailbox.

## No mailbox? Run it live against a local test server

`live_run.sh` runs steps 2-6 with no human and no real account. It starts a
local in-memory IMAPS server (GreenMail, see `compose.live.yml`), seeds it,
connects it to Nuvrail as an agent, runs `agent.py` through the proxy, rejects
the staged operation through the same API the approval UI calls, and lists the
mailbox from the server side before and after (`verify_mailbox.py`):

```bash
cp .env.example .env
docs/assets/3am-demo/live_run.sh
```

The gateway verifies the test server's certificate as it would Gmail's; the
script generates a throwaway CA per run and trusts it only inside the demo's
gateway container (`SSL_CERT_FILE`). No product code changes. CI runs this on
every change to the demo, the gateway or the API
(`.github/workflows/demo-3am-live.yml`). Footage from it must be captioned as
a **local test mail server**, not a real provider.

## The video, generated in CI (stage 2)

`capture/` films the live run instead of a person: the
[`3am demo (capture)`](../../../.github/workflows/demo-3am-capture.yml)
workflow runs `live_run.sh` with `DEMO_CAPTURE=1`, which also starts the web
UI and lets Playwright click the **real Reject button** on the staged card
(the script then re-reads the operation over the API and fails unless it is
`rejected`). The terminal scenes replay that run's own transcripts verbatim.

```
live_run.sh (DEMO_CAPTURE=1) ──► transcripts + scene-4-ui.webm   (real Reject tap)
capture.mjs terminal         ──► title, agent, listings, end card
render.sh (ffmpeg)           ──► 3am-demo.mp4 (60-90 s) · 3am-demo.gif (< 5 MB) · 4 stills
```

The output is a workflow artifact (`3am-demo-assets`); the workflow commits
nothing. Every scene carries the caption "local test mail server". To run it
yourself you need docker, node 20, ffmpeg and
`npm ci && npx playwright install chromium` in `capture/`.

## Accuracy notes (for anyone recording or captioning this)

- The staged reply is exactly `OK [STAGED] Operation queued — ID: op_…`.
  The `EXPUNGE` reply is `OK Noted`, not an error: the agent can't tell it
  was blocked, and doesn't need to.
- The agent uses `UID STORE`. The proxy rejects non-UID `STORE`, so a script
  using plain `STORE` would show a `BAD`, not the staging behaviour.
- An **approved** delete only sets `\Deleted` today; the gateway never
  expunges (nuvrail #168 tracks moving it to Trash). The demo shows a
  **reject**, so don't narrate "moved to Trash".
- In the open-source build nothing executes without a human. Don't mention
  auto-approval rules.

`tests/gateway/test_3am_demo.py` replays `agent.py` against a fake server and
checks each wire line against the gateway's real parser and classifier, so the
claims above fail CI if they stop being true.
