"""Billing rules that must hold regardless of what Stripe returns."""
import json

from app.services.billing.stripe_service import LIVE_STATUSES, TIER_BY_INTERVAL, pricing_table


def test_live_statuses_include_trial_and_grace():
    # Trialing users must have full access — that is the whole point of a trial.
    assert "trialing" in LIVE_STATUSES
    assert "active" in LIVE_STATUSES
    # A failed renewal should not lock someone out during Stripe's retry window.
    assert "past_due" in LIVE_STATUSES
    assert "canceled" not in LIVE_STATUSES
    assert "incomplete_expired" not in LIVE_STATUSES


def test_tier_mapping():
    assert TIER_BY_INTERVAL["month"] == "pro"
    assert TIER_BY_INTERVAL["year"] == "pro_annual"


def test_pricing_matches_the_spec():
    plans = {p["id"]: p for p in pricing_table()}
    assert plans["monthly"]["amount_cents"] == 699
    assert plans["annual"]["amount_cents"] == 5000
    assert plans["monthly"]["trial_days"] == 15
    assert plans["annual"]["trial_days"] == 15


def test_annual_is_actually_cheaper():
    plans = {p["id"]: p for p in pricing_table()}
    monthly_year = plans["monthly"]["amount_cents"] * 12
    assert plans["annual"]["amount_cents"] < monthly_year
    assert monthly_year - plans["annual"]["amount_cents"] == 3388  # $33.88 saved


def test_every_plan_lists_features():
    for p in pricing_table():
        assert p["features"], f"{p['id']} has no feature list"


# --- a notification is a doorbell, not a document ------------------------------

def test_a_forged_apple_notification_cannot_grant_or_revoke(monkeypatch):
    """Apple signs server notifications with a certificate chain, and the
    decoder does not check it. Every field in one is therefore attacker
    controlled.

    Acting on them directly -- which this did -- means a forged DID_RENEW with
    an expiry in 2099 is permanent free Pro, and a forged REVOKE cancels a
    paying customer. The endpoint is unauthenticated by design; Apple has to be
    able to reach it.

    The fix is not to verify the signature but to stop treating the payload as
    a claim: it supplies an idempotency key and an id to look up, and the
    entitlement comes from asking Apple what the subscription actually is.
    """
    import inspect

    from app.services.billing import iap

    src = inspect.getsource(iap.handle_apple_notification)

    # the authoritative re-fetch happens
    assert "verify_apple(" in src, (
        "the notification handler never asks Apple what the subscription is")

    # and nothing is granted straight from the payload any more
    granted_from_payload = (
        "apply_entitlement(" in src
        and "verify_apple(" not in src.split("apply_entitlement(")[0][-400:]
    )
    assert not granted_from_payload, (
        "entitlement is still applied from the unverified notification body")


def test_a_notification_apple_will_not_confirm_is_left_for_retry(monkeypatch):
    """Marking it processed would mean neither Apple nor we ever retry it, and
    a real renewal would be dropped because Apple's API happened to be down."""
    import inspect

    from app.services.billing import iap

    src = inspect.getsource(iap.handle_apple_notification)
    assert '"status": "failed"' in src
    assert "deferred:" in src
    # and it must not fall through to marking the row processed
    failed_at = src.index('"status": "failed"')
    returns_after = src.index("deferred:", failed_at)
    processed_at = src.index('"status": "processed"')
    assert returns_after < processed_at, (
        "the failure path falls through into marking the event processed")


def test_a_deferred_notification_is_answered_with_a_retryable_status():
    """The word "deferred" did nothing at all, and the test above is why.

    It checked that the STRING appears in the source. It does. But the router
    put that string in a JSON body under a 200, and a 200 means delivered:
    Apple and Google stopped retrying, and the renewal, cancellation or refund
    was gone. There is no dashboard to replay a store notification from -- the
    retry IS the recovery -- so the status code is the only part either store
    acts on.

    Tested through the reply the route actually returns, because the source
    check above passed for the entire life of the bug.
    """
    from app.routers.billing import _store_reply

    for handled in ("processed", "duplicate", "ignored:no_transaction", "", None):
        r = _store_reply(handled)
        assert r.status_code == 200, (
            f"{handled!r} was handled, so asking the store to resend it is wrong")

    for deferred in ("deferred:claim_failed", "deferred:1", "deferred:DID_RENEW"):
        r = _store_reply(deferred)
        assert r.status_code == 503, (
            f"{deferred!r} answered {r.status_code} -- the store takes any 2xx "
            f"as delivered and will never send it again")
        # `.body` (bytes), not `.content` -- JSONResponse has no `.content`, so
        # this line raised AttributeError and never checked anything. The route
        # was right the whole time; every status assertion above it passed.
        assert json.loads(r.body)["received"] is False


def test_a_sick_database_is_not_reported_as_a_duplicate_purchase():
    """The idempotency claim caught EVERY exception and called it a duplicate.

    That is right for a unique violation -- the notification really has been
    seen. It is wrong for a timeout, a dropped connection, or any other blip:
    the claim never happened, the notification was never processed, and
    answering "duplicate" told the store we were done with it.

    The default has to be 'not a duplicate'. A retried duplicate costs one
    wasted round trip; a swallowed renewal costs somebody their subscription.
    """
    import pytest

    from app.services.billing.iap import claim_event, is_duplicate

    real = [Exception('duplicate key value violates unique constraint "pkey"'),
            Exception("23505"),
            Exception("relation already exists")]
    blips = [Exception("connection timed out"), TimeoutError("read timeout"),
             Exception(""), Exception("upstream connect error"),
             ConnectionResetError("reset by peer")]
    for exc in real:
        assert is_duplicate(exc) is True, exc
    for exc in blips:
        assert is_duplicate(exc) is False, (
            f"{exc!r} would be swallowed as a duplicate and the notification lost")

    # And the claim turns each into the right outcome.
    import app.services.billing.iap as I

    class _Boom:
        def __init__(self, exc): self.exc = exc
        def table(self, *_a): return self
        def insert(self, *_a): return self
        def execute(self): raise self.exc

    real_dup, blip = real[0], blips[0]
    orig = I.service
    try:
        # Both outcomes RAISE. A returned string could be -- and in a mutation
        # was -- dropped by a caller that forgot to read it, which silently
        # carries on as though the claim had succeeded.
        I.service = lambda: _Boom(real_dup)
        with pytest.raises(I.NotHandled) as dup:
            claim_event({"id": "x"})
        assert dup.value.outcome == "duplicate"

        I.service = lambda: _Boom(blip)
        with pytest.raises(I.NotHandled) as blipped:
            claim_event({"id": "x"})
        assert blipped.value.outcome.startswith("deferred:"), (
            f"a database blip gave {blipped.value.outcome!r} instead of a retry")

        # A successful claim returns nothing, so there is no value to ignore.
        class _Ok:
            def table(self, *_a): return self
            def insert(self, *_a): return self
            def execute(self): return None
        I.service = lambda: _Ok()
        assert claim_event({"id": "x"}) is None
    finally:
        I.service = orig

    # And forgetting to catch it fails SAFE: the exception escapes, the request
    # 500s, and a non-2xx is exactly what makes the store send it again.
    assert issubclass(I.NotHandled, Exception)


def test_an_empty_google_token_cannot_match_every_subscriber():
    """The lookup was `like('google_iap:%' + token[:32] + '%')`. An empty token
    makes that `google_iap:%%`, which matches every row in the table; the first
    one won. An anonymous Pub/Sub push could therefore cancel a stranger's
    subscription, repeatedly, using a fresh message id each time to get past the
    idempotency insert.

    Two things were wrong and both are fixed: the token is length-checked before
    it is used, and the lookup is an equality rather than a pattern -- a `_` in
    a token is a single-character wildcard in SQL, so even an honest token could
    have matched the wrong row.
    """
    import inspect

    from app.services.billing import iap

    src = inspect.getsource(iap.handle_google_notification)
    assert "MIN_GOOGLE_TOKEN" in src, "the token length is never checked"
    assert '.like("stripe_subscription_id"' not in src, "still a pattern match"
    assert '.eq("stripe_subscription_id"' in src

    # the check must come BEFORE the lookup, not after
    assert src.index("MIN_GOOGLE_TOKEN") < src.index('.eq("stripe_subscription_id"')
    assert iap.MIN_GOOGLE_TOKEN >= 20


def test_the_scan_counter_is_incremented_by_the_database_not_by_us():
    """It read the count, added one, and wrote it back. Two scans arriving
    together both read the same number and both wrote one more than it, so N
    concurrent requests cost ONE quota unit -- a free user got unlimited scans
    by sending them at once, each a paid GPT-4o call.

    Verified against PostgreSQL 16 with the migration applied: 40 concurrent
    calls against a quota of 3 allow exactly 3, and the stored counter lands on
    3 rather than drifting.
    """
    import inspect

    from app import deps

    src = inspect.getsource(deps.consume_ai_scan)
    assert 'rpc("consume_ai_scan"' in src, "the increment is not atomic"
    assert "used + 1" not in src, "the read-modify-write is still there"


def test_a_subscription_is_not_a_blank_cheque():
    """Pro was unmetered. One account, unlimited vision calls, at a per-scan
    cost the subscription does not cover."""
    from app.config import settings

    assert settings.pro_daily_scan_ceiling > settings.free_tier_daily_scans
    assert 20 <= settings.pro_daily_scan_ceiling <= 2000, (
        "high enough that no real person meets it, low enough that a script does")


def test_a_counter_that_cannot_be_reached_is_not_a_free_pass():
    """Failing open on a paid call means breaking the database is how you get
    free GPT-4o. It fails closed and the user retries."""
    import inspect

    from app import deps

    src = inspect.getsource(deps.consume_ai_scan)
    assert "raise UpstreamError" in src
    assert "except Exception" in src
    assert src.index("except Exception") < src.index("raise UpstreamError")
