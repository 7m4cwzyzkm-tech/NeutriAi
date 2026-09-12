"""Overeating detection is deterministic on purpose — a model must never be the
thing deciding whether a user gets an alert."""
import pytest

from app.models.common import Macros
from app.services.nutrition.assessment import _expected_by_now, assess_deterministic

TARGETS = dict(target_kcal=2300, protein_g=180, carbs_g=220, fat_g=75, fiber_g=32)


def assess(day, meal, slot="dinner", meals=3, activity=0):
    return assess_deterministic(
        targets=TARGETS, day_totals=day, meal=meal,
        meal_slot=slot, meals_today=meals, activity_kcal=activity,
    )


def test_on_target_is_no_alert():
    r = assess(Macros(kcal=1100, protein_g=90, carbs_g=110, fat_g=30),
               Macros(kcal=520, protein_g=42, carbs_g=40, fat_g=18))
    assert r["severity"] == "none"
    assert r["kcal_over"] == 0


@pytest.mark.parametrize(
    "kcal,expected",
    [(2400, "none"), (2500, "mild"), (2800, "moderate"), (3600, "severe")],
)
def test_severity_bands(kcal, expected):
    r = assess(Macros(kcal=kcal, protein_g=120, carbs_g=250, fat_g=90),
               Macros(kcal=600, protein_g=30, carbs_g=60, fat_g=20))
    assert r["severity"] == expected


def test_activity_earns_headroom_but_only_partly():
    day = Macros(kcal=2700, protein_g=150, carbs_g=280, fat_g=85)
    meal = Macros(kcal=600, protein_g=40, carbs_g=60, fat_g=20)
    without = assess(day, meal)
    with_activity = assess(day, meal, activity=800)
    assert with_activity["kcal_over"] < without["kcal_over"]
    # Only 60% of 800 is credited, so the target rises by 480, not 800.
    assert with_activity["_context"]["adjusted_target"] == 2300 + 480


def test_carb_load_detected():
    r = assess(Macros(kcal=2000, protein_g=80, carbs_g=250, fat_g=50),
               Macros(kcal=1200, protein_g=20, carbs_g=200, fat_g=25))
    assert r["carb_load_flag"] is True


def test_carb_load_not_flagged_for_balanced_meal():
    r = assess(Macros(kcal=2000, protein_g=140, carbs_g=180, fat_g=70),
               Macros(kcal=700, protein_g=50, carbs_g=60, fat_g=28))
    assert r["carb_load_flag"] is False


def test_oversized_meal_flagged_even_when_day_is_fine():
    r = assess(Macros(kcal=1800, protein_g=100, carbs_g=200, fat_g=60),
               Macros(kcal=1500, protein_g=40, carbs_g=180, fat_g=55))
    assert r["severity"] == "mild"
    assert r["kcal_over"] == 0
    assert "over" not in r["headline"].lower() or "Big portion" in r["headline"]


def test_never_recommends_starving():
    r = assess(Macros(kcal=4000, protein_g=150, carbs_g=450, fat_g=150),
               Macros(kcal=1500, protein_g=40, carbs_g=180, fat_g=60))
    blob = " ".join([r["headline"], r["detail"], *r["portion_advice"],
                     *r["next_meal"]["suggestions"]]).lower()
    for word in ("skip", "starve", "punish", "burn it off", "cheat", "guilty", "bad food"):
        assert word not in blob, f"assessment used shaming/harmful language: {word}"


def test_next_meal_emphasis_reflects_the_gap():
    protein_short = assess(Macros(kcal=1800, protein_g=60, carbs_g=240, fat_g=70),
                           Macros(kcal=600, protein_g=10, carbs_g=90, fat_g=20))
    assert protein_short["next_meal"]["emphasis"] == "protein"

    nearly_done = assess(Macros(kcal=2250, protein_g=175, carbs_g=215, fat_g=74),
                         Macros(kcal=400, protein_g=35, carbs_g=30, fat_g=12))
    assert nearly_done["next_meal"]["emphasis"] == "light"


def test_eating_curve_is_monotonic():
    from datetime import datetime, timezone

    prev = -1
    for hour in range(0, 24):
        v = _expected_by_now(2300, datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc))
        assert v >= prev
        prev = v
    assert _expected_by_now(2300, datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)) == 0


# --- the home screen's fasting card ------------------------------------------

def test_the_dashboard_finishes_the_fast_it_returns():
    """The rollup returns `to_jsonb(f)` -- the raw `fasts` row, which carries a
    start time and a target and nothing else. `pct`, `elapsed_minutes` and
    `phase` are computed in `lifestyle._to_out`, and the rollup does not go
    through it.

    So the home screen rendered strokeDasharray="NaN" and read "NaNh / NaNm",
    while the Fasting tab showed the same fast correctly -- one fast, two
    readings, in one app.
    """
    from datetime import datetime, timedelta, timezone

    from app.routers.lifestyle import _to_out

    started = datetime.now(timezone.utc) - timedelta(minutes=200)
    raw = {"id": "f1", "protocol": "16:8", "status": "active",
           "started_at": started.isoformat(), "target_minutes": 960,
           "actual_minutes": None, "ended_at": None}

    # what the rollup hands over
    for missing in ("pct", "elapsed_minutes", "phase"):
        assert missing not in raw

    out = _to_out(raw).model_dump(mode="json")
    assert out["elapsed_minutes"] == pytest.approx(200, abs=1)
    assert 0 < out["pct"] < 100
    assert out["phase"]
    # the two values the ring and the label divide by
    assert isinstance(out["elapsed_minutes"], int)
    assert out["elapsed_minutes"] // 60 == 3


def test_the_dashboard_route_actually_does_it():
    """Behavioural, not textual. The first version of this test checked that
    the string "_to_out" appeared in the route's source -- and deleting the
    line that used it left the import behind, so the check passed on a
    mutation that broke the card."""
    from datetime import datetime, timedelta, timezone

    from app.routers.profiles import finish_fast

    started = datetime.now(timezone.utc) - timedelta(minutes=200)
    payload = {"active_fast": {"id": "f1", "protocol": "16:8", "status": "active",
                               "started_at": started.isoformat(),
                               "target_minutes": 960, "actual_minutes": None,
                               "ended_at": None}}
    out = finish_fast(payload)["active_fast"]
    for computed in ("pct", "elapsed_minutes", "phase"):
        assert computed in out, f"{computed} is still missing from the dashboard"
    assert out["elapsed_minutes"] == pytest.approx(200, abs=1)


def test_a_broken_fast_costs_the_card_not_the_screen():
    """A home screen missing one card beats a home screen that 500s."""
    from app.routers.profiles import finish_fast

    junk = finish_fast({"active_fast": {"id": "x"}, "meals": [1, 2]})
    assert junk["active_fast"] is None
    assert junk["meals"] == [1, 2], "the rest of the dashboard was lost too"

    # and a dashboard with no fast at all is untouched
    assert finish_fast({"meals": []}) == {"meals": []}
    assert finish_fast(None) is None
