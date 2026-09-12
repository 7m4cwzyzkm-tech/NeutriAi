"""Is the footprint's instability the model's box, or the segmenter? Offline, free.

WHAT THIS TESTS

`segment_hosted.py:775` decides which SAM2 masks belong to an item:

    ys, xs = np.nonzero(m)
    cx, cy = float(xs.mean()), float(ys.mean())
    if not (x0 <= cx < x1 and y0 <= cy < y1):
        continue

A mask is in the footprint if and only if its CENTROID lands inside the model's
bounding box. A hard binary test -- on a box that arrives quantised to a 0.05 grid,
where one step is 5% of the frame, roughly 30 px on a 600 px image. A mask whose
centroid sits near that edge flips wholly in or out, carrying its entire area with
it. On photo 35 the per-item union went 7 masks, then 3, then 3 across three scans
of one photograph.

The alternative is an OVERLAP-FRACTION rule: include a mask when a sufficient share
of its own area lies inside the box. A box that shifts 30 px then changes each
mask's inside-fraction a little, instead of flipping whole masks.

WHY THIS COSTS NOTHING

The mask sets are already cached in mask_overlays/masks-*.npz from the height work,
and `dev maskstability` measured SAM2 to be pixel-identical across three cold calls
(29 masks, IoU 1.0000). So the mask set is a fixed input and the only variable left
is the box. That is exactly what this perturbs. ZERO model calls.

THE CACHED SETS ARE NOT PRODUCTION'S, AND THE ABOVE IS WHY THAT WAS MISSED

Read the determinism result precisely: IoU 1.0000 across repeat calls on IDENTICAL
BYTES. `mask_stability` encodes the local JPEG itself; production uploads to
storage, fetches back, decodes and re-encodes through the scan path. Same
photograph, different bytes -- and nothing measured says SAM2 is stable across
encodings. "Fixed input" was true of the cache and never established of the scan.

It is now known to be false. Replaying production's OWN logged box for photo 35
over the cached 16 masks takes 6 masks where production took 7. The per-mask rule
below decides each mask on its own -- no mask's verdict depends on any other -- so
a superset cannot yield FEWER takes than a subset. The two sets are therefore
different, whatever their counts agree on, and a count match was never an identity
match.

So a run against the cache measures the RULE'S SENSITIVITY on a plausible mask set,
which is a real and useful question, and it does NOT reproduce any particular
production scan. For that, pass --dump: `segment_hosted.dump_masks` writes the pool
the pipeline actually chose from, with the model's actual boxes, at the moment of
choosing. Exact by construction rather than by argument.

WHERE THE BOXES COME FROM, AND WHY THEY ARE NOT THE REAL ONES

The .npz caches masks, not boxes, and the bench never saved them. Recovering what
the model actually answered on some past scan is not the question. The question is
HOW SENSITIVE THE RULE IS TO WHERE THE BOX SITS -- so a plausible box is derived
from the masks themselves, quantised to the same 0.05 grid the model's answers land
on, and then perturbed.

The absolute swing depends on which box you start from, so several plausible
starting boxes are used per photograph and the spread across them is reported. A
single starting box would give a precise number about nothing.

THE TRAP THIS SCRIPT IS BUILT TO AVOID

Stability alone is not success. An overlap rule with a LOW threshold takes every
mask and returns the whole plate every time -- perfectly stable, completely wrong. A
HIGH threshold takes nothing, also perfectly stable. Both would pass a swing-only
test with flying colours, and a rule that is stable at the wrong value is a worse
bug than the one we have, because it fails quietly.

So two numbers are reported per rule, always together:

    swing   max/min union area across the box perturbations
    value   the union area at the UNPERTURBED box, against the centroid rule's

The overlap rule has to be stable AND land near the centroid rule's answer at the
correct box.

THE THRESHOLD IS SWEPT, NOT CHOSEN

0.2 to 0.8. Picking one and reporting it is how a constant gets chosen to fit an
argument. If no value is both stable and correct, that is the finding.

THE CONTROLS ARE THE FALSIFIER

34-pizza-slice and 24-macaroni-salad are single connected masses and both scored
spread 0 g on the bench. Under the topology account they should ALREADY show low
swing under the centroid rule -- one big mask whose centroid sits solidly inside any
sensible box. If they swing as much as 42-chips and 44-grapes, the topology account
is wrong and the diagnosis needs revisiting. They are reported next to the unstable
set, whatever they say.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

import numpy as np                                               # noqa: E402

HDR, GRN, RED, YEL, DIM, OFF = ("\033[1m", "\033[32m", "\033[31m",
                                "\033[33m", "\033[2m", "\033[0m")
ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE = ROOT / "mask_overlays"

# The real pipeline's pre-filters, copied so the comparison is like-for-like. The
# centroid test sits AFTER these and before the ring check; replacing it without
# them would measure a different pipeline.
CONTAINS_BOX_LIMIT = 0.95      # segment_hosted.py:525
MAX_MASK_OVER_BOX = 2.0        # segment_hosted.py:545
MAX_MASK_FRAME_SHARE = 0.35    # segment_hosted.py:560

GRID = 0.05                    # the model's quantisation, and the perturbation unit
THRESHOLDS = (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)

# Membership here is a CLAIM that the photograph's mask set is verified fixed,
# not a convenience list -- the replay's whole premise is a fixed mask set, and a
# row that looks like a measurement and is not is worse than a missing row.
# 35-slider-fries-plate earned its entry on 2026-09-12 and LOST IT the same day.
# Three cold SAM2 calls gave 16 masks each at IoU 1.0000, and `found=16` matched
# the production log's `plate_hint_bound before=16` -- so it was admitted on a
# COUNT MATCH, which is not an identity match. The monotonicity argument in the
# header shows the sets differ. It stays listed because the rule-sensitivity
# question it answers does not need production's exact masks; it does not mean
# what its neighbours mean, and no row here reproduces a real scan. Use --dump.
UNSTABLE = ["23-brussels-sprouts-plate", "27-potroast-beef-alone",
            "35-slider-fries-plate",
            "42-chips-spread", "44-grapes-spread", "45-grapes-cluster"]
CONTROLS = ["24-macaroni-salad-plate", "34-pizza-slice-plate"]


def load(stem: str):
    p = CACHE / f"masks-{stem}.npz"
    if not p.exists():
        return None
    z = np.load(p)
    masks = [z[k] for k in z.files if k.startswith("m")]
    return [m.astype(bool) for m in masks] or None


def quantise(v: float) -> float:
    return round(v / GRID) * GRID


def seed_boxes(masks: list, shape: tuple[int, int], top: int = 3) -> list[dict]:
    """Plausible item boxes, derived from the masks and quantised like the model's.

    The model boxes the FOOD, so a food-sized mask's own bounding box is the
    closest honest stand-in available offline. Several are returned because the
    swing depends on where you start.
    """
    H, W = shape
    frame = float(H * W)
    out = []
    # Food-sized: big enough to be an item, small enough not to be the plate.
    # DERIVED FROM EACH ITEM'S OWN MASK EXTENT, AND NOT ENLARGED.
    #
    # An earlier version raised the floor to 4 grid steps a side so that boxes
    # stopped falling off their food. That is tuning the experiment into
    # agreeing: one grid step is 5% of the frame and small foods on these plates
    # occupy about 5% of the frame, so a box that leaves its food after two
    # steps is not a badly chosen seed -- it is the finding restated. The
    # model's quantisation is on the same scale as the object it describes,
    # which is why the effect is severe for small separated foods and absent
    # for a pizza slice.
    cand = [m for m in masks if 0.005 <= m.sum() / frame <= MAX_MASK_FRAME_SHARE]
    cand.sort(key=lambda m: -int(m.sum()))
    for m in cand[:top]:
        ys, xs = np.nonzero(m)
        x = quantise(xs.min() / W)
        y = quantise(ys.min() / H)
        w = quantise((xs.max() - xs.min() + 1) / W)
        h = quantise((ys.max() - ys.min() + 1) / H)
        if w >= GRID and h >= GRID:
            out.append({"x": x, "y": y, "w": w, "h": h})
    return out


def _prefilter(masks: list, x0, x1, y0, y1, box_px: float) -> list:
    keep = []
    for m in masks:
        area = int(m.sum())
        if not area:
            continue
        inside = int(m[y0:y1, x0:x1].sum())
        if inside / box_px >= CONTAINS_BOX_LIMIT or area > box_px * MAX_MASK_OVER_BOX:
            continue
        keep.append((m, area, inside))
    return keep


def union_area(masks: list, box: dict, shape: tuple[int, int],
               rule: str, thresh: float = 0.5) -> float:
    """Union area as a fraction of frame, under one rule. 0.0 when nothing is taken."""
    H, W = shape
    x0, x1 = int(max(0, box["x"] * W)), int(min(W, (box["x"] + box["w"]) * W))
    y0, y1 = int(max(0, box["y"] * H)), int(min(H, (box["y"] + box["h"]) * H))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    box_px = float((x1 - x0) * (y1 - y0)) or 1.0
    union = np.zeros((H, W), bool)
    for m, area, inside in _prefilter(masks, x0, x1, y0, y1, box_px):
        if m.mean() > MAX_MASK_FRAME_SHARE:
            continue
        if rule == "centroid":
            ys, xs = np.nonzero(m)
            cx, cy = float(xs.mean()), float(ys.mean())
            take = (x0 <= cx < x1 and y0 <= cy < y1)
        else:
            take = (inside / area) >= thresh
        if take:
            union |= m
    return float(union.mean())


def sweep(masks: list, shape: tuple[int, int], box: dict, rule: str,
          thresh: float = 0.5, reach: int = 1, mode: str = "translate") -> dict:
    """Drift and empties at one perturbation radius.

    DRIFT, ANCHORED TO THE UNPERTURBED VALUE:

        drift_k = |area_k - area_0| / area_0

    Dimensionless, defined when a perturbation empties the union (that is
    drift 1.0), and it reads straight through to grams -- a drift of 0.6 is a
    60% error in that item's weight. `max/min` has no such reading even when it
    is finite, and it is undefined exactly where the interesting cases are.

    AN EMPTY IS A FIRST-CLASS OUTCOME, NOT A DEGENERATE ONE. If one grid step
    empties an item's footprint, that item's grams go to zero or fall back to a
    prior, which is the failure being hunted. Counted, never smoothed away.
    """
    base = union_area(masks, box, shape, rule, thresh)
    drifts, empties, n = [], 0, 0
    for dx in range(-reach, reach + 1):
        for dy in range(-reach, reach + 1):
            if dx == 0 and dy == 0:
                continue
            if max(abs(dx), abs(dy)) != reach:      # this ring only, never pooled
                continue
            if mode == "translate":
                b = dict(box, x=box["x"] + dx * GRID, y=box["y"] + dy * GRID)
            else:
                # SIZE, CENTRE HELD. `box_px` appears in BOTH drop conditions at
                # segment_hosted.py:770 -- the size ceiling is a MULTIPLE of the
                # box's own area, not an absolute -- so resizing moves the
                # threshold itself and every mask near it flips together.
                # Translating moves masks across a fixed boundary one at a time.
                w = box["w"] + dx * GRID
                h = box["h"] + dy * GRID
                if w < GRID or h < GRID:
                    continue
                b = {"x": box["x"] + (box["w"] - w) / 2.0,
                     "y": box["y"] + (box["h"] - h) / 2.0, "w": w, "h": h}
            v = union_area(masks, b, shape, rule, thresh)
            n += 1
            if v <= 0.0:
                empties += 1
            if base > 0:
                drifts.append(abs(v - base) / base)
    drifts = drifts or [float("nan")]
    return {"base": base, "max": max(drifts), "med": sorted(drifts)[len(drifts) // 2],
            "empties": empties, "n": n}


def _line(label: str, r1: dict, r2: dict, extra: str = "") -> str:
    def cell(r):
        if r["base"] <= 0:
            return f"{'no footprint':>22s}"
        return (f"max {r['max']:5.2f} med {r['med']:5.2f} "
                f"empty {r['empties']}/{r['n']}")
    return f"    {label:20s} {r1['base']:8.4f}  |  {cell(r1)}  |  {cell(r2)}{extra}"


def run(stem: str, tag: str, top: int = 3) -> list:
    masks = load(stem)
    if masks is None:
        print(f"  {YEL}{stem}: no cached masks{OFF}")
        return []
    shape = masks[0].shape
    boxes = seed_boxes(masks, shape, top)
    if not boxes:
        print(f"  {YEL}{stem}: no food-sized mask to derive a box from{OFF}")
        return []

    print(f"\n{HDR}{stem}{OFF}  {DIM}{tag} -- {len(masks)} masks at "
          f"{shape[1]}x{shape[0]}, {len(boxes)} box(es) from item extents{OFF}")
    print(f"    {'rule':20s} {'area@0':>8s}  |  {'+-1 STEP (primary)':>36s}  |  "
          f"{'+-2 STEPS (stress)':>36s}")

    rows = []
    for n, box in enumerate(boxes, 1):
        frac = box["w"] * box["h"]
        print(f"  {DIM}box {n}: x={box['x']:.2f} y={box['y']:.2f} "
              f"w={box['w']:.2f} h={box['h']:.2f}  ({frac * 100:.1f}% of frame){OFF}")
        c1 = sweep(masks, shape, box, "centroid", reach=1, mode="translate")
        cs1 = sweep(masks, shape, box, "centroid", reach=1, mode="size")
        c2 = sweep(masks, shape, box, "centroid", reach=2, mode="translate")
        cs2 = sweep(masks, shape, box, "centroid", reach=2, mode="size")
        print(_line("centroid TRANSLATE", c1, c2))
        print(_line("centroid RESIZE", cs1, cs2))
        for t in THRESHOLDS:
            o1 = sweep(masks, shape, box, "overlap", t, reach=1)
            o2 = sweep(masks, shape, box, "overlap", t, reach=2)
            ratio = (o1["base"] / c1["base"]) if c1["base"] > 0 else float("nan")
            near = 0.7 <= ratio <= 1.4
            better = (o1["empties"] < c1["empties"]) or (
                o1["max"] == o1["max"] and c1["max"] == c1["max"]
                and o1["max"] < c1["max"] * 0.5)
            mark = ""
            if better and near:
                mark = GRN + "  better+near" + OFF
            elif better and not near:
                mark = YEL + "  better, WRONG value" + OFF
            print(_line(f"overlap t={t:.1f}", o1, o2, mark))
            rows.append(dict(stem=stem, tag=tag, box=n, t=t, near=near,
                             shape=shape,
                             c1=c1, o1=o1, c2=c2, o2=o2, cs1=cs1, cs2=cs2))
    return rows


def run_dump(path: pathlib.Path) -> list:
    """One production scan, replayed against the masks that scan actually used.

    Nothing is derived here. The pool is the list `_union_in_box` iterated and
    the boxes are the ones the vision model returned, both written by the
    pipeline at the moment of choosing. The seeded rows elsewhere in this script
    answer "how sensitive is the rule"; this one answers "what did THIS scan do",
    and only this one is entitled to that claim.
    """
    from app.services.ai.segment_hosted import load_mask_dump

    d = load_mask_dump(path)
    masks, shape = d["pool"], d["shape"]
    # KEEP THE TRUE ITEM INDEX. A box the model did not return is written as
    # null, and renumbering after dropping it would label every later item with
    # someone else's number -- and these rows exist to be matched against
    # `sam2_box_union item=` lines in the log.
    boxes = [(i, b) for i, b in enumerate(d["boxes"] or []) if isinstance(b, dict)]
    if not masks:
        print(f"  {YEL}{path.name}: the scan had an EMPTY pool -- "
              f"{len(d['raw'])} masks returned, none survived the exclusions{OFF}")
        return []
    if not boxes:
        print(f"  {YEL}{path.name}: no usable boxes recorded{OFF}")
        return []

    tag = "PRODUCTION SCAN"
    print(f"\n{HDR}{path.stem}{OFF}  {DIM}{tag} -- digest {d['digest'][:12]}, "
          f"{len(d['raw'])} masks returned, {len(masks)} in the pool at "
          f"{shape[1]}x{shape[0]}, {len(boxes)} real box(es){OFF}")
    print(f"    {'rule':20s} {'area@0':>8s}  |  {'+-1 STEP (primary)':>36s}  |  "
          f"{'+-2 STEPS (stress)':>36s}")

    rows = []
    for n, box in boxes:
        frac = box["w"] * box["h"]
        print(f"  {DIM}item {n}: x={box['x']:.2f} y={box['y']:.2f} "
              f"w={box['w']:.2f} h={box['h']:.2f}  ({frac * 100:.1f}% of frame){OFF}")
        c1 = sweep(masks, shape, box, "centroid", reach=1, mode="translate")
        cs1 = sweep(masks, shape, box, "centroid", reach=1, mode="size")
        c2 = sweep(masks, shape, box, "centroid", reach=2, mode="translate")
        cs2 = sweep(masks, shape, box, "centroid", reach=2, mode="size")
        print(_line("centroid TRANSLATE", c1, c2))
        print(_line("centroid RESIZE", cs1, cs2))
        for t in THRESHOLDS:
            o1 = sweep(masks, shape, box, "overlap", t, reach=1)
            o2 = sweep(masks, shape, box, "overlap", t, reach=2)
            ratio = (o1["base"] / c1["base"]) if c1["base"] > 0 else float("nan")
            near = 0.7 <= ratio <= 1.4
            print(_line(f"overlap t={t:.1f}", o1, o2))
            rows.append(dict(stem=path.stem, tag=tag, box=n, t=t, near=near,
                             shape=shape,
                             c1=c1, o1=o1, c2=c2, o2=o2, cs1=cs1, cs2=cs2))
    return rows


def main() -> int:
    import os

    dump_dir = None
    argv = sys.argv[1:]
    if "--dump" in argv:
        i = argv.index("--dump")
        dump_dir = argv[i + 1] if i + 1 < len(argv) else ""
    elif os.environ.get("NUTRIAI_MASK_DUMP"):
        dump_dir = os.environ["NUTRIAI_MASK_DUMP"]
    if dump_dir is not None:
        d = pathlib.Path(dump_dir or ".")
        files = sorted(d.glob("masks-*.npz"))
        print(f"\n{HDR}Box-perturbation replay -- PRODUCTION DUMPS{OFF}")
        print(f"{DIM}  Offline. Zero model calls. Masks and boxes written by the\n"
              f"  pipeline itself, so this replays real scans rather than plausible\n"
              f"  ones. {len(files)} dump(s) in {d}.{OFF}")
        if not files:
            print(f"\n  {RED}no dumps found -- set NUTRIAI_MASK_DUMP before the "
                  f"scan, not after it{OFF}\n")
            return 1
        rows = []
        for f in files:
            rows += run_dump(f)
        print(f"\n{'=' * 74}\n")
        return 0 if rows else 1

    print(f"\n{HDR}Box-perturbation replay{OFF}")
    print(f"{DIM}  Offline. Zero model calls. Masks from mask_overlays/masks-*.npz,\n"
          f"  boxes derived from the masks and quantised to the model's 0.05 grid,\n"
          f"  perturbed +/-1 and +/-2 steps in x and y (25 positions per box).\n"
          f"  SWING is max/min union area; VALUE is the union at the unperturbed\n"
          f"  box. A rule must be BOTH stable and near the centroid rule's value --\n"
          f"  a low threshold takes everything and a high one takes nothing, and\n"
          f"  both are perfectly stable and perfectly useless.{OFF}")

    all_rows = []
    for stem in UNSTABLE:
        all_rows += run(stem, "HIGH-SPREAD ON THE BENCH")
    print(f"\n{HDR}CONTROLS -- single connected masses, spread 0 g on the bench{OFF}")
    print(f"{DIM}  These should already be flat under the centroid rule. If they swing\n"
          f"  like the set above, the topology account is wrong.{OFF}")
    for stem in CONTROLS:
        all_rows += run(stem, "CONTROL")
    print(f"\n{HDR}CONTROLS, NARROWED TO THE DOMINANT MASK{OFF}")
    print(f"{DIM}  The hypothesis names ONE CONNECTED MASS. A control seeded from the\n"
          f"  top three masks includes small incidental ones and is not instantiating\n"
          f"  that condition. Both are reported: if narrowing flips the empties, the\n"
          f"  reader sees exactly what changed; if it does not, that is a genuine\n"
          f"  problem for the topology account and it stays visible.{OFF}")
    for stem in CONTROLS:
        all_rows += run(stem, "CONTROL-DOMINANT", top=1)

    if not all_rows:
        print(f"\n  {RED}nothing to report{OFF}\n")
        return 1

    print(f"\n{'=' * 74}")
    print(f"{HDR}Summary -- +-1 step is the result, +-2 is a stress case{OFF}")
    for label in ("HIGH-SPREAD ON THE BENCH", "CONTROL", "CONTROL-DOMINANT"):
        rows = [r for r in all_rows if r["tag"] == label]
        if not rows:
            continue
        boxes = {(r["stem"], r["box"]): r for r in rows}
        cmax = [b["c1"]["max"] for b in boxes.values() if b["c1"]["max"] == b["c1"]["max"]]
        cemp = sum(b["c1"]["empties"] for b in boxes.values())
        cn = sum(b["c1"]["n"] for b in boxes.values())
        smax = [b["cs1"]["max"] for b in boxes.values() if b["cs1"]["max"] == b["cs1"]["max"]]
        semp = sum(b["cs1"]["empties"] for b in boxes.values())
        sn = sum(b["cs1"]["n"] for b in boxes.values())
        # MIXED RESOLUTIONS ARE NOT POOLABLE, AND THE SCRIPT SAYS SO ITSELF.
        #
        # The height-fit caches are (960, 1280); `mask_stability` writes at the
        # scan path's 1568 cap. Drift is dimensionless so each row means
        # something on its own, but a finer mask set changes granularity in ways
        # that are not the phenomenon, so a median ACROSS resolutions is not a
        # measurement of anything. Read such a row against itself.
        shapes = {r["shape"] for r in rows}
        print(f"\n  {label}  ({len(boxes)} boxes)")
        if len(shapes) > 1:
            by = {}
            for r in rows:
                by.setdefault(r["shape"], set()).add(r["stem"])
            print(f"    {YEL}MIXED RESOLUTIONS -- the medians below pool "
                  f"{len(shapes)} mask resolutions and are not comparable "
                  f"across them:{OFF}")
            for sh, stems in sorted(by.items()):
                print(f"      {DIM}{sh[1]}x{sh[0]}: {', '.join(sorted(stems))}{OFF}")
        print(f"    centroid TRANSLATE   max drift median "
              f"{(sorted(cmax)[len(cmax) // 2] if cmax else float('nan')):5.2f}   "
              f"empties {cemp}/{cn}")
        print(f"    centroid RESIZE      max drift median "
              f"{(sorted(smax)[len(smax) // 2] if smax else float('nan')):5.2f}   "
              f"empties {semp}/{sn}")
        for t in THRESHOLDS:
            sub = [r for r in rows if r["t"] == t]
            om = [r["o1"]["max"] for r in sub if r["o1"]["max"] == r["o1"]["max"]]
            oe = sum(r["o1"]["empties"] for r in sub)
            on = sum(r["o1"]["n"] for r in sub)
            nr = sum(1 for r in sub if r["near"])
            print(f"    overlap t={t:.1f}         max drift median "
                  f"{(sorted(om)[len(om) // 2] if om else float('nan')):5.2f}   "
                  f"empties {oe}/{on}   value near centroid {nr}/{len(sub)}")
    print(f"\n{DIM}  Acceptance: on the HIGH-SPREAD set the overlap rule must cut drift\n"
          f"  and empties at +-1 while landing near the centroid rule's value; the\n"
          f"  CONTROLS must be near-flat under both at +-1 and survive +-2.\n"
          f"  Anything else, including both rules looking alike, is a NEGATIVE result\n"
          f"  -- and both rules emptying at +-1 on small foods would be a negative for\n"
          f"  the overlap rule and a strong positive for the diagnosis: a quantised\n"
          f"  box should not be defining the footprint at all.{OFF}")
    print(f"{'=' * 74}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
