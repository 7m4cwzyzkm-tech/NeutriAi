"""The food-scan pipeline: photo in, logged meal out.

    images -> [GPT-4o Vision] -> detections with area ratios
           -> [portion estimator] -> grams with a confidence band
           -> [nutrition resolver] -> macros per item
           -> [Claude] -> sanity check, merges, corrections, final confidence
           -> meal + meal_items + intake assessment persisted

Every stage degrades rather than failing: if Claude is down we ship the
geometric answer; if the vision model is down we return a clear "add it
manually" result instead of a 500.
"""
from __future__ import annotations

import asyncio
import base64
import io
import time
from datetime import date, datetime, timezone

import structlog

from ...config import settings
from ...db import maybe_one, service
from ...models.common import Macros
from ...models.nutrition import DetectedItem, ScanResult
from ..nutrition import assessment, resolver
from .client import ask_reasoning, ask_vision, record_usage
from .portion import GeometryHint, band_label, estimate_grams, reconcile_multi_image
from .prompts import FOOD_REASONING_SYSTEM, FOOD_VISION_SYSTEM, FOOD_VISION_USER

log = structlog.get_logger()

MAX_IMAGE_EDGE = 1280      # anything larger is wasted tokens
JPEG_QUALITY = 82


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------
def downscale_jpeg(data: bytes) -> str:
    """Resize and re-encode to base64 JPEG. Vision quality plateaus well below
    phone resolution, and tokens scale with pixels, so this is pure savings."""
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)          # honour phone orientation
        img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:  # noqa: BLE001
        log.warning("image_downscale_failed", error=str(exc)[:200])
        return base64.b64encode(data).decode()


async def fetch_images(bucket: str, paths: list[str]) -> list[str]:
    """Pull the uploaded objects out of Supabase Storage and prep them."""
    sb = service()

    def _one(path: str) -> str | None:
        try:
            return downscale_jpeg(sb.storage.from_(bucket).download(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("image_fetch_failed", path=path, error=str(exc)[:200])
            return None

    results = await asyncio.gather(*(asyncio.to_thread(_one, p) for p in paths))
    return [r for r in results if r]


# ---------------------------------------------------------------------------
# Stage 1: recognition
# ---------------------------------------------------------------------------
async def detect_foods(images: list[str], user_id: str) -> dict:
    multi = (
        f"There are {len(images)} photos of the SAME meal from different angles. "
        "Report the union of foods once, using the clearest view for each."
        if len(images) > 1
        else "There is one photo."
    )
    call = await ask_vision(
        pipeline="food_recognition",
        system=FOOD_VISION_SYSTEM,
        user_text=FOOD_VISION_USER.format(multi_note=multi),
        images=images,
        max_tokens=2000,
    )
    await record_usage(call, user_id)
    if not call.ok or not isinstance(call.payload, dict):
        return {"items": [], "plate_detected": False, "plate_area_ratio": 0.0,
                "_error": call.error or "vision returned nothing usable"}
    payload = dict(call.payload)
    payload["_call"] = call
    return payload


# ---------------------------------------------------------------------------
# Stage 2 + 3: geometry and macros
# ---------------------------------------------------------------------------
async def build_items(
    detections: list[dict], hint: GeometryHint
) -> tuple[list[DetectedItem], list[str]]:
    notes: list[str] = []
    names = [str(d.get("name") or "food").strip().lower() for d in detections]
    facts = await resolver.resolve_many(names)

    items: list[DetectedItem] = []
    for det, name in zip(detections, names):
        fact = facts.get(name) or {}
        est = estimate_grams(
            name=name,
            area_ratio=float(det.get("area_ratio") or 0.0),
            hint=hint,
            shape_hint=det.get("shape"),
            density=(float(fact["density_g_ml"]) if fact.get("density_g_ml") else None),
            ai_prior_grams=(float(det["typical_serving_g"]) if det.get("typical_serving_g") else None),
            detection_confidence=float(det.get("confidence") or 0.6),
        )
        notes.extend(est.notes)
        macros = resolver.macros_for(fact, est.grams) if fact else Macros()
        items.append(
            DetectedItem(
                name=(fact.get("display_name") or name)[:120],
                cuisine=det.get("cuisine") or fact.get("cuisine"),
                grams=est.grams,
                grams_low=est.grams_low,
                grams_high=est.grams_high,
                estimation_method=est.method,
                pixel_area_ratio=est.pixel_area_ratio,
                depth_factor=est.depth_factor,
                confidence=est.confidence,
                bbox=det.get("bbox"),
                macros=macros,
                food_fact_id=fact.get("id"),
            )
        )
    # Dedupe repeated notes while keeping order.
    seen: set[str] = set()
    unique_notes = [n for n in notes if not (n in seen or seen.add(n))]
    return items, unique_notes[:6]


# ---------------------------------------------------------------------------
# Stage 4: reasoning
# ---------------------------------------------------------------------------
async def refine(
    items: list[DetectedItem], profile: dict, day_totals: Macros, user_id: str, scene: str
) -> tuple[list[DetectedItem], dict]:
    if not items:
        return items, {"overall_confidence": 0.0, "needs_review": True, "corrections": []}

    call = await ask_reasoning(
        pipeline="food_reasoning",
        system=FOOD_REASONING_SYSTEM,
        user_text=(
            f"Scene: {scene}\n"
            f"User: goal={profile.get('goal')}, diet={profile.get('diet_mode')}, "
            f"weight={profile.get('weight_kg')} kg\n"
            f"Already eaten today: {day_totals.kcal:.0f} kcal\n"
            f"Detections (index, name, grams, method, confidence, kcal):\n"
            + "\n".join(
                f"{i}. {it.name} | {it.grams} g | {it.estimation_method} | "
                f"conf {it.confidence} | {it.macros.kcal:.0f} kcal"
                for i, it in enumerate(items)
            )
            + "\n\nReturn the corrected meal."
        ),
        max_tokens=2000,
    )
    await record_usage(call, user_id)

    if not call.ok or not isinstance(call.payload, dict):
        avg = sum(i.confidence for i in items) / len(items)
        return items, {
            "overall_confidence": round(avg * 0.9, 3),
            "needs_review": avg < 0.6,
            "corrections": [],
            "title": items[0].name.title() if items else "Meal",
        }

    p = call.payload
    refined: list[DetectedItem] = []
    for i, spec in enumerate(p.get("items") or []):
        if i >= len(items):
            break
        if spec.get("keep") is False:
            continue
        item = items[i].model_copy()
        new_grams = spec.get("grams")
        if isinstance(new_grams, (int, float)) and 1 <= float(new_grams) <= 3000:
            factor = float(new_grams) / max(item.grams, 0.01)
            if abs(factor - 1.0) > 0.02:
                item.grams = round(float(new_grams), 1)
                item.macros = item.macros.scaled(factor)
                item.grams_low = round(item.grams * 0.85, 1)
                item.grams_high = round(item.grams * 1.15, 1)
        if spec.get("name"):
            item.name = str(spec["name"])[:120]
        if isinstance(spec.get("confidence"), (int, float)):
            item.confidence = round(float(spec["confidence"]), 3)
        refined.append(item)

    if not refined:
        refined = items

    return refined, {
        "overall_confidence": round(float(p.get("overall_confidence") or 0.6), 3),
        "needs_review": bool(p.get("needs_review")),
        "corrections": [str(c)[:200] for c in (p.get("corrections") or [])][:5],
        "warnings": [str(c)[:200] for c in (p.get("warnings") or [])][:5],
        "title": str(p.get("title") or "Meal")[:120],
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
async def run_scan(
    *,
    user_id: str,
    scan_id: str,
    image_paths: list[str],
    meal_slot: str | None,
    calibration_id: str | None,
    plate_diameter_mm: float | None,
) -> ScanResult:
    t0 = time.perf_counter()
    sb = service()
    sb.table("food_scans").update({"status": "processing"}).eq("id", scan_id).execute()

    profile = maybe_one(
        sb.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    ) or {}
    targets = maybe_one(
        sb.table("nutrition_targets").select("*").eq("user_id", user_id)
        .order("effective_from", desc=True).limit(1).execute()
    ) or {}
    today = date.today().isoformat()
    summary = maybe_one(
        sb.table("daily_summaries").select("*").eq("user_id", user_id).eq("day", today)
        .limit(1).execute()
    ) or {}
    day_totals = Macros(
        kcal=float(summary.get("kcal_in") or 0),
        protein_g=float(summary.get("protein_g") or 0),
        carbs_g=float(summary.get("carbs_g") or 0),
        fat_g=float(summary.get("fat_g") or 0),
        fiber_g=float(summary.get("fiber_g") or 0),
        sugar_g=float(summary.get("sugar_g") or 0),
    )

    calibration = None
    if calibration_id:
        calibration = maybe_one(
            sb.table("scan_calibrations").select("*").eq("id", calibration_id)
            .eq("user_id", user_id).limit(1).execute()
        )
    elif not plate_diameter_mm:
        calibration = maybe_one(
            sb.table("scan_calibrations").select("*").eq("user_id", user_id)
            .eq("is_default", True).limit(1).execute()
        )

    images = await fetch_images("meal-photos", image_paths)
    if not images:
        return await _fail(scan_id, "Could not read the uploaded photo.", t0)

    detection = await detect_foods(images, user_id)
    raw_items = detection.get("items") or []
    if not raw_items:
        return await _fail(
            scan_id,
            detection.get("scene_notes") or detection.get("_error")
            or "No food was recognised in this photo.",
            t0,
            status="needs_review",
        )

    hint = GeometryHint(
        plate_ellipse_area_ratio=(float(detection.get("plate_area_ratio") or 0) or None),
        plate_diameter_mm=(
            plate_diameter_mm
            or (float(calibration["real_diameter_mm"]) if calibration and calibration.get("real_diameter_mm") else None)
        ),
        reference_area_mm2=(
            float(calibration["real_area_mm2"]) if calibration and calibration.get("real_area_mm2") else None
        ),
        image_count=len(images),
    )

    items, geo_notes = await build_items(raw_items, hint)
    items, meta = await refine(
        items, profile, day_totals, user_id, str(detection.get("scene_notes") or "")
    )

    totals = Macros()
    for it in items:
        totals = totals + it.macros

    # ---- persist meal ----
    now = datetime.now(timezone.utc)
    meal = sb.table("meals").insert({
        "user_id": user_id,
        "scan_id": scan_id,
        "eaten_at": now.isoformat(),
        "day": today,
        "meal_slot": meal_slot or _slot_for_hour(now.hour),
        "title": meta.get("title") or "Scanned meal",
        "kcal": round(totals.kcal, 2),
        "protein_g": round(totals.protein_g, 2),
        "carbs_g": round(totals.carbs_g, 2),
        "fat_g": round(totals.fat_g, 2),
        "fiber_g": round(totals.fiber_g, 2),
        "sugar_g": round(totals.sugar_g, 2),
        "sodium_mg": round(totals.sodium_mg, 2),
        "confidence": meta.get("overall_confidence"),
        "photo_path": image_paths[0],
    }).execute().data[0]

    if items:
        sb.table("meal_items").insert([
            {
                "meal_id": meal["id"],
                "food_fact_id": it.food_fact_id,
                "name": it.name,
                "cuisine": it.cuisine,
                "grams": it.grams,
                "grams_low": it.grams_low,
                "grams_high": it.grams_high,
                "estimation_method": it.estimation_method,
                "pixel_area_ratio": it.pixel_area_ratio,
                "depth_factor": it.depth_factor,
                "confidence": it.confidence,
                "kcal": round(it.macros.kcal, 2),
                "protein_g": round(it.macros.protein_g, 2),
                "carbs_g": round(it.macros.carbs_g, 2),
                "fat_g": round(it.macros.fat_g, 2),
                "fiber_g": round(it.macros.fiber_g, 2),
                "sugar_g": round(it.macros.sugar_g, 2),
                "sodium_mg": round(it.macros.sodium_mg, 2),
                "bbox": it.bbox,
            }
            for it in items
        ]).execute()

    # ---- intake assessment ----
    meals_today = len(
        sb.table("meals").select("id").eq("user_id", user_id).eq("day", today).execute().data or []
    )
    activity_kcal = int(summary.get("kcal_out") or 0)
    verdict = None
    if targets:
        verdict = await assessment.assess(
            user_id=user_id,
            profile=profile,
            targets=targets,
            day_totals=day_totals + totals,
            meal=totals,
            meal_slot=meal["meal_slot"],
            meals_today=meals_today,
            activity_kcal=activity_kcal,
        )
        sb.table("intake_assessments").insert({
            "user_id": user_id, "meal_id": meal["id"], "day": today,
            "severity": verdict["severity"], "kcal_over": verdict["kcal_over"],
            "pct_of_target": verdict["pct_of_target"],
            "carb_load_flag": verdict["carb_load_flag"],
            "meal_frequency": verdict["meal_frequency"],
            "headline": verdict["headline"], "detail": verdict["detail"],
            "portion_advice": verdict["portion_advice"],
            "macro_corrections": verdict["macro_corrections"],
            "next_meal": verdict["next_meal"],
        }).execute()

    latency = int((time.perf_counter() - t0) * 1000)
    confidence = float(meta.get("overall_confidence") or 0.5)
    sb.table("food_scans").update({
        "status": "complete",
        "vision_model": settings.vision_model,
        "reasoning_model": settings.reasoning_model,
        "overall_confidence": confidence,
        "confidence_band": band_label(confidence),
        "latency_ms": latency,
    }).eq("id", scan_id).execute()

    # Streak + motivation are side effects; never let them break the response.
    asyncio.create_task(_post_scan_effects(user_id, meal["id"], verdict, totals))

    return ScanResult(
        scan_id=scan_id,
        meal_id=meal["id"],
        status="complete",
        items=items,
        totals=totals,
        overall_confidence=confidence,
        confidence_band=band_label(confidence),
        needs_review=bool(meta.get("needs_review")),
        notes=(meta.get("corrections") or []) + geo_notes,
        latency_ms=latency,
        assessment=verdict,
    )


async def _post_scan_effects(user_id: str, meal_id: str, verdict: dict | None, totals: Macros):
    try:
        from ..motivation.engine import on_event

        service().rpc(
            "bump_streak",
            {"p_user": user_id, "p_kind": "log", "p_day": date.today().isoformat()},
        ).execute()
        trigger = "meal_logged"
        if verdict and verdict["severity"] in ("moderate", "severe"):
            trigger = "overate"
        await on_event(user_id, trigger, {"kcal": round(totals.kcal), "meal_id": meal_id})
    except Exception as exc:  # noqa: BLE001
        log.warning("post_scan_effects_failed", error=str(exc)[:200])


async def _fail(scan_id: str, message: str, t0: float, status: str = "failed") -> ScanResult:
    latency = int((time.perf_counter() - t0) * 1000)
    service().table("food_scans").update(
        {"status": status, "error": message[:400], "latency_ms": latency}
    ).eq("id", scan_id).execute()
    return ScanResult(
        scan_id=scan_id, status=status, items=[], totals=Macros(),
        overall_confidence=0.0, confidence_band="low", needs_review=True,
        notes=[message], latency_ms=latency,
    )


def _slot_for_hour(hour: int) -> str:
    if 4 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 15:
        return "lunch"
    if 17 <= hour < 22:
        return "dinner"
    return "snack"
