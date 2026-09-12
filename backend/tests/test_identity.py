"""Profile bootstrap.

The bug this locks down: every user-scoped table has a foreign key to
profiles(id), but nothing created that row except GET /me. Any other route
hit first -- a scan, a water log, even the entitlement lookup that gates the
scan -- died on a foreign key violation the client saw as a bare 500.
"""
from __future__ import annotations

import pytest

from app.errors import Unauthorized
from app.services import identity


class FakeResp:
    def __init__(self, data):
        self.data = data


class FakeTable:
    """Just enough of the postgrest builder to exercise ensure_profile."""

    def __init__(self, existing, on_insert):
        self._existing = existing
        self._on_insert = on_insert
        self.inserted: list[dict] = []

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def insert(self, payload):
        self._pending = payload
        return self

    def execute(self):
        pending = getattr(self, "_pending", None)
        if pending is None:
            return FakeResp(list(self._existing))
        self._pending = None
        self.inserted.append(pending)
        return self._on_insert(pending)


class FakeClient:
    def __init__(self, table):
        self._table = table

    def table(self, _name):
        return self._table


@pytest.fixture(autouse=True)
def _clear_cache():
    identity._KNOWN.clear()
    yield
    identity._KNOWN.clear()


def _install(monkeypatch, existing, on_insert):
    table = FakeTable(existing, on_insert)
    monkeypatch.setattr(identity, "service", lambda: FakeClient(table))
    return table


def test_returns_existing_profile_without_inserting(monkeypatch):
    row = {"id": "u1", "handle": "gil"}
    table = _install(monkeypatch, [row], lambda p: FakeResp([p]))
    assert identity.ensure_profile("u1") == row
    assert table.inserted == []


def test_creates_profile_on_first_sight(monkeypatch):
    table = _install(monkeypatch, [], lambda p: FakeResp([p]))
    out = identity.ensure_profile("u1", "gil@example.com")
    assert out["id"] == "u1"
    assert out["handle"] == "gil"
    assert len(table.inserted) == 1


def test_deleted_account_gets_401_not_500(monkeypatch):
    """A deleted user's JWT stays valid for up to an hour. Creating a profile
    then violates profiles.id -> auth.users(id). The client needs a 401 so it
    signs out, not a 500 it retries forever."""
    def boom(_p):
        raise RuntimeError(
            '{"code": "23503", "message": "violates foreign key constraint '
            '\\"profiles_id_fkey\\""}'
        )

    _install(monkeypatch, [], boom)
    with pytest.raises(Unauthorized):
        identity.ensure_profile("gone")


def test_handle_collision_retries_with_suffix(monkeypatch):
    attempts: list[str] = []

    def collide_once(payload):
        attempts.append(payload["handle"])
        if len(attempts) == 1:
            raise RuntimeError('duplicate key value violates unique constraint "profiles_handle_key"')
        return FakeResp([payload])

    _install(monkeypatch, [], collide_once)
    out = identity.ensure_profile("abcd1234", "gil@example.com")
    assert attempts == ["gil", "gilabcd"]
    assert out["handle"] == "gilabcd"


def test_concurrent_first_request_does_not_500(monkeypatch):
    """The app opens several requests at once on launch. Two can both find no
    profile and both insert; one wins and the other gets a duplicate key on
    profiles.id. The loser must return the winner's row, not retry with a
    different handle -- that hits the same primary key and fails again."""
    state = {"created": False}

    class RacingTable(FakeTable):
        def execute(self):
            pending = getattr(self, "_pending", None)
            if pending is None:
                # The select: empty the first time, populated once the "other
                # request" has created the row.
                return FakeResp([{"id": "u1", "handle": "gil"}] if state["created"] else [])
            self._pending = None
            self.inserted.append(pending)
            state["created"] = True          # the other request won the race
            raise RuntimeError(
                'duplicate key value violates unique constraint "profiles_pkey"'
            )

    table = RacingTable([], lambda p: FakeResp([p]))
    monkeypatch.setattr(identity, "service", lambda: FakeClient(table))

    out = identity.ensure_profile("u1", "gil@example.com")
    assert out["id"] == "u1"
    assert len(table.inserted) == 1, "should not retry a second handle after a PK clash"


def test_cached_call_skips_the_round_trip(monkeypatch):
    calls = {"n": 0}

    class CountingTable(FakeTable):
        def execute(self):
            if getattr(self, "_pending", None) is None:
                calls["n"] += 1
            return super().execute()

    table = CountingTable([{"id": "u1", "handle": "gil"}], lambda p: FakeResp([p]))
    monkeypatch.setattr(identity, "service", lambda: FakeClient(table))

    identity.ensure_profile_cached("u1")
    identity.ensure_profile_cached("u1")
    identity.ensure_profile_cached("u1")
    assert calls["n"] == 1, "the cache should collapse repeat lookups to one query"


def test_handle_is_sanitised():
    assert identity.handle_for("u1", "gil.cons+tag@example.com") == "gil.constag"
    assert identity.handle_for("abcd1234", None) == "userabcd1234"
    assert identity.handle_for("abcd1234", "!!!@example.com") == "uabcd1234"


def test_forget_drops_the_cache_entry(monkeypatch):
    _install(monkeypatch, [{"id": "u1"}], lambda p: FakeResp([p]))
    identity.ensure_profile_cached("u1")
    assert "u1" in identity._KNOWN
    identity.forget("u1")
    assert "u1" not in identity._KNOWN
