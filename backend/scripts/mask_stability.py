"""Does SAM2 return the same masks for the same photograph? Three calls, one answer.

WHY THIS EXISTS, AND WHY IT COMES BEFORE THE BOX-PERTURBATION REPLAY

Every spread this bench has ever reported EXCLUDES the segmenter, by construction
and by accident. `_auto_masks` memoises on the digest of the image it re-encodes
and `_SEGMENTER` is a process singleton, so `--runs 3` on one photograph makes ONE
segmenter call; runs two and three read the cache. `sam2_auto_masks found=` fires
only on a memo miss, and in the photo-35 log it appears once, not three times.

That made `--runs 3` an accidental clean experiment isolating the vision model --
which is how the 3.8x footprint swing was attributed to the model's quantised box
rather than to SAM2. But it also means the published spreads are a FLOOR, not a
total: in production every scan is a fresh photograph, the segmenter runs cold every
time, and it contributes whatever variance it has. No run to date could have
detected that. We know it was excluded, not that it is absent.

The offline box-perturbation replay holds the mask set FIXED and moves only the box.
If SAM2 varies, that assumption is false and the replay would report a clean result
for the wrong reason. So this runs first.

THE PRIOR, STATED BEFORE THE MEASUREMENT, SO A NULL RESULT MEANS SOMETHING

`meta/sam-2`'s automatic generator samples a FIXED GRID -- `points_per_side` -- not
random points, and the model version is pinned. Determinism is therefore the
EXPECTED outcome by design. That makes "no difference found" a confirmation rather
than a shrug, and it sharpens the alternative: if masks vary on a fixed grid with a
pinned version, the cause is not sampling. It is the endpoint -- non-deterministic
GPU reduction, or version drift behind the tag -- which is a different problem with
a different owner.

HOW THE MEMO IS DEFEATED, WITHOUT TOUCHING PRODUCTION

Three SEPARATE segmenter instances. `_auto_memo` is assigned in `__init__`
(segment_hosted.py:292), so instances never share a cache. The bytes, the decoded
array and the re-encode are all identical across the three calls; only the object
holding the cache differs.

Two routes were considered and rejected:

  a bench-only flag to skip the memo
      adds a code path that exists only for testing, which is the defect class this
      project has produced six times over.

  re-encoding the JPEG to change the digest
      JPEG is lossy. Measured on 17-carrots-plate.jpg, a quality-95 re-encode moves
      pixels by up to 16 levels and changes 20.5% of them. Any variance measured
      that way includes SAM2 responding to a DIFFERENT photograph -- the exact
      confound this test exists to exclude.

  a JPEG COM segment (0xFFFE) to change the digest without touching pixels
      Correct about the segment -- verified, the decoded array is bit-identical --
      but it cannot reach the hash. `encode_image` re-encodes from the ARRAY, so the
      digest is of bytes the segmenter just produced, and a comment stripped at
      decode never appears in them. The memo hits anyway.

WHAT IS COMPARED, AND WHAT IS NOT

The MASK SET, never the union. A union is taken inside an item's bounding box, the
box comes from the vision model, and the vision model is the thing we already know
varies -- two draws would differ for the reason we are trying to exclude.

  - the count, which is what `sam2_auto_masks found=` reports
  - each mask's own pixel area, sorted (the code already sorts by area at :457)
  - IoU of the matched pairs

COST: three model calls. Both mask sets are written to .npz so every later
comparison is free.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

import cv2                                                       # noqa: E402
import numpy as np                                               # noqa: E402

from scripts import _mask_cache                                  # noqa: E402

from app.services.ai import segment_hosted                       # noqa: E402
from app.services.ai.segment_hosted import encode_image          # noqa: E402

HDR, GRN, RED, YEL, DIM, OFF = ("\033[1m", "\033[32m", "\033[31m",
                                "\033[33m", "\033[2m", "\033[0m")
ROOT = pathlib.Path(__file__).resolve().parents[2]
PHOTOS = ROOT / "photos"
OUT = ROOT / "mask_overlays"

# 44-grapes-spread: the highest spread relative to mass in the unstable set
# (138 g on 90 g weighed), many separated pieces so the mask count is the most
# likely to wobble if it ever does, and it has an archived .npz for the free
# across-weeks comparison below. 42-chips-spread is the backup.
DEFAULT_PHOTO = "44-grapes-spread.jpg"
DRAWS = 3

# The SCAN PATH's decode size, which is why this script's masks are the
# comparable ones. `height_fit` uses 1280; both used to write the same
# filename. Recorded in the name and in the file now.
LONG_EDGE = 1568

# Two masks are "the same mask" above this. Well clear of anything a genuinely
# different segmentation would produce, and not a tuned number -- the answer this
# script gives should not be sensitive to it, and if it is, say so.
PAIR_IOU = 0.90


def _load(path: pathlib.Path):
    arr = cv2.imread(str(path))
    if arr is None:
        return None
    rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    if max(h, w) > 1568:                       # the scan path's own resize
        s = 1568 / max(h, w)
        rgb = cv2.resize(rgb, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return rgb


def _iou(a, b) -> float:
    union = int((a | b).sum())
    return float((a & b).sum()) / union if union else 0.0


def _match(left: list, right: list) -> tuple[list, int, int]:
    """Greedy best-IoU pairing. Returns (pair_ious, unmatched_left, unmatched_right)."""
    taken, pairs = set(), []
    for i, a in enumerate(left):
        best, best_j = 0.0, None
        for j, b in enumerate(right):
            if j in taken:
                continue
            v = _iou(a, b)
            if v > best:
                best, best_j = v, j
        if best_j is not None and best > 0:
            taken.add(best_j)
            pairs.append(best)
    return pairs, len(left) - len(pairs), len(right) - len(taken)


def _compare(label: str, a: list, b: list) -> bool:
    """Print one comparison. True when the two sets are indistinguishable."""
    ca, cb = len(a), len(b)
    areas_a = sorted(int(m.sum()) for m in a)
    areas_b = sorted(int(m.sum()) for m in b)
    same_count = ca == cb
    same_areas = areas_a == areas_b
    pairs, lost_a, lost_b = _match(a, b)
    strong = [p for p in pairs if p >= PAIR_IOU]

    print(f"\n{HDR}{label}{OFF}")
    tint = GRN if same_count else RED
    print(f"  count            {tint}{ca} vs {cb}{OFF}")
    tint = GRN if same_areas else RED
    print(f"  sorted areas     {tint}{'identical' if same_areas else 'DIFFER'}{OFF}")
    if not same_areas:
        # The first divergence, because a whole sorted list is unreadable and the
        # first difference is what names the mask that moved.
        for n, (x, y) in enumerate(zip(areas_a, areas_b)):
            if x != y:
                print(f"    first divergence at rank {n}: {x} px vs {y} px")
                break
    if pairs:
        print(f"  matched pairs    {len(pairs)}  "
              f"(IoU >= {PAIR_IOU}: {len(strong)})")
        print(f"  IoU min/median   {min(pairs):.4f} / "
              f"{sorted(pairs)[len(pairs) // 2]:.4f}")
    if lost_a or lost_b:
        print(f"  {YEL}unmatched        {lost_a} in first, {lost_b} in second{OFF}")

    # THE IoU IS NOT DECORATION, AND THE AREAS ALONE WOULD LIE.
    #
    # Measured on the archived 44-grapes set: rolling every mask 12 px sideways
    # leaves the sorted areas IDENTICAL and drops the median pair IoU to 0.709.
    # A segmenter that returned the same shapes in the wrong places would pass an
    # area-only check and be reported as stable. Position has to be checked too.
    placed = bool(pairs) and min(pairs) >= PAIR_IOU
    if same_count and same_areas and not placed:
        print(f"  {RED}areas match but positions do not -- same shapes, moved{OFF}")
    return same_count and same_areas and placed


def main() -> int:
    args = sys.argv[1:]
    name = next((a for a in args if a.endswith(".jpg")), DEFAULT_PHOTO)
    photo = PHOTOS / name
    if not photo.exists():
        print(f"  {RED}no such photograph: {photo}{OFF}")
        return 1

    print(f"\n{HDR}Is SAM2 the same twice? {name}{OFF}")
    print(f"{DIM}  Prior: meta/sam-2 samples a fixed grid (points_per_side), not random\n"
          f"  points, on a pinned version -- so determinism is the EXPECTED result.\n"
          f"  A difference would point at the endpoint, not at sampling.{OFF}")

    rgb = _load(photo)
    if rgb is None:
        print(f"  {RED}unreadable{OFF}")
        return 1

    # Proof that the three calls see the same photograph: the segmenter hashes what
    # `encode_image` produces, so that is the artefact to compare, not the file.
    blob = encode_image(rgb)
    import hashlib
    print(f"\n  encoded once: {len(blob)} bytes, "
          f"sha256 {hashlib.sha256(blob).hexdigest()[:16]}")
    print(f"  {DIM}all {DRAWS} calls send these exact bytes; only the instance "
          f"holding the memo differs{OFF}")

    shape = (rgb.shape[0], rgb.shape[1])
    draws: list[list] = []
    for n in range(1, DRAWS + 1):
        seg = segment_hosted.from_settings()          # a fresh, empty _auto_memo
        if not seg.available():
            print(f"\n  {RED}no segmenter configured -- nothing to measure{OFF}")
            return 1
        masks = seg._auto_masks(blob, shape)          # noqa: SLF001  (a lab probe)
        print(f"  draw {n}: {len(masks)} masks"
              + (f"   {RED}{seg.last_error}{OFF}" if seg.last_error else ""))
        draws.append(masks)

    for n, masks in enumerate(draws, 1):
        dest = OUT / f"stability-{photo.stem}-draw{n}-le{LONG_EDGE}.npz"
        _mask_cache.save(dest, masks, writer=_mask_cache.STABILITY,
                         long_edge=LONG_EDGE, shape=shape)
        print(f"  saved {dest.name}")

    agreed = True
    for i in range(len(draws)):
        for j in range(i + 1, len(draws)):
            agreed &= _compare(f"draw {i + 1} vs draw {j + 1}", draws[i], draws[j])

    # THE FREE ONE. Archived masks from the height work, weeks old and from a
    # different code version.
    archived = _mask_cache.path_for(OUT, _mask_cache.HEIGHT_FIT, photo.stem,
                                    1280)
    if archived.exists():
        old, _meta = _mask_cache.load(archived)
        old = old or []
        here = draws[0][0].shape if (draws and draws[0]) else None
        if old and here is not None and old[0].shape == here:
            _compare(f"draw 1 vs archived {archived.name}", draws[0], old)
        else:
            print(f"\n{DIM}  {archived.name}: {len(old)} masks at "
                  f"{old[0].shape if old else '?'} against this run's {here} -- "
                  f"different shape, so not comparable{OFF}")
        print(f"  {DIM}ASYMMETRIC EVIDENCE: a match here is strong -- stable across\n"
              f"  weeks and code versions. A mismatch is NOT conclusive, because\n"
              f"  points_per_side or the plate-hint path may have changed since.{OFF}")

    # AND UNDER THE NAME THE REPLAY READS -- NAMESPACED, BECAUSE IT USED TO
    # CLOBBER height_fit's CACHE.
    #
    # This wrote `masks-<stem>.npz` at 1568. `height_fit` READS
    # `masks-<stem>.npz` at 1280. Same name, different segmentation, last
    # writer wins, no error anywhere -- and `masks-35-slider-fries-plate.npz`
    # on disk today is this script's 1568 write sitting where height_fit
    # expects 1280. The height constants were fitted from that cache.
    #
    # Now each writer has its own namespace and the resolution is in the name
    # AND inside the file. See scripts/_mask_cache.py.
    cache = _mask_cache.path_for(OUT, _mask_cache.STABILITY, photo.stem,
                                 LONG_EDGE)
    existed = cache.exists()
    _mask_cache.save(cache, draws[0], writer=_mask_cache.STABILITY,
                     long_edge=LONG_EDGE, shape=shape)
    print(f"  {'overwrote' if existed else 'created'} {cache.name}"
          f"  {DIM}(the name `dev boxreplay` reads){OFF}")

    print(f"\n{'=' * 66}")
    if agreed:
        print(f"  {GRN}SAM2 is stable on this photograph.{OFF} The spreads in the bench\n"
              f"  report are the TOTAL, the root-cause account is complete, and the\n"
              f"  box-perturbation replay may proceed as written.")
    else:
        print(f"  {RED}SAM2 is NOT stable.{OFF} Median-sampling comes off the superseded\n"
              f"  list for a MEASURED reason, and the box-perturbation replay needs a\n"
              f"  second axis before its result can mean anything.")
        print(f"  {YEL}masks-{photo.stem}.npz was written from DRAW 1. The replay assumes\n"
              f"  a fixed mask set, so its result on this photograph is moot until the\n"
              f"  segmenter's own variance is accounted for.{OFF}")
    print(f"{'=' * 66}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
