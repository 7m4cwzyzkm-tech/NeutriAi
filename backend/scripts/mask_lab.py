#!/usr/bin/env python3
"""Can we MEASURE the food instead of asking the model how big it is?

    dev mask              print the numbers
    dev mask --overlay    also write a mask picture per photo, to look at

This is an instrument. Nothing here runs in a scan and nothing here changes an
estimate.

WHY IT EXISTS

The estimator referees between two numbers the model reports -- the food's
share of the frame, and its bounding box -- and neither is a measurement. Four
different ways of refereeing have now been tried and scored on the weighed
bench:

    rule                          error   worst error   run-to-run move
    saw-tooth floor               19.7%       98%           108%
    pure box ceiling              21.2%       58%           100%
    box ceiling x 1.05            21.6%       66%           100%
    capped floor                  18.7%       64%            60%
    geometric blend               24.4%       46%            41%

Every one of them lands on the same mediocre trade-off curve: buy stability,
pay for it in accuracy. Sweeping the blend exponent from 0 to 1 traces the
curve out and never leaves it. That is what it looks like when the information
you need is not in your inputs, and no further arrangement of those two numbers
is going to produce it.

WHAT THIS MEASURES INSTEAD

`food_on_plate` learns the plate's own colour from its rim and calls food
whatever is not that colour. `measure_items` then divides the food between the
model's boxes using ONLY their centres.

The property that matters: the answer does not move when the model redraws a
box. Measured here, quadrupling and quartering every box on a photo:

    photo 14, box area x4  ->  refried beans +0%, rice +0%, drumstick +0%
    photo 13, box area x4  ->  skewer +0%, tomatoes +0%, potatoes +0%
    photo 07, box area x4  ->  all four items +0%

against the shipped rule, where a box drawn twice as wide is four times the
food. Two items on tonight's bench had their boxes redrawn between runs and
their weights doubled -- +93% on cherry tomatoes, +63% on refried beans. This
is what does not do that.

It leans on the model for exactly one number, the plate's outline, which is the
one thing measurement says it is good at: 50% of frame claimed against 53.1%
photographed.

WHAT IS NOT SETTLED

The mask is a colour rule, not a food segmenter, and it fails visibly. Run with
--overlay and look:

  * 07 and 14 are clean -- every food caught, plate and credit card excluded.
  * 13 is not. It counts the pool of sauce as food and loses the charred end of
    the skewer into shadow.

So this is not ready to size a portion. The question it is here to answer is
how often it is right, and whether its failures are the kind you can detect --
because a mask that is wrong in a way we can SEE is a fixable engineering
problem, and a model that redraws a box between two runs of the same photo is
not.

The plate outlines below were measured by hand off the photographs. In a scan
the model supplies them. Add photos as their plates get measured.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np                                   # noqa: E402
from PIL import Image, ImageOps                      # noqa: E402

from app.services.ai import food_seg                 # noqa: E402
from app.services.ai.portion import (                # noqa: E402
    GeometryHint, mm2_per_frame,
)

MAX_EDGE = 1568
PHOTOS = Path(__file__).resolve().parents[2] / "photos"
OUT = Path(__file__).resolve().parents[2] / "mask_overlays"
PLATE_MM = 254.0

# photo -> (plate box, [(item, weighed g, seed box)])
# Boxes are normalised {x, y, w, h}. The item boxes only ever contribute their
# CENTRES, which is why they can be this rough.
BENCH = {
    # NOTE: these are coordinates in the image AS THE PIPELINE LOADS IT, after
    # EXIF rotation. 07 and 13 are stored landscape with an orientation flag and
    # arrive portrait; measuring them off the file as it appears in a viewer
    # puts every box on its side. Cost an hour the first time.
    "07-plate-4items-topdown.jpg": (
        {"x": 0.045, "y": 0.100, "w": 0.895, "h": 0.730},
        [("refried beans",       79, {"x": 0.460, "y": 0.160, "w": 0.260, "h": 0.200}),
         ("mexican rice",        66, {"x": 0.620, "y": 0.270, "w": 0.300, "h": 0.280}),
         ("drumstick in mole",  148, {"x": 0.250, "y": 0.240, "w": 0.300, "h": 0.440}),
         ("spaghetti casserole",133, {"x": 0.400, "y": 0.530, "w": 0.430, "h": 0.230})],
    ),
    "13-kebab-plate.jpg": (
        {"x": 0.140, "y": 0.155, "w": 0.720, "h": 0.635},
        # The tomatoes and potatoes are interleaved -- their centres are 0.06
        # apart -- so nearest-seed cannot be expected to split them. Kept in
        # deliberately: it is the case that shows the limit of the method.
        [("meat skewer",        335, {"x": 0.520, "y": 0.280, "w": 0.320, "h": 0.370}),
         ("cherry tomatoes",     27, {"x": 0.310, "y": 0.355, "w": 0.240, "h": 0.095}),
         ("potatoes",            66, {"x": 0.330, "y": 0.290, "w": 0.250, "h": 0.310})],
    ),
    "14-plate-mole-chicken-card.jpg": (
        {"x": 0.285, "y": 0.205, "w": 0.660, "h": 0.570},
        [("refried beans",      109, {"x": 0.440, "y": 0.255, "w": 0.270, "h": 0.225}),
         ("mexican rice",        72, {"x": 0.680, "y": 0.290, "w": 0.240, "h": 0.240}),
         ("drumstick in mole",  146, {"x": 0.420, "y": 0.545, "w": 0.430, "h": 0.135})],
    ),
}


def load(path: Path) -> np.ndarray:
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    return np.array(img)


def frame_mm2(rgb: np.ndarray, plate: dict) -> float:
    """Frame area in mm2 -- through the SAME function a scan uses.

    This started out computing the scale itself, from the plate's longest axis
    in pixels. It was 15% off what the pipeline gets from the same plate, so the
    lab and the bench reported different grams for the same photograph and there
    was no way to tell which to believe. An instrument that measures a pipeline
    has to use the pipeline's own ruler.
    """
    h, w = rgb.shape[:2]
    # The plate's share of the frame: an ellipse inscribed in its box.
    area_ratio = plate["w"] * plate["h"] * math.pi / 4.0
    mm2, _method = mm2_per_frame(GeometryHint(
        plate_ellipse_area_ratio=area_ratio,
        plate_diameter_mm=PLATE_MM,
        plate_ellipse_wh=(plate["w"], plate["h"]),
        aspect_ratio=w / h,
    ))
    return float(mm2 or 0.0)


def _scaled(box: dict, f: float) -> dict:
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    bw, bh = box["w"] * f, box["h"] * f
    return {"x": cx - bw / 2, "y": cy - bh / 2, "w": bw, "h": bh}


def main() -> int:
    overlay = "--overlay" in sys.argv
    if overlay:
        OUT.mkdir(exist_ok=True)

    rows: list[tuple[str, str, float, float, float]] = []
    for name, (plate, items) in BENCH.items():
        path = PHOTOS / name
        if not path.exists():
            print(f"{name}: not found, skipped")
            continue
        rgb = load(path)
        mask = food_seg.food_on_plate(rgb, plate)
        if mask is None:
            print(f"{name}: the mask refused -- no measurement, which is the "
                  f"correct answer when it cannot see")
            continue

        boxes = [b for _, _, b in items]
        base = food_seg.measure_items(rgb, boxes, plate)
        big = food_seg.measure_items(rgb, [_scaled(b, 2.0) for b in boxes], plate)
        small = food_seg.measure_items(rgb, [_scaled(b, 0.5) for b in boxes], plate)

        mm2 = frame_mm2(rgb, plate)
        print(f"\n{name}")
        print(f"  plate {PLATE_MM:.0f} mm  ->  frame is {mm2/100:.0f} cm2   "
              f"food covers {mask.mean():.1%} of the frame")
        print(f"  {'item':22}{'weighed':>8}{'footprint':>11}{'mm2/g':>8}"
              f"{'box x2':>9}{'box x0.5':>10}")
        for (label, grams, _), a, b, s in zip(items, base, big, small):
            if a is None:
                print(f"  {label:22}{grams:8}      not measured")
                continue
            drift_b = "same" if b and abs(b / a - 1) < 0.005 else (
                f"{(b/a-1)*100:+.0f}%" if b else "lost")
            drift_s = "same" if s and abs(s / a - 1) < 0.005 else (
                f"{(s/a-1)*100:+.0f}%" if s else "lost")
            print(f"  {label:22}{grams:8}{a:10.2%}{a*mm2/grams:8.1f}"
                  f"{drift_b:>9}{drift_s:>10}")
            rows.append((name, label, grams, a * mm2, a * mm2 / grams))

        if overlay:
            out = rgb.copy()
            out[mask] = (0.45 * out[mask] + 0.55 * np.array([255, 40, 40])).astype(np.uint8)
            dest = OUT / f"{Path(name).stem}-mask.png"
            Image.fromarray(out).save(dest)
            print(f"  overlay -> {dest}")

    # The only test that matters: photograph one food twice and see whether
    # the measurement agrees with itself. Everything else on the bench is an
    # accuracy question; this is the consistency question.
    by_food: dict[str, list[tuple[str, float]]] = {}
    for photo, label, _grams, _mm2, per_g in rows:
        by_food.setdefault(label, []).append((photo[:2], per_g))
    repeats = {k: v for k, v in by_food.items() if len(v) > 1}
    if repeats:
        print("\nthe same food, photographed more than once")
        print(f"  {'food':22}" + "".join(f"{'photo '+p:>13}" for p, _ in
                                         next(iter(repeats.values()))) + f"{'apart':>9}")
        for label, seen in repeats.items():
            vals = [v for _, v in seen]
            spread = max(vals) / min(vals) - 1
            print(f"  {label:22}" + "".join(f"{v:11.1f}  " for v in vals)
                  + f"{spread*100:7.0f}%")
        print("  (mm2 of footprint per weighed gram -- one physical food should give")
        print("   one number, however far away the camera was)")

    if rows:
        ratios = [r[4] for r in rows]
        print(f"\n{'':2}{len(rows)} items   footprint per gram spans "
              f"{min(ratios):.0f} to {max(ratios):.0f} mm2/g "
              f"({max(ratios)/min(ratios):.1f}x across DIFFERENT foods, which is"
              f" the height and density difference between them and is expected)")
    if not overlay:
        print("\n    run  dev mask --overlay  to write the mask pictures and look at them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
