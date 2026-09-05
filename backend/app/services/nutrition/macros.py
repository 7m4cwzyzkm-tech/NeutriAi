"""Energy expenditure and macro target maths.

We use Mifflin-St Jeor for BMR because it outperforms Harris-Benedict on modern
populations and needs no body-fat input (which most users do not know). When a
user *does* record body fat, we switch to Katch-McArdle, which is more accurate
because it works from lean mass.
"""
from __future__ import annotations

from datetime import date

ACTIVITY_MULTIPLIER = {
    "sedentary": 1.20,     # desk job, no training
    "light": 1.375,        # 1-3 sessions/week
    "moderate": 1.55,      # 3-5 sessions/week
    "active": 1.725,       # 6-7 sessions/week
    "very_active": 1.90,   # physical job + training
    "athlete": 2.05,       # two-a-days
}

# Fraction of TDEE added or removed for each goal. A 20% deficit is the usual
# ceiling before lean-mass loss and adherence both fall off a cliff.
GOAL_DELTA = {
    "lose": -0.20,
    "maintain": 0.0,
    "gain": 0.12,
    "recomp": -0.08,
}

# g of protein per kg of bodyweight. Higher in a deficit (protects lean mass)
# and for athletes.
PROTEIN_G_PER_KG = {
    "lose": 2.2, "maintain": 1.8, "gain": 2.0, "recomp": 2.4,
}

# Fraction of calories from fat. Hormonal floor is ~0.5 g/kg, so we never go
# below 20% regardless of what the diet mode asks for.
DIET_PROFILE = {
    #                  fat_pct  carb_note
    "balanced":        (0.30, None),
    "low_carb":        (0.45, 100),      # cap carbs at 100 g
    "keto":            (0.72, 30),       # cap carbs at 30 g
    "high_protein":    (0.25, None),
    "athlete":         (0.25, None),
    "vegetarian":      (0.30, None),
    "vegan":           (0.28, None),
    "pescatarian":     (0.30, None),
    "paleo":           (0.40, 130),
    "mediterranean":   (0.35, None),
}

# Absolute floors. We do not prescribe below these, ever.
MIN_KCAL = {"male": 1500, "female": 1200, "other": 1300}


def age_from(birth_date: date | str | None) -> int:
    if not birth_date:
        return 30
    if isinstance(birth_date, str):
        try:
            birth_date = date.fromisoformat(birth_date[:10])
        except ValueError:
            return 30
    today = date.today()
    return max(
        14,
        today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day)),
    )


def bmr_mifflin(weight_kg: float, height_cm: float, age: int, sex: str) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    if sex == "male":
        return base + 5
    if sex == "female":
        return base - 161
    return base - 78  # midpoint for unspecified


def bmr_katch(weight_kg: float, body_fat_pct: float) -> float:
    lean = weight_kg * (1 - body_fat_pct / 100.0)
    return 370 + 21.6 * lean


def water_target_ml(weight_kg: float, activity: str) -> int:
    """1 US gallon is the product's headline goal, but we scale it for body size
    so a 50 kg user is not told to drink four litres."""
    base = weight_kg * 35.0            # 35 mL/kg, standard clinical guidance
    if activity in ("active", "very_active", "athlete"):
        base += 500
    return int(max(2000, min(5000, round(base / 50) * 50)))


def compute_targets(profile: dict, body_fat_pct: float | None = None) -> dict:
    """Turn a profile row into a full ``nutrition_targets`` payload."""
    weight = float(profile.get("weight_kg") or 75)
    height = float(profile.get("height_cm") or 172)
    sex = profile.get("sex") or "other"
    activity = profile.get("activity_level") or "moderate"
    goal = profile.get("goal") or "maintain"
    diet = profile.get("diet_mode") or "balanced"
    age = age_from(profile.get("birth_date"))

    if body_fat_pct and 3 <= body_fat_pct <= 60:
        bmr = bmr_katch(weight, body_fat_pct)
        bmr_method = "katch_mcardle"
    else:
        bmr = bmr_mifflin(weight, height, age, sex)
        bmr_method = "mifflin_st_jeor"

    tdee = bmr * ACTIVITY_MULTIPLIER.get(activity, 1.55)
    target = tdee * (1 + GOAL_DELTA.get(goal, 0.0))

    floor = MIN_KCAL.get(sex, 1300)
    floored = False
    if target < floor:
        target, floored = float(floor), True

    # Protein first (it is the one macro with a hard requirement), then fat to a
    # hormonal-safe percentage, then carbs take whatever is left.
    protein_g = weight * PROTEIN_G_PER_KG.get(goal, 1.8)
    if diet in ("athlete", "high_protein"):
        protein_g = max(protein_g, weight * 2.2)
    protein_g = min(protein_g, target * 0.40 / 4)   # never more than 40% of energy

    fat_pct, carb_cap = DIET_PROFILE.get(diet, (0.30, None))
    fat_g = max(weight * 0.5, target * fat_pct / 9)

    carbs_kcal = target - (protein_g * 4 + fat_g * 9)
    carbs_g = max(0.0, carbs_kcal / 4)

    if carb_cap is not None and carbs_g > carb_cap:
        # Push the surplus energy into fat, which is what a low-carb plan does.
        surplus_kcal = (carbs_g - carb_cap) * 4
        carbs_g = float(carb_cap)
        fat_g += surplus_kcal / 9

    fiber_g = max(25, min(45, round(target / 1000 * 14)))   # 14 g per 1000 kcal
    sugar_max = int(round(target * 0.10 / 4))               # WHO free-sugar guidance

    return {
        "bmr_kcal": int(round(bmr)),
        "tdee_kcal": int(round(tdee)),
        "target_kcal": int(round(target)),
        "protein_g": int(round(protein_g)),
        "carbs_g": int(round(carbs_g)),
        "fat_g": int(round(fat_g)),
        "fiber_g": int(fiber_g),
        "sugar_g_max": sugar_max,
        "water_ml": water_target_ml(weight, activity),
        "rationale": {
            "bmr_method": bmr_method,
            "age_used": age,
            "activity_multiplier": ACTIVITY_MULTIPLIER.get(activity, 1.55),
            "goal_delta_pct": round(GOAL_DELTA.get(goal, 0.0) * 100, 1),
            "protein_g_per_kg": round(protein_g / weight, 2),
            "fat_pct_of_kcal": round(fat_g * 9 / max(target, 1) * 100, 1),
            "carb_cap_applied": carb_cap,
            "calorie_floor_applied": floored,
            "note": (
                "Targets recompute whenever weight, activity, goal or diet mode change."
            ),
        },
    }


def kcal_burn(met: float, weight_kg: float, minutes: float) -> int:
    """MET-based expenditure: kcal = MET x 3.5 x kg / 200 x minutes."""
    return int(round(met * 3.5 * weight_kg / 200.0 * minutes))
