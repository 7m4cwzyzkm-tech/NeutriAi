"""Food scanning and meal logging."""
from __future__ import annotations

import posixpath

from datetime import date, datetime, timezone

import structlog
from fastapi import APIRouter, BackgroundTasks, Query, status

from ..db import maybe_one, one, rows, service
from ..deps import AiScanDep, CurrentUserDep
from ..errors import Forbidden, NotFound
from ..models.common import Macros, Ok
from ..models.nutrition import (
    AssessmentOut, CalibrationIn, VesselMeasurementIn, MealIn, MealOut, ScanRequest, ScanResult,
)
from ..services.ai import vision
from ..services.ai.portion import _key
from ..services import (
    accuracy_checks, calibration, food_identity, portion_learning, scale_learning,
)
from ..services.nutrition import resolver

log = structlog.get_logger()
router = APIRouter(tags=["nutrition"])


def notes_update(body) -> dict:
    """`{"notes": ...}` only when the client actually sent one.

    `notes` defaults to None, so a body that never mentions it looked identical
    to a body clearing it -- and the edit screen never mentioned it. Correcting
    one gram value therefore erased whatever the person had written about that
    meal, unrecoverably, with nothing on screen to say so.

    `model_fields_set` is the only thing that tells absent from null, which is
    exactly the distinction that was missing.
    """
    return ({"notes": body.notes}
            if "notes" in getattr(body, "model_fields_set", set()) else {})


@router.post("/scans", response_model=ScanResult, status_code=status.HTTP_201_CREATED)
async def create_scan(body: ScanRequest, user: CurrentUserDep, _quota: AiScanDep):
    """Analyse an uploaded meal photo.

    The client uploads to the ``meal-photos`` bucket under ``{user_id}/...``
    first, then posts the object keys here. Runs inline because the user is
    staring at a spinner; typical latency is 4-8 s.

    THE PREFIX IS CHECKED HERE, and this docstring used to claim RLS did it.
    RLS governs the UPLOAD. The read further down uses the service key, which
    bypasses both RLS and the storage policy by design, so nothing between this
    line and the download ever compared the path to the caller.

        POST /v1/scans {"image_paths": ["<someone-else-uuid>/IMG_0042.jpg"]}

    returned that person's meal, analysed, to whoever asked. Their photo, their
    food, their plate. One prefix comparison closes it.
    """
    for path in body.image_paths:
        # Normalised first: "abc/../def/x.jpg" starts with the right prefix and
        # does not stay there.
        clean = posixpath.normpath(str(path or "").strip().lstrip("/"))
        if clean.startswith("..") or not clean.startswith(f"{user.id}/"):
            log.warning("scan_foreign_image_path", user=str(user.id)[:8],
                        path=str(path)[:80])
            raise Forbidden("That photo does not belong to you.")

    scan = one(
        service().table("food_scans").insert({
            "user_id": user.id,
            "image_paths": body.image_paths,
            "meal_slot": body.meal_slot,
            "calibration_id": body.calibration_id,
            "camera_distance_mm": body.camera_distance_mm,
            "camera_fov_deg": body.camera_fov_deg,
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
        camera_distance_mm=body.camera_distance_mm,
        camera_fov_deg=body.camera_fov_deg,
        camera_aspect_ratio=body.camera_aspect_ratio,
        measure_footprints=body.measure_footprints,
        # What the person typed about the plate before the camera opened.
        # Required by the app and, until this line, read by nothing.
        note=body.note,
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
    row = {**body.model_dump(), "user_id": user.id}
    # The vessel is typed by a person and matched against a value generated by
    # a model, so both sides have to be normalised the same way. "Bowl",
    # "soup bowl" and "bowl " were all failing to match the model's "bowl",
    # and the photo was then sized against whatever the default calibration
    # was -- usually a dinner plate, roughly 2x wrong on a bowl, with nothing
    # telling the user their measurement had gone unused.
    if row.get("vessel"):
        row["vessel"] = _key(row["vessel"])
    return one(
        user.sb.table("scan_calibrations")
        .upsert(row, on_conflict="user_id,label")
        .execute()
    )


@router.get("/calibrations/progress")
async def calibration_progress(user: CurrentUserDep):
    """How well this user's own crockery is measured, and what would help next.

    The headline is the ACCURACY, not a count of photos. "12 of 20 photos" is a
    measure of the user's compliance; "your dinner plate is measured to 2.4%"
    is a measure of what they are getting for it, and it is the number that
    decides whether their portions are right. Scale multiplies every gram: a
    width 10% wrong makes every portion off that plate 21% wrong in area before
    the food has even been named.

    Everything here is computed from the stored observations at request time,
    not from a counter someone incremented. A number that cannot be recomputed
    cannot be trusted, and this one is shown to the user as a promise.
    """
    return scale_learning.progress(user.id)


@router.post("/calibrations/measure", status_code=201)
async def record_measurement(body: VesselMeasurementIn, user: CurrentUserDep):
    """The user measured a vessel with a tape. Worth about 28 photos.

    Kept separate from POST /calibrations, which writes a size directly. This
    files an OBSERVATION, so a tape reading sits in the same history as every
    inferred one and the published width and its error are recomputed from the
    whole record. It also means a mistyped measurement can be seen and removed
    rather than silently becoming the answer.
    """
    vessel = _key(body.vessel)
    scale_learning.record(
        user_id=user.id, vessel=vessel,
        width_mm=body.width_mm, source="tape",
    )
    return {"vessel": vessel, **scale_learning.progress(user.id)}


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

    # Capture what we estimated BEFORE deleting it. The difference between
    # this and what the user just typed is the only ground truth the app ever
    # gets, and it is what teaches the estimator their vessel sizes.
    original_items = rows(
        user.sb.table("meal_items").select("name, grams, estimation_method, detected_name")
        .eq("meal_id", meal_id).execute()
    )

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
            # Sodium was the one macro never written per item, so the sum of a
            # meal's items disagreed with the meal's own figure for it.
            "sodium_mg": round(m.sodium_mg, 2),
        })
    if new_items:
        user.sb.table("meal_items").insert(new_items).execute()

    updated = one(
        user.sb.table("meals").update({
            "title": body.title or existing["title"],
            "meal_slot": body.meal_slot.value,
            # Only when the client actually SENT one.
            #
            # `notes` defaults to None, so a body that never mentions it looked
            # identical to a body clearing it -- and the edit screen did not
            # mention it. Correcting a gram value therefore erased whatever the
            # person had written about that meal, unrecoverably, with nothing on
            # screen to say so. `model_fields_set` is the only thing that can
            # tell "absent" from "null", which is exactly the distinction that
            # was missing.
            **notes_update(body),
            "kcal": round(totals.kcal, 2), "protein_g": round(totals.protein_g, 2),
            "carbs_g": round(totals.carbs_g, 2), "fat_g": round(totals.fat_g, 2),
            "fiber_g": round(totals.fiber_g, 2), "sugar_g": round(totals.sugar_g, 2),
            # ...and it was the one macro this update left alone, so a user who
            # deleted every item but one saw kcal drop to 90 while sodium
            # stayed at the original scan's 1,400 mg.
            "sodium_mg": round(totals.sodium_mg, 2),
            "is_verified": True, "confidence": 1.0,
        }).eq("id", meal_id).execute()
    )
    fresh = one(
        user.sb.table("meals").select("*, meal_items(*)").eq("id", meal_id).limit(1).execute()
    )
    # A tester read these numbers off a kitchen scale; anyone else typed a
    # guess. Only tagged and logged for now -- nothing learns differently.
    # Read with the service role: since 0029 the client role may SELECT only
    # the five public profile columns, so through user.sb this read is
    # refused and is_tester() would quietly answer False for every tester.
    # Filtered to the verified user.id, so access is unchanged.
    tester = accuracy_checks.is_tester(service(), user.id)
    source = "weighed" if tester else "typed"

    # Only after the correction is safely stored. Learning is a bonus and
    # must never be able to fail a user's edit.
    calibration.learn_from_correction(
        user.id, existing.get("scan_id"), original_items, body.items, source=source
    )
    # The other half of the same correction. These two are deliberately
    # disjoint: calibration takes corrections on VESSEL-scaled photos and learns
    # the vessel; this takes the ones where a card or a measured plate already
    # fixed the scale, and learns how tall the food stood. Attributing a
    # correction to the wrong term moves a value that was not at fault.
    portion_learning.learn_from_correction(
        existing.get("scan_id"), original_items, body.items, source=source
    )
    # And the third thing a correction can carry: what the food ACTUALLY is.
    #
    # Kept separate from the two above because it is not a scale correction at
    # all. A renamed food changes the density, the height prior and the
    # nutrition lookup together -- one plate of rajas called "creamy chicken"
    # instead of "creamy mushroom sauce" moved the meal from 186 g to 315 g and
    # its energy by 40%, on geometry that was within 12% both times.
    food_identity.learn_from_correction(user.id, original_items, body.items, source=source)

    # A tester's correction is also a prediction check: what the scan said
    # against what their scale said, per item they changed. Logged only; a
    # failure here must not fail the correction they just saved.
    if tester:
        try:
            accuracy_checks.record(accuracy_checks.corrected_rows(
                user.id, existing.get("scan_id"), meal_id, original_items, body.items,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning("accuracy_check_log_failed", meal_id=meal_id, error=str(exc)[:200])

    return MealOut(**{**fresh, "items": fresh.pop("meal_items", [])})


@router.post("/meals/{meal_id}/verify", response_model=MealOut)
async def verify_meal(meal_id: str, user: CurrentUserDep):
    """A tester's "matches what I weighed": the scan already equals their
    kitchen scale, so there is nothing to type and nothing to learn.

    Items are left exactly as they are and no learn_from_correction runs. One
    scan_accuracy_checks row per item records predicted == actual, and the
    meal is marked verified, as a correction does. Testers only -- checked
    here, not just by hiding the button.
    """
    existing = maybe_one(
        user.sb.table("meals").select("id, scan_id").eq("id", meal_id)
        .eq("user_id", user.id).limit(1).execute()
    )
    if not existing:
        raise NotFound("Meal not found.")
    # Service role, as in correct_meal -- via user.sb this is refused under 0029.
    if not accuracy_checks.is_tester(service(), user.id):
        raise Forbidden("Only invited testers can verify a scan against a scale.")

    items = rows(
        user.sb.table("meal_items").select("name, grams").eq("meal_id", meal_id).execute()
    )
    # Recorded BEFORE the meal is marked verified: this row is the whole point
    # of the action, so if it cannot be written the tester should see a
    # failure and retry, not a verified meal with no check behind it.
    accuracy_checks.record(
        accuracy_checks.matched_rows(user.id, existing.get("scan_id"), meal_id, items)
    )
    user.sb.table("meals").update({"is_verified": True, "confidence": 1.0}) \
        .eq("id", meal_id).execute()
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
    # Compared by id, not by whole row: `cached` rows carry seven columns and a
    # resolved fact carries all of them, so `fresh not in cached` was never
    # false and an already-listed food was returned twice, in two shapes.
    known = {row.get("id") for row in cached}
    return cached if fresh.get("id") in known else cached + [fresh]
