#!/usr/bin/env python3
"""Accuracy bench for the food-scan pipeline.

Photograph a meal you have weighed, run it through, and record how far off the
estimate was. Results accumulate in a CSV so a kitchen-scale session produces a
dataset rather than an impression.

    dev.bat scan meal.jpg --actual "jasmine rice=180, grilled chicken=210"
    dev.bat scan meal.jpg --actual "pasta=320" --slot dinner --plate 270
    dev.bat scan --report          summarise everything recorded so far
    dev.bat scan --report --csv    dump the raw rows

Why the CSV matters more than any single scan: one photo tells you almost
nothing -- the estimator carries a +-25-35% band and it is honest about that.
Twenty photos tell you whether a food class is *biased*, which is the thing you
can actually fix. A consistent +30% on rice is one constant in portion.py;
random scatter is not a bug, it is the stated error bar.

The tuning target is SHAPE_FACTORS / HEIGHT_PRIORS_MM / DENSITY_G_ML at the top
of app/services/ai/portion.py. See docs/OPTIMIZATION.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

GRN, RED, YEL, DIM, HDR, CYN, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[36m", "\033[0m"
)

RESULTS = Path(__file__).resolve().parents[1] / "scan_results.csv"
FIELDS = [
    "timestamp", "image", "food", "actual_g", "estimated_g", "error_g", "error_pct",
    "method", "confidence", "band_low", "band_high", "in_band",
    "kcal", "protein_g", "carbs_g", "fat_g", "scan_id", "latency_ms",
]

# A persistent account so history accumulates across sessions.
BENCH_EMAIL = "neutriai.bench@example.com"
BENCH_PASSWORD = "neutriai-bench-password-123"


def call(url, *, method="GET", body=None, headers=None, timeout=120, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if raw is None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, {"raw": text[:400]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)[:250]}


def token_for_bench() -> tuple[str, str]:
    """Sign in to the bench account, creating it once if needed."""
    sb = settings.supabase_url.rstrip("/")
    anon = settings.supabase_anon_key
    creds = {"email": BENCH_EMAIL, "password": BENCH_PASSWORD}

    status, tok = call(f"{sb}/auth/v1/token?grant_type=password",
                       method="POST", body=creds, headers={"apikey": anon})
    if status != 200 or not (tok or {}).get("access_token"):
        call(f"{sb}/auth/v1/signup", method="POST", body=creds, headers={"apikey": anon})
        status, tok = call(f"{sb}/auth/v1/token?grant_type=password",
                           method="POST", body=creds, headers={"apikey": anon})
    if status != 200 or not (tok or {}).get("access_token"):
        print(f"{RED}Could not sign in to the bench account.{OFF}")
        print(f"  {tok}")
        sys.exit(1)
    return tok["access_token"], tok["user"]["id"]


def upload(path: Path, uid: str, token: str) -> str:
    """Straight to Supabase Storage, same route the phone takes."""
    key = f"{uid}/bench-{int(time.time())}-{path.name}"
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/meal-photos/{key}"
    ctype = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    status, res = call(url, method="POST", raw=path.read_bytes(),
                       headers={"apikey": settings.supabase_anon_key,
                                "Authorization": f"Bearer {token}",
                                "Content-Type": ctype}, timeout=90)
    if status not in (200, 201):
        print(f"{RED}Upload failed (HTTP {status}): {res}{OFF}")
        sys.exit(1)
    return key


def parse_actuals(text: str) -> dict[str, float]:
    """'rice=180, chicken=210' -> {'rice': 180.0, 'chicken': 210.0}"""
    out = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        name, grams = part.rsplit("=", 1)
        try:
            out[name.strip().lower()] = float(re.sub(r"[^0-9.]", "", grams))
        except ValueError:
            continue
    return out


def match(detected: str, actuals: dict[str, float]) -> tuple[str, float] | None:
    """Loose name matching -- the model says 'jasmine rice', you wrote 'rice'."""
    d = detected.lower()
    if d in actuals:
        return d, actuals[d]
    for name, grams in actuals.items():
        if name in d or d in name:
            return name, grams
        # any shared significant word
        if set(w for w in name.split() if len(w) > 3) & set(w for w in d.split() if len(w) > 3):
            return name, grams
    return None


def record(rows: list[dict]) -> None:
    exists = RESULTS.exists()
    with RESULTS.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def run_scan(args) -> int:
    photo = Path(args.image)
    if not photo.exists():
        print(f"{RED}No such file: {photo}{OFF}")
        return 1
    actuals = parse_actuals(args.actual) if args.actual else {}

    print(f"\n{HDR}Scan bench{OFF}  {DIM}{photo.name}{OFF}")
    if actuals:
        print(f"  {DIM}weighed: " + ", ".join(f"{k} {v:.0f}g" for k, v in actuals.items()) + OFF)

    token, uid = token_for_bench()
    key = upload(photo, uid, token)
    print(f"  {DIM}uploaded, analysing...{OFF}")

    payload = {"image_paths": [key]}
    if args.slot:
        payload["meal_slot"] = args.slot
    if args.plate:
        payload["plate_diameter_mm"] = args.plate

    t0 = time.perf_counter()
    status, res = call(f"{args.api.rstrip('/')}/scans", method="POST", body=payload,
                       headers={"Authorization": f"Bearer {token}"}, timeout=180)
    elapsed = int((time.perf_counter() - t0) * 1000)

    if status not in (200, 201):
        err = (res or {}).get("error", {})
        print(f"\n{RED}Scan failed (HTTP {status}): {err.get('message', res)}{OFF}")
        if err.get("code") in ("upstream_error", "internal_error"):
            print(f"  {YEL}Check ANTHROPIC_API_KEY and OPENAI_API_KEY in .env.")
            print(f"  Both need billing enabled -- a zero-balance account 401s in a")
            print(f"  way that looks exactly like a bad key.{OFF}")
        return 1

    items = res.get("items") or []
    print(f"\n{HDR}Detected{OFF}  {DIM}{elapsed}ms, "
          f"confidence {res.get('overall_confidence')} ({res.get('confidence_band')}){OFF}\n")

    rows, errors = [], []
    for it in items:
        est = float(it["grams"])
        lo, hi = it.get("grams_low"), it.get("grams_high")
        m = match(it["name"], actuals)
        line = (f"  {it['name'][:28]:28s} {est:7.1f}g  "
                f"[{lo:.0f}-{hi:.0f}]  {it['estimation_method']:16s} "
                f"conf {it['confidence']:.2f}")

        if m:
            label, actual = m
            err_g = est - actual
            err_pct = err_g / actual * 100
            in_band = bool(lo and hi and lo <= actual <= hi)
            colour = GRN if abs(err_pct) <= 20 else (YEL if abs(err_pct) <= 40 else RED)
            band_note = (f"{GRN}within band{OFF}" if in_band
                         else f"{RED}outside the stated band{OFF}")
            print(line)
            print(f"      {colour}actual {actual:.0f}g  ->  {err_pct:+.1f}%  "
                  f"({err_g:+.0f}g){OFF}   {band_note}")
            errors.append(err_pct)
            rows.append({
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "image": photo.name, "food": it["name"], "actual_g": round(actual, 1),
                "estimated_g": round(est, 1), "error_g": round(err_g, 1),
                "error_pct": round(err_pct, 1), "method": it["estimation_method"],
                "confidence": it["confidence"], "band_low": lo, "band_high": hi,
                "in_band": in_band,
                "kcal": round(it["macros"]["kcal"], 1),
                "protein_g": round(it["macros"]["protein_g"], 1),
                "carbs_g": round(it["macros"]["carbs_g"], 1),
                "fat_g": round(it["macros"]["fat_g"], 1),
                "scan_id": res.get("scan_id"), "latency_ms": res.get("latency_ms") or elapsed,
            })
        else:
            print(line + (f"\n      {DIM}not weighed{OFF}" if actuals else ""))

    totals = res.get("totals") or {}
    print(f"\n  {HDR}meal total: {totals.get('kcal', 0):.0f} kcal  "
          f"P{totals.get('protein_g', 0):.0f} C{totals.get('carbs_g', 0):.0f} "
          f"F{totals.get('fat_g', 0):.0f}{OFF}")

    if res.get("notes"):
        print(f"\n  {DIM}how it was calculated:{OFF}")
        for n in res["notes"]:
            print(f"    {DIM}- {n}{OFF}")

    if rows:
        record(rows)
        mean_abs = statistics.mean(abs(e) for e in errors)
        in_band = sum(1 for r in rows if r["in_band"])
        print(f"\n  {HDR}mean absolute error: {mean_abs:.1f}%{OFF}"
              f"   {in_band}/{len(rows)} inside the reported band")
        print(f"  {DIM}recorded to {RESULTS.name} ({len(rows)} row(s)){OFF}")
    elif actuals:
        print(f"\n  {YEL}No detected food matched your --actual names.{OFF}")
        print(f"  {DIM}Detected: {', '.join(i['name'] for i in items)}{OFF}")
    return 0


def report(show_csv: bool) -> int:
    if not RESULTS.exists():
        print(f"\n{YEL}No results yet. Run a scan with --actual first.{OFF}\n")
        return 1
    rows = list(csv.DictReader(RESULTS.open(encoding="utf-8")))
    if not rows:
        print(f"\n{YEL}scan_results.csv is empty.{OFF}\n")
        return 1

    if show_csv:
        print(RESULTS.read_text(encoding="utf-8"))
        return 0

    errs = [float(r["error_pct"]) for r in rows]
    in_band = sum(1 for r in rows if r["in_band"] == "True")

    print(f"\n{HDR}Scan accuracy -- {len(rows)} measurements{OFF}\n")
    print(f"  mean absolute error : {statistics.mean(abs(e) for e in errs):5.1f}%")
    print(f"  median              : {statistics.median(abs(e) for e in errs):5.1f}%")
    print(f"  bias (signed mean)  : {statistics.mean(errs):+5.1f}%  "
          f"{DIM}<- consistent sign means a tunable prior, not noise{OFF}")
    if len(errs) > 1:
        print(f"  spread (stdev)      : {statistics.stdev(errs):5.1f}%")
    print(f"  inside stated band  : {in_band}/{len(rows)} "
          f"({in_band / len(rows) * 100:.0f}%)  "
          f"{DIM}<- should be ~70%+ if the bands are honest{OFF}")

    # Per food: this is what you actually tune from.
    by_food: dict[str, list[float]] = {}
    for r in rows:
        by_food.setdefault(r["food"].lower(), []).append(float(r["error_pct"]))
    biased = [(f, e) for f, e in by_food.items() if len(e) >= 3]
    if biased:
        print(f"\n{HDR}By food{OFF} {DIM}(3+ samples -- these are tunable){OFF}\n")
        for food, e in sorted(biased, key=lambda x: -abs(statistics.mean(x[1]))):
            mean = statistics.mean(e)
            flag = (f"{RED}systematic{OFF}" if abs(mean) > 20
                    else f"{YEL}slight{OFF}" if abs(mean) > 10 else f"{GRN}fine{OFF}")
            print(f"  {food[:30]:30s} n={len(e):2d}  bias {mean:+6.1f}%  {flag}")
        print(f"\n  {DIM}A consistent bias is one constant in portion.py:")
        print(f"  over-estimating -> lower that food's HEIGHT_PRIORS_MM or SHAPE_FACTORS")
        print(f"  under-estimating -> raise them. Density lives in DENSITY_G_ML.{OFF}")

    by_method: dict[str, list[float]] = {}
    for r in rows:
        by_method.setdefault(r["method"], []).append(abs(float(r["error_pct"])))
    print(f"\n{HDR}By estimation method{OFF}\n")
    for method, e in sorted(by_method.items(), key=lambda x: statistics.mean(x[1])):
        print(f"  {method:18s} n={len(e):2d}  mean abs error {statistics.mean(e):5.1f}%")
    print(f"\n  {DIM}plate_reference should beat pixel_area, which should beat ai_prior.")
    print(f"  If it does not, the geometry ladder is not earning its complexity.{OFF}\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("image", nargs="?", help="path to the meal photo")
    p.add_argument("--actual", help='weighed grams, e.g. "rice=180, chicken=210"')
    p.add_argument("--slot", choices=["breakfast", "lunch", "dinner", "snack"])
    p.add_argument("--plate", type=float, help="plate diameter in mm, if you measured it")
    p.add_argument("--api", default="http://localhost:8000/v1")
    p.add_argument("--report", action="store_true", help="summarise recorded results")
    p.add_argument("--csv", action="store_true", help="with --report, dump raw rows")
    a = p.parse_args()

    if a.report:
        return report(a.csv)
    if not a.image:
        p.print_help()
        return 1
    return run_scan(a)


if __name__ == "__main__":
    sys.exit(main())
