"""Check every bench photograph's declared vessel size against its own card.

WHY THIS EXISTS

Each bench case declares a vessel diameter in millimetres, and every gram on
that photograph is scaled by it -- area goes as the square, so a diameter 10%
wrong is 21% on every weight. Those numbers arrive by hand, from a tape or a
message, and a typo in one of them is indistinguishable from an estimator bug
when the bench reports the miss.

So: measure the vessel in the picture against the credit card in the picture,
and say whether it agrees with what the case claims. A card is 85.60 x 53.98 mm
by ISO/IEC 7810, exact, on every card in every wallet.

WHAT "AGREE" MEANS, AND WHY IT IS NOT ZERO

The card lies on the TABLE. The vessel's rim sits above it -- nearer the lens,
so it measures LARGER by D / (D - h) for camera distance D and rim height h.
Measured on this user's own crockery against a tape: a dinner plate reads about
5% over, a 4.5 in soup crock about 30% over. That is not error, it is geometry,
and portion.py carries the derivation.

So the expectation is not "equal" but "a few percent over for a plate, a lot
over for a bowl". This prints the raw disagreement and leaves the judgement
visible rather than hiding it behind a pass mark.

WHAT IT CANNOT DO

Nothing here proves a WEIGHT. It checks the scale, which is one term. A photo
can pass this and still be mislabelled.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

CARD_LONG, CARD_SHORT = 85.60, 53.98      # ISO/IEC 7810 ID-1, exact

# How far over the tape a card-measured vessel may read before it is worth a
# second look, by vessel kind. A plate's rim is low; a crock's is not.
PLATE_EXPECTED_OVER = (0.0, 0.12)
BOWL_EXPECTED_OVER = (0.10, 0.45)
BOWL_MM = 150.0                            # anything under this is a bowl


def measure(path):
    """(card px/mm, vessel equal-area diameter mm) or a reason it could not."""
    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im.thumbnail((1600, 1600), Image.LANCZOS)
    a = np.asarray(im)
    H, W = a.shape[:2]
    hsv = cv2.cvtColor(a, cv2.COLOR_RGB2HSV)
    hue, sat, val = (hsv[:, :, i].astype(int) for i in range(3))

    # The card by SHAPE as well as colour. Colour alone catches green food --
    # zucchini, brussels sprouts, a caesar salad -- and reports a card with the
    # wrong aspect, which is worse than reporting none.
    m = ((hue >= 35) & (hue <= 90) & (sat > 40) & (val > 40)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    best = None
    for c in cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]:
        if cv2.contourArea(c) < 0.0012 * H * W:
            continue
        (_, _), (w1, h1), _ = cv2.minAreaRect(c)
        long_px, short_px = max(w1, h1), min(w1, h1)
        if short_px <= 0:
            continue
        aspect = long_px / short_px
        fill = cv2.contourArea(c) / (long_px * short_px)
        if fill > 0.85 and 1.40 < aspect < 1.80:
            score = abs(aspect - 1.586)
            if best is None or score < best[0]:
                best = (score, long_px, short_px, aspect)
    if best is None:
        return None, None, None, "no card in frame"
    _, long_px, short_px, aspect = best
    ppm = ((long_px / CARD_LONG) + (short_px / CARD_SHORT)) / 2

    # The vessel: unsaturated, and never the card's own rows.
    ves = (sat < 60).astype(np.uint8)
    ves = cv2.morphologyEx(ves, cv2.MORPH_CLOSE, np.ones((31, 31), np.uint8))
    ves = cv2.morphologyEx(ves, cv2.MORPH_OPEN, np.ones((13, 13), np.uint8))
    cnts = cv2.findContours(ves, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]
    if not cnts:
        return ppm, None, aspect, "no vessel found"

    # THE LARGEST BLOB IS NOT THE PLATE, and taking it was the first version's
    # bug. A pale shirt cuff at the edge, a napkin, a second plate half out of
    # frame -- all unsaturated, all touching the border, all bigger than the
    # plate once they merge with it. That is what "squashed 1.58" meant: a
    # round plate cannot be that elliptical from overhead, so the outline had
    # swallowed something.
    #
    # So: throw away anything touching the frame edge, then take the ROUNDEST
    # large candidate rather than the biggest. A plate seen from above is a
    # circle, and roundness is the property being measured anyway.
    import math

    best_v = None
    for c in cnts:
        area = cv2.contourArea(c)
        if area < 0.02 * H * W:
            continue
        x, y, w, h = cv2.boundingRect(c)
        if x <= 2 or y <= 2 or x + w >= W - 2 or y + h >= H - 2:
            continue
        hull = cv2.convexHull(c)
        if len(hull) < 5:
            continue
        (_, _), (d1, d2), _ = cv2.fitEllipse(hull)
        ratio = max(d1, d2) / max(min(d1, d2), 1e-6)
        if ratio > 1.35:
            continue
        # Prefer round; break ties toward the larger object.
        score = ratio - 0.15 * (area / (H * W))
        if best_v is None or score < best_v[0]:
            best_v = (score, hull, ratio)
    if best_v is None:
        return ppm, None, aspect, "no round vessel clear of the frame edge"
    _, hull, ratio = best_v
    eq = 2 * math.sqrt(cv2.contourArea(hull) / math.pi) / ppm
    return ppm, eq, aspect, ("round" if ratio < 1.15 else f"ratio {ratio:.2f}")


def main() -> int:
    from scripts.bench_all import CASES

    GRN, YEL, RED, DIM, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
    photos = pathlib.Path(__file__).resolve().parents[2] / "photos"

    print(f"\n  Declared vessel size against the card in the same photograph")
    print(f"  {DIM}a card is 85.60 x 53.98 mm exactly; a rim above the table reads "
          f"over, which is geometry, not error{OFF}\n")
    print(f"  {'photo':32s} {'declared':>9s} {'measured':>9s} {'over':>7s}  note")
    flagged = 0
    seen: set[str] = set()
    for case in CASES:
        name, declared = case[0], case[1]
        if name in seen or declared is None:
            continue
        seen.add(name)
        path = photos / name
        if not path.exists():
            print(f"  {name[:32]:32s} {RED}missing{OFF}")
            flagged += 1
            continue
        try:
            ppm, eq, aspect, note = measure(path)
        except Exception as exc:  # noqa: BLE001
            print(f"  {name[:32]:32s} {RED}could not measure: {str(exc)[:40]}{OFF}")
            flagged += 1
            continue
        if eq is None:
            print(f"  {name[:32]:32s} {declared:>7.0f}mm {DIM}{note:>18s}{OFF}")
            continue
        over = eq / declared - 1.0
        lo, hi = BOWL_EXPECTED_OVER if declared < BOWL_MM else PLATE_EXPECTED_OVER
        ok = lo - 0.06 <= over <= hi
        colour = GRN if ok else YEL
        if not ok:
            flagged += 1
        print(f"  {name[:32]:32s} {declared:>7.0f}mm {eq:>7.0f}mm "
              f"{colour}{over:>+6.0%}{OFF}  {DIM}card aspect {aspect:.3f}, {note}{OFF}")

    print()
    if flagged:
        print(f"  {YEL}{flagged} photograph(s) worth a second look{OFF} {DIM}-- a "
              f"vessel that reads far from its declared size means the declared\n"
              f"  number is wrong, or the wrong vessel was measured. Either way "
              f"every gram on that photo is scaled by it.{OFF}")
    else:
        print(f"  {GRN}every declared vessel agrees with its own card{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
