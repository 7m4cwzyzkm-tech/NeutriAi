"""Can a BOX-PROMPTED model return a whole-burger mask? One number decides it.

WHY THIS IS THE BINDING QUESTION

`dev dumpmask` drew production's own pool for photo 35 and no mask in it is
burger-sized:

    the model's burger box          9.00% of frame
    the largest mask in the pool    36,174 px = 1.96% of frame
    the three masks actually taken  1.35% of frame after overlap

SAM2's automatic generator never produced a whole-burger mask, so the shrink
guard refusing that footprint is catching a real segmentation failure rather
than misfiring. And `meta/sam-2`'s published inputs are exactly

    image, points_per_side, pred_iou_thresh, stability_score_thresh, use_m2m

-- no prompt of any kind. It cannot be asked for the burger. That makes
composite foods structurally unmeasurable by the configured path, and it
explains "9 live, 12 shadow" better than any tuning hypothesis.

`vufinder/sam3` publishes `prompts`, taking per-concept JSON with `text`,
`positive_boxes`, `negative_boxes`, `positive_points`, `negative_points`, all
normalised to [0,1]. It is the first candidate that can be handed the box the
vision model already produces.

THE TEST, AND IT IS ONE NUMBER

Feed it photo 35 and the burger box. Report the returned mask's area as a
fraction of frame.

    near 9%       composite foods are measurable; the 12 shadow footprints
                  become a solvable problem and the segmenter should change
    1-2%          the ceiling is real, this is not a SAM2 quirk, and the
                  design has to work around composites rather than through
                  them

OUTSIDE THE PIPELINE, ON PURPOSE

Nothing here imports or touches `segment_hosted`'s call path, and integrating
is a separate decision made after the number exists. `encode_image` IS reused,
because the point is to send the bytes production sends -- a probe on a
differently-encoded image is the mistake that invalidated a week of replays.

DRY BY DEFAULT. Prints the payload and spends nothing without `--go`.
"""
from __future__ import annotations

import base64
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv

    load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")
except Exception:  # noqa: BLE001
    pass

import numpy as np                                               # noqa: E402
from PIL import Image, ImageDraw, ImageOps                       # noqa: E402

from app.config import settings                                  # noqa: E402
from app.services.ai.segment_hosted import encode_image          # noqa: E402

ESC = "\033"
HDR, GRN, RED, YEL, DIM, CYN, OFF = (
    ESC + "[1m", ESC + "[32m", ESC + "[31m", ESC + "[33m",
    ESC + "[2m", ESC + "[36m", ESC + "[0m")

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHOTOS = ROOT / "photos"
OUT = ROOT / "mask_overlays"

MODEL = "vufinder/sam3"
PHOTO = "35-slider-fries-plate.jpg"

# Production's own box for the burger, from
# docs/evidence/2026-09-12-photo35-production-masks.npz -- pixel corners at
# 1176x1568, which is what the scan path decodes this photograph to.
BURGER_BOX_PX = (352, 392, 705, 862)

# What SAM2 managed on this item, for the comparison the whole probe is for.
SAM2_UNION_FRAC = 0.0135
SAM2_BEST_MASK_FRAC = 36174 / (1176 * 1568)

LONG_EDGE = 1568


def _corners_to_sam3(x0, y0, x1, y1, W, H):
    """Pixel corners -> sam3's normalised CENTRE form.

    NOT x,y,w,h. The schema says "Boxes are in the format of: center_x,
    center_y, width, height", normalised to [0,1]. Passing corner-origin
    coordinates would put the box up and to the left by half its own size and
    return a mask of the wrong thing -- which would read exactly like a
    negative result.
    """
    return [((x0 + x1) / 2) / W, ((y0 + y1) / 2) / H,
            (x1 - x0) / W, (y1 - y0) / H]


def _load_production_bytes():
    """The photograph as the scan path encodes it, and its decoded array."""
    img = ImageOps.exif_transpose(Image.open(PHOTOS / PHOTO)).convert("RGB")
    img.thumbnail((LONG_EDGE, LONG_EDGE), Image.LANCZOS)
    rgb = np.array(img)
    return rgb, encode_image(rgb)


def _plate_negative_box(rgb, W, H):
    """The plate's bounding box, so the model can be told what is NOT food."""
    from app.services.ai import food_seg

    circle = food_seg._plate_by_hough(rgb)          # noqa: SLF001
    if circle is None or not circle.any():
        return None
    ys, xs = np.nonzero(circle)
    return _corners_to_sam3(int(xs.min()), int(ys.min()),
                            int(xs.max()), int(ys.max()), W, H)


def build_variants(rgb):
    """The prompts to try, cheapest-and-sharpest first. One prediction each."""
    H, W = rgb.shape[:2]
    box = _corners_to_sam3(*BURGER_BOX_PX, W, H)
    neg = _plate_negative_box(rgb, W, H)

    variants = [
        ("box only", {"positive_boxes": [box]}),
        ("box + text", {"text": "cheeseburger", "positive_boxes": [box]}),
    ]
    if neg is not None:
        variants.append(
            ("box + text - plate",
             {"text": "cheeseburger", "positive_boxes": [box],
              "negative_boxes": [neg]}))
    return variants, box, neg


def _report_plan(variants, box, neg, rgb):
    H, W = rgb.shape[:2]
    print(f"\n{HDR}sam3 probe -- PLAN ONLY, nothing spent{OFF}")
    print(f"  {DIM}model {MODEL}   photo {PHOTO}   decoded {W}x{H} "
          f"(the scan path's {LONG_EDGE}){OFF}")
    print(f"\n  burger box   px {BURGER_BOX_PX}")
    print(f"  {DIM}-> sam3 centre form "
          f"[{box[0]:.5f}, {box[1]:.5f}, {box[2]:.5f}, {box[3]:.5f}]{OFF}")
    if neg:
        print(f"  plate negative  {DIM}[{neg[0]:.5f}, {neg[1]:.5f}, "
              f"{neg[2]:.5f}, {neg[3]:.5f}]{OFF}")
    print(f"\n  {CYN}{len(variants)} prediction(s){OFF}, one per variant:")
    for name, spec in variants:
        print(f"    {name:22s} {json.dumps(spec)[:96]}")
    print(f"\n  {DIM}for comparison, what SAM2 produced on this item:")
    print(f"    box                 {BURGER_BOX_PX[2]-BURGER_BOX_PX[0]}x"
          f"{BURGER_BOX_PX[3]-BURGER_BOX_PX[1]} px = "
          f"{(BURGER_BOX_PX[2]-BURGER_BOX_PX[0])*(BURGER_BOX_PX[3]-BURGER_BOX_PX[1])/(W*H):.2%}"
          f" of frame")
    print(f"    union it took       {SAM2_UNION_FRAC:.2%} of frame")
    print(f"    best single mask    {SAM2_BEST_MASK_FRAC:.2%} of frame{OFF}")
    print(f"\n  {YEL}Add --go to spend it.{OFF}\n")


def _predict(blob, spec, key):
    import httpx

    uri = "data:image/jpeg;base64," + base64.b64encode(blob).decode()
    r = httpx.post(
        f"https://api.replicate.com/v1/models/{MODEL}/predictions",
        timeout=300,
        headers={"Authorization": f"Bearer {key}",
                 "Prefer": "wait"},
        json={"input": {"image": uri, "prompts": [json.dumps(spec)]}},
    )
    return r.status_code, (r.json() if r.content else {})


def main() -> int:
    argv = sys.argv[1:]
    rgb, blob = _load_production_bytes()
    variants, box, neg = build_variants(rgb)

    if "--go" not in argv:
        _report_plan(variants, box, neg, rgb)
        return 0

    key = settings.segmenter_api_key
    if not key:
        print(f"\n  {RED}no SEGMENTER_API_KEY{OFF}\n")
        return 1
    print(f"\n{HDR}sam3 probe{OFF}  {DIM}key {len(key)} chars, prefix "
          f"{key[:4]!r}   {len(variants)} prediction(s){OFF}")

    H, W = rgb.shape[:2]
    frame = float(H * W)
    worst = 0
    for name, spec in variants:
        status, body = _predict(blob, spec, key)
        if status not in (200, 201):
            print(f"  {RED}{name}: HTTP {status}{OFF} "
                  f"{DIM}{str(body)[:200]}{OFF}")
            worst = 1
            continue
        print(f"\n  {CYN}{name}{OFF}  {DIM}status "
              f"{body.get('status')}{OFF}")
        out = body.get("output")
        print(f"    {DIM}output keys/type: "
              f"{list(out) if isinstance(out, dict) else type(out).__name__}{OFF}")
        # The output shape is NOT assumed. Printed, then decoded if it turns
        # out to be a mask image; a probe that guessed the schema and reported
        # 0.00% would be indistinguishable from a real negative.
        print(f"    {DIM}{json.dumps(out)[:400] if out is not None else 'no output'}{OFF}")
    print()
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
