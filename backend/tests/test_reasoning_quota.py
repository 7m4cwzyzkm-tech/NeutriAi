"""POST /recipes/{id}/adapt and POST /plans call a paid reasoning model and
were gated by ProDep alone -- require_pro only checks subscription status,
not call volume, and free_launch_mode skips even that check for everyone
right now. consume_ai_reasoning / AiReasoningDep closes that gap the same
way consume_ai_scan already closes it for meal/equipment scans.
"""
from __future__ import annotations

import inspect

import pytest

from app import deps
from app.errors import QuotaExceeded, UpstreamError


def _user():
    return deps.CurrentUser(id="11111111-1111-1111-1111-111111111111",
                             email=None, jwt="x", claims={})


# --- the quota numbers themselves -----------------------------------------

def test_reasoning_quotas_are_lower_than_the_scan_quotas():
    """Deliberate, not a copy-paste of the scan numbers: a recipe adaptation
    (4000 output tokens) or a full plan generation (8000) costs several
    times what a vision scan (1400) does, and both are naturally occasional
    actions rather than something done several times per meal."""
    from app.config import settings

    assert settings.free_tier_daily_reasoning_calls <= settings.free_tier_daily_scans
    assert settings.pro_daily_reasoning_ceiling < settings.pro_daily_scan_ceiling
    assert 20 <= settings.pro_daily_reasoning_ceiling <= 200, (
        "high enough that no real person meets it, low enough that a script does")


# --- consume_ai_reasoning: the RPC call itself -----------------------------

class _FakeRpc:
    def __init__(self, params, row):
        self.params = params
        self._row = row

    def execute(self):
        return type("R", (), {"data": [self._row]})()


class _FakeClient:
    """Just enough of the Supabase client to capture the RPC call's params."""

    def __init__(self, row=None, raises=None):
        self._row = row
        self._raises = raises
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name, params):
        if self._raises:
            raise self._raises
        self.calls.append((name, params))
        return _FakeRpc(params, self._row)


async def test_consume_ai_reasoning_uses_the_pro_ceiling_during_free_launch_mode(monkeypatch):
    """Mirrors consume_ai_scan's own free_launch_mode test exactly: an
    entitlement that is NOT really active still gets p_is_active=True sent
    to the RPC, so the server-side v_limit becomes p_pro_ceiling
    (0027_atomic_reasoning_quota.sql) rather than the free tier's daily cap."""
    monkeypatch.setattr(deps.settings, "free_launch_mode", True)
    fake = _FakeClient({"used": 1, "quota": deps.settings.pro_daily_reasoning_ceiling, "allowed": True})
    monkeypatch.setattr(deps, "service", lambda: fake)

    ent = {"is_active": False, "tier": "free"}
    result = await deps.consume_ai_reasoning(_user(), ent)

    assert result is ent
    assert len(fake.calls) == 1
    name, params = fake.calls[0]
    assert name == "consume_ai_reasoning"
    assert params["p_is_active"] is True, "must reuse the Pro-tier ceiling path"
    assert params["p_pro_ceiling"] == deps.settings.pro_daily_reasoning_ceiling


async def test_consume_ai_reasoning_uses_the_real_state_when_free_launch_mode_is_off(monkeypatch):
    monkeypatch.setattr(deps.settings, "free_launch_mode", False)
    fake = _FakeClient({"used": 1, "quota": deps.settings.free_tier_daily_reasoning_calls, "allowed": True})
    monkeypatch.setattr(deps, "service", lambda: fake)

    ent = {"is_active": False, "tier": "free"}
    await deps.consume_ai_reasoning(_user(), ent)

    _name, params = fake.calls[0]
    assert params["p_is_active"] is False, "an inactive account must still hit the free cap"


async def test_consume_ai_reasoning_allows_a_call_under_quota(monkeypatch):
    monkeypatch.setattr(deps.settings, "free_launch_mode", False)
    fake = _FakeClient({"used": 1, "quota": 2, "allowed": True})
    monkeypatch.setattr(deps, "service", lambda: fake)

    ent = {"is_active": True, "tier": "pro"}
    result = await deps.consume_ai_reasoning(_user(), ent)
    assert result is ent


async def test_consume_ai_reasoning_raises_quota_exceeded_over_the_free_cap(monkeypatch):
    monkeypatch.setattr(deps.settings, "free_launch_mode", False)
    fake = _FakeClient({"used": 3, "quota": 2, "allowed": False})
    monkeypatch.setattr(deps, "service", lambda: fake)

    with pytest.raises(QuotaExceeded) as exc:
        await deps.consume_ai_reasoning(_user(), {"is_active": False, "tier": "free"})
    assert exc.value.detail["upgrade"] is True


async def test_consume_ai_reasoning_raises_quota_exceeded_over_the_pro_ceiling(monkeypatch):
    monkeypatch.setattr(deps.settings, "free_launch_mode", False)
    fake = _FakeClient({"used": 41, "quota": 40, "allowed": False})
    monkeypatch.setattr(deps, "service", lambda: fake)

    with pytest.raises(QuotaExceeded) as exc:
        await deps.consume_ai_reasoning(_user(), {"is_active": True, "tier": "pro"})
    assert exc.value.detail["upgrade"] is False, "an active subscriber over the ceiling is not offered an upgrade"


async def test_a_reasoning_quota_check_that_cannot_be_reached_fails_closed(monkeypatch):
    """Same as consume_ai_scan: failing open on a paid call means breaking
    the database is how you get free reasoning calls. It must fail closed."""
    fake = _FakeClient(raises=RuntimeError("db unreachable"))
    monkeypatch.setattr(deps, "service", lambda: fake)

    with pytest.raises(UpstreamError):
        await deps.consume_ai_reasoning(_user(), {"is_active": False, "tier": "free"})


def test_the_reasoning_counter_is_incremented_by_the_database_not_by_us():
    """Same source-level guard as test_billing.py's equivalent for
    consume_ai_scan. There is no live-Postgres concurrency test for
    consume_ai_scan in this suite either -- its own docstring records a
    one-time manual verification ("40 concurrent calls... verified against
    PostgreSQL 16"), not a repeatable automated test, and this sandbox has
    no live database to run one against. consume_ai_reasoning's SQL
    (0027_atomic_reasoning_quota.sql) is structurally identical to
    consume_ai_scan's already-proven-safe SQL -- one UPDATE ... RETURNING
    statement, no separate read before the write -- so the race-safety
    property transfers by construction. This test guards that the Python
    side still calls the atomic RPC rather than reintroducing a
    read-modify-write."""
    src = inspect.getsource(deps.consume_ai_reasoning)
    assert 'rpc("consume_ai_reasoning"' in src, "the increment is not atomic"
    assert "used + 1" not in src, "a read-modify-write has been reintroduced"


def test_the_reasoning_quota_migration_has_no_separate_read_before_the_write():
    """Static proxy for the same race-safety property, one level down: the
    SQL itself must increment and check in one statement, the same shape
    0026_atomic_scan_quota.sql already proved safe under concurrency."""
    import re
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "supabase" / "migrations" / "0027_atomic_reasoning_quota.sql"
    )
    sql = path.read_text()
    assert "create or replace function public.consume_ai_reasoning" in sql
    # The increment happens INSIDE the update ... returning statement, not
    # as a separate select beforehand.
    body = sql.split("consume_ai_reasoning(", 1)[1]
    update_idx = body.index("ai_reasoning_used_today = ai_reasoning_used_today + 1")
    select_idx = body.lower().find("select ai_reasoning_used_today")
    assert select_idx == -1 or select_idx > update_idx, (
        "a separate SELECT before the UPDATE is exactly the race consume_ai_scan's "
        "own migration fixed -- this must stay one statement")
    assert re.search(r"revoke all on function public\.consume_ai_reasoning", sql)


# --- get_entitlement: both daily counters roll over together --------------

class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeEntitlementsTable:
    """Just enough of the postgrest builder to exercise get_entitlement's
    daily rollover -- same style as test_profile_update.py's FakeProfilesTable."""

    def __init__(self, row: dict):
        self.row = dict(row)
        self.last_patch: dict | None = None

    def select(self, *_a, **_k):
        return self

    def insert(self, payload):
        self.last_patch = payload
        self._inserting = True
        return self

    def update(self, patch):
        self.last_patch = patch
        self._inserting = False
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        if self.last_patch is not None:
            self.row.update(self.last_patch)
            self.last_patch = None
        return _FakeResp([dict(self.row)])


class _FakeEntitlementsClient:
    def __init__(self, table):
        self._table = table

    def table(self, _name):
        return self._table


async def test_get_entitlement_resets_both_counters_on_the_same_day_rollover(monkeypatch):
    """quota_reset_on is shared deliberately (0027's own migration comment) --
    both counters must actually reset together, not just the scan one."""
    stale = {
        "user_id": "u1", "tier": "free", "is_active": False,
        "ai_scans_used_today": 3, "ai_scans_quota": 3,
        "ai_reasoning_used_today": 2, "ai_reasoning_quota": 2,
        "quota_reset_on": "2020-01-01",
    }
    table = _FakeEntitlementsTable(stale)
    monkeypatch.setattr(deps, "service", lambda: _FakeEntitlementsClient(table))

    row = await deps.get_entitlement(_user())

    assert row["ai_scans_used_today"] == 0
    assert row["ai_reasoning_used_today"] == 0
    assert row["quota_reset_on"] != "2020-01-01"


async def test_get_entitlement_seeds_the_reasoning_quota_for_a_new_user(monkeypatch):
    """A brand-new entitlement row must take its reasoning quota from
    settings, the same way it already does for ai_scans_quota -- not
    silently fall back to the column's own SQL default regardless of what
    the setting says."""
    from app.config import settings

    monkeypatch.setattr(settings, "free_tier_daily_reasoning_calls", 5)

    class _NoRowThenInsertTable(_FakeEntitlementsTable):
        def __init__(self):
            self._selected = False

        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def insert(self, payload):
            self._inserted = payload
            return self

        def execute(self):
            if not self._selected:
                self._selected = True
                return _FakeResp([])  # maybe_one -> None
            return _FakeResp([{**self._inserted, "quota_reset_on": __import__("datetime").date.today().isoformat()}])

    fresh = _NoRowThenInsertTable()
    monkeypatch.setattr(deps, "service", lambda: _FakeEntitlementsClient(fresh))

    await deps.get_entitlement(_user())

    assert fresh._inserted["ai_reasoning_quota"] == 5
