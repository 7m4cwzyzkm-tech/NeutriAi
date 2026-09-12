"""How much does the same photo move between runs?

This exists because of a mistake. Refried beans came back 84.5 g on one run and
67.6 g on the next, from the same photo, with no code between them that touched
beans. Meanwhile changes were being judged by comparing single runs -- 7.4%
against 11.8% -- as though the difference meant something.

It might not. If the model's own spread is +/- 20%, then a single-run comparison
cannot resolve anything smaller than that, and every conclusion drawn from one
is a coin flip dressed as evidence.

So: run one photo N times, and report what the model does when nothing changes.
Everything measured after this can be read against that floor.

    dev repeat photo.jpg --n 10 --plate 267
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.scan_bench import (  # noqa: E402
    DIM, GRN, HDR, OFF, RED, YEL, call, resolve_photo, token_for_bench, upload,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--plate", type=float)
    ap.add_argument("--api", default="http://localhost:8000/v1")
    args = ap.parse_args()

    photo = resolve_photo(args.image)
    if not photo.exists():
        print(f"{RED}no such file: {photo}{OFF}")
        return 1

    token, uid = token_for_bench()
    print(f"\n{HDR}Run-to-run variance{OFF}  {DIM}{photo.name}, {args.n} runs{OFF}\n")

    by_item: dict[str, list[float]] = defaultdict(list)
    totals: list[float] = []
    names_seen: list[frozenset[str]] = []

    try:
        _collect(args, photo, token, uid, by_item, totals, names_seen)
    except KeyboardInterrupt:
        # Ctrl+C must not throw away runs that have already been paid for.
        print(f"\n  {YEL}stopped early — reporting the {len(totals)} run(s) "
              f"already done{OFF}")

    return _report(totals, by_item, names_seen)


def _collect(args, photo, token, uid, by_item, totals, names_seen) -> None:
    for i in range(args.n):
        # A dropped connection on run 4 must not throw away runs 1-3. Skip the
        # run, keep the data, carry on -- the whole point of this script is
        # collecting enough samples to see past a single bad one.
        try:
            key = upload(photo, uid, token)
            body: dict = {"image_paths": [key]}
            if args.plate:
                body["plate_diameter_mm"] = args.plate
            status, res = call(f"{args.api.rstrip('/')}/scans", method="POST", body=body,
                               headers={"Authorization": f"Bearer {token}"}, timeout=180)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"  {RED}run {i+1} skipped{OFF}  {str(exc)[:90]}")
            continue
        if status not in (200, 201):
            print(f"  {RED}run {i+1} failed{OFF}  HTTP {status}")
            continue
        items = res.get("items") or []
        names_seen.append(frozenset(str(it.get("name", "")).lower() for it in items))
        run_total = 0.0
        for it in items:
            g = float(it.get("grams") or 0)
            by_item[str(it.get("name", "")).lower()].append(g)
            run_total += g
        totals.append(run_total)
        print(f"  run {i+1:>2}  {run_total:7.1f} g  {DIM}"
              f"{', '.join(str(it.get('name'))[:18] for it in items)}{OFF}")

def _report(totals, by_item, names_seen) -> int:
    if len(totals) < 2:
        print(f"\n{RED}not enough successful runs to measure anything{OFF}")
        return 1

    print(f"\n{HDR}Meal total{OFF}")
    mean = statistics.mean(totals)
    sd = statistics.pstdev(totals)
    print(f"  mean {mean:.1f} g   sd {sd:.1f} g   "
          f"spread {min(totals):.0f}-{max(totals):.0f} g   "
          f"{YEL}+/- {sd / mean * 100:.1f}%{OFF}")

    print(f"\n{HDR}Per item{OFF}  {DIM}items the model did not name every run are "
          f"the bigger problem{OFF}")
    for name, vals in sorted(by_item.items(), key=lambda kv: -len(kv[1])):
        seen = len(vals)
        m = statistics.mean(vals)
        s = statistics.pstdev(vals) if seen > 1 else 0.0
        flag = "" if seen == len(totals) else f"  {RED}named in only {seen}/{len(totals)} runs{OFF}"
        print(f"  {name:<28} {m:6.1f} g  +/- {s / m * 100 if m else 0:4.1f}%{flag}")

    distinct = len(set(names_seen))
    print(f"\n{HDR}Identification{OFF}")
    print(f"  {distinct} distinct item set(s) across {len(names_seen)} runs"
          + (f"   {GRN}stable{OFF}" if distinct == 1 else f"   {RED}unstable{OFF}"))

    floor = sd / mean * 100
    print(f"\n{'=' * 62}")
    print(f"Noise floor: {YEL}{floor:.1f}%{OFF} on the meal total.")
    print(f"{DIM}Any change smaller than roughly {floor * 2:.0f}% cannot be judged from a "
          f"single run.{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
