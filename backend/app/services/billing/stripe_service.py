"""Stripe subscriptions.

Design rules that keep billing from drifting out of sync with access:

1. **Stripe is the source of truth for money; ``entitlements`` is the source of
   truth for access.** The app never asks Stripe "is this user paid?" on the
   request path — it reads one row.
2. **Every state change flows through a webhook.** The checkout redirect is a UX
   nicety, not the thing that grants access; a user who closes the browser mid-
   redirect still gets their subscription.
3. **Webhooks are idempotent.** Stripe retries for three days. We record each
   event id and no-op on replays.
4. **Trials need no card by default**, so the 15 days are frictionless; the
   subscription simply moves to ``incomplete`` if no payment method is added.
"""
from __future__ import annotations

from datetime import datetime, timezone

import stripe
import structlog

from ...config import settings
from ...db import maybe_one, service
from ...errors import AppError, UpstreamError

log = structlog.get_logger()
stripe.api_key = settings.stripe_secret_key
stripe.api_version = "2024-11-20.acacia"

# Which Stripe statuses grant access. 'past_due' stays live deliberately: a
# failed renewal should not lock someone out mid-retry.
LIVE_STATUSES = {"trialing", "active", "past_due"}

TIER_BY_INTERVAL = {"month": "pro", "year": "pro_annual"}


def _ts(v) -> str | None:
    return datetime.fromtimestamp(v, tz=timezone.utc).isoformat() if v else None


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
def ensure_customer(user_id: str, email: str | None, name: str | None = None) -> str:
    sb = service()
    row = maybe_one(
        sb.table("billing_customers").select("*").eq("user_id", user_id).limit(1).execute()
    )
    if row:
        return row["stripe_customer_id"]
    try:
        customer = stripe.Customer.create(
            email=email, name=name, metadata={"user_id": user_id}
        )
    except stripe.StripeError as exc:
        raise UpstreamError(f"Stripe customer creation failed: {exc.user_message or exc}") from exc
    sb.table("billing_customers").insert({
        "user_id": user_id, "stripe_customer_id": customer.id
    }).execute()
    return customer.id


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------
def price_for(plan: str) -> str:
    price = settings.stripe_price_annual if plan == "annual" else settings.stripe_price_monthly
    if not price:
        raise AppError(f"No Stripe price configured for the {plan} plan.", code="plan_unavailable")
    return price


def has_used_trial(user_id: str) -> bool:
    """One trial per user, forever. Checked against our own history so a user
    cannot re-trial by cancelling and returning."""
    rows_ = (
        service().table("subscriptions").select("trial_start")
        .eq("user_id", user_id).not_.is_("trial_start", "null").limit(1).execute()
    )
    return bool(rows_.data)


def create_checkout(
    *, user_id: str, email: str | None, plan: str, promo_code: str | None,
    success_url: str | None, cancel_url: str | None,
) -> dict:
    customer_id = ensure_customer(user_id, email)
    trial_days = 0 if has_used_trial(user_id) else settings.stripe_trial_days

    sub_data: dict = {"metadata": {"user_id": user_id, "plan": plan}}
    if trial_days:
        sub_data["trial_period_days"] = trial_days
        # If they never add a card, cancel rather than silently locking them out
        # in an "incomplete" state they cannot see.
        sub_data["trial_settings"] = {"end_behavior": {"missing_payment_method": "cancel"}}

    params: dict = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [{"price": price_for(plan), "quantity": 1}],
        "subscription_data": sub_data,
        "success_url": success_url or settings.stripe_success_url,
        "cancel_url": cancel_url or settings.stripe_cancel_url,
        "client_reference_id": user_id,
        "allow_promotion_codes": True,
        "payment_method_collection": "if_required" if trial_days else "always",
        "metadata": {"user_id": user_id, "plan": plan},
    }

    if promo_code:
        try:
            found = stripe.PromotionCode.list(code=promo_code, active=True, limit=1)
            if found.data:
                params["discounts"] = [{"promotion_code": found.data[0].id}]
                params.pop("allow_promotion_codes", None)
            else:
                raise AppError("That promo code isn't valid.", code="invalid_promo")
        except stripe.StripeError as exc:
            raise UpstreamError(f"Promo code lookup failed: {exc}") from exc

    try:
        session = stripe.checkout.Session.create(**params)
    except stripe.StripeError as exc:
        raise UpstreamError(f"Stripe checkout failed: {exc.user_message or exc}") from exc

    return {"url": session.url, "session_id": session.id, "trial_days": trial_days}


def create_portal(user_id: str, return_url: str | None = None) -> str:
    row = maybe_one(
        service().table("billing_customers").select("stripe_customer_id")
        .eq("user_id", user_id).limit(1).execute()
    )
    if not row:
        raise AppError("No billing account yet — start a subscription first.",
                       code="no_billing_account")
    try:
        session = stripe.billing_portal.Session.create(
            customer=row["stripe_customer_id"],
            return_url=return_url or settings.stripe_portal_return_url,
        )
    except stripe.StripeError as exc:
        raise UpstreamError(f"Stripe portal failed: {exc.user_message or exc}") from exc
    return session.url


# ---------------------------------------------------------------------------
# Entitlement sync
# ---------------------------------------------------------------------------
def apply_subscription(sub: dict) -> None:
    """Write one Stripe subscription object into our tables and recompute access."""
    sb = service()
    user_id = (sub.get("metadata") or {}).get("user_id")
    if not user_id:
        # Fall back to the customer mapping, which always exists.
        row = maybe_one(
            sb.table("billing_customers").select("user_id")
            .eq("stripe_customer_id", sub.get("customer")).limit(1).execute()
        )
        user_id = row["user_id"] if row else None
    if not user_id:
        log.warning("stripe_sub_without_user", subscription=sub.get("id"))
        return

    item = (sub.get("items", {}).get("data") or [{}])[0]
    price = item.get("price") or {}
    interval = (price.get("recurring") or {}).get("interval", "month")
    status = sub.get("status", "incomplete")
    discount = sub.get("discount") or {}
    coupon = discount.get("coupon") or {}

    sb.table("subscriptions").upsert({
        "user_id": user_id,
        "stripe_subscription_id": sub["id"],
        "stripe_price_id": price.get("id", ""),
        "status": status,
        "plan_interval": interval,
        "amount_cents": int(price.get("unit_amount") or 0),
        "currency": price.get("currency", "usd"),
        "trial_start": _ts(sub.get("trial_start")),
        "trial_end": _ts(sub.get("trial_end")),
        "current_period_start": _ts(sub.get("current_period_start")),
        "current_period_end": _ts(sub.get("current_period_end")),
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
        "canceled_at": _ts(sub.get("canceled_at")),
        "promo_code": (discount.get("promotion_code") if isinstance(discount.get("promotion_code"), str) else None),
        "discount_pct": coupon.get("percent_off"),
        "metadata": sub.get("metadata") or {},
    }, on_conflict="stripe_subscription_id").execute()

    is_live = status in LIVE_STATUSES
    tier = "trial" if status == "trialing" else TIER_BY_INTERVAL.get(interval, "pro")
    sb.table("entitlements").upsert({
        "user_id": user_id,
        "tier": tier if is_live else "free",
        "is_active": is_live,
        "expires_at": _ts(sub.get("current_period_end")),
        "source": "stripe" if is_live else "none",
        # Pro is effectively unmetered; the number is a runaway-cost guard.
        "ai_scans_quota": 500 if is_live else settings.free_tier_daily_scans,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="user_id").execute()

    log.info("entitlement_synced", user_id=user_id, tier=tier, status=status, active=is_live)


def revoke(user_id: str) -> None:
    service().table("entitlements").upsert({
        "user_id": user_id, "tier": "free", "is_active": False,
        "expires_at": None, "source": "none",
        "ai_scans_quota": settings.free_tier_daily_scans,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="user_id").execute()


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
HANDLED = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.trial_will_end",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
}


def verify_webhook(payload: bytes, signature: str) -> dict:
    if not settings.stripe_webhook_secret:
        raise UpstreamError("Stripe webhook secret is not configured.")
    try:
        return stripe.Webhook.construct_event(
            payload, signature, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise AppError("Invalid Stripe signature.", code="bad_signature") from exc


def handle_event(event: dict) -> str:
    """Process one verified webhook. Returns a short status for logging."""
    sb = service()
    event_id, etype = event["id"], event["type"]

    # Idempotency: claim the event id first. A duplicate insert means a replay.
    try:
        sb.table("stripe_events").insert({
            "id": event_id, "type": etype, "payload": event, "status": "received"
        }).execute()
    except Exception:  # noqa: BLE001
        log.info("stripe_event_replay_ignored", event_id=event_id, type=etype)
        return "duplicate"

    if etype not in HANDLED:
        sb.table("stripe_events").update({"status": "ignored"}).eq("id", event_id).execute()
        return "ignored"

    obj = event["data"]["object"]
    try:
        if etype == "checkout.session.completed":
            if obj.get("subscription"):
                sub = stripe.Subscription.retrieve(
                    obj["subscription"], expand=["items.data.price", "discount"]
                )
                # Checkout carries the user id; the subscription may not yet.
                uid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("user_id")
                if uid and not (sub.metadata or {}).get("user_id"):
                    sub = stripe.Subscription.modify(sub.id, metadata={"user_id": uid})
                apply_subscription(dict(sub))

        elif etype.startswith("customer.subscription."):
            if etype.endswith("deleted"):
                uid = (obj.get("metadata") or {}).get("user_id")
                if uid:
                    revoke(uid)
                sb.table("subscriptions").update({"status": "canceled"}).eq(
                    "stripe_subscription_id", obj["id"]
                ).execute()
            elif etype.endswith("trial_will_end"):
                uid = (obj.get("metadata") or {}).get("user_id")
                if uid:
                    sb.table("notifications").insert({
                        "user_id": uid, "kind": "system",
                        "title": "Your trial ends in 3 days",
                        "body": "Add a payment method to keep unlimited AI scans and coaching.",
                        "deep_link": "nutriai://billing",
                    }).execute()
            else:
                apply_subscription(obj)

        elif etype == "invoice.payment_succeeded":
            if obj.get("subscription"):
                sub = stripe.Subscription.retrieve(
                    obj["subscription"], expand=["items.data.price", "discount"]
                )
                apply_subscription(dict(sub))

        elif etype == "invoice.payment_failed":
            uid = None
            row = maybe_one(
                sb.table("billing_customers").select("user_id")
                .eq("stripe_customer_id", obj.get("customer")).limit(1).execute()
            )
            uid = row["user_id"] if row else None
            if uid:
                sb.table("notifications").insert({
                    "user_id": uid, "kind": "system",
                    "title": "Payment didn't go through",
                    "body": "Update your card to keep your subscription active.",
                    "deep_link": "nutriai://billing",
                }).execute()

        sb.table("stripe_events").update({
            "status": "processed",
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", event_id).execute()
        return "processed"

    except Exception as exc:  # noqa: BLE001
        log.exception("stripe_event_failed", event_id=event_id, type=etype)
        sb.table("stripe_events").update({
            "status": "failed", "error": str(exc)[:500]
        }).eq("id", event_id).execute()
        # Return 200 anyway (the router does): a failed handler should be fixed
        # and replayed from the Stripe dashboard, not retried into a hot loop.
        return "failed"


def pricing_table() -> list[dict]:
    return [
        {
            "id": "monthly", "name": "NutriAI Pro", "interval": "month",
            "amount_cents": 699, "currency": "usd",
            "trial_days": settings.stripe_trial_days,
            "features": [
                "Unlimited AI food scans", "Pixel-to-gram portion accuracy",
                "Personalised macros and overeating alerts",
                "AI workout coach and equipment scanning",
                "AI recipe personalisation", "All wearable integrations",
            ],
        },
        {
            "id": "annual", "name": "NutriAI Pro (Annual)", "interval": "year",
            "amount_cents": 5000, "currency": "usd",
            "trial_days": settings.stripe_trial_days,
            "savings_note": "Save $33.88 — about 40% off monthly",
            "features": [
                "Everything in monthly", "Two months free versus paying monthly",
                "Priority AI processing", "Early access to new features",
            ],
        },
    ]
