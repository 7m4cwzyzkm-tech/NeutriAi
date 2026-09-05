"""Account deletion.

Both stores require in-app account deletion for any app with accounts (Apple
guideline 5.1.1(v); Google's Data deletion policy). "Contact support to delete"
is a rejection.

Deletion here is real, not a soft flag. The order matters and is not arbitrary:

1. **Cancel billing first.** If we delete the account and the subscription
   survives, the user keeps being charged for an app they can no longer open.
   That is the one failure here that costs them money, so it goes first and its
   outcome is reported even when later steps fail.
2. **Delete storage objects.** Rows cascade; S3-style object storage does not.
   Meal photos would otherwise outlive the account indefinitely.
3. **Delete the auth user.** Every table keys off ``profiles.id`` which
   references ``auth.users(id) ON DELETE CASCADE``, so this one statement
   removes all 39 tables' worth of the user's data.

Anything that fails is reported rather than swallowed, because a partial
deletion that claims success is worse than an honest error.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog

from ..config import settings
from ..db import maybe_one, rows, service

log = structlog.get_logger()

USER_BUCKETS = ["meal-photos", "equipment-photos", "recipe-photos", "avatars", "post-media"]


def _cancel_billing(user_id: str) -> dict:
    """Cancel any live subscription so deletion never leaves someone paying."""
    sb = service()
    result: dict = {"cancelled": [], "errors": [], "manual_action_required": []}

    subs = rows(
        sb.table("subscriptions").select("stripe_subscription_id,status,metadata")
        .eq("user_id", user_id).execute()
    )
    for sub in subs:
        sid = sub.get("stripe_subscription_id") or ""
        if sub.get("status") in ("canceled", "incomplete_expired"):
            continue

        # IAP subscriptions cannot be cancelled server-side — only the user can,
        # through Apple or Google. Say so plainly instead of pretending.
        if sid.startswith(("apple_iap:", "google_iap:")):
            store = "the App Store" if sid.startswith("apple") else "Google Play"
            result["manual_action_required"].append(
                f"Your subscription was purchased through {store} and must be "
                f"cancelled there — deleting your account does not stop it."
            )
            continue

        try:
            import stripe

            from ..config import settings

            stripe.api_key = settings.stripe_secret_key
            stripe.Subscription.delete(sid)
            result["cancelled"].append(sid)
            log.info("subscription_cancelled_on_delete", user_id=user_id, sub=sid)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"Could not cancel {sid}: {str(exc)[:160]}")
            log.warning("cancel_failed_on_delete", user_id=user_id, error=str(exc)[:200])
    return result


def _purge_storage(user_id: str) -> dict:
    """Remove every object under the user's folder in each bucket."""
    sb = service()
    removed, errors = 0, []
    for bucket in USER_BUCKETS:
        try:
            objects = sb.storage.from_(bucket).list(user_id)
            paths = [f"{user_id}/{o['name']}" for o in (objects or []) if o.get("name")]
            if paths:
                sb.storage.from_(bucket).remove(paths)
                removed += len(paths)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{bucket}: {str(exc)[:120]}")
            log.warning("storage_purge_failed", bucket=bucket, error=str(exc)[:160])
    return {"objects_removed": removed, "errors": errors}


def _delete_auth_user(user_id: str) -> tuple[bool, str]:
    """Remove the auth user, which cascades every table keyed off profiles.id.

    Called over plain HTTP rather than through supabase-py's admin client
    because the two Supabase key generations authenticate differently, and the
    SDK does not always present the newer one the way GoTrue expects:

      legacy  service_role JWT  -> apikey AND Authorization: Bearer
      current sb_secret_...     -> apikey; the same value in Authorization is
                                   parsed as a JWT and rejected

    So we try the header shape that matches the key we actually hold, then fall
    back to the other, then to the SDK. Whichever works, works -- and if none
    do, the caller gets the real reason instead of a generic failure.
    """
    import httpx

    base = settings.supabase_url.rstrip("/")
    key = settings.supabase_service_key
    url = f"{base}/auth/v1/admin/users/{user_id}"
    new_format = key.startswith("sb_")

    # apikey-only first for new keys, both-headers first for legacy ones.
    attempts = (
        [{"apikey": key}, {"apikey": key, "Authorization": f"Bearer {key}"}]
        if new_format
        else [{"apikey": key, "Authorization": f"Bearer {key}"}, {"apikey": key}]
    )

    errors: list[str] = []
    for headers in attempts:
        try:
            r = httpx.request("DELETE", url, headers=headers, timeout=20.0)
            if r.status_code in (200, 204):
                return True, ""
            errors.append(f"HTTP {r.status_code}: {r.text[:160]}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")

    # Last resort: whatever the SDK does.
    try:
        service().auth.admin.delete_user(user_id)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        errors.append(f"sdk: {type(exc).__name__}: {str(exc)[:160]}")

    return False, " | ".join(errors)


def summarize(user_id: str) -> dict:
    """What deletion will remove. Shown on the confirmation screen so the user
    is making an informed choice rather than tapping through a scary dialog."""
    sb = service()

    def count(table: str, column: str = "user_id") -> int:
        try:
            res = sb.table(table).select("id", count="exact").eq(column, user_id).execute()
            return res.count if res.count is not None else len(res.data or [])
        except Exception:  # noqa: BLE001
            return 0

    sub = maybe_one(
        sb.table("subscriptions").select("status,stripe_subscription_id,current_period_end")
        .eq("user_id", user_id).order("created_at", desc=True).limit(1).execute()
    )
    sid = (sub or {}).get("stripe_subscription_id") or ""
    store_managed = sid.startswith(("apple_iap:", "google_iap:"))

    return {
        "meals": count("meals"),
        "workouts": count("workouts"),
        "recipes": count("recipes", "author_id"),
        "posts": count("posts", "author_id"),
        "photos": count("food_scans") + count("equipment_scans"),
        "subscription_active": bool(sub and sub.get("status") in ("trialing", "active", "past_due")),
        "subscription_store_managed": store_managed,
        "subscription_note": (
            "Your subscription was bought through the app store and must be cancelled there."
            if store_managed else
            "Your subscription will be cancelled automatically."
        ) if sub and sub.get("status") in ("trialing", "active", "past_due") else None,
    }


def delete_account(user_id: str) -> dict:
    """Permanently delete a user. Not reversible, not a soft delete."""
    sb = service()
    started = datetime.now(timezone.utc)
    report: dict = {"user_id": user_id, "started_at": started.isoformat()}

    report["billing"] = _cancel_billing(user_id)
    report["storage"] = _purge_storage(user_id)

    deleted, why = _delete_auth_user(user_id)
    report["auth_user_deleted"] = deleted
    if not deleted:
        report["error"] = why
        log.error("account_deletion_failed", user_id=user_id, reason=why)
        return report

    # Verify rather than assume the cascade fired.
    leftover = maybe_one(
        sb.table("profiles").select("id").eq("id", user_id).limit(1).execute()
    )
    report["profile_row_remaining"] = bool(leftover)
    if leftover:
        # Belt and braces: if the FK cascade is missing for any reason, remove
        # the profile explicitly so the rest of the tables cascade from it.
        sb.table("profiles").delete().eq("id", user_id).execute()
        report["profile_deleted_explicitly"] = True

    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    log.info("account_deleted", user_id=user_id,
             objects=report["storage"]["objects_removed"])
    return report
