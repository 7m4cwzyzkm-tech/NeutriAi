"""Register a measured vessel, so scans stop guessing its size.

Scale is the largest error in the estimator by a wide margin. Measured across
weighed meals:

    measured plate      per-item error   7-9%
    guessed bowl        meal error      -10% to +27%
    guessed container   meal error      -35%
    no reference at all meal error      -32%

A prior cannot fix this. One person's soup bowl is 152 mm and another's is 190,
and no central default is right for both -- which is why the guessed vessels
swing in both directions rather than leaning one way. The only cure is knowing
the actual vessel, and that means measuring it once.

    dev calibrate --vessel dinner_plate --width 267 --label "my dinner plate"
    dev calibrate --vessel bowl         --width 152 --label "my soup bowl"
    dev calibrate --vessel takeout_box  --w 165 --h 114 --label "alfredo box"

Round vessels take --width (the diameter). Rectangular ones take --w and --h,
because a 165x114 box holds nothing like a 165 mm circle: 18,810 mm2 against
21,382, a 14% difference that lands straight in the grams.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.scan_bench import DIM, GRN, HDR, OFF, RED, call, token_for_bench  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vessel", required=True,
                    help="dinner_plate, side_plate, bowl, takeout_box, tray, ...")
    ap.add_argument("--label", help="what you call it")
    ap.add_argument("--width", type=float, help="diameter in mm, for a round vessel")
    ap.add_argument("--w", type=float, help="width in mm, for a rectangular one")
    ap.add_argument("--h", type=float, help="depth in mm, for a rectangular one")
    ap.add_argument("--default", action="store_true", help="use when no vessel matches")
    ap.add_argument("--api", default="http://localhost:8000/v1")
    ap.add_argument("--list", action="store_true", help="show what is already saved")
    args = ap.parse_args()

    token, _uid = token_for_bench()
    # /calibrations, NOT /scans/calibrations. The router carries no prefix of
    # its own, and the wrong path returns 405 rather than 404 because
    # GET /scans/{scan_id} cheerfully matches "calibrations" as an id.
    url = f"{args.api.rstrip('/')}/calibrations"
    auth = {"Authorization": f"Bearer {token}"}

    if args.list:
        status, rows = call(url, headers=auth)
        print(f"\n{HDR}Saved calibrations{OFF}\n")
        for r in rows or []:
            size = (f"{r.get('real_diameter_mm')} mm across"
                    if r.get("real_diameter_mm")
                    else f"{r.get('real_area_mm2')} mm2")
            learned = f"  {DIM}learned from {r.get('samples')} corrections{OFF}" if r.get("learned") else ""
            print(f"  {r.get('vessel') or '(any)':<14} {size:<20} {r.get('label')}{learned}")
        if not rows:
            print(f"  {DIM}none yet{OFF}")
        return 0

    body: dict = {
        "label": args.label or args.vessel.replace("_", " "),
        "reference_kind": "plate",
        "vessel": args.vessel,
        "is_default": bool(args.default),
    }

    if args.width:
        body["real_diameter_mm"] = args.width
        described = f"{args.width:.0f} mm across"
    elif args.w and args.h:
        # Area, not diameter: a rectangle's usable surface is w*h, and calling
        # it a circle of width w overstates it by 4/pi -- 27%.
        body["real_area_mm2"] = round(args.w * args.h, 1)
        equiv = math.sqrt(4 * args.w * args.h / math.pi)
        described = (f"{args.w:.0f} x {args.h:.0f} mm = {args.w * args.h:,.0f} mm2 "
                     f"({DIM}a circle of the same area would be {equiv:.0f} mm{OFF})")
    else:
        print(f"{RED}give --width for a round vessel, or both --w and --h for a "
              f"rectangular one{OFF}")
        return 1

    status, res = call(url, method="POST", body=body, headers=auth)
    if status not in (200, 201):
        print(f"{RED}failed{OFF}  HTTP {status}: {res}")
        return 1

    print(f"{GRN}saved{OFF}  {args.vessel}: {described}")
    print(f"{DIM}Scans that detect a {args.vessel.replace('_', ' ')} will now use this "
          f"instead of a prior.{OFF}")

    # Also file it as an OBSERVATION, so the scale-learning side sees it.
    #
    # Without this the two halves disagree about the same measurement. The line
    # above writes scan_calibrations, which is what a scan reads, so the portion
    # is right -- but vessel_observations is what the progress card and the 1%
    # target are computed from, and a tape reading is the single strongest
    # observation there is. It would have shown "no measurement yet" for a plate
    # the user had just measured with a tape.
    #
    # Rectangular vessels are skipped on purpose: the observation model records
    # a WIDTH, and a box's width alone does not describe it. Better to record
    # nothing than to record a number that means something else.
    if args.width:
        m_status, m_res = call(
            f"{args.api.rstrip('/')}/calibrations/measure", method="POST",
            body={"vessel": args.vessel, "width_mm": args.width}, headers=auth,
        )
        if m_status in (200, 201):
            vessels = (m_res or {}).get("vessels") or []
            mine = next((v for v in vessels if v.get("vessel") == args.vessel), None)
            if mine:
                print(f"{DIM}Scale accuracy for this vessel: "
                      f"{mine.get('error_pct')}% "
                      f"({mine.get('observations')} observation(s)).{OFF}")
        else:
            # Never fatal. The calibration above is already saved and working;
            # this only affects what the progress card knows.
            print(f"{DIM}(observation not filed: HTTP {m_status} -- the "
                  f"calibration itself is saved and in use){OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
