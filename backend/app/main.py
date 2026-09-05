"""NutriAI API entry point."""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .errors import install_error_handlers
from .logging_conf import configure_logging
from .routers import ALL_ROUTERS

configure_logging()
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("nutriai_starting", env=settings.env, prefix=settings.api_prefix)
    # supabase_jwt_secret is deliberately absent: projects using asymmetric
    # signing keys have no shared secret, and an empty value is correct there.
    # security.py picks the verification path from the token's own header.
    missing = [
        k for k in ("supabase_url", "supabase_service_key")
        if not getattr(settings, k)
    ]
    if missing and settings.is_prod:
        raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")
    if missing:
        log.warning("config_incomplete", missing=missing)
    yield
    log.info("nutriai_stopping")


app = FastAPI(
    title="NutriAI API",
    version="1.0.0",
    description=(
        "AI nutrition, fitness and lifestyle platform. All endpoints except "
        "`/healthz`, `/v1/billing/plans` and `/v1/webhooks/stripe` require a "
        "Supabase bearer token."
    ),
    lifespan=lifespan,
    docs_url=None if settings.is_prod else "/docs",
    redoc_url=None if settings.is_prod else "/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ---------------------------------------------------------------------------
# Request id + timing
# ---------------------------------------------------------------------------
@app.middleware("http")
async def observability(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = rid
    structlog.contextvars.bind_contextvars(request_id=rid, path=request.url.path)
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    ms = int((time.perf_counter() - t0) * 1000)
    response.headers["x-request-id"] = rid
    response.headers["server-timing"] = f"app;dur={ms}"
    if ms > 3000:
        log.warning("slow_request", path=request.url.path, ms=ms)
    return response


# ---------------------------------------------------------------------------
# Rate limiting
#
# In-process sliding window. Fine for a single instance; swap the store for
# Redis (see docs/DEPLOYMENT.md) the moment you run more than one.
# ---------------------------------------------------------------------------
_hits: dict[str, deque[float]] = defaultdict(deque)
# Provider webhooks must never be rate limited: Apple and Google burst
# notifications after an outage, and a 429 to Stripe starts a 3-day retry storm.
EXEMPT = {
    "/healthz", "/readyz",
    "/v1/webhooks/stripe", "/v1/webhooks/apple", "/v1/webhooks/google",
}


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path in EXEMPT:
        return await call_next(request)
    key = request.headers.get("authorization", "")[-32:] or (
        request.client.host if request.client else "anon"
    )
    now = time.time()
    window = _hits[key]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= settings.rate_limit_per_minute:
        return JSONResponse(
            status_code=429,
            content={"error": {"code": "rate_limited",
                               "message": "Too many requests. Try again in a minute."}},
            headers={"retry-after": "60"},
        )
    window.append(now)
    return await call_next(request)


install_error_handlers(app)

for r in ALL_ROUTERS:
    app.include_router(r, prefix=settings.api_prefix)


@app.get("/healthz", tags=["ops"])
async def healthz():
    return {"ok": True, "service": "nutriai-api", "version": app.version, "env": settings.env}


@app.get("/readyz", tags=["ops"])
async def readyz():
    """Readiness: can we actually reach Supabase?"""
    try:
        from .db import service

        service().table("exercises").select("slug").limit(1).execute()
        from .security import auth_mode

        # Surface how tokens will be verified. A deployment with neither a JWKS
        # nor a legacy secret answers every request with 401, and that should be
        # visible here rather than discovered by a user.
        auth = auth_mode()
        ready = bool(auth["jwks_keys_cached"] or auth["legacy_hs256_secret_configured"])
        return {
            "ok": ready,
            "database": "reachable",
            "auth": auth,
            **({} if ready else {"warning": "No JWKS keys and no legacy JWT secret — auth will reject every request."}),
        }
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={"ok": False, "database": "unreachable", "detail": str(exc)[:200]},
        )
