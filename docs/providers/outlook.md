# Outlook / Office 365 setup (OAuth2 via Azure AD)

> **Status: built, not yet verified against a live account.** The code, unit tests and this guide exist, but nobody has connected a real Outlook / Microsoft 365 mailbox end to end yet. Outlook shares Gmail's XOAUTH2 wire path, so expect any rough edges in the Azure app setup below. If you try it, please [open an issue](https://github.com/agens-field/nuvrail/issues/new/choose) with the result. Status for every provider: [README → Provider status](../../README.md#provider-status).

_User + operator guide for connecting a Microsoft Outlook.com or Office 365
mailbox to Nuvrail. Microsoft deprecated IMAP/SMTP **basic auth** for Exchange
Online in 2023, so OAuth2 (Azure AD, XOAUTH2) is the **only** supported path._

Wire format is identical to Gmail's XOAUTH2 — only the authorization endpoints,
scopes, and upstream hosts differ. Everything provider-specific lives in one
`ProviderConfig` row (`api/routes/oauth2.py`, `_PROVIDERS["microsoft"]`) and one
token-refresh branch (`gateway/oauth2_tokens.py`, `_refresh_microsoft_token`).

---

## How the flow works

```
  Browser                Nuvrail API                 Microsoft (Azure AD)
  ───────                ───────────                 ────────────────────
     │  GET /oauth2/microsoft/start                          │
     │ ─────────────────────► mint state, build auth_url     │
     │ ◄───────────────────── {auth_url, state}              │
     │                                                       │
     │  redirect to login.microsoftonline.com/<tenant>/…/authorize
     │ ─────────────────────────────────────────────────────► consent screen
     │ ◄───────────────────────────────────────────────────── user approves
     │                                                       │
     │  GET /oauth2/microsoft/callback?code=…&state=…        │
     │ ─────────────────────► validate state + provider      │
     │                        exchange code ─────────────────► /oauth2/v2.0/token
     │                        ◄────────────── refresh + access + id_token
     │                        store refresh token (encrypted secret store)
     │                        INSERT agent_credentials
     │                          upstream_host = outlook.office365.com
     │                          upstream_smtp_host = smtp.office365.com
     │                          oauth2_provider = "microsoft"
     │ ◄───────────────────── redirect → SPA /#/oauth2/callback
     │  GET /oauth2/microsoft/result  → one-time agent token
     │
  Later, on every mailbox connection:
     get_access_token() → cache hit?  → serve
                        → cache miss? → _refresh_microsoft_token()
                                          POST /oauth2/v2.0/token grant=refresh_token
                                          (scope MUST be echoed — Microsoft quirk)
```

State is keyed by a globally-unique nonce and records which provider it was
minted for; the callback refuses a state whose provider doesn't match the path
(`error=invalid_state`) so a Google flow can't be crossed into the Microsoft
callback.

---

## One-time operator setup (register the Azure AD app)

1. Go to the [Azure portal](https://portal.azure.com) → **Microsoft Entra ID**
   (Azure AD) → **App registrations** → **New registration**.
2. **Supported account types:** choose to match your `MICROSOFT_TENANT`:
   - Personal Outlook.com **and** any org → *"Accounts in any organizational
     directory and personal Microsoft accounts"* → keep `MICROSOFT_TENANT=common`
     (the default).
   - A single company tenant only → *"Accounts in this organizational directory
     only"* → set `MICROSOFT_TENANT` to that tenant's GUID or domain.
3. **Redirect URI:** platform **Web**, value **exactly** matching
   `MICROSOFT_REDIRECT_URI`, e.g.
   `https://nuvrail.example.com/api/v1/oauth2/microsoft/callback`
   (local dev default: `http://localhost:8080/api/v1/oauth2/microsoft/callback`).
   A mismatch here is the #1 cause of a failed callback.
4. **API permissions → Add a permission → APIs my organization uses →**
   *Office 365 Exchange Online* → **Delegated permissions**, add:
   - `IMAP.AccessAsUser.All`
   - `SMTP.Send`

   Then **Microsoft Graph → Delegated**: `offline_access`, `openid`, `email`.
   Grant admin consent if your tenant requires it.
5. **Certificates & secrets → New client secret.** Copy the secret **value**
   (not the ID) immediately — it is shown once.
6. Set the server env vars (see `.env.example`):

   ```bash
   MICROSOFT_CLIENT_ID=<application (client) id>
   MICROSOFT_CLIENT_SECRET=<client secret value>
   MICROSOFT_REDIRECT_URI=https://nuvrail.example.com/api/v1/oauth2/microsoft/callback
   MICROSOFT_TENANT=common   # or your tenant GUID/domain
   ```

The client secret is only read at consent time; the long-lived **refresh
token** minted per user is stored in the configured encrypted secret store
(never in plaintext, never in the DB), exactly like the Gmail path.

---

## Connecting a mailbox (end user)

1. `https://nuvrail.example.com` → **Agents** → **Connect Outlook**.
2. Sign in to Microsoft and approve the requested permissions.
3. Copy the one-time agent username + token shown on success — you won't see the
   token again. Give those to your AI agent as its IMAP/SMTP credentials.

Connection settings Nuvrail uses upstream (you don't configure these — they're
set automatically for the `microsoft` provider):

| Setting | Value |
|---|---|
| IMAP host | `outlook.office365.com` |
| IMAP port | `993` (implicit TLS) |
| SMTP host | `smtp.office365.com` |
| SMTP port | `587` (STARTTLS) |
| Auth | XOAUTH2 (bearer access token, auto-refreshed) |

Folder normalization for Outlook (Deleted Items, Junk Email, Sent Items) is
handled by `OUTLOOK_PROFILE` in `gateway/provider_profiles.py`; Outlook
auto-saves sent mail on SMTP relay, so Nuvrail suppresses agent `APPEND`s to
the Sent folder to avoid duplicates.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Callback fails, `error=invalid_state` | Redirect URI in Azure doesn't exactly match `MICROSOFT_REDIRECT_URI`, or the consent took longer than the 5-min state TTL — retry. |
| `503 oauth2_not_configured` on **Connect Outlook** | `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` not set on the server. |
| `did not return a refresh_token` | `offline_access` not in the granted scopes, or consent not forced — re-register the permission and reconnect. |
| Token refresh fails with `invalid_grant` | The user revoked access or changed their password; reconnect the mailbox. |
| Refresh fails after tenant change | If you switched `MICROSOFT_TENANT`, existing single-tenant refresh tokens may not resolve at `common` and vice-versa — reconnect. |

> **Note on revocation:** unlike Google, Microsoft has no simple token-revoke
> endpoint wired into Nuvrail today. On account deletion we delete the stored
> refresh token from the secret store (severing Nuvrail's access), but the grant
> itself is cleared by the user from
> [account.live.com → Privacy → Apps and services](https://account.live.com/consent/Manage)
> (personal) or by an admin in Entra ID (org). This is tracked as a follow-up.
