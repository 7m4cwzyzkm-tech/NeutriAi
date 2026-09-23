"""FastAPI dependencies: identity, Supabase handle, entitlement gating."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated

import structlog
from fastapi import Depends, Header, Request

from .config import settings
from .db import maybe_one, service
from .errors import PaymentRequired, QuotaExceeded, Unauthorized, UpstreamError

log = structlog.get_logger()
from .security import decode_supabase_jwt
from .services.identity import ensure_profile_cached


@dataclass(slots=True)
class CurrentUser:
    id: str
    email: str | None
    jwt: str
    claims: dict

    @property
    def sb(self):
        """RLS-scoped Supabase client for this caller."""
        from .db import as_user

        return as_user(self.jwt)


async def current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Missing bearer token.")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_supabase_jwt(token)
    uid = claims.get("sub")
    if not uid:
        raise Unauthorized("Token has no subject.")
    # Every user-scoped table has a FK to profiles(id). Guarantee the row
    # exists here so no route has to care whether GET /me ran first.
    # One query per user per process; see services.identity.
    ensure_profile_cached(uid, claims.get("email"))
    return CurrentUser(id=uid, email=claims.get("email"), jwt=token, claims=claims)


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]


async def get_entitlement(user: CurrentUserDep) -> dict:
    """Read (and lazily create) the caller's entitlement row."""
    sb = service()
    row = maybe_one(
        sb.table("entitlements").select("*").eq("user_id", user.id).limit(1).execute()
    )
    if row is None:
        row = (
            sb.table("entitlements")
            .insert(
                {
                    "user_id": user.id,
                    "tier": "free",
                    "is_active": False,
                    "ai_scans_quota": settings.free_tier_daily_scans,
                    "ai_reasoning_quota": settings.free_tier_daily_reasoning_calls,
                }
            )
            .execute()
            .data[0]
        )
    # Roll the daily AI quotas over at first touch of a new day. Scans and
    # reasoning calls share one quota_reset_on column deliberately -- both
    # reset on the same daily cadence, and a second reset-date column would
    # just be two dates that must always agree.
    if str(row.get("quota_reset_on")) != date.today().isoformat():
        row = (
            sb.table("entitlements")
            .update({
                "ai_scans_used_today": 0, "ai_reasoning_used_today": 0,
                "quota_reset_on": date.today().isoformat(),
            })
            .eq("user_id", user.id)
            .execute()
            .data[0]
        )
    return row


EntitlementDep = Annotated[dict, Depends(get_entitlement)]


async def require_pro(ent: EntitlementDep) -> dict:
    """Hard paywall. Trialing counts as Pro — that is the point of a trial."""
    # free_launch_mode: the product is free for everyone right now, with no
    # way to actually collect payment set up yet -- see config.py. This is
    # the only line that changes when it is off; ent.get("is_active") below,
    # and everywhere else in the codebase, is untouched, so billing status
    # displays and account logic keep reporting the real subscription state.
    if settings.free_launch_mode:
        return ent
    if not ent.get("is_active"):
        raise PaymentRequired(
            "This feature needs an active NeutriAI subscription.",
            detail={"tier": ent.get("tier"), "trial_available": ent.get("tier") == "free"},
        )
    return ent


ProDep = Annotated[dict, Depends(require_pro)]


async def consume_ai_scan(user: CurrentUserDep, ent: EntitlementDep) -> dict:
    """Metered gate for expensive vision calls.

    Free users get a small daily allowance so the app is usable before they
    pay; Pro users are unmetered but still counted for cost observability.

    free_launch_mode folds into `is_active` here, not just the RPC call --
    everyone gets the Pro-tier ceiling (settings.pro_daily_scan_ceiling)
    instead of the free tier's daily cap while it's on, and the "used your
    free scans, upgrade" message below only makes sense for someone who
    could actually upgrade right now. `ent` itself, returned unchanged, is
    still the real entitlement row -- this is what GATES a scan, not what
    the account's subscription state is reported as.
    """
    is_active = bool(ent.get("is_active")) or settings.free_launch_mode
    try:
        # Counted in the DATABASE, in one statement.
        #
        # This used to read the count, add one, and write it back. Two scans
        # arriving together both read the same number and both wrote one more
        # than it, so N concurrent requests cost ONE quota unit -- a free user
        # got unlimited scans by sending them at once, and every one of those
        # is a paid GPT-4o call. Verified against PostgreSQL 16: 40 concurrent
        # requests against a quota of 3 now allow exactly 3.
        result = service().rpc("consume_ai_scan", {
            "p_user_id": str(user.id),
            "p_is_active": is_active,
            "p_pro_ceiling": int(settings.pro_daily_scan_ceiling),
        }).execute()
        row = (getattr(result, "data", None) or [{}])[0]
    except Exception as exc:  # noqa: BLE001
        # A counter that cannot be reached must not become a free pass. Failing
        # closed on a paid call is the right direction: the user retries, and
        # nobody discovers that breaking the database is how you get free scans.
        log.warning("scan_quota_unavailable", error=str(exc)[:200])
        raise UpstreamError("Could not check your scan allowance. Try again.") from exc

    if not row.get("allowed", False):
        raise QuotaExceeded(
            "You have used today's AI scans."
            if is_active else "You have used today's free AI scans.",
            detail={"used": row.get("used"), "quota": row.get("quota"),
                    "upgrade": not is_active},
        )
    return ent


AiScanDep = Annotated[dict, Depends(consume_ai_scan)]


async def consume_ai_reasoning(user: CurrentUserDep, ent: EntitlementDep) -> dict:
    """Metered gate for expensive reasoning calls (recipe adaptation, plan
    generation) -- the equivalent of consume_ai_scan for a gap that had no
    counter at all.

    POST /recipes/{id}/adapt and POST /plans were gated by ProDep alone --
    require_pro only checks is_active (and free_launch_mode skips even that
    check for everyone right now), so nothing ever counted how many times a
    user called either route in a day. This mirrors consume_ai_scan exactly,
    down to the free_launch_mode handling: everyone gets the Pro-tier
    ceiling (settings.pro_daily_reasoning_ceiling) instead of the free
    tier's daily cap while it's on, for the same reason -- the "used your
    free calls, upgrade" message only makes sense for someone who could
    actually upgrade right now. `ent`, returned unchanged, is still the real
    entitlement row; this is what GATES a call, not what the account's
    subscription state is reported as.
    """
    is_active = bool(ent.get("is_active")) or settings.free_launch_mode
    try:
        # Same atomic, race-safe shape as consume_ai_scan's RPC call -- see
        # 0027_atomic_reasoning_quota.sql for why this has to be one
        # statement rather than read-modify-write.
        result = service().rpc("consume_ai_reasoning", {
            "p_user_id": str(user.id),
            "p_is_active": is_active,
            "p_pro_ceiling": int(settings.pro_daily_reasoning_ceiling),
        }).execute()
        row = (getattr(result, "data", None) or [{}])[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("reasoning_quota_unavailable", error=str(exc)[:200])
        raise UpstreamError("Could not check your call allowance. Try again.") from exc

    if not row.get("allowed", False):
        raise QuotaExceeded(
            "You have used today's AI calls."
            if is_active else "You have used today's free AI calls.",
            detail={"used": row.get("used"), "quota": row.get("quota"),
                    "upgrade": not is_active},
        )
    return ent


AiReasoningDep = Annotated[dict, Depends(consume_ai_reasoning)]


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")
