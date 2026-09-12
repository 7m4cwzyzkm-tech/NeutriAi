"""Profile, onboarding, targets, body metrics."""
from __future__ import annotations

from datetime import date

import structlog
from fastapi import APIRouter, Query

from ..db import maybe_one, one, rows, service
from ..deps import CurrentUserDep
from ..errors import AppError, NotFound
from ..models.common import Ok
from ..models.profile import (
    BodyMetricIn, ProfileIn, ProfileOut, RestrictionIn, TargetsOut,
)
from ..services import account, identity, push
from ..services.nutrition.macros import compute_targets

log = structlog.get_logger()

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
    """The profile is created by the auth dependency on first authenticated
    request, so by the time this runs it exists. ensure_profile is still the
    call made here rather than a bare select: it keeps this endpoint correct
    on its own terms, and it is the one place that returns the deleted-account
    401 if the row is somehow absent."""
    return ProfileOut(**identity.ensure_profile(user.id, user.email))


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
    data = user.sb.rpc(
        "dashboard", {"p_day": (day or date.today()).isoformat()}
    ).execute().data

    # The active fast, finished off.
    #
    # The rollup returns `to_jsonb(f)` -- the raw `fasts` row, which has a start
    # time and a target and nothing else. `pct`, `elapsed_minutes` and `phase`
    # are computed, in `lifestyle._to_out`, and the rollup does not go through
    # it. So the home screen's ring rendered strokeDasharray="NaN" and read
    # "NaNh / NaNm" while the Fasting tab showed the same fast correctly.
    #
    # Finished HERE rather than in the SQL on purpose. Computing it a second
    # time in plpgsql would be two implementations of one rule -- fasting phase
    # boundaries, of all things -- and they would drift the first time either
    # was touched.
    return finish_fast(data)


def finish_fast(data):
    """Fill in the computed half of the active fast.

    A named function rather than four lines inside the route, because a test
    that can only read the route's SOURCE is not a test: the first version of
    this checked that the string "_to_out" appeared somewhere in the function,
    and deleting the line that used it left the import behind, so the check
    passed on a mutation that broke the feature.
    """
    if not isinstance(data, dict) or not isinstance(data.get("active_fast"), dict):
        return data
    try:
        from .lifestyle import _to_out
        data["active_fast"] = _to_out(data["active_fast"]).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001
        # A home screen missing one card beats a home screen that 500s.
        log.warning("dashboard_fast_enrich_failed", error=str(exc)[:200])
        data["active_fast"] = None
    return data


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
