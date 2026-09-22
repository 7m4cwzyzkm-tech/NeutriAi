"""PATCH /me: sex and birth_date round-trip.

The bug this locks down was not actually in this handler -- write and read
both traced clean -- but nothing in the suite exercised the contract these
two fields go through: a StrEnum and a `date` on the request model, an
`exclude_none` partial-update dict, an explicit `.isoformat()` override for
birth_date, and a response model rebuilt from whatever Supabase hands back.
A future change to any one of those (dropping the isoformat override, adding
`use_enum_values`, renaming a column) would reintroduce exactly the "does not
persist" symptom this was reported as, silently. This exercises the same
`ProfileIn` / `ProfileOut` models and the same `update_profile` handler the
real endpoint runs, with a fake Supabase table standing in for the network
call -- the same style as `test_identity.py`.
"""
from __future__ import annotations

from datetime import date

from app.models.common import Sex
from app.models.profile import ProfileIn
from app.routers import profiles


class FakeResp:
    def __init__(self, data):
        self.data = data


class FakeProfilesTable:
    """Just enough of the postgrest builder to exercise update_profile."""

    def __init__(self, row: dict):
        self.row = dict(row)
        self.last_patch: dict | None = None
        self.patches: list[dict] = []          # every update(), in call order

    def update(self, patch):
        self.last_patch = patch
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        self.row.update(self.last_patch or {})
        self.patches.append(self.last_patch or {})
        return FakeResp([dict(self.row)])


class FakeClient:
    def __init__(self, table: FakeProfilesTable):
        self._table = table

    def table(self, _name):
        return self._table


class FakeUser:
    def __init__(self, uid: str, table: FakeProfilesTable):
        self.id = uid
        self.sb = FakeClient(table)


BASE_ROW = {
    "id": "u1", "handle": "gil", "display_name": "", "avatar_url": None,
    "bio": "", "sex": None, "birth_date": None, "height_cm": None,
    "weight_kg": None, "target_weight_kg": None, "activity_level": "moderate",
    "goal": "maintain", "diet_mode": "balanced", "unit_system": "imperial",
    "timezone": "UTC", "is_private": False, "onboarded_at": None,
}


async def test_sex_and_birth_date_round_trip():
    """Exactly what the "About You" step sends: sex + birth_date, nothing
    else -- the two fields Gil reported as not saving."""
    table = FakeProfilesTable(BASE_ROW)
    user = FakeUser("u1", table)
    body = ProfileIn(sex="male", birth_date="1990-03-04")

    out = await profiles.update_profile(body, user)  # type: ignore[arg-type]

    # The patch sent to the database: birth_date must be the ISO STRING the
    # column expects, never a raw datetime.date -- that override is the one
    # line in the handler that exists only for this field.
    assert table.last_patch == {"sex": Sex.male, "birth_date": "1990-03-04"}
    assert isinstance(table.last_patch["birth_date"], str)

    # And it comes back out through the response model correctly typed.
    assert out.sex == Sex.male
    assert out.birth_date == date(1990, 3, 4)


async def test_partial_update_does_not_touch_other_fields(monkeypatch):
    """A step-0-only save must not send height/weight/etc as null and wipe
    them -- exclude_none is what makes the five-step onboarding flow safe to
    call once per step.

    height_cm and weight_kg are already set here, same as a user redoing
    "About You" after finishing the rest of onboarding earlier -- which also
    means this patch completes all four required fields and trips the
    onboarded_at / recompute-targets branch. compute_targets has its own
    coverage in test_macros.py, so that branch is stubbed out here rather
    than re-tested.
    """
    table = FakeProfilesTable({**BASE_ROW, "height_cm": 180.0, "weight_kg": 80.0})
    user = FakeUser("u1", table)
    monkeypatch.setattr(profiles, "service", lambda: FakeClient(table))
    monkeypatch.setattr(profiles, "_recompute_targets", lambda *_a, **_k: {})
    body = ProfileIn(sex="female", birth_date="1985-12-25")

    await profiles.update_profile(body, user)  # type: ignore[arg-type]

    # The actual profile patch (patches[0]) -- the onboarded_at update that
    # follows it, once all four fields are present, is a second, separate
    # call and must not be confused with this one.
    assert set(table.patches[0].keys()) == {"sex", "birth_date"}
    # Untouched fields must still be there afterwards.
    assert table.row["height_cm"] == 180.0
    assert table.row["weight_kg"] == 80.0


async def test_sex_other_round_trips():
    """"Prefer not to say" maps to sex=other on the wire, not a sentinel the
    backend does not recognise."""
    table = FakeProfilesTable(BASE_ROW)
    user = FakeUser("u1", table)
    body = ProfileIn(sex="other")

    out = await profiles.update_profile(body, user)  # type: ignore[arg-type]

    assert table.last_patch == {"sex": Sex.other}
    assert out.sex == Sex.other
