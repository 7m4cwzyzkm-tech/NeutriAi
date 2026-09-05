#!/usr/bin/env python3
"""Bench for the AI workout coach (app/services/ai/coach.py).

Note: the coach lives in coach.py, not workout.py.

Offline checks need no keys — plan validation, PR maths, HR zones, the
equipment→library filter. The live checks need ANTHROPIC_API_KEY,
OPENAI_API_KEY and Supabase.

    python -m scripts.coach_lab                       # all offline checks
    python -m scripts.coach_lab --library dumbbell,bench
    python -m scripts.coach_lab --prs
    python -m scripts.coach_lab --scan photo.jpg --user <uuid>   # live
    python -m scripts.coach_lab --plan --equipment none --user <uuid>  # live
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai.coach import (  # noqa: E402
    VALID_EQUIPMENT, _fallback_plan, _validate, epley_1rm, hr_zones,
)

HDR, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
GRN, RED, YEL = "\033[32m", "\033[31m", "\033[33m"

# A stand-in library so validation can be exercised with no database.
STUB_LIB = [
    {"slug": "push-up", "name": "Push-Up", "kind": "calisthenics",
     "primary_muscle": "chest", "difficulty": 2, "regression_slug": "incline-push-up"},
    {"slug": "incline-push-up", "name": "Incline Push-Up", "kind": "calisthenics",
     "primary_muscle": "chest", "difficulty": 1, "regression_slug": None},
    {"slug": "bodyweight-squat", "name": "Bodyweight Squat", "kind": "calisthenics",
     "primary_muscle": "quads", "difficulty": 1, "regression_slug": None},
    {"slug": "inverted-row", "name": "Inverted Row", "kind": "calisthenics",
     "primary_muscle": "back", "difficulty": 2, "regression_slug": None},
    {"slug": "plank", "name": "Plank", "kind": "core",
     "primary_muscle": "core", "difficulty": 1, "regression_slug": None},
    {"slug": "hip-hinge", "name": "Bodyweight Hip Hinge", "kind": "calisthenics",
     "primary_muscle": "hamstrings", "difficulty": 1, "regression_slug": None},
    {"slug": "pike-push-up", "name": "Pike Push-Up", "kind": "calisthenics",
     "primary_muscle": "shoulders", "difficulty": 3, "regression_slug": "incline-push-up"},
    {"slug": "band-pull-apart", "name": "Band Pull-Apart", "kind": "mobility",
     "primary_muscle": "rear_delt", "difficulty": 1, "regression_slug": None},
]


def ok(label: str, passed: bool, detail: str = "") -> None:
    mark = f"{GRN}  ok {OFF}" if passed else f"{RED}FAIL{OFF}"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def check_validation() -> None:
    """The safety property: never prescribe equipment the user does not have."""
    print(f"\n{HDR}1. Plan validation — the safety guarantee{OFF}")
    print(f"{DIM}  A model asked for a bodyweight plan will still occasionally emit a")
    print(f"  barbell movement. _validate() is what stops that reaching a user.{OFF}\n")

    plan = {"days": [{"title": "Full Body", "blocks": [
        {"slug": "bench-press", "name": "Barbell Bench Press", "sets": 4, "reps": "5"},
        {"slug": "back-squat", "name": "Back Squat", "sets": 4, "reps": "5"},
        {"slug": "push-up", "name": "Push-Up", "sets": 3, "reps": "12"},
        {"slug": "plank", "name": "Plank", "sets": 3, "reps": "45s"},
        {"slug": "invented-exercise", "name": "Nonsense Machine Fly", "sets": 3, "reps": "10"},
    ]}]}
    fixed, warnings = _validate(plan, STUB_LIB)
    survived = [b["slug"] for b in fixed["days"][0]["blocks"]]

    print(f"  {DIM}submitted:{OFF} bench-press, back-squat, push-up, plank, invented-exercise")
    print(f"  {DIM}survived: {OFF} {', '.join(survived) or '(none)'}\n")
    ok("barbell work removed", "bench-press" not in survived and "back-squat" not in survived)
    ok("hallucinated exercise removed", "invented-exercise" not in survived)
    ok("valid bodyweight work kept", "push-up" in survived and "plank" in survived)
    ok("every removal is explained", len(warnings) >= 3, f"{len(warnings)} warnings")
    for w in warnings:
        print(f"        {DIM}· {w}{OFF}")

    clean = {"days": [{"blocks": [{"slug": "push-up", "name": "Push-Up", "sets": 3, "reps": "10"}]}]}
    _, no_warn = _validate(clean, STUB_LIB)
    ok("a valid plan passes untouched", not no_warn)


def check_fallback() -> None:
    """There is always a plan, even with every model down."""
    print(f"\n{HDR}2. Deterministic fallback — no AI required{OFF}")
    print(f"{DIM}  If Claude is unreachable, _fallback_plan() still produces a real")
    print(f"  programme from the same validated library.{OFF}\n")

    req = SimpleNamespace(goal="build_muscle", days_per_week=4, weeks=4,
                          session_minutes=45, experience="intermediate", limitations=[])
    plan = _fallback_plan(req, ["none"], STUB_LIB)
    ok("a plan was produced", bool(plan.get("days")))
    ok("all weeks generated", len(plan["days"]) == 16, f"{len(plan['days'])} sessions (4×4)")
    ok("every session has work", all(d["blocks"] for d in plan["days"]))
    ok("progression rule stated", bool(plan["progression"].get("rule")))
    wk1 = [d for d in plan["days"] if d["week_index"] == 1][0]
    wk4 = [d for d in plan["days"] if d["week_index"] == 4][0]
    ok("volume rises across weeks",
       wk4["blocks"][0]["sets"] > wk1["blocks"][0]["sets"],
       f"week 1: {wk1['blocks'][0]['sets']} sets → week 4: {wk4['blocks'][0]['sets']} sets")
    print(f"\n  {DIM}Sample — {wk1['title']}:{OFF}")
    for b in wk1["blocks"]:
        print(f"        {b['name']:26s} {b['sets']} × {b['reps']}  rest {b['rest_s']}s")


def check_prs() -> None:
    print(f"\n{HDR}3. PR detection — Epley 1RM{OFF}")
    print(f"{DIM}  1RM = weight × (1 + reps/30), with two corrections.{OFF}\n")
    for w, r, note in [
        (100, 1, "a true single is the weight itself, not weight×1.033"),
        (100, 5, "100 × (1 + 5/30) = 116.7"),
        (100, 10, "100 × (1 + 10/30) = 133.3"),
        (60, 30, "clamped at 12 reps — beyond that Epley is fiction"),
    ]:
        print(f"    {w:5.0f} kg × {r:2d} reps → {epley_1rm(w, r):6.1f} kg   {DIM}{note}{OFF}")
    ok("single rep is exact", epley_1rm(100, 1) == 100.0)
    ok("high reps clamped", epley_1rm(60, 30) == epley_1rm(60, 12))

    print(f"\n{HDR}4. Heart-rate zones{OFF}")
    print(f"{DIM}  HRmax defaults to Tanaka (208 − 0.7×age), better than 220−age.{OFF}\n")
    for avg, mx, age, dur in [(150, 190, 35, 3600), (120, None, 45, 1800), (175, 195, 28, 2700)]:
        z = hr_zones(avg, mx, age, dur)
        dist = "  ".join(f"{k}:{v // 60}m" for k, v in z.items() if v)
        print(f"    avg {avg} max {mx or '—'} age {age} → {dist}")
        ok(f"  zones sum to {dur}s", sum(z.values()) == dur)
    ok("no HR data yields no zones", hr_zones(None, None, 30, 3600) == {})


def check_library(equipment: list[str]) -> None:
    print(f"\n{HDR}5. Equipment → library filter{OFF}")
    print(f"{DIM}  An exercise lists alternatives, not requirements: 'none, bench'")
    print(f"  means either works. One satisfied option is enough.{OFF}\n")
    unknown = [e for e in equipment if e not in VALID_EQUIPMENT]
    ok("equipment vocabulary is closed", not unknown,
       f"unknown: {unknown}" if unknown else f"{len(VALID_EQUIPMENT)} valid values")
    try:
        from app.services.ai.coach import library_for
        lib = library_for(equipment)
        print(f"\n  {len(lib)} exercises usable with: {', '.join(equipment)}")
        for e in sorted(lib, key=lambda x: (x.get("primary_muscle") or "", x["name"]))[:14]:
            print(f"    {e['name']:30s} {DIM}{e.get('primary_muscle', '')}{OFF}")
        if len(lib) > 14:
            print(f"    {DIM}… and {len(lib) - 14} more{OFF}")
    except Exception as exc:
        print(f"  {YEL}needs the database (seed.sql must be loaded): {str(exc)[:90]}{OFF}")


async def live_scan(image: str, user_id: str) -> None:
    print(f"\n{HDR}Live: equipment detection{OFF}\n")
    from app.services.ai.coach import scan_equipment
    from app.db import service

    path = f"{user_id}/lab-{Path(image).name}"
    with open(image, "rb") as fh:
        service().storage.from_("equipment-photos").upload(path, fh.read())
    print(f"  uploaded → equipment-photos/{path}")
    result = await scan_equipment(user_id, [path], "lab test")
    print(json.dumps(result, indent=2)[:1600])
    ok("equipment list returned", bool(result.get("equipment")))
    ok("falls back rather than failing",
       result["equipment"] != [] , f"fallback={result.get('fallback')}")


async def live_plan(equipment: list[str], user_id: str) -> None:
    print(f"\n{HDR}Live: plan generation{OFF}\n")
    from app.services.ai.coach import generate_plan, library_for

    req = SimpleNamespace(goal="build_muscle", days_per_week=4, weeks=4,
                          session_minutes=45, experience="intermediate", limitations=[])
    plan = await generate_plan(user_id, req, equipment, {"weight_kg": 80, "goal": "lose",
                                                         "activity_level": "moderate"}, [])
    lib_slugs = {e["slug"] for e in library_for(equipment)}
    used = {b["slug"] for d in plan["days"] for b in d["blocks"]}
    print(f"  name:        {plan['name']}")
    print(f"  sessions:    {len(plan['days'])}")
    print(f"  progression: {plan['progression'].get('model')} — {plan['progression'].get('rule')}")
    print(f"  calisthenics fallback: {plan['is_calisthenics_fallback']}\n")
    ok("all requested weeks present",
       len({d['week_index'] for d in plan['days']}) == req.weeks)
    ok("every prescribed slug is in the user's library", used <= lib_slugs,
       f"stray: {used - lib_slugs}" if used - lib_slugs else "")
    ok("at least one rest day per week",
       any(d.get("kind") == "rest" for d in plan["days"]) or req.days_per_week < 7,
       "or fewer than 7 training days requested")
    for d in plan["days"][:4]:
        print(f"\n  {HDR}W{d['week_index']}D{d['day_index']} — {d['title']}{OFF} ({d['est_minutes']} min)")
        for b in d["blocks"]:
            print(f"      {b['name']:28s} {b['sets']} × {b['reps']}  {DIM}{b.get('load_hint','')}{OFF}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--library", help="comma-separated equipment, e.g. dumbbell,bench")
    p.add_argument("--prs", action="store_true")
    p.add_argument("--scan", metavar="IMAGE", help="live: detect equipment in a photo")
    p.add_argument("--plan", action="store_true", help="live: generate a plan")
    p.add_argument("--equipment", default="none", help="comma-separated, for --plan")
    p.add_argument("--user", help="user uuid, required for live checks")
    a = p.parse_args()

    if a.scan or a.plan:
        if not a.user:
            print(f"{RED}--user <uuid> is required for live checks.{OFF}")
            return 2
        eq = [e.strip() for e in a.equipment.split(",") if e.strip()]
        asyncio.run(live_scan(a.scan, a.user) if a.scan else live_plan(eq, a.user))
        return 0

    if a.prs:
        check_prs(); return 0
    if a.library:
        check_library([e.strip() for e in a.library.split(",")]); return 0

    check_validation()
    check_fallback()
    check_prs()
    check_library(["none"])
    print(f"\n{DIM}Live checks need keys: --scan photo.jpg --user <uuid>, or --plan --user <uuid>{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
