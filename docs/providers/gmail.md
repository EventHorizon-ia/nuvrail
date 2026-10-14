# Gmail setup (OAuth2 via Google, XOAUTH2)

> **Status:** verified against a live account. Google Workspace mailboxes have run through Nuvrail on our own deployment since 2026-09-10. Status for every provider: [README → Provider status](../../README.md#provider-status).

_User + operator guide for connecting a **Gmail** or **Google Workspace**
mailbox to Nuvrail. Google requires OAuth2 (XOAUTH2) for IMAP/SMTP — app
passwords are unavailable to most accounts and basic auth is not supported, so
OAuth2 is the **only** path._

Wire format is identical to Outlook's XOAUTH2 — only the authorization
endpoints, scopes, and upstream hosts differ. Everything provider-specific
lives in one `ProviderConfig` row (`api/routes/oauth2.py`,
`_PROVIDERS["google"]`) and one token-refresh branch
(`gateway/oauth2_tokens.py`, `_refresh_google_token`).

---

## How the flow works

```
  Browser                Nuvrail API                    Google
  ───────                ───────────                    ──────
     │  GET /oauth2/google/start                             │
     │ ─────────────────────► mint state, build auth_url     │
     │ ◄───────────────────── {auth_url, state}              │
     │                                                       │
     │  redirect to accounts.google.com/o/oauth2/v2/auth     │
     │ ─────────────────────────────────────────────────────► consent screen
     │ ◄───────────────────────────────────────────────────── user approves
     │                                                       │
     │  GET /oauth2/google/callback?code=…&state=…           │
     │ ─────────────────────► validate state + provider      │
     │                        exchange code ─────────────────► oauth2.googleapis.com/token
     │                        ◄────────────── refresh + access + id_token
     │                        store refresh token (encrypted secret store)
     │                        INSERT agent_credentials
     │                          upstream_host      = imap.gmail.com
     │                          upstream_smtp_host = smtp.gmail.com
     │                          oauth2_provider    = "google"
     │ ◄───────────────────── redirect → SPA /#/oauth2/callback
     │  GET /oauth2/google/result  → one-time agent token
     │
  Later, on every mailbox connection:
     get_access_token() → cache hit?  → serve
                        → cache miss? → _refresh_google_token()
                                          POST oauth2.googleapis.com/token grant=refresh_token
```

State is keyed by a globally-unique nonce and records which provider it was
minted for; the callback refuses a state whose provider doesn't match the path
(`error=invalid_state`) so a Microsoft flow can't be crossed into the Google
callback.

The account email is read (display-only, non-authoritative) from the `email`
claim of the `id_token` Google mints during the code exchange — that's why the
scope includes `openid email` in addition to mailbox access.

---

## One-time operator setup (register the Google OAuth2 client)

1. Go to the [Google Cloud Console](https://console.cloud.google.com) and
   **create (or select) a project** for your Nuvrail instance.
2. **APIs & Services → Enable APIs & Services → enable the *Gmail API*** for the
   project. (IMAP/SMTP over XOAUTH2 uses the same `https://mail.google.com/`
   OAuth scope; enabling the Gmail API is what surfaces that scope on the
   consent screen.)
3. **APIs & Services → OAuth consent screen:**
   - **User type:** *External* (unless every mailbox is inside one Google
     Workspace org, in which case *Internal* skips verification).
   - Add the scopes below (or add them at the client step). For an *External*
     app left in **Testing**, add each mailbox address under **Test users** —
     otherwise consent is refused. To serve arbitrary users you must **publish**
     the app, and the `https://mail.google.com/` scope is *restricted*, so
     Google requires a security assessment before verification. For a
     self-hosted, single-operator instance, staying in **Testing** with your own
     addresses as test users is the common path.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID:**
   - **Application type:** *Web application*.
   - **Authorized redirect URI:** **exactly** matching `GOOGLE_REDIRECT_URI`,
     e.g. `https://nuvrail.example.com/api/v1/oauth2/google/callback`
     (local dev default: `http://localhost:8080/api/v1/oauth2/google/callback`).
     A mismatch here is the #1 cause of a failed callback.
5. **Scopes** requested by Nuvrail (set on the consent screen):
   - `https://mail.google.com/` — full IMAP/SMTP access (XOAUTH2).
   - `openid` and `email` — to read the connecting account's address for
     display.
6. Copy the generated **client ID** and **client secret**, then set the server
   env vars (see `.env.example`):

   ```bash
   GOOGLE_CLIENT_ID=<oauth client id>
   GOOGLE_CLIENT_SECRET=<oauth client secret>
   GOOGLE_REDIRECT_URI=https://nuvrail.example.com/api/v1/oauth2/google/callback
   ```

Nuvrail requests offline access (`access_type=offline`) and forces the consent
screen (`prompt=consent`) on the authorization request — that combination is
how Google reliably returns a **refresh token** on the code grant. The client
secret is only read at consent time; the long-lived refresh token minted per
user is stored in the configured encrypted secret store (never in plaintext,
never in the DB), exactly like the Outlook path.

---

## Connecting a mailbox (end user)

1. `https://nuvrail.example.com` → **Agents** → **Connect Gmail**.
2. Sign in to Google and approve the requested permissions.
   - If the app is unpublished (**Testing**), you'll see an "unverified app"
     warning; proceed via **Advanced → Go to … (unsafe)**. This is expected for
     a self-hosted instance you operate.
3. Copy the one-time agent username + token shown on success — you won't see the
   token again. Give those to your AI agent as its IMAP/SMTP credentials.

Connection settings Nuvrail uses upstream (you don't configure these — they're
set automatically for the `google` provider):

| Setting | Value |
|---|---|
| IMAP host | `imap.gmail.com` |
| IMAP port | `993` (implicit TLS) |
| SMTP host | `smtp.gmail.com` |
| SMTP port | `587` (STARTTLS) |
| Auth | XOAUTH2 (bearer access token, auto-refreshed) |

Folder normalization for Gmail (`[Gmail]/All Mail` archive, `[Gmail]/Trash`,
`[Gmail]/Spam`) is handled by `GMAIL_PROFILE` in
`gateway/provider_profiles.py`. Gmail auto-saves sent mail to `[Gmail]/Sent
Mail` on every SMTP relay, so Nuvrail suppresses agent `APPEND`s to the Sent
folder to avoid duplicate copies.

> **Gmail's non-standard folders:** Gmail exposes its labels as special-use IMAP
> folders under the `[Gmail]/` prefix rather than plain `Sent`/`Trash`/`Archive`
> names. Nuvrail maps these for you via `GMAIL_PROFILE`; you don't need to
> configure folder names by hand.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Callback fails, `error=invalid_state` | Redirect URI in the Cloud Console doesn't exactly match `GOOGLE_REDIRECT_URI`, or the consent took longer than the 5-min state TTL — retry. |
| `503 oauth2_not_configured` on **Connect Gmail** | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` not set on the server. |
| `did not return a refresh_token` | Consent wasn't forced or offline access wasn't granted (Nuvrail sends `access_type=offline` + `prompt=consent`, so this usually means a stale prior grant) — reconnect the mailbox to force a fresh consent. |
| `access_denied` / "app isn't verified" blocks sign-in | The OAuth app is in **Testing** and the address isn't a **Test user**, or the app needs publishing — add the address as a test user, or publish the app. |
| Token refresh fails with `invalid_grant` | The user revoked access, changed their password, or the refresh token expired (unpublished "Testing" apps expire refresh tokens after 7 days) — reconnect the mailbox. Publishing the app removes the 7-day expiry. |

> **Note on revocation:** unlike Microsoft, Google exposes a token-revoke
> endpoint that Nuvrail calls best-effort
> (`revoke_google_refresh_token` → `https://oauth2.googleapis.com/revoke`) when
> a credential is removed, severing Nuvrail's access at the provider. Users can
> also review and revoke access at any time from
> [myaccount.google.com → Security → Third-party access](https://myaccount.google.com/connections).

---

_See also: [Outlook / Office 365 setup](outlook.md) ·
[iCloud Mail setup](icloud.md) ·
[generic IMAP provider guide](../provider-imap-guide.md) ·
[60-second quickstart](../../README.md)._
