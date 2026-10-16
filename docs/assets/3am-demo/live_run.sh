#!/usr/bin/env bash
# The 3am demo, run live end to end with no human and no real mailbox.
#
# README.md's steps 1-6 need a throwaway mailbox, a browser and someone to
# click Reject. This script does the same steps against a local GreenMail
# IMAPS server (compose.live.yml) so CI can prove the demo's claims on every
# change, and so stage 2 (screen capture) has a known-good run to film.
#
#   1. throwaway CA + cert for "mail"  (openssl; deleted on exit)
#   2. docker compose up gateway + mail       (compose.live.yml override)
#   3. seed_mailbox.py  ──direct IMAPS──► mail          40 msgs, 38 match
#   4. manage_users.py create; POST /api/v1/agents (upstream = mail:3993)
#   5. agent.py ──:10143──► Nuvrail ──► mail
#        assert: "38 messages", "OK [STAGED]", "OK Noted"
#   6. verify_mailbox.py  (server side)       40 msgs, 0 \Deleted  ← held, not executed
#   7. GET /api/v1/operations?status=pending  exactly 1 op; POST .../reject
#        (DEMO_CAPTURE=1: capture/capture.mjs clicks Reject in the real UI
#         instead, and the op is re-read over the API: must be 'rejected')
#   8. verify_mailbox.py  (server side)       40 msgs, 0 \Deleted  ← rejected = untouched
#   9. docker compose down -v                 (always, via trap)
#
# Run from the repo root:  cp .env.example .env && docs/assets/3am-demo/live_run.sh
# Needs docker, openssl, python3 (stdlib only). Uses host ports 8080, 10143
# and 3993; stop any running Nuvrail stack first. `down -v` DELETES the
# nuvrail_data volume. Every credential is random per run and never printed.
# DEMO_ARTIFACT_DIR (optional) receives the agent transcript and listings.
# DEMO_CAPTURE=1 (optional) also starts the web UI (host port 3000) and films
# step 7 with Playwright into DEMO_CAPTURE_DIR; needs node + capture/'s deps.
set -euo pipefail

[ -f .env ] || { echo "error: no .env; run 'cp .env.example .env' first." >&2; exit 2; }
DEMO_DIR="docs/assets/3am-demo"
ART="${DEMO_ARTIFACT_DIR:-}"
[ -n "$ART" ] && mkdir -p "$ART"

COMPOSE=(docker compose -f docker-compose.yml -f "$DEMO_DIR/compose.live.yml")
API="http://127.0.0.1:8080"

rand() { python3 -c 'import secrets; print(secrets.token_hex(16))'; }
mask() { [ -n "${GITHUB_ACTIONS:-}" ] && echo "::add-mask::$1"; return 0; }
step() { echo; echo "==> $*"; }
fail() { echo "LIVE RUN FAIL: $*" >&2; exit 1; }
keep() { [ -n "$ART" ] && tee "$ART/$1" || cat; }

DEMO_CERT_DIR="$(mktemp -d)"
DEMO_MAIL_PASSWORD="$(rand)"; DEMO_KEYSTORE_PASSWORD="$(rand)"
USER_EMAIL="demo-owner@example.com"; USER_PASSWORD="$(rand)"
mask "$DEMO_MAIL_PASSWORD"; mask "$DEMO_KEYSTORE_PASSWORD"; mask "$USER_PASSWORD"
export DEMO_CERT_DIR DEMO_MAIL_PASSWORD DEMO_KEYSTORE_PASSWORD

cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo; echo "---- compose ps ----"; "${COMPOSE[@]}" ps -a || true
    echo; echo "---- compose logs (tail 200) ----"; "${COMPOSE[@]}" logs --no-color --tail=200 || true
  fi
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$DEMO_CERT_DIR"
  exit "$rc"
}
trap cleanup EXIT

step "1. throwaway CA + server cert for 'mail' (this run only)"
(
  cd "$DEMO_CERT_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=Nuvrail 3am demo throwaway CA" \
    -keyout ca.key -out ca.pem 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=mail" -keyout mail.key -out mail.csr 2>/dev/null
  printf 'subjectAltName=DNS:mail,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' > ext.cnf
  openssl x509 -req -in mail.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 1 \
    -extfile ext.cnf -out mail.crt 2>/dev/null
  openssl pkcs12 -export -name mail -inkey mail.key -in mail.crt -certfile ca.pem \
    -passout "pass:$DEMO_KEYSTORE_PASSWORD" -out mail.p12
  rm -f ca.key mail.key mail.csr ca.srl ext.cnf
  chmod 755 . && chmod 644 ./*
)
echo "CA + cert ready (private keys exist only inside mail.p12, deleted on exit)"

CAPTURE="${DEMO_CAPTURE:-}"
WEB="http://127.0.0.1:${NUVRAIL_HOST_WEB_PORT:-3000}"
SERVICES=(gateway mail); [ -n "$CAPTURE" ] && SERVICES+=(web)

step "2. docker compose up ${SERVICES[*]}"
"${COMPOSE[@]}" up -d --build "${SERVICES[@]}"
deadline=$(( $(date +%s) + 180 ))
until curl -fsS "$API/health" >/dev/null 2>&1; do
  [ "$(date +%s)" -lt "$deadline" ] || fail "gateway /health not up after 180s"; sleep 2
done
# Wait for an IMAPS greeting that verifies against the demo CA (proves the cert).
until python3 - "$DEMO_CERT_DIR/ca.pem" <<'PY' 2>/dev/null
import socket, ssl, sys
ctx = ssl.create_default_context(cafile=sys.argv[1])
with socket.create_connection(("127.0.0.1", 3993), timeout=5) as s, \
     ctx.wrap_socket(s, server_hostname="localhost") as t:
    assert t.recv(256).startswith(b"* OK")
PY
do
  [ "$(date +%s)" -lt "$deadline" ] || fail "mail IMAPS :3993 not up (or cert does not verify) after 180s"; sleep 2
done
echo "gateway healthy; mail IMAPS up, cert verifies against the demo CA"
if [ -n "$CAPTURE" ]; then
  until curl -fsS "$WEB/" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "web UI $WEB not up after 180s"; sleep 2
  done
  echo "web UI up at $WEB"
fi

export SEED_IMAP_HOST=127.0.0.1 SEED_IMAP_PORT=3993 SEED_IMAP_USER=demo@demo.test \
       SEED_IMAP_PASSWORD="$DEMO_MAIL_PASSWORD"

step "3. seed the mailbox directly (bypasses Nuvrail)"
python3 "$DEMO_DIR/seed_mailbox.py"
python3 "$DEMO_DIR/verify_mailbox.py" --expect-total 40 --expect-deleted 0 | keep 0-seeded.txt

step "4. create the owner account and connect the mailbox as an agent"
"${COMPOSE[@]}" exec -T gateway python3 scripts/manage_users.py create \
  "$USER_EMAIL" --name "Demo owner" --password="$USER_PASSWORD" >/dev/null \
  || fail "manage_users.py create failed"
json() { python3 -c "import json,sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))" "$@"; }
field() { python3 -c "import json,sys; print(json.loads(sys.stdin.read())[sys.argv[1]])" "$1"; }
TOKEN="$(curl -fsS -X POST "$API/api/v1/auth/login" -H 'Content-Type: application/json' \
  --data "$(json email "$USER_EMAIL" password "$USER_PASSWORD")" | field token)" || fail "login failed"
mask "$TOKEN"
AUTH=(-H "Authorization: Bearer $TOKEN")
agent="$(curl -fsS -X POST "$API/api/v1/agents" "${AUTH[@]}" -H 'Content-Type: application/json' \
  --data "$(python3 -c 'import json,sys; print(json.dumps({"label": "3am demo", "upstream_host": "mail", "upstream_imap_port": 3993, "upstream_smtp_port": 3465, "upstream_user": "demo@demo.test", "upstream_password": sys.argv[1]}))' "$DEMO_MAIL_PASSWORD")")" \
  || fail "POST /api/v1/agents failed"
NUVRAIL_AGENT_USER="$(field agent_username <<<"$agent")"
NUVRAIL_AGENT_TOKEN="$(field agent_token <<<"$agent")"
mask "$NUVRAIL_AGENT_TOKEN"
export NUVRAIL_AGENT_USER NUVRAIL_AGENT_TOKEN
echo "agent connected: $NUVRAIL_AGENT_USER -> demo@demo.test on mail:3993"

step "5. run the agent through the proxy (localhost:10143)"
out="$(python3 "$DEMO_DIR/agent.py")" || fail "agent.py exited non-zero"
printf '%s\n' "$out" | keep 1-agent.txt
grep -q '38 messages "resolved"' <<<"$out" || fail "agent did not find 38 messages"
grep -q 'OK \[STAGED\] Operation queued' <<<"$out" || fail "UID STORE was not staged"
grep -A1 '^agent> EXPUNGE' <<<"$out" | grep -q 'OK Noted' || fail "EXPUNGE reply was not 'OK Noted'"

step "6. server-side listing while the op is pending: nothing touched"
python3 "$DEMO_DIR/verify_mailbox.py" --expect-total 40 --expect-deleted 0 | keep 2-pending.txt

if [ -n "$CAPTURE" ]; then
  step "7. reject the staged operation: real Reject tap in the approval UI (filmed)"
else
  step "7. reject the staged operation (the 'Reject' tap, via the same API the UI uses)"
fi
pending="$(curl -fsS "$API/api/v1/operations?status=pending" "${AUTH[@]}")"
op_id="$(python3 -c '
import json, sys
ops = json.loads(sys.stdin.read())["operations"]
for o in ops:
    print(o["id"], o["op_type"], o.get("imap_command"), file=sys.stderr)
if len(ops) != 1:
    sys.exit(f"want exactly 1 pending op, got {len(ops)}")
print(ops[0]["id"])' <<<"$pending")" || fail "pending-operations check failed"
if [ -n "$CAPTURE" ]; then
  DEMO_WEB_URL="$WEB" DEMO_UI_TOKEN="$TOKEN" \
    node "$DEMO_DIR/capture/capture.mjs" ui || fail "UI capture (Reject tap) failed"
  status="$(curl -fsS "$API/api/v1/operations/$op_id" "${AUTH[@]}" | field status)"
else
  status="$(curl -fsS -X POST "$API/api/v1/operations/$op_id/reject" "${AUTH[@]}" | field status)"
fi
[ "$status" = "rejected" ] || fail "reject returned status '$status'"
echo "rejected $op_id" | keep 3-rejected.txt

step "8. server-side listing after reject: all 40 still there, none flagged"
python3 "$DEMO_DIR/verify_mailbox.py" --expect-total 40 --expect-deleted 0 | keep 4-after-reject.txt

echo; echo "LIVE RUN OK: staged, EXPUNGE swallowed, rejected, mailbox untouched (local test mail server)."
