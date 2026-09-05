"""In-app purchases — Apple StoreKit 2 and Google Play Billing.

Why this exists: Apple and Google require their own billing for digital
subscriptions sold inside a native app. Stripe Checkout for the same product is
a guaranteed rejection. Stripe stays for the web; this handles the phone.

The design point that keeps this from becoming a second billing system: both
providers terminate in ``apply_entitlement()``, which writes the same
``entitlements`` row that ``stripe_service.apply_subscription()`` writes. Access
is decided in exactly one place, by exactly one row, regardless of who took the
money. ``entitlements.source`` records which.

Two verification paths, mirroring Stripe's two:

* **Client-initiated** — the app sends a receipt after purchase. Fast, but a
  user who force-quits mid-purchase never sends it.
* **Server notifications** — Apple's App Store Server Notifications V2 and
  Google's Pub/Sub Real-Time Developer Notifications. These are the source of
  truth, exactly as Stripe webhooks are. Renewals, cancellations, refunds,
  billing retries and grace periods arrive here and nowhere else.

Ship both. The receipt path makes the purchase feel instant; the notification
path makes it correct.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from ...config import settings
from ...db import maybe_one, service
from ...errors import AppError, UpstreamError

log = structlog.get_logger()

# Product ids must match App Store Connect and Play Console exactly.
PRODUCT_TIERS: dict[str, tuple[str, str]] = {
    "app.neutriai.pro.monthly": ("pro", "month"),
    "app.neutriai.pro.annual": ("pro_annual", "year"),
}

APPLE_PROD = "https://api.storekit.itunes.apple.com/inApps/v1"
APPLE_SANDBOX = "https://api.storekit-sandbox.itunes.apple.com/inApps/v1"
GOOGLE_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"

# Apple notification types that mean "this subscription is live".
APPLE_LIVE = {"SUBSCRIBED", "DID_RENEW", "OFFER_REDEEMED", "DID_CHANGE_RENEWAL_PREF"}
APPLE_DEAD = {"EXPIRED", "REFUND", "REVOKE", "GRACE_PERIOD_EXPIRED"}
# Google notification codes: 1 recovered, 2 renewed, 4 purchased, 7 restarted
GOOGLE_LIVE = {1, 2, 4, 7}
GOOGLE_DEAD = {3, 12, 13}          # canceled, revoked, expired


# ===========================================================================
# The single shared exit point
# ===========================================================================
def apply_entitlement(
    *,
    user_id: str,
    product_id: str,
    is_active: bool,
    expires_at: datetime | None,
    source: str,                    # 'apple_iap' | 'google_iap'
    original_transaction_id: str | None = None,
    is_trial: bool = False,
) -> None:
    """Write the entitlement. Deliberately identical in shape and effect to
    ``stripe_service.apply_subscription`` so access has one definition."""
    tier, _interval = PRODUCT_TIERS.get(product_id, ("pro", "month"))
    if is_trial and is_active:
        tier = "trial"

    service().table("entitlements").upsert(
        {
            "user_id": user_id,
            "tier": tier if is_active else "free",
            "is_active": is_active,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "source": source if is_active else "none",
            "ai_scans_quota": 500 if is_active else settings.free_tier_daily_scans,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="user_id",
    ).execute()

    # Mirror into `subscriptions` so one query answers "what is this user on?"
    # across all three payment rails.
    if original_transaction_id:
        service().table("subscriptions").upsert(
            {
                "user_id": user_id,
                "stripe_subscription_id": f"{source}:{original_transaction_id}",
                "stripe_price_id": product_id,
                "status": "active" if is_active else "canceled",
                "plan_interval": PRODUCT_TIERS.get(product_id, ("pro", "month"))[1],
                "amount_cents": 699 if "monthly" in product_id else 5000,
                "currency": "usd",
                "current_period_end": expires_at.isoformat() if expires_at else None,
                "metadata": {"source": source, "product_id": product_id},
            },
            on_conflict="stripe_subscription_id",
        ).execute()

    log.info("iap_entitlement_applied", user_id=user_id, source=source,
             product=product_id, tier=tier, active=is_active)


def _ms_to_dt(ms: Any) -> datetime | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Apple — StoreKit 2 / App Store Server API
# ===========================================================================
def _decode_jws(token: str) -> dict:
    """Read a JWS payload.

    NOTE: this decodes without verifying. Apple signs these with a certificate
    chain rooted at the Apple Root CA; a production deployment MUST verify that
    chain (the `app-store-server-library` package does it for you) before
    trusting the contents. Decoding alone is fine for the *client receipt* path
    because we immediately re-fetch the authoritative record from Apple's API,
    but a server notification acted on without verification is forgeable.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise AppError("Malformed Apple JWS.", code="bad_receipt")
    payload = parts[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _apple_jwt() -> str:
    """Sign the ES256 token the App Store Server API requires.

    Needs, from App Store Connect → Users and Access → Integrations → In-App
    Purchase: the key id, the issuer id, and the .p8 private key.
    """
    from jose import jwt

    key_id = getattr(settings, "apple_key_id", "")
    issuer = getattr(settings, "apple_issuer_id", "")
    private_key = getattr(settings, "apple_private_key", "").replace("\\n", "\n")
    bundle = getattr(settings, "apple_bundle_id", "app.neutriai.mobile")
    if not (key_id and issuer and private_key):
        raise UpstreamError("Apple IAP credentials are not configured.")

    now = int(time.time())
    return jwt.encode(
        {"iss": issuer, "iat": now, "exp": now + 3000,
         "aud": "appstoreconnect-v1", "bid": bundle},
        private_key,
        algorithm="ES256",
        headers={"kid": key_id, "typ": "JWT"},
    )


async def verify_apple(user_id: str, transaction_id: str, sandbox: bool = False) -> dict:
    """Verify a StoreKit 2 transaction and apply the entitlement.

    The app sends ``transaction.id`` from StoreKit 2 — not the old base64
    receipt blob, which is deprecated. We then ask Apple for the authoritative
    subscription status rather than trusting anything the client said.
    """
    base = APPLE_SANDBOX if sandbox else APPLE_PROD
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{base}/subscriptions/{transaction_id}",
                headers={"Authorization": f"Bearer {_apple_jwt()}"},
            )
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Could not reach Apple: {exc}") from exc

    # A production transaction queried against production returns 404 in
    # sandbox and vice versa. Retry the other environment once, which is what
    # Apple's own guidance says to do.
    if r.status_code == 404 and not sandbox:
        return await verify_apple(user_id, transaction_id, sandbox=True)
    if r.status_code >= 400:
        raise UpstreamError(f"Apple rejected the transaction: {r.text[:200]}")

    body = r.json()
    groups = body.get("data") or []
    if not groups:
        raise AppError("No subscription found for that transaction.", code="no_subscription")

    latest = (groups[0].get("lastTransactions") or [{}])[0]
    signed = latest.get("signedTransactionInfo")
    info = _decode_jws(signed) if signed else {}
    renewal = _decode_jws(latest["signedRenewalInfo"]) if latest.get("signedRenewalInfo") else {}

    # status: 1 active, 2 expired, 3 billing retry, 4 grace, 5 revoked
    status = int(latest.get("status") or 0)
    expires = _ms_to_dt(info.get("expiresDate"))
    # Billing retry and grace both keep access, matching how Stripe's
    # `past_due` is treated. A failed renewal is not a cancellation.
    is_active = status in (1, 3, 4)
    product_id = info.get("productId", "")

    apply_entitlement(
        user_id=user_id,
        product_id=product_id,
        is_active=is_active,
        expires_at=expires,
        source="apple_iap",
        original_transaction_id=info.get("originalTransactionId"),
        is_trial=info.get("offerType") == 1,
    )
    return {"provider": "apple", "status": status, "is_active": is_active,
            "product_id": product_id, "expires_at": expires.isoformat() if expires else None,
            "environment": "sandbox" if sandbox else "production"}


async def handle_apple_notification(signed_payload: str) -> str:
    """App Store Server Notifications V2.

    Register the URL in App Store Connect → App Information → App Store Server
    Notifications (set both the production and sandbox URLs).
    """
    payload = _decode_jws(signed_payload)
    notification_type = payload.get("notificationType", "")
    subtype = payload.get("subtype", "")
    data = payload.get("data") or {}
    info = _decode_jws(data["signedTransactionInfo"]) if data.get("signedTransactionInfo") else {}

    original_tx = info.get("originalTransactionId")
    if not original_tx:
        return "ignored:no_transaction"

    # Idempotency, exactly as with Stripe: claim the notification id first.
    notification_id = payload.get("notificationUUID") or f"apple:{original_tx}:{time.time()}"
    try:
        service().table("stripe_events").insert({
            "id": f"apple:{notification_id}",
            "type": f"apple.{notification_type}.{subtype}".rstrip("."),
            "payload": {"notificationType": notification_type, "subtype": subtype,
                        "originalTransactionId": original_tx},
            "status": "received",
        }).execute()
    except Exception:
        return "duplicate"

    sub = maybe_one(
        service().table("subscriptions").select("user_id")
        .eq("stripe_subscription_id", f"apple_iap:{original_tx}").limit(1).execute()
    )
    if not sub:
        log.warning("apple_notification_unknown_user", original_tx=original_tx)
        return "ignored:unknown_user"

    is_active = notification_type in APPLE_LIVE or (
        notification_type == "DID_FAIL_TO_RENEW" and subtype == "GRACE_PERIOD"
    )
    if notification_type in APPLE_DEAD:
        is_active = False

    apply_entitlement(
        user_id=sub["user_id"],
        product_id=info.get("productId", ""),
        is_active=is_active,
        expires_at=_ms_to_dt(info.get("expiresDate")),
        source="apple_iap",
        original_transaction_id=original_tx,
    )
    service().table("stripe_events").update({
        "status": "processed", "processed_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", f"apple:{notification_id}").execute()
    return f"processed:{notification_type}"


# ===========================================================================
# Google Play Billing
# ===========================================================================
async def _google_token() -> str:
    """Service-account access token for the Android Publisher API."""
    sa_json = getattr(settings, "google_play_service_account", "")
    if not sa_json:
        raise UpstreamError("Google Play service account is not configured.")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa_json),
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        creds.refresh(Request())
        return creds.token
    except ImportError as exc:
        raise UpstreamError(
            "google-auth is required for Play Billing: pip install google-auth"
        ) from exc


async def verify_google(user_id: str, product_id: str, purchase_token: str) -> dict:
    """Verify a Play purchase token and apply the entitlement."""
    package = getattr(settings, "android_package_name", "app.neutriai.mobile")
    token = await _google_token()
    url = (f"{GOOGLE_API}/applications/{package}/purchases/subscriptionsv2/"
           f"tokens/{purchase_token}")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Could not reach Google Play: {exc}") from exc
    if r.status_code >= 400:
        raise UpstreamError(f"Google rejected the purchase token: {r.text[:200]}")

    body = r.json()
    state = body.get("subscriptionState", "")
    line = (body.get("lineItems") or [{}])[0]
    expiry = line.get("expiryTime")
    expires = None
    if expiry:
        expires = datetime.fromisoformat(expiry.replace("Z", "+00:00"))

    # ON_HOLD and IN_GRACE_PERIOD keep access — same reasoning as Stripe's
    # past_due and Apple's billing-retry state.
    is_active = state in (
        "SUBSCRIPTION_STATE_ACTIVE",
        "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
        "SUBSCRIPTION_STATE_ON_HOLD",
    )
    apply_entitlement(
        user_id=user_id,
        product_id=line.get("productId") or product_id,
        is_active=is_active,
        expires_at=expires,
        source="google_iap",
        original_transaction_id=body.get("latestOrderId") or purchase_token[:64],
        is_trial=bool(line.get("offerDetails", {}).get("offerId")),
    )

    # Play requires acknowledgement within three days or it auto-refunds.
    if body.get("acknowledgementState") == "ACKNOWLEDGEMENT_STATE_PENDING":
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(
                f"{GOOGLE_API}/applications/{package}/purchases/subscriptions/"
                f"{product_id}/tokens/{purchase_token}:acknowledge",
                headers={"Authorization": f"Bearer {token}"},
                json={},
            )
        log.info("google_purchase_acknowledged", user_id=user_id)

    return {"provider": "google", "state": state, "is_active": is_active,
            "product_id": line.get("productId"),
            "expires_at": expires.isoformat() if expires else None}


async def handle_google_notification(message: dict) -> str:
    """Play Real-Time Developer Notifications, delivered via Pub/Sub push.

    The body is a Pub/Sub envelope whose `message.data` is base64 JSON.
    """
    data_b64 = (message.get("message") or {}).get("data")
    if not data_b64:
        return "ignored:no_data"
    payload = json.loads(base64.b64decode(data_b64))
    sub_notice = payload.get("subscriptionNotification")
    if not sub_notice:
        return "ignored:not_a_subscription"

    purchase_token = sub_notice.get("purchaseToken")
    notification_type = int(sub_notice.get("notificationType") or 0)
    message_id = (message.get("message") or {}).get("messageId", purchase_token)

    try:
        service().table("stripe_events").insert({
            "id": f"google:{message_id}",
            "type": f"google.subscription.{notification_type}",
            "payload": payload, "status": "received",
        }).execute()
    except Exception:
        return "duplicate"

    sub = maybe_one(
        service().table("subscriptions").select("user_id")
        .like("stripe_subscription_id", f"google_iap:%{purchase_token[:32]}%")
        .limit(1).execute()
    )
    if not sub:
        return "ignored:unknown_user"

    if notification_type in GOOGLE_LIVE:
        # Re-verify rather than trusting the notification's own claim.
        await verify_google(sub["user_id"], sub_notice.get("subscriptionId", ""), purchase_token)
    elif notification_type in GOOGLE_DEAD:
        apply_entitlement(
            user_id=sub["user_id"], product_id=sub_notice.get("subscriptionId", ""),
            is_active=False, expires_at=None, source="google_iap",
            original_transaction_id=purchase_token[:64],
        )
    service().table("stripe_events").update({
        "status": "processed", "processed_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", f"google:{message_id}").execute()
    return f"processed:{notification_type}"
