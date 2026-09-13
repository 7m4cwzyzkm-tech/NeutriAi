#!/usr/bin/env python3
r"""Is the depth endpoint wired up, and is it returning a depth map or a picture?

    dev depthcheck                     offline -- no key, no network, no cost
    dev depthcheck 13                  one bench photo, end to end
    dev depthcheck "C:\photos\x.jpg" --width 254

WHY THIS EXISTS
---------------
Wiring a hosted model up wrong is quiet. The endpoint answers, the image is
valid, the shapes line up, the bill arrives -- and every height is nonsense,
because what came back was a COLOUR-MAPPED rendering of a depth map rather than
the map. Dark red and dark blue sit at opposite ends of the scale and both come
out dark, so depth stops being monotonic and the plane fit lands anywhere.

So this asks the endpoint one question at a time and says which answer it gave:

    is a provider configured
    did it answer, and how fast
    is it a depth map or a picture of one
    how many levels does it carry (8-bit is 256 across the whole scene)
    can the plate's plane be found in it
    how far off square is the camera, and what does that cost
    what height does the food come out at, against the prior it replaces

RUN IT OFFLINE FIRST. With no arguments it puts a ray-traced plate through the
decode and the geometry with no network at all, so the install can be proved
before a key is pasted or a cent is spent.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GRN, RED, YEL, DIM, HDR, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
PHOTOS = Path(__file__).resolve().parents[2] / "photos"
REPO = Path(__file__).resolve().parents[2]

# Everything printed is also written to scratch/, without the colour codes.
#
# So the command is just `dev depthcheck --survey` -- no `> file 2>&1`, which
# is one more thing to get right and, run twice by a stray paste, fails with
# "the file is being used by another process" and looks like a bug in the tool.
#
# SCRATCH, NOT THE REPO ROOT. It used to land next to the repo, and a stale
# `depthsurvey.txt` sat there for days reporting "9 of 16 measurable" from
# box-ellipse tilts the code had already refused. A transcript is one run's
# output: it goes to scratch/, and anything worth keeping is written into a notes
# file or docs/evidence/ under its own dated name.
TRANSCRIPT_DIR = REPO / "scratch"
TRANSCRIPT: list[str] = []
_ANSI = __import__("re").compile(r"\033\[[0-9;]*m")


def say(line: str = ""):
    print(line)
    TRANSCRIPT.append(_ANSI.sub("", line))


def saved(name: str) -> None:
    """Tell the user where the transcript went, or that it could not be saved."""
    where = write_transcript(name)
    if where is None:
        print(f"  {YEL}could not write a transcript{OFF}  {DIM}the folder is read-only{OFF}\n")
    else:
        print(f"  {DIM}saved to {where.relative_to(REPO).as_posix()}{OFF}\n")


def write_transcript(name: str) -> Path | None:
    """Save the run into scratch/, under a name nothing else is holding open."""
    try:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    for candidate in [TRANSCRIPT_DIR / name] + [TRANSCRIPT_DIR / f"{Path(name).stem}-{i}.txt"
                                                for i in range(2, 10)]:
        try:
            candidate.write_text("\n".join(TRANSCRIPT) + "\n", encoding="utf-8")
            return candidate
        except OSError:
            continue
    return None


def ok(label, detail=""):
    say(f"  {GRN}PASS{OFF}  {label}" + (f"   {DIM}{detail}{OFF}" if detail else ""))


def bad(label, detail=""):
    say(f"  {RED}FAIL{OFF}  {label}" + (f"   {DIM}{detail}{OFF}" if detail else ""))


def note(label, detail=""):
    say(f"  {DIM}....{OFF}  {label}" + (f"   {DIM}{detail}{OFF}" if detail else ""))


# ---------------------------------------------------------------------------
# offline: prove the decode and the geometry without spending anything
# ---------------------------------------------------------------------------

def offline() -> int:
    import numpy as np

    from app.services.ai import depth_hosted as H
    from app.services.ai import depth_map as D

    say(f"\n{HDR}Offline self-test{OFF}  {DIM}no key, no network, no cost{OFF}\n")

    # A ray-traced plate: a real pinhole camera, a plate tilted 35 degrees, and
    # a dome whose mean height over its own footprint is exactly 20.0 mm.
    from tests.test_depth_map import _scene
    depth, plate, food = _scene("hemisphere", peak_mm=30.0)

    fit = D.fit_plate(depth, plate)
    if fit is None:
        bad("the plate's plane could not be fitted to a synthetic plate")
        return 1
    ok("plate plane fitted", f"tilt {fit.tilt_deg:.1f} deg, "
       f"error amplification {fit.error_amplification:.1f}x")

    got = D.measure_heights(depth, plate, [food], 254.0)[0]
    if got is None:
        bad("a ray-traced 20.0 mm mound measured nothing")
        return 1
    err = abs(got - 20.0) / 20.0
    (ok if err < 0.10 else bad)("geometry recovers a known height",
                                f"{got:.2f} mm against a true 20.00 mm")

    # the decode, over the same map an endpoint would return
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray((depth * 65535).astype(np.uint16)).save(buf, "PNG")
    back = H.decode_depth(buf.getvalue(), depth.shape)
    if back is None:
        bad("a 16-bit PNG did not survive the decode")
        return 1
    ok("16-bit PNG decoded", f"{back.shape[1]}x{back.shape[0]}, "
       f"{len(np.unique(back)):,} distinct levels")

    round_trip = D.measure_heights(back, plate, [food], 254.0)[0]
    (ok if round_trip and abs(round_trip - 20.0) / 20.0 < 0.10 else bad)(
        "the same height survives the round trip", f"{round_trip} mm")

    # and the refusal that matters
    ys = np.mgrid[0:120, 0:160][0] / 119.0
    r = np.clip(1.5 - abs(ys - 0.75) * 4, 0, 1)
    g = np.clip(1.5 - abs(ys - 0.50) * 4, 0, 1)
    b = np.clip(1.5 - abs(ys - 0.25) * 4, 0, 1)
    buf = io.BytesIO()
    Image.fromarray((np.dstack([r, g, b]) * 255).astype(np.uint8), "RGB").save(buf, "PNG")
    (ok if H.decode_depth(buf.getvalue(), (120, 160)) is None else bad)(
        "a colour-mapped image is refused", "the one wiring mistake that hides")

    say(f"\n  {DIM}The measurement and the decode are sound. What is not proved\n"
        f"  here is your endpoint -- run `dev depthcheck 13` for that.{OFF}\n")
    return 0


# ---------------------------------------------------------------------------
# online: ask the configured endpoint
# ---------------------------------------------------------------------------

async def plate_of(photo: Path):
    """The plate's mask and the tilt it reports, with no depth call at all.

    Split out because the tilt is the thing that decides whether a photograph
    can be depth-measured AT ALL, and it costs nothing to find out: the tilt is
    the plate ellipse's minor axis over its major, which is pixels. So this
    question can be answered before a depth provider exists, before a key is
    pasted, and before a cent is spent.
    """
    import numpy as np
    from PIL import Image, ImageOps

    from app.services.ai import depth_map as D
    from app.services.ai import food_seg, vision

    img = ImageOps.exif_transpose(Image.open(photo)).convert("RGB")
    img.thumbnail((1568, 1568), Image.LANCZOS)
    rgb = np.asarray(img)

    try:
        detection = await vision.detect_foods(
            [vision.downscale_jpeg(photo.read_bytes()).b64], "depth-probe")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"the vision model could not be reached: {str(exc)[:90]}")
    items = [d for d in (detection.get("items") or []) if isinstance(d, dict)]
    plate_bbox = detection.get("plate_bbox")
    if not items and not plate_bbox:
        # Say what actually went wrong. `detect_foods` puts the provider's own
        # message in `_error`, and an earlier version of this function ignored
        # it and printed a guess -- "is OPENAI_API_KEY set?" -- across sixteen
        # photographs when the real answer, in the response every time, was
        # "You have no credits remaining". A diagnostic that guesses is worse
        # than no diagnostic: it sends someone to check the one thing that was
        # fine.
        raise RuntimeError(str(detection.get("_error")
                               or "the vision model returned nothing"))
    if not plate_bbox:
        return rgb, items, None, None, None, False
    mask, source = food_seg.plate_surface(rgb, plate_bbox)
    if mask is None or not mask.any():
        return rgb, items, plate_bbox, None, None, False
    if not food_seg.plate_is_measured(source):
        # A box-derived ellipse can fence food but cannot report a tilt: its
        # axis ratio is the model's bounding box, which for most photographs is
        # the aspect ratio of the image. Reporting one would be reporting the
        # camera's sensor shape as the angle of the plate.
        return rgb, items, plate_bbox, mask, None, False
    # Two gates, not one. The tilt decides whether the scale can be pinned;
    # the frame decides whether the plate has a measurable ellipse at all. On a
    # first look at the bench photographs the SECOND one may bite harder -- a
    # plate photographed close fills the shot -- so it is reported separately
    # rather than folded into a single yes or no.
    h, w = mask.shape[:2]
    on_border = int(mask[0, :].sum() + mask[-1, :].sum()
                    + mask[:, 0].sum() + mask[:, -1].sum())
    cut = on_border > D.BORDER_TOUCH_TOLERANCE * 2 * (h + w)

    axes = D.plate_axes_px(mask)
    if axes is None:
        return rgb, items, plate_bbox, mask, None, cut
    major, minor = axes
    cos = min(1.0, minor / major)
    return (rgb, items, plate_bbox, mask,
            float(np.degrees(np.arccos(cos))), cut)


def tilt_verdict(tilt):
    from app.services.ai import depth_map as D
    if tilt is None:
        return None, ("the plate's outline was never measured -- only the "
                      "model's box, which carries no angle")
    amp = (1.0 / max(np.tan(np.radians(tilt)) ** 2, 1e-9)) + 2.0
    if tilt < D.MIN_TILT_DEG:
        return False, (f"{tilt:.1f} deg -- too square-on. A 1% error in the "
                       f"plate ellipse would be {amp:.0f}% on the height")
    if tilt > D.MAX_TILT_DEG:
        return False, f"{tilt:.1f} deg -- nearly edge-on, the food hides its own footprint"
    return True, f"{tilt:.1f} deg, a 1% ellipse error costs {amp:.1f}%"


async def survey() -> int:
    """Which of the bench photographs could be depth-measured at all?

    The honest question to ask before paying for a depth model: this method
    needs the camera off square, and a flat overhead photograph of a plate
    carries no information about its own scale. If most real photographs are
    overhead, the answer is a capture nudge -- "tilt the phone a little" -- not
    a bigger model. One vision call per photo; no depth calls.
    """
    say(f"\n{HDR}Which photographs can be depth-measured?{OFF}  "
          f"{DIM}plate tilt only -- no depth calls, nothing spent{OFF}\n")
    photos = sorted(PHOTOS.glob("*.jpg"))
    usable = 0
    for photo in photos:
        try:
            _rgb, _items, plate_bbox, _mask, tilt, cut = await plate_of(photo)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)
            say(f"  {photo.name:<38} {RED}failed{OFF} {DIM}{reason[:90]}{OFF}")
            # An account problem is the same on every photograph. Stopping is
            # both faster and clearer than fifteen more identical failures
            # scrolling the real message off the top of the screen.
            if any(w in reason.lower() for w in
                   ("credit", "quota", "billing", "401", "invalid_api_key",
                    "authentication")):
                say(f"\n  {RED}Stopping -- this is an account problem, not a "
                    f"photograph problem.{OFF}")
                say(f"  {DIM}{reason[:300]}{OFF}\n")
                return 1
            continue
        if not plate_bbox:
            say(f"  {photo.name:<38} {DIM}no plate -- paper, tray or container{OFF}")
            continue
        good, why = tilt_verdict(tilt)
        if cut:
            say(f"  {photo.name:<38} {YEL}refused{OFF}     "
                  f"{DIM}the frame cuts the plate -- no measurable ellipse{OFF}")
        elif good:
            usable += 1
            say(f"  {photo.name:<38} {GRN}measurable{OFF}  {DIM}{why}{OFF}")
        else:
            say(f"  {photo.name:<38} {YEL}refused{OFF}     {DIM}{why}{OFF}")
    say(f"\n  {usable} of {len(photos)} photographs can be depth-measured.")
    say(f"  {DIM}Every one that does not keeps its prior height, exactly as today.\n"
        f"  If that fraction is low, the fix is at capture time -- ask for a\n"
        f"  slight angle -- not a larger depth model.{OFF}\n")
    return 0


async def against_photo(photo: Path, plate_mm: float | None) -> int:
    import numpy as np

    from app.config import settings
    from app.services.ai import depth_map as D
    from app.services.ai import vision

    say(f"\n{HDR}{photo.name}{OFF}\n")

    # The tilt first, because it costs nothing and it is the question that
    # decides whether the rest is worth asking.
    try:
        rgb, items, plate_bbox, plate_mask, tilt, cut = await plate_of(photo)
    except RuntimeError as exc:
        bad("could not read the photo's geometry", str(exc))
        return 1
    if not plate_bbox:
        bad("no plate found in the photo", "the scale has nothing to stand on")
        return 1
    ok("plate found", f"{len(items)} items on it")
    if plate_mask is None:
        bad("the plate's pixels could not be masked")
        return 1
    (bad if cut else ok)(
        "whole plate in frame",
        "the frame cuts the plate -- its ellipse is whatever the crop left"
        if cut else "the ellipse is complete")
    good, why = tilt_verdict(tilt)
    (ok if good else bad)("camera angle usable", why)
    good = good and not cut

    provider = vision.DEPTH_PROVIDER
    if not provider.available():
        note("no depth provider configured",
             f"DEPTH_PROVIDER={settings.depth_provider or '(unset)'}")
        say(f"\n  {DIM}Everything above was free. To measure heights, set\n"
              f"  DEPTH_PROVIDER, DEPTH_API_KEY, DEPTH_MODEL_VERSION, DEPTH_OUTPUT_FIELD\n"
              f"  and DEPTH_MODEL_SIZE=Small (required -- the licence) in .env --\n"
              f"  .env.example lists every one of them with what it is for.{OFF}\n")
        return 0 if good else 1
    ok("provider configured", provider.name)
    if not good:
        say(f"\n  {DIM}Not calling the endpoint: this photograph would be refused\n"
              f"  anyway, and a refused measurement should not be a paid one.{OFF}\n")
        return 1

    started = time.monotonic()
    depth = provider.depth(rgb)
    ms = int((time.monotonic() - started) * 1000)
    if depth is None:
        bad("the endpoint returned nothing usable", f"{ms} ms")
        say(f"\n  {DIM}Either it errored, or what came back was refused. The log line\n"
              f"  says which: depth_colour_mapped_rejected, depth_replicate_http,\n"
              f"  depth_output_unrecognised or depth_provider_failed.{OFF}\n")
        return 1
    ok("depth map returned", f"{ms} ms, {depth.shape[1]}x{depth.shape[0]}")

    levels = len(np.unique(depth))
    (ok if levels > 512 else note)(
        "levels carried", f"{levels:,}" + ("" if levels > 512
                                           else "  -- 8-bit; ask for 16-bit if you can"))

    if not D.orientation_is_sane(depth, plate_mask):
        bad("the map reads food as BELOW the plate",
            "this model returns depth, not inverse depth")
        return 1
    ok("orientation sane", "food reads nearer than the bare rim")

    fit = D.fit_plate(depth, plate_mask)
    if fit is None:
        bad("the plate's plane could not be fitted",
            "food on the rim, or the map is too rough")
        return 1

    diameter = plate_mm or 254.0
    scale = D.mm_per_depth_unit(fit, diameter)
    if scale is None:
        bad("the scale could not be pinned", f"plate taken as {diameter:.0f} mm")
        return 1
    ok("scale pinned", f"1 depth unit = {scale:.4g} mm, plate {diameter:.0f} mm")

    from app.services.ai import food_seg
    from app.services.ai.portion import (
        HEIGHT_PRIORS_MM, PROFILE_FACTORS, _classify_shape,
    )
    masks = food_seg.item_masks(rgb, [d.get("bbox") for d in items], plate_bbox)
    heights = D.measure_heights(depth, plate_mask, masks, diameter)

    say(f"\n  {HDR}{'food':<26}{'measured':>10}{'prior':>10}{'change':>10}{OFF}")
    for det, h in zip(items, heights):
        name = str(det.get("name") or "?")[:24]
        shape = _classify_shape(det, None)
        prior = HEIGHT_PRIORS_MM.get(shape, 24.0) * PROFILE_FACTORS.get(shape, 1.0)
        if h is None:
            say(f"  {name:<26}{DIM}{'--':>10}{OFF}{prior:>9.1f}mm"
                  f"{DIM}{'not measured':>12}{OFF}")
            continue
        pct = (h - prior) / prior * 100
        colour = GRN if abs(pct) < 40 else YEL
        say(f"  {name:<26}{colour}{h:>9.1f}mm{OFF}{prior:>9.1f}mm{colour}{pct:>9.0f}%{OFF}")

    measured = [h for h in heights if h is not None]
    amp = fit.error_amplification
    say(f"\n  {DIM}{len(measured)} of {len(items)} items measured. Height enters the "
          f"weight linearly,\n  so a {amp:.0f}x amplified 1% ellipse error is "
          f"{amp:.0f}% on the grams.{OFF}\n")
    return 0 if measured else 1


def resolve(target: str) -> Path | None:
    p = Path(target)
    if p.exists():
        return p
    hits = sorted(PHOTOS.glob(f"{target.zfill(2)}*")) if target.isdigit() else []
    if not hits:
        hits = sorted(PHOTOS.glob(f"*{target}*"))
    return hits[0] if hits else None


async def main() -> int:
    ap = argparse.ArgumentParser(description="check the depth endpoint end to end")
    ap.add_argument("photo", nargs="?",
                    help="a photo path, or a bench photo number. omit for the "
                         "offline self-test")
    ap.add_argument("--width", type=float, default=None,
                    help="the plate's real diameter in mm (default 254)")
    ap.add_argument("--survey", action="store_true",
                    help="which bench photos clear the tilt floor (no depth calls)")
    args = ap.parse_args()

    # Saved here rather than inside each mode, so a run that stops early still
    # leaves a transcript -- an early exit is exactly the run worth reading.
    if args.survey:
        code, name = await survey(), "depthsurvey.txt"
    elif not args.photo:
        code, name = offline(), "depthcheck.txt"
    else:
        photo = resolve(args.photo)
        if photo is None:
            say(f"{RED}no photo matching {args.photo}{OFF}  {DIM}looked in {PHOTOS}{OFF}")
            code, name = 1, "depthcheck.txt"
        else:
            code, name = await against_photo(photo, args.width), "depthcheck.txt"
    saved(name)
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
