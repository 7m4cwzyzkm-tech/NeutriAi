"""Stripe checkout, portal, subscription status and webhooks."""
from __future__ import annotations

from fastapi import APIRouter, Header, Request, Response, status

from ..db import maybe_one
from ..deps import CurrentUserDep, EntitlementDep
from ..models.billing import (
    CheckoutRequest, CheckoutSession, PortalSession, PricingPlan, SubscriptionOut,
)
from ..services.billing import iap
from ..services.billing import stripe_service as svc

router = APIRouter(tags=["billing"])


@router.get("/billing/plans", response_model=list[PricingPlan])
async def plans():
    return [PricingPlan(**p) for p in svc.pricing_table()]


@router.get("/billing/subscription", response_model=SubscriptionOut)
async def subscription(user: CurrentUserDep, ent: EntitlementDep):
    sub = maybe_one(
        user.sb.table("subscriptions").select("*").eq("user_id", user.id)
        .order("created_at", desc=True).limit(1).execute()
    )
    return SubscriptionOut(
        tier=ent.get("tier", "free"),
        is_active=bool(ent.get("is_active")),
        status=(sub or {}).get("status"),
        plan_interval=(sub or {}).get("plan_interval"),
        amount_cents=(sub or {}).get("amount_cents"),
        currency=(sub or {}).get("currency", "usd"),
        trial_end=(sub or {}).get("trial_end"),
        current_period_end=(sub or {}).get("current_period_end"),
        cancel_at_period_end=bool((sub or {}).get("cancel_at_period_end")),
        promo_code=(sub or {}).get("promo_code"),
        ai_scans_used_today=int(ent.get("ai_scans_used_today") or 0),
        ai_scans_quota=int(ent.get("ai_scans_quota") or 0),
    )


@router.post("/billing/checkout", response_model=CheckoutSession, status_code=201)
async def checkout(body: CheckoutRequest, user: CurrentUserDep):
    result = svc.create_checkout(
        user_id=user.id, email=user.email, plan=body.plan,
        promo_code=body.promo_code, success_url=body.success_url,
        cancel_url=body.cancel_url,
    )
    return CheckoutSession(**result)


@router.post("/billing/portal", response_model=PortalSession)
async def portal(user: CurrentUserDep, return_url: str | None = None):
    return PortalSession(url=svc.create_portal(user.id, return_url))


@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request, stripe_signature: str = Header(default="")):
    """Stripe's endpoint. Always answers 200 once the signature verifies —
    a handler bug should be replayed from the dashboard, not retried into a
    hot loop for three days."""
    payload = await request.body()
    event = svc.verify_webhook(payload, stripe_signature)
    outcome = svc.handle_event(event)
    return Response(
        content=f'{{"received":true,"outcome":"{outcome}"}}',
        media_type="application/json",
        status_code=status.HTTP_200_OK,
    )


# ===========================================================================
# In-app purchases
#
# Apple and Google require their own billing for digital subscriptions bought
# inside the native app; Stripe Checkout there is a guaranteed rejection. Both
# paths below terminate in iap.apply_entitlement(), which writes the same
# entitlements row that the Stripe webhook writes — so access has exactly one
# definition no matter who took the money.
# ===========================================================================
@router.post("/billing/iap/apple", response_model=dict)
async def verify_apple_purchase(
    user: CurrentUserDep, transaction_id: str, sandbox: bool = False
):
    """Called by the app right after a StoreKit 2 purchase completes.

    Send `transaction.id` from StoreKit 2, not the legacy base64 receipt. We
    re-fetch the authoritative record from Apple rather than trusting the
    client, so a tampered payload buys nothing.
    """
    return await iap.verify_apple(user.id, transaction_id, sandbox=sandbox)


@router.post("/billing/iap/google", response_model=dict)
async def verify_google_purchase(
    user: CurrentUserDep, product_id: str, purchase_token: str
):
    """Called by the app after a Play Billing purchase completes.

    Also acknowledges the purchase — Play auto-refunds anything unacknowledged
    after three days.
    """
    return await iap.verify_google(user.id, product_id, purchase_token)


@router.post("/webhooks/apple", include_in_schema=False)
async def apple_notifications(request: Request):
    """App Store Server Notifications V2. Register in App Store Connect.

    This, not the client receipt call, is the source of truth for renewals,
    cancellations, refunds and grace periods — exactly as Stripe webhooks are.
    """
    body = await request.json()
    outcome = await iap.handle_apple_notification(body.get("signedPayload", ""))
    return {"received": True, "outcome": outcome}


@router.post("/webhooks/google", include_in_schema=False)
async def google_notifications(request: Request):
    """Play Real-Time Developer Notifications, delivered via Pub/Sub push."""
    outcome = await iap.handle_google_notification(await request.json())
    return {"received": True, "outcome": outcome}
