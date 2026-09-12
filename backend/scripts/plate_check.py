"""Which plate the scan path actually uses, drawn over the photograph.

WHY THIS EXISTS

The Hough detector was scored 30/30 and believed. It was scored through
`plate_surface(rgb, None)` -- no plate box -- which is how `dev replay` and
`dev segcheck` call it. Every real scan passes a box, and the box branch
returned unconditionally, so on a real scan the detector never ran. A number
measured through a door production does not use is not a measurement of
production.

So this calls `plate_surface` THE WAY A SCAN DOES, with a plate box, and draws
what comes back. The table says which source won; the pictures say whether it
was right. Only the pictures can: two footprints once agreed to 97% and were
both the credit card.

FREE unless --sam2 is passed. The Hough pass touches no network at all.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import cv2                                                       # noqa: E402
import numpy as np                                               # noqa: E402

from app.services.ai import food_seg                             # noqa: E402

HDR, GRN, RED, YEL, DIM, OFF = ("\033[1m", "\033[32m", "\033[31m",
                                "\033[33m", "\033[2m", "\033[0m")
ROOT = pathlib.Path(__file__).resolve().parents[2]
PHOTOS = ROOT / "photos"
OUT = ROOT / "mask_overlays" / "plate"

# The model's plate box is not available offline, and a scan always has one.
# This stands in for it: the whole frame less a margin, which is what the model
# returns for a plate filling the picture. It is deliberately GENEROUS -- a
# tight stand-in would flatter the box branch by fencing it correctly for free.
STAND_IN_BOX = {"x": 0.08, "y": 0.08, "w": 0.84, "h": 0.84}

# Drawn, not filled, so the food underneath stays visible and a circle sitting
# on the well instead of the rim is obvious rather than hidden.
CIRCLE_RGB = (60, 220, 60)        # Hough
OUTLINE_RGB = (230, 60, 230)      # SAM2


def _edge(mask: np.ndarray) -> np.ndarray:
    m = mask.astype(np.uint8)
    return cv2.morphologyEx(m, cv2.MORPH_GRADIENT, np.ones((5, 5), np.uint8)).astype(bool)


def _load(path: pathlib.Path):
    arr = cv2.imread(str(path))
    if arr is None:
        return None
    rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    if max(h, w) > 1568:
        s = 1568 / max(h, w)
        rgb = cv2.resize(rgb, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return rgb


def main() -> int:
    args = sys.argv[1:]
    want_sam2 = "--sam2" in args
    photos = sorted(p for p in PHOTOS.glob("*.jpg"))
    if not photos:
        print(f"  {RED}no photographs in {PHOTOS}{OFF}")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"\n{HDR}The plate a scan gets{OFF}")
    if not want_sam2:
        print(f"{DIM}  Hough only, free. --sam2 also asks the segmenter "
              f"and costs about 2c per photograph.{OFF}")
    print(f"\n  {'photo':34s} {'source':8s} {'circle':>8s} {'IoU':>6s}  overlay")

    agreements: list[float] = []
    no_circle: list[str] = []
    # (name, radius as a fraction of the short side). A plate has TWO
    # concentric edges and the inner one -- the well -- is often the stronger,
    # which is what killed the sixth method: one photograph passed every check
    # having locked the well, and reported a 34.7 degree tilt for a plate shot
    # from almost overhead. A well-lock does not look wrong in a table. It
    # looks like a SMALL CIRCLE. So the small ones are ranked out here and
    # looked at first, rather than left to whoever remembers.
    radii: list[tuple[str, float]] = []
    for path in photos:
        rgb = _load(path)
        if rgb is None:
            print(f"  {path.name:34s} {RED}unreadable{OFF}")
            continue
        mask, source = food_seg.plate_surface(rgb, STAND_IN_BOX)
        circle = None
        try:
            circle = food_seg._plate_by_hough(rgb)
        except Exception:                                        # noqa: BLE001
            circle = None
        if circle is None:
            no_circle.append(path.name)

        iou = float("nan")
        outline = None
        if want_sam2:
            try:
                seg = food_seg.segmenter()
                if seg.available() and hasattr(seg, "plate_outline"):
                    got = seg.plate_outline(rgb, STAND_IN_BOX)
                    if got is not None and got.any():
                        outline = got.astype(bool)
            except Exception:                                    # noqa: BLE001
                outline = None
            if outline is not None and circle is not None:
                iou = food_seg.plates_agree(outline, circle)
                agreements.append(iou)

        canvas = rgb.copy()
        if circle is not None:
            canvas[_edge(circle)] = CIRCLE_RGB
        if outline is not None:
            canvas[_edge(outline)] = OUTLINE_RGB
        dest = OUT / f"{path.stem}.png"
        cv2.imwrite(str(dest), cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))

        if circle is not None:
            ys, xs = np.nonzero(circle)
            r_px = (xs.max() - xs.min() + 1) / 2.0
            radii.append((path.name, r_px / min(rgb.shape[:2])))
        pct = f"{circle.mean()*100:5.1f}%" if circle is not None else "   --"
        tint = GRN if source in ("sam2", food_seg.CIRCLE_PLATE_SOURCE) else YEL
        print(f"  {path.name:34s} {tint}{source:8s}{OFF} {pct:>8s} "
              f"{('  --' if iou != iou else f'{iou:6.3f}')}  {dest.name}")

    print(f"\n  {len(photos)} photographs, overlays in {OUT}")
    if no_circle:
        print(f"  {YEL}no circle on {len(no_circle)}: "
              f"{', '.join(no_circle[:6])}{'...' if len(no_circle) > 6 else ''}{OFF}")

    # THE WELL-LOCK SUSPECTS.
    if radii:
        radii.sort(key=lambda t: t[1])
        floor = food_seg.HOUGH_R_MIN
        print(f"\n{HDR}Smallest circles -- look at these overlays first{OFF}")
        print(f"{DIM}  A circle on the well instead of the rim reads as a small "
              f"one. The band floor is {floor:.2f} of the short side; a circle "
              f"sitting on it is either a small vessel or a well.{OFF}")
        for name, frac in radii[:5]:
            near = f"  {YEL}at the band floor{OFF}" if frac <= floor * 1.15 else ""
            print(f"    {name:34s} r = {frac:.3f} of short side{near}")

    # WHAT THE THRESHOLD WOULD DECIDE, so PLATE_AGREEMENT_IOU is set from the
    # bench rather than from a plausible-looking number.
    if agreements:
        print(f"\n{HDR}PLATE_AGREEMENT_IOU, if it were set to{OFF}")
        for t in (0.50, 0.60, 0.70, 0.80, 0.90):
            keep = sum(1 for v in agreements if v >= t)
            mark = "  <- current" if abs(t - food_seg.PLATE_AGREEMENT_IOU) < 1e-9 else ""
            print(f"    {t:.2f}   SAM2 used on {keep:2d} of {len(agreements)}{mark}")
        print(f"\n  {DIM}Look at the overlays before choosing. A high IoU on a "
              f"circle that sits on the well is still wrong.{OFF}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
