"""Profile, onboarding, targets, body metrics."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from ..db import maybe_one, one, rows, service
from ..deps import CurrentUserDep
from ..errors import AppError, NotFound, Unauthorized
from ..models.common import Ok
from ..models.profile import (
    BodyMetricIn, ProfileIn, ProfileOut, RestrictionIn, TargetsOut,
)
from ..services import account, push
from ..services.nutrition.macros import compute_targets

router = APIRouter(prefix="/me", tags=["profile"])


def _recompute_targets(user_id: str, profile: dict) -> dict:
    sb = service()
    latest_bf = maybe_one(
        sb.table("body_metrics").select("body_fat_pct").eq("user_id", user_id)
        .not_.is_("body_fat_pct", "null").order("measured_at", desc=True).limit(1).execute()
    )
    targets = compute_targets(profile, (latest_bf or {}).get("body_fat_pct"))
    payload = {**targets, "user_id": user_id, "effective_from": date.today().isoformat()}
    sb.table("nutrition_targets").upsert(payload, on_conflict="user_id,effective_from").execute()
    # Keep hydration in step with the computed water target.
    sb.table("hydration_settings").upsert(
        {"user_id": user_id, "daily_goal_ml": targets["water_ml"]}, on_conflict="user_id"
    ).execute()
    return targets


@router.get("", response_model=ProfileOut)
async def get_profile(user: CurrentUserDep):
    row = maybe_one(user.sb.table("profiles").select("*").eq("id", user.id).limit(1).execute())
    if row:
        return ProfileOut(**row)

    # No profile yet. Two very different situations produce this, and they need
    # opposite responses:
    #
    #   a) first call after signup  -> create the profile
    #   b) the account was deleted  -> the token is still cryptographically
    #      valid (correct signature, not yet expired) but the auth user is
    #      gone. Creating a profile would violate profiles.id -> auth.users(id).
    #
    # Case (b) used to surface as a 500. A deleted user's app can hold a valid
    # JWT for up to an hour, so that meant every request failing opaquely
    # instead of the client simply signing out.
    handle = (user.email or f"user{user.id[:8]}").split("@")[0][:20]
    handle = "".join(c for c in handle if c.isalnum() or c in "_.") or f"u{user.id[:8]}"

    for candidate in (handle, f"{handle}{user.id[:4]}"):
        try:
            created = service().table("profiles").insert(
                {"id": user.id, "handle": candidate, "display_name": handle}
            ).execute()
            return ProfileOut(**created.data[0])
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            # 23503 = foreign key violation: no matching auth.users row.
            if "23503" in message or "foreign key" in message or "auth.users" in message:
                raise Unauthorized(
                    "This account no longer exists. Please sign in again."
                ) from exc
            # Anything else is a handle collision; fall through and retry once.
            continue

    raise AppError("Could not create your profile. Please try again.", code="profile_create_failed")


@router.patch("", response_model=ProfileOut)
async def update_profile(body: ProfileIn, user: CurrentUserDep):
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not patch:
        raise AppError("Nothing to update.")
    if body.birth_date:
        patch["birth_date"] = body.birth_date.isoformat()
    # First time the profile is complete enough to compute targets, mark onboarded.
    updated = one(
        user.sb.table("profiles").update(patch).eq("id", user.id).execute(), "profile"
    )
    if all(updated.get(k) for k in ("weight_kg", "height_cm", "sex", "birth_date")):
        if not updated.get("onboarded_at"):
            updated = one(
                service().table("profiles")
                .update({"onboarded_at": "now()"}).eq("id", user.id).execute(),
                "profile",
            )
        _recompute_targets(user.id, updated)
    return ProfileOut(**updated)


@router.get("/targets", response_model=TargetsOut)
async def get_targets(user: CurrentUserDep, recompute: bool = Query(False)):
    sb = user.sb
    if recompute:
        profile = one(sb.table("profiles").select("*").eq("id", user.id).limit(1).execute())
        return TargetsOut(**_recompute_targets(user.id, profile))
    row = maybe_one(
        sb.table("nutrition_targets").select("*").eq("user_id", user.id)
        .order("effective_from", desc=True).limit(1).execute()
    )
    if not row:
        profile = maybe_one(sb.table("profiles").select("*").eq("id", user.id).limit(1).execute())
        if not profile or not profile.get("weight_kg"):
            raise NotFound("Finish onboarding before targets can be calculated.")
        return TargetsOut(**_recompute_targets(user.id, profile))
    return TargetsOut(**row)


@router.get("/dashboard")
async def dashboard(user: CurrentUserDep, day: date | None = None):
    """One round trip for the entire home screen."""
    return user.sb.rpc(
        "dashboard", {"p_day": (day or date.today()).isoformat()}
    ).execute().data


@router.get("/restrictions")
async def list_restrictions(user: CurrentUserDep):
    return rows(user.sb.table("dietary_restrictions").select("*").eq("user_id", user.id).execute())


@router.post("/restrictions", status_code=201)
async def add_restriction(body: RestrictionIn, user: CurrentUserDep):
    return one(
        user.sb.table("dietary_restrictions")
        .upsert({**body.model_dump(), "user_id": user.id}, on_conflict="user_id,kind,label")
        .execute()
    )


@router.delete("/restrictions/{restriction_id}", response_model=Ok)
async def remove_restriction(restriction_id: str, user: CurrentUserDep):
    user.sb.table("dietary_restrictions").delete().eq("id", restriction_id).eq(
        "user_id", user.id
    ).execute()
    return Ok(message="Removed.")


@router.post("/body-metrics", status_code=201)
async def log_body_metric(body: BodyMetricIn, user: CurrentUserDep):
    row = one(
        user.sb.table("body_metrics")
        .insert({**body.model_dump(exclude_none=True), "user_id": user.id})
        .execute()
    )
    # A new weight changes every calorie target, so recompute immediately.
    if body.weight_kg:
        profile = one(
            service().table("profiles").update({"weight_kg": body.weight_kg})
            .eq("id", user.id).execute()
        )
        _recompute_targets(user.id, profile)
    return row


@router.get("/body-metrics")
async def list_body_metrics(user: CurrentUserDep, limit: int = Query(60, le=365)):
    return rows(
        user.sb.table("body_metrics").select("*").eq("user_id", user.id)
        .order("measured_at", desc=True).limit(limit).execute()
    )


@router.get("/streaks")
async def get_streaks(user: CurrentUserDep):
    return {
        r["kind"]: {"current": r["current_len"], "best": r["best_len"], "last_day": r["last_day"]}
        for r in rows(user.sb.table("streaks").select("*").eq("user_id", user.id).execute())
    }


@router.get("/history")
async def history(user: CurrentUserDep, days: int = Query(30, ge=1, le=365)):
    return rows(
        user.sb.table("daily_summaries").select("*").eq("user_id", user.id)
        .order("day", desc=True).limit(days).execute()
    )


# ===========================================================================
# Push token registration
# ===========================================================================
@router.post("/push-token", response_model=Ok)
async def register_push_token(user: CurrentUserDep, token: str):
    """Store the device's Expo push token.

    Called on every app launch, not just the first: tokens rotate on reinstall
    and after some OS updates, and a stale token silently stops all delivery.
    """
    ok = await push.register_token(user.id, token)
    if not ok:
        raise AppError("That does not look like an Expo push token.", code="bad_push_token")
    return Ok(message="Push notifications enabled.")


@router.delete("/push-token", response_model=Ok)
async def clear_push_token(user: CurrentUserDep):
    service().table("profiles").update({"push_token": None}).eq("id", user.id).execute()
    return Ok(message="Push notifications disabled on this device.")


# ===========================================================================
# Account deletion
#
# Required by Apple guideline 5.1.1(v) and Google's data deletion policy. An
# app with accounts and no in-app deletion path is rejected.
# ===========================================================================
@router.get("/deletion-preview")
async def deletion_preview(user: CurrentUserDep):
    """What deletion will remove, so the confirmation screen can be specific
    rather than a generic 'are you sure?'."""
    return account.summarize(user.id)


@router.delete("/account", response_model=Ok)
async def delete_account(user: CurrentUserDep, confirm: str = Query(...)):
    """Permanently delete this account and all its data.

    ``confirm`` must be the literal string DELETE. A destructive, irreversible
    action reached by a single tap is a support ticket waiting to happen.
    """
    if confirm != "DELETE":
        raise AppError(
            "Deletion must be confirmed by passing confirm=DELETE.",
            code="confirmation_required",
        )
    report = account.delete_account(user.id)
    if not report.get("auth_user_deleted"):
        # Say what actually failed. A generic "contact support" on a
        # store-required path hides the one detail needed to fix it.
        raise AppError(
            "We could not delete your account. Your subscription was not left "
            f"charging. Reason: {report.get('error', 'unknown')}",
            code="deletion_failed",
            detail={
                "reason": report.get("error"),
                "billing": report.get("billing"),
                "storage_objects_removed": (report.get("storage") or {}).get("objects_removed"),
            },
        )
    notes = report["billing"].get("manual_action_required") or []
    return Ok(message=" ".join(["Your account and all its data have been deleted."] + notes))
