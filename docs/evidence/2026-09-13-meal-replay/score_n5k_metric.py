"""OUR BENCH IN NUTRITION5K'S METRIC, beside our own. Written 13 Sep 2026.

    backend\\.venv\\Scripts\\python.exe docs\\evidence\\2026-09-13-meal-replay\\score_n5k_metric.py

Nutrition5k (Thames et al., CVPR 2021) reports, per dish:
    MAE           mean over dishes of |predicted - true|, in kcal (or g)
    MAE %         that MAE divided by the MEAN true value over the test set
This bench has reported the mean (and median) over meals of |predicted/true - 1|.

They are different statistics. Ours weights every meal equally however small, so a
23 kcal plate that reads 179 kcal contributes +664% -- a long tail dominates the
mean. Theirs weights by absolute kcal, so a small plate's miss barely moves it.
Neither is "the" error. Both are printed so neither direction flatters.

Also printed: the always-predict-the-mean BASELINE on THIS bench (Nutrition5k's
first row, 60.2% on theirs), so each figure has a same-data floor beside it.

Reads score.py's tables and replay.json by running score.py silently; no network.
"""
from __future__ import annotations

import contextlib
import io
import runpy
import statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent

with contextlib.redirect_stdout(io.StringIO()):
    G = runpy.run_path(str(HERE / "score.py"), run_name="score")

photos, parse_actuals, truth_of, hand_pair = G["photos"], G["parse_actuals"], G["truth_of"], G["hand_pair"]
SERVED, boot_median = G["SERVED"], G["boot_median"]


def rows_for(arm: str, *, drop_phantom: bool = False):
    out = []
    for name, r in photos:
        items = r["arms"][arm]
        actual = parse_actuals(r["entry"]["actual"])
        pair = hand_pair(name[:2], items, r["entry"]["actual"])
        keep = [i for i in range(len(items)) if not (drop_phantom and pair[i] is None)]
        est_k = sum(items[i]["grams"] * SERVED[items[i]["name"]][0] / 100 for i in keep)
        est_c = sum(items[i]["grams"] * SERVED[items[i]["name"]][1] / 100 for i in keep)
        est_g = sum(items[i]["grams"] for i in keep)
        tru_k = sum(w * truth_of(l)[0] / 100 for l, w in actual.items())
        tru_c = sum(w * truth_of(l)[1] / 100 for l, w in actual.items())
        tru_g = sum(actual.values())
        out.append(dict(photo=name[:2], est_k=est_k, tru_k=tru_k, est_c=est_c, tru_c=tru_c,
                        est_g=est_g, tru_g=tru_g))
    return out


def report(label: str, rows: list[dict], est: str, tru: str, unit: str, pct_floor: float = 0.0):
    mae = st.mean(abs(x[est] - x[tru]) for x in rows)
    mean_true = st.mean(x[tru] for x in rows)
    per = [abs(x[est] / x[tru] - 1) * 100 for x in rows if x[tru] >= pct_floor]
    lo, hi = boot_median(per)
    base_mae = st.mean(abs(mean_true - x[tru]) for x in rows)
    base_per = [abs(mean_true / x[tru] - 1) * 100 for x in rows if x[tru] >= pct_floor]
    print(f"  {label:34s} MAE {mae:6.1f} {unit:4s} = {100 * mae / mean_true:5.1f}% of mean ({mean_true:5.0f})"
          f"  | per-meal mean {st.mean(per):5.1f}%  median {st.median(per):5.1f}% (boot {lo:.0f}-{hi:.0f})"
          f"  | baseline {100 * base_mae / mean_true:5.1f}% of mean, per-meal mean {st.mean(base_per):5.1f}%"
          + (f"  [n={len(per)} of {len(rows)} for %]" if len(per) != len(rows) else ""))


for arm in ("CAL", "UNCAL"):
    print(f"\n{arm}  (n={len(photos)} photos; Nutrition5k columns first, ours after)")
    for tag, drop in (("all detections", False), ("phantom detections removed", True)):
        rows = rows_for(arm, drop_phantom=drop)
        print(f" {tag}")
        report("energy", rows, "est_k", "tru_k", "kcal")
        report("carbs", rows, "est_c", "tru_c", "g", pct_floor=2.0)
        report("mass", rows, "est_g", "tru_g", "g")

print("\nNutrition5k Table 3 (calories, their test set), for reference:")
print("  mean baseline 60.2% | 2D direct 26.1% | depth 4th channel 18.8% | volume scalar 16.5% | per-gram (mass given) 9.5%")
