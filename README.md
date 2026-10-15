# Nuvrail

**The approval layer between AI agents and your inbox.**

<p align="center">
  <img src="docs/assets/3am-demo/3am-demo.gif" width="800" alt="Demo: an AI agent's bulk delete is staged by Nuvrail and rejected with one tap; the mailbox is untouched.">
</p>

<p align="center"><em>An agent tries to delete 38 emails at 3am. Nothing happens until a human says yes.</em><br>
<sub>The agent's <code>EXPUNGE</code> gets <code>OK Noted</code> and is never forwarded; the staged delete is rejected with one tap.
Recorded in CI against a local test mail server; the "agent" is a short script, and any IMAP client behaves the same through the proxy.
<a href="docs/assets/3am-demo/README.md">Reproduce it yourself</a> · <a href="docs/assets/3am-demo/3am-demo.mp4">MP4 (67 s)</a></sub></p>

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Self-host](https://img.shields.io/badge/self--host-docker%20compose-2496ED?logo=docker&logoColor=white)](#-try-it-in-60-seconds-local)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Stars](https://img.shields.io/github/stars/agens-field/nuvrail?style=social)](https://github.com/agens-field/nuvrail/stargazers)

> Email is the most dangerous thing you can hand an AI agent. Nuvrail makes it safe to try —
> reads flow through instantly, every write waits for one human tap. **If that sounds useful,
> [★ star the repo](https://github.com/agens-field/nuvrail) and [self-host it in 60 seconds](#-try-it-in-60-seconds-local).**

AI agents that can send email are useful. AI agents that can send email *without any human check* are a liability. Nuvrail sits between your AI agent and your real mail server. The agent sees a normal IMAP/SMTP server. Every write — every move, delete, flag change, and outbound send — is staged for your approval before it touches the real mailbox. Reads pass through instantly; nothing blocks the agent from working. Only destructive or irreversible actions require a human to say yes.

---

## How it works

```
  AI agent                  Nuvrail                   Real mail server
  ────────     IMAP/SMTP     ───────     IMAP/SMTP     ────────────────
  (Claude,  ──────────────► gateway ──────────────►  (Gmail, iCloud,
   Cursor,   reads: pass       │       on approval    Outlook, or any
   custom)   through           │       only           IMAP/SMTP server)
             instantly         │
                         writes: staged
                         → human reviews
                           & approves
```

1. **Agent writes a command** — e.g. `UID MOVE 42 Trash` or sends an email via SMTP.
2. **Gateway intercepts it** — returns `OK [STAGED]` immediately so the agent can keep working.
3. **You get a notification** — a push notification on your phone or browser shows you exactly what the agent wants to do, with the subject, sender, and destination.
4. **You approve or reject** — one tap. On approval the operation executes against the real server. On rejection the proxy reverts its local state and silently surfaces the rejection to the agent on its next command.

The agent can never permanently delete mail. `EXPUNGE` is blocked at the gateway layer, and a delete (`\Deleted`) is staged like any other write — the worst the agent can do is ask to delete a message, and even that requires your sign-off.

> 📖 **Why this exists:** [Why email is the most dangerous thing you can give an AI agent](docs/why-email-is-dangerous.md) —
> the failure modes, why scoped OAuth and read-only tokens don't close them, and why the fix belongs on the wire.
>
> 🧭 **Guides:** [Stop an AI agent from deleting your emails](docs/guides/stop-ai-agent-deleting-emails.md) ·
> [Add human approval before an AI agent sends email](docs/guides/human-approval-before-ai-agent-sends-email.md) ·
> [Human in the loop for LLM email agents](docs/guides/human-in-the-loop-llm-email-agent.md)

**Wiring up a specific agent?** Step-by-step recipes: [Claude Desktop](docs/integrations/claude-desktop.md) ·
[Cursor](docs/integrations/cursor.md) · [LangChain](docs/integrations/langchain.md).

---

## ⚡ Try it in 60 seconds (local)

Just want to see it work? You need **Docker with the Compose plugin** — nothing else. No
domain, no TLS, no nginx. This runs everything on `localhost` for evaluation.

```bash
git clone https://github.com/agens-field/nuvrail.git
cd nuvrail
cp .env.example .env

docker compose up --build
```

> 🔌 **Port 3000 already taken?** Set `NUVRAIL_HOST_WEB_PORT=3400` in `.env`
> (likewise `NUVRAIL_HOST_API_PORT`, `NUVRAIL_HOST_IMAP_PORT`, `NUVRAIL_HOST_SMTP_PORT`)
> and use that port in the URLs below. Ports are published host-side only — nothing
> about the host or port is baked into the web bundle, so `docker compose up -d` picks
> the change up without a rebuild.

> ⏱️ **First build takes several minutes — this is normal, not a hang.** The initial
> `--build` compiles both images from scratch (Python deps for the gateway, a Vite build
> for the web PWA), so a true fresh clone with a cold cache is typically **~5–9 min** on
> first run — not 60 seconds. Later `docker compose up` runs start in seconds. If the
> terminal sits quiet mid-build, it is compiling, not stuck; let it finish.

Sign-up is **closed by default** (`NUVRAIL_SIGNUP_MODE=closed`), so the web app has no
"create account" button. Create your first account from the CLI once the containers are up
(it prompts for a password):

```bash
docker compose exec gateway python3 scripts/manage_users.py create you@example.com --name "You"
```

Then open **<http://localhost:3000>** and log in. (Prefer self-serve sign-up? See
[Account creation](#account-creation-signup-modes).) The API is on
`http://localhost:8080`:

```bash
# Give the containers ~10–20s after the build finishes to come up healthy,
# then this returns {"status": "ok", ...}. A connection-refused here just
# means the gateway is still starting — wait a moment and retry.
curl -s http://localhost:8080/health   # → {"status": "ok", ...}
```

That's it — the approval UI, REST API, and IMAP/SMTP proxies (`localhost:10143` / `localhost:10587`)
are all running. Point a test IMAP/SMTP client at the proxy ports and watch writes get staged for
approval in the UI.

> 🌐 **Running it on a remote box?** The browser never calls the API directly —
> the web container proxies `/api/` to the gateway over the compose network — so only
> the web port needs publishing. Set `NUVRAIL_BIND_ADDR=0.0.0.0` in `.env`, rerun
> `docker compose up -d`, and open `http://<host>:3000`. No rebuild, no `VITE_API_URL`,
> no CORS entry. Do this on a trusted network only: it is plain HTTP carrying bearer
> tokens — see [Deploying to production](#deploying-to-production) for the TLS path.

> ℹ️ **Local trial only.** With `NUVRAIL_MASTER_KEY` left blank the gateway auto-generates a
> key on first run (fine for kicking the tires, **not** for real mail). To connect a real
> mailbox with agents on other machines, follow [Deploying to production](#deploying-to-production)
> below — that adds TLS, nginx, and a persisted master key.

Like what you see? **[★ Star the repo](https://github.com/agens-field/nuvrail)** — it's the
fastest way to help other people find it.

---

## Architecture

```
Internet
    │
    ├──► :993  IMAPS  ──► nginx (TLS) ──► IMAP proxy (10143)  ──► upstream IMAP
    ├──► :465  SMTPS  ──► nginx (TLS) ──► SMTP proxy (10587)  ──► upstream SMTP
    └──► :443  HTTPS  ──► nginx (TLS) ──► Web PWA + REST API (3000/8080)

All external TLS terminates at nginx. Docker containers are bound to 127.0.0.1 only.
No plain-text port is reachable from outside the host.
```

**Components:**
- **IMAP proxy** (`gateway/proxy.py`) — asyncio IMAP4rev1 state machine. Intercepts writes, passes reads, maintains a local SQLite mirror for optimistic consistency.
- **SMTP proxy** (`gateway/smtp_proxy.py`) — asyncio. Intercepts `DATA` (outbound sends), stages them. Relays on approval.
- **Staging engine** (`gateway/staging.py`) — assigns op IDs, stores snapshots for revert, tracks 48-hour expiry.
- **REST API** (`api/`) — FastAPI. Approve/reject operations, manage agents, audit log, push subscriptions.
- **Web PWA** (`web/`) — React 18 + Vite + TypeScript + Tailwind. Login, pending operations, audit log, agent management. Installable as a PWA on iOS/Android.

**Key design constraints:**
- Standard IMAP/SMTP on both ends — no agent modifications required
- EXPUNGE and CLOSE never forwarded; `\Deleted` is staged for approval like any other write
- AES-256-GCM for all credentials at rest; TLS everywhere in transit
- Push payloads are end-to-end encrypted to your device (Web Push, RFC 8291), so the push relay never sees email metadata
- Immutable audit log — every action is recorded forever

---

## Deploying to production

The 60-second path above is for evaluation on `localhost`. To run Nuvrail as a real service —
reachable by an agent on another machine, terminating TLS, connecting a real mailbox — follow
the full deployment below.

### Prerequisites

- Ubuntu 22.04 LTS (or equivalent Debian-based distro)
- DNS `A` record pointing at your server:
  - `nuvrail.example.com` → server IP (approval UI + proxy)
- Git, Docker Engine + Compose plugin, nginx with stream module, certbot

```bash
# Docker Engine
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in

# nginx with stream module + certbot
sudo apt install -y nginx libnginx-mod-stream certbot python3-certbot-nginx
```

---

### Step 1 — Clone and configure

```bash
sudo mkdir -p /opt/nuvrail && sudo chown $USER:$USER /opt/nuvrail
git clone https://github.com/agens-field/nuvrail.git /opt/nuvrail
cd /opt/nuvrail
cp .env.example .env
nano .env
```

Required environment variables:

| Variable | Description |
|---|---|
| `NUVRAIL_MASTER_KEY` | 64 hex chars — `python3 -c "import secrets; print(secrets.token_bytes(32).hex())"` |
| `NUVRAIL_CORS_ORIGINS` | `https://nuvrail.example.com` |
| `NUVRAIL_SIGNUP_MODE` | Who may create accounts: `closed` (default), `invite`, or `open`. See [Account creation](#account-creation-signup-modes). |
| `NUVRAIL_ALLOW_OPEN_SIGNUP` | Required ack (`1`) **only** when `NUVRAIL_SIGNUP_MODE=open`; the API refuses to start with `open` unless this is set. |
| `NUVRAIL_VAPID_EMAIL` | Admin contact email, e.g. `mailto:you@yourdomain.com` |
| `GOOGLE_CLIENT_ID` | GCP OAuth2 client ID (required for Gmail agent setup) |
| `GOOGLE_CLIENT_SECRET` | GCP OAuth2 client secret (required for Gmail agent setup) |
| `GOOGLE_REDIRECT_URI` | `https://nuvrail.example.com/api/v1/oauth2/google/callback` |
| `MICROSOFT_CLIENT_ID` | Azure AD app client ID (required for Outlook/O365 agent setup) |
| `MICROSOFT_CLIENT_SECRET` | Azure AD app client secret (required for Outlook/O365 agent setup) |
| `MICROSOFT_REDIRECT_URI` | `https://nuvrail.example.com/api/v1/oauth2/microsoft/callback` |
| `MICROSOFT_TENANT` | Azure AD tenant — `common` (default) for personal + multi-tenant, or a tenant GUID/domain for single-tenant |

> ⚠️ **Back up `NUVRAIL_MASTER_KEY`.** It encrypts every upstream credential in the
> database. Losing it makes all stored agent credentials unrecoverable.

**Choose your signup mode now** — it decides who can create an account on this
deployment and it is fail-closed, so a fresh `.env` is already private. Pick one
and put the matching lines in `.env`:

```bash
# closed (DEFAULT) — no public sign-up; you provision users from the admin CLI.
# This is also what you get if you set nothing at all. Best for a private /
# single-tenant instance.
NUVRAIL_SIGNUP_MODE=closed

# invite — public /auth/register works ONLY with a valid single-use invite code
# that you mint from the admin CLI. Best for onboarding a known group.
NUVRAIL_SIGNUP_MODE=invite

# open — anyone can self-register. REQUIRES the explicit ack below, or the API
# refuses to start. Only for a genuinely public deployment.
NUVRAIL_SIGNUP_MODE=open
NUVRAIL_ALLOW_OPEN_SIGNUP=1
```

You operate each mode in [Step 6 — Create your account](#step-6--create-your-account);
the full reference is [Account creation (signup modes)](#account-creation-signup-modes).
You can change the mode later on a live box — see
[Switching modes on a running deployment](#switching-modes-on-a-running-deployment).

---

### Step 2 — nginx (HTTP first) + TLS certificates

Certificates don't exist yet, so we bring nginx up with a **minimal HTTP-only**
config first, let certbot obtain the certs against the running nginx, and only
*then* add the TLS listeners and the mail stream proxies (Step 3). Doing it in
this order avoids the chicken-and-egg trap where nginx refuses to start because
it references `fullchain.pem` files that haven't been issued yet.

#### 2a — Minimal HTTP server block

`/etc/nginx/sites-available/nuvrail` — start with just this:

```nginx
server {
    listen 80;
    server_name nuvrail.example.com;
    # certbot's --nginx plugin will add the ACME challenge location here,
    # then insert the TLS server block in Step 2b.
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

> ℹ️ **Ports and bind address.** The block above reaches the containers on
> `127.0.0.1:3000` and `127.0.0.1:8080` — the compose defaults. If you remapped either
> with `NUVRAIL_HOST_WEB_PORT` / `NUVRAIL_HOST_API_PORT`, use your numbers here. Leave
> `NUVRAIL_BIND_ADDR` at its `127.0.0.1` default: nginx reaches the containers over
> loopback, so nothing should be published to the network. Note the SPA calls `/api/`
> on its own origin, so the `location /api/` block above is what serves it — there is
> no `VITE_API_URL` to set.

```bash
sudo ln -s /etc/nginx/sites-available/nuvrail /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

#### 2b — Obtain certificates (certbot drives nginx)

```bash
sudo ufw allow 80   # ACME HTTP-01 challenge
sudo certbot --nginx \
  -d nuvrail.example.com \
  --non-interactive --agree-tos --redirect \
  -m you@yourdomain.com
```

`certbot --nginx` obtains the cert, rewrites the port-80 block to redirect to
HTTPS, and adds a `listen 443 ssl` block wired to the freshly issued
`fullchain.pem` / `privkey.pem`. It also installs a systemd renewal timer.
Verify:

```bash
sudo certbot certificates          # confirm the cert exists
sudo nginx -t                      # config still valid
```

> The mail proxies (:993 / :465) reuse this same certificate — that's why we
> mint it before configuring the stream module below.

---

### Step 3 — Add the mail stream proxies + finalize web config

With certificates now in place, add the TLS-terminating stream proxies for
IMAP/SMTP and confirm the web server block is correct.

#### Enable the stream module

Add at the **top level** of `/etc/nginx/nginx.conf` (outside `http {}`):

```nginx
stream {
    include /etc/nginx/stream.d/*.conf;
}
```

```bash
sudo mkdir -p /etc/nginx/stream.d
```

#### `/etc/nginx/stream.d/nuvrail-proxy.conf`

```nginx
# IMAP SSL (:993) → gateway container (:10143)
server {
    listen 993 ssl;
    ssl_certificate     /etc/letsencrypt/live/nuvrail.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nuvrail.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    proxy_pass          127.0.0.1:10143;
    proxy_timeout       3600s;
    proxy_connect_timeout 5s;
}

# SMTPS (:465) → gateway container (:10587)
server {
    listen 465 ssl;
    ssl_certificate     /etc/letsencrypt/live/nuvrail.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nuvrail.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    proxy_pass          127.0.0.1:10587;
    proxy_timeout       300s;
    proxy_connect_timeout 5s;
}
```

#### `/etc/nginx/sites-available/nuvrail` (final web config)

certbot already rewrote this file in Step 2b into an HTTP→HTTPS redirect plus a
`listen 443 ssl` block for `nuvrail.example.com`. Confirm it matches the shape
below; add the hardening headers if certbot didn't:

```nginx
server {
    listen 80;
    server_name nuvrail.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name nuvrail.example.com;
    ssl_certificate     /etc/letsencrypt/live/nuvrail.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nuvrail.example.com/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header Referrer-Policy no-referrer;

    # REST API + health probe → API backend on :8080.
    # These MUST precede the `location /` SPA catch-all, or nginx routes
    # them to the web app and you get index.html instead of JSON.
    location /api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # SET, not append ($proxy_add_x_forwarded_for). The API trusts the
        # FIRST entry of this header to key its per-IP login lockout, so
        # appending would preserve a value the caller sent and let anyone
        # forge their IP to sidestep the lockout. $remote_addr is the peer
        # nginx actually accepted the connection from.
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location = /health {
        proxy_pass http://127.0.0.1:8080/health;
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

### Step 4 — Build and start

```bash
cd /opt/nuvrail
docker compose build
docker compose up -d
docker compose ps
```

Check the gateway logs to confirm all three services started:

```bash
docker compose logs gateway --tail=30
# Look for:
#   Nuvrail IMAP proxy listening on 0.0.0.0:10143
#   Nuvrail SMTP proxy listening on 0.0.0.0:10587
#   Application startup complete.
#
# The startup log also prints the effective signup mode, e.g.
#   Human account signup mode: closed
# Confirm it matches what you set in Step 1.
```

> If you set `NUVRAIL_SIGNUP_MODE=open` **without** `NUVRAIL_ALLOW_OPEN_SIGNUP=1`,
> the gateway **refuses to start** (you'll see a `RuntimeError` about the missing
> ack in the logs, and `docker compose ps` shows the container restarting). That
> is intentional fail-closed behavior — either add the ack var or pick a
> non-open mode, then `docker compose up -d` again.

---

### Step 5 — Verify the installation

```bash
# Approval UI
curl -s -o /dev/null -w "%{http_code}" https://nuvrail.example.com/
# → 200

# API health (route is unprefixed /health, not /api/v1/health)
curl -s https://nuvrail.example.com/health | python3 -m json.tool
# → {"status": "ok", ...}

# IMAP proxy (should see: * OK Nuvrail IMAP proxy ready)
openssl s_client -connect nuvrail.example.com:993 -quiet 2>/dev/null

# SMTP proxy (should see: 220 Nuvrail SMTP proxy ready)
openssl s_client -connect nuvrail.example.com:465 -quiet 2>/dev/null
```

---

### Step 6 — Create your account

How you create the first account depends on the signup mode you chose in Step 1.
Follow the subsection for your mode. After you have a bearer token, every mode
logs in the same way:

```bash
# Log in any time to get a fresh bearer token (works in every mode)
TOKEN=$(curl -s -X POST https://nuvrail.example.com/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email": "you@yourdomain.com", "password": "your-strong-password"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
```

#### 6a. Closed mode (default) — provision from the admin CLI

There is no public sign-up. Bootstrap the first account from inside the gateway
container, then use that account to add any others:

```bash
# create the first account (prompts for a password; prints a bearer token ONCE)
docker compose exec gateway python3 scripts/manage_users.py create you@yourdomain.com --name "Your Name"

# add more accounts the same way
docker compose exec gateway python3 scripts/manage_users.py create alice@example.com --name "Alice"

# see who exists
docker compose exec gateway python3 scripts/manage_users.py list
```

Verify the endpoint is actually closed (should print `403`):

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://nuvrail.example.com/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"stranger@example.com","password":"x"}'
# → 403
```

#### 6b. Invite mode — mint a code, then the user registers with it

With `NUVRAIL_SIGNUP_MODE=invite`, public registration works **only** with a
valid single-use code you mint. End-to-end:

```bash
# 1) mint a code (the raw code is shown ONCE — copy it; only its hash is stored)
docker compose exec gateway python3 scripts/manage_invites.py mint --note "for alice"
#   Invite code (shown once — copy it now):
#       <THE-CODE>

# 2) hand <THE-CODE> to the person out-of-band; they register with it:
curl -s -X POST https://nuvrail.example.com/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email": "alice@example.com", "password": "her-strong-password", "display_name": "Alice", "invite_code": "<THE-CODE>"}' \
  | python3 -m json.tool
#   → 201 with the new user; the code is now spent
```

Registering without a code (or with a spent/expired/revoked one) returns a
uniform `403`. See [Operating invite codes](#operating-invite-codes) below for
TTLs, listing, and revocation.

#### 6c. Open mode — public self-registration

With `NUVRAIL_SIGNUP_MODE=open` **and** `NUVRAIL_ALLOW_OPEN_SIGNUP=1` set (Step 1),
anyone can self-register from the web first-run screen or the API:

```bash
curl -s -X POST https://nuvrail.example.com/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email": "you@yourdomain.com", "password": "your-strong-password", "display_name": "Your Name"}' \
  | python3 -m json.tool
# → 201 with the new user
```

> ⚠️ **Open means open.** Every stranger who can reach this URL can create an
> account and start proxying mailboxes through your instance. Use it only for a
> deployment you actually intend to be public; prefer `invite` for a known group.

---

### Account creation (signup modes)

A Nuvrail deployment holds other people's mailboxes, so **who can create an
account is a first-class, fail-closed setting**, controlled by
`NUVRAIL_SIGNUP_MODE`:

| Mode | Public `/auth/register` | How accounts are created | Use for |
|---|---|---|---|
| `closed` **(default)** | disabled (403) | admin CLI only (`manage_users.py`) | private / single-tenant instances |
| `invite` | requires a valid single-use invite code | admin mints codes (`manage_invites.py`), user redeems one at registration | onboarding a known group without a manual step per person |
| `open` | enabled | anyone can self-register | genuinely public deployments |

Fail-closed by design:

- An **unset or unrecognized** `NUVRAIL_SIGNUP_MODE` resolves to `closed` — the
  safe state is the one you get by doing nothing.
- `open` additionally requires `NUVRAIL_ALLOW_OPEN_SIGNUP=1`. Setting
  `NUVRAIL_SIGNUP_MODE=open` **without** that ack makes the API **refuse to
  start**, so an intended-open box fails loudly instead of an intended-closed
  box silently opening.

**Closed mode — provision users (admin CLI):**

```bash
docker compose exec gateway python3 scripts/manage_users.py create alice@example.com --name "Alice"
docker compose exec gateway python3 scripts/manage_users.py list
```

**Invite mode — mint / list / revoke single-use codes (admin CLI):**

```bash
# mint a code (raw code is shown ONCE — copy it; only its hash is stored)
docker compose exec gateway python3 scripts/manage_invites.py mint --note "for alice"

# optional TTL: Nd / Nh / Nm (default: never expires)
docker compose exec gateway python3 scripts/manage_invites.py mint --expires-in 7d

# see all codes and their status (active / redeemed / expired / revoked)
docker compose exec gateway python3 scripts/manage_invites.py list

# kill an unspent code early (use the code_hash from `list`)
docker compose exec gateway python3 scripts/manage_invites.py revoke <code_hash>
```

The user then registers with the code:

```bash
curl -s -X POST https://nuvrail.example.com/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email": "alice@example.com", "password": "her-strong-password", "invite_code": "<the-code>"}'
```

Codes are **single-use** — spent atomically at redemption, so a code cannot be
used twice even under concurrent requests. A bad, expired, revoked, or
already-used code is rejected with a uniform `403` (the endpoint never reveals
which codes exist).

#### Operating invite codes

Day-to-day operator tasks for `invite` mode. All commands run inside the gateway
container so they talk to the same DB the API uses.

```bash
# Mint with an optional expiry. TTL is Nd / Nh / Nm (days/hours/minutes);
# omit --expires-in for a code that never expires.
docker compose exec gateway python3 scripts/manage_invites.py mint --expires-in 7d --note "onboarding-batch"

# List every code with its status and a hash prefix (never the raw code):
docker compose exec gateway python3 scripts/manage_invites.py list
#   status    created               expires               redeemed_by              code_hash
#   active    2026-07-23T...        2026-07-30T...         -                        3f9a...
#   redeemed  2026-07-20T...        never                 alice@example.com        7c21...
```

Status meanings:

| Status | Meaning |
|---|---|
| `active` | mintable/usable now — not yet redeemed, not expired, not revoked |
| `redeemed` | already spent by the shown account (single-use — cannot be reused) |
| `expired` | passed its `--expires-in` TTL before being redeemed |
| `revoked` | you killed it early (below) — permanently unusable |

```bash
# Kill an unspent code early. Use the FULL code_hash from `list`
# (revoking an already-redeemed/revoked/unknown hash is a no-op, exits non-zero):
docker compose exec gateway python3 scripts/manage_invites.py revoke <code_hash>
```

Operator guarantees to rely on: codes are **single-use** and spent atomically
(no double-spend under concurrency); only the **SHA-256 hash** is stored, so a
DB leak never reveals a usable code; and every rejection (bad / expired /
revoked / already-used / missing) returns an **identical `403`**, so the
endpoint never leaks which codes exist.

#### Switching modes on a running deployment

Signup mode is read from the environment, so changing it is: edit `.env`, then
recreate the gateway container.

```bash
# 1) edit .env — set the new NUVRAIL_SIGNUP_MODE (and NUVRAIL_ALLOW_OPEN_SIGNUP=1
#    if and only if the new mode is `open`)
nano .env

# 2) recreate the gateway so it picks up the new env, then confirm the mode:
docker compose up -d gateway
docker compose logs gateway --tail=10   # look for "Human account signup mode: <mode>"
```

Safety notes:

- **closed/invite → open** widens access immediately on restart. Double-check
  you meant to; remember `open` needs `NUVRAIL_ALLOW_OPEN_SIGNUP=1` or the
  gateway refuses to boot (fail-loud).
- **open → closed/invite** locks down new sign-ups on restart. **Existing
  accounts are unaffected** — this only closes the front door; it does not
  delete or disable anyone who already registered. Review existing accounts
  (`manage_users.py list`) if you are locking down after a public window.
- Switching **into `invite`** does not auto-mint anything — mint codes
  (above) before you tell users to register, or their `/auth/register` calls
  get a `403`.

---

## Connecting an email account

Nuvrail has setup flows for Gmail (OAuth2), Outlook / Office 365 (OAuth2), iCloud, and any standard IMAP/SMTP server. They are not all equally proven yet; see the status table below. Each connected account gets a unique agent credential — the username and token you give to your AI agent.

### Provider status

Not every provider has been run against a real account yet. Here is exactly
where each one stands, so you know what you are trying.

| Provider | Auth | Verified against a live account | Automated coverage |
|---|---|---|---|
| [Gmail / Google Workspace](docs/providers/gmail.md) | OAuth2 (XOAUTH2) | **Yes.** Google Workspace mailboxes on our own deployment, in use since 2026-09-10 | Unit tests: OAuth2 flow, token refresh, provider profile |
| [Outlook / Microsoft 365](docs/providers/outlook.md) | OAuth2 (XOAUTH2) | **Not yet** | Unit tests: OAuth2 flow, token refresh, provider profile |
| [iCloud Mail](docs/providers/icloud.md) | App-specific password | **Not yet** | Unit tests: provider profile, Sent/Trash folder mapping |
| [Any standard IMAP/SMTP](docs/providers/generic-imap.md) | Username + password over TLS | Not against a third-party provider. CI runs a local GreenMail IMAPS server end to end | Unit tests, plus the [3am demo live run](.github/workflows/demo-3am-live.yml) in CI on every gateway/API change: IMAP read passthrough, a staged delete held, rejected, mailbox untouched |

"Not yet" means the code and the setup guide exist and the unit tests pass,
but nobody has connected a real mailbox of that kind end to end. Outlook uses
the same XOAUTH2 wire path as Gmail, so the likeliest failures are in the
Azure app registration, scopes or endpoints, not in the proxy. If you run
Nuvrail against Outlook, iCloud or another provider, please
[open an issue](https://github.com/agens-field/nuvrail/issues/new/choose)
with what worked and what didn't. That is how a row moves to "Yes".

### Gmail via OAuth2 (recommended)

1. One-time server setup: register a Google Cloud OAuth2 client and set `GOOGLE_CLIENT_ID`
   / `GOOGLE_CLIENT_SECRET` — see **[docs/providers/gmail.md](docs/providers/gmail.md)**.
2. Go to `https://nuvrail.example.com` → **Agents** → **Connect Gmail**
3. Complete the Google OAuth2 consent flow
4. Copy the one-time agent token that appears on success — you won't see it again

### Outlook / Office 365 via OAuth2 (recommended)

Microsoft deprecated IMAP/SMTP basic auth for Exchange Online in 2023, so OAuth2
(Azure AD) is the only supported path for Outlook.com / Microsoft 365 accounts.

1. One-time server setup: register an Azure AD app and set `MICROSOFT_CLIENT_ID`
   / `MICROSOFT_CLIENT_SECRET` — see **[docs/providers/outlook.md](docs/providers/outlook.md)**.
2. Go to `https://nuvrail.example.com` → **Agents** → **Connect Outlook**
3. Complete the Microsoft consent flow
4. Copy the one-time agent token that appears on success — you won't see it again

### Standard IMAP/SMTP

The easiest way is the web interface — no API calls needed:

1. Go to `https://nuvrail.example.com` → **Agents** → **+ Add agent** → **Connect via IMAP**
2. Enter your IMAP/SMTP server details (host, ports, username, password). Any
   standard IMAP/SMTP provider works.
3. Copy the one-time agent token that appears on success — you won't see it again

Step-by-step walkthrough (settings, app-specific passwords, TLS, and
troubleshooting) for any standard mailbox: **[docs/providers/generic-imap.md](docs/providers/generic-imap.md)**.

<details>
<summary>Prefer the API? Same thing via <code>curl</code>:</summary>

```bash
curl -s -X POST https://nuvrail.example.com/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "label": "my-work-email",
    "upstream_host": "imap.yourdomain.com",
    "upstream_smtp_host": "smtp.yourdomain.com",
    "upstream_imap_port": 993,
    "upstream_smtp_port": 587,
    "upstream_user": "you@yourdomain.com",
    "upstream_password": "your-email-password"
  }' | python3 -m json.tool
# Returns agent_username + agent_token (one-time — copy it now)
```

</details>

---

## Configuring your AI agent

Point any IMAP/SMTP client at the Nuvrail proxy using the agent credentials. The agent doesn't need to know Nuvrail is there.

| Setting | Value |
|---|---|
| **IMAP host** | `nuvrail.example.com` |
| **IMAP port** | `993` (SSL) |
| **SMTP host** | `nuvrail.example.com` |
| **SMTP port** | `465` (SSL) |
| **Username** | agent username from setup (e.g. `nuvrail_abc123`) |
| **Password** | agent token from setup (one-time, copy it at creation) |

### Example: Claude / MCP email tool

```json
{
  "imap": {
    "host": "nuvrail.example.com",
    "port": 993,
    "ssl": true,
    "username": "nuvrail_abc123",
    "password": "your-agent-token"
  },
  "smtp": {
    "host": "nuvrail.example.com",
    "port": 465,
    "ssl": true,
    "username": "nuvrail_abc123",
    "password": "your-agent-token"
  }
}
```

**What the agent can do without approval:**
- Read any message (`FETCH`)
- Search (`SEARCH`, `UID SEARCH`)
- List folders (`LIST`, `LSUB`)
- Check status (`STATUS`, `NOOP`)

**What requires your approval:**
- Move or copy messages (`UID MOVE`, `UID COPY`)
- Flag changes (`UID STORE`)
- Send outbound email (SMTP `DATA`)
- Create or rename folders

**Permanently blocked:**
- `EXPUNGE` — the gateway never allows permanent deletion

---

## Approving operations

When an agent stages an operation:

1. **Push notification** arrives on your phone/browser (requires enabling notifications on the PWA)
2. Open the approval UI at `https://nuvrail.example.com`
3. Each pending operation shows: type, subject, sender, destination folder or recipient
4. Tap **Approve** to execute against the real mail server, or **Reject** to revert

Operations expire after 48 hours if not acted on — the agent receives a rejection and the state reverts.

You can also approve/reject via the API:

```bash
# List pending operations
curl -s https://nuvrail.example.com/api/v1/operations?status=pending \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# Approve
curl -s -X POST https://nuvrail.example.com/api/v1/operations/op_XXXXXX/approve \
  -H "Authorization: Bearer $TOKEN"

# Reject
curl -s -X POST https://nuvrail.example.com/api/v1/operations/op_XXXXXX/reject \
  -H "Authorization: Bearer $TOKEN"
```

---

## Development setup

```bash
git clone https://github.com/agens-field/nuvrail.git
cd nuvrail

# Python dependencies (requires Python 3.11+)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Run the gateway locally (needs a .env with NUVRAIL_MASTER_KEY set)
cp .env.example .env
python3 -m gateway.proxy    # IMAP proxy on :10143
python3 -m gateway.smtp_proxy   # SMTP proxy on :10587
uvicorn api.main:app --port 8080    # REST API

# Web UI
cd web
npm install
npm run dev     # Vite dev server on :5173
```

---

## Operations

### Updating the deployment

```bash
cd /opt/nuvrail
git pull
docker compose build gateway
docker compose up -d gateway
```

The `nuvrail_data` Docker volume persists across rebuilds — your database, VAPID keys, and (if stored there) the master key all survive redeployment.

### Useful commands

```bash
# Container status
docker compose ps

# Follow gateway logs
docker compose logs gateway -f --tail=50

# Inspect pending operations in the DB directly
docker compose exec gateway python3 -c "
import asyncio
from pathlib import Path
from gateway.state_db import get_db
async def main():
    async with get_db(Path('/data/nuvrail.db')) as db:
        async with db.execute(
            'SELECT id, op_type, status, folder_from, created_at '
            'FROM staged_operations ORDER BY created_at DESC LIMIT 10'
        ) as c:
            for row in await c.fetchall():
                print(dict(row))
asyncio.run(main())
"

# Check OAuth2 token health for an agent
docker compose exec gateway python3 scripts/check_oauth2_token.py nuvrail_XXXXXXXX

# Test IMAP connectivity directly (bypasses proxy)
docker compose exec gateway python3 scripts/test_imap_direct.py nuvrail_XXXXXXXX

# Renew TLS certificates
sudo certbot renew && sudo systemctl reload nginx
```

### Firewall

```bash
sudo ufw allow 22    # SSH
sudo ufw allow 80    # HTTP → HTTPS redirect
sudo ufw allow 443   # HTTPS: approval UI + API
sudo ufw allow 993   # IMAP SSL: AI agent connects here
sudo ufw allow 465   # SMTPS: AI agent connects here
sudo ufw enable

# Do NOT expose ports 10143, 10587, or 8080 to the internet —
# these are internal Docker ports, accessed only via nginx.
```

---

## License

Nuvrail core is licensed under the **GNU Affero General Public License v3.0**
(AGPL-3.0) — see [LICENSE](LICENSE). Copyright © 2026 Agens Field.

Under the AGPL's network-use clause, anyone who runs a modified version as a
network service must make their modified source available to its users.

The communications and security core — everything that proxies mail, stages
changes, and enforces the approval and audit guarantees — is and remains open
source under AGPL-3.0. Commercial or enterprise features may be built as
separate add-on packages layered *on top of* this project (Nuvrail exposes
plugin seams for exactly this), shipped under whatever license their authors
choose. Those add-ons are **not** part of this repository. The one constraint
is that the open-source core stays open: an add-on extends it, it does not
replace or close it.

---

## Security model

- **Credentials at rest:** all upstream passwords and OAuth2 tokens are encrypted with AES-256-GCM using `NUVRAIL_MASTER_KEY`. The key never leaves the host.
- **TLS everywhere:** all external connections use TLS. The internal Docker network uses plain TCP between nginx and the proxy containers — nginx terminates TLS before handing off.
- **No EXPUNGE:** permanent deletion is blocked at the protocol level, unconditionally. The gateway cannot be configured to allow it.
- **Per-agent isolation:** each AI agent has its own credentials and can only access the upstream account it was created for. There is no path for one agent to affect another agent's mailbox.
- **Audit log:** every staged operation, approval, rejection, and execution is recorded in the database with a timestamp. The log is append-only.
- **Rate limiting:** repeated authentication failures from a single IP trigger a lockout.
- **Push payload privacy:** the push payload carries the operation ID, an urgency flag, and a short description of the action (for example the recipient and subject, or the sender and subject). That text is shown in the OS notification so you can triage from the lock screen. The payload is end-to-end encrypted to your subscribed browser/device (Web Push, RFC 8291 `aes128gcm`), so the push relay (FCM, Mozilla autopush, Apple) cannot read it. The email body is never in the push payload; full detail is fetched over authenticated HTTPS when you open the app. If lock-screen previews are a concern on a shared device, turn off notification previews in your OS settings.

---

## Security

### Secret scanning (gitleaks)

Nuvrail uses [gitleaks](https://github.com/gitleaks/gitleaks) to prevent credential leaks before they reach the remote.

A pre-push hook runs `gitleaks detect` automatically on every `git push`. The hook is in `.git/hooks/pre-push` — it is a no-op if gitleaks is not installed (prints a warning, doesn't block).

**Installation:**

```bash
# macOS
brew install gitleaks

# Ubuntu 22.04+
sudo apt install gitleaks

# From source (requires Go)
go install github.com/gitleaks/gitleaks/v8@latest
```

**Manual scan:**

```bash
gitleaks detect --source . --no-git --config .gitleaks.toml
```

The Nuvrail-specific rules are in `.gitleaks.toml` (committed to the repo). They block:
- `.env` files containing secrets
- `master.key` (AES-256-GCM key material)
- `vapid_private.pem` (Web Push credentials)
- Files under `~/.nuvrail/` (runtime data directory)
- Google OAuth2 `client_secret.json` files

If you hit a false positive, add an allowlist entry to `.gitleaks.toml` and commit it.

### npm audit status

| Directory | Status |
|-----------|--------|
| `web/` | 0 vulnerabilities ✅ |
