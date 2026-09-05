"""Overeating detection and next-meal correction.

Two layers, deliberately:

1. A **deterministic** layer that decides severity from arithmetic. This is what
   the app gates on, because a model should never be the thing that decides
   whether a user sees an alert.
2. An **AI** layer that writes the human-facing wording, using the deterministic
   verdict as a constraint it may not contradict.

If the model call fails, layer 1 still produces a complete, useful assessment.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone

import structlog

from ...models.common import Macros
from ..ai.client import ask_reasoning, record_usage
from ..ai.prompts import INTAKE_SYSTEM

log = structlog.get_logger()

# Share of the daily calorie budget a single meal should occupy.
SLOT_SHARE = {
    "breakfast": 0.25, "lunch": 0.33, "dinner": 0.35,
    "snack": 0.12, "pre_workout": 0.15, "post_workout": 0.20,
}

# Severity thresholds, as a fraction over the *day's* target.
SEVERITY_BANDS = [
    (0.50, "severe"),
    (0.20, "moderate"),
    (0.07, "mild"),
]


def _expected_by_now(target_kcal: int, now: datetime, tz_offset_min: int = 0) -> float:
    """How many calories a person on plan would reasonably have eaten by now.

    A flat linear curve punishes anyone who eats late, so we use an eating-day
    curve: nothing before 06:00, most intake between 12:00 and 20:00.
    """
    local_minutes = (now.hour * 60 + now.minute + tz_offset_min) % 1440
    if local_minutes < 6 * 60:
        frac = 0.0
    elif local_minutes < 12 * 60:
        frac = 0.25 * (local_minutes - 6 * 60) / (6 * 60)
    elif local_minutes < 20 * 60:
        frac = 0.25 + 0.62 * (local_minutes - 12 * 60) / (8 * 60)
    else:
        frac = 0.87 + 0.13 * min(1.0, (local_minutes - 20 * 60) / (3 * 60))
    return target_kcal * frac


def assess_deterministic(
    *,
    targets: dict,
    day_totals: Macros,
    meal: Macros,
    meal_slot: str,
    meals_today: int,
    now: datetime | None = None,
    tz_offset_min: int = 0,
    activity_kcal: int = 0,
) -> dict:
    """Pure arithmetic verdict. No model, no network, fully testable."""
    now = now or datetime.now(timezone.utc)
    target = int(targets.get("target_kcal") or 2000)
    # Exercise genuinely earns some headroom, but we only credit 60% of it —
    # wearables systematically overestimate active burn.
    adjusted_target = target + int(activity_kcal * 0.6)

    total = day_totals.kcal
    over = total - adjusted_target
    pct_of_target = (total / adjusted_target * 100) if adjusted_target else 0.0

    severity = "none"
    for threshold, label in SEVERITY_BANDS:
        if over > adjusted_target * threshold:
            severity = label
            break

    # A single meal blowing past its slot share is worth flagging even when the
    # day as a whole is still fine — that's the "portion" signal, not the
    # "total" signal.
    slot_budget = adjusted_target * SLOT_SHARE.get(meal_slot, 0.20)
    meal_over_slot = meal.kcal > slot_budget * 1.6
    if severity == "none" and meal_over_slot:
        severity = "mild"

    # Carb load: this meal alone is more than half the day's carb budget and is
    # mostly carbohydrate.
    carb_target = float(targets.get("carbs_g") or 200)
    meal_carb_kcal_share = (meal.carbs_g * 4) / max(meal.kcal, 1)
    carb_load = meal.carbs_g > carb_target * 0.5 and meal_carb_kcal_share > 0.55

    pace = _expected_by_now(adjusted_target, now, tz_offset_min)
    ahead_of_pace = total > pace * 1.15 and pace > 0

    remaining = max(0.0, adjusted_target - total)
    protein_left = max(0.0, float(targets.get("protein_g") or 150) - day_totals.protein_g)
    carbs_left = carb_target - day_totals.carbs_g
    fat_left = float(targets.get("fat_g") or 70) - day_totals.fat_g
    fiber_left = max(0.0, float(targets.get("fiber_g") or 30) - day_totals.fiber_g)

    advice: list[str] = []
    if meal_over_slot:
        shrink = int((1 - slot_budget / max(meal.kcal, 1)) * 100)
        advice.append(
            f"This {meal_slot.replace('_', ' ')} ran about {shrink}% over its usual share — "
            f"a smaller starch portion would bring it in line."
        )
    if carb_load:
        advice.append(
            f"Carbs here ({meal.carbs_g:.0f} g) were over half today's budget; "
            f"pairing them with protein next time evens out the curve."
        )
    if protein_left > 40 and remaining < protein_left * 4:
        advice.append(
            f"Protein is still {protein_left:.0f} g short with only {remaining:.0f} kcal left — "
            "lean sources will stretch that further."
        )
    if fiber_left > 15:
        advice.append(f"Fibre is {fiber_left:.0f} g short; vegetables or legumes close that gap cheaply.")

    emphasis = "balanced"
    if protein_left > 30:
        emphasis = "protein"
    elif fiber_left > 15:
        emphasis = "fibre"
    if remaining < adjusted_target * 0.15:
        emphasis = "light"

    if severity == "none":
        if ahead_of_pace:
            headline = f"Logged. You're a little ahead of pace with {remaining:.0f} kcal left."
        else:
            headline = f"Nicely on track — {remaining:.0f} kcal and {protein_left:.0f} g protein to go."
        detail = (
            f"You're at {total:.0f} of {adjusted_target} kcal "
            f"({pct_of_target:.0f}% of today's target) across {meals_today} meals."
        )
    elif over <= 0:
        # Flagged on portion size alone: the day is still within budget, so the
        # message is about this plate, not about the day.
        headline = (
            f"Big portion for one {meal_slot.replace('_', ' ')} — "
            f"{remaining:.0f} kcal still left today."
        )
        detail = (
            f"This meal was {meal.kcal:.0f} kcal against a usual "
            f"{slot_budget:.0f} kcal for a {meal_slot.replace('_', ' ')}. "
            f"The day as a whole is still on target at {total:.0f} of {adjusted_target} kcal."
        )
    else:
        headline = f"You're {over:.0f} kcal over today's target."
        detail = (
            f"Today totals {total:.0f} kcal against a {adjusted_target} kcal target "
            f"({pct_of_target:.0f}%). One day over doesn't undo a week of consistency — "
            f"tomorrow starts clean."
        )

    return {
        "severity": severity,
        "kcal_over": round(max(0.0, over), 1),
        "pct_of_target": round(pct_of_target, 1),
        "carb_load_flag": carb_load,
        "meal_frequency": meals_today,
        "headline": headline[:120],
        "detail": detail,
        "portion_advice": advice[:3],
        "macro_corrections": {
            "protein_g": round(protein_left, 1),
            "carbs_g": round(carbs_left, 1),
            "fat_g": round(fat_left, 1),
            "fiber_g": round(fiber_left, 1),
        },
        "next_meal": {
            "target_kcal": int(remaining),
            "emphasis": emphasis,
            "suggestions": _fallback_suggestions(emphasis, int(remaining)),
        },
        "_context": {
            "adjusted_target": adjusted_target,
            "activity_credited_kcal": int(activity_kcal * 0.6),
            "ahead_of_pace": ahead_of_pace,
            "expected_by_now": int(pace),
            "slot_budget": int(slot_budget),
        },
    }


def _fallback_suggestions(emphasis: str, kcal: int) -> list[str]:
    if kcal < 150:
        return ["Herbal tea or sparkling water if you want something in hand.",
                "A small piece of fruit if you're genuinely hungry."]
    table = {
        "protein": [
            f"Greek yoghurt with berries (~{min(kcal, 250)} kcal, 20 g protein)",
            f"Grilled chicken over greens (~{min(kcal, 400)} kcal, 40 g protein)",
            "Cottage cheese with tomato and black pepper",
        ],
        "fibre": [
            "Lentil soup with a slice of rye",
            "Roasted vegetables with chickpeas and tahini",
            "Overnight oats with chia and raspberries",
        ],
        "light": [
            "Broth-based soup",
            "Cucumber, tomato and feta salad",
            "A boiled egg and an apple",
        ],
        "vegetables": [
            "Big mixed salad with olive oil and seeds",
            "Stir-fried greens with garlic and tofu",
        ],
        "balanced": [
            f"Salmon, quinoa and broccoli (~{min(kcal, 550)} kcal)",
            f"Turkey and avocado wrap (~{min(kcal, 480)} kcal)",
            "Rice bowl with beans, egg and salsa",
        ],
    }
    return table.get(emphasis, table["balanced"])[:3]


async def assess(
    *,
    user_id: str,
    profile: dict,
    targets: dict,
    day_totals: Macros,
    meal: Macros,
    meal_slot: str,
    meals_today: int,
    activity_kcal: int = 0,
    use_ai: bool = True,
) -> dict:
    """Deterministic verdict, optionally reworded by the model."""
    base = assess_deterministic(
        targets=targets,
        day_totals=day_totals,
        meal=meal,
        meal_slot=meal_slot,
        meals_today=meals_today,
        activity_kcal=activity_kcal,
    )
    if not use_ai:
        base.pop("_context", None)
        return base

    ctx = base.pop("_context", {})
    call = await ask_reasoning(
        pipeline="intake_assessment",
        system=INTAKE_SYSTEM,
        user_text=(
            f"User goal: {profile.get('goal')}, diet mode: {profile.get('diet_mode')}, "
            f"activity: {profile.get('activity_level')}.\n"
            f"Daily targets: {targets}\n"
            f"Eaten so far today: {day_totals.model_dump()}\n"
            f"This meal ({meal_slot}): {meal.model_dump()}\n"
            f"Meals logged today: {meals_today}. Activity credited: "
            f"{ctx.get('activity_credited_kcal', 0)} kcal.\n"
            f"Deterministic verdict you MUST NOT contradict: severity="
            f"{base['severity']}, kcal_over={base['kcal_over']}, "
            f"carb_load={base['carb_load_flag']}, remaining="
            f"{base['next_meal']['target_kcal']} kcal.\n"
            "Write the user-facing version."
        ),
        max_tokens=900,
    )
    await record_usage(call, user_id)

    if call.ok and isinstance(call.payload, dict):
        p = call.payload
        # The model may reword, never re-rate.
        base["headline"] = str(p.get("headline") or base["headline"])[:120]
        base["detail"] = str(p.get("detail") or base["detail"])[:600]
        if isinstance(p.get("portion_advice"), list) and p["portion_advice"]:
            base["portion_advice"] = [str(x)[:200] for x in p["portion_advice"]][:3]
        nm = p.get("next_meal") or {}
        if isinstance(nm.get("suggestions"), list) and nm["suggestions"]:
            base["next_meal"]["suggestions"] = [str(x)[:160] for x in nm["suggestions"]][:3]
        if nm.get("emphasis"):
            base["next_meal"]["emphasis"] = str(nm["emphasis"])[:24]
    return base
