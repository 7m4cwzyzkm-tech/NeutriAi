"""Programme generation must never prescribe equipment the user lacks."""
from types import SimpleNamespace

from app.services.ai import coach
from app.services.ai.coach import VALID_EQUIPMENT, _validate, epley_1rm, hr_zones

LIB = [
    {"slug": "push-up", "name": "Push-Up", "primary_muscle": "chest",
     "difficulty": 2, "regression_slug": "incline-push-up"},
    {"slug": "incline-push-up", "name": "Incline Push-Up", "primary_muscle": "chest",
     "difficulty": 1, "regression_slug": None},
    {"slug": "bodyweight-squat", "name": "Bodyweight Squat", "primary_muscle": "quads",
     "difficulty": 1, "regression_slug": None},
]


def test_unavailable_exercise_is_substituted_or_dropped():
    plan = {"days": [{"blocks": [
        {"slug": "bench-press", "name": "Barbell Bench Press", "sets": 3, "reps": "5"},
        {"slug": "push-up", "name": "Push-Up", "sets": 3, "reps": "10"},
    ]}]}
    fixed, warnings = _validate(plan, LIB)
    slugs = [b["slug"] for b in fixed["days"][0]["blocks"]]
    assert "bench-press" not in slugs
    assert "push-up" in slugs
    assert warnings


def test_valid_plan_passes_through_untouched():
    plan = {"days": [{"blocks": [{"slug": "push-up", "name": "Push-Up", "sets": 3, "reps": "10"}]}]}
    fixed, warnings = _validate(plan, LIB)
    assert not warnings
    assert fixed["days"][0]["blocks"][0]["slug"] == "push-up"


def test_epley_matches_known_values():
    # 100 kg x 5 -> 100 * (1 + 5/30) = 116.67
    assert round(epley_1rm(100, 5), 1) == 116.7
    assert epley_1rm(100, 1) == 100.0


def test_epley_caps_high_rep_sets():
    """Beyond ~12 reps the formula overestimates badly, so we clamp."""
    assert epley_1rm(60, 30) == epley_1rm(60, 12)


def test_hr_zones_sum_to_duration():
    z = hr_zones(avg_hr=150, max_hr=190, age=35, duration_s=3600)
    assert sum(z.values()) == 3600


def test_hr_zones_empty_without_data():
    assert hr_zones(None, None, 30, 3600) == {}


def test_equipment_vocabulary_is_closed():
    assert "dumbbell" in VALID_EQUIPMENT
    assert "none" in VALID_EQUIPMENT
    assert "hoverboard" not in VALID_EQUIPMENT


# --- Previous block -> new block -------------------------------------------
OLD_PLAN = {
    "id": "p1", "name": "Garage Strength", "weeks": 3,
    "progression": {"model": "double_progression", "rule": "Add reps to 12, then 2.5 kg."},
}
OLD_FINAL_WEEK = [
    {"week_index": 3, "day_index": 2, "title": "Lower", "blocks": [
        {"slug": "goblet-squat", "name": "Goblet Squat", "sets": 4, "reps": "10-12",
         "load_hint": "24 kg"},
    ]},
    {"week_index": 3, "day_index": 1, "title": "Upper", "blocks": [
        {"slug": "push-up", "name": "Push-Up", "sets": 4, "reps": "12"},
    ]},
    {"week_index": 3, "day_index": 3, "title": "Rest", "blocks": []},
]


def test_previous_block_summary_carries_the_final_weeks_prescription():
    text = coach.previous_block_summary(OLD_PLAN, OLD_FINAL_WEEK)
    assert "Garage Strength (3 weeks)" in text
    assert "Add reps to 12, then 2.5 kg. (model: double_progression)" in text
    assert "final week (week 3)" in text
    # Day order, sets x reps and load hint all survive; empty rest days do not.
    assert text.index("- Upper: Push-Up 4x12") < text.index("- Lower: Goblet Squat 4x10-12 @ 24 kg")
    assert "Rest:" not in text


def test_no_previous_plan_means_no_summary():
    assert coach.previous_block_summary(None, []) is None


def test_previous_block_summary_is_bounded():
    huge = [{"day_index": 1, "title": "X", "blocks": [
        {"name": "Move " * 50, "sets": 3, "reps": "10"}] * 100}]
    assert len(coach.previous_block_summary(OLD_PLAN, huge)) <= coach.PREVIOUS_BLOCK_MAX_CHARS


async def _prompt_for(monkeypatch, previous_block):
    seen = {}

    async def fake_ask(**kw):
        seen.update(kw)
        return SimpleNamespace(ok=False, payload=None, error="stubbed")

    async def fake_usage(*_a, **_k):
        return None

    monkeypatch.setattr(coach, "ask_reasoning", fake_ask)
    monkeypatch.setattr(coach, "record_usage", fake_usage)
    monkeypatch.setattr(
        coach, "library_for", lambda _eq: [{**e, "kind": "calisthenics"} for e in LIB],
    )
    req = SimpleNamespace(goal="build_muscle", experience="advanced", days_per_week=3,
                          weeks=3, session_minutes=45, limitations=[])
    await coach.generate_plan("u1", req, ["none"], {}, [], previous_block=previous_block)
    return seen["user_text"]


async def test_generate_plan_puts_the_previous_block_in_the_prompt(monkeypatch):
    summary = coach.previous_block_summary(OLD_PLAN, OLD_FINAL_WEEK)
    text = await _prompt_for(monkeypatch, summary)
    assert "Previous block (the plan this one replaces" in text
    assert summary in text
    assert "Weeks: 3" in text


async def test_generate_plan_without_a_previous_block_is_unchanged(monkeypatch):
    text = await _prompt_for(monkeypatch, None)
    assert "Previous block" not in text
    assert "Exercise library (use these exact slugs only):" in text
