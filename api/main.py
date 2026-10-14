"""
Nuvrail Approval REST API — Milestone 1.0+.

FastAPI application exposing:
  /api/v1/operations  — list, approve, reject
  /api/v1/audit       — audit log (read-only)

Base URL: http://localhost:8080/api/v1

Background tasks:
  - Operation expiry loop: runs every hour, expires pending ops past 48h,
    reverts local state, writes audit entries.

Sub-milestone: 1.0 (operations CRUD)
Sub-milestone: 1.2 (audit, expiry)
Sub-milestone: 3.3 (auto-approval rules)
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from api.limiter import limiter
from api.routes import account, audit, auth, config, features, health, oauth2, operations, push
from api.signup import resolve_signup_mode
from gateway.audit import run_audit_verification_loop
from gateway.expiry import run_expiry_loop
from gateway.extensions import load_plugins
from gateway.retention import run_retention_loop
from gateway.scheduler import run_scheduled_execution_loop
from gateway.scrubber import run_scrubber_loop
from gateway.state_db import DB_PATH, init_db

logger = logging.getLogger(__name__)

# Distribution name in pyproject.toml. The version lives in exactly one place
# (pyproject.toml [project].version); the release workflow checks it against
# the tag and web/package.json, so the API must read it rather than repeat it.
_DIST_NAME = "nuvrail-gateway"
_UNKNOWN_VERSION = "0+unknown"


def _package_version() -> str:
    """Return the installed nuvrail-gateway version.

    CI and the Docker image both ``pip install -e .``, so package metadata is
    present there. A bare checkout run without installing has no metadata;
    report a PEP 440 placeholder instead of a stale hardcoded number.
    """
    try:
        return importlib.metadata.version(_DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return _UNKNOWN_VERSION


_EXPIRY_INTERVAL = float(os.environ.get("NUVRAIL_EXPIRY_INTERVAL_SECONDS", "3600"))
_EXPIRY_INITIAL_DELAY = float(os.environ.get("NUVRAIL_EXPIRY_INITIAL_DELAY_SECONDS", "60"))
_SCRUBBER_INTERVAL = float(os.environ.get("NUVRAIL_SCRUBBER_INTERVAL_SECONDS", "3600"))
_SCRUBBER_INITIAL_DELAY = float(os.environ.get("NUVRAIL_SCRUBBER_INITIAL_DELAY_SECONDS", "90"))
# Cool-down scheduler runs frequently (deadlines are minutes, not hours).
_SCHEDULER_INTERVAL = float(os.environ.get("NUVRAIL_SCHEDULER_INTERVAL_SECONDS", "30"))
_SCHEDULER_INITIAL_DELAY = float(os.environ.get("NUVRAIL_SCHEDULER_INITIAL_DELAY_SECONDS", "15"))
# Data-retention purge: deleted accounts hard-purged after the window (R5).
# Sweep daily — the window is months, so daily granularity is ample.
_RETENTION_INTERVAL = float(os.environ.get("NUVRAIL_RETENTION_INTERVAL_SECONDS", "86400"))
_RETENTION_INITIAL_DELAY = float(os.environ.get("NUVRAIL_RETENTION_INITIAL_DELAY_SECONDS", "120"))
# Audit-log chain verification: the hash chain is only tamper-*evident* if
# something verifies it. Sweep hourly and alert (ERROR log) on any break.
_AUDIT_VERIFY_INTERVAL = float(os.environ.get("NUVRAIL_AUDIT_VERIFY_INTERVAL_SECONDS", "3600"))
_AUDIT_VERIFY_INITIAL_DELAY = float(os.environ.get("NUVRAIL_AUDIT_VERIFY_INITIAL_DELAY_SECONDS", "45"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize DB and start background tasks on startup; clean up on shutdown."""
    await init_db()

    # Start operation expiry background loop
    expiry_task = asyncio.create_task(
        run_expiry_loop(
            db_path=DB_PATH,
            interval_seconds=_EXPIRY_INTERVAL,
            initial_delay_seconds=_EXPIRY_INITIAL_DELAY,
        ),
        name="nuvrail-expiry-loop",
    )
    # Start body-preview scrubber background loop (issue #29)
    scrubber_task = asyncio.create_task(
        run_scrubber_loop(
            db_path=DB_PATH,
            interval_seconds=_SCRUBBER_INTERVAL,
            initial_delay_seconds=_SCRUBBER_INITIAL_DELAY,
        ),
        name="nuvrail-scrubber-loop",
    )
    # Start cool-down scheduled-execution loop (Tier 2 approve_after rules).
    # No-op in builds with no rules engine — nothing ever sets
    # scheduled_execute_at — so it is safe to always run.
    scheduler_task = asyncio.create_task(
        run_scheduled_execution_loop(
            db_path=DB_PATH,
            interval_seconds=_SCHEDULER_INTERVAL,
            initial_delay_seconds=_SCHEDULER_INITIAL_DELAY,
        ),
        name="nuvrail-scheduler-loop",
    )
    # Start data-retention purge loop (R5 — GDPR storage limitation): hard-
    # deletes accounts whose deleted_at is older than NUVRAIL_RETENTION_DAYS.
    retention_task = asyncio.create_task(
        run_retention_loop(
            db_path=DB_PATH,
            interval_seconds=_RETENTION_INTERVAL,
            initial_delay_seconds=_RETENTION_INITIAL_DELAY,
        ),
        name="nuvrail-retention-loop",
    )
    # Start audit-log chain verification loop (tamper-evidence is only useful if
    # something checks it; logs at ERROR on any chain break).
    audit_verify_task = asyncio.create_task(
        run_audit_verification_loop(
            db_path=DB_PATH,
            interval_seconds=_AUDIT_VERIFY_INTERVAL,
            initial_delay_seconds=_AUDIT_VERIFY_INITIAL_DELAY,
        ),
        name="nuvrail-audit-verify-loop",
    )
    logger.info(
        "Nuvrail API started — expiry every %.0fs, scrubber every %.0fs, "
        "scheduler every %.0fs, retention every %.0fs, audit-verify every %.0fs",
        _EXPIRY_INTERVAL, _SCRUBBER_INTERVAL, _SCHEDULER_INTERVAL, _RETENTION_INTERVAL,
        _AUDIT_VERIFY_INTERVAL,
    )

    yield

    # Cancel background tasks on shutdown
    for task in (expiry_task, scrubber_task, scheduler_task, retention_task, audit_verify_task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    logger.info("Nuvrail API shutdown — background loops stopped")


app = FastAPI(title="Nuvrail Approval API", version=_package_version(), lifespan=lifespan)

# Rate limiting (slowapi) — attached before CORS so 429s get proper headers.
# The limiter is stored on app.state so route decorators can reference it.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

def resolve_cors_origins(raw: str, env: str) -> list[str]:
    """Resolve the allowed CORS origins, failing closed in production.

    - Explicit ``NUVRAIL_CORS_ORIGINS`` (comma-separated) → that parsed list.
    - Empty in **production** (``NUVRAIL_ENV`` in {prod, production}) → raises
      RuntimeError. A wildcard CORS policy lets any website drive the API in a
      logged-in user's browser, so a missing env var must not silently ship as
      an open policy — refuse to start instead.
    - Empty otherwise (dev/test/ci) → ``["*"]`` for local convenience.
    """
    parsed = [o.strip() for o in raw.split(",") if o.strip()]
    if parsed:
        return parsed
    if env.strip().lower() in ("prod", "production"):
        raise RuntimeError(
            f"NUVRAIL_CORS_ORIGINS must be set when NUVRAIL_ENV={env!r}. "
            "Refusing to start with wildcard ('*') CORS in production — set it "
            "to your deployed origin(s), e.g. https://app.nuvrail.com."
        )
    logger.warning(
        "NUVRAIL_CORS_ORIGINS is not set — allowing all origins (*) in "
        "non-production env %r. This is refused in production (NUVRAIL_ENV=prod).",
        env,
    )
    return ["*"]


_CORS_ORIGINS = resolve_cors_origins(
    os.environ.get("NUVRAIL_CORS_ORIGINS", ""),
    os.environ.get("NUVRAIL_ENV", "dev"),
)


# Resolve the human-account signup policy once at startup (fail-closed). This
# also raises immediately if NUVRAIL_SIGNUP_MODE=open lacks its ack env var, so
# a misconfigured "open" box refuses to boot rather than silently accepting
# public registration. The register route re-resolves at request time via
# api.signup.current_signup_mode() against the same function — one source of
# truth, no drift.
SIGNUP_MODE = resolve_signup_mode(
    os.environ.get("NUVRAIL_SIGNUP_MODE", ""),
    os.environ.get("NUVRAIL_ALLOW_OPEN_SIGNUP", ""),
)
logger.info("Human account signup mode: %s", SIGNUP_MODE)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /health is intentionally unprefixed — fly.io probes it at the root path.
app.include_router(health.router)
app.include_router(operations.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
app.include_router(push.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(oauth2.router, prefix="/api/v1")
app.include_router(account.router, prefix="/api/v1")
app.include_router(features.router, prefix="/api/v1")
app.include_router(config.router, prefix="/api/v1")

# Load optional plugins (the enterprise package, if installed). No-op in the
# public build. Registers plugin routers (e.g. the auto-approval /rules API),
# migrations, entitlements, and the auto-decision provider. Runs after core
# routers so plugins can extend them.
load_plugins(app)
