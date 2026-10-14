# Connecting iCloud Mail to Nuvrail

> **Status: built, not yet verified against a live account.** The iCloud profile, unit tests and this guide exist, but nobody has connected a real iCloud mailbox end to end yet. If you try it, please [open an issue](https://github.com/agens-field/nuvrail/issues/new/choose) with the result. Status for every provider: [README → Provider status](../../README.md#provider-status).

This guide walks you through connecting an **iCloud Mail** account (`@icloud.com`,
`@me.com`, or `@mac.com`) to Nuvrail so your AI agent can read and act on that
mailbox through the approval proxy.

> **TL;DR** — iCloud does **not** support OAuth2 for IMAP/SMTP. You authenticate
> with an **app-specific password** generated at
> [appleid.apple.com](https://appleid.apple.com/account/manage) — *not* your
> normal Apple ID password. Everything else is standard IMAP/SMTP over TLS.

---

## Before you start

You will need:

- An Apple ID with **two-factor authentication enabled** (required — app-specific
  passwords are only available on accounts with 2FA).
- Access to [appleid.apple.com](https://appleid.apple.com) to generate the
  app-specific password.
- A running Nuvrail instance (see the [60-second quickstart](../../README.md)).

---

## Connection settings at a glance

| Setting                | Value                     |
| ---------------------- | ------------------------- |
| IMAP host              | `imap.mail.me.com`        |
| IMAP port              | `993` (SSL/TLS)           |
| SMTP host              | `smtp.mail.me.com`        |
| SMTP port              | `587` (STARTTLS)          |
| Username               | Your full iCloud email address (e.g. `you@icloud.com`) |
| Password               | An **app-specific password** (see below) |

These are Apple's standard iCloud Mail server settings; Nuvrail auto-detects the
iCloud provider profile from the `mail.me.com` / `mac.com` hostname and applies
the correct folder mapping (see [iCloud-specific behavior](#icloud-specific-behavior)).

---

## Step 1 — Generate an app-specific password

1. Sign in at **[appleid.apple.com/account/manage](https://appleid.apple.com/account/manage)**.
2. In the **Sign-In and Security** section, choose **App-Specific Passwords**.
3. Click **Generate an app-specific password** (the **+** button).
4. Give it a recognizable label, e.g. `Nuvrail`, and click **Create**.
5. Apple shows you a password formatted like `abcd-efgh-ijkl-mnop`.
   **Copy it now** — Apple will not show it again.

> **Note:** The dashes are part of the password Apple displays, but iCloud
> accepts the password with or without them. Nuvrail stores whatever you paste;
> if a login fails, try pasting it without the dashes (`abcdefghijklmnop`).

If you ever revoke this password in the Apple ID console, the connection will
stop working and you'll need to generate a new one and update the credential in
Nuvrail.

---

## Step 2 — Add the account in Nuvrail

1. Open the Nuvrail setup UI and choose **Add account → iCloud Mail**.
2. Enter:
   - **Email address**: your full iCloud address (`you@icloud.com`).
   - **Password**: the **app-specific password** from Step 1 — *not* your Apple
     ID login password.
3. The IMAP/SMTP hosts and ports are pre-filled from the table above; leave them
   as-is unless you have a specific reason to change them.
4. Click **Save**.

Nuvrail **validates the connection before storing the credential**: it opens an
IMAP TLS session to `imap.mail.me.com:993`, logs in, and selects `INBOX`. If the
login fails you'll see a clear error rather than a silent failure —
see [Troubleshooting](#troubleshooting).

---

## Step 3 — Point your agent at Nuvrail

Your AI agent connects to the **Nuvrail proxy**, not to iCloud directly. Use the
Nuvrail-issued host/port and per-agent credential from the setup UI — the agent
never sees your app-specific password. From the agent's perspective it's a
standard RFC 3501 IMAP server; Nuvrail handles the iCloud-specific translation
and stages every mutating action for your approval.

---

## iCloud-specific behavior

Nuvrail applies an iCloud provider profile automatically. Two differences from
Gmail/Outlook are worth knowing:

- **Sent mail is saved by Nuvrail, not by iCloud.** Unlike Gmail and Outlook,
  iCloud does **not** automatically add a copy to the Sent folder when you relay
  a message over SMTP. Nuvrail compensates by appending approved sent messages
  to **`Sent Messages`** after a successful send, so your Sent folder stays
  correct. (If your agent also tries to append to `Sent Messages`, Nuvrail
  suppresses the duplicate.)
- **iCloud folder names.** iCloud uses non-standard folder names for the common
  actions. Nuvrail maps them for you:

  | Action  | iCloud folder      |
  | ------- | ------------------ |
  | Archive | `Archive`          |
  | Trash   | `Deleted Messages` |
  | Junk    | `Junk`             |
  | Sent    | `Sent Messages`    |

You don't need to configure any of this — it's applied when Nuvrail detects the
iCloud host.

---

## Troubleshooting

**"IMAP authentication failed. Wrong username or password."**
The most common cause is using your **Apple ID login password** instead of an
**app-specific password**. Regenerate an app-specific password (Step 1) and try
again. Also confirm the username is your full email address, including the domain.

**Login worked before and now fails.**
An app-specific password you revoked (or one tied to a since-changed Apple ID
password) is no longer valid. Generate a new app-specific password and update the
credential in Nuvrail.

**App-Specific Passwords option is missing at appleid.apple.com.**
App-specific passwords require **two-factor authentication** on your Apple ID.
Enable 2FA first, then the option appears under Sign-In and Security.

**Connection times out.**
Confirm you can reach `imap.mail.me.com:993` from wherever Nuvrail runs (some
networks block outbound IMAP). The ports are `993` for IMAP (SSL) and `587` for
SMTP (STARTTLS).

---

## Why app-specific passwords (and not OAuth2)?

Apple does not offer an OAuth2 flow for third-party IMAP/SMTP access to iCloud
Mail. App-specific passwords are Apple's supported mechanism for exactly this
case: a scoped, individually-revocable credential that never exposes your primary
Apple ID password. They can be revoked from the Apple ID console at any time
without affecting your main account, which makes them a good fit for Nuvrail's
least-privilege posture.

---

_See also: [Provider IMAP Guide](../provider-imap-guide.md) for the per-provider
normalization rules Nuvrail applies internally._
