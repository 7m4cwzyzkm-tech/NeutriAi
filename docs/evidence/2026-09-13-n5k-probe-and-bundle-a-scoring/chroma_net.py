# PROVENANCE
# Detector net effect on the meal score, 13 Sep 2026, at 532a573 (before the detector shipped):
# the replay rig run twice per photo, grey vs chroma x3, with committed height branches.
# Result chroma_net.json: UNCAL 37.3% -> 43.4% MAE/mean. Zero paid calls.
# Moved from a session scratchpad; only RIG rewritten to be relative.
#
"""NET EFFECT of the chroma card detector on the meal score. Zero paid calls.

Uses the committed replay rig's helpers (cached detection, recorded densities, injected
SAM2 footprints, reconstructed height branch, every paid path blocked). Per photo, two
runs that differ ONLY in the detector inside downscale_jpeg:
    grey    shipped find_reference            -- must reproduce the recorded grams (control)
    chroma  find_reference on chroma x3 (FROZEN gain), same gates
Arms: CAL (declared plate; rung 1b outranks the card, so it must NOT move) and UNCAL
(a subscriber with no calibration -- where the card can take the scale).
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import io
import json
import math
import runpy
import statistics as st
import sys
from collections import Counter
from pathlib import Path

import numpy as np

RIG = Path(__file__).resolve().parents[1] / "2026-09-13-meal-replay"
sys.path.insert(0, str(RIG))
import replay as RP  # noqa: E402  (applies the rig's patches; main() is not run)
import cv2  # noqa: E402
from PIL import Image  # noqa: E402

HERE = Path(__file__).resolve().parent
GAIN = 3.0
real_find = RP.vision.find_reference
CALLS = Counter()


def chroma_find(img):
    CALLS["chroma"] += 1
    lab = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2LAB).astype(np.float32)
    c = np.clip(np.sqrt((lab[:, :, 1] - 128) ** 2 + (lab[:, :, 2] - 128) ** 2) * GAIN, 0, 255).astype(np.uint8)
    return real_find(Image.fromarray(np.dstack([c, c, c])))


def grey_find(img):
    CALLS["grey"] += 1
    return real_find(img)


async def main():
    recorded = json.loads((RP.INPUTS / "paired_v2.json").read_text(encoding="utf-8"))
    committed = json.loads((RIG / "replay.json").read_text(encoding="utf-8"))
    out = {}
    for name, rec in recorded.items():
        entry = rec["entry"]
        detection = json.loads((RP.INPUTS / "sweep_cache" / f"{name}.json").read_text(encoding="utf-8"))["detection"]
        facts = {n: {"display_name": n, "density_g_ml": v["density_g_ml"], "source": v["source"]}
                 for n, v in rec["facts"].items()}

        async def shared(_names, _f=facts):
            return {n: dict(_f[n]) for n in _names if n in _f}
        RP.resolver.resolve_many = shared
        n_items = len(rec["arms"]["CAL"])
        used = [next((rec["arms"][a][i]["measured_used"] for a in rec["arms"]
                      if rec["arms"][a][i]["measured_used"] is not None), None) for i in range(n_items)]
        pieces = committed[name]["pieces"]
        RP.vision._measured_areas = lambda d, r, b, _u=used, _p=pieces: (list(_u), list(_p), "sam2")
        row = {"entry": entry, "vessel": detection.get("container")}
        for det, fn in (("grey", grey_find), ("chroma", chroma_find)):
            RP.vision.find_reference = fn
            fetched = await RP.vision.fetch_images("meal-photos", [f"{RP.FOLDER}/{RP.STORED[name]}"])
            ref = fetched[0].reference if fetched else None
            cal = await RP.arm(detection, fetched, entry["plate"], entry["distance"])
            unc = await RP.arm(detection, fetched, None, None)
            mm2_unc, rung_unc = RP.frame_mm2(detection, fetched, None, None)
            mm2_cal, rung_cal = RP.frame_mm2(detection, fetched, entry["plate"], entry["distance"])
            row[det] = {"card": bool(ref), "card_frame_width_mm": (ref.frame_width_mm if ref else None),
                        "rung_unc": rung_unc, "rung_cal": rung_cal, "mm2_unc": mm2_unc, "mm2_cal": mm2_cal,
                        "CAL": cal, "UNCAL": unc}
        RP.vision.find_reference = real_find
        RP.resolver.resolve_many = RP._blocked
        ctrl = max(RP.diff(row["grey"][a], rec["arms"][a]) for a in ("CAL", "UNCAL"))
        cal_moved = RP.diff(row["grey"]["CAL"], row["chroma"]["CAL"])
        row["control_max_abs_g"], row["cal_moved_g"] = ctrl, cal_moved
        out[name] = row
        g, c = row["grey"], row["chroma"]
        print(f"{name:32s} control {ctrl:5.2f} g | card {('Y' if g['card'] else '-')}->{('Y' if c['card'] else '-')} "
              f"vessel {str(row['vessel']):12s} UNCAL rung {g['rung_unc']}->{c['rung_unc']} "
              f"frame {g['mm2_unc'] or 0:,.0f}->{c['mm2_unc'] or 0:,.0f} mm2 | CAL moved {cal_moved:.2f} g | "
              f"UNCAL g {[round(i['grams']) for i in g['UNCAL']]} -> {[round(i['grams']) for i in c['UNCAL']]}")
    (HERE / "chroma_net.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(f"\ndetector calls: {dict(CALLS)}  (both must be > 0, or the patch point is wrong)")
    return out


out = asyncio.run(main())

# ---------------- score ----------------
with contextlib.redirect_stdout(io.StringIO()):
    G = runpy.run_path(str(RIG / "score.py"), run_name="score")
SERVED, truth_of, parse_actuals, hand_pair = G["SERVED"], G["truth_of"], G["parse_actuals"], G["hand_pair"]
V2 = json.loads((RP.INPUTS / "paired_v2.json").read_text(encoding="utf-8"))


def meal(name, items):
    served = [it["name"] for it in V2[name]["arms"]["CAL"]]
    est = sum(it["grams"] * SERVED[s][0] / 100 for it, s in zip(items, served))
    actual = parse_actuals(V2[name]["entry"]["actual"])
    tru = sum(w * truth_of(l)[0] / 100 for l, w in actual.items())
    return est, tru


scored = [n for n, r in out.items() if r["entry"]["kind"] == "per-item" and r["entry"]["actual"]]
print(f"\nMEAL ENERGY, {len(scored)} meals (served rows and truth rows from score.py)")
for arm in ("CAL", "UNCAL"):
    for det in ("grey", "chroma"):
        rows = [meal(n, out[n][det][arm]) for n in scored]
        mae = st.mean(abs(e - t) for e, t in rows)
        per = [abs(e / t - 1) * 100 for e, t in rows]
        sg = [(e / t - 1) * 100 for e, t in rows]
        within = sum(p <= 10 for p in per)
        print(f"  {arm:5s} {det:6s}  MAE/mean {100 * mae / st.mean(t for _, t in rows):5.1f}%   per-meal mean {st.mean(per):5.1f}%  "
              f"median {st.median(per):5.1f}%   signed {st.mean(sg):+6.1f}%   within 10%: {within}/{len(rows)}")

print("\nUNCAL RUNG HISTOGRAM (frame scale source, 27 photos incl. 14)")
for det in ("grey", "chroma"):
    print(f"  {det:6s} {dict(Counter(r[det]['rung_unc'] for r in out.values()))}")
print("  item methods:", {det: dict(Counter(i['method'] for r in out.values() for i in r[det]['UNCAL'])) for det in ('grey', 'chroma')})

print("\nPER MEAL, UNCAL energy error grey -> chroma, where the rung moved")
moved = []
for n in scored:
    r = out[n]
    if r["grey"]["rung_unc"] == r["chroma"]["rung_unc"]:
        continue
    eg, t = meal(n, r["grey"]["UNCAL"])
    ec, _ = meal(n, r["chroma"]["UNCAL"])
    moved.append((n, abs(eg / t - 1), abs(ec / t - 1)))
    print(f"  {n[:2]} {str(r['vessel']):12s} {r['grey']['rung_unc']:17s}->{r['chroma']['rung_unc']:17s} "
          f"card frame {r['chroma']['card_frame_width_mm']} mm   energy {100 * (eg / t - 1):+7.1f}% -> {100 * (ec / t - 1):+7.1f}%   "
          f"{'better' if abs(ec / t - 1) < abs(eg / t - 1) else 'WORSE'}")
if moved:
    print(f"  moved meals: {len(moved)}; better {sum(c < g for _, g, c in moved)}, worse {sum(c > g for _, g, c in moved)}; "
          f"median |err| {100 * st.median(g for _, g, _ in moved):.1f}% -> {100 * st.median(c for _, _, c in moved):.1f}%")
print(f"\ncontrol worst {max(r['control_max_abs_g'] for r in out.values()):.2f} g; CAL arm moved on "
      f"{sum(r['cal_moved_g'] > 0.05 for r in out.values())} photos (must be 0)")
