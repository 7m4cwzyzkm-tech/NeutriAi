"""Programme generation must never prescribe equipment the user lacks."""
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
