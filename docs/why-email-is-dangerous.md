# Why email is the most dangerous thing you can give an AI agent

We wire agents into everything now. Calendars, docs, code repos, Slack. Most of
those integrations have a blast radius you can reason about: a bad calendar write
is annoying, a bad doc edit is in version history, a bad `git push` is on a branch
behind review. Email is different. When you hand an AI agent raw IMAP/SMTP access,
you hand it the one interface in your stack that is simultaneously **your most
sensitive datastore, your most trusted outbound identity, and permanently
destructive** — with no review step anywhere in the loop.

This is a builder's writeup of why that specific combination is dangerous, why the
usual answers don't fix it, and the shape of the fix we ended up building. If you
run agents against a real mailbox, this is the failure mode to design around before
it finds you.

## 1. The concrete threat

Give an agent an app password or an OAuth token with mail scope, point its IMAP/SMTP
client at your mailbox, and tell it to "keep my inbox under control overnight." Here
is a walked-through failure, step by step, with nothing exotic in it:

1. At 3:11am the agent is triaging. It finds a draft you saved but never sent — a
   proposal to your biggest prospect, pricing section still blank, subject line still
   `SUBJECT TBC`. Its heuristic reads the thread as "awaiting reply, high urgency."
2. It calls SMTP. `MAIL FROM: <you>`. The message goes out — from **you**, signed as
   **you**, through **your** authenticated session. The recipient's server sees a
   perfectly legitimate email. There is no "an agent sent this" header. As far as the
   world is concerned, you sent it.
3. Later it decides to "clean up" a resolved thread. It issues `UID STORE ... +FLAGS
   (\Deleted)` followed by `EXPUNGE`. The onboarding history for a $200k account is
   now gone. Not in Trash — **gone**. EXPUNGE is permanent; there is no undo and no
   trash to recover from.
4. Answering a question from your VP of Engineering, it hits reply-all on a thread
   that happens to include three external contractors and your board chair. The body
   quotes salary figures from earlier in the thread. The agent had no way to know who
   was on the CC line or what was sensitive.

None of these require a jailbreak, a prompt injection, or a malicious model. Every
one is the agent **doing exactly what it was told**, with a slightly wrong
interpretation and a tool that executes irreversible actions on the first try. The
danger isn't that the model is evil. It's that email gives a probabilistic system a
deterministic, irreversible, identity-bearing weapon and asks it to be careful.

Now layer on the read side. That same IMAP session can `FETCH` every message you
own: password-reset links, 2FA codes, legal threads, financials, the private
correspondence of everyone who has ever emailed you. A single over-broad instruction
— or a single crafted email that the agent reads and obeys — and all of it is
in-context and exfiltratable. Your inbox is the skeleton key to most of your other
accounts, and you just gave an autonomous process the whole ring.

## 2. Why the usual answers don't close it

The reflex is "scope the credentials." That helps at the edges and misses the middle.

- **Scoped OAuth / provider permissions.** Gmail and Microsoft OAuth scopes are
  coarse. `gmail.modify` covers reading, moving, labeling, and deleting; `gmail.send`
  is all-or-nothing send. There is no scope that means "may send, but only after a
  human approves *this specific message*." OAuth gates *which categories of action*
  are possible over the whole session — it cannot gate *this individual write* on a
  human decision. Once `send` is granted, every send is pre-authorized forever.

- **Read-only tokens.** A read-only token defuses the destructive and outbound risk
  and, in exchange, deletes the product. An agent that can't send, file, or archive
  can't actually manage your inbox — it's a search box. And read-only does nothing
  about the *read* threat, which is half the exposure: read-only still means every
  secret in your mailbox is one instruction away from leaving.

- **"Undo send" / delayed send.** A 30-second cancel window is a UI affordance for a
  human who is watching. An agent working through your inbox at 3am is not watching,
  and neither are you. Undo-send assumes a person is present at the moment of the
  mistake. With agents, nobody is.

- **Sandboxing the agent.** You can sandbox the process, the filesystem, the network.
  You cannot sandbox the *mailbox* — the whole point is to act on the real one. The
  dangerous surface isn't the agent's environment; it's the credential you handed it.

The gap they all share: **there is no approval step at the moment of the write.**
Every other place we let agents act, we've built one — a PR before merge, a staged
deploy before prod, a confirm dialog before `rm -rf`. Email is the exception. It has
`MAIL FROM` and `EXPUNGE` and nothing in between the intent and the irreversible act.

## 3. The fix: an IMAP/SMTP proxy with human-in-the-loop approval

The clean place to add the missing review step is not inside the agent (you can't
trust the thing you're guarding against to guard itself) and not inside the mail
provider (you don't control it). It's **the wire between them**.

Put a proxy in the middle that speaks IMAP and SMTP on both sides. To the agent it
looks like an ordinary mail server — no SDK, no special client, no cooperation from
the model required. It intercepts every command and splits them by consequence:

- **Reads pass through instantly.** `FETCH`, `SEARCH`, `LIST` — the agent stays fast
  and useful; nothing blocks it from working.
- **Writes are staged, not executed.** Every `MOVE`, `STORE`/flag change, and every
  outbound send (at SMTP `DATA`) returns `OK [STAGED]` immediately, so the agent keeps going,
  but nothing touches the real mailbox yet.
- **A human approves the write.** You get a push notification, and the approval UI
  shows exactly what the agent wants to do — subject, sender, destination — so you
  approve or reject with one tap. (The notification is end-to-end encrypted to your
  device via Web Push, so the push relay in between can't read it — and in the
  open-source build nothing executes until you approve it.)
  On approval it executes against the real server. On rejection the proxy reverts its
  local state and surfaces the rejection to the agent the next time it reads that folder.
- **Permanent deletion is blocked outright.** `EXPUNGE` (and `CLOSE`, which expunges
  as a side effect) is never forwarded to the real server, and a `\Deleted` flag is
  staged like any other write. The worst any agent can do is *ask* to delete a
  message — and that waits for your yes. Even after you approve, the gateway only
  sets the flag; it never expunges.

Here is that, end to end. An agent told to "clean up my inbox" flags 38 messages
`\Deleted` and sends `EXPUNGE`. The flagging is staged (`OK [STAGED]`), the `EXPUNGE`
is answered `OK Noted` and never forwarded, and one tap on **Reject** leaves the
mailbox untouched:

![Demo: an AI agent's bulk delete is staged by Nuvrail and rejected with one tap; the mailbox is untouched.](assets/3am-demo/3am-demo.gif)

*Recorded in CI against a local test mail server, with a real click on the Reject
button. The "agent" is a short script so you can reproduce it; any IMAP client
behaves the same through the proxy. [Reproduce it yourself](assets/3am-demo/README.md).*

This inverts the trust model. The agent no longer needs to be trusted with your
identity and your history; it needs to be trusted only to *propose*. The
consequential half — send, move, delete — becomes a decision a human makes with full
context, at the moment it matters, in one tap. You get the leverage of an autonomous
inbox agent without betting your reputation and your data on it never
misinterpreting a single instruction.

The proxy is also the audit trail email never had: every staged action, every
approval, every rejection is a logged event. When something does go out, you know
exactly what, when, and that you approved it — instead of finding out from the
recipient.

## 4. Try it in ~15 minutes (self-host)

This is open source (AGPL-3.0) and self-hostable. You need Docker with the Compose
plugin — no domain, no TLS, nothing else — to run the whole thing on `localhost`:

```bash
git clone https://github.com/agens-field/nuvrail.git
cd nuvrail
cp .env.example .env
docker compose up --build
```

The first build compiles both images from scratch, so a cold clone is typically
**~5–9 minutes**. If the terminal goes quiet mid-build it's compiling, not stuck.
Later starts come up in seconds. Health check:

```bash
curl -s http://localhost:8080/health   # → {"status": "ok", ...}
```

Sign-up is **closed by default** (a box that holds mailboxes should be private
unless you say otherwise), so create your first account from the admin CLI:

```bash
docker compose exec gateway python3 scripts/manage_users.py create you@example.com --name "You"
```

Then open `http://localhost:3000`, log in, connect a mailbox, and point your
agent's IMAP/SMTP client at the proxy (`localhost:10143` / `localhost:10587`)
instead of at your provider. The first write your agent makes comes back
`OK [STAGED]` and waits for you in the approval UI.

Wiring a specific agent? There are step-by-step recipes for
[Claude Desktop](integrations/claude-desktop.md), [Cursor](integrations/cursor.md),
and [LangChain](integrations/langchain.md). Full deployment (TLS, nginx, a real
mailbox reachable by an agent on another machine) is in the
[README](../README.md#deploying-to-production).

If the idea is useful, a ★ on [the repo](https://github.com/agens-field/nuvrail)
helps other people building agents find it before they learn this the hard way.
And don't take our word for any of the above — the interception, staging, and
the `EXPUNGE` block are all in [`gateway/`](../gateway). Read the code.

---

*The uncomfortable summary: we spent a decade building review steps into every tool
we let humans use carelessly, then handed AI agents the one tool that never got one.
Email doesn't need the agent to be smarter. It needs the approval step the rest of
your stack already has.*
