"""A food the user ADDS on the meal screen gets real nutrition on save.

The meal screen sends an added food as `{name, grams}` -- no `source_index`,
no `macros` -- because it has no nutrition for it. `correct_meal` looks a food
up only when `macros` is absent and stores any block it is sent as-is, so:

- the added item must be resolved by name and stored with the resolver's
  numbers, not zero;
- a detected item, sent with its macros block, must not be re-resolved;
- a zero macros block is still a block: sent for an added food it would be
  stored as a 0 kcal item. This is why the client omits the key.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.models.nutrition import MealIn
from app.routers import scans


class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters = db, table, {}
        self.op, self.payload = "select", None

    def select(self, *_a):
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def limit(self, _n):
        return self

    def delete(self):
        self.op = "delete"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, patch):
        self.op, self.payload = "update", patch
        return self

    def execute(self):
        rows = self.db[self.table]
        match = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.op == "delete":
            self.db[self.table] = [r for r in rows if r not in match]
            return SimpleNamespace(data=match)
        if self.op == "insert":
            new = self.payload if isinstance(self.payload, list) else [self.payload]
            rows.extend(dict(r) for r in new)
            return SimpleNamespace(data=new)
        if self.op == "update":
            for r in match:
                r.update(self.payload)
        if self.table == "meals":
            match = [{**r, "meal_items": [i for i in self.db["meal_items"]
                                          if i["meal_id"] == r["id"]]} for r in match]
        return SimpleNamespace(data=match)


class _SB:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        return _Query(self.db, name)


GARLIC_BREAD_FACT = {"id": "ff-garlic-bread", "kcal_per_100g": 350.0, "protein_per_100g": 8.0,
                     "carbs_per_100g": 42.0, "fat_per_100g": 16.0, "fiber_per_100g": 2.0,
                     "sugar_per_100g": 3.0, "sodium_mg_per_100g": 400.0}


def _db():
    return {
        "profiles": [{"id": "u1", "is_tester": False}],
        "meals": [{"id": "m1", "user_id": "u1", "scan_id": "s1", "title": "Lunch",
                   "meal_slot": "lunch", "day": "2026-09-24",
                   "eaten_at": "2026-09-24T12:00:00+00:00", "kcal": 195.0,
                   "protein_g": 4.0, "carbs_g": 42.0, "fat_g": 0.5, "fiber_g": 0.6,
                   "sugar_g": 0.1, "sodium_mg": 2.0, "is_verified": False,
                   "confidence": 0.6}],
        "meal_items": [{"id": "i1", "meal_id": "m1", "name": "rice", "grams": 150.0,
                        "estimation_method": "vessel_reference"}],
    }


def _run(monkeypatch, body_json):
    db = _db()
    resolved: list[str] = []

    async def fake_resolve(name, **_kw):
        resolved.append(name)
        return GARLIC_BREAD_FACT

    monkeypatch.setattr(scans.resolver, "resolve", fake_resolve)
    monkeypatch.setattr(scans, "service", lambda: _SB(db))
    for mod in (scans.calibration, scans.portion_learning, scans.food_identity):
        monkeypatch.setattr(mod, "learn_from_correction", lambda *a, **k: None)
    user = SimpleNamespace(id="u1", sb=_SB(db))
    out = asyncio.run(scans.correct_meal("m1", MealIn(**body_json), user))
    return out, db, resolved


# Exactly what MealDetailScreen.save() sends: the detected rice with its macros
# and source_index, and the added food as name + grams only.
BODY = {
    "title": "Lunch", "meal_slot": "lunch",
    "items": [
        {"name": "rice", "grams": 150, "source_index": 0,
         "macros": {"kcal": 195, "protein_g": 4, "carbs_g": 42, "fat_g": 0.5,
                    "fiber_g": 0.6, "sugar_g": 0.1, "sodium_mg": 2}},
        {"name": "garlic bread", "grams": 60},
    ],
}


def test_added_food_is_resolved_and_stored_with_real_macros(monkeypatch):
    out, db, resolved = _run(monkeypatch, BODY)
    assert resolved == ["garlic bread"]          # the detected rice is not re-resolved
    added = next(i for i in db["meal_items"] if i["name"] == "garlic bread")
    assert added["kcal"] == 210.0                # 350 kcal/100 g x 60 g
    assert added["sodium_mg"] == 240.0
    assert added["food_fact_id"] == "ff-garlic-bread"
    assert added["estimation_method"] == "user_entered"
    assert out.kcal == 405.0                     # 195 + 210


def test_the_added_item_carries_no_source_index():
    item = MealIn(**BODY).items[1]
    assert item.source_index is None and item.macros is None


def test_a_zero_macros_block_would_have_been_stored_as_zero(monkeypatch):
    """Why the client omits `macros` for an added food rather than sending its
    unknown per-gram rates, which are zero."""
    body = {**BODY, "items": [BODY["items"][0],
                              {"name": "garlic bread", "grams": 60,
                               "macros": {"kcal": 0, "protein_g": 0, "carbs_g": 0,
                                          "fat_g": 0, "fiber_g": 0, "sugar_g": 0,
                                          "sodium_mg": 0}}]}
    _, db, resolved = _run(monkeypatch, body)
    assert resolved == []
    assert next(i for i in db["meal_items"] if i["name"] == "garlic bread")["kcal"] == 0


def test_a_negative_sentinel_index_is_refused():
    """Why the client leaves sourceIndex undefined rather than using -1."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MealIn(**{**BODY, "items": [{"name": "garlic bread", "grams": 60, "source_index": -1}]})
