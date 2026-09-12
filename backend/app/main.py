"""NeutriAI API entry point."""
from __future__ import annotations

import time
import uuid
from collections import OrderedDict, deque
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
    log.info("neutriai_starting", env=settings.env, prefix=settings.api_prefix)
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
    log.info("neutriai_stopping")


app = FastAPI(
    title="NeutriAI API",
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
# Bounded on purpose. The old store was a defaultdict keyed on whatever the
# caller put in the Authorization header, and it never evicted anything: 50,000
# requests with a rotating header left 50,000 buckets behind, a slow memory leak
# an attacker controls the rate of. An LRU with a hard cap cannot be grown.
RATE_LIMIT_BUCKETS = 20_000
_hits: OrderedDict[str, deque[float]] = OrderedDict()


def _client_key(request: Request) -> str:
    """Who to count this request against.

    THE ADDRESS, NOT THE TOKEN, and that is the fix.

    This middleware runs before authentication, so anything it reads from the
    request is unverified. The old key was the last 32 characters of the
    Authorization header, which the caller chooses: measured, 50,000 requests
    with a rotating header got 0 blocked, while 1,000 requests with no header at
    all got 880 blocked -- so it throttled honest anonymous traffic and waved
    the attack through.

    An address is not free to rotate, which is the property a pre-auth limit
    needs. Per-USER fairness is a different question with a different answer:
    the scan quota in deps.py runs AFTER the token is verified, which is the
    only place a user id can be trusted.

    `x-forwarded-for` is honoured only when the deployment says it is behind a
    proxy. Trusting it unconditionally would hand the rotating key straight
    back, in a different header.
    """
    if settings.trust_proxy_header:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    return (request.client.host if request.client else "unknown")[:64]
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
    key = _client_key(request)
    now = time.time()
    window = _hits.get(key)
    if window is None:
        window = _hits[key] = deque()
        # Make room before adding, so the cap is a cap rather than a target.
        while len(_hits) > RATE_LIMIT_BUCKETS:
            _hits.popitem(last=False)
    _hits.move_to_end(key)
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
    if not window:                      # nothing left in the minute: drop it
        _hits.pop(key, None)
    return await call_next(request)


install_error_handlers(app)

for r in ALL_ROUTERS:
    app.include_router(r, prefix=settings.api_prefix)


@app.get("/healthz", tags=["ops"])
async def healthz():
    # WHETHER THE MASK DUMP IS ARMED, ASKED FROM OUTSIDE THE PROCESS.
    #
    # `--reload` reloads CODE, not the ENVIRONMENT. A dev server started
    # before NUTRIAI_MASK_DUMP existed hot-loads the dumping code and then
    # writes nothing, with no error anywhere -- which is how a paid bench run
    # of photo 35 was spent capturing nothing at all. Nothing in the process
    # could have reported it, because the bench runs in a different one.
    #
    # Read here rather than cached at import, for the same reason `dump_masks`
    # reads it at call time: the answer must describe THIS request.
    import os

    from .services.ai.segment_hosted import MASK_DUMP_ENV

    return {"ok": True, "service": "neutriai-api", "version": app.version,
            "env": settings.env,
            "mask_dump": (os.environ.get(MASK_DUMP_ENV) or "").strip() or None}


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
