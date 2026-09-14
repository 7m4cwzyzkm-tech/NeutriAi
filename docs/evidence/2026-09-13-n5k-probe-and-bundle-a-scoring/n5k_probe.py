# PROVENANCE
# The PAID Nutrition5k depth-rung probe, 13 Sep 2026, run at HEAD 303f6b5 (result: n5k_probe.json/.log).
# Moved from a session scratchpad; only REPO was rewritten to be relative.
# COST: with n5k/detections present it buys no vision call, but it does NOT block the LLM
# nutrition fallback -- run n5k_rung_acceptance.py for a zero-cost replay of these detections.
# rgb images are downloaded into n5k/rgb/ on first use (CC BY 4.0) and not committed.
#
"""Nutrition5k depth-rung probe. PAID: one vision detection + second_look + SAM2 per dish,
plus an LLM nutrition estimate for any name USDA cannot answer.

    python n5k_probe.py --dry        selection, downloads, rung arithmetic only; no model call
    python n5k_probe.py              the probe

Per dish, ONE detection, two arms built from it:
  CLEAN   camera_distance_mm 359, camera_fov_deg 69.05 (both from the paper: 35.9 cm and
          5.957e-3 cm^2/px at 640x480), no plate diameter, no calibration -- the request a
          depth-capable phone would send.
  BENCH   the same, plus what _run_scan adds for the bench account: its saved `dinner_plate`
          calibration (254 mm, 50,670.75 mm^2) IF the model names the container dinner_plate
          (_calibration_fits is an exact label match). Shows whether rung 1 pre-empts rung 2.
Nutrition facts are resolved once per dish and shared by both arms. Nothing is written:
no meals, items, scans, usage rows, and no food_facts rows (the shared cache).
`refine` is not run, as in the replay rig.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import statistics as st
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
N5K = HERE / "n5k"
REPO = Path(__file__).resolve().parents[3]
os.chdir(REPO / "backend")
sys.path.insert(0, str(REPO / "backend"))
import structlog  # noqa: E402

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
from app.services.ai import portion, vision  # noqa: E402
from app.services.ai.portion import GeometryHint  # noqa: E402
from app.services.nutrition import resolver  # noqa: E402

BUCKET = "https://storage.googleapis.com/nutrition5k_dataset/nutrition5k_dataset/imagery/realsense_overhead"
DISTANCE_MM = 359.0
PX_AREA_CM2 = 5.957e-3
FOV_DEG = 2 * math.degrees(math.atan((640 * math.sqrt(PX_AREA_CM2) / 2) / 35.9))
TRUE_FRAME_MM2 = 640 * 480 * PX_AREA_CM2 * 100
BENCH_CAL = {"vessel": "dinner_plate", "real_diameter_mm": 254.0, "real_area_mm2": 50670.75}


# ---- nothing persisted -------------------------------------------------------
async def _no_usage(*_a, **_k):
    return None


vision.record_usage = _no_usage
resolver._store = lambda fact, key: {**fact, "display_name": fact["name"], "canonical_key": key}


def rgb_bytes(dish_id: str) -> bytes:
    path = N5K / "rgb" / f"{dish_id}.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{BUCKET}/{dish_id}/rgb.png", timeout=60) as r:
            path.write_bytes(r.read())
    return path.read_bytes()


def hint_for(det: dict, prepared, arm: str) -> GeometryHint:
    container = str(det.get("container") or "").strip().lower() or None
    cal = BENCH_CAL if (arm == "BENCH" and vision._calibration_fits(BENCH_CAL, container)) else None
    return GeometryHint(
        plate_ellipse_area_ratio=(float(det.get("plate_area_ratio") or 0) or None),
        plate_ellipse_wh=vision._plate_ellipse(det, prepared.aspect),
        plate_diameter_mm=(cal["real_diameter_mm"] if cal else None),
        reference_area_mm2=(cal["real_area_mm2"] if cal else None),
        depth_mm=DISTANCE_MM, camera_fov_deg=FOV_DEG, aspect_ratio=prepared.aspect, image_count=1,
        vessel=(str(det.get("container")) if det.get("container") else None),
        vessel_shape=(str(det.get("container_shape")) if det.get("container_shape") else None),
        reference_kind=(prepared.reference.kind if prepared.reference else None),
        reference_frame_width_mm=(prepared.reference.frame_width_mm if prepared.reference else None),
        reference_tilt_deg=(prepared.reference.tilt_deg if prepared.reference else None),
    )


async def main() -> int:
    dry = "--dry" in sys.argv
    sel = json.loads((N5K / "selection.json").read_text(encoding="utf-8"))["dishes"]
    print(f"FOV from the paper {FOV_DEG:.2f} deg; rig frame {TRUE_FRAME_MM2:,.0f} mm^2")
    cache = N5K / "detections"
    cache.mkdir(exist_ok=True)
    rows = []
    for d in sel:
        raw = rgb_bytes(d["id"])
        prepared = vision.downscale_jpeg(raw)
        if dry:
            h = GeometryHint(depth_mm=DISTANCE_MM, camera_fov_deg=FOV_DEG, aspect_ratio=prepared.aspect)
            mm2, m = portion.mm2_per_frame(h)
            print(f"  {d['id']}  {len(raw):7d} bytes  aspect {prepared.aspect:.4f}  card "
                  f"{'yes' if prepared.reference else 'no'}  clean-hint rung {m}  {mm2 / TRUE_FRAME_MM2:.4f}x rig")
            continue
        cpath = cache / f"{d['id']}.json"
        if cpath.exists():
            blob = json.loads(cpath.read_text(encoding="utf-8"))
        else:
            det = await vision.detect_foods([prepared.b64], None)
            det = {k: v for k, v in det.items() if k != "_call"}
            items = [x for x in (det.get("items") or []) if isinstance(x, dict)]
            notes = await vision.second_look(items, prepared.raw, None) if items else []
            blob = {"detection": det, "second_look_notes": notes}
            cpath.write_text(json.dumps(blob), encoding="utf-8")
        det = blob["detection"]
        dets = [x for x in (det.get("items") or []) if isinstance(x, dict)]
        if not dets:
            rows.append({"id": d["id"], "error": det.get("_error") or "no items"})
            print(f"  {d['id']}: no items"); continue
        names = [vision._lookup_name(x) for x in dets]
        real_many = resolver.resolve_many
        facts = await real_many(names)

        async def shared(_n, _f=facts):
            return copy.deepcopy(_f)
        resolver.resolve_many = shared
        out = {"id": d["id"], "truth": {k: d[k] for k in ("kcal", "mass", "fat", "carb", "protein")},
               "container": det.get("container"), "container_shape": det.get("container_shape"),
               "card": bool(prepared.reference), "arms": {}}
        try:
            for arm in ("CLEAN", "BENCH"):
                hint = hint_for(det, prepared, arm)
                mm2, rung = portion.mm2_per_frame(hint)
                items, _ = await vision.build_items(copy.deepcopy(dets), hint, raw=prepared.raw,
                                                    plate_bbox=(det.get("plate_bbox") if isinstance(det.get("plate_bbox"), dict) else None),
                                                    user_id=None)
                tot = {"mass": sum(i.grams for i in items),
                       "kcal": sum(i.macros.kcal for i in items), "fat": sum(i.macros.fat_g for i in items),
                       "carb": sum(i.macros.carbs_g for i in items), "protein": sum(i.macros.protein_g for i in items)}
                out["arms"][arm] = {"frame_rung": rung, "frame_mm2": mm2,
                                    "frame_vs_rig": (mm2 / TRUE_FRAME_MM2) if mm2 else None,
                                    "item_methods": [i.estimation_method for i in items],
                                    "items": [(i.name, round(i.grams, 1), round(i.macros.kcal, 1)) for i in items],
                                    "totals": tot}
        finally:
            resolver.resolve_many = real_many
        rows.append(out)
        c = out["arms"]["CLEAN"]
        b = out["arms"]["BENCH"]
        t = out["truth"]
        print(f"  {d['id']} container={det.get('container')!s:12s} CLEAN rung {c['frame_rung']:16s} "
              f"{c['frame_vs_rig']:.4f}x  mass {c['totals']['mass']:6.0f}/{t['mass']:5.0f} "
              f"({100 * (c['totals']['mass'] / t['mass'] - 1):+5.0f}%)  kcal {c['totals']['kcal']:5.0f}/{t['kcal']:5.0f} "
              f"({100 * (c['totals']['kcal'] / t['kcal'] - 1):+5.0f}%)  | BENCH rung {b['frame_rung']} "
              f"kcal {100 * (b['totals']['kcal'] / t['kcal'] - 1):+5.0f}%  items {c['item_methods']}")
        (HERE / "n5k_probe.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    if dry:
        return 0
    ok = [r for r in rows if "arms" in r]
    for arm in ("CLEAN", "BENCH"):
        rung_counts = {}
        for r in ok:
            rung_counts[r["arms"][arm]["frame_rung"]] = rung_counts.get(r["arms"][arm]["frame_rung"], 0) + 1
        print(f"\n{arm}: frame rung {rung_counts}; dishes {len(ok)} (one per built-up plate)")
        for q in ("kcal", "mass", "carb"):
            est = [r["arms"][arm]["totals"][q] for r in ok]
            tru = [r["truth"][q] for r in ok]
            mae = st.mean(abs(e - t) for e, t in zip(est, tru))
            per = [abs(e / t - 1) * 100 for e, t in zip(est, tru) if t > 2]
            print(f"  {q:5s} MAE {mae:6.1f} = {100 * mae / st.mean(tru):5.1f}% of mean ({st.mean(tru):.0f})  "
                  f"per-dish mean {st.mean(per):5.1f}%  median {st.median(per):5.1f}%  signed "
                  f"{st.mean((e / t - 1) * 100 for e, t in zip(est, tru) if t > 2):+5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
