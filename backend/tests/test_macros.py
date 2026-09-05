"""Macro maths must be internally consistent — a target whose macros don't add
up to its calories is the kind of bug users notice within a day."""
import pytest

from app.services.nutrition.macros import (
    ACTIVITY_MULTIPLIER, age_from, bmr_katch, bmr_mifflin, compute_targets,
    kcal_burn, water_target_ml,
)

PROFILES = [
    dict(weight_kg=95, height_cm=180, sex="male", activity_level="moderate",
         goal="lose", diet_mode="balanced", birth_date="1984-05-01"),
    dict(weight_kg=58, height_cm=163, sex="female", activity_level="active",
         goal="gain", diet_mode="high_protein", birth_date="1996-01-01"),
    dict(weight_kg=80, height_cm=175, sex="male", activity_level="very_active",
         goal="maintain", diet_mode="keto", birth_date="1990-01-01"),
    dict(weight_kg=48, height_cm=155, sex="female", activity_level="sedentary",
         goal="lose", diet_mode="low_carb", birth_date="1970-03-03"),
]


@pytest.mark.parametrize("profile", PROFILES)
def test_macros_sum_to_target(profile):
    t = compute_targets(profile)
    from_macros = t["protein_g"] * 4 + t["carbs_g"] * 4 + t["fat_g"] * 9
    # Integer rounding of three macros can drift a few kcal; more than 1% is a bug.
    assert abs(from_macros - t["target_kcal"]) < t["target_kcal"] * 0.01


@pytest.mark.parametrize("profile", PROFILES)
def test_never_below_calorie_floor(profile):
    t = compute_targets(profile)
    assert t["target_kcal"] >= 1200, "must never prescribe below the clinical floor"


def test_keto_respects_carb_cap():
    t = compute_targets(PROFILES[2])
    assert t["carbs_g"] <= 30


def test_low_carb_respects_carb_cap():
    t = compute_targets(PROFILES[3])
    assert t["carbs_g"] <= 100


def test_deficit_is_bounded():
    """A 20% deficit is the ceiling. Anything steeper costs lean mass."""
    t = compute_targets(PROFILES[0])
    assert t["target_kcal"] >= t["tdee_kcal"] * 0.79


def test_protein_scales_with_bodyweight():
    light = compute_targets({**PROFILES[0], "weight_kg": 60})
    heavy = compute_targets({**PROFILES[0], "weight_kg": 110})
    assert heavy["protein_g"] > light["protein_g"]


def test_activity_monotonic():
    prev = 0
    for level in ["sedentary", "light", "moderate", "active", "very_active", "athlete"]:
        t = compute_targets({**PROFILES[2], "activity_level": level, "goal": "maintain"})
        assert t["tdee_kcal"] > prev
        prev = t["tdee_kcal"]


def test_katch_used_when_body_fat_known():
    plain = compute_targets(PROFILES[0])
    with_bf = compute_targets(PROFILES[0], body_fat_pct=18)
    assert plain["rationale"]["bmr_method"] == "mifflin_st_jeor"
    assert with_bf["rationale"]["bmr_method"] == "katch_mcardle"


def test_mifflin_known_values():
    # 10*80 + 6.25*180 - 5*30 + 5 = 1780
    assert bmr_mifflin(80, 180, 30, "male") == pytest.approx(1780, abs=1)
    # same, minus 161 for the female constant
    assert bmr_mifflin(80, 180, 30, "female") == pytest.approx(1614, abs=1)


def test_katch_known_value():
    # lean = 80 * 0.8 = 64; 370 + 21.6*64 = 1752.4
    assert bmr_katch(80, 20) == pytest.approx(1752.4, abs=0.5)


def test_water_target_scales_and_clamps():
    assert 2000 <= water_target_ml(45, "sedentary") <= 5000
    assert water_target_ml(110, "athlete") > water_target_ml(50, "sedentary")


def test_age_handles_bad_input():
    assert age_from(None) == 30
    assert age_from("not-a-date") == 30
    assert age_from("1990-06-15") >= 30


def test_met_burn():
    # 8 METs, 80 kg, 30 min -> 8*3.5*80/200*30 = 336
    assert kcal_burn(8.0, 80, 30) == 336


# ---------------------------------------------------------------------------
# Configuration parsing
# ---------------------------------------------------------------------------
def _settings_from(text: str):
    """Build Settings from an env-file body, without pytest fixtures."""
    import tempfile
    from pathlib import Path

    from app.config import Settings

    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / ".env"
        env.write_text(text)
        return Settings(_env_file=str(env))


def test_list_settings_accept_plain_env_values():
    """`CORS_ORIGINS=*` must not crash.

    pydantic-settings treats list-typed fields as "complex" and JSON-decodes
    them straight from the env file, before any validator runs. Without the
    NoDecode annotation this raised SettingsError at import time, taking the
    whole app down — including every test.
    """
    s = _settings_from(
        "CORS_ORIGINS=*    # inline comment\n"
        "NUTRITION_PROVIDER_ORDER=usda,nutritionix\n"
        "SUPABASE_URL=https://example.supabase.co\n"
    )
    assert s.cors_origins == ["*"]
    assert s.nutrition_provider_order == ["usda", "nutritionix"]
    assert s.supabase_url == "https://example.supabase.co"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('["https://a.com","https://b.com"]', ["https://a.com", "https://b.com"]),
        ("https://a.com,https://b.com", ["https://a.com", "https://b.com"]),
        ("*", ["*"]),
        ("*   # trailing comment", ["*"]),
    ],
)
def test_list_settings_accept_json_and_csv(raw, expected):
    assert _settings_from(f"CORS_ORIGINS={raw}\n").cors_origins == expected
