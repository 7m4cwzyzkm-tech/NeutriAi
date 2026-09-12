#!/usr/bin/env python3
"""Run the scan pipeline stage by stage, in-process, with real tracebacks.

    dev scandebug "C:\\path\\to\\photo.jpg"

Why this exists: the API deliberately hides stack traces from clients, and
the pipeline is written to degrade rather than throw. So a 500 from /scans
tells you almost nothing. This runs the same stages directly, catches each
one separately, and prints exactly which stage failed and why.

Stages, in order:
    1  config        settings load, keys present
    2  auth          sign in, and make sure the profiles row exists
    3  upload        push the photo to Supabase Storage
    4  download      pull it back with the service key   <- RLS / bucket issues
    5  downscale     PIL decode + resize                 <- HEIC, CMYK, EXIF
    6  vision        the actual model call               <- keys, billing, quota
    7  resolve       USDA lookup for each detected name
    8  portion       geometry -> grams
    9  full          vision.run_scan end to end

It never writes to scan_results.csv -- this is a diagnostic, not a bench.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GRN, RED, YEL, DIM, HDR, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

_failed = False


def stage(n: int, name: str):
    print(f"\n{HDR}[{n}] {name}{OFF}")


def ok(msg: str) -> None:
    print(f"  {GRN}ok{OFF}    {msg}")


def bad(msg: str, exc: BaseException | None = None) -> None:
    global _failed
    _failed = True
    print(f"  {RED}FAIL{OFF}  {msg}")
    if exc is not None:
        print(f"\n{DIM}" + "-" * 68 + OFF)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        print(f"{DIM}" + "-" * 68 + f"{OFF}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--plate", type=float, default=None)
    args = ap.parse_args()

    photo = Path(args.image)
    if not photo.exists():
        print(f"{RED}No such file: {photo}{OFF}")
        return 1

    print(f"\n{HDR}Scan pipeline diagnostic{OFF}  {DIM}{photo.name}"
          f"  ({photo.stat().st_size / 1024:.0f} KB){OFF}")

    # ---- 1 config -------------------------------------------------------
    stage(1, "config")
    try:
        from app.config import settings
        ok(f"loaded, env={settings.env}")
        for key in ("anthropic_api_key", "openai_api_key", "supabase_url",
                    "supabase_service_key"):
            val = getattr(settings, key, "") or ""
            if not val:
                bad(f"{key} is empty")
            else:
                ok(f"{key} set ({len(val)} chars)")
    except Exception as exc:
        bad("settings failed to load", exc)
        return 1

    # ---- 2 auth ---------------------------------------------------------
    stage(2, "auth")
    try:
        from scripts.scan_bench import token_for_bench, upload
        token, uid = token_for_bench()
        ok(f"signed in, user {uid[:8]}...")
        # Stages 8 and 9 write rows that reference profiles(id). Going through
        # the API this is handled by the auth dependency; here we bypass HTTP,
        # so do the same bootstrap explicitly.
        from app.services.identity import ensure_profile
        prof = ensure_profile(uid, None)
        ok(f"profile present, handle {prof.get('handle')}")
    except Exception as exc:
        bad("bench sign-in failed", exc)
        return 1

    # ---- 3 upload -------------------------------------------------------
    stage(3, "upload")
    try:
        key = upload(photo, uid, token)
        ok(f"stored at {key}")
    except SystemExit:
        bad("upload failed -- see the HTTP error above")
        return 1
    except Exception as exc:
        bad("upload raised", exc)
        return 1

    # ---- 4 download (service key) ---------------------------------------
    stage(4, "download with service key")
    raw = b""
    try:
        from app.db import service
        raw = service().storage.from_("meal-photos").download(key)
        ok(f"{len(raw)} bytes back")
    except Exception as exc:
        bad("service-key download failed -- this is what makes fetch_images "
            "return nothing", exc)
        return 1

    # ---- 5 downscale ----------------------------------------------------
    stage(5, "decode + downscale")
    b64 = ""
    try:
        from app.services.ai.vision import downscale_jpeg
        prepared = downscale_jpeg(raw)
        b64, aspect = prepared.b64, prepared.aspect
        if prepared.reference:
            r = prepared.reference
            ok(f"reference: {r.kind}, frame {r.frame_width_mm:.0f} mm across, "
               f"{r.consensus} edge settings agreed, tilt {r.tilt_deg:.0f} deg")
        else:
            ok("reference: none found in the pixels (normal for most photos)")
        ok(f"base64 length {len(b64)}"
           + (f", aspect {aspect:.3f} ({'portrait' if aspect < 1 else 'landscape'})" if aspect else ""))
        try:
            import base64 as _b64
            import io as _io

            from PIL import Image
            img = Image.open(_io.BytesIO(_b64.b64decode(b64)))
            ok(f"decodes as {img.format} {img.size[0]}x{img.size[1]} {img.mode}")
        except Exception as exc:
            bad("the downscaled bytes are not a readable image -- PIL likely "
                "failed on the original and it fell back to raw base64", exc)
    except Exception as exc:
        bad("downscale raised", exc)
        return 1

    # ---- 6 vision -------------------------------------------------------
    stage(6, "vision call")
    detection = {}
    try:
        from app.services.ai.vision import detect_foods
        detection = await detect_foods([b64], uid)
        if detection.get("_error"):
            bad(f"model returned no usable payload: {detection['_error']}")
        else:
            names = [d.get("name") for d in (detection.get("items") or [])]
            ok(f"plate_detected={detection.get('plate_detected')} "
               f"plate_area_ratio={detection.get('plate_area_ratio')}")

            # The plate's outline gives the camera tilt for free: a round plate
            # is a circle from straight above and an ellipse from an angle, and
            # h/w is cos(tilt). Printed because it decides whether any reported
            # height is a measurement or an invention.
            from app.services.ai.portion import GeometryHint
            from app.services.ai.vision import _plate_ellipse
            wh = _plate_ellipse(detection)
            tilt = GeometryHint(
                plate_ellipse_wh=wh,
                vessel_shape=detection.get("container_shape"),
            ).tilt_deg
            if wh:
                ok(f"plate_ellipse w={wh[0]:.3f} h={wh[1]:.3f}  ->  tilt "
                   f"{'unknown (not a round vessel)' if tilt is None else f'{tilt:.0f} deg'}")
            else:
                ok("plate_ellipse not reported — no tilt reading, height priors will be used")

            ok(f"detected: {names or 'nothing'}")
            for d in detection.get("items") or []:
                hr = d.get("height_ratio")
                # The peak height the model claims, converted to mm against the
                # plate. This is the number to check with a ruler before
                # USE_MEASURED_HEIGHT is ever turned back on.
                as_mm = ""
                if hr and detection.get("plate_diameter_mm"):
                    as_mm = f" (~{float(hr) * float(detection['plate_diameter_mm']):.0f} mm)"
                print(f"        {DIM}{d.get('name'):<24} area_ratio="
                      f"{d.get('area_ratio')} coverage={d.get('plate_coverage')} "
                      f"height_ratio={hr}{as_mm} shape={d.get('shape')} "
                      f"typical={d.get('typical_serving_g')}{OFF}")
    except Exception as exc:
        bad("vision call raised", exc)
        return 1

    # ---- 7 resolve ------------------------------------------------------
    stage(7, "nutrition resolve")
    try:
        from app.services.nutrition import resolver
        names = [str(d.get("name") or "food").strip().lower()
                 for d in (detection.get("items") or [])]
        if not names:
            print(f"  {YEL}skip{OFF}  nothing detected to resolve")
        else:
            facts = await resolver.resolve_many(names)
            for n in names:
                f = facts.get(n)
                if f:
                    ok(f"{n} -> {f.get('display_name')} ({f.get('source')})")
                else:
                    bad(f"{n} -> NO MATCH in any nutrition provider")
    except Exception as exc:
        bad("resolver raised", exc)

    # ---- 8 portion ------------------------------------------------------
    stage(8, "portion estimate")
    try:
        from app.services.ai.portion import GeometryHint
        from app.services.ai.vision import build_items
        hint = GeometryHint(
            plate_ellipse_area_ratio=(float(detection.get("plate_area_ratio") or 0) or None),
            plate_diameter_mm=args.plate,
            reference_area_mm2=None,
            image_count=1,
        )
        items, notes = await build_items(detection.get("items") or [], hint)
        for it in items:
            ok(f"{it.name:<24} {it.grams:7.1f}g  [{it.grams_low:.0f}-{it.grams_high:.0f}]  "
               f"{it.estimation_method}  conf {it.confidence:.2f}")
        for n in notes:
            print(f"        {DIM}{n}{OFF}")
    except Exception as exc:
        bad("portion estimation raised", exc)

    # ---- 9 full run -----------------------------------------------------
    stage(9, "full run_scan")
    try:
        from app.db import one, service
        from app.services.ai import vision
        scan = one(service().table("food_scans").insert({
            "user_id": uid, "image_paths": [key], "status": "pending",
        }).execute())
        result = await vision.run_scan(
            user_id=uid, scan_id=scan["id"], image_paths=[key],
            meal_slot=None, calibration_id=None, plate_diameter_mm=args.plate,
        )
        ok(f"status={result.status} items={len(result.items)} "
           f"kcal={result.totals.kcal:.0f} confidence={result.overall_confidence}")
        for n in result.notes:
            print(f"        {DIM}{n}{OFF}")
    except Exception as exc:
        bad("run_scan raised -- THIS is the 500 the API was hiding", exc)

    print()
    if _failed:
        print(f"{RED}{HDR}Something failed above. The first FAIL is the one that "
              f"matters -- later ones are usually knock-on.{OFF}\n")
        return 1
    print(f"{GRN}{HDR}Every stage passed.{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
