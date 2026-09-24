"""AI workout coach: equipment photo -> inventory -> progressive programme.

Two guarantees that matter more than the model quality:

* **We never prescribe equipment the user doesn't have.** The generated plan is
  validated against the detected inventory and the exercise library after
  generation; anything unsatisfiable is swapped for a regression that is, or
  dropped.
* **There is always a plan.** No equipment detected is not an error — it routes
  to a real calisthenics programme built from the seeded bodyweight library. If
  the model itself fails, a deterministic template still produces a usable week.
"""
from __future__ import annotations

import structlog

from ...config import settings
from ...db import rows, service
from .client import ask_reasoning, ask_vision, record_usage
from .prompts import COACH_SYSTEM, EQUIPMENT_VISION_SYSTEM
from .vision import fetch_images

log = structlog.get_logger()

VALID_EQUIPMENT = {
    "none", "dumbbell", "kettlebell", "barbell", "resistance_band", "bench",
    "squat_rack", "pull_up_bar", "cable_machine", "smith_machine", "treadmill",
    "bike", "rower", "medicine_ball", "trx", "plate", "jump_rope", "box",
    "machine_generic",
}


# ---------------------------------------------------------------------------
# Equipment detection
# ---------------------------------------------------------------------------
async def scan_equipment(user_id: str, image_paths: list[str], space_note: str | None) -> dict:
    # Equipment detection has no geometry, so only the encoded image is used.
    images = [p.b64 for p in await fetch_images("equipment-photos", image_paths)]
    if not images:
        return {"equipment": ["none"], "detected": [], "confidence": 0.0,
                "space_note": space_note or "", "fallback": True}

    call = await ask_vision(
        pipeline="equipment_detection",
        system=EQUIPMENT_VISION_SYSTEM,
        user_text=(
            "Inventory the training equipment in this space. "
            + (f"The user adds: {space_note}. " if space_note else "")
            + "Return only the JSON object."
        ),
        images=images,
        max_tokens=1400,
    )
    await record_usage(call, user_id)

    if not call.ok or not isinstance(call.payload, dict):
        return {"equipment": ["none"], "detected": [], "confidence": 0.0,
                "space_note": space_note or "", "fallback": True}

    p = call.payload
    detected = [d for d in (p.get("items") or []) if d.get("equipment") in VALID_EQUIPMENT]
    equipment = sorted({str(d["equipment"]) for d in detected}) or ["none"]
    confs = [float(d.get("confidence") or 0.5) for d in detected]
    return {
        "equipment": equipment,
        "detected": detected,
        "confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
        "space_note": (p.get("space_note") or space_note or "")[:200],
        "notes": [str(n)[:160] for n in (p.get("notes") or [])][:5],
        "fallback": equipment == ["none"],
    }


# ---------------------------------------------------------------------------
# Exercise library
# ---------------------------------------------------------------------------
def library_for(equipment: list[str]) -> list[dict]:
    """Every exercise whose equipment requirements are a subset of what we have.

    ``none`` is always available, which is what makes the calisthenics fallback
    work without a special code path.
    """
    have = set(equipment) | {"none"}
    all_ex = rows(
        service().table("exercises")
        .select("slug,name,kind,primary_muscle,equipment,difficulty,regression_slug,progression_slug,met")
        .execute()
    )
    usable = []
    for ex in all_ex:
        req = set(ex.get("equipment") or ["none"])
        # An exercise lists alternatives, not requirements: 'none, bench' means
        # it works with either. So one satisfied option is enough.
        if req & have:
            usable.append(ex)
    return usable


def _fallback_plan(req, equipment: list[str], lib: list[dict]) -> dict:
    """Deterministic full-body template, used when the model is unavailable."""
    by_muscle: dict[str, list[dict]] = {}
    for ex in lib:
        by_muscle.setdefault(ex.get("primary_muscle") or "other", []).append(ex)

    def pick(muscles: list[str], n: int = 1) -> list[dict]:
        out = []
        for m in muscles:
            for ex in sorted(by_muscle.get(m, []), key=lambda e: e.get("difficulty", 3)):
                out.append(ex)
                if len(out) >= n:
                    return out
        return out

    patterns = [
        ("Full Body A", ["quads", "chest", "back", "core"]),
        ("Full Body B", ["hamstrings", "shoulders", "lats", "core"]),
        ("Full Body C", ["quads", "chest", "back", "core"]),
    ]
    days = []
    for w in range(1, req.weeks + 1):
        for d in range(1, req.days_per_week + 1):
            title, muscles = patterns[(d - 1) % len(patterns)]
            blocks = []
            for m in muscles:
                chosen = pick([m], 1)
                if not chosen:
                    continue
                ex = chosen[0]
                # Isometric and mobility work is prescribed in seconds; giving a
                # plank a rep range is the kind of detail that tells a user the
                # programme was generated by something that wasn't paying attention.
                timed = ex.get("kind") in ("core", "mobility") or ex["slug"] in (
                    "plank", "hollow-hold", "cat-cow", "worlds-greatest-stretch"
                )
                blocks.append({
                    "slug": ex["slug"], "name": ex["name"],
                    "sets": 3 + (1 if w >= 3 else 0),
                    "reps": f"{20 + 10 * w}s hold" if timed else "8-12",
                    "rest_s": 45 if timed else 90,
                    "tempo": None if timed else "2-0-1-0",
                    "load_hint": (
                        "Add 10 seconds when the last set still feels controlled."
                        if timed else
                        "Add load or reps when the top of the range feels easy."
                    ),
                    "notes": "", "superset_with": None,
                })
            days.append({
                "week_index": w, "day_index": d,
                "title": f"{title} (W{w})", "kind": "strength",
                "est_minutes": req.session_minutes, "blocks": blocks,
            })
    return {
        "name": f"{req.weeks}-Week {'Bodyweight' if equipment == ['none'] else 'Home'} Programme",
        "rationale": "Generated from your equipment list using a full-body template.",
        "safety_notes": ["Warm up for 5 minutes.", "Stop a set when form breaks, not at failure."],
        "progression": {
            "model": "double_progression",
            "rule": "Hit the top of the rep range on every set, then add load or a harder variation.",
            "deload_week": max(4, req.weeks),
        },
        "days": days,
    }


def _validate(plan: dict, lib: list[dict]) -> tuple[dict, list[str]]:
    """Drop or substitute anything the user cannot actually perform."""
    by_slug = {e["slug"]: e for e in lib}
    warnings: list[str] = []
    for day in plan.get("days") or []:
        fixed = []
        for b in day.get("blocks") or []:
            slug = b.get("slug")
            if slug in by_slug:
                b["name"] = by_slug[slug]["name"]
                fixed.append(b)
                continue
            # Try the named regression, then anything for the same muscle.
            replacement = None
            src = by_slug.get(str(slug or ""))
            if src and src.get("regression_slug") in by_slug:
                replacement = by_slug[src["regression_slug"]]
            if replacement is None:
                target = (b.get("name") or "").lower()
                for ex in lib:
                    if ex["slug"].split("-")[0] in target:
                        replacement = ex
                        break
            if replacement:
                warnings.append(
                    f"Swapped '{b.get('name', slug)}' for '{replacement['name']}' "
                    "— the original needs equipment you don't have."
                )
                b["slug"], b["name"] = replacement["slug"], replacement["name"]
                fixed.append(b)
            else:
                warnings.append(f"Removed '{b.get('name', slug)}' — no usable substitute.")
        day["blocks"] = fixed
    return plan, warnings[:8]


# How much of the previous block goes into the prompt. A final week is a
# handful of days of 3-6 blocks each; the cap only stops a malformed plan
# from turning into an unbounded prompt.
PREVIOUS_BLOCK_MAX_CHARS = 2500


def previous_block_summary(plan: dict | None, final_week_days: list[dict]) -> str | None:
    """The plan being replaced, as the model should read it: its progression
    rule and what its FINAL week prescribed, so a new block can start at or
    above where that one ended rather than from a cold start.

    These are PRESCRIBED numbers (what the old plan told the user to do), not
    what the user actually lifted -- the plan row does not hold performance.
    None when there is no previous plan to build on.
    """
    if not plan:
        return None
    progression = plan.get("progression") or {}
    weeks = plan.get("weeks")
    lines = [
        f"Name: {plan.get('name') or 'Training plan'} ({weeks} weeks)",
        f"Progression rule: {progression.get('rule') or 'not stated'}"
        + (f" (model: {progression['model']})" if progression.get("model") else ""),
    ]
    if final_week_days:
        lines.append(f"Its final week (week {weeks}) prescribed:")
        for d in sorted(final_week_days, key=lambda d: d.get("day_index") or 0):
            blocks = d.get("blocks") or []
            if not blocks:
                continue
            parts = []
            for b in blocks:
                part = f"{b.get('name') or b.get('slug')} {b.get('sets')}x{b.get('reps')}"
                if b.get("load_hint"):
                    part += f" @ {b['load_hint']}"
                parts.append(part)
            lines.append(f"- {d.get('title') or 'Day'}: " + "; ".join(parts))
    text = "\n".join(lines)
    return text[:PREVIOUS_BLOCK_MAX_CHARS]


async def generate_plan(
    user_id: str, req, equipment: list[str], profile: dict, history: list[dict],
    previous_block: str | None = None,
) -> dict:
    """`previous_block`, when given, is previous_block_summary() of the plan
    this one replaces -- the only input that makes a new block continue from
    the last one. Everything else is unchanged by it."""
    lib = library_for(equipment)
    is_fallback = equipment in ([], ["none"])

    call = await ask_reasoning(
        pipeline="workout_plan",
        model=settings.coach_model,
        system=COACH_SYSTEM,
        user_text=(
            f"Goal: {req.goal}\nExperience: {req.experience}\n"
            f"Days per week: {req.days_per_week}\nWeeks: {req.weeks}\n"
            f"Session budget: {req.session_minutes} minutes\n"
            f"Available equipment: {', '.join(equipment) or 'none (bodyweight only)'}\n"
            f"Limitations: {', '.join(req.limitations) or 'none stated'}\n"
            f"Body: {profile.get('weight_kg')} kg, goal {profile.get('goal')}, "
            f"activity {profile.get('activity_level')}\n"
            f"Recent sessions: {len(history)} in the last 30 days\n\n"
            + (
                "Previous block (the plan this one replaces -- start at or above "
                "where its final week left off):\n"
                f"{previous_block}\n\n"
                if previous_block else ""
            )
            + "Exercise library (use these exact slugs only):\n"
            + "\n".join(
                f"- {e['slug']} | {e['name']} | {e['kind']} | {e.get('primary_muscle')} | "
                f"diff {e.get('difficulty')}"
                for e in lib
            )
            + "\n\nGenerate the full programme."
        ),
        max_tokens=8000,
    )
    await record_usage(call, user_id)

    if call.ok and isinstance(call.payload, dict) and call.payload.get("days"):
        plan = call.payload
    else:
        log.warning("plan_generation_fell_back", user_id=user_id, error=call.error)
        plan = _fallback_plan(req, equipment, lib)

    plan, warnings = _validate(plan, lib)
    plan["safety_notes"] = list(plan.get("safety_notes") or []) + warnings
    plan["is_calisthenics_fallback"] = is_fallback
    return plan


# ---------------------------------------------------------------------------
# PR detection
# ---------------------------------------------------------------------------
def epley_1rm(weight_kg: float, reps: int) -> float:
    """Epley formula, with two corrections.

    A single at weight W *is* a 1RM of W, but the raw formula returns
    W * (1 + 1/30) — a 3% inflation on the one case we actually measured
    directly. And beyond about 12 reps the formula overestimates badly, so we
    clamp rather than report fiction.
    """
    if reps <= 1:
        return float(weight_kg)
    return weight_kg * (1 + min(reps, 12) / 30.0)


def detect_prs(user_id: str, workout_id: str, sets: list[dict]) -> list[dict]:
    """Compare this session's sets against stored bests and record new ones."""
    sb = service()
    prs: list[dict] = []
    best_by_slug: dict[str, dict] = {}

    for s in sets:
        slug = s.get("exercise_slug") or (s.get("exercise_name") or "").lower().replace(" ", "-")
        if s.get("is_warmup"):
            continue
        w, r = s.get("weight_kg"), s.get("reps")
        candidates: list[tuple[str, float, str]] = []
        if w and r:
            candidates.append(("1rm", round(epley_1rm(float(w), int(r)), 1), "kg"))
            candidates.append(("volume", round(float(w) * int(r), 1), "kg"))
        elif r:
            candidates.append(("reps", float(r), "reps"))
        if s.get("duration_s"):
            candidates.append(("duration", float(s["duration_s"]), "s"))
        if s.get("distance_m"):
            candidates.append(("distance", float(s["distance_m"]), "m"))

        for metric, value, unit in candidates:
            key = f"{slug}:{metric}"
            if key not in best_by_slug:
                existing = (
                    sb.table("personal_records").select("value")
                    .eq("user_id", user_id).eq("exercise_slug", slug).eq("metric", metric)
                    .order("value", desc=True).limit(1).execute()
                )
                best_by_slug[key] = {"value": float(existing.data[0]["value"]) if existing.data else 0.0}
            if value > best_by_slug[key]["value"]:
                best_by_slug[key]["value"] = value
                prs.append({
                    "user_id": user_id, "exercise_slug": slug, "metric": metric,
                    "value": value, "unit": unit, "workout_id": workout_id,
                })

    if prs:
        sb.table("personal_records").insert(prs).execute()
        sb.table("workout_sets").update({"is_pr": True}).eq("workout_id", workout_id).execute()
    return prs


def hr_zones(avg_hr: int | None, max_hr: int | None, age: int, duration_s: int) -> dict:
    """Approximate time-in-zone. A real implementation reads the sample stream
    from the wearable; this gives a usable answer from summary data alone."""
    if not avg_hr or not duration_s:
        return {}
    hrmax = max_hr or int(208 - 0.7 * age)   # Tanaka, better than 220-age
    pct = avg_hr / max(hrmax, 1)
    zone = 1
    for i, edge in enumerate([0.60, 0.70, 0.80, 0.90], start=1):
        if pct >= edge:
            zone = i + 1
    # Put most of the time in the average zone, spilling into neighbours.
    out = {f"z{i}": 0 for i in range(1, 6)}
    out[f"z{zone}"] = int(duration_s * 0.6)
    if zone > 1:
        out[f"z{zone - 1}"] = int(duration_s * 0.25)
    if zone < 5:
        out[f"z{zone + 1}"] = int(duration_s * 0.15)
    return out
