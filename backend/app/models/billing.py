from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .common import InputBase, Base


class CheckoutRequest(InputBase):
    plan: str = Field("monthly", pattern="^(monthly|annual)$")
    promo_code: str | None = Field(None, max_length=40)
    success_url: str | None = None
    cancel_url: str | None = None


class CheckoutSession(Base):
    url: str
    session_id: str
    trial_days: int


class PortalSession(Base):
    url: str


class SubscriptionOut(Base):
    tier: str
    is_active: bool
    status: str | None = None
    plan_interval: str | None = None
    amount_cents: int | None = None
    currency: str = "usd"
    trial_end: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    promo_code: str | None = None
    ai_scans_used_today: int = 0
    ai_scans_quota: int = 0
    manage_url: str | None = None
    # settings.free_launch_mode, carried through so the client can tell "no
    # real limit right now" apart from "really on the free tier" without a
    # second endpoint -- this response is already fetched wherever that
    # distinction matters (ProfileScreen's Plan card).
    free_launch_mode: bool = False


class PricingPlan(Base):
    id: str
    name: str
    interval: str
    amount_cents: int
    currency: str = "usd"
    trial_days: int
    savings_note: str | None = None
    features: list[str] = []
