# PROVENANCE
# Bundle A piece 2 ACCEPTANCE, 13 Sep 2026, run at a2fc9b7 (result: n5k_rung_piece2.json).
# Moved from a session scratchpad; OLD/REPO rewritten to be relative, and rgb images are now
# downloaded on first use instead of read from the scratchpad. Zero paid calls: vision, second_look
# and the LLM nutrition fallback are blocked. MUST RE-RUN after Bundle A piece 3.
#
"""BUNDLE A PIECE 2 ACCEPTANCE: the 16 cached Nutrition5k detections, zero model calls.

    python n5k_rung_acceptance.py <label>

For each dish, ONE cached detection (+ cached second_look), nutrition facts resolved once
and shared by both arms, two hints:
  CLEAN   camera distance 359 mm, FOV 69.05 deg, no calibration
  BENCH   the same, plus the bench account's saved `dinner_plate` calibration (254 mm),
          passed through vision._calibration_scale with origin "vessel" -- exactly what
          _run_scan does after a label match
PASS (piece 2): BENCH == CLEAN dish for dish (rung, grams, kcal), on all 16.
Blocked: every paid path (vision detection, second_look, the LLM nutrition fallback) and
every database write. USDA searches for names not cached are free and allowed.
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
from pathlib import Path

OLD = Path(__file__).resolve().parent
N5K = OLD / "n5k"
HERE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[3]
os.chdir(REPO / "backend")
sys.path.insert(0, str(REPO / "backend"))
import structlog  # noqa: E402

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
from app.services.ai import portion, vision  # noqa: E402
from app.services.ai.portion import GeometryHint  # noqa: E402
from app.services.nutrition import resolver  # noqa: E402

if not hasattr(vision, "_calibration_scale"):
    sys.exit("vision._calibration_scale does not exist -- piece 2 is not in this checkout")

DISTANCE_MM = 359.0
FOV_DEG = 2 * math.degrees(math.atan((640 * math.sqrt(5.957e-3) / 2) / 35.9))
BENCH_CAL = {"vessel": "dinner_plate", "real_diameter_mm": 254.0, "real_area_mm2": 50670.75}


def _blocked(*_a, **_k):
    raise RuntimeError("paid call attempted")


vision.detect_foods = _blocked
vision.second_look = _blocked
vision.ask_vision = _blocked


async def _no_usage(*_a, **_k):
    return None


async def _no_ai(name):
    raise RuntimeError(f"LLM nutrition fallback blocked for {name!r}")


vision.record_usage = _no_usage
resolver._ai_estimate = _no_ai
resolver._store = lambda fact, key: {**fact, "display_name": fact["name"], "canonical_key": key}


def hint_for(det, prepared, arm):
    """BENCH_OLD is the same saved calibration with scale_inferred forced False -- the
    ordering before piece 2, in the same run and on the same facts, so before and after
    share everything but the rung order."""
    container = str(det.get("container") or "").strip().lower() or None
    if arm in ("BENCH", "BENCH_OLD"):
        diameter, area, inferred = vision._calibration_scale(None, BENCH_CAL, "vessel", container)
        if arm == "BENCH_OLD":
            inferred = False
    else:
        diameter, area, inferred = None, None, False
    return GeometryHint(
        plate_ellipse_area_ratio=(float(det.get("plate_area_ratio") or 0) or None),
        plate_ellipse_wh=vision._plate_ellipse(det, prepared.aspect),
        plate_diameter_mm=diameter, reference_area_mm2=area, scale_inferred=inferred,
        depth_mm=DISTANCE_MM, camera_fov_deg=FOV_DEG, aspect_ratio=prepared.aspect, image_count=1,
        vessel=(str(det.get("container")) if det.get("container") else None),
        vessel_shape=(str(det.get("container_shape")) if det.get("container_shape") else None),
        reference_kind=(prepared.reference.kind if prepared.reference else None),
        reference_frame_width_mm=(prepared.reference.frame_width_mm if prepared.reference else None),
        reference_tilt_deg=(prepared.reference.tilt_deg if prepared.reference else None),
    )


async def main(label):
    sel = json.loads((N5K / "selection.json").read_text(encoding="utf-8"))["dishes"]
    rows, fails = [], []
    for d in sel:
        rgb_path = N5K / "rgb" / f"{d['id']}.png"
        if not rgb_path.exists():
            import urllib.request
            rgb_path.parent.mkdir(parents=True, exist_ok=True)
            url = ("https://storage.googleapis.com/nutrition5k_dataset/nutrition5k_dataset/"
                   f"imagery/realsense_overhead/{d['id']}/rgb.png")
            with urllib.request.urlopen(url, timeout=60) as resp:
                rgb_path.write_bytes(resp.read())
        raw = rgb_path.read_bytes()
        prepared = vision.downscale_jpeg(raw)
        det = json.loads((N5K / "detections" / f"{d['id']}.json").read_text(encoding="utf-8"))["detection"]
        dets = [x for x in (det.get("items") or []) if isinstance(x, dict)]
        names = [vision._lookup_name(x) for x in dets]
        real = resolver.resolve_many
        try:
            facts = await real(names)
        except Exception as exc:  # noqa: BLE001
            fails.append((d["id"], str(exc)[:80]))
            facts = {}

        async def shared(_n, _f=facts):
            return copy.deepcopy(_f)
        resolver.resolve_many = shared
        out = {"id": d["id"], "truth": {k: d[k] for k in ("kcal", "mass")}, "container": det.get("container"), "arms": {}}
        try:
            for arm in ("CLEAN", "BENCH", "BENCH_OLD"):
                hint = hint_for(det, prepared, arm)
                mm2, rung = portion.mm2_per_frame(hint)
                items, _ = await vision.build_items(copy.deepcopy(dets), hint, raw=prepared.raw,
                                                    plate_bbox=(det.get("plate_bbox") if isinstance(det.get("plate_bbox"), dict) else None),
                                                    user_id=None)
                out["arms"][arm] = {"rung": rung, "mm2": mm2, "grams": [round(i.grams, 2) for i in items],
                                    "kcal": sum(i.macros.kcal for i in items), "mass": sum(i.grams for i in items)}
        finally:
            resolver.resolve_many = real
        c, b, o = out["arms"]["CLEAN"], out["arms"]["BENCH"], out["arms"]["BENCH_OLD"]
        same = (c["rung"] == b["rung"] and c["grams"] == b["grams"])
        out["identical"] = same
        rows.append(out)
        print(f"  {d['id']} container={str(det.get('container')):12s} CLEAN {c['rung']:15s} BENCH {b['rung']:15s} "
              f"BENCH_OLD {o['rung']:15s} kcal {c['kcal']:6.0f} / {b['kcal']:6.0f} / {o['kcal']:6.0f}  "
              f"{'IDENTICAL' if same else 'DIFFERENT'}")
    (HERE / f"n5k_rung_{label}.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    for arm in ("CLEAN", "BENCH", "BENCH_OLD"):
        e = [r["arms"][arm]["kcal"] for r in rows]
        t = [r["truth"]["kcal"] for r in rows]
        mae = st.mean(abs(x - y) for x, y in zip(e, t))
        per = [abs(x / y - 1) * 100 for x, y in zip(e, t)]
        print(f"[{label}] {arm}: rungs {sorted({r['arms'][arm]['rung'] for r in rows})}  energy MAE/mean {100 * mae / st.mean(t):.1f}%  "
              f"per-dish mean {st.mean(per):.1f}%  median {st.median(per):.1f}%")
    print(f"[{label}] dishes identical CLEAN vs BENCH: {sum(r['identical'] for r in rows)}/16"
          + (f"   facts lookups that failed (no LLM allowed): {fails}" if fails else ""))


asyncio.run(main(sys.argv[1]))
