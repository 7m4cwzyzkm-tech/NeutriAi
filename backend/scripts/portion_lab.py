#!/usr/bin/env python3
"""Interactive bench for the pixel-to-gram estimator.

Runs entirely offline — no API keys, no database, no network. This is pure
geometry, so you can exercise it the moment you unzip the repo.

    python -m scripts.portion_lab                 # the standard reference set
    python -m scripts.portion_lab --ladder        # same food, all five rungs
    python -m scripts.portion_lab --sweep rice    # area sweep for one food
    python -m scripts.portion_lab --food "pad thai" --area 0.22 --plate 270
    python -m scripts.portion_lab --multi         # multi-angle reconciliation

Reading the output:
  method      which rung of the scale ladder was used (see docs/ARCHITECTURE.md)
  conf        confidence, capped by that rung's ceiling
  low–high    the honest band; it widens as the geometry gets worse
  notes       every adjustment the estimator made, and why
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai.portion import (  # noqa: E402
    DEFAULT_PLATE_DIAMETER_MM, DENSITY_G_ML, HEIGHT_PRIORS_MM, SHAPE_FACTORS,
    GeometryHint, band_label, density_for, estimate_grams, mm2_per_frame,
    reconcile_multi_image,
)

# Real plates, measured. Each tuple is (food, share of frame, what a kitchen
# scale says a normal restaurant serving of this weighs).
REFERENCE_SET = [
    ("grilled chicken breast", 0.10, "120–250 g"),
    ("jasmine rice",           0.14, "150–250 g cooked"),
    ("spaghetti bolognese",    0.22, "300–450 g"),
    ("caesar salad",           0.20, "80–200 g"),
    ("french fries",           0.15, "100–180 g"),
    ("tomato soup",            0.18, "300–450 g"),
    ("salmon fillet",          0.09, "120–200 g"),
    ("margherita pizza slice", 0.13, "100–150 g"),
    ("scrambled eggs",         0.11, "100–180 g"),
    ("broccoli florets",       0.08, "70–150 g"),
]

HDR = "\033[1m"; DIM = "\033[2m"; OFF = "\033[0m"
GRN = "\033[32m"; YEL = "\033[33m"; RED = "\033[31m"


def colour(conf: float) -> str:
    return GRN if conf >= 0.78 else (YEL if conf >= 0.55 else RED)


def show(label: str, e, expected: str = "") -> None:
    band = f"{e.grams_low:.0f}–{e.grams_high:.0f}"
    spread = (e.grams_high - e.grams_low) / max(e.grams, 1) * 100
    print(
        f"  {label:26s} {e.grams:7.1f} g   [{band:>11s}] ±{spread:4.0f}%   "
        f"{e.method:16s} {colour(e.confidence)}conf {e.confidence:.2f} "
        f"({band_label(e.confidence)}){OFF}"
        + (f"   {DIM}expect {expected}{OFF}" if expected else "")
    )
    for n in e.notes:
        print(f"      {DIM}· {n}{OFF}")


def reference_run(hint: GeometryHint) -> None:
    print(f"\n{HDR}Reference set — plate detected, 27 cm diameter, 55% of frame{OFF}")
    print(f"{DIM}  grams = frame_mm² × area_ratio × height_mm × shape_factor × density / 1000{OFF}\n")
    for food, area, expected in REFERENCE_SET:
        show(food, estimate_grams(name=food, area_ratio=area, hint=hint,
                                  detection_confidence=0.8), expected)


def ladder_run(food: str, area: float) -> None:
    """The same food through every rung, so you can see confidence degrade."""
    print(f"\n{HDR}Scale ladder — '{food}' at {area:.0%} of frame{OFF}")
    print(f"{DIM}  Each rung is a worse source of real-world scale than the one above it.{OFF}\n")
    rungs = [
        ("1. calibrated reference",
         GeometryHint(reference_area_mm2=57256, plate_ellipse_area_ratio=0.55)),
        ("1b. known plate diameter",
         GeometryHint(plate_diameter_mm=270, plate_ellipse_area_ratio=0.55)),
        ("2. depth sensor (350 mm)",
         GeometryHint(depth_mm=350)),
        ("3. multi-angle",
         GeometryHint(plate_ellipse_area_ratio=0.55, image_count=3)),
        ("4. assumed 27 cm plate",
         GeometryHint(plate_ellipse_area_ratio=0.55)),
        ("5. no geometry at all",
         GeometryHint()),
    ]
    for label, hint in rungs:
        e = estimate_grams(name=food, area_ratio=area, hint=hint,
                           ai_prior_grams=180, detection_confidence=0.8)
        show(label, e)


def sweep_run(food: str, hint: GeometryHint) -> None:
    print(f"\n{HDR}Area sweep — '{food}'{OFF}")
    print(f"{DIM}  Grams must rise monotonically with area, and clamp at 1500 g.{OFF}\n")
    for area in (0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.45, 0.60, 0.99):
        e = estimate_grams(name=food, area_ratio=area, hint=hint, detection_confidence=0.85)
        show(f"area {area:.0%}", e)


def multi_run(food: str, hint: GeometryHint) -> None:
    print(f"\n{HDR}Multi-angle reconciliation — '{food}'{OFF}\n")
    for title, areas in [
        ("angles AGREE (0.14 / 0.145 / 0.138)", [0.140, 0.145, 0.138]),
        ("angles DISAGREE (0.08 / 0.15 / 0.30)", [0.08, 0.15, 0.30]),
    ]:
        views = [estimate_grams(name=food, area_ratio=a, hint=hint,
                                detection_confidence=0.75) for a in areas]
        merged = reconcile_multi_image(views)
        print(f"  {HDR}{title}{OFF}")
        for i, v in enumerate(views):
            print(f"    view {i + 1}: {v.grams:7.1f} g  conf {v.confidence:.2f}")
        show("  → reconciled", merged)
        print()


def constraints_run() -> None:
    """The physical guards. These are the ones that stop absurd output."""
    print(f"\n{HDR}Geometry constraints{OFF}\n")
    plate = GeometryHint(plate_diameter_mm=270, plate_ellipse_area_ratio=0.55)

    print(f"  {HDR}a) Food cannot exceed the plate it sits on{OFF}")
    e = estimate_grams(name="rice", area_ratio=0.99, hint=plate, detection_confidence=0.9)
    show("area 99% vs plate 55%", e)
    print(f"     {DIM}Expect: capped to 0.55, confidence cut by 40%.{OFF}\n")

    print(f"  {HDR}b) Geometry vs the model's serving prior{OFF}")
    e = estimate_grams(name="rice", area_ratio=0.40, hint=plate,
                       ai_prior_grams=100, detection_confidence=0.9)
    show("geometry 4x the prior", e)
    print(f"     {DIM}Expect: geometric-mean blend, confidence × 0.85.{OFF}\n")

    print(f"  {HDR}c) Absolute rails (3 g – 1500 g){OFF}")
    show("tiny", estimate_grams(name="olive oil", area_ratio=0.0005, hint=plate))
    show("huge", estimate_grams(name="beef stew", area_ratio=0.54, hint=plate))
    print()

    print(f"  {HDR}d) Priors in play{OFF}")
    for shape in SHAPE_FACTORS:
        print(f"     {shape:9s} shape×{SHAPE_FACTORS[shape]:.2f}  "
              f"height {HEIGHT_PRIORS_MM[shape]:.0f} mm")
    print(f"\n     {DIM}Densities: {len(DENSITY_G_ML)} foods, default "
          f"{DENSITY_G_ML['default']} g/mL{OFF}")
    print(f"     {DIM}Tune these from real corrections — see docs/OPTIMIZATION.md{OFF}\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--food", help="single food name to test")
    p.add_argument("--area", type=float, default=0.15, help="share of frame, 0-1")
    p.add_argument("--plate", type=float, default=DEFAULT_PLATE_DIAMETER_MM,
                   help="plate diameter in mm")
    p.add_argument("--plate-ratio", type=float, default=0.55,
                   help="plate's share of the frame, 0-1")
    p.add_argument("--depth", type=float, help="camera distance in mm (ARKit)")
    p.add_argument("--ladder", action="store_true", help="show all five rungs")
    p.add_argument("--sweep", metavar="FOOD", help="area sweep for one food")
    p.add_argument("--multi", action="store_true", help="multi-angle reconciliation")
    p.add_argument("--constraints", action="store_true", help="physical guards")
    p.add_argument("--all", action="store_true", help="everything")
    a = p.parse_args()

    hint = GeometryHint(
        plate_ellipse_area_ratio=a.plate_ratio,
        plate_diameter_mm=a.plate,
        depth_mm=a.depth,
    )

    ran = False
    if a.food:
        print(f"\n{HDR}Single food{OFF}\n")
        print(f"  {DIM}density used: {density_for(a.food):.2f} g/mL   "
              f"scale source: {mm2_per_frame(hint)[1]}{OFF}\n")
        show(a.food, estimate_grams(name=a.food, area_ratio=a.area, hint=hint,
                                    detection_confidence=0.8))
        ran = True
    if a.sweep:
        sweep_run(a.sweep, hint); ran = True
    if a.ladder or a.all:
        ladder_run(a.food or "jasmine rice", a.area); ran = True
    if a.multi or a.all:
        multi_run(a.food or "jasmine rice", hint); ran = True
    if a.constraints or a.all:
        constraints_run(); ran = True
    if not ran:
        reference_run(hint)
        print(f"\n{DIM}Try --ladder, --multi, --constraints, or --all.{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
