#!/usr/bin/env bash
# Boot-smoke the README "Try it in 60 seconds" path, end to end.
#
# CI used to stop at `docker compose build`, so a stack that builds but does not
# come up (bad entrypoint, nginx /api/ proxy broken, manage_users.py crashing on
# a fresh DB) shipped green. This script walks the same path a first-time user
# does and fails loudly at the first step that would have failed for them.
#
#   cp .env.example .env            (caller does this, exactly as the README says)
#        │
#        ▼
#   docker compose up -d --build
#        │
#        ▼
#   1. gateway  GET :8080/health            → {"status": "ok"}   (poll, 180s cap)
#   2. IMAP     :10143 greeting             → "* OK"
#   3. SMTP     :10587 greeting             → "220"
#   4. account  manage_users.py create      → exit 0             (closed signup)
#   5. web      GET :3000/                  → 200, SPA shell
#   6. login    POST :3000/api/v1/auth/login → 200 + token        (web → gateway proxy)
#   7. authed   GET :3000/api/v1/account/token with that token → 200
#   8. gateway container RestartCount == 0  (no crash-loop hidden by restart policy)
#        │
#        ▼
#   docker compose down -v   (always, via trap; logs dumped on failure)
#
# Run locally from the repo root:  cp .env.example .env && scripts/quickstart_smoke.sh
# It uses the host ports from .env / the compose defaults, so stop any running
# Nuvrail stack first. It tears down with `down -v`, which DELETES the
# nuvrail_data volume — do not run it against a stack whose data you want.
set -euo pipefail

if [ ! -f .env ]; then
  echo "error: no .env — run 'cp .env.example .env' first (the README's step)." >&2
  exit 2
fi
# Same host-port overrides docker-compose.yml reads (all optional). Parsed, not
# sourced: .env is compose syntax, not shell, and must not be executed.
env_get() {
  sed -n "s/^[[:space:]]*$1=\([^#[:space:]]*\).*/\1/p" .env | tail -n 1
}
API_PORT="$(env_get NUVRAIL_HOST_API_PORT)";  API_PORT="${API_PORT:-8080}"
WEB_PORT="$(env_get NUVRAIL_HOST_WEB_PORT)";  WEB_PORT="${WEB_PORT:-3000}"
IMAP_PORT="$(env_get NUVRAIL_HOST_IMAP_PORT)"; IMAP_PORT="${IMAP_PORT:-10143}"
SMTP_PORT="$(env_get NUVRAIL_HOST_SMTP_PORT)"; SMTP_PORT="${SMTP_PORT:-10587}"
HEALTH_TIMEOUT_S="${SMOKE_HEALTH_TIMEOUT_S:-180}"

SMOKE_EMAIL="smoke@example.com"
# Throwaway password for a throwaway account in a volume deleted on exit.
SMOKE_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

step() { echo; echo "==> $*"; }
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo; echo "---- docker compose ps ----"; docker compose ps -a || true
    echo; echo "---- docker compose logs (tail 200) ----"
    docker compose logs --no-color --tail=200 || true
  fi
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  exit "$rc"
}
trap cleanup EXIT

# Read a banner line from host:port; prints it (CR stripped) or fails.
banner() {
  python3 - "$1" "$2" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
with socket.create_connection((host, port), timeout=10) as s:
    s.settimeout(10)
    print(s.recv(512).decode("utf-8", "replace").strip())
PY
}

step "docker compose up -d --build"
docker compose up -d --build

step "1. gateway health on :$API_PORT (up to ${HEALTH_TIMEOUT_S}s)"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_S ))
until body="$(curl -fsS "http://127.0.0.1:${API_PORT}/health" 2>/dev/null)" \
      && python3 -c 'import json,sys; sys.exit(json.loads(sys.argv[1]).get("status") != "ok")' "$body"; do
  [ "$(date +%s)" -lt "$deadline" ] || fail "gateway /health not ok after ${HEALTH_TIMEOUT_S}s"
  sleep 2
done
echo "health: $body"

step "2. IMAP proxy greeting on :$IMAP_PORT"
imap="$(banner 127.0.0.1 "$IMAP_PORT")" || fail "no IMAP greeting on :$IMAP_PORT"
echo "imap: $imap"
case "$imap" in "* OK"*) ;; *) fail "unexpected IMAP greeting: $imap" ;; esac

step "3. SMTP proxy greeting on :$SMTP_PORT"
smtp="$(banner 127.0.0.1 "$SMTP_PORT")" || fail "no SMTP greeting on :$SMTP_PORT"
echo "smtp: $smtp"
case "$smtp" in "220"*) ;; *) fail "unexpected SMTP greeting: $smtp" ;; esac

step "4. create the first account with scripts/manage_users.py (closed signup)"
# README form prompts for the password; -T + --password is the non-interactive
# equivalent. The bearer token it prints is for a throwaway DB, but keep it out
# of the CI log anyway.
docker compose exec -T gateway python3 scripts/manage_users.py create \
  "$SMOKE_EMAIL" --name "Smoke" --password "$SMOKE_PASSWORD" >/dev/null \
  || fail "manage_users.py create exited non-zero"
docker compose exec -T gateway python3 scripts/manage_users.py list | grep -q "$SMOKE_EMAIL" \
  || fail "created account not listed by manage_users.py list"
echo "account created: $SMOKE_EMAIL"

step "5. web app on :$WEB_PORT"
html="$(curl -fsS "http://127.0.0.1:${WEB_PORT}/")" || fail "web :$WEB_PORT did not return 200"
grep -q '<div id="root">' <<<"$html" || fail "web :$WEB_PORT returned 200 but not the SPA shell"
echo "web: 200, SPA shell served"

step "6. log in through the web origin (nginx /api/ → gateway)"
login="$(curl -fsS -X POST "http://127.0.0.1:${WEB_PORT}/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  --data "$(python3 -c 'import json,sys; print(json.dumps({"email": sys.argv[1], "password": sys.argv[2]}))' "$SMOKE_EMAIL" "$SMOKE_PASSWORD")")" \
  || fail "login via :$WEB_PORT/api/v1/auth/login failed"
token="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["token"])' "$login")" \
  || fail "login response had no token"
echo "login: 200, token issued"

step "7. authenticated request through the web origin"
code="$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${token}" "http://127.0.0.1:${WEB_PORT}/api/v1/account/token")"
[ "$code" = "200" ] || fail "GET /api/v1/account/token returned $code, want 200"
echo "authed: 200"

step "8. gateway did not crash-loop"
restarts="$(docker inspect -f '{{.RestartCount}}' "$(docker compose ps -q gateway)")"
[ "$restarts" = "0" ] || fail "gateway container restarted $restarts time(s)"
echo "gateway restarts: 0"

echo; echo "SMOKE OK: README quickstart boots, serves, and authenticates."
