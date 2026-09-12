"""What the measured footprint is worth, measured offline against the scale.

WHAT THIS IS, AND WHAT IT IS NOT

It is not `dev benchall`. That needs the vision model, and there are no API keys
in this container. This runs the parts that do not need one:

  hand annotation   the plate's box and each food's box, read off the photograph
                    against a 10% grid. A real measurement, stated as one.
  the pixels        food_seg's colour rule, offline, for each food's footprint.
  the geometry      estimate_grams, with the plate's known diameter.
  the scale         the weighed grams already in bench_all's table.

The comparison is BEFORE vs AFTER on the one term this build changed:

  before   area = the bounding-box rail's ceiling. That is where every plate
           photo lands today, because the model's area claim exceeded its own
           box on 20 of 22 bench items, so the rail always fires.
  after    area = the footprint measured from the pixels.

Everything else is held: same name, same density, same height prior, same
profile, same plate. Two honest caveats, both in the conservative direction:

  1. The boxes here are HAND-drawn and tight. The model's are quantised to a
     0.05 grid and measured undersized -- a real 4.84% footprint boxed at 4.00%.
     So the "before" here is BETTER than what ships, and the improvement this
     prints is a floor, not a ceiling.
  2. The footprints come from the colour rule, not SAM2. The shipped code
     refuses colour footprints as areas for good reason. They stand in here to
     measure the ARITHMETIC, not to endorse the mask.

No serving prior is passed, so nothing blends toward a typical portion. This is
geometry against a kitchen scale, which is what the question was.
"""
import pathlib
import sys, math
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image, ImageOps

from app.services.ai import food_seg
from app.services.ai.portion import (
    GeometryHint, estimate_grams, BBOX_FILL_CEILING, BOX_TIGHTNESS,
)

GRN, YEL, RED, DIM, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"

# file, plate diameter mm, plate box, [(name, food_group, shape, box, weighed_g)]
# file, plate diameter mm, plate box, [(name, food_group, shape, box, weighed_g)]
#
# Annotated on the EXIF-ROTATED image, which is what the pipeline sees. The
# first pass of this was annotated on the un-rotated thumbnail and every number
# it printed was void -- the photographs are portrait on disk and landscape on
# screen, and nothing about a wrong-orientation box looks wrong in a table.
#
# The check that caught it, and that every box below passes: these are top-down
# plates, so the plate's pixel width and height must come out within a few
# percent of each other. They do -- 560x570, 507x500, 449x443, 486x484.
SCENES = [
    ("16-rajas-rice-nocard.jpg", 225.4,
     {"x": 0.095, "y": 0.205, "w": 0.790, "h": 0.590}, [
        ("rajas con crema", "vegetable", "mound",
         {"x": 0.360, "y": 0.265, "w": 0.470, "h": 0.320}, 98),
        ("mexican rice", "grain", "mound",
         {"x": 0.200, "y": 0.400, "w": 0.430, "h": 0.330}, 77),
     ]),
    ("15-rajas-rice-card.jpg", 225.4,
     {"x": 0.140, "y": 0.290, "w": 0.730, "h": 0.540}, [
        ("rajas con crema", "vegetable", "mound",
         {"x": 0.330, "y": 0.335, "w": 0.460, "h": 0.325}, 98),
        ("mexican rice", "grain", "mound",
         {"x": 0.200, "y": 0.485, "w": 0.400, "h": 0.305}, 77),
     ]),
    ("07-plate-4items-topdown.jpg", 254.0,
     {"x": 0.055, "y": 0.120, "w": 0.910, "h": 0.695}, [
        ("refried beans", "legume", "mound",
         {"x": 0.420, "y": 0.175, "w": 0.300, "h": 0.175}, 79),
        ("mexican rice", "grain", "mound",
         {"x": 0.600, "y": 0.280, "w": 0.340, "h": 0.270}, 66),
        ("spaghetti", "grain", "mound",
         {"x": 0.370, "y": 0.550, "w": 0.460, "h": 0.220}, 133),
        ("mole chicken leg", "protein", "mound",
         {"x": 0.260, "y": 0.300, "w": 0.260, "h": 0.420}, 148),
     ]),
    ("08-plate-mole-chicken-topdown.jpg", 254.0,
     {"x": 0.100, "y": 0.145, "w": 0.825, "h": 0.610}, [
        ("mexican rice", "grain", "mound",
         {"x": 0.300, "y": 0.200, "w": 0.400, "h": 0.200}, 72),
        ("refried beans and egg", "legume", "mound",
         {"x": 0.190, "y": 0.385, "w": 0.270, "h": 0.180}, 109),
        ("mole chicken leg", "protein", "mound",
         {"x": 0.500, "y": 0.300, "w": 0.250, "h": 0.360}, 146),
     ]),
]


def load(name):
    img = ImageOps.exif_transpose(Image.open(str(pathlib.Path(__file__).resolve().parents[2] / "photos" / name))).convert("RGB")
    img.thumbnail((1568, 1568), Image.LANCZOS)   # the same edge the detector sees
    return np.array(img)


def hint_for(pb, diameter_mm):
    """A plate_reference hint built from the HAND-measured ellipse.

    The plate's ellipse area as a fraction of the frame, from the annotation --
    not from a model box on a 0.05 grid. Both the before and after runs use it,
    so it cannot favour either.
    """
    return GeometryHint(
        plate_diameter_mm=diameter_mm,
        plate_ellipse_area_ratio=math.pi / 4 * pb["w"] * pb["h"],
    )


def run(only=None):
    before_errs, after_errs = [], []
    rows = []
    scenes = [s for s in SCENES
              if not only or any(s[0].startswith(pfx) for pfx in only)]
    if not scenes:
        print(f"{RED}no photo matches {only}{OFF}  "
              f"{DIM}try: {', '.join(s[0][:2] for s in SCENES)}{OFF}")
        return 1
    for fname, dia, pb, items in scenes:
        rgb = load(fname)
        boxes = [it[3] for it in items]
        measured, source = food_seg.measure_items_with_source(rgb, boxes, pb)
        hint = hint_for(pb, dia)
        print(f"\n{fname}   {DIM}plate {dia:.0f} mm, "
              f"{hint.plate_ellipse_area_ratio:.1%} of frame, masks from {source}{OFF}")
        for (name, group, shape, box, weighed), area in zip(items, measured):
            box_ratio = box["w"] * box["h"]
            railed = box_ratio * BBOX_FILL_CEILING * BOX_TIGHTNESS
            common = dict(name=name, hint=hint, shape_hint=shape, bbox=box,
                          food_group=group, detection_confidence=0.8)
            # BEFORE: the area the rail lands on, which is where every plate
            # photo lands today.
            b = estimate_grams(area_ratio=railed, **common)
            if area is None:
                print(f"  {name[:24]:<24}  {RED}not measured{OFF}")
                continue
            # AFTER: the footprint measured off the pixels.
            a = estimate_grams(area_ratio=railed, measured_area_ratio=area, **common)
            be = (b.grams - weighed) / weighed * 100
            ae = (a.grams - weighed) / weighed * 100
            before_errs.append(abs(be)); after_errs.append(abs(ae))
            col = GRN if abs(ae) <= 10 else YEL if abs(ae) <= 20 else RED
            print(f"  {name[:24]:<24} {weighed:>4.0f} g   "
                  f"box {box_ratio:>5.1%}->{railed:>5.1%}  {b.grams:>6.1f} g {be:>+7.1f}%   "
                  f"{DIM}|{OFF}  measured {area:>5.1%}  {col}{a.grams:>6.1f} g {ae:>+7.1f}%{OFF}")
            rows.append((fname, name, weighed, b.grams, a.grams, box_ratio, area))
    def mean(xs): return sum(xs) / len(xs) if xs else float("nan")
    print(f"\n{'=' * 78}")
    print(f"  n = {len(after_errs)} items")
    print(f"  before (box rail)   mean absolute error  {mean(before_errs):5.1f}%")
    print(f"  after  (measured)   mean absolute error  {mean(after_errs):5.1f}%")
    gap = mean(before_errs) - mean(after_errs)
    verdict = (f"{GRN}measuring the footprint improves it by {gap:.1f} points{OFF}"
               if gap > 0 else
               f"{RED}measuring the footprint made it WORSE by {-gap:.1f} points{OFF}")
    print(f"  {verdict}")
    print(f"  {'MEETS' if mean(after_errs) <= 10 else 'does not meet'} the 10% target")
    print(f"{'=' * 78}")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="+", metavar="PREFIX",
                    help="only these photos, by filename prefix (e.g. --only 16 08)")
    raise SystemExit(run(ap.parse_args().only))
