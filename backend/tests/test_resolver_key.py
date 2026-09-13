"""The nutrition cache key keeps the preparation.

The bug this locks down: `canonical` dropped "raw", "cooked" and "fresh", so
`raw rice`, `cooked rice` and `rice` all keyed the same `food_facts` row -- one
table shared by every user. Whichever name reached the key first was searched
and stored; every later name was served that row and never searched. Raw white
rice is 365 kcal per 100 g and cooked is 130, so one meal-prep weigher logging
dry rice would have set every user's cooked bowl at 2.8x, permanently.
RESOLVER_VERSION cleared rows on a matching-logic change, never on a collision.

The SEARCH was never stripped -- `resolve` hands the full name to the providers
-- so this is a wrong-cache bug, fixed at the key.
"""
from __future__ import annotations

import asyncio

from app.services.nutrition import resolver


class FakeResp:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    """Just enough of the postgrest builder for resolve's read, update and upsert."""

    def __init__(self, store: dict):
        self._store = store
        self._filters: list = []
        self._write = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append(lambda r: r.get(col) == val)
        return self

    def ilike(self, col, pattern):
        needle = pattern.strip("%").lower()
        self._filters.append(lambda r: needle in str(r.get(col) or "").lower())
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def update(self, payload):
        self._write = ("update", payload)
        return self

    def upsert(self, payload, on_conflict=None):
        self._write = ("upsert", payload)
        return self

    def execute(self):
        if self._write and self._write[0] == "upsert":
            payload = self._write[1]
            key = payload["canonical_key"]
            row = {**self._store.get(key, {}), **payload, "id": key}
            self._store[key] = row
            return FakeResp([dict(row)])
        matched = [r for r in self._store.values() if all(f(r) for f in self._filters)]
        if self._write:
            for r in matched:
                r.update(self._write[1])
        return FakeResp([dict(r) for r in matched])


class FakeClient:
    def __init__(self, store):
        self._store = store

    def table(self, _name):
        return FakeQuery(self._store)


def _fact(name: str, kcal: float, carbs: float) -> dict:
    return {"name": name, "source": "usda", "source_id": f"fdc-{name}", "cuisine": None,
            "density_g_ml": None, "kcal_per_100g": kcal, "protein_per_100g": 2.7,
            "carbs_per_100g": carbs, "fat_per_100g": 0.3, "fiber_per_100g": 0.4,
            "sugar_per_100g": 0.1, "sodium_mg_per_100g": 1.0, "serving_hints": [], "raw": {}}


def test_preparation_words_stay_in_the_key():
    raw, cooked, bare = (resolver.canonical(n) for n in ("raw rice", "cooked rice", "rice"))
    assert len({raw, cooked, bare}) == 3
    assert resolver.canonical("fresh spinach") != resolver.canonical("spinach")
    assert resolver.canonical("raw chicken breast") != resolver.canonical("cooked chicken breast")
    # Spelling noise still collapses -- that is what the key is for.
    assert resolver.canonical("Cooked  Rice") == resolver.canonical("cooked rice")


def test_raw_rice_and_cooked_rice_are_different_cached_rows(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(resolver, "service", lambda: FakeClient(store))
    usda = {"raw rice": (365.0, 80.0), "cooked rice": (130.0, 28.2)}
    searched: list[str] = []

    async def race(query, order=None):
        searched.append(query)
        return _fact(query, *usda[query])

    monkeypatch.setattr(resolver.providers, "race_providers", race)

    raw = asyncio.run(resolver.resolve("raw rice"))
    cooked = asyncio.run(resolver.resolve("cooked rice"))

    assert raw["kcal_per_100g"] == 365.0
    assert cooked["kcal_per_100g"] == 130.0, "cooked rice was served raw rice's cached row"
    assert len(store) == 2
    # The second name was SEARCHED, with its preparation intact, not served.
    assert searched == ["raw rice", "cooked rice"]

    # And each name now hits its own row without searching again.
    again = asyncio.run(resolver.resolve("cooked rice"))
    assert again["kcal_per_100g"] == 130.0
    assert searched == ["raw rice", "cooked rice"]


def test_rows_written_under_the_collapsing_key_are_not_served():
    """The rows the old key wrote are invalidated by the version, not left in place."""
    assert resolver.RESOLVER_VERSION >= 3
    assert not resolver._is_current({"raw": {"resolver_version": 2}})
    assert resolver._is_current({"raw": {"resolver_version": resolver.RESOLVER_VERSION}})
