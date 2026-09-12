#!/usr/bin/env python3
"""How much would measuring the food's area, instead of asking for it, be worth?

    dev seg

This is a measuring instrument, not part of the scan pipeline. Nothing here
runs in production and nothing here changes an estimate.

It exists because of a decision that should not be made on a hunch. Every plate
photo on the bench prints the same line -- the model's reported area is two to
three times its own bounding box -- and the estimator has to referee between
two numbers, neither of which is a measurement. Real segmentation (SAM 2,
FoodSAM) would replace both with a pixel mask. That is real work, so the
question to answer first is what it would buy.

The method here is deliberately crude: food is what is saturated or dark on a
pale, uniform background. It is not a food segmenter and it would not survive a
patterned plate. What it IS good enough for is one number -- roughly how much
of the frame the food really covers -- measured against a scale we trust, on
photographs of meals that were weighed.

Read the output as an upper bound on what better area measurement can give,
not as a segmenter worth shipping.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2                                              # noqa: E402
import numpy as np                                      # noqa: E402
from PIL import Image, ImageOps                         # noqa: E402
from app.services.ai.portion import (                   # noqa: E402
    DEFAULT_CAMERA_FOV_DEG, HEIGHT_PRIORS_MM, PROFILE_FACTORS,
    _classify_shape, density_for,
)
from app.services.ai.reference_cv import find_reference  # noqa: E402

MAX_EDGE = 1568
PHOTOS = Path(__file__).resolve().parents[2] / "photos"


def load(path: Path):
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    return img


def find_plate(img):
    """The plate's outline, as an ellipse, or None.

    A plate is the largest bright near-elliptical region in a food photo, and
    unlike the food it has a size the user can measure with a ruler. Its LONG
    axis is its true diameter at any camera angle -- a circle projects to an
    ellipse whose major axis is unforeshortened -- which is why the long axis
    is what gets returned.
    """
    rgb = np.asarray(img)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat, val = hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)
    bright = ((val > 140) & (sat < 80)).astype(np.uint8) * 255
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    frame_area = img.size[0] * img.size[1]
    for c in contours:
        if len(c) < 20:
            continue
        area = cv2.contourArea(c)
        if not (0.06 * frame_area < area < 0.90 * frame_area):
            continue
        (cx, cy), (d1, d2), angle = cv2.fitEllipse(c)
        major, minor = max(d1, d2), min(d1, d2)
        if minor <= 0 or major / minor > 2.2:          # too squashed to be a plate
            continue
        # The fitted ellipse should actually describe the region it came from.
        if abs(np.pi / 4 * d1 * d2 - area) / max(area, 1) > 0.35:
            continue
        if best is None or major > best[0]:
            best = (major, (cx, cy), (d1, d2), angle)
    return best


def scale_mm_per_px(img, distance_mm=None, plate_mm=None):
    """Millimetres per pixel, from a card, else a measured plate, else distance."""
    found = find_reference(img)
    if found:
        return found.mm_per_px, f"card ({found.consensus} settings agreed)"
    if plate_mm:
        plate = find_plate(img)
        if plate:
            return plate_mm / plate[0], f"{plate_mm:.0f} mm plate, found in pixels"
    if distance_mm:
        w, h = img.size
        long_side = 2.0 * distance_mm * np.tan(np.radians(DEFAULT_CAMERA_FOV_DEG) / 2)
        frame_w = long_side if w >= h else long_side * (w / h)
        return frame_w / w, f"{distance_mm:.0f} mm camera distance"
    return None, "no scale"


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """Fill enclosed holes in a binary mask, using OpenCV only.

    Flood the background inwards from a border of zeros; whatever the flood
    never reaches is enclosed, and therefore inside the shape.
    """
    h, w = mask.shape
    padded = np.zeros((h + 2, w + 2), np.uint8)
    padded[1:-1, 1:-1] = mask
    flood = padded.copy()
    cv2.floodFill(flood, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 255)
    holes = (flood[1:-1, 1:-1] == 0)
    return ((mask > 0) | holes).astype(np.uint8)


def food_mask(img):
    """Food is what is saturated or dark against a pale, uniform surface.

    Crude on purpose -- see the module docstring. Plates, paper and tablecloths
    are all pale and washed out; cooked food is neither.

    OpenCV only. scipy would be tidier and is not worth a dependency the
    backend does not otherwise need for a tool that never runs in production.
    """
    rgb = np.asarray(img)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat, val = hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)

    mask = (((sat > 70) & (val > 35) & (val < 245)) | (val < 80)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    mask = _fill_holes(mask)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((11, 11), np.uint8))

    # Drop specks. Real food arrives in a few large pieces.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.zeros_like(mask)
    floor = 0.002 * mask.size
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] > floor:
            keep[labels == i] = 1
    return keep.astype(bool)


def analyse(name, weighed_g, food_name, distance_mm=None, plate_mm=None,
            exclude_card=True):
    path = PHOTOS / name
    if not path.exists():
        return None
    img = load(path)
    w, h = img.size
    mm_px, how = scale_mm_per_px(img, distance_mm, plate_mm)
    if not mm_px:
        return None

    mask = food_mask(img)
    found = find_reference(img)      # also the only tilt reading these photos have
    if exclude_card and found:
        pts = np.array(found.corners)
        x0, y0 = int(max(0, pts[:, 0].min() - 20)), int(max(0, pts[:, 1].min() - 20))
        x1, y1 = int(pts[:, 0].max() + 20), int(pts[:, 1].max() + 20)
        mask[y0:y1, x0:x1] = False

    area_mm2 = float(mask.sum()) * mm_px * mm_px
    frame_mm2 = (w * mm_px) * (h * mm_px)
    shape = _classify_shape(food_name, None)
    prior_mm = HEIGHT_PRIORS_MM[shape]
    profile = PROFILE_FACTORS[shape]
    density = density_for(food_name, None, None)
    hpd = prior_mm * profile * density
    grams = area_mm2 * hpd / 1000.0

    # ---- the height back-solve ------------------------------------------
    #
    # No ruler needed, and no new photograph. The estimator is
    #
    #     grams = area x height x profile x density / 1000
    #
    # and on these photos three of those four are already known: the area was
    # just MEASURED off the pixels against a trusted scale, the density comes
    # from the food table, and the grams came off a kitchen scale. So the
    # height is the only unknown, and it can simply be solved for:
    #
    #     height = grams x 1000 / (area x profile x density)
    #
    # That is the height the food MUST have stood at for the weighed answer to
    # be true. Comparing it against HEIGHT_PRIORS_MM says directly whether the
    # prior table is too tall or too short, using data already in hand.
    #
    # The honest caveat: `weighed_g` is the whole meal and `density_for` is
    # taken from one representative food, so this is a meal-level effective
    # height, not a per-item one. It cannot say WHICH item on a mixed plate is
    # wrong. It can say whether the table as a whole leans, and by how much,
    # which is the question that has been open since USE_MEASURED_HEIGHT was
    # switched off.
    denom = area_mm2 * profile * density
    implied_mm = (weighed_g * 1000.0 / denom) if denom > 0 else None

    tilt = found.tilt_deg if found else None
    return {
        "name": name, "how": how, "mm_px": mm_px,
        "area_mm2": area_mm2, "share": area_mm2 / frame_mm2,
        "shape": shape, "hpd": hpd, "grams": grams,
        "prior_mm": prior_mm, "implied_mm": implied_mm, "tilt": tilt,
        "weighed": weighed_g, "err": (grams - weighed_g) / weighed_g * 100,
    }


# (photo, weighed total g, a name carrying the right shape, distance mm, plate mm)
#
# The plate meals are the honest half of this table. The taco and kebab shape
# priors were set from those two meals, so testing on them is partly circular;
# nothing here was ever fitted to 06, 07 or 08.
CASES = [
    ("10-tacos-paper-card.jpg",   133, "taco with scrambled eggs", None, None),
    ("11-kebab-paper-card.jpg",   428, "grilled meat skewer",      330, None),
    ("12-kebab-paper-nocard.jpg", 428, "grilled meat skewer",      330, None),
    ("13-kebab-plate.jpg",        428, "grilled meat skewer",      330, 254),
    ("06-plate-meat-beans-rice.jpg",  310, "mexican rice", None, 254),
    ("07-plate-4items-topdown.jpg",   426, "mexican rice", None, 254),
    ("08-plate-mole-chicken-topdown.jpg", 327, "mexican rice", None, 254),
]


def main() -> int:
    print("\n\033[1mWhat measured area is worth\033[0m  "
          "\033[2mfood footprint from pixels, against a trusted scale\033[0m\n")
    print("  %-26s %-22s %9s %8s %9s %8s" %
          ("photo", "scale from", "footprint", "of frame", "implies", "weighed"))
    rows = []
    for photo, weighed, food, dist, plate in CASES:
        r = analyse(photo, weighed, food, dist, plate)
        if not r:
            print("  %-26s (missing or unscaleable)" % photo)
            continue
        rows.append(r)
        colour = "\033[32m" if abs(r["err"]) < 20 else "\033[33m" if abs(r["err"]) < 40 else "\033[31m"
        print("  %-26s %-22s %8.0f mm2 %7.1f%% %8.0f g %7.0f g  %s%+.1f%%\033[0m" %
              (r["name"][:26], r["how"], r["area_mm2"], 100 * r["share"],
               r["grams"], r["weighed"], colour, r["err"]))
    if rows:
        mae = sum(abs(r["err"]) for r in rows) / len(rows)
        print("\n  \033[1mmean absolute %.1f%%\033[0m  \033[2mfrom measured area + current"
              " height priors, with no model area estimate involved\033[0m\n" % mae)

    # ---- what height would the weighed answer have required? -------------
    solved = [r for r in rows if r.get("implied_mm")]
    if solved:
        print("\033[1mThe height back-solve\033[0m  \033[2mmeasured area + weighed grams "
              "leave height as the only unknown, so solve for it\033[0m\n")
        print("  %-26s %-12s %8s %10s %8s %7s" %
              ("photo", "shape", "prior", "must be", "off by", "tilt"))
        for r in solved:
            ratio = r["implied_mm"] / r["prior_mm"] if r["prior_mm"] else 0.0
            colour = ("\033[32m" if 0.85 <= ratio <= 1.18 else
                      "\033[33m" if 0.6 <= ratio <= 1.7 else "\033[31m")
            tilt = ("%.0f deg" % r["tilt"]) if r["tilt"] is not None else "--"
            print("  %-26s %-12s %5.0f mm %7.0f mm  %s x%.2f\033[0m %7s" %
                  (r["name"][:26], r["shape"], r["prior_mm"], r["implied_mm"],
                   colour, ratio, tilt))
        lean = sum(r["implied_mm"] / r["prior_mm"] for r in solved) / len(solved)
        print("\n  \033[1mthe table leans x%.2f\033[0m  \033[2m(above 1.0 means the priors "
              "are too SHORT and the app under-feeds\n  every meal; below 1.0 means too "
              "tall. A number near 1.0 clears the height\n  table and puts the remaining "
              "error somewhere else.)\033[0m" % lean)
        print("\n  \033[2mMeal-level, not per-item: the weighed figure is the whole meal "
              "and the density\n  comes from one representative food. It says whether the "
              "table leans, not which\n  item on a mixed plate is wrong. Tilt is shown "
              "only where a card was found to\n  measure it from.\033[0m\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
