"""Recipe macro computation and AI personalization."""
from __future__ import annotations

import re

import structlog

from ...db import maybe_one, rows, service
from ...models.common import Macros
from ..nutrition import resolver
from .client import ask_reasoning, record_usage
from .prompts import RECIPE_ADAPT_SYSTEM

log = structlog.get_logger()

# Volume-to-gram conversions. Density-aware where it matters, because "1 cup of
# flour" and "1 cup of honey" are 120 g and 340 g respectively.
UNIT_ML = {
    "cup": 236.6, "cups": 236.6, "c": 236.6,
    "tbsp": 14.8, "tablespoon": 14.8, "tablespoons": 14.8, "tbs": 14.8,
    "tsp": 4.9, "teaspoon": 4.9, "teaspoons": 4.9,
    "ml": 1.0, "milliliter": 1.0, "millilitre": 1.0,
    "l": 1000.0, "liter": 1000.0, "litre": 1000.0,
    "fl oz": 29.6, "floz": 29.6, "pint": 473.2, "quart": 946.4,
}
UNIT_G = {
    "g": 1.0, "gram": 1.0, "grams": 1.0, "gs": 1.0,
    "kg": 1000.0, "kilogram": 1000.0,
    "oz": 28.35, "ounce": 28.35, "ounces": 28.35,
    "lb": 453.6, "lbs": 453.6, "pound": 453.6, "pounds": 453.6,
}
# Countable items: approximate mass of one.
UNIT_EACH_G = {
    "egg": 50, "eggs": 50, "clove": 3, "cloves": 3, "onion": 150, "onions": 150,
    "tomato": 120, "tomatoes": 120, "banana": 118, "apple": 182, "potato": 173,
    "carrot": 61, "carrots": 61, "slice": 30, "slices": 30, "breast": 174,
    "fillet": 150, "can": 400, "cans": 400, "handful": 30, "pinch": 0.5,
}

_FRACTIONS = {"½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75, "⅛": 0.125}
_QTY = re.compile(r"^\s*(\d+\s+\d+/\d+|\d+/\d+|\d*\.?\d+)")


def parse_quantity(text: str) -> tuple[float | None, str | None, str]:
    """Split '2 1/2 cups jasmine rice' -> (2.5, 'cup', 'jasmine rice')."""
    s = text.strip()
    for glyph, val in _FRACTIONS.items():
        s = s.replace(glyph, f" {val} ")
    s = re.sub(r"\s+", " ", s).strip()

    qty = None
    # "a handful of spinach" and "an onion" carry a quantity of one, written
    # as an article. Without this they fall through to the 100 g default.
    if re.match(r"^(a|an)\s+", s, flags=re.I):
        qty = 1.0
        s = re.sub(r"^(a|an)\s+", "", s, flags=re.I)
    m = _QTY.match(s)
    if m:
        raw = m.group(1)
        try:
            if " " in raw:                       # mixed number "2 1/2"
                whole, frac = raw.split()
                n, d = frac.split("/")
                qty = float(whole) + float(n) / float(d)
            elif "/" in raw:
                n, d = raw.split("/")
                qty = float(n) / float(d)
            else:
                qty = float(raw)
        except (ValueError, ZeroDivisionError):
            qty = None
        s = s[m.end():].strip()

    unit = None
    lowered = s.lower()
    for candidate in sorted(list(UNIT_ML) + list(UNIT_G) + list(UNIT_EACH_G), key=len, reverse=True):
        if lowered.startswith(candidate + " ") or lowered == candidate:
            unit = candidate
            s = s[len(candidate):].strip()
            break

    name = re.sub(r"^(of|the)\s+", "", s, flags=re.I).strip(" ,.")
    return qty, unit, name or text.strip()


# Things a recipe lists without a quantity because the amount is a trace.
# Defaulting these to 100 g is not a rounding error — 100 g of salt is roughly
# 39,000 mg of sodium, which would wreck the recipe's numbers and any sodium
# warning built on them.
_TRACE_G = 2.0
_TRACE_WORDS = (
    "salt", "pepper", "seasoning", "spice", "herbs", "garnish", "zest",
    "cinnamon", "paprika", "cumin", "oregano", "thyme", "rosemary", "basil",
    "chili flake", "nutmeg", "cayenne", "vanilla", "baking powder",
    "baking soda", "food colouring", "food coloring", "to taste",
)


def to_grams(qty: float | None, unit: str | None, name: str, density_g_ml: float | None) -> float:
    if qty is None:
        lowered = name.lower()
        if any(w in lowered for w in _TRACE_WORDS):
            return _TRACE_G
        # Genuinely unquantified but not a seasoning: a conservative single
        # portion, flagged by the caller as low-confidence.
        return 100.0
    if unit in UNIT_G:
        return qty * UNIT_G[unit]
    if unit in UNIT_ML:
        from .portion import density_for

        return qty * UNIT_ML[unit] * density_for(name, density_g_ml)
    if unit in UNIT_EACH_G:
        return qty * UNIT_EACH_G[unit]
    # Bare number with no unit: treat as a count of the named item. Scan every
    # token, because "3 large eggs" and "1/2 onion, diced" both carry the
    # countable word away from position 0.
    tokens = re.findall(r"[a-z]+", name.lower())
    for tok in tokens:
        if tok in UNIT_EACH_G:
            return qty * UNIT_EACH_G[tok]
    return qty * 100.0


async def compute_recipe_macros(ingredients: list[dict], servings: int) -> tuple[list[dict], Macros]:
    """Resolve every ingredient and return (enriched ingredients, per-serving macros)."""
    parsed = []
    for ing in ingredients:
        raw = ing.get("raw_text") or ing.get("name") or ""
        qty, unit, name = parse_quantity(raw)
        parsed.append({
            **ing,
            "raw_text": raw,
            "name": ing.get("name") or name,
            "quantity": ing.get("quantity") if ing.get("quantity") is not None else qty,
            "unit": ing.get("unit") or unit,
        })

    facts = await resolver.resolve_many([p["name"] for p in parsed])

    total = Macros()
    enriched = []
    for p in parsed:
        fact = facts.get(p["name"]) or {}
        grams = p.get("grams") or to_grams(
            p.get("quantity"), p.get("unit"), p["name"],
            float(fact["density_g_ml"]) if fact.get("density_g_ml") else None,
        )
        m = resolver.macros_for(fact, grams) if fact else Macros()
        if not p.get("is_optional"):
            total = total + m
        enriched.append({
            **p,
            "grams": round(grams, 1),
            "food_fact_id": fact.get("id"),
            "kcal": round(m.kcal, 2), "protein_g": round(m.protein_g, 2),
            "carbs_g": round(m.carbs_g, 2), "fat_g": round(m.fat_g, 2),
            "fiber_g": round(m.fiber_g, 2), "sugar_g": round(m.sugar_g, 2),
        })

    per_serving = total.scaled(1.0 / max(servings, 1))
    return enriched, per_serving


async def adapt(user_id: str, recipe: dict, req, profile: dict) -> dict:
    """Rewrite a recipe for one user's constraints."""
    sb = service()
    restrictions = rows(
        sb.table("dietary_restrictions").select("*").eq("user_id", user_id).execute()
    )
    targets = maybe_one(
        sb.table("nutrition_targets").select("*").eq("user_id", user_id)
        .order("effective_from", desc=True).limit(1).execute()
    ) or {}
    ingredients = rows(
        sb.table("recipe_ingredients").select("*").eq("recipe_id", recipe["id"])
        .order("position").execute()
    )

    fasting = None
    if req.consider_fasting_window:
        fasting = maybe_one(
            sb.table("fasting_settings").select("*").eq("user_id", user_id).limit(1).execute()
        )
    workload = 0
    if req.consider_workout_load:
        workload = len(rows(
            sb.table("workouts").select("id").eq("user_id", user_id).limit(20).execute()
        ))

    allergies = [r for r in restrictions if r["kind"] in ("allergy", "intolerance")]
    constraints = []
    if req.honor_allergies and allergies:
        constraints.append(
            "ABSOLUTE ALLERGY CONSTRAINTS (must be removed entirely): "
            + ", ".join(f"{a['label']} ({a['severity']})" for a in allergies)
        )
    other = [r for r in restrictions if r["kind"] not in ("allergy", "intolerance")]
    if other:
        constraints.append("Avoid/prefer: " + ", ".join(r["label"] for r in other))
    if req.honor_diet_mode:
        constraints.append(f"Diet mode: {profile.get('diet_mode')}")
    if req.max_kcal_per_serving:
        constraints.append(f"Max {req.max_kcal_per_serving} kcal per serving")
    if req.max_carbs_g:
        constraints.append(f"Max {req.max_carbs_g} g carbs per serving")
    if req.min_protein_g:
        constraints.append(f"At least {req.min_protein_g} g protein per serving")
    if targets:
        constraints.append(
            f"Daily targets: {targets.get('target_kcal')} kcal, "
            f"{targets.get('protein_g')} g protein, {targets.get('carbs_g')} g carbs"
        )
    if fasting:
        constraints.append(
            f"Eating window opens {fasting.get('eating_window_start')} "
            f"({fasting.get('protocol')}) — the first meal after a fast should be "
            "protein-forward and gentle on digestion."
        )
    if workload >= 8:
        constraints.append("Training frequently — do not cut carbohydrate aggressively.")
    if req.extra_notes:
        constraints.append(f"User note: {req.extra_notes}")

    call = await ask_reasoning(
        pipeline="recipe_adaptation",
        system=RECIPE_ADAPT_SYSTEM,
        user_text=(
            f"ORIGINAL RECIPE: {recipe['title']}\n"
            f"Servings: {recipe['servings']} -> target {req.target_servings or recipe['servings']}\n"
            f"Cuisine: {recipe.get('cuisine')}\n"
            f"Current per serving: {recipe.get('kcal_per_serving')} kcal, "
            f"{recipe.get('protein_g_per_serving')} g protein, "
            f"{recipe.get('carbs_g_per_serving')} g carbs, "
            f"{recipe.get('fat_g_per_serving')} g fat\n\n"
            "INGREDIENTS:\n"
            + "\n".join(f"- {i['raw_text']}" for i in ingredients)
            + "\n\nSTEPS:\n"
            + "\n".join(f"{s.get('n', i + 1)}. {s.get('text', '')}"
                        for i, s in enumerate(recipe.get("steps") or []))
            + "\n\nCONSTRAINTS:\n"
            + "\n".join(f"- {c}" for c in constraints)
            + "\n\nRewrite the recipe."
        ),
        max_tokens=4000,
    )
    await record_usage(call, user_id)

    if not call.ok or not isinstance(call.payload, dict):
        return {
            "title": recipe["title"],
            "adaptation_note": "Adaptation is temporarily unavailable — this is the original recipe.",
            "substitutions": [], "ingredients": ingredients,
            "steps": recipe.get("steps") or [],
            "servings": recipe["servings"],
            "warnings": ["The AI adaptation service did not respond; nothing was changed."],
            "shopping_list": [],
        }

    p = call.payload
    servings = int(p.get("servings") or req.target_servings or recipe["servings"])
    enriched, per_serving = await compute_recipe_macros(p.get("ingredients") or [], servings)

    warnings = [str(w)[:200] for w in (p.get("warnings") or [])]
    # Verify the model actually removed the allergens it claimed to.
    if req.honor_allergies:
        blob = " ".join(i.get("raw_text", "").lower() for i in enriched)
        for a in allergies:
            if a["label"].lower() in blob:
                warnings.insert(
                    0,
                    f"'{a['label']}' still appears in the adapted ingredients — "
                    "do not cook this without checking.",
                )

    return {
        "title": str(p.get("title") or recipe["title"])[:120],
        "adaptation_note": str(p.get("adaptation_note") or "")[:1200],
        "substitutions": p.get("substitutions") or [],
        "ingredients": enriched,
        "steps": p.get("steps") or recipe.get("steps") or [],
        "servings": servings,
        "per_serving": per_serving,
        "shopping_list": p.get("shopping_list") or [],
        "warnings": warnings[:6],
    }
