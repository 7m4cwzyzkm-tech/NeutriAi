"""What a footprint actually weighs, measured against the kitchen scale.

THE QUESTION THIS ANSWERS

Once a footprint is measured, the weight is decided by two numbers we do not
measure at all:

    grams = footprint_mm2 x height_mm x profile x density / 1000
                            ^^^^^^^^^   ^^^^^^^^^^^^^^^^^
                            a table     a table

On the weighed carrots the tables say 21 mm and 0.85. Solved against the scale,
the same photograph implies 11 mm at that density. Two guesses multiply, and
their product sets the answer -- so their product is what has to be measured.

It is measurable, and this is the only file that measures it. From a footprint
and a weight:

    grams per mm2 = weighed_grams / footprint_mm2

One number per food, straight off the scale, with no density table and no
height prior in it. That is what this prints.

TWO INDEPENDENT FOOTPRINTS, ALWAYS BOTH

SAM2 returns the plate with the food punched OUT of it, and separately one mask
per piece of food. So the footprint can be measured twice from a single call:

    union    the per-piece masks that sit on the plate, unioned
    holes    the plate mask's own holes, which are the food's outline

They come from different parts of the same answer, so when they agree the
footprint is real, and when they disagree neither is trusted. On the weighed
carrots: union 6.27% of frame, holes 6.65% -- 6% apart.

The holes matter beyond the cross-check. On nine of twenty bench items the
per-piece masks were treated as absent and the item fell back to the model's
plate-coverage claim. The plate mask was there the whole time with the food's
shape cut out of it. A hole is not a missing measurement.

COST. One call per photograph, about 2 cents, and every mask is cached to
mask_overlays/ on the way past. A second run reads the cache and is free.
Pass --fresh to pay again deliberately.
"""
import argparse
import ast
import math
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np

from scripts import _mask_cache

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHOTOS = ROOT / "photos"
CACHE = ROOT / "mask_overlays"

DIM, HDR, GRN, YEL, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[33m", "\033[31m", "\033[0m")

# Six predictions a minute with a burst of one, for an account under $5 of
# credit. Eleven seconds between calls keeps us under it without thinking
# about it; a 429 still gets three tries because the limit resets on a window.
PACE_S = 11.0
RETRY_SLEEP_S = 30.0
RETRIES = 3

# How closely the two footprints must agree before either is believed.
AGREE_MIN = 0.70

# Every plate correctly identified here measured 1.00-1.01. Every mask wrongly
# taken for a plate measured 1.33-1.56. There is nothing in between.
MAX_PLATE_ASPECT = 1.25
# Largest hole as a share of its own mask. Plates topped out at 0.32 across
# every cached photograph, tables started at 0.51.
MAX_HOLE_OVER_MASK = 0.35


def bench_all_plate_diameters() -> dict[str, float | None]:
    """{photo filename: declared plate diameter mm}, one entry per row of
    bench_all.CASES -- read from bench_all.py's own SOURCE, not imported.

    `import scripts.bench_all` would run that module's top level, which pulls
    in `app.config` (reads backend/.env), `scripts.scan_bench` and
    `app.services.ai.segment_hosted` (an HTTP client, `httpx`) -- all fine for
    a script that runs the bench for real, wrong for a script that only wants
    six numbers out of a literal list. Parsing the source with `ast` and
    `ast.literal_eval` never executes any of bench_all.py's imports, so this
    is the same numbers with none of that risk -- the same technique
    test_wiring.py's own reachability check already uses, for the same
    reason ("read from the SOURCE rather than from a live import graph").
    `literal_eval` also refuses anything that is not a plain literal, so a
    CASES row that ever stopped being a static tuple would raise here rather
    than silently executing something.
    """
    path = pathlib.Path(__file__).resolve().parent / "bench_all.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CASES" for t in node.targets
        ):
            cases = ast.literal_eval(node.value)
            return {row[0]: row[1] for row in cases}
    raise RuntimeError(f"CASES not found in {path}")


# ONE FOOD ON THE PLATE, EACH WEIGHED ALONE.
#
# Only single-food photographs are here, and deliberately. With one food on the
# plate the footprint needs no bounding box from the vision model: everything
# on the plate is the food. That removes the model's 0.05-grid box -- the
# largest error term in the bench -- from the measurement entirely, so what is
# left is the segmenter and the scale.
#
# Grams are from bench_all's table, which is this user's own kitchen scale with
# the plate tared off. The plate diameter is ALSO from bench_all's table --
# looked up by filename via bench_all_plate_diameters() above -- rather than
# copied here a second time: two typed copies of the same number is exactly
# how rows 40-45 sat at a stale 222 mm here for days after bench_all.py was
# corrected to 217 (measured 21 Sep 2026; see bench_all.py and HANDOFF.md).
# `test_height_fit_plates_match_bench_all.py` fails if this ever drifts from
# bench_all.py again.
_PLATE_MM = bench_all_plate_diameters()

# (filename, food name, weighed grams) -- the two fields bench_all does not
# carry in this shape: its weight is bundled into one "food=grams" string
# (parsed by parse_actuals for the bench's own scoring), and it has no
# per-food split for the reason this file exists -- only single-food photos
# are listed here at all, so no split is needed.
_ROWS = [
    ("17-carrots-plate.jpg",          "steamed carrots",     58),
    ("18-zucchini-plate.jpg",         "steamed zucchini",    74),
    ("20-spaghetti-plate.jpg",        "spaghetti with sauce", 253),
    ("21-bbq-chicken-plate.jpg",      "bbq chicken thigh",   159),
    ("22-roast-beef-plate.jpg",       "roast beef",           83),
    ("23-brussels-sprouts-plate.jpg", "brussels sprouts",     65),
    ("24-macaroni-salad-plate.jpg",   "macaroni salad",       89),
    ("25-smashed-potatoes-plate.jpg", "smashed potatoes",    136),
    ("26-potroast-rice-alone.jpg",    "white rice",           69),
    ("27-potroast-beef-alone.jpg",    "pot roast",           114),
    ("28-potroast-bread-alone.jpg",   "dinner roll",          42),
    ("30-caesar-salad-plate.jpg",     "caesar salad",        123),
    ("34-pizza-slice-plate.jpg",      "pizza slice",         120),

    # THE HEAP-VS-LAYER PAIRS -- same food, same weight, two arrangements.
    # A different plate: nominal 8.75 in / 222 mm, never tape-measured.
    # MEASURED 21 Sep 2026 (photo, ruler across the rim, +/-3 mm): outer
    # diameter 217 mm, rim height 20 mm, flat inner floor 155 mm, rim uniform
    # all round. 217, not the 229 above -- looked up from bench_all, below.
    ("40-trailmix-spread.jpg",        "trail mix",            44),
    ("41-trailmix-heaped.jpg",        "trail mix",            44),
    ("42-chips-spread.jpg",           "tortilla chips",       25),
    ("43-chips-heaped.jpg",           "tortilla chips",       25),
    ("44-grapes-spread.jpg",          "grapes",               90),
    ("45-grapes-cluster.jpg",         "grapes",               90),
]

WEIGHED = [(name, _PLATE_MM[name], food, grams) for name, food, grams in _ROWS]


def _bbox(m):
    ys, xs = np.nonzero(m)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _fill(m):
    """The mask with its holes closed, via its outer contours."""
    import cv2
    u8 = m.astype(np.uint8) * 255
    cnts, _ = cv2.findContours(u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = np.zeros_like(u8)
    cv2.drawContours(out, cnts, -1, 255, cv2.FILLED)
    return out > 0


def pick_plate(masks, shape):
    """The plate: round, not running off the frame, and holed like a plate.

    THE RULE THAT LOOKED BETTER AND WAS WORSE. An earlier version of this
    relaxed the roundness limit to 1.70 and replaced the frame test with the
    hole signature below, to rescue four photographs reported as "no plate
    found". It rescued nothing. On 26 and 30 it took the TABLETOP as the plate
    and the CREDIT CARD lying on it as the food, and
    those two wrong answers agreed with each other to 97% and 94%. Rendering
    the masks over the photographs is what caught it; the agreement number
    never would, because two consistent measurements of the wrong object are
    still consistent.

    So the limits stay tight and "no plate found" stays the answer when the
    plate is not clearly there. A refusal costs one photograph. A confident
    wrong footprint poisons the fit that every future weight is built on.

    Three tests, all of which must pass:
      round        aspect <= 1.25. Every correct plate here measured 1.00-1.01;
                   every wrong pick measured 1.33-1.56.
      in frame     a mask spanning the frame is the tabletop.
      holed right  the plate has the FOOD punched out of it -- small holes.
                   The tabletop has the PLATE punched out of it: one hole as
                   big as the mask. Plates topped out at 0.32 here, tables
                   started at 0.51.
    """
    import cv2
    H, W = shape
    best = None
    for m in masks:
        f = float(m.mean())
        if not (0.08 <= f <= 0.60):
            continue
        x0, y0, x1, y1 = _bbox(m)
        hh, ww = y1 - y0 + 1, x1 - x0 + 1
        if hh > 0.95 * H or ww > 0.95 * W:
            continue                                  # runs off the frame
        if max(hh, ww) / min(hh, ww) > MAX_PLATE_ASPECT:
            continue                                  # a plate is round
        hole = _fill(m) & ~m
        n, _l, stats, _c = cv2.connectedComponentsWithStats(hole.astype(np.uint8), 8)
        areas = [int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n)]
        if areas and max(areas) / float(m.sum()) > MAX_HOLE_OVER_MASK:
            continue                                  # the tabletop
        if best is None or f > float(best.mean()):
            best = m
    return best


def footprints(masks, shape):
    """Both measurements, as a share of the plate. (union, holes, n_masks)."""
    plate = pick_plate(masks, shape)
    if plate is None:
        return None
    pf = float(plate.mean())
    filled = _fill(plate)
    plate_px = float(filled.sum()) or 1.0

    holes = float((filled & ~plate).sum()) / plate_px

    union = np.zeros(shape, bool)
    taken = 0
    for m in masks:
        if m is plate:
            continue
        if float(m.mean()) >= pf * 0.90:
            continue                                  # the plate, or the table
        ys, xs = np.nonzero(m)
        cy, cx = int(ys.mean()), int(xs.mean())
        if not filled[cy, cx]:
            continue                                  # not on the plate
        if not m[cy, cx]:
            continue                                  # a ring, not a piece
        if float((m & filled).sum()) / float(m.sum()) < 0.85:
            continue                                  # spills off = rim arc
        union |= m
        taken += 1
    return (float((union & filled).sum()) / plate_px, holes, taken,
            filled, union)


def _photo(name):
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(PHOTOS / name)).convert("RGB")
        im.thumbnail((1280, 1280), Image.LANCZOS)
        return np.asarray(im)
    except Exception:                                 # noqa: BLE001
        return None


def draw(name, arr, plate_filled, union):
    """The measurement painted onto the photograph, so it can be looked at.

    THIS IS NOT DECORATION. The agreement column passed two footprints that
    were both the credit card, on two photographs, at 97% and 94%. Nothing in
    the numbers said so. Painting the plate blue and the food red and looking
    at it said so immediately. Any run that changes the selection rule gets
    looked at before its numbers are believed.
    """
    from PIL import Image
    out = arr.copy()
    sel = plate_filled & ~union
    out[sel] = (0.45 * out[sel] + 0.55 * np.array([0, 120, 255])).astype(np.uint8)
    out[union] = (0.35 * out[union] + 0.65 * np.array([255, 30, 30])).astype(np.uint8)
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"fit-{name.rsplit('.', 1)[0]}.png"
    im = Image.fromarray(out)
    im.thumbnail((900, 900), Image.LANCZOS)
    im.save(path)
    return path


# SHOW SAM2 THE PLATE, NOT THE TABLECLOTH.
#
# The automatic generator seeds a 32x32 grid -- 1,024 points -- over the WHOLE
# frame. The plate is 33-42% of it, so roughly 600 of those points land on the
# tablecloth, and SAM2 does exactly what it is asked and segments the pattern.
# On the weighed grapes it returned 15 masks and not one was a grape.
#
# It is not failing. It is answering a question about the wrong picture.
#
# Cropping to the plate was impossible before the Hough rim detector, because
# on precisely these backgrounds no plate could be found. Now it can:
#   a circle inscribed in its own box is 78% of that box, so nearly every seed
#   lands on plate or food instead of 40%; the pattern is not in the input at
#   all; and rescaling the crop back up puts 3-4x more pixels on the food.
# Same single call, same cost. Only the bytes change.
#
# This lives HERE and not in the product on purpose. It is a hypothesis with a
# mechanism, not a measurement, until the numbers on these same photographs say
# otherwise -- they are already cached, so it is a direct before-and-after.
CROP = False
CROP_MARGIN = 0.04
CROP_LONG_EDGE = 1280

# THIS SCRIPT'S OWN DECODE SIZE, and it is NOT the scan path's 1568.
# Recorded in every cache file it writes so the difference can never
# again be invisible. The height constants were fitted on masks at
# this size against footprints production produces at 1568.
LONG_EDGE = 1280


def _plate_crop(rgb):
    """(y0, y1, x0, x1) around the plate, or None if there is no plate."""
    from app.services.ai import food_seg

    plate, _src = food_seg.plate_surface(rgb, None)
    if plate is None or not plate.any():
        return None
    ys, xs = plate.nonzero()
    H, W = plate.shape
    h, w = int(ys.max() - ys.min() + 1), int(xs.max() - xs.min() + 1)
    my, mx = int(h * CROP_MARGIN), int(w * CROP_MARGIN)
    return (max(0, int(ys.min()) - my), min(H, int(ys.max()) + 1 + my),
            max(0, int(xs.min()) - mx), min(W, int(xs.max()) + 1 + mx))


def _masks_back(masks, crop, shape):
    """Masks measured on the crop, put back where they came from."""
    import cv2
    y0, y1, x0, x1 = crop
    H, W = shape
    out = []
    for m in masks:
        small = cv2.resize(m.astype(np.uint8), (x1 - x0, y1 - y0),
                           interpolation=cv2.INTER_NEAREST)
        full = np.zeros((H, W), np.uint8)
        full[y0:y1, x0:x1] = small
        out.append(full.astype(bool))
    return out


def masks_for(name, fresh):
    """Cached masks if we have them, otherwise one paid call, then cached."""
    # A DIFFERENT FILE WHEN CROPPED, AND THIS MATTERS TWICE OVER.
    #
    # Reading the uncropped cache would make --crop silently do nothing, and
    # WRITING over it would destroy the before-numbers this experiment exists
    # to be compared against. Both were in the first version of this.
    stem = name.rsplit('.', 1)[0] + ("-crop" if CROP else "")
    # NAMESPACED, AND THE RESOLUTION IS PART OF THE NAME.
    #
    # This script decodes at 1280 and `mask_stability` decodes at 1568, the
    # scan path's size, and both used to write `masks-<stem>.npz`. Last writer
    # won, silently, and the cache on disk still shows four resolutions under
    # one naming scheme. See scripts/_mask_cache.py.
    path = _mask_cache.path_for(CACHE, _mask_cache.HEIGHT_FIT, stem, LONG_EDGE)
    if path.exists() and not fresh:
        got, meta = _mask_cache.load(path, expect_long_edge=LONG_EDGE)
        if got is None:
            print(f"    {meta}")
        else:
            return got, "cached"
    stale = [f.name for f in _mask_cache.legacy_files(CACHE) if stem in f.name]
    if stale and not fresh:
        print(f"    ignoring un-namespaced {', '.join(stale)} -- no recorded "
              f"resolution, so it cannot be told from mask_stability's 1568")

    from PIL import Image, ImageOps
    from app.services.ai import food_seg, segment_hosted

    seg = segment_hosted.from_settings()
    if type(seg).__name__ == "NullSegmenter":
        return None, "no segmenter configured"
    im = ImageOps.exif_transpose(Image.open(PHOTOS / name)).convert("RGB")
    im.thumbnail((1280, 1280), Image.LANCZOS)
    arr = np.asarray(im)
    H, W = arr.shape[:2]

    full_shape = (H, W)
    crop = _plate_crop(arr) if CROP else None
    if crop is not None:
        y0, y1, x0, x1 = crop
        sub = Image.fromarray(arr[y0:y1, x0:x1])
        scale = CROP_LONG_EDGE / max(sub.size)
        if scale > 1.0:
            sub = sub.resize((int(sub.width * scale), int(sub.height * scale)),
                             Image.LANCZOS)
        arr = np.asarray(sub)
        H, W = arr.shape[:2]
    seed = (W // 2, H // 2)
    try:
        plate, _ = food_seg.plate_surface(arr, None)
        if plate is not None and plate.any():
            ys, xs = np.nonzero(plate)
            seed = (int(xs.mean()), int(ys.mean()))
    except Exception:                                 # noqa: BLE001
        pass
    # PACE, AND RETRY THE RATE LIMIT.
    #
    # Replicate throttles an account under $5 of credit to SIX predictions a
    # minute with a burst of one. The first run of this file fired as fast as
    # it could and lost four of thirteen photographs to HTTP 429 -- reported as
    # "no masks returned", which reads exactly like the segmenter going silent
    # and is nothing of the kind. A rate limit is not a measurement failure and
    # must never be counted as one.
    masks = None
    err = "no masks returned"
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(RETRY_SLEEP_S)
        try:
            seg._auto_memo = None
            seg.segment(arr, [seed])
        except Exception as exc:                      # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            continue
        got = getattr(seg, "_auto_memo", None)
        if got and got[1]:
            masks = list(got[1])
            break
        err = getattr(seg, "last_error", None) or "no masks returned"
        if "429" not in str(err):
            break                                     # not the limit; real
    if masks is None:
        return None, err
    if crop is not None:
        masks = _masks_back(masks, crop, full_shape)
    time.sleep(PACE_S)                                # stay under the limit
    _mask_cache.save(path, masks, writer=_mask_cache.HEIGHT_FIT,
                     long_edge=LONG_EDGE, shape=full_shape, seed=seed)
    return masks, "paid"


def run(fresh, only, overlay):
    rows = [r for r in WEIGHED if not only or r[0].split("-")[0] in only]
    missing = [r[0] for r in rows if not (PHOTOS / r[0]).exists()]
    if missing:
        print(f"\n  Cannot run: {len(missing)} weighed photo(s) missing from {PHOTOS}")
        for n in missing:
            print(f"    {n}")
        print("  This is not passing, it is not running.\n")
        return 2
    if not rows:
        print("  nothing selected")
        return 2

    from app.services.ai.food_seg import MEASURED_HEIGHTS_MM
    from app.services.ai.portion import _classify_shape, density_for

    print(f"\n{HDR}Footprint against the scale{OFF}  "
          f"{DIM}{len(rows)} single-food weighed photographs{OFF}\n")
    print(f"  {'photo':<32}{'g':>5}{'union':>8}{'holes':>8}{'agree':>8}"
          f"{'mm2':>8}{'g/mm2':>9}")

    out = []
    for name, plate_mm, food, grams in rows:
        masks, how = masks_for(name, fresh)
        if masks is None:
            print(f"  {name:<32}{grams:>5}  {RED}{how}{OFF}")
            continue
        got = footprints(masks, masks[0].shape)
        if got is None:
            print(f"  {name:<32}{grams:>5}  {YEL}no plate found{OFF}")
            continue
        union, holes, n, filled, umask = got
        plate_mm2 = math.pi * (plate_mm / 2.0) ** 2
        # The two must agree before either is used. Ratio, not difference,
        # because these are areas spanning an order of magnitude.
        hi, lo = max(union, holes), min(union, holes)
        agree = (lo / hi) if hi > 0 else 0.0
        # WHEN THEY DISAGREE, NEITHER IS USED.
        #
        # The first run took max(union, holes) on a disagreement and fed it to
        # the fit. The spaghetti agreed 0% -- union 0.0%, holes 1.6% -- and its
        # 654 mm2 "footprint" entered the table as 0.387 g/mm2 and a 368 mm
        # tall pile of pasta, forty times anything else measured here. A
        # cross-check that still reports a number when it fails is not a
        # cross-check. It is dropped, loudly, and the fit never sees it.
        if agree < AGREE_MIN:
            print(f"  {name:<32}{grams:>5}{union:>7.1%}{holes:>8.1%}"
                  f"{RED}{agree:>8.0%}{OFF}   {DIM}dropped -- the two "
                  f"measurements disagree{OFF}")
            continue
        area = plate_mm2 * (union + holes) / 2.0
        if area <= 0:
            print(f"  {name:<32}{grams:>5}  {YEL}no footprint{OFF}")
            continue
        drawn = ""
        if overlay:
            arr = _photo(name)
            if arr is not None and arr.shape[:2] == filled.shape:
                draw(name, arr, filled, umask)
                drawn = f"  {DIM}drawn{OFF}"
        print(f"  {name:<32}{grams:>5}{union:>7.1%}{holes:>8.1%}"
              f"{GRN}{agree:>8.0%}{OFF}{area:>8,.0f}{grams/area:>9.5f}{drawn}")
        out.append((food, grams, area, agree))

    if not out:
        print(f"\n  {RED}nothing measured{OFF}\n")
        return 1

    print(f"\n{HDR}What the tables say, against what the scale says{OFF}\n")
    print(f"  {'food':<24}{'shape':<13}{'table h':>9}{'table d':>9}"
          f"{'implied h':>11}{'x off':>8}")
    by_shape = {}
    for food, grams, area, agree in out:
        shape = _classify_shape(food, None)
        dens = density_for(food)
        th = MEASURED_HEIGHTS_MM.get(shape, MEASURED_HEIGHTS_MM["default"])
        implied = grams / dens * 1000.0 / area
        ratio = th / implied if implied else float("nan")
        flag = RED if (ratio > 1.5 or ratio < 0.67) else ""
        print(f"  {food:<24}{shape:<13}{th:>9.0f}{dens:>9.2f}"
              f"{implied:>11.1f}{flag}{ratio:>8.2f}{OFF}")
        by_shape.setdefault(shape, []).append(grams / area)

    print(f"\n{HDR}Grams per mm2 of footprint, measured{OFF}  "
          f"{DIM}no height prior, no density table{OFF}\n")
    print(f"  {'shape':<13}{'n':>3}{'mean':>10}{'min':>10}{'max':>10}{'spread':>9}")
    for shape in sorted(by_shape):
        v = sorted(by_shape[shape])
        spread = (v[-1] / v[0]) if v[0] > 0 else float("nan")
        print(f"  {shape:<13}{len(v):>3}{sum(v)/len(v):>10.5f}"
              f"{v[0]:>10.5f}{v[-1]:>10.5f}{spread:>8.1f}x")
    print(f"\n  {DIM}A shape whose spread is near 1x is one number away from "
          f"being measured{OFF}")
    print(f"  {DIM}rather than assumed. A wide spread means the shape class is "
          f"mixing foods.{OFF}\n")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true",
                    help="pay for new masks instead of reading the cache")
    ap.add_argument("--crop", action="store_true",
                    help="crop to the plate before the segmenter call")
    ap.add_argument("--overlay", action="store_true",
                    help="paint each measurement onto its photograph and save it")
    ap.add_argument("only", nargs="*", help="photo numbers, e.g. 17 18 22")
    a = ap.parse_args()
    if a.crop:
        globals()["CROP"] = True
        print("  cropping to the plate before each segmenter call")
    raise SystemExit(run(a.fresh, set(a.only), a.overlay))
