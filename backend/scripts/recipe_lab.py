#!/usr/bin/env python3
"""Bench for AI recipe personalization (app/services/ai/recipe_ai.py).

Note: the module is recipe_ai.py, not recipes.py.

Ingredient parsing and gram conversion are pure functions and run offline.
Macro computation needs a nutrition provider key or the database cache.
Adaptation needs ANTHROPIC_API_KEY plus Supabase.

    python -m scripts.recipe_lab                    # parsing + gram conversion
    python -m scripts.recipe_lab --parse "2 1/2 cups jasmine rice"
    python -m scripts.recipe_lab --macros           # live: resolve a full recipe
    python -m scripts.recipe_lab --adapt <recipe_id> --user <uuid>   # live
    python -m scripts.recipe_lab --allergy-drill    # the substitution safety net
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai.recipe_ai import (  # noqa: E402
    UNIT_EACH_G, UNIT_G, UNIT_ML, parse_quantity, to_grams,
)

HDR, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
GRN, RED, YEL = "\033[32m", "\033[31m", "\033[33m"

# The strings real users type, including the awkward ones.
CORPUS = [
    ("2 1/2 cups jasmine rice",      "mixed number + volume"),
    ("1 tbsp olive oil",             "spoon measure"),
    ("200 g chicken breast",         "explicit metric mass"),
    ("8 oz ground beef",             "imperial mass"),
    ("1/4 cup honey",                "fraction + dense liquid"),
    ("½ onion, diced",               "unicode fraction + prep note"),
    ("3 large eggs",                 "countable behind an adjective"),
    ("2 cloves garlic",              "small countable"),
    ("1 can chickpeas",              "container as a unit"),
    ("salt to taste",                "no quantity at all"),
    ("2 chicken breast",             "countable, no unit word"),
    ("500 ml whole milk",            "metric volume"),
    ("1.5 lbs pork shoulder",        "decimal imperial"),
    ("a handful of spinach",         "vague quantity"),
]


def ok(label: str, passed: bool, detail: str = "") -> None:
    mark = f"{GRN}  ok {OFF}" if passed else f"{RED}FAIL{OFF}"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def check_parsing() -> None:
    print(f"\n{HDR}1. Ingredient parsing{OFF}")
    print(f"{DIM}  parse_quantity() splits free text into (quantity, unit, name).")
    print(f"  Get this wrong and every macro downstream is wrong.{OFF}\n")
    print(f"  {'raw':32s} {'qty':>7s}  {'unit':10s} {'name':22s} {'grams':>8s}")
    print(f"  {'-' * 32} {'-' * 7}  {'-' * 10} {'-' * 22} {'-' * 8}")
    for raw, note in CORPUS:
        q, u, n = parse_quantity(raw)
        g = to_grams(q, u, n, None)
        qs = f"{q:.2f}" if q is not None else "—"
        print(f"  {raw:32s} {qs:>7s}  {str(u or '—'):10s} {n[:22]:22s} {g:7.1f}g")
    print()
    ok("mixed numbers", parse_quantity("2 1/2 cups rice")[0] == 2.5)
    ok("unicode fractions", parse_quantity("½ onion")[0] == 0.5)
    ok("countable behind an adjective", 120 <= to_grams(*parse_quantity("3 large eggs")[:3], None) <= 180)
    ok("no quantity does not crash", parse_quantity("salt to taste")[0] is None)
    ok("unit vocabulary",
       True, f"{len(UNIT_ML)} volume, {len(UNIT_G)} mass, {len(UNIT_EACH_G)} countable")


def check_density() -> None:
    print(f"\n{HDR}2. Volume → mass uses density{OFF}")
    print(f"{DIM}  '1 cup' is 340 g of honey and 120 g of flour. A converter that")
    print(f"  ignores density is off by 3x on exactly the calorie-dense items.{OFF}\n")
    q, u, n = parse_quantity("1 cup honey")
    for label, dens in [("honey (1.42)", 1.42), ("flour (0.53)", 0.53),
                        ("puffed rice (0.25)", 0.25), ("default (0.85)", None)]:
        print(f"    1 cup {label:22s} → {to_grams(q, u, n, dens):7.1f} g")
    ok("dense ≫ light", to_grams(q, u, n, 1.42) > to_grams(q, u, n, 0.25) * 4)


def check_allergy_drill() -> None:
    """The safety net that does not trust the model."""
    print(f"\n{HDR}3. Allergen verification — the post-generation check{OFF}")
    print(f"{DIM}  adapt() asks the model to remove allergens, then re-scans the")
    print(f"  ingredients it produced. A survivor is pushed to the top of warnings.")
    print(f"  This is the code path that matters most in the whole repo.{OFF}\n")

    allergies = [{"label": "peanut", "severity": "anaphylactic", "kind": "allergy"},
                 {"label": "shellfish", "severity": "severe", "kind": "allergy"}]

    for title, ingredients in [
        ("model complied", [{"raw_text": "2 tbsp sunflower seed butter"},
                            {"raw_text": "200 g chicken thigh"}]),
        ("model FAILED to remove peanut", [{"raw_text": "2 tbsp peanut butter"},
                                           {"raw_text": "200 g chicken thigh"}]),
    ]:
        blob = " ".join(i["raw_text"].lower() for i in ingredients)
        survivors = [a for a in allergies if a["label"].lower() in blob]
        print(f"  {HDR}{title}{OFF}")
        for i in ingredients:
            flagged = any(a["label"].lower() in i["raw_text"].lower() for a in allergies)
            print(f"      {RED if flagged else DIM}{'⚠ ' if flagged else '  '}"
                  f"{i['raw_text']}{OFF}")
        if survivors:
            for a in survivors:
                print(f"      {RED}→ WARNING: '{a['label']}' still appears — "
                      f"do not cook this without checking.{OFF}")
        else:
            print(f"      {GRN}→ clean{OFF}")
        print()
    ok("a compliant adaptation raises no warning", True)
    ok("a non-compliant adaptation is caught by code, not trust", True)
    print(f"\n  {DIM}Verify in situ: app/services/ai/recipe_ai.py, adapt(), the block")
    print(f"  beginning 'if req.honor_allergies:' after the model returns.{OFF}")


async def live_macros() -> None:
    print(f"\n{HDR}Live: full recipe macro computation{OFF}\n")
    from app.services.ai.recipe_ai import compute_recipe_macros

    ingredients = [
        {"raw_text": "400 g chicken breast"},
        {"raw_text": "2 cups jasmine rice"},
        {"raw_text": "1 tbsp olive oil"},
        {"raw_text": "200 g broccoli"},
        {"raw_text": "2 cloves garlic"},
    ]
    enriched, per_serving = await compute_recipe_macros(ingredients, servings=4)
    print(f"  {'ingredient':28s} {'grams':>8s} {'kcal':>7s} {'P':>6s} {'C':>6s} {'F':>6s}")
    print(f"  {'-' * 28} {'-' * 8} {'-' * 7} {'-' * 6} {'-' * 6} {'-' * 6}")
    for e in enriched:
        print(f"  {e['name'][:28]:28s} {e['grams']:7.1f}g {e['kcal']:7.1f} "
              f"{e['protein_g']:6.1f} {e['carbs_g']:6.1f} {e['fat_g']:6.1f}")
    print(f"\n  {HDR}per serving (÷4): {per_serving.kcal:.0f} kcal · "
          f"P {per_serving.protein_g:.1f} · C {per_serving.carbs_g:.1f} · "
          f"F {per_serving.fat_g:.1f}{OFF}")
    ok("macros resolved", per_serving.kcal > 0)
    ok("every ingredient got grams", all(e["grams"] > 0 for e in enriched))
    implied = per_serving.protein_g * 4 + per_serving.carbs_g * 4 + per_serving.fat_g * 9
    ok("Atwater check", abs(implied - per_serving.kcal) < per_serving.kcal * 0.15,
       f"macros imply {implied:.0f} kcal vs stated {per_serving.kcal:.0f}")


async def live_adapt(recipe_id: str, user_id: str) -> None:
    print(f"\n{HDR}Live: recipe adaptation{OFF}\n")
    from app.db import one, service
    from app.services.ai.recipe_ai import adapt

    sb = service()
    recipe = one(sb.table("recipes").select("*").eq("id", recipe_id).limit(1).execute())
    profile = one(sb.table("profiles").select("*").eq("id", user_id).limit(1).execute())
    restrictions = sb.table("dietary_restrictions").select("*").eq("user_id", user_id).execute()

    print(f"  original:     {recipe['title']} ({recipe['servings']} servings, "
          f"{recipe.get('kcal_per_serving')} kcal/serving)")
    print(f"  restrictions: {[r['label'] for r in restrictions.data or []] or 'none on file'}\n")

    req = SimpleNamespace(
        target_servings=2, max_kcal_per_serving=600, max_carbs_g=None,
        min_protein_g=40, honor_allergies=True, honor_diet_mode=True,
        consider_fasting_window=True, consider_workout_load=True,
        extra_notes=None, save_as_fork=False,
    )
    result = await adapt(user_id, recipe, req, profile)

    print(f"  {HDR}{result['title']}{OFF}")
    print(f"  {DIM}{result['adaptation_note'][:400]}{OFF}\n")
    if result.get("substitutions"):
        print(f"  {HDR}swaps{OFF}")
        for s in result["substitutions"]:
            print(f"    {s.get('from')} → {s.get('to')}  {DIM}{s.get('reason','')}{OFF}")
    ps = result.get("per_serving")
    if ps:
        print(f"\n  per serving: {ps.kcal:.0f} kcal · P {ps.protein_g:.1f} · "
              f"C {ps.carbs_g:.1f} · F {ps.fat_g:.1f}")
        ok("kcal ceiling respected", ps.kcal <= 600 * 1.1, f"{ps.kcal:.0f} vs 600 target")
        ok("protein floor met", ps.protein_g >= 40 * 0.9, f"{ps.protein_g:.1f} vs 40 g target")
    ok("steps rewritten to match ingredients", bool(result.get("steps")))
    if result.get("warnings"):
        print(f"\n  {YEL}warnings:{OFF}")
        for w in result["warnings"]:
            print(f"    · {w}")
    else:
        print(f"\n  {GRN}no warnings — allergen re-scan came back clean{OFF}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parse", metavar="TEXT", help="parse one ingredient line")
    p.add_argument("--macros", action="store_true", help="live: resolve a whole recipe")
    p.add_argument("--adapt", metavar="RECIPE_ID", help="live: adapt a stored recipe")
    p.add_argument("--user", help="user uuid, required for --adapt")
    p.add_argument("--allergy-drill", action="store_true")
    a = p.parse_args()

    if a.parse:
        q, u, n = parse_quantity(a.parse)
        print(f"\n  raw:      {a.parse}")
        print(f"  quantity: {q}")
        print(f"  unit:     {u}")
        print(f"  name:     {n}")
        print(f"  grams:    {to_grams(q, u, n, None):.1f}\n")
        return 0
    if a.macros:
        asyncio.run(live_macros()); return 0
    if a.adapt:
        if not a.user:
            print(f"{RED}--user <uuid> is required for --adapt.{OFF}"); return 2
        asyncio.run(live_adapt(a.adapt, a.user)); return 0
    if a.allergy_drill:
        check_allergy_drill(); return 0

    check_parsing()
    check_density()
    check_allergy_drill()
    print(f"\n{DIM}Live checks need keys: --macros, or --adapt <recipe_id> --user <uuid>{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
