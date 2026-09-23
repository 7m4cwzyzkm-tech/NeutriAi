"""The free-launch period: NeutriAI launches free for everyone while it
collects real corrected usage data, with no way to actually collect payment
set up yet. `settings.free_launch_mode` is the single toggle that reverts
this to real gating -- these tests lock down both states.
"""
from __future__ import annotations

import pytest

from app import deps
from app.config import Settings
from app.errors import PaymentRequired


def _settings_from(text: str) -> Settings:
    """Build Settings from an env-file body, without pytest fixtures.

    Same helper as tests/test_macros.py's _settings_from -- duplicated
    rather than imported, since that one is private to its own file.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / ".env"
        env.write_text(text)
        return Settings(_env_file=str(env))


def test_free_launch_mode_defaults_to_true():
    """On unless explicitly turned off -- this is the current product
    state, not a maybe."""
    assert _settings_from("").free_launch_mode is True


def test_free_launch_mode_env_var_turns_it_off():
    assert _settings_from("FREE_LAUNCH_MODE=false\n").free_launch_mode is False
    assert _settings_from("FREE_LAUNCH_MODE=true\n").free_launch_mode is True


# --- require_pro -------------------------------------------------------

async def test_require_pro_is_bypassed_during_free_launch_mode(monkeypatch):
    monkeypatch.setattr(deps.settings, "free_launch_mode", True)
    ent = {"is_active": False, "tier": "free"}
    result = await deps.require_pro(ent)
    assert result is ent, "must return the entitlement unchanged, not fabricate one"


async def test_require_pro_still_gates_when_free_launch_mode_is_off(monkeypatch):
    monkeypatch.setattr(deps.settings, "free_launch_mode", False)
    with pytest.raises(PaymentRequired):
        await deps.require_pro({"is_active": False, "tier": "free"})
    # An active subscriber is unaffected either way.
    ent = {"is_active": True, "tier": "pro"}
    assert await deps.require_pro(ent) is ent


# --- consume_ai_scan -----------------------------------------------------

class _FakeRpc:
    def __init__(self, params, row):
        self.params = params
        self._row = row

    def execute(self):
        return type("R", (), {"data": [self._row]})()


class _FakeClient:
    """Just enough of the Supabase client to capture the RPC call's params."""

    def __init__(self, row):
        self._row = row
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _FakeRpc(params, self._row)


def _user():
    return deps.CurrentUser(id="11111111-1111-1111-1111-111111111111",
                             email=None, jwt="x", claims={})


async def test_consume_ai_scan_uses_the_pro_ceiling_during_free_launch_mode(monkeypatch):
    """free_launch_mode reuses the RPC's existing 'Pro users are unmetered
    but still counted' path -- p_is_active=True, so v_limit becomes
    p_pro_ceiling server-side (supabase/migrations/0026_atomic_scan_quota.sql),
    not the free tier's daily cap -- for an entitlement that is NOT really
    active."""
    monkeypatch.setattr(deps.settings, "free_launch_mode", True)
    fake = _FakeClient({"used": 1, "quota": deps.settings.pro_daily_scan_ceiling, "allowed": True})
    monkeypatch.setattr(deps, "service", lambda: fake)

    ent = {"is_active": False, "tier": "free"}
    result = await deps.consume_ai_scan(_user(), ent)

    assert result is ent
    assert len(fake.calls) == 1
    name, params = fake.calls[0]
    assert name == "consume_ai_scan"
    assert params["p_is_active"] is True, "must reuse the Pro-tier ceiling path"
    assert params["p_pro_ceiling"] == deps.settings.pro_daily_scan_ceiling


async def test_consume_ai_scan_uses_the_real_state_when_free_launch_mode_is_off(monkeypatch):
    monkeypatch.setattr(deps.settings, "free_launch_mode", False)
    fake = _FakeClient({"used": 1, "quota": deps.settings.free_tier_daily_scans, "allowed": True})
    monkeypatch.setattr(deps, "service", lambda: fake)

    ent = {"is_active": False, "tier": "free"}
    await deps.consume_ai_scan(_user(), ent)

    _name, params = fake.calls[0]
    assert params["p_is_active"] is False, "an inactive account must still hit the free cap"


# --- GET /billing/subscription carries the flag ---------------------------

def test_subscription_response_carries_the_flag():
    """The mobile Profile card's one signal for 'no real limit right now' --
    no new endpoint, this response is already fetched by useSubscription()."""
    from app.models.billing import SubscriptionOut

    assert SubscriptionOut.model_fields["free_launch_mode"].default is False, (
        "the model's own default must not silently claim free-launch on a "
        "response nobody set the field on")

    import inspect

    from app.routers import billing

    src = inspect.getsource(billing.subscription)
    assert "free_launch_mode=settings.free_launch_mode" in src
