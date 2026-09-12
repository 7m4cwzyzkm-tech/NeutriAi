"""What the pipeline ACTUALLY chose, drawn on the photograph. Offline, free.

WHY THIS EXISTS AND WHY `dev mask` COULD NOT DO IT

`mask_lab` renders the COLOUR rule over three retired photographs (07, 13, 14)
from a hardcoded table of hand-typed box centres, and ignores a filename
argument entirely -- it reads only the string "--overlay" from argv. The
footprints that set the grams come from SAM2, through
`segment_hosted.segment_boxes`, on the model's own boxes. Those two have never
been the same picture, and the bench spent weeks advising an overlay of the
wrong one.

So this reads a MASK DUMP -- written by the pipeline at the moment it chose,
keyed by the digest of the bytes sent to the model -- and draws:

    the plate hint          the fence the pool was bounded to
    the model's box         per item, exactly as the model returned it
    the union               the pixels that became this item's footprint
    every other mask        outlined, labelled with WHY it was not taken

The verdicts come from `HostedSegmenter._mask_verdict`, the same method
`_union_in_box` calls. Not a reimplementation of the rule -- the rule itself.
A picture that disagreed with production would be worse than no picture.

WHAT IT ANSWERS, AND EACH ANSWER IS A DIFFERENT FIX

  * the union covers the food        -> the footprint is fine, look at scale
  * the union covers part of it      -> the selection rule is dropping food
  * the box does not reach the food  -> the model's box is undersized

On photo 35 the fries need 43.5 mm of height to reach their weighed 65 g even
with a perfect mask AND a corrected scale, against a table whose tallest
loose-food entry is 21.0 mm. The height cannot be the answer, so one of the
three above must be -- which is exactly what this distinguishes.

NO MODEL CALLS. The dump is already on disk or this does nothing.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv

    load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")
except Exception:  # noqa: BLE001
    pass

import cv2                                                       # noqa: E402
import numpy as np                                               # noqa: E402
from PIL import Image, ImageDraw, ImageOps                       # noqa: E402

from app.services.ai import segment_hosted                       # noqa: E402
from app.services.ai.segment_hosted import load_mask_dump        # noqa: E402

ESC = "\033"
HDR, GRN, RED, YEL, DIM, CYN, OFF = (
    ESC + "[1m", ESC + "[32m", ESC + "[31m", ESC + "[33m",
    ESC + "[2m", ESC + "[36m", ESC + "[0m")

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHOTOS = ROOT / "photos"
DUMPS = ROOT / "mask_dumps"
OUT = ROOT / "mask_overlays"

# Set by `--photo`. Shape alone cannot identify the photograph and the
# digest cannot either; see `_photo_for`.
WANT = None

# Taken masks are drawn SOLID, refused ones only OUTLINED. A refused mask
# painted as heavily as a taken one is how an overlay misleads: the eye reads
# area, and the whole question here is which pixels COUNTED.
UNION_RGB = (255, 48, 48)      # what became the footprint
DROPPED_RGB = (64, 140, 255)   # considered and refused
HINT_RGB = (255, 214, 64)      # the plate fence
BOX_RGB = (0, 230, 120)        # the model's box

_S = segment_hosted.HostedSegmenter
VERDICT_NOTE = {
    _S.SWALLOWS_BOX: "fills the box -- the plate, or the table under it",
    _S.OVER_BOX: "bigger than the box allows",
    _S.OFF_CENTRE: "centroid outside the box",
    _S.RING: "ring -- its own centre is not on it",
    _S.EMPTY: "empty",
}


def _decode(path):
    """Exactly the scan path's decode, or None. Same longest edge, same EXIF."""
    try:
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        img.thumbnail((1568, 1568), Image.LANCZOS)
        return np.array(img)
    except Exception:  # noqa: BLE001
        return None


def _photo_for(shape, hint, want=None):
    """The photograph this dump was made from. Identified BY ITS PLATE.

    SHAPE IS NOT ENOUGH AND THE FIRST VERSION OF THIS WAS WRONG BECAUSE OF IT:
    seventeen photographs in this bench decode to 1176x1568, so shape matching
    drew photo 35's masks over a plate of carrots and said so only in a
    warning. An overlay on the wrong photograph is worse than no overlay --
    every conclusion drawn from it is about a picture that was never scanned.

    The digest cannot settle it either, which is this whole session's finding:
    production uploads, fetches and re-encodes, so the bytes it hashed are not
    the bytes a local file encodes to. `mask_stability` got 199239e25505b5e3
    where production got 2bce2dc323bf.

    So match on the PLATE HINT, which the dump carries. It is the plate found
    in production's own decode, and the same plate found in the local decode
    agrees with it to within re-encoding jitter -- a rim is a big, stable
    feature. Reported as an IoU so the reader can see how firm the match is
    rather than trusting a silent pick, and `--photo NAME` overrides it
    outright when the operator simply knows.
    """
    H, W = shape
    if want:
        for path in sorted(PHOTOS.glob("*.jpg")):
            if want.lower() in path.name.lower():
                rgb = _decode(path)
                if rgb is None or rgb.shape[:2] != (H, W):
                    return [], f"{path.name} does not decode to {W}x{H}"
                return [(rgb, path.name, None)], None
        return [], f"no photograph matching {want!r}"

    sized = []
    for path in sorted(PHOTOS.glob("*.jpg")):
        rgb = _decode(path)
        if rgb is not None and rgb.shape[:2] == (H, W):
            sized.append((rgb, path.name))
    if hint is None or not len(sized):
        return [(r, n, None) for r, n in sized], None

    from app.services.ai import food_seg

    scored = []
    for rgb, name in sized:
        circle = food_seg._plate_by_hough(rgb)          # noqa: SLF001
        if circle is None:
            continue
        inter = int((circle & hint).sum())
        union = int((circle | hint).sum())
        scored.append((inter / union if union else 0.0, rgb, name))
    if not scored:
        return [(r, n, None) for r, n in sized], None
    scored.sort(key=lambda t: -t[0])
    return [(r, n, iou) for iou, r, n in scored], None


def _paint(canvas, mask, rgb, alpha):
    canvas[mask] = ((1 - alpha) * canvas[mask]
                    + alpha * np.array(rgb)).astype(np.uint8)


def _outline(draw, mask, rgb):
    """A two-pixel border, so a refused mask shows its extent without mass."""
    cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_LIST,
                               cv2.CHAIN_APPROX_SIMPLE)
    for c in cnts:
        if len(c) < 2:
            continue
        pts = [(int(p[0][0]), int(p[0][1])) for p in c]
        draw.line(pts + [pts[0]], fill=rgb, width=2)


def render(path: pathlib.Path) -> int:
    d = load_mask_dump(path)
    H, W = d["shape"]
    hits, why = _photo_for((H, W), d["plate_hint"], want=WANT)
    print("\n" + HDR + path.name + OFF + "  " + DIM
          + "digest " + d["digest"][:12]
          + ", " + str(len(d["raw"])) + " masks returned, "
          + str(len(d["pool"])) + " in the pool, "
          + str(len(d["boxes"])) + " box(es), "
          + str(W) + "x" + str(H) + OFF)
    if not hits:
        print("  " + RED + (why or ("no photograph in " + str(PHOTOS)
              + " decodes to " + str(W) + "x" + str(H))) + " -- cannot draw" + OFF)
        return 1
    photo, name, iou = hits[0]
    if iou is None:
        print("  " + DIM + "photograph: " + name + " (named, not matched)" + OFF)
    else:
        runner = hits[1][2] if len(hits) > 1 and hits[1][2] is not None else 0.0
        firm = iou >= 0.90 and (iou - runner) >= 0.15
        print("  " + (DIM if firm else YEL) + "photograph: " + name
              + " (plate IoU %.3f" % iou
              + (", next best %.3f" % runner if len(hits) > 1 else "")
              + ")" + OFF)
        if not firm:
            print("  " + YEL + "THE MATCH IS NOT FIRM -- do not trust this "
                  "drawing." + OFF + DIM
                  + " Name the photograph with `dev dumpmask --photo <name>`; "
                  + str(len(hits)) + " decode to this size." + OFF)

    seg = _S()
    written = 0
    frame = float(H * W)
    for i, box in enumerate(d["boxes"]):
        geom = seg.box_pixels(box, (H, W))
        if geom is None:
            print("  item " + str(i) + ": " + YEL + "no usable box" + OFF)
            continue
        x0, y0, x1, y1 = geom

        canvas = photo.copy()
        if d["plate_hint"] is not None:
            _paint(canvas, d["plate_hint"], HINT_RGB, 0.14)

        union = np.zeros((H, W), bool)
        rows = []
        for m in d["pool"]:
            verdict, facts = seg._mask_verdict(m, geom)      # noqa: SLF001
            if verdict == seg.TAKEN:
                union |= m
            rows.append((verdict, facts, m))

        _paint(canvas, union, UNION_RGB, 0.50)
        img = Image.fromarray(canvas)
        draw = ImageDraw.Draw(img)
        for verdict, _f, m in rows:
            if verdict != seg.TAKEN:
                _outline(draw, m, DROPPED_RGB)
        draw.rectangle([x0, y0, x1, y1], outline=BOX_RGB, width=4)

        took = sum(1 for v, _f, _m in rows if v == seg.TAKEN)
        print("  " + CYN + "item " + str(i) + OFF
              + "  box (%d, %d, %d, %d)" % (x0, y0, x1, y1)
              + "  %.2f%% of frame" % ((x1 - x0) * (y1 - y0) / frame * 100)
              + "  union %.4f of frame from %d mask(s)" % (union.mean(), took))
        for verdict, f, _m in sorted(rows, key=lambda r: -r[1]["area"]):
            tag = (GRN + "TAKEN  " + OFF) if verdict == seg.TAKEN \
                else (DIM + "dropped" + OFF)
            why = "" if verdict == seg.TAKEN else \
                "  " + DIM + VERDICT_NOTE.get(verdict, verdict) + OFF
            print("      " + tag
                  + " %8d px  %6.2f%% of frame  over_box %5.2f"
                  % (f["area"], f["area"] / frame * 100, f.get("over_box", 0.0))
                  + why)

        OUT.mkdir(exist_ok=True)
        dest = OUT / ("dump-" + d["digest"][:12] + "-item" + str(i) + ".png")
        img.save(dest)
        print("      " + GRN + "->" + OFF + " " + str(dest))
        written += 1

    print("\n  " + DIM
          + "solid red = the union that set the grams.  "
          + "blue outline = considered and refused.\n  "
          + "green = the model's box.  "
          + "yellow wash = the plate fence the pool was bounded to." + OFF)
    return 0 if written else 1


def main() -> int:
    global WANT
    argv = sys.argv[1:]
    if "--photo" in argv:
        i = argv.index("--photo")
        WANT = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith("-")]
    if not DUMPS.exists():
        print("\n" + RED + "no " + str(DUMPS) + OFF)
        print("  " + DIM
              + "Scan with NUTRIAI_MASK_DUMP set -- `dev api` sets it, but a\n"
              + "  RELOADING server does NOT pick it up: reload re-reads code,\n"
              + "  not the environment. Stop it and start it again." + OFF + "\n")
        return 1
    files = sorted(DUMPS.glob("masks-*.npz"))
    if args:
        files = [f for f in files if any(a in f.name for a in args)]
    if not files:
        print("\n" + RED + "no dumps in " + str(DUMPS) + OFF
              + (DIM + " matching " + " ".join(args) + OFF if args else ""))
        return 1
    print("\n" + HDR + "Production's own masks, drawn" + OFF + "  "
          + DIM + str(len(files)) + " dump(s). Zero model calls." + OFF)
    worst = 0
    for f in files:
        worst = max(worst, render(f))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
