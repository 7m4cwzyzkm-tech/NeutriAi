"""Food scanning and meal logging."""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Query, status

from ..db import maybe_one, one, rows, service
from ..deps import AiScanDep, CurrentUserDep
from ..errors import NotFound
from ..models.common import Macros, Ok
from ..models.nutrition import (
    AssessmentOut, CalibrationIn, MealIn, MealOut, ScanRequest, ScanResult,
)
from ..services.ai import vision
from ..services.nutrition import resolver

router = APIRouter(tags=["nutrition"])


@router.post("/scans", response_model=ScanResult, status_code=status.HTTP_201_CREATED)
async def create_scan(body: ScanRequest, user: CurrentUserDep, _quota: AiScanDep):
    """Analyse an uploaded meal photo.

    The client uploads to the ``meal-photos`` bucket under ``{user_id}/...``
    first (RLS enforces the prefix), then posts the object keys here. Runs
    inline because the user is staring at a spinner; typical latency is 4-8 s.
    """
    scan = one(
        service().table("food_scans").insert({
            "user_id": user.id,
            "image_paths": body.image_paths,
            "meal_slot": body.meal_slot,
            "calibration_id": body.calibration_id,
            "status": "pending",
        }).execute()
    )
    return await vision.run_scan(
        user_id=user.id,
        scan_id=scan["id"],
        image_paths=body.image_paths,
        meal_slot=body.meal_slot.value if body.meal_slot else None,
        calibration_id=body.calibration_id,
        plate_diameter_mm=body.plate_diameter_mm,
    )


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: str, user: CurrentUserDep):
    scan = maybe_one(
        user.sb.table("food_scans").select("*").eq("id", scan_id).eq("user_id", user.id)
        .limit(1).execute()
    )
    if not scan:
        raise NotFound("Scan not found.")
    meal = maybe_one(
        user.sb.table("meals").select("*, meal_items(*)").eq("scan_id", scan_id).limit(1).execute()
    )
    return {"scan": scan, "meal": meal}


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
@router.get("/calibrations")
async def list_calibrations(user: CurrentUserDep):
    return rows(user.sb.table("scan_calibrations").select("*").eq("user_id", user.id).execute())


@router.post("/calibrations", status_code=201)
async def add_calibration(body: CalibrationIn, user: CurrentUserDep):
    if body.is_default:
        user.sb.table("scan_calibrations").update({"is_default": False}).eq(
            "user_id", user.id
        ).execute()
    return one(
        user.sb.table("scan_calibrations")
        .upsert({**body.model_dump(), "user_id": user.id}, on_conflict="user_id,label")
        .execute()
    )


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------
@router.get("/meals", response_model=list[MealOut])
async def list_meals(user: CurrentUserDep, day: date | None = None, limit: int = Query(50, le=200)):
    q = user.sb.table("meals").select("*, meal_items(*)").eq("user_id", user.id)
    if day:
        q = q.eq("day", day.isoformat())
    result = rows(q.order("eaten_at", desc=True).limit(limit).execute())
    return [MealOut(**{**m, "items": m.pop("meal_items", [])}) for m in result]


@router.post("/meals", response_model=MealOut, status_code=201)
async def create_meal(body: MealIn, user: CurrentUserDep):
    """Manual meal entry, or logging a saved recipe."""
    items: list[dict] = []
    totals = Macros()

    if body.recipe_id:
        recipe = maybe_one(
            user.sb.table("recipes").select("*, recipe_ingredients(*)")
            .eq("id", body.recipe_id).limit(1).execute()
        )
        if not recipe:
            raise NotFound("Recipe not found.")
        factor = body.servings / max(recipe["servings"], 1)
        for ing in recipe.get("recipe_ingredients") or []:
            m = Macros(
                kcal=float(ing["kcal"]), protein_g=float(ing["protein_g"]),
                carbs_g=float(ing["carbs_g"]), fat_g=float(ing["fat_g"]),
                fiber_g=float(ing["fiber_g"]), sugar_g=float(ing["sugar_g"]),
            ).scaled(factor)
            totals = totals + m
            items.append({"name": ing["name"], "grams": float(ing["grams"]) * factor,
                          "food_fact_id": ing.get("food_fact_id"), "macros": m,
                          "estimation_method": "user_entered"})
    else:
        for it in body.items:
            if it.macros:
                m = it.macros
                fact_id = it.food_fact_id
            else:
                fact = await resolver.resolve(it.name)
                m = resolver.macros_for(fact, it.grams)
                fact_id = fact.get("id")
            totals = totals + m
            items.append({"name": it.name, "grams": it.grams, "food_fact_id": fact_id,
                          "macros": m, "estimation_method": "user_entered"})

    eaten = body.eaten_at or datetime.now(timezone.utc)
    meal = one(
        user.sb.table("meals").insert({
            "user_id": user.id,
            "recipe_id": body.recipe_id,
            "eaten_at": eaten.isoformat(),
            "day": eaten.date().isoformat(),
            "meal_slot": body.meal_slot.value,
            "title": body.title or (items[0]["name"] if items else "Meal"),
            "notes": body.notes,
            "kcal": round(totals.kcal, 2), "protein_g": round(totals.protein_g, 2),
            "carbs_g": round(totals.carbs_g, 2), "fat_g": round(totals.fat_g, 2),
            "fiber_g": round(totals.fiber_g, 2), "sugar_g": round(totals.sugar_g, 2),
            "sodium_mg": round(totals.sodium_mg, 2),
            "is_verified": True, "confidence": 1.0,
        }).execute()
    )
    if items:
        user.sb.table("meal_items").insert([
            {
                "meal_id": meal["id"], "name": i["name"], "grams": round(i["grams"], 1),
                "food_fact_id": i["food_fact_id"], "estimation_method": i["estimation_method"],
                "confidence": 1.0,
                "kcal": round(i["macros"].kcal, 2),
                "protein_g": round(i["macros"].protein_g, 2),
                "carbs_g": round(i["macros"].carbs_g, 2),
                "fat_g": round(i["macros"].fat_g, 2),
                "fiber_g": round(i["macros"].fiber_g, 2),
                "sugar_g": round(i["macros"].sugar_g, 2),
            }
            for i in items
        ]).execute()

    fresh = one(
        user.sb.table("meals").select("*, meal_items(*)").eq("id", meal["id"]).limit(1).execute()
    )
    return MealOut(**{**fresh, "items": fresh.pop("meal_items", [])})


@router.patch("/meals/{meal_id}", response_model=MealOut)
async def correct_meal(meal_id: str, body: MealIn, user: CurrentUserDep):
    """User corrections. This is also our accuracy feedback loop: a corrected
    meal is ground truth we can later use to tune the portion estimator."""
    existing = maybe_one(
        user.sb.table("meals").select("*").eq("id", meal_id).eq("user_id", user.id)
        .limit(1).execute()
    )
    if not existing:
        raise NotFound("Meal not found.")

    totals = Macros()
    user.sb.table("meal_items").delete().eq("meal_id", meal_id).execute()
    new_items = []
    for it in body.items:
        fact = await resolver.resolve(it.name) if not it.macros else {}
        m = it.macros or resolver.macros_for(fact, it.grams)
        totals = totals + m
        new_items.append({
            "meal_id": meal_id, "name": it.name, "grams": it.grams,
            "food_fact_id": it.food_fact_id or fact.get("id"),
            "estimation_method": "user_entered", "confidence": 1.0,
            "kcal": round(m.kcal, 2), "protein_g": round(m.protein_g, 2),
            "carbs_g": round(m.carbs_g, 2), "fat_g": round(m.fat_g, 2),
            "fiber_g": round(m.fiber_g, 2), "sugar_g": round(m.sugar_g, 2),
        })
    if new_items:
        user.sb.table("meal_items").insert(new_items).execute()

    updated = one(
        user.sb.table("meals").update({
            "title": body.title or existing["title"],
            "meal_slot": body.meal_slot.value,
            "notes": body.notes,
            "kcal": round(totals.kcal, 2), "protein_g": round(totals.protein_g, 2),
            "carbs_g": round(totals.carbs_g, 2), "fat_g": round(totals.fat_g, 2),
            "fiber_g": round(totals.fiber_g, 2), "sugar_g": round(totals.sugar_g, 2),
            "is_verified": True, "confidence": 1.0,
        }).eq("id", meal_id).execute()
    )
    fresh = one(
        user.sb.table("meals").select("*, meal_items(*)").eq("id", meal_id).limit(1).execute()
    )
    return MealOut(**{**fresh, "items": fresh.pop("meal_items", [])})


@router.delete("/meals/{meal_id}", response_model=Ok)
async def delete_meal(meal_id: str, user: CurrentUserDep):
    user.sb.table("meals").delete().eq("id", meal_id).eq("user_id", user.id).execute()
    return Ok(message="Meal deleted.")


@router.get("/assessments", response_model=list[AssessmentOut])
async def list_assessments(user: CurrentUserDep, day: date | None = None):
    q = user.sb.table("intake_assessments").select("*").eq("user_id", user.id)
    if day:
        q = q.eq("day", day.isoformat())
    return [AssessmentOut(**a) for a in rows(q.order("created_at", desc=True).limit(20).execute())]


@router.get("/foods/search")
async def search_foods(q: str = Query(min_length=2), user: CurrentUserDep = None):
    """Type-ahead over the shared food cache, falling back to a live lookup."""
    cached = rows(
        service().table("food_facts").select(
            "id,display_name,kcal_per_100g,protein_per_100g,carbs_per_100g,fat_per_100g,serving_hints"
        ).ilike("display_name", f"%{q}%").order("hits", desc=True).limit(12).execute()
    )
    if len(cached) >= 5:
        return cached
    fresh = await resolver.resolve(q)
    return cached + [fresh] if fresh not in cached else cached
