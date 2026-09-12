#!/usr/bin/env python3
r"""Does the model actually know how tall the food is?

    dev depth "C:\photos\11-kebab-paper-card.jpg" --peaks "meat=55,tomato=28,potato=30"

WHY THIS EXISTS
---------------
`USE_MEASURED_HEIGHT` in portion.py is switched OFF, and its comment says
exactly what it is waiting for:

    To turn back on: run `dev scandebug` on an angled photo of a weighed meal,
    compare the reported height_ratio against the real peak height measured
    with a ruler, and fix whichever term is wrong. Not before.

Until then EVERY item on EVERY photo gets its height from a fixed table --
18 mm for anything flat, 40 mm for anything chunky -- whether it is a tortilla
or a stack of kebab. That single table is the best current explanation for the
one pattern that survives the whole weighed bench: spread foods come out heavy
and piled foods come out light.

    11-kebab-paper-card    meat -40.3%   tomatoes +130.4%   potatoes  +88.3%
    12-kebab-paper-nocard  meat -27.4%   tomatoes +156.3%   potatoes +109.4%

Same three foods, two photos, and the tall thing is always too light while the
short things are always too heavy. That is not noise and it is not area.

THE TWO UNKNOWNS, AND HOW THIS SEPARATES THEM
---------------------------------------------
Turning measured height on once made things worse, and the reason was never
established. There are two candidate culprits and they need opposite fixes:

    (1) the model under-reports how tall food stands, or
    (2) PROFILE_FACTORS -- how much of the bounding box the food actually
        fills in the vertical -- is too low.

One bench run cannot tell them apart, because both show up as "too light".
A ruler can, because it splits the chain in two:

    reported height  ->  [ compare to ruler ]  ->  true height  ->  [ estimator ]  ->  grams

Check the LEFT half against the ruler and you have tested (1) alone. Feed the
ruler's own number into the estimator and check the grams against the scale and
you have tested (2) alone, with (1) removed from the chain. This script prints
both halves side by side.

WHY IT ASKS FOR TWO HEIGHTS
---------------------------
`height_ratio` is defined in the prompt as a fraction of the VESSEL'S width --
which quietly means the whole mechanism cannot work on paper, on a board, or on
a bare table, because there is no vessel for the fraction to be a fraction of.
`reference_width_mm()` confirms it: plate diameter or a known vessel, nothing
else. On the card photos the tilt is known (the card measures its own angle) and
the scale is known (the card is 85.6 mm) and the height still falls back to the
table, because the ruler it wants does not exist.

So the prompt now also asks for `height_ratio_self`: the same height over the
food's OWN width. That needs no vessel and works on every surface. Both are
printed against the same ruler reading, on the same photo, so the bench decides
which definition the model reports accurately rather than anyone guessing.

NOTHING HERE CHANGES AN ESTIMATE. Like `dev seg`, this is an instrument.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GRN, RED, YEL, DIM, HDR, CYN, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[36m", "\033[0m"
)


def _num(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _match_peak(name: str, peaks: dict[str, float]) -> float | None:
    """Match a ruler reading to a detected food by loose name overlap.

    The user types "meat=55"; the model says "grilled meat skewer". Requiring
    the two to be equal would make the flag unusable on exactly the photos it
    is for.
    """
    n = (name or "").strip().lower()
    if not n:
        # An empty name matches every key under "n in key" and would silently
        # borrow the first ruler reading in the list for a food nobody named.
        return None
    for key, mm in peaks.items():
        if key in n or n in key:
            return mm
    for key, mm in peaks.items():
        if any(w and w in n for w in key.split()):
            return mm
    return None


def _err(got: float | None, truth: float | None) -> str:
    if got is None or not truth:
        return ""
    pct = (got - truth) / truth * 100.0
    colour = GRN if abs(pct) <= 20 else (YEL if abs(pct) <= 45 else RED)
    return f"  {colour}{pct:+.0f}%{OFF}"


async def main() -> int:
    ap = argparse.ArgumentParser(description="Check reported food height against a ruler.")
    ap.add_argument("photo", help="path to the photograph")
    ap.add_argument("--peaks", default="",
                    help='ruler readings in mm, e.g. "meat=55,tomato=28,potato=30". '
                         "Partial names are fine.")
    ap.add_argument("--plate", type=float, default=None,
                    help="plate diameter in mm, if this photo has a plate you measured")
    ap.add_argument("--distance", type=float, default=None,
                    help="camera-to-food distance in mm, measured ALONG THE LENS'S "
                         "LINE OF SIGHT -- the slant distance from the camera to the "
                         "food, not the vertical drop. On an angled shot those differ "
                         "by 1/cos(tilt), and frame area goes as the square of it.")
    ap.add_argument("--fov", type=float, default=None,
                    help="camera field of view in degrees across the LONG axis "
                         "(default 68)")
    args = ap.parse_args()

    peaks: dict[str, float] = {}
    for part in args.peaks.split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            mm = _num(v)
            if mm:
                peaks[k.strip().lower()] = mm

    path = Path(args.photo)
    if not path.exists():
        print(f"{RED}no such photo:{OFF} {path}")
        return 1

    from app.services.ai.portion import (
        GeometryHint, HEIGHT_PRIORS_MM, PROFILE_FACTORS, _classify_shape,
        mm2_per_frame, normalize_bbox, railed_area,
    )
    from app.services.ai.vision import _plate_ellipse, detect_foods, downscale_jpeg

    print(f"\n{HDR}{path.name}{OFF}")

    prepared = downscale_jpeg(path.read_bytes())
    ref = prepared.reference
    detection = await detect_foods([prepared.b64], "depth-lab")
    if detection.get("_error") or not detection.get("items"):
        print(f"  {RED}vision returned nothing usable:{OFF} {detection.get('_error')}")
        return 1

    items = [d for d in detection.get("items") or [] if isinstance(d, dict)]
    vessel = str(detection.get("container") or "").strip().lower() or None
    wh = _plate_ellipse(detection)
    hint = GeometryHint(
        plate_ellipse_area_ratio=_num(detection.get("plate_area_ratio")) or None,
        plate_diameter_mm=args.plate,
        depth_mm=args.distance,
        camera_fov_deg=args.fov,
        aspect_ratio=prepared.aspect,
        vessel=vessel,
        vessel_shape=detection.get("container_shape"),
        plate_ellipse_wh=wh,
        reference_kind=(ref.kind if ref else None),
        reference_frame_width_mm=(ref.frame_width_mm if ref else None),
        reference_tilt_deg=(ref.tilt_deg if ref else None),
    )

    # ---- what this photo can measure at all -----------------------------
    print(f"\n{HDR}what this photo gives us{OFF}")
    print(f"  surface           {vessel or 'none reported'}")
    tilt = hint.camera_tilt_deg
    if hint.tilt_deg is not None:
        src = f"the {vessel or 'vessel'}'s outline"
    elif hint.reference_tilt_deg is not None:
        src = f"the {ref.kind if ref else 'reference'}'s own shape"
    else:
        src = None
    if tilt is None:
        print(f"  camera tilt       {RED}unknown{OFF}  — nothing round or "
              f"rectangular to measure it from")
    else:
        verdict = (f"{GRN}enough to see the food's side{OFF}" if tilt >= 20
                   else f"{YEL}too flat — height is not visible from here{OFF}")
        print(f"  camera tilt       {tilt:.0f} deg, from {src}   {verdict}")

    if args.distance:
        # Said out loud because getting it wrong is invisible and expensive: the
        # depth rung computes frame width as 2 * d * tan(fov/2), where d is the
        # distance along the lens's line of sight. Hand it the VERTICAL height
        # above an angled shot instead and at 45 degrees it is 0.71x too small,
        # which is half the frame area and half the food.
        print(f"  distance          {args.distance:.0f} mm along the line of "
              f"sight (slant, not vertical)")

    frame_mm2, method = mm2_per_frame(hint)
    frame_w_mm = None
    if frame_mm2 and prepared.aspect:
        frame_w_mm = math.sqrt(frame_mm2 * prepared.aspect)
        print(f"  scale             {method}: the frame is {frame_w_mm:.0f} mm across")
    else:
        print(f"  scale             {RED}none{OFF} ({method}) — no absolute size "
              f"in this photo, so no height in mm either")

    # Does the CURRENT pipeline have a ruler for the vessel-referenced height?
    from app.services.ai.portion import reference_width_mm
    ref_w = reference_width_mm(hint)
    if ref_w:
        print(f"  height ruler      {GRN}{ref_w:.0f} mm{OFF} "
              f"(what height_ratio is a fraction of)")
    else:
        print(f"  height ruler      {RED}none{OFF} — reference_width_mm() knows only a "
              f"plate diameter or a\n                    known vessel, so "
              f"height_ratio cannot be converted to mm on\n                    this "
              f"photo no matter what the model reports")

    # ---- per item --------------------------------------------------------
    print(f"\n{HDR}height, three ways{OFF}   "
          f"{DIM}(ruler = what you measured; the rest is what the app thinks){OFF}")
    print(f"  {DIM}{'food':<26}{'prior':>7}{'vessel-ref':>13}{'self-ref':>12}"
          f"{'ruler':>9}{OFF}")

    rows = []
    for det in items:
        name = str(det.get("name") or "food")[:26]
        shape = _classify_shape(name, det.get("shape"))
        prior = HEIGHT_PRIORS_MM.get(shape, HEIGHT_PRIORS_MM["default"])

        hr = _num(det.get("height_ratio"))
        hs = _num(det.get("height_ratio_self"))
        truth = _match_peak(name, peaks)

        # Vessel-referenced: only convertible where a ruler exists.
        vessel_mm = (hr * ref_w) if (hr and ref_w) else None

        # Self-referenced: needs the item's own width in mm, which any scale
        # rung can give -- including the card on a paper photo, where the
        # vessel-referenced number has nothing to lean on.
        self_mm = None
        box = normalize_bbox(det.get("bbox"))
        if hs and frame_w_mm and box and _num(box.get("w")):
            self_mm = hs * float(box["w"]) * frame_w_mm

        def cell(v, width=13):
            return (f"{v:.0f} mm" if v is not None else "--").rjust(width)

        print(f"  {name:<26}{prior:>5.0f} mm"
              f"{cell(vessel_mm)}{cell(self_mm, 12)}"
              f"{(f'{truth:.0f} mm' if truth else '--'):>9}")
        if truth:
            print(f"    {DIM}vs ruler:{OFF}  prior{_err(prior, truth)}   "
                  f"vessel-ref{_err(vessel_mm, truth) or '  --'}   "
                  f"self-ref{_err(self_mm, truth) or '  --'}")
        rows.append((name, shape, prior, vessel_mm, self_mm, truth,
                     railed_area(_num(det.get('area_ratio'), 0.0) or 0.0, det.get('bbox'))))

    # ---- what the truth would be worth ----------------------------------
    if peaks and frame_mm2:
        print(f"\n{HDR}and if the app simply knew the true height{OFF}")
        print(f"  {DIM}grams the geometry would produce with the prior, against the "
              f"grams it would\n  produce with your ruler reading. The gap is what "
              f"measuring height is worth.{OFF}")
        for name, shape, prior, _v, _s, truth, area in rows:
            if not truth or not area:
                continue
            pf = PROFILE_FACTORS.get(shape, PROFILE_FACTORS.get("default", 0.5))
            mm2 = frame_mm2 * area
            # Density is left out deliberately: it cancels in the ratio, and
            # this line is about height alone.
            with_prior = mm2 * prior * pf
            with_truth = mm2 * truth * pf
            ratio = with_truth / with_prior if with_prior else 0.0
            colour = GRN if 0.85 <= ratio <= 1.15 else YEL if 0.6 <= ratio <= 1.6 else RED
            print(f"  {name:<26} shape={shape:<11} "
                  f"prior {prior:.0f} mm -> ruler {truth:.0f} mm   "
                  f"{colour}x{ratio:.2f}{OFF} on the grams")

    print(f"\n{DIM}Nothing above changed an estimate. USE_MEASURED_HEIGHT is still "
          f"off.{OFF}")
    print(f"{DIM}Read it this way: if 'vessel-ref' and 'self-ref' sit near the ruler, "
          f"the model\ncan see height and the fault is PROFILE_FACTORS. If they are "
          f"far below it, the\nmodel under-reports height and no factor will fix "
          f"that.{OFF}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
