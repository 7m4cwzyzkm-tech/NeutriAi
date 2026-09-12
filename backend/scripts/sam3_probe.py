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
    print(f"\n{HDR}  THE TRANSPORT, since the first attempt used the wrong one{OFF}")
    print(f"    POST  {DIM}https://api.replicate.com/v1/predictions{OFF}")
    print(f"    {DIM}headers  Authorization: Bearer <key>,  Prefer: wait{OFF}")
    print(f'    {DIM}body     {{"version": "<hash>", "input": '
          f'{{"image": "data:image/jpeg;base64,...", '
          f'"prompts": ["<json>"]}}}}{OFF}')
    print(f"    {DIM}sent by HostedSegmenter._predict -- production's own call "
          f"path, not a second one{OFF}")
    print(f"\n    {DIM}The first attempt posted to "
          f"/v1/models/{MODEL}/predictions, the OFFICIAL-models")
    print(f"    endpoint, which 404s for a community model. Three 404s, no "
          f"prediction")
    print(f"    object created, nothing billed -- confirmed: "
          f"GET /v1/predictions holds")
    print(f"    no sam3 prediction at all.{OFF}")
    print(f"\n  {YEL}Add --go to spend it.{OFF}")


def resolve_version(key) -> tuple[str | None, str]:
    """The version hash a community model's prediction must name.

    `POST /v1/models/{owner}/{name}/predictions` is the OFFICIAL-models
    endpoint and 404s for a community model however correct the slug is. That
    is exactly what the first three attempts hit: three 404s, no prediction
    object created, nothing billed -- confirmed against GET /v1/predictions,
    which holds no sam3 prediction at all.
    """
    import httpx

    r = httpx.get(f"https://api.replicate.com/v1/models/{MODEL}",
                  headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if r.status_code != 200:
        return None, f"GET /v1/models/{MODEL} -> HTTP {r.status_code}"
    body = r.json()
    vid = ((body.get("latest_version") or {}).get("id") or "")
    if not vid:
        return None, f"{MODEL} publishes no latest_version.id"
    return vid, (f"{MODEL} is {body.get('visibility')}, "
                 f"{body.get('run_count')} runs")


def _predict(blob, spec, key, version, timeout_s=300.0):
    """Production's own transport. NOT a second implementation of it.

    `HostedSegmenter._predict` posts to /v1/predictions with
    {"version": ..., "input": ...}, sends `Prefer: wait`, polls `urls.get`
    until the status is terminal, and separates a transport failure from a
    model failure in `last_error`. It makes these calls every day in
    production.

    The first version of this probe rolled its own POST and 404'd -- the same
    mistake as a renderer reimplementing the selection rule, which is why
    `_mask_verdict` was extracted rather than copied.
    """
    from app.services.ai.segment_hosted import HostedSegmenter

    seg = HostedSegmenter(dialect="replicate", api_key=key, version=version,
                          timeout_s=timeout_s)
    uri = "data:image/jpeg;base64," + base64.b64encode(blob).decode()
    body = {"version": version,
            "input": {"image": uri, "prompts": [json.dumps(spec)]}}
    payload = seg._predict(body)          # noqa: SLF001  (a lab probe)
    return payload, seg.last_error


def main() -> int:
    argv = sys.argv[1:]
    rgb, blob = _load_production_bytes()
    variants, box, neg = build_variants(rgb)

    key = settings.segmenter_api_key
    if not key:
        print(f"\n  {RED}no SEGMENTER_API_KEY{OFF}\n")
        return 1

    # Resolved in the DRY form too. The version hash is the thing the first
    # attempt was missing, so it is the thing the dry run has to show before
    # anyone spends on it.
    version, note = resolve_version(key)

    if "--go" not in argv:
        _report_plan(variants, box, neg, rgb)
        print(f"\n  {DIM}{note}{OFF}")
        if version:
            print(f"  resolved version id  {GRN}{version}{OFF}\n")
            return 0
        print(f"  {RED}could not resolve a version id -- do not run --go{OFF}\n")
        return 1

    if not version:
        print(f"\n  {RED}{note}{OFF}\n")
        return 1
    print(f"\n{HDR}sam3 probe{OFF}  {DIM}key {len(key)} chars, prefix "
          f"{key[:4]!r}   {len(variants)} prediction(s){OFF}")
    print(f"  {DIM}version {version}{OFF}")

    H, W = rgb.shape[:2]
    frame = float(H * W)
    worst = 0
    for name, spec in variants:
        body, err = _predict(blob, spec, key, version)
        if not body:
            # RAW, AND THEN STOP. An identical failure across prompts is a
            # transport or access question, and trying further variants would
            # spend money answering it.
            print(f"\n  {RED}{name}: no prediction{OFF}")
            print(f"    {DIM}{err}{OFF}")
            print(f"\n  {YEL}Stopping rather than trying the remaining "
                  f"variants.{OFF} {DIM}The same failure on every prompt is "
                  f"not a prompt problem.{OFF}\n")
            return 1
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
