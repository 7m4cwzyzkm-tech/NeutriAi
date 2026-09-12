#!/usr/bin/env python3
r"""Does MEASURING each food's footprint beat asking the model for it?

    dev measure                 every weighed per-item photo
    dev measure 14              just one

WHY
---
The bench has now said the same thing three ways.

The model's `area_ratio` is not a usable number: across 22 weighed items it came
back larger than the item's own bounding box every single time, by 2x to 6x, and
never once smaller. So the rail was changed to land on the box instead, and the
per-item error nearly halved -- 44.7% to 25.0%. But the meal bias went from
-0.3% to -15.2%: everything is now systematically light.

Both of those are the same fact. `box x BBOX_FILL_CEILING` is being asked to
absorb two separate errors at once:

    how much of its box a food really fills      measured at 0.60 - 0.80
    how much smaller the model's box is than     photo 14's drumstick was
    the food's real box                          BOXED at 3% of frame while
                                                 its real footprint is 3.29%

The second one is physically impossible -- food cannot exceed its own bounding
box -- so the model's boxes are simply too small. Tuning BBOX_FILL_CEILING up
until the bias vanishes would land near 0.94, which is not a fill fraction any
real food has. It would be one wrong constant cancelling another, which is the
one kind of fix this codebase has agreed never to ship.

THE ALTERNATIVE
---------------
Stop asking, and measure -- but NOT from inside the box. Seeding GrabCut with
each box was tried and scored 50.9% against the model's own 38.3%: GrabCut only
looks inside the rectangle it is handed, so it inherited the undersizing and
measured it more precisely. Every method that starts from the box does.

So `food_seg` finds all the food FIRST, using no boxes at all -- the trick that
reproduced weighed meals at -3.0% and -0.0% in `dev seg` -- and only then asks
the boxes which pile is which. The box says WHERE, never HOW BIG.

This script settles whether that holds per item across the whole weighed bench.
It runs the real detector, then estimates each item TWICE from the same photo,
same scale, same height priors -- once with the model's area, once with the
measured one -- and prints both against the scale.

NOTHING HERE CHANGES THE PIPELINE. It is an instrument, like `dev seg`.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GRN, RED, YEL, DIM, HDR, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
PHOTOS = Path(__file__).resolve().parents[2] / "photos"

# (file, plate mm, weighed per item, camera distance mm)
CASES = [
    ("06-plate-meat-beans-rice.jpg", 254,
     {"chicken": 130, "refried beans": 60, "mexican rice": 120}, None),
    ("07-plate-4items-topdown.jpg", 254,
     {"spaghetti": 133, "mexican rice": 66, "refried beans": 79, "chicken": 148}, None),
    ("08-plate-mole-chicken-topdown.jpg", 254,
     {"mexican rice": 72, "refried beans": 109, "chicken": 146}, None),
    ("11-kebab-paper-card.jpg", None, {"beef": 335, "potatoes": 66, "tomatoes": 27}, 330),
    ("12-kebab-paper-nocard.jpg", None, {"beef": 335, "potatoes": 66, "tomatoes": 27}, 330),
    ("13-kebab-plate.jpg", 254, {"beef": 335, "potatoes": 66, "tomatoes": 27}, 330),
    ("14-plate-mole-chicken-card.jpg", 254,
     {"mexican rice": 72, "refried beans": 109, "chicken": 146}, None),
]

def measured_ratios(rgb, boxes, plate_bbox):
    """Every item's real footprint, as a fraction of the frame.

    Delegates to `food_seg`, which finds all the food first and only then asks
    the boxes which pile is which. The earlier version of this function handed
    each box to GrabCut and scored 50.9% against the model's own 38.3% -- worse,
    because GrabCut only looks inside the rectangle it is given and the model's
    rectangles are undersized. See food_seg's docstring for the measurements.
    """
    from app.services.ai.food_seg import measure_items
    try:
        return measure_items(rgb, boxes, plate_bbox)
    except Exception as exc:  # noqa: BLE001
        print(f"    {DIM}segmentation failed: {str(exc)[:90]}{OFF}")
        return [None] * len(boxes)


async def one(photo: str, plate, weights: dict, distance) -> list[tuple]:
    import numpy as np
    from PIL import Image, ImageOps

    from app.services.ai.food_seg import MEASURED_HEIGHTS_MM
    from app.services.ai.portion import (
        PROFILE_FACTORS, GeometryHint, _classify_shape, density_for,
        estimate_grams, mm2_per_frame,
    )
    from app.services.ai.vision import _plate_ellipse, detect_foods, downscale_jpeg
    from scripts.scan_bench import match

    path = PHOTOS / photo
    if not path.exists():
        print(f"  {RED}missing{OFF} {photo}")
        return []
    raw = path.read_bytes()
    prepared = downscale_jpeg(raw)
    detection = await detect_foods([prepared.b64], "measure-lab")
    items = [d for d in (detection.get("items") or []) if isinstance(d, dict)]
    if not items:
        print(f"  {RED}no detections{OFF} {photo}")
        return []

    ref = prepared.reference
    hint = GeometryHint(
        plate_ellipse_area_ratio=(detection.get("plate_area_ratio") or None),
        plate_diameter_mm=plate,
        depth_mm=distance,
        aspect_ratio=prepared.aspect,
        vessel=(str(detection.get("container")) if detection.get("container") else None),
        vessel_shape=detection.get("container_shape"),
        plate_ellipse_wh=_plate_ellipse(detection),
        reference_kind=(ref.kind if ref else None),
        reference_frame_width_mm=(ref.frame_width_mm if ref else None),
        reference_tilt_deg=(ref.tilt_deg if ref else None),
    )
    # The SAME pixels the model was shown, so a fraction of the frame means the
    # same thing to both sides of this comparison.
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((1280, 1280), Image.LANCZOS)
    rgb = np.asarray(img)

    frame_mm2, _scale_method = mm2_per_frame(hint)
    if not frame_mm2:
        frame_mm2 = 0.0
    plate_bbox = detection.get("plate_bbox")
    ratios = measured_ratios(rgb, [d.get("bbox") for d in items], plate_bbox)

    print(f"\n{HDR}{photo}{OFF}")
    if not plate_bbox:
        print(f"  {DIM}no plate_bbox reported — the food mask is unfenced here{OFF}")
    print(f"  {DIM}{'food':<26}{'model area':>11}{'measured':>10}"
          f"{'model g':>10}{'measured g':>12}{'weighed':>9}{OFF}")
    rows = []
    for det in items:
        name = str(det.get("name") or "food")
        hit = match(name, dict(weights))
        if not hit:
            continue
        _, truth = hit
        model_area = float(det.get("area_ratio") or 0.0)
        meas_area = ratios[items.index(det)]
        common = dict(
            hint=hint, shape_hint=det.get("shape"),
            density=density_for(name, None, det.get("food_group")),
            food_group=det.get("food_group"),
            ai_prior_grams=det.get("typical_serving_g"),
            detection_confidence=0.8, bbox=det.get("bbox"),
        )
        g_model = estimate_grams(name=name, area_ratio=model_area, **common).grams
        # The measured footprint is a MEASUREMENT, so it is passed with no box:
        # there is nothing left for the rail to correct.
        # A measured footprint needs the heights calibrated FOR a measured
        # footprint. The shipped priors were tuned against railed areas and
        # overshoot by 16-71% when handed an honest one -- so using them here
        # would test the wrong pair and blame the segmentation for it.
        g_meas = None
        if meas_area:
            shape = _classify_shape(name, det.get("shape"))
            g_meas = (meas_area * frame_mm2
                      * MEASURED_HEIGHTS_MM.get(shape, MEASURED_HEIGHTS_MM["default"])
                      * PROFILE_FACTORS[shape]
                      * density_for(name, None, det.get("food_group")) / 1000.0)

        def col(g):
            if g is None:
                return f"{'--':>9}"
            e = (g - truth) / truth * 100
            c = GRN if abs(e) < 15 else YEL if abs(e) < 35 else RED
            return f"{c}{g:6.0f} g{OFF}"
        print(f"  {name[:26]:<26}{model_area:>10.1%}"
              f"{(f'{meas_area:.1%}' if meas_area else '--'):>10}"
              f"  {col(g_model)}  {col(g_meas)}  {truth:6.0f} g")
        rows.append((photo, name, truth, g_model, g_meas))
    return rows


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="?", help="run one photo, by the number in its name")
    args = ap.parse_args()
    cases = CASES
    if args.only:
        cases = [c for c in CASES if c[0].startswith(args.only.zfill(2))]
        if not cases:
            print(f"{RED}no case starting {args.only}{OFF}")
            return 1

    print(f"\n{HDR}Measured footprint against the model's claim{OFF}  "
          f"{DIM}same photo, same scale, same height priors{OFF}")
    rows = []
    for photo, plate, weights, dist in cases:
        rows.extend(await one(photo, plate, weights, dist))

    scored = [r for r in rows if r[4] is not None]
    if not scored:
        print(f"\n{RED}nothing measured{OFF}")
        return 1
    me = sum(abs(r[3] - r[2]) / r[2] for r in scored) / len(scored) * 100
    mm = sum(abs(r[4] - r[2]) / r[2] for r in scored) / len(scored) * 100
    bm = sum((r[3] - r[2]) / r[2] for r in scored) / len(scored) * 100
    bn = sum((r[4] - r[2]) / r[2] for r in scored) / len(scored) * 100
    print(f"\n{'=' * 74}")
    print(f"  n={len(scored)} items measured on both sides\n")
    print(f"  {'':<22}{'mean abs':>10}{'bias':>10}")
    print(f"  {'model area (shipped)':<22}{me:>9.1f}%{bm:>9.1f}%")
    win = GRN if mm < me else RED
    print(f"  {'measured footprint':<22}{win}{mm:>9.1f}%{bn:>9.1f}%{OFF}")
    print(f"\n  {DIM}If the measured column wins, the box stops being an area estimate\n"
          f"  and goes back to being what it is good at -- saying WHERE to look.{OFF}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
