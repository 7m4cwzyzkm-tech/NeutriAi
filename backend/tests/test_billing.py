"""Billing rules that must hold regardless of what Stripe returns."""
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
