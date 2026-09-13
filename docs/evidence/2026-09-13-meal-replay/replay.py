#!/usr/bin/env python3
"""THE REPLAY RIG. Rebuilds the 12 Sep clean paired run with ZERO paid calls,
then perturbs ONE term at a time. Written 13 Sep 2026.

WHAT IT PROVED ON ITS FIRST RUN
    Reproduced every recorded gram of the 12 Sep run to 0.00 g, on all 27
    photographs, on all four recorded arms (CAL, UNCAL, CAL_GEO, UNCAL_GEO) --
    no vision call, no SAM2 call, no depth call, no USDA call, no LLM call.
    A control that reproduces exactly is what makes every perturbed arm below a
    measurement of that term alone, not of run-to-run noise.

RUN IT (from the repo root, PowerShell or cmd)
    backend\\.venv\\Scripts\\python.exe docs\\evidence\\2026-09-13-meal-replay\\replay.py
    backend\\.venv\\Scripts\\python.exe docs\\evidence\\2026-09-13-meal-replay\\replay.py --only 29 35
    backend\\.venv\\Scripts\\python.exe docs\\evidence\\2026-09-13-meal-replay\\score.py
  A full run takes a few minutes. It writes replay.json next to itself (use
  --out to write elsewhere and keep the committed one) and exits 1 if any
  photograph's control misses the recorded grams by more than 0.5 g.

WHAT IT NEEDS -- IF ANY OF THESE IS GONE THE RIG IS DEAD
  1. inputs/paired_v2.json and inputs/sweep_cache/*.json, IN THIS DIRECTORY.
     BOTH CAME FROM A SESSION SCRATCHPAD (the 12 Sep uncalibrated-sweep session,
     %TEMP%\\claude\\...scratch-2026-09-12-22a854\\...\\scratchpad) that is
     deleted when that session is. They are committed here for that reason.
     sweep_cache holds the ONE bought vision response per photograph; without
     it every replay re-buys a detection and the detections differ.
     paired_v2.json holds the recorded grams (the control), the resolved
     density per lookup name, and each item's measured SAM2 footprint.
     Hashes: MANIFEST.txt.
  2. backend/.env with the Supabase service credentials. The rig READS:
       - the bench photos from Storage bucket `meal-photos`, under the bench
         user's folder (STORED below). If those objects are deleted, the bytes
         the run used are gone; photos/ on disk has not been verified to be
         byte-identical, so do not substitute it without re-checking the control.
       - portion_learning.learned_heights() and food_identity.aliases_for(),
         exactly as build_items does. These are LIVE tables. If they change,
         the control stops reproducing -- which is the check working.
     It WRITES nothing: no meals, items, scans, facts, calibrations.
  3. backend/app at a commit where portion.py and vision.py match 8f049a5 on
     the weight path (true at 5942ed8: only config, depth_hosted and the USDA
     POST changed). Code drift also shows up as a failed control.
  NOT needed: vision/LLM keys, USDA key, SAM2/Replicate key, depth provider.
  Every one of those paths is replaced below and a call to it raises.

WHAT IS HELD FIXED, AND HOW EACH TERM CAN BE PERTURBED ALONE
    detection     sweep_cache                          edit the cached dict before arm()
    density       paired_v2 facts, replayed into       change facts[name]["density_g_ml"]
                  resolver.resolve_many (no lookup)
    footprint     paired_v2 measured_used, injected    scale `used` before the patch
                  as source "sam2" via _measured_areas
    height branch piece_share was not recorded; it is  pass other `pieces`
                  RECONSTRUCTED as the combination that
                  reproduces all four recorded arms
    plate area    Hough circle, as production          STATE["hough"]=False -> model's ratio
    frame scale   portion.mm2_per_frame, the single    STATE["k"] multiplies mm^2/frame
                  call site every rung goes through
                  (portion.py:1899)
    blend         BLEND_MAX_WEIGHT_BY_METHOD           blend=False zeroes it for the call
    depth         None, as the run (NullDepth)
  The arms this file runs: the four controls, CAL and UNCAL with frame area
  x1.2, CAL /1.2, CAL with the model's own plate_area_ratio, and UNCAL with the
  calibrated arm's frame scale substituted (same rung, same blend cap) -- the
  scale-alone counterfactual for a subscriber.

  Nutrition (kcal, carbs) is NOT computed here -- this rig is grams. score.py
  turns grams into macros from the served and correct rows it states.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import hashlib
import itertools
import json
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BACKEND = ROOT / "backend"
INPUTS = HERE / "inputs"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)                      # Settings reads .env relative to the cwd
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

import structlog  # noqa: E402

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

from app.services.ai import food_seg, portion, vision  # noqa: E402
from app.services.ai.portion import GeometryHint  # noqa: E402
from app.services.ai.segmenter import NullSegmenter  # noqa: E402
from app.services.nutrition import resolver  # noqa: E402

FOLDER = "b5568163-36f1-46b7-9d35-4ec81e39b38c"      # the bench account's storage folder
USER_ID = FOLDER
STORED = {
    "14-plate-mole-chicken-card.jpg": "bench-1788918232-14-plate-mole-chicken-card.jpg",
    "17-carrots-plate.jpg": "bench-1789195370-17-carrots-plate.jpg",
    "18-zucchini-plate.jpg": "bench-1789195451-18-zucchini-plate.jpg",
    "19-fish-veg-plate.jpg": "bench-1789191205-19-fish-veg-plate.jpg",
    "20-spaghetti-plate.jpg": "bench-1789195539-20-spaghetti-plate.jpg",
    "21-bbq-chicken-plate.jpg": "bench-1789195592-21-bbq-chicken-plate.jpg",
    "22-roast-beef-plate.jpg": "bench-1789190485-22-roast-beef-plate.jpg",
    "23-brussels-sprouts-plate.jpg": "bench-1789190581-23-brussels-sprouts-plate.jpg",
    "24-macaroni-salad-plate.jpg": "bench-1789190657-24-macaroni-salad-plate.jpg",
    "25-smashed-potatoes-plate.jpg": "bench-1789190737-25-smashed-potatoes-plate.jpg",
    "26-potroast-rice-alone.jpg": "bench-1789191287-26-potroast-rice-alone.jpg",
    "27-potroast-beef-alone.jpg": "bench-1789191363-27-potroast-beef-alone.jpg",
    "28-potroast-bread-alone.jpg": "bench-1789191433-28-potroast-bread-alone.jpg",
    "29-potroast-plate-4items.jpg": "bench-1789191533-29-potroast-plate-4items.jpg",
    "30-caesar-salad-plate.jpg": "bench-1789192200-30-caesar-salad-plate.jpg",
    "31-chicken-noodle-crock.jpg": "bench-1789192283-31-chicken-noodle-crock.jpg",
    "32-beef-posole-crock.jpg": "bench-1789192355-32-beef-posole-crock.jpg",
    "33-cheese-broccoli-crock.jpg": "bench-1789192431-33-cheese-broccoli-crock.jpg",
    "34-pizza-slice-plate.jpg": "bench-1789192502-34-pizza-slice-plate.jpg",
    "35-slider-fries-plate.jpg": "bench-1789230220-35-slider-fries-plate.jpg",
    "36-slider-fries-card.jpg": "bench-1789202270-36-slider-fries-card.jpg",
    "40-trailmix-spread.jpg": "bench-1789192575-40-trailmix-spread.jpg",
    "41-trailmix-heaped.jpg": "bench-1789193185-41-trailmix-heaped.jpg",
    "42-chips-spread.jpg": "bench-1789193344-42-chips-spread.jpg",
    "43-chips-heaped.jpg": "bench-1789193421-43-chips-heaped.jpg",
    "44-grapes-spread.jpg": "bench-1789193511-44-grapes-spread.jpg",
    "45-grapes-cluster.jpg": "bench-1789195715-45-grapes-cluster.jpg",
}
CONTROL_TOLERANCE_G = 0.5


# ---- nothing paid can run -------------------------------------------------
def _blocked(*_a, **_k):
    raise RuntimeError("paid call attempted in the replay rig")


food_seg._SEGMENTER = NullSegmenter()
vision.detect_foods = _blocked
vision.second_look = _blocked
vision.ask_vision = _blocked
vision._measured_heights = lambda dets, *a, **k: [None] * len(dets)
_real_resolve = resolver.resolve_many
resolver.resolve_many = _blocked       # re-bound per photograph to the recorded facts

_real_mm2 = portion.mm2_per_frame
_real_plate = vision._measured_plate_area_ratio
STATE = {"k": 1.0, "hough": True}
_plate_memo: dict[str, float | None] = {}


def _mm2(hint):
    mm2, method = _real_mm2(hint)
    return (mm2 * STATE["k"] if mm2 else mm2), method


def _plate(raw):
    """The Hough plate area, memoised on the DIGEST of the bytes.

    It was keyed on id(raw) and that was wrong: CPython reuses an object's id
    once the previous photograph's bytes are freed, so photo 25 was handed photo
    23's circle (0.2447 for 0.2644) and every gram moved by exactly that ratio.
    The control caught it -- 10.5 g off on 25 -- which is the reason it exists.
    """
    if not STATE["hough"]:
        return None
    key = hashlib.sha256(raw).hexdigest() if raw else ""
    if key not in _plate_memo:
        _plate_memo[key] = _real_plate(raw)
    return _plate_memo[key]


portion.mm2_per_frame = _mm2
vision._measured_plate_area_ratio = _plate


@contextmanager
def blend_off():
    saved, saved_default = dict(portion.BLEND_MAX_WEIGHT_BY_METHOD), portion.BLEND_MAX_WEIGHT
    try:
        for k in portion.BLEND_MAX_WEIGHT_BY_METHOD:
            portion.BLEND_MAX_WEIGHT_BY_METHOD[k] = 0.0
        portion.BLEND_MAX_WEIGHT = 0.0
        yield
    finally:
        portion.BLEND_MAX_WEIGHT_BY_METHOD.clear()
        portion.BLEND_MAX_WEIGHT_BY_METHOD.update(saved)
        portion.BLEND_MAX_WEIGHT = saved_default


# ---- the scan path's hint and build_items call, as the 12 Sep harness made them
def hint_for(detection: dict, fetched, plate_diameter_mm, camera_distance_mm) -> GeometryHint:
    """vision._run_scan's GeometryHint with the calibration lookup removed: CAL passes a
    diameter (which skips both lookups in _run_scan); UNCAL passes none and no lookup is
    made -- a user with no saved calibration."""
    measured_aspect = next((p.aspect for p in fetched if p.aspect), None)
    reference = next((p.reference for p in fetched if p.reference), None)
    return GeometryHint(
        plate_ellipse_area_ratio=(float(detection.get("plate_area_ratio") or 0) or None),
        plate_ellipse_wh=vision._plate_ellipse(detection),
        plate_diameter_mm=plate_diameter_mm,
        reference_area_mm2=None,
        depth_mm=camera_distance_mm,
        camera_fov_deg=None,
        aspect_ratio=measured_aspect,
        image_count=len(fetched),
        vessel=(str(detection.get("container")) if detection.get("container") else None),
        vessel_shape=(str(detection.get("container_shape")) if detection.get("container_shape") else None),
        reference_kind=(reference.kind if reference else None),
        reference_frame_width_mm=(reference.frame_width_mm if reference else None),
        reference_tilt_deg=(reference.tilt_deg if reference else None),
    )


async def run_arm(detection, fetched, plate, distance) -> list[dict]:
    items, _notes = await vision.build_items(
        copy.deepcopy(detection.get("items") or []),
        hint_for(detection, fetched, plate, distance),
        raw=(fetched[0].raw if len(fetched) == 1 else None),
        plate_bbox=(detection.get("plate_bbox") if isinstance(detection.get("plate_bbox"), dict) else None),
        user_id=USER_ID,
    )
    return [{"name": it.name, "grams": it.grams, "method": it.estimation_method,
             "confidence": it.confidence, "measured_used": it.measured_area_used} for it in items]


async def arm(detection, fetched, plate, distance, *, k=1.0, hough=True, blend=True):
    STATE["k"], STATE["hough"] = k, hough
    try:
        if blend:
            return await run_arm(detection, fetched, plate, distance)
        with blend_off():
            return await run_arm(detection, fetched, plate, distance)
    finally:
        STATE["k"], STATE["hough"] = 1.0, True


def frame_mm2(detection, fetched, plate, distance):
    """The mm^2-per-frame an arm actually uses, after build_items' measured-plate swap."""
    hint = hint_for(detection, fetched, plate, distance)
    measured = _plate(fetched[0].raw) if len(fetched) == 1 else None
    if measured:
        hint = replace(hint, plate_ellipse_area_ratio=measured)
    return _real_mm2(hint)


def diff(a, b):
    if len(a) != len(b):
        return float("inf")
    return max((abs(x["grams"] - y["grams"]) for x, y in zip(a, b)), default=0.0)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", metavar="PREFIX", help="photo prefixes, e.g. --only 29 35")
    ap.add_argument("--out", default=str(HERE / "replay.json"))
    args = ap.parse_args()

    recorded = json.loads((INPUTS / "paired_v2.json").read_text(encoding="utf-8"))
    names = [n for n in recorded if not args.only or n.startswith(tuple(args.only))]
    out: dict = {"_provenance": {
        "written": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "by": "docs/evidence/2026-09-13-meal-replay/replay.py",
        "inputs": "inputs/paired_v2.json (recorded 12 Sep 20:16 -0700), inputs/sweep_cache (12 Sep 17:28-17:39)",
        "paid_calls": 0,
        "units": "grams per item per arm; frame_mm2 in mm^2; k_true_uncal = CAL mm^2 / UNCAL mm^2",
    }}
    failed = []
    for name in names:
        rec = recorded[name]
        entry = rec["entry"]
        cache = INPUTS / "sweep_cache" / (name + ".json")
        if not cache.exists():
            print(f"{name}: no cached detection -- the rig will not buy one. Skipped.")
            failed.append(name)
            continue
        detection = json.loads(cache.read_text(encoding="utf-8"))["detection"]
        fetched = await vision.fetch_images("meal-photos", [f"{FOLDER}/{STORED[name]}"])
        if not fetched:
            print(f"{name}: stored photo could not be fetched. Skipped.")
            failed.append(name)
            continue
        facts = {n: {"display_name": n, "density_g_ml": v["density_g_ml"], "source": v["source"]}
                 for n, v in rec["facts"].items()}

        async def shared(_names, _f=facts):
            return {n: dict(_f[n]) for n in _names if n in _f}
        resolver.resolve_many = shared

        n = len(rec["arms"]["CAL"])
        used = [next((rec["arms"][a][i]["measured_used"] for a in rec["arms"]
                      if rec["arms"][a][i]["measured_used"] is not None), None) for i in range(n)]
        idx = [i for i, u in enumerate(used) if u is not None]
        best = None
        for combo in itertools.product((None, 0.95, 0.5), repeat=len(idx)):
            pieces = [None] * n
            for i, p in zip(idx, combo):
                pieces[i] = p
            vision._measured_areas = lambda d, r, b, _u=used, _p=pieces: (list(_u), list(_p), "sam2")
            got = {
                "CAL": await arm(detection, fetched, entry["plate"], entry["distance"]),
                "UNCAL": await arm(detection, fetched, None, None),
                "CAL_GEO": await arm(detection, fetched, entry["plate"], entry["distance"], blend=False),
                "UNCAL_GEO": await arm(detection, fetched, None, None, blend=False),
            }
            err = max(diff(got[a], rec["arms"][a]) for a in got)
            if best is None or err < best[0]:
                best = (err, pieces, got)
            if err < 0.051:
                break
        err, pieces, control = best
        vision._measured_areas = lambda d, r, b, _u=used, _p=pieces: (list(_u), list(_p), "sam2")

        mm2_cal, m_cal = frame_mm2(detection, fetched, entry["plate"], entry["distance"])
        mm2_unc, m_unc = frame_mm2(detection, fetched, None, None)
        k_true = (mm2_cal / mm2_unc) if (mm2_cal and mm2_unc) else None
        mm2_model, _ = _real_mm2(hint_for(detection, fetched, entry["plate"], entry["distance"]))

        arms = dict(control)
        arms["CAL_k1.2"] = await arm(detection, fetched, entry["plate"], entry["distance"], k=1.2)
        arms["CAL_k0.833"] = await arm(detection, fetched, entry["plate"], entry["distance"], k=1 / 1.2)
        arms["UNCAL_k1.2"] = await arm(detection, fetched, None, None, k=1.2)
        arms["CAL_MODELRATIO"] = await arm(detection, fetched, entry["plate"], entry["distance"], hough=False)
        if k_true:
            arms["UNCAL_TRUESCALE"] = await arm(detection, fetched, None, None, k=k_true)
        resolver.resolve_many = _blocked

        ref = next((p.reference for p in fetched if p.reference), None)
        out[name] = {
            "entry": entry, "control_max_abs_g": round(err, 3), "pieces": pieces,
            "frame_mm2": {"CAL": mm2_cal, "CAL_method": m_cal, "UNCAL": mm2_unc, "UNCAL_method": m_unc,
                          "CAL_model_ratio": mm2_model},
            "k_true_uncal": k_true,
            "hough_ratio": _plate(fetched[0].raw), "model_ratio": detection.get("plate_area_ratio"),
            "aspect": next((p.aspect for p in fetched if p.aspect), None),
            "card_frame_width_mm": (ref.frame_width_mm if ref else None),
            "arms": arms,
        }
        Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
        flag = "" if err <= CONTROL_TOLERANCE_G else "   <-- CONTROL FAILED"
        if flag:
            failed.append(name)
        print(f"{name:32s} control max|dg| {err:6.2f}  pieces {pieces}  k_true_uncal "
              f"{(round(k_true, 3) if k_true else None)}  ({m_cal} vs {m_unc}){flag}")
    print(f"written {args.out}")
    if failed:
        print(f"\nNOT REPRODUCED: {failed}. Nothing perturbed on these photographs is a "
              f"measurement of one term. See WHAT IT NEEDS in this file's header.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
