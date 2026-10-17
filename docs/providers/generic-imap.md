# Connecting a generic IMAP/SMTP provider to Nuvrail

> **Status:** CI runs this code path end to end on every gateway/API change against a local GreenMail IMAPS server (the [3am demo live run](../../.github/workflows/demo-3am-live.yml)). It is also verified against a live third-party provider: MXRoute (IMAP + SMTP), in use on our own deployment since July 2026. Status for every provider: [README → Provider status](../../README.md#provider-status).

This guide walks you through connecting **any standard IMAP/SMTP mailbox** to
Nuvrail — a self-hosted mail server, a small business host, a privacy provider
(Fastmail, Proton Bridge, Migadu, Mailbox.org, Zoho, …), or your own Dovecot/
Postfix box. If your provider speaks RFC 3501 IMAP and RFC 5321 SMTP over TLS,
Nuvrail can proxy it.

> **TL;DR** — Point Nuvrail at your provider's IMAP and SMTP host/port, log in
> with a username and password, and you're done. If your provider offers
> **app-specific passwords**, use one instead of your primary password. Nuvrail
> applies a conservative **Generic IMAP** profile — it does not assume any
> provider-specific folder names or the `UID MOVE` extension.

For the three providers with dedicated setup flows, use their guides instead:
[Gmail](gmail.md) (OAuth2), [Outlook / Microsoft 365](outlook.md) (OAuth2), and
[iCloud Mail](icloud.md) (app-specific password). This page is for everything
else.

---

## Before you start

You will need:

- The **IMAP and SMTP server settings** for your provider — host, port, and TLS
  mode. These are on your provider's "IMAP settings" / "mail client setup" help
  page. If you run your own server, you already know them.
- A **username** (usually your full email address) and **password**. If your
  provider supports app-specific passwords (see [Step 1](#step-1--prefer-an-app-specific-password-if-your-provider-offers-one)),
  generate one for Nuvrail rather than reusing your login password.
- A running Nuvrail instance (see the [60-second quickstart](../../README.md)).

You do **not** need OAuth2 credentials. OAuth2 is only used for the three
providers that require it (Gmail, Outlook, and — for those flows —
Nuvrail-managed client apps). A generic provider authenticates with a plain
username and password over TLS.

---

## Connection settings at a glance

Nuvrail needs four upstream values for IMAP and SMTP. The standard ports are:

| Setting        | Typical value           | Notes                                        |
| -------------- | ----------------------- | -------------------------------------------- |
| IMAP host      | `imap.yourprovider.com` | From your provider's mail-client docs        |
| IMAP port      | `993` (implicit SSL/TLS)| `143` + STARTTLS also works if that's all your host offers |
| SMTP host      | `smtp.yourprovider.com` | From your provider's mail-client docs        |
| SMTP port      | `587` (STARTTLS)        | `465` (implicit SSL/TLS) also common         |
| Username       | Your full email address (e.g. `you@yourdomain.com`) — some hosts want just the local part; check their docs |
| Password       | Your mail password, or an **app-specific password** if available |

> **Always use TLS.** Nuvrail connects to your upstream over TLS (`993`/`465`
> implicit, or `143`/`587` with STARTTLS). Do **not** use the plaintext ports
> (`110` POP3, or `143`/`25` without STARTTLS) — your credentials would cross
> the network in the clear. If your provider only offers plaintext, treat that
> as a red flag and use a different provider.

---

## Step 1 — Prefer an app-specific password if your provider offers one

Many IMAP providers (Fastmail, Zoho, Yahoo, AOL, and others) let you generate a
scoped **app-specific password** that is separate from your login password and
individually revocable. If yours does, use it:

1. Find the **app passwords** (sometimes "third-party app access" or "mail app
   passwords") section in your provider's account/security settings.
2. Generate a new password, labeled something recognizable like `Nuvrail`.
3. Copy it — many providers show it only once.

This matches Nuvrail's least-privilege posture: the credential is scoped to mail
access, can be revoked without touching your main password, and never exposes
your primary login. If your provider **requires** app-specific passwords once
two-factor authentication is on (common), this step is mandatory, not optional.

If your provider does not offer app-specific passwords, use your normal mailbox
password — but strongly prefer a provider that does.

---

## Step 2 — Add the account in Nuvrail

The easiest path is the web UI:

1. Open the Nuvrail setup UI and choose **Agents → + Add agent → Connect via
   IMAP**.
2. Enter your **IMAP/SMTP server details** from the table above:
   - **Email address / username**: as your provider specifies (usually the full
     address).
   - **Password**: your app-specific password (preferred) or mailbox password.
   - **IMAP host / port** and **SMTP host / port**: from your provider's docs.
3. Click **Save**.

<details>
<summary>Prefer the API? Same thing via <code>curl</code>:</summary>

```bash
curl -s -X POST https://nuvrail.example.com/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "label": "my-work-email",
    "upstream_host": "imap.yourprovider.com",
    "upstream_smtp_host": "smtp.yourprovider.com",
    "upstream_imap_port": 993,
    "upstream_smtp_port": 587,
    "upstream_user": "you@yourdomain.com",
    "upstream_password": "your-app-specific-password"
  }' | python3 -m json.tool
# Returns agent_username + agent_token (one-time — copy it now)
```

</details>

Nuvrail **validates the connection before storing the credential**: it opens an
IMAP TLS session to your `upstream_host`, logs in, and selects `INBOX`. If the
login fails you get a clear error rather than a silent failure — see
[Troubleshooting](#troubleshooting).

---

## Step 3 — Point your agent at Nuvrail

Your AI agent connects to the **Nuvrail proxy**, not to your provider directly.
Use the Nuvrail-issued host/port and the one-time per-agent credential from
setup — the agent never sees your upstream password. From the agent's
perspective it is a standard RFC 3501 IMAP server; Nuvrail forwards reads
instantly and stages every mutating action (move, delete, flag change, outbound
send) for your approval.

See [Configuring your AI agent](../../README.md#configuring-your-ai-agent) for
the proxy host/port table and a Claude/MCP config example.

---

## Generic-provider behavior

When Nuvrail cannot match your upstream host to a known provider (Gmail, iCloud,
Outlook), it applies the conservative **Generic IMAP** profile
(`GENERIC_PROFILE` in [`gateway/provider_profiles.py`](../../gateway/provider_profiles.py)).
Three differences from the named-provider profiles are worth knowing:

- **No folder remapping.** Named providers remap the common actions to their own
  folder names (e.g. iCloud's `Deleted Messages`, Gmail's `[Gmail]/Trash`). The
  generic profile makes **no assumptions** about your folder layout: archive,
  trash, and junk are left to standard IMAP flags and your server's own folders
  rather than a hardcoded name. Folder-role detection still works when your
  server advertises RFC 6154 **SPECIAL-USE** attributes on `LIST` — most modern
  IMAP servers (Dovecot included) do — so archive/delete/spam intents are still
  recognized on compliant servers, including localized folder names. See the
  [Provider IMAP Guide](../provider-imap-guide.md#special-use-folder-roles-rfc-6154)
  for how SPECIAL-USE discovery works.
- **Nuvrail saves your Sent mail.** The generic profile sets `sent_folder="Sent"`,
  so after a successful SMTP relay Nuvrail performs an IMAP `APPEND` of the
  approved message to your `Sent` folder. If your server auto-saves on relay (so
  the message would appear twice) or uses a different Sent folder name, see
  [Troubleshooting](#duplicate-or-missing-sent-messages) — the `APPEND` is
  non-fatal if the folder is missing.
- **`UID MOVE` is not assumed.** The generic profile sets `move_capable=False`,
  so on approval Nuvrail executes moves as the classic `COPY` + `STORE \Deleted`
  + `EXPUNGE` sequence rather than RFC 6851 `UID MOVE`. This works on every
  RFC 3501 server, at the cost of being slightly less efficient than a native
  `MOVE`. You do not need to configure this.

You don't set any of this up — it's applied automatically when Nuvrail does not
recognize your provider host.

---

## Troubleshooting

**"IMAP authentication failed. Wrong username or password."**
Three common causes: (1) using your login password when your provider requires
an **app-specific password** (regenerate one per [Step 1](#step-1--prefer-an-app-specific-password-if-your-provider-offers-one));
(2) the wrong **username form** — some hosts want the full email address, others
want just the local part; check your provider's mail-client docs; (3) IMAP
access is disabled on the account — many providers require you to explicitly
enable IMAP/SMTP in account settings first.

**Connection times out or is refused.**
Confirm you can reach the IMAP/SMTP host and port from wherever Nuvrail runs
(some networks block outbound `993`/`587`). Double-check the host spelling and
that you're using the **TLS** ports (`993`/`465` implicit, or `143`/`587` with
STARTTLS) — not a plaintext port.

**TLS certificate errors.**
Your provider's IMAP/SMTP endpoint must present a valid TLS certificate for the
host you configured. If you run your own server with a self-signed certificate,
install a real certificate (e.g. Let's Encrypt) rather than disabling
verification — Nuvrail does not proxy credentials over an unverified connection.

**Duplicate or missing Sent messages.**
The generic profile appends approved sent messages to a folder named `Sent`.
If your provider **auto-saves** to Sent on SMTP relay, you may see duplicates;
if your Sent folder has a **different name**, the `APPEND` silently no-ops and
the message won't appear there. Either case is a provider quirk worth a
dedicated profile — please [open an issue](https://github.com/agens-field/nuvrail/issues)
naming your provider and its Sent-folder behavior so we can add a profile for it.

**Archive/delete lands in the wrong place (or nowhere).**
The generic profile relies on your server advertising RFC 6154 SPECIAL-USE
folder roles. If your server doesn't, intent detection falls back to name
heuristics and may not match non-English or unusual folder names. Again, an
issue naming the provider helps us add a proper profile.

---

## Adding a first-class profile for your provider

If you use a provider often enough that the generic defaults chafe (wrong Sent
behavior, non-standard folders, or missing `UID MOVE`), a dedicated provider
profile is a small, welcome contribution. Profiles are declarative — see the
existing `GMAIL_PROFILE` / `ICLOUD_PROFILE` / `OUTLOOK_PROFILE` in
[`gateway/provider_profiles.py`](../../gateway/provider_profiles.py) and the
[Provider IMAP Guide](../provider-imap-guide.md) for the normalization rules,
then add your profile plus a detection-table entry and a test in
[`tests/gateway/test_provider_profiles.py`](../../tests/gateway/test_provider_profiles.py).

---

_See also: [Provider IMAP Guide](../provider-imap-guide.md) for the per-provider
normalization rules Nuvrail applies internally, and the main
[README](../../README.md#connecting-an-email-account) for account-setup basics._
