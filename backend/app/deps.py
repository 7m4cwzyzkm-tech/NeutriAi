"""FastAPI dependencies: identity, Supabase handle, entitlement gating."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated

from fastapi import Depends, Header, Request

from .config import settings
from .db import maybe_one, service
from .errors import PaymentRequired, QuotaExceeded, Unauthorized
from .security import decode_supabase_jwt


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
                }
            )
            .execute()
            .data[0]
        )
    # Roll the daily AI quota over at first touch of a new day.
    if str(row.get("quota_reset_on")) != date.today().isoformat():
        row = (
            sb.table("entitlements")
            .update({"ai_scans_used_today": 0, "quota_reset_on": date.today().isoformat()})
            .eq("user_id", user.id)
            .execute()
            .data[0]
        )
    return row


EntitlementDep = Annotated[dict, Depends(get_entitlement)]


async def require_pro(ent: EntitlementDep) -> dict:
    """Hard paywall. Trialing counts as Pro — that is the point of a trial."""
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
    """
    sb = service()
    used = int(ent.get("ai_scans_used_today") or 0)
    if not ent.get("is_active") and used >= int(ent.get("ai_scans_quota") or 0):
        raise QuotaExceeded(
            "You have used today's free AI scans.",
            detail={"used": used, "quota": ent.get("ai_scans_quota"), "upgrade": True},
        )
    sb.table("entitlements").update({"ai_scans_used_today": used + 1}).eq(
        "user_id", user.id
    ).execute()
    return ent


AiScanDep = Annotated[dict, Depends(consume_ai_scan)]


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")
