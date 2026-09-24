"""Tester weighed-verification: the rows that record predicted vs. scale."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.errors import Forbidden
from app.routers import scans
from app.services import accuracy_checks as AC


def _item(name, grams, source_index=None):
    return SimpleNamespace(name=name, grams=grams, source_index=source_index)


ORIGINAL = [
    {"name": "rice", "grams": 150.0},
    {"name": "creamy mushroom sauce", "grams": 113.0},
    {"name": "tortilla", "grams": 40.0},
]


def test_corrected_rows_only_for_items_whose_grams_changed():
    rows = AC.corrected_rows("u1", "s1", "m1", ORIGINAL, [
        _item("rice", 180.0),                              # changed, by name
        _item("rajas con crema", 98.0, source_index=1),    # renamed AND changed
        _item("tortilla", 40.0),                           # unchanged: no row
        _item("salsa", 30.0),                              # added: no prediction
    ])
    assert [(r["item_name"], r["predicted_grams"], r["actual_grams"]) for r in rows] == [
        ("rice", 150.0, 180.0),
        ("rajas con crema", 113.0, 98.0),
    ]
    assert {r["outcome"] for r in rows} == {"corrected"}
    assert {(r["user_id"], r["scan_id"], r["meal_id"]) for r in rows} == {("u1", "s1", "m1")}


def test_each_original_is_claimed_once():
    """Two tortillas corrected against one detected tortilla: one row, not two."""
    rows = AC.corrected_rows("u1", None, "m1", [{"name": "tortilla", "grams": 40.0}], [
        _item("tortilla", 30.0), _item("tortilla", 35.0),
    ])
    assert len(rows) == 1 and rows[0]["actual_grams"] == 30.0


def test_matched_rows_record_predicted_equal_to_actual():
    rows = AC.matched_rows("u1", "s1", "m1", [{"name": "rice", "grams": 150.0}])
    assert rows == [{
        "user_id": "u1", "scan_id": "s1", "meal_id": "m1", "outcome": "matched",
        "item_name": "rice", "predicted_grams": 150.0, "actual_grams": 150.0,
    }]


# --- a minimal in-memory Supabase for the route -----------------------------
class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.update_with = db, table, {}, None
        self.client_role = False

    def select(self, cols="*", *_a):
        # The client role under 0029: only the five public profile columns.
        if self.client_role and self.table == "profiles" and not (
            {c.strip() for c in cols.split(",")}
            <= {"id", "handle", "display_name", "avatar_url", "bio"}
        ):
            raise RuntimeError("permission denied for table profiles")
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def limit(self, _n):
        return self

    def update(self, patch):
        self.update_with = patch
        return self

    def execute(self):
        matching = [r for r in self.db[self.table]
                    if all(r.get(k) == v for k, v in self.filters.items())]
        if self.update_with is not None:
            for r in matching:
                r.update(self.update_with)
        if self.table == "meals":
            matching = [{**r, "meal_items": [i for i in self.db["meal_items"]
                                             if i["meal_id"] == r["id"]]} for r in matching]
        return SimpleNamespace(data=matching)


class _SB:
    """client_role=True behaves like user.sb under 0029; False like service()."""

    def __init__(self, db, client_role=False):
        self.db, self.client_role = db, client_role

    def table(self, name):
        q = _Query(self.db, name)
        q.client_role = self.client_role
        return q


def _as_user(monkeypatch, db):
    """A caller whose own client cannot read is_tester (0029), with the
    route's service() pointed at the same data -- where the flag lives."""
    monkeypatch.setattr(scans, "service", lambda: _SB(db))
    return SimpleNamespace(id="u1", sb=_SB(db, client_role=True))


def _db(tester: bool):
    return {
        "profiles": [{"id": "u1", "is_tester": tester}],
        "meals": [{"id": "m1", "user_id": "u1", "scan_id": "s1", "title": "Lunch",
                   "meal_slot": "lunch", "day": "2026-09-24",
                   "eaten_at": "2026-09-24T12:00:00+00:00", "kcal": 200.0,
                   "protein_g": 4.0, "carbs_g": 44.0, "fat_g": 0.5, "fiber_g": 0.6,
                   "sugar_g": 0.1, "is_verified": False, "confidence": 0.6}],
        "meal_items": [{"id": "i1", "meal_id": "m1", "name": "rice", "grams": 150.0}],
    }


def test_verify_is_refused_for_a_non_tester(monkeypatch):
    recorded = []
    monkeypatch.setattr(AC, "record", recorded.extend)
    db = _db(tester=False)
    user = _as_user(monkeypatch, db)
    with pytest.raises(Forbidden):
        asyncio.run(scans.verify_meal("m1", user))
    assert recorded == [] and db["meals"][0]["is_verified"] is False


def test_verify_records_a_match_and_marks_the_meal_verified(monkeypatch):
    recorded = []
    monkeypatch.setattr(AC, "record", recorded.extend)
    db = _db(tester=True)
    user = _as_user(monkeypatch, db)
    asyncio.run(scans.verify_meal("m1", user))
    assert [(r["outcome"], r["item_name"], r["predicted_grams"], r["actual_grams"])
            for r in recorded] == [("matched", "rice", 150.0, 150.0)]
    assert db["meals"][0]["is_verified"] is True
    # Nothing was wrong, so nothing about the meal's items changes.
    assert db["meal_items"] == _db(tester=True)["meal_items"]


def test_is_tester_is_false_when_the_column_is_not_there_yet():
    class Broken:
        def table(self, _name):
            raise RuntimeError('column profiles.is_tester does not exist')
    assert AC.is_tester(Broken(), "u1") is False


def test_is_tester_must_be_read_with_the_service_role():
    """The bug this locks down: under 0029 the caller's own client cannot read
    is_tester, and is_tester() turns that refusal into False -- so reading it
    through user.sb silently demoted every tester (verify -> 403). The routes
    pass service(); this shows why they must."""
    db = _db(tester=True)
    assert AC.is_tester(_SB(db, client_role=True), "u1") is False   # refused
    assert AC.is_tester(_SB(db), "u1") is True                       # service role
