# PROVENANCE
# Per-piece bench scorer for Bundle A, 13 Sep 2026. Reads a replay-rig output and chroma_net.json
# (same folder). Moved from a session scratchpad; only RIG rewritten to be relative.
# Needed for the whole-bundle score after piece 3.
#
"""Score one Bundle A piece from a replay-rig output.

    python score_piece.py <replay_out.json> <label>

Checks: CAL arm equals the recorded (grey) grams; UNCAL arm equals the reference
arm in chroma_net.json (argument 3: 'grey' or 'chroma', default 'chroma').
Scores meal energy, both statistics, with score.py's served and truth tables.
"""
from __future__ import annotations

import contextlib
import io
import json
import runpy
import statistics as st
import sys
from collections import Counter
from pathlib import Path

RIG = Path(__file__).resolve().parents[1] / "2026-09-13-meal-replay"
HERE = Path(__file__).resolve().parent
out = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
out.pop("_provenance", None)
label = sys.argv[2]
ref_arm = sys.argv[3] if len(sys.argv) > 3 else "chroma"
net = json.loads((HERE / "chroma_net.json").read_text(encoding="utf-8"))
V2 = json.loads((RIG / "inputs" / "paired_v2.json").read_text(encoding="utf-8"))
with contextlib.redirect_stdout(io.StringIO()):
    G = runpy.run_path(str(RIG / "score.py"), run_name="score")
SERVED, truth_of, parse_actuals = G["SERVED"], G["truth_of"], G["parse_actuals"]


def diff(a, b):
    if len(a) != len(b):
        return float("inf")
    return max((abs(x["grams"] - y["grams"]) for x, y in zip(a, b)), default=0.0)


cal_bad, unc_bad = [], []
for n, r in out.items():
    dc = diff(r["arms"]["CAL"], V2[n]["arms"]["CAL"])
    du = diff(r["arms"]["UNCAL"], net[n][ref_arm]["UNCAL"])
    if dc > 0.05:
        cal_bad.append((n[:2], round(dc, 2)))
    if du > 0.05:
        unc_bad.append((n[:2], round(du, 2), [round(i["grams"], 1) for i in r["arms"]["UNCAL"]],
                        [round(i["grams"], 1) for i in net[n][ref_arm]["UNCAL"]]))
print(f"[{label}] CAL arm vs recorded grams: {'IDENTICAL on all ' + str(len(out)) if not cal_bad else cal_bad}")
print(f"[{label}] UNCAL arm vs chroma_net '{ref_arm}' arm: {'IDENTICAL on all ' + str(len(out)) if not unc_bad else ''}")
for b in unc_bad:
    print(f"    DIFF {b}")
print(f"[{label}] UNCAL frame rung: {dict(Counter(r['frame_mm2']['UNCAL_method'] for r in out.values()))}")


def meal(name, items):
    served = [it["name"] for it in V2[name]["arms"]["CAL"]]
    est = sum(it["grams"] * SERVED[s][0] / 100 for it, s in zip(items, served))
    tru = sum(w * truth_of(l)[0] / 100 for l, w in parse_actuals(V2[name]["entry"]["actual"]).items())
    return est, tru


scored = [n for n in out if V2[n]["entry"]["kind"] == "per-item" and V2[n]["entry"]["actual"]]
for arm in ("CAL", "UNCAL"):
    rows = [meal(n, out[n]["arms"][arm]) for n in scored]
    mae = st.mean(abs(e - t) for e, t in rows)
    per = [abs(e / t - 1) * 100 for e, t in rows]
    print(f"[{label}] {arm:5s} energy  MAE/mean {100 * mae / st.mean(t for _, t in rows):5.1f}%  per-meal mean {st.mean(per):5.1f}%  "
          f"median {st.median(per):5.1f}%  within 10% {sum(p <= 10 for p in per)}/{len(rows)}  (n={len(rows)})")
