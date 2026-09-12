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
PHOTOS = Path(__file__).resolve().parents[2] / "photos"


def resolve_photo(name: str) -> Path:
    """Find a photo whether you typed a full path or just its name.

    `dev` runs everything from the backend folder, so a relative path typed at
    the repo root -- which is where you are standing, and where the photos
    folder actually is -- resolves against the wrong folder and comes back
    "No such file" for a file that is plainly sitting there. The path was never
    the interesting part of the command, so it should not be a thing to get
    right.
    """
    p = Path(name)
    if p.exists():
        return p
    for candidate in (PHOTOS / p.name, PHOTOS / name):
        if candidate.exists():
            return candidate
    return p
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


def _sign_in() -> tuple[str, str]:
    """Sign in to the bench account, creating it once if needed. Nothing else.

    Factored out so a MID-RUN refresh cannot accidentally do the rest of what
    `token_for_bench` does -- see `refresh_bench_token` for why that matters.
    """
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


def token_for_bench() -> tuple[str, str]:
    """A token, and the account prepared for measuring. START OF RUN ONLY."""
    token, uid = _sign_in()
    _unmeter(uid)
    _clear_today(uid)
    return token, uid


def refresh_bench_token() -> str:
    """A fresh token, mid-run, WITHOUT touching the account's state.

    WHY THIS IS NOT `token_for_bench`.

    That function also calls `_clear_today`, which wipes the bench account's
    meals for the day. Calling it mid-run would delete the photographs already
    scored -- and worse, silently change the conditions the remaining ones are
    measured under, because the reasoning stage is told what the account has
    eaten today and reasons about portions with it. A measuring instrument must
    not carry state between measurements; it must not reset it halfway either.

    The token lives an hour. A 27-photo run at three runs each takes about
    seventy minutes, so the run is structurally guaranteed to outlive its own
    credential -- it lost photographs 24 to 27 to exactly this.
    """
    token, _uid = _sign_in()
    return token


def _clear_today(uid: str) -> None:
    """Wipe the bench account's meals for today before measuring anything.

    The reasoning stage is told what the user has eaten so far, and it uses it:
    on a bench that had been running all evening it remarked that the user had
    consumed 19,715 kcal, "roughly 10x typical daily intake", and reasoned
    about portions with that in mind. Every accuracy figure taken that evening
    was measured with an absurd day total whispering in the model's ear.

    A measuring instrument must not carry state between measurements. This
    clears only the bench account, only for today, using the service key.
    """
    try:
        from datetime import date

        from app.db import service

        sb = service()
        today = date.today().isoformat()
        meals = sb.table("meals").select("id").eq("user_id", uid).eq(
            "day", today
        ).execute()
        ids = [m["id"] for m in (meals.data or [])]
        for mid in ids:
            sb.table("meal_items").delete().eq("meal_id", mid).execute()
        if ids:
            sb.table("meals").delete().eq("user_id", uid).eq("day", today).execute()
        sb.table("daily_summaries").delete().eq("user_id", uid).eq("day", today).execute()
        if ids:
            print(f"  {DIM}cleared {len(ids)} bench meal(s) logged today{OFF}")
    except Exception as exc:  # noqa: BLE001
        # A bench that cannot clear its own history is still a usable bench.
        print(f"  {YEL}could not clear today's bench meals: {str(exc)[:120]}{OFF}")


def _unmeter(uid: str) -> None:
    """Take the bench account off the free-tier scan quota.

    The paywall gives free users three AI scans a day, which is a product
    decision and should stay exactly as it is. But the bench is a measuring
    instrument -- running twelve photos through it is the entire point, and
    hitting a 429 on the fourth makes it useless. So this one account is
    marked active, using the service key, on every run.

    This is deliberately not a config flag: FREE_TIER_DAILY_SCANS stays at its
    real value so local testing exercises the same paywall a user meets.
    Nothing here touches any other account.
    """
    try:
        from app.db import service
        from app.services.identity import ensure_profile

        # entitlements.user_id references profiles(id), so the profile has to
        # exist before the entitlement row can.
        ensure_profile(uid, BENCH_EMAIL)

        sb = service()
        existing = sb.table("entitlements").select("user_id").eq(
            "user_id", uid
        ).limit(1).execute()
        row = {"tier": "pro", "is_active": True, "ai_scans_used_today": 0}
        if existing.data:
            sb.table("entitlements").update(row).eq("user_id", uid).execute()
        else:
            sb.table("entitlements").insert({**row, "user_id": uid,
                                             "ai_scans_quota": 9999}).execute()
    except Exception as exc:  # noqa: BLE001
        print(f"{YEL}Could not lift the bench quota ({str(exc)[:120]}).{OFF}")
        print(f"{DIM}  Scans will stop after the daily free allowance.{OFF}")


class UploadFailed(RuntimeError):
    """One photograph could not be uploaded. Not a reason to end the run."""


UPLOAD_TRIES = 3
UPLOAD_BACKOFF_S = 5.0


def upload(path: Path, uid: str, token: str) -> str:
    """Straight to Supabase Storage, same route the phone takes.

    RETRIES, AND RAISES RATHER THAN EXITS.

    This called sys.exit(1) on any non-200. Twice in one evening a bench run
    of 21 photographs x 3 runs died on a single transient upload -- once at
    photo 4, once at photo 16 -- taking every photo after it with it. Sixty-odd
    paid model calls bought a partial answer both times, and the cause was a
    socket write timing out, not anything about the food.

    A photograph that cannot be uploaded is one missing row. The bench already
    knows how to report a photo it could not score; it did not know how to
    survive one. Three tries with a short backoff, then raise -- and the caller
    records it and carries on to the next photograph.
    """
    body = path.read_bytes()
    ctype = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    last = ""
    for attempt in range(1, UPLOAD_TRIES + 1):
        key = f"{uid}/bench-{int(time.time())}-{path.name}"
        url = (f"{settings.supabase_url.rstrip('/')}"
               f"/storage/v1/object/meal-photos/{key}")
        status, res = call(url, method="POST", raw=body,
                           headers={"apikey": settings.supabase_anon_key,
                                    "Authorization": f"Bearer {token}",
                                    "Content-Type": ctype}, timeout=90)
        if status in (200, 201):
            return key
        last = f"HTTP {status}: {res}"
        if attempt < UPLOAD_TRIES:
            print(f"  {YEL}upload attempt {attempt} failed ({last}) — "
                  f"retrying in {UPLOAD_BACKOFF_S:.0f}s{OFF}")
            time.sleep(UPLOAD_BACKOFF_S)
    raise UploadFailed(f"{path.name}: {last}")


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


# Words that mean the same food to a kitchen scale. This is a HARNESS
# convenience, not an accuracy setting: it decides which weighed number a
# detection is scored against, never what the estimator computes.
#
# It exists because the biggest item on three photos went unscored. The scale
# said "beef 335 g"; the model said "grilled meat skewer". No shared word, no
# match, so the 335 g item -- 78% of the meal -- was silently dropped from the
# per-item figure, leaving it computed almost entirely from 27 g of tomatoes.
SAME_FOOD = [
    {"beef", "steak", "meat", "kebab", "skewer", "brochette"},
    {"chicken", "poultry", "drumstick", "thigh", "breast"},
    {"potato", "potatoes"},
    {"tomato", "tomatoes"},
    {"pasta", "spaghetti", "noodles", "fideo", "casserole"},
    {"rice"},
    {"beans", "refried"},
    # Rajas: chile strips in a cream sauce. The model will not say "rajas" --
    # it says poblano, anaheim, green chile, or pepper strips, and all of those
    # are the same food on the plate. Singular and plural both, because the
    # matcher compares whole words.
    {"rajas", "poblano", "poblanos", "anaheim", "chile", "chiles", "chilies",
     "chilli", "chillies", "pepper", "peppers", "crema"},
]


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
    # ...or words that mean the same food.
    d_words = set(d.replace(",", " ").split())
    for name, grams in actuals.items():
        n_words = set(name.replace(",", " ").split())
        for group in SAME_FOOD:
            if (d_words & group) and (n_words & group):
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
    photo = resolve_photo(args.image)
    if not photo.exists():
        print(f"{RED}No such file: {args.image}{OFF}")
        if PHOTOS.exists():
            names = sorted(f.name for f in PHOTOS.iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png"))
            print(f"  {DIM}photos\\ holds: " + ", ".join(names) + OFF)
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
    if args.distance:
        payload["camera_distance_mm"] = args.distance

    t0 = time.perf_counter()
    status, res = call(f"{args.api.rstrip('/')}/scans", method="POST", body=payload,
                       headers={"Authorization": f"Bearer {token}"}, timeout=180)
    elapsed = int((time.perf_counter() - t0) * 1000)

    if status not in (200, 201):
        err = (res or {}).get("error", {})
        print(f"\n{RED}Scan failed (HTTP {status}): {err.get('message', res)}{OFF}")
        if err.get("code") == "upstream_error":
            print(f"  {YEL}A provider call failed. Check the keys with `dev keys`; a")
            print(f"  zero-balance account 401s in a way that looks like a bad key.{OFF}")
        elif err.get("code") == "internal_error":
            # Do not speculate. An internal error is a bug in our own code far
            # more often than a key problem, and guessing at the cause here has
            # sent people to check billing while the real fault was a database
            # constraint. Point at the tool that prints the actual traceback.
            print(f"  {YEL}That is a bug on our side, not a key problem.")
            print(f"  Run the same photo through the diagnostic for the real cause:")
            print(f"    dev scandebug \"{photo}\"{OFF}")
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

    if args.total:
        est_total = sum(float(i["grams"]) for i in items)
        err_pct = (est_total - args.total) / args.total * 100
        colour = GRN if abs(err_pct) <= 20 else (YEL if abs(err_pct) <= 40 else RED)
        methods = sorted({i["estimation_method"] for i in items})
        print(f"  {colour}weighed total {args.total:.0f} g  ->  estimated "
              f"{est_total:.1f} g  ({err_pct:+.1f}%){OFF}"
              f"   {DIM}via {', '.join(methods)}{OFF}")

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
    # For food you cannot split on a scale. Two tacos weigh 133 g together;
    # nobody can weigh the tortilla apart from the egg inside it, and the model
    # will quite reasonably report them as separate items. Per-item matching
    # has nothing to match, and the run reports nothing at all -- so the
    # measurement that IS available gets thrown away. This keeps it.
    p.add_argument("--total", type=float,
                   help="weighed grams for the WHOLE meal, when items cannot be weighed apart")
    p.add_argument("--slot", choices=["breakfast", "lunch", "dinner", "snack"])
    p.add_argument("--plate", type=float, help="plate diameter in mm, if you measured it")
    # With the aspect ratio this gives the frame size by trigonometry, which is
    # the only scale a photo with no plate and no reference object has.
    p.add_argument("--distance", type=float,
                   help="how far the lens was from the food, in mm (12 in = 305)")
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
