#!/usr/bin/env python3
"""Run every weighed photo and report accuracy across all of them.

    dev benchall

Single scans tell you about one meal. This is the set of photos with real
scale readings behind them, run together, so a change can be judged on
whether it helps overall rather than on whether it helped the last plate.

That distinction has already mattered: a fix that took one plate from +104%
to -7% made single-item vessels worse at the same time, and only looking at
everything at once showed it.

Weights are from a kitchen scale. Where only a meal total was recorded, the
comparison is against the sum of the detected items -- noted per entry, since
a total says nothing about whether the split between items is right.
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.scan_bench import (  # noqa: E402
    CYN, DIM, GRN, HDR, OFF, RED, YEL, UploadFailed, call, match, parse_actuals,
    token_for_bench, upload,
)

PHOTOS = Path(__file__).resolve().parents[2] / "photos"

# (file, plate diameter mm or None, weighed grams, kind, camera distance mm)
#
# The distance is how far the lens was from the food, and it is a real
# measurement: with the aspect ratio it gives the frame size by trigonometry,
# which is the only scale a photo with no plate and no card has. Optional --
# leave it None for the older cases, which were shot before anyone was
# recording it.
CASES = [
    # ONE PLATE, ONE CARD, ONE TARE, ALL WEIGHED THE SAME DAY.
    #
    # THE FIRST SIXTEEN PHOTOGRAPHS WERE RETIRED, and not because they were
    # inconvenient. Every one of them fails the check the new set passes:
    #
    #   NO CARD IN FRAME on 06, 07, 08, 13, 14, 15, 16, so their declared plate
    #   size cannot be verified from the photograph. That is the term that
    #   multiplies every gram -- area goes as the square of it -- and those
    #   plates were carried at 267-269 mm for weeks before a tape said 254. If
    #   any declared size is still wrong, the bench is scoring the estimator
    #   against a bad ruler and reporting the ruler's error as the model's.
    #
    #   NO SCALE AT ALL on 01, 03, 04 and 05 -- paper, a takeout box, a bowl of
    #   assumed size. Those fall to a serving prior, so no geometry change is
    #   visible in them at all.
    #
    #   NOT INDEPENDENT: 15, 16 and the no-reference variant of 15 are one meal
    #   scored three times, and they supplied three of the ten scored items.
    #
    # The photographs are archived under photos/legacy/, not deleted. They cost
    # nothing to keep and they are the only record of what the estimator did
    # before; deleting evidence because it is awkward is the habit this whole
    # bench exists to break. They are simply out of the score, where they can
    # no longer move a number nobody can verify.
    #
    # What replaced each thing they were carrying:
    #   weighed multi-item split (was 07, 08)  -> 29, 35, 36
    #   card against no card     (was 11 v 12) -> 35 against 36, on a plate
    #   the uncalibrated path    (was 13 v 13u)-> 29 against 29u, below
    #
    # (file, plate diameter mm or None, weighed grams, kind, camera distance mm)
    ("17-carrots-plate.jpg",           229, "steamed carrots=58",        "per-item"),
    ("18-zucchini-plate.jpg",          229, "steamed zucchini=74",       "per-item"),
    ("20-spaghetti-plate.jpg",         229, "spaghetti with sauce=253",  "per-item"),
    ("21-bbq-chicken-plate.jpg",       229, "bbq chicken thigh=159",     "per-item"),
    ("22-roast-beef-plate.jpg",        229, "roast beef=83",             "per-item"),
    ("23-brussels-sprouts-plate.jpg",  229, "brussels sprouts=65",       "per-item"),
    ("24-macaroni-salad-plate.jpg",    229, "macaroni salad=89",         "per-item"),
    ("25-smashed-potatoes-plate.jpg",  229, "smashed potatoes=136",      "per-item"),

    # The one multi-item plate in this set, scored on the TOTAL only.
    #
    # Its four foods were weighed -- carrots 58, zucchini 74, green beans 48,
    # fish 48 -- but they sum to 228 g against a plated 291 g, so they are not
    # this plate's portions and pretending otherwise would score a bookkeeping
    # gap as a vision error. The total is real and the total is what it gets.
    ("19-fish-veg-plate.jpg",          229, "whole plate=291",           "total"),

    # ---------------------------------------------------------------------
    # THE POT ROAST PLATE, and it is the most valuable case on this bench.
    #
    # Four foods on one plate, EACH WEIGHED SEPARATELY -- 69, 114, 84, 42 --
    # and then photographed assembled. Every other multi-item photograph here
    # has either a total or a partial split, so the shipped split has been
    # scored against a weighed one on exactly one plate (07) until now. A meal
    # total can be right while the split between items is wrong, and the split
    # is what a person actually eats.
    #
    # The tare checks a second time and on a different food: the bread is one
    # dinner roll and nets 42 g, which is what a dinner roll weighs.
    #
    # NOT FOUR INDEPENDENT MEALS. The three single-food photographs below are
    # the SAME portions as the assembled plate, spread out on the plate they
    # were weighed on rather than piled. That makes them a good test of whether
    # presentation moves the answer -- 69 g of rice piled looks like far more
    # than 69 g spread thin, and the estimator should not care -- but anything
    # counting distinct meals must count these four as one.
    #
    # The spinach was weighed and has no photograph of its own; it appears only
    # on the assembled plate.
    ("26-potroast-rice-alone.jpg",     229, "white rice=69",             "per-item"),
    ("27-potroast-beef-alone.jpg",     229, "pot roast=114",             "per-item"),
    ("28-potroast-bread-alone.jpg",    229, "dinner roll=42",            "per-item"),
    ("29-potroast-plate-4items.jpg",   229,
     "white rice=69, pot roast=114, steamed spinach=84, dinner roll=42", "per-item"),

    # ---------------------------------------------------------------------
    # THE TWO CASES NOTHING HERE COULD TEST: a leaf pile, and liquid in a bowl.
    #
    # CAESAR SALAD is the opposite failure mode from everything above. Leaves
    # cover a huge footprint and weigh almost nothing -- 123 g spread over most
    # of a 9 in plate -- so an estimator tuned on dense piled food should read
    # it far too heavy. Nothing on this bench tested that direction before.
    #
    # THE SOUPS ARE IN THE 4.5 in CROCK, not the plate, and their tare is the
    # crock's 138 g. That is forced rather than assumed: the posole reads 320 g
    # gross, and 320 minus a crock-plus-plate tare of 458 g is negative, so the
    # crock alone is the only tare the arithmetic permits.
    #
    # 32 and 33 sit in the crock ON A PLATE, and that is deliberate. It is the
    # vessel-identification trap: the food is in a 114 mm crock inside a 229 mm
    # plate, and a pipeline that measures the wrong one is out by four times in
    # area. 31 is the same food shape with no underliner, so the pair separates
    # "cannot size a bowl" from "picked the wrong vessel". Expect these to
    # score badly; that is what they are for.
    ("30-caesar-salad-plate.jpg",      229, "caesar salad=123",          "per-item"),
    ("31-chicken-noodle-crock.jpg",    114, "chicken noodle soup=151",   "per-item"),
    ("32-beef-posole-crock.jpg",       114, "beef posole=182",           "per-item"),
    ("33-cheese-broccoli-crock.jpg",   114, "cheese and broccoli soup=152", "per-item"),

    # FLAT BREAD-BASED FOOD, and a second weighed two-item split.
    #
    # A pizza slice is the shape nothing here had: a thin wedge whose footprint
    # is nearly its whole volume, where the height prior does almost all the
    # work and gets it wrong in one direction for the crust and the other for
    # the cheese.
    #
    # 35 holds a slider and a pile of fries, both weighed -- 145 and 65 -- so
    # it is the second plate on this bench that can score a SPLIT against a
    # scale rather than a total. The fries are also the first food here whose
    # pieces overlap and cast shadows on each other.
    ("34-pizza-slice-plate.jpg",       229, "pizza slice=120",           "per-item"),

    # ------------------------------------------------------------------
    # THE HEAP-VS-LAYER PAIRS. Same food, same weight, photographed twice.
    #
    # These exist to settle one rule. SEPARATE_PIECES_HEIGHT_MM says pieces on
    # a plate cannot stack -- true of carrot coins and zucchini slices, FALSE
    # of fries and lettuce, measured at -76.8% and +198% on this bench.
    # Topology alone cannot tell a layer from a heap. These can.
    #
    # A DIFFERENT PLATE: 8.75 inches of Styrofoam weighing 3 g, not the 9-inch
    # 320 g plate every photograph above uses. 222 mm, not 229 -- 3% in the
    # diameter is 6% in the weight, so it is stated per row, never assumed.
    #
    # Shot from 10-12 inches, stated as 280 mm. Everything above was shot with
    # no recorded distance, so this is also the first look at whether the
    # camera-distance rung is worth anything.
    #
    # Deliberately awkward, and that is the point: a MIXTURE rather than one
    # food, grapes still on the stem, and a patterned tablecloth instead of
    # plain wood.
    ("40-trailmix-spread.jpg",         222, "trail mix=44",       "per-item", 280),
    ("41-trailmix-heaped.jpg",         222, "trail mix=44",       "per-item", 280),
    ("42-chips-spread.jpg",            222, "tortilla chips=25",  "per-item", 280),
    ("43-chips-heaped.jpg",            222, "tortilla chips=25",  "per-item", 280),
    ("44-grapes-spread.jpg",           222, "grapes=90",          "per-item", 280),
    ("45-grapes-cluster.jpg",          222, "grapes=90",          "per-item", 280),
    ("35-slider-fries-plate.jpg",      229, "cheeseburger slider=145, french fries=65",
     "per-item"),

    # THE SAME PLATE, PHOTOGRAPHED WITH THE CARD AND WITHOUT IT.
    #
    # 35 and 36 are one meal shot twice: identical food, identical plate,
    # identical distance, and the only difference in the frame is whether a
    # credit card is lying on the table. Everything else is held, so the gap
    # between them IS what the reference object is worth.
    #
    # The bench has asked this before and only on PAPER -- kebabs at 11 against
    # 12, where there was no vessel at all and the card was the only scale in
    # the picture. This asks it on a plate, where the card is competing with a
    # perfectly good vessel rather than standing in for a missing one, and the
    # ladder has to decide which to believe. That decision has never been
    # measured.
    ("36-slider-fries-card.jpg",       229, "cheeseburger slider=145, french fries=65",
     "per-item"),

    # THE PATH EVERY NEW USER TAKES, rebuilt on new data.
    #
    # The same pot roast plate with the diameter NOT given, so the app falls
    # back to its assumed 270 mm. The real plate is 229 mm and area goes as the
    # square, so this is a 39% inflation on every gram, silently, for anyone
    # who has not measured their crockery -- which at signup is everyone.
    #
    # This is what the trial-period calibration arc exists to close, so it is
    # also the number that says whether the arc is working. The old bench asked
    # this with the kebab plate; it is asked here with a four-item plate whose
    # split is weighed, which is strictly more informative.
    ("29-potroast-plate-4items.jpg",   None,
     "white rice=69, pot roast=114, steamed spinach=84, dinner roll=42", "per-item"),
]


# Which photos contain something that says how big a pixel is.
#
# False means the photo has no plate, no reference object and no recorded camera
# distance -- the estimate falls to a typical-serving prior, and every geometry
# change in this codebase is invisible to it. Kept in the bench, reported apart.
MEASURED_SCALE = {
    # Empty on purpose. Every photograph on this bench now has a plate or a
    # crock of known size, so every one of them carries a measured scale. The
    # four that did not -- paper, a takeout box, two bowls of assumed size --
    # were retired with the rest of the first set: an estimate that falls to a
    # serving prior cannot show a geometry change, so it could never say
    # whether any of this work helped.
}


def run_one(api, token, uid, filename, plate, actual, distance=None):
    photo = PHOTOS / filename
    if not photo.exists():
        return None, f"missing file {photo.name}"
    try:
        key = upload(photo, uid, token)
    except UploadFailed as exc:
        # One photograph lost to the network is a missing row, not a dead run.
        return None, f"upload failed: {str(exc)[:120]}"
    # Ask for the shadow measurement. Costs the bench a few seconds a photo and
    # costs a real scan nothing, because a real scan does not set this.
    body = {"image_paths": [key], "measure_footprints": True}
    if plate:
        body["plate_diameter_mm"] = plate
    if distance:
        body["camera_distance_mm"] = distance
    status, res = call(f"{api.rstrip('/')}/scans", method="POST", body=body,
                       headers={"Authorization": f"Bearer {token}"}, timeout=180)
    if status not in (200, 201):
        return None, describe_failure(status, res)
    return res, None


def _run_total(res: dict) -> float:
    """Meal grams. Tolerant, because everything here came back over HTTP from a
    model, and one item with a string in its grams must not take down the run
    that is measuring the noise."""
    total = 0.0
    for item in (res or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            total += float(item.get("grams") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _median_run(runs: list[dict]) -> dict:
    """The middle run by meal total.

    A real run, not an average of several -- averaging would invent an item
    list that never happened, and the per-item numbers have to come from a
    scan that actually occurred.
    """
    return sorted(runs, key=_run_total)[len(runs) // 2]


def _run_spread(runs: list[dict]) -> float | None:
    """How far apart the repeats were, in grams. This is the noise floor, and
    a change smaller than it is not a result."""
    if len(runs) < 2:
        return None
    totals = [_run_total(r) for r in runs]
    return max(totals) - min(totals)


def _item_spreads(runs: list[dict]) -> list[tuple[str, float, float]]:
    """Per-item spread across the repeats: (name, min, max).

    The meal total hides this, which is the limitation of picking a median run
    by total. Cherry tomatoes went 24.6 g to 47.3 g between two runs -- a 92%
    swing on the item, and 23 g on a 373 g meal, so the total moved 6% and the
    median-by-total would happily have chosen the run with the bad tomato.

    A small item is where the model's box is least reliable in relative terms:
    the box moved one percentage point of frame, and at 27 g one point IS the
    whole answer. So the items are reported separately from the meal.
    """
    by_name: dict[str, list[float]] = {}
    for run in runs:
        for item in (run or {}).get("items") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().lower()
            try:
                grams = float(item.get("grams") or 0)
            except (TypeError, ValueError):
                continue
            if name and grams > 0:
                by_name.setdefault(name, []).append(grams)
    out = []
    for name, vals in by_name.items():
        if len(vals) > 1 and min(vals) > 0 and max(vals) / min(vals) > 1.25:
            out.append((name, min(vals), max(vals)))
    return sorted(out, key=lambda r: -(r[2] / r[1]))


# The target, stated so a run either meets it or does not.
#
# 10% per item, chosen against Google's Nutrition5k: RGB alone 18.8% mass error,
# RGB plus a depth volume scalar 13.7%, professional nutritionists 41%. Under
# 10% would be better than any published number on this task.
#
# Per ITEM, not per meal. A meal total can be right while the split between its
# items is wrong, and the split is what a macro figure is built from.
TARGET_ITEM_ERROR = 10.0


# Student's t, two-sided 95%, by degrees of freedom. Small table rather than a
# dependency; above 30 the value is flat enough that 2.04 is honest.
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36,
        8: 2.31, 9: 2.26, 10: 2.23, 11: 2.20, 12: 2.18, 13: 2.16, 14: 2.14,
        15: 2.13, 16: 2.12, 17: 2.11, 18: 2.10, 19: 2.09, 20: 2.09,
        24: 2.06, 29: 2.05}


def confidence_interval(values: list[float]) -> tuple[float, float] | None:
    """The 95% interval for the mean of these errors, or None below n=3.

    WHY THE BENCH REFUSES TO PRINT A BARE MEAN.

    A mean over ten items looks like a measurement and is a sample. Ten items
    with a spread this wide put the interval several points either side, and a
    change smaller than that interval is not a result -- it is the sample
    moving. Four separate accuracy decisions were argued on single-run means
    before this existed.
    """
    n = len(values)
    if n < 3:
        return None
    mean = statistics.mean(values)
    sem = statistics.stdev(values) / math.sqrt(n)
    df = n - 1
    t = _T95.get(df) or next((v for k, v in sorted(_T95.items()) if k >= df), 2.04)
    return mean - t * sem, mean + t * sem


class BenchNotReady(Exception):
    """The bench cannot produce a meaningful number, and knows it before paying.

    An EXCEPTION rather than a returned reason, deliberately. The returned
    version was correct and `main` could still ignore it -- one `if` away from
    spending a full run against an API that was not up, which is exactly the
    failure it exists to prevent. A raise cannot be dropped by forgetting to
    read it.
    """


def preflight(cases, api: str, runs: int) -> list:
    """Check everything that can be checked for free, BEFORE spending a call.

    Three runs of this bench were burned in a row for reasons that cost nothing
    to detect up front: the API was not running, and the file on disk was an
    older copy than the one being edited. Each time the bench opened, uploaded
    a photograph, failed, and tracebacked -- so the operator learned about it
    after the wait rather than before it.

    Returns the cases worth running and a reason to stop, or None to proceed.
    Nothing here costs a model call.
    """
    missing = [c[0] for c in cases if not (PHOTOS / c[0]).exists()]
    present = [c for c in cases if (PHOTOS / c[0]).exists()]

    # WHICH COPY OF THIS FILE IS RUNNING. Printed always, because the answer to
    # "why is it still scanning the old photographs" is almost never the bench
    # and almost always a second copy of this script.
    first = cases[0][0] if cases else "none"
    print(f"  {DIM}{len(CASES)} case(s) in this file, first is {first}{OFF}")

    if missing:
        print(f"  {YEL}{len(missing)} photograph(s) not on disk{OFF} {DIM}-- "
              f"{', '.join(m[:24] for m in missing[:6])}"
              f"{' ...' if len(missing) > 6 else ''}{OFF}")
        print(f"  {DIM}expected in {PHOTOS}{OFF}")
    if not present:
        raise BenchNotReady("none of the photographs this bench names are on disk")
    if len(missing) > len(cases) / 2:
        raise BenchNotReady(
            f"{len(missing)} of {len(cases)} photographs are missing -- a score "
            f"over the rest would not mean anything")

    # Is the API up? One cheap unauthenticated call, before any upload.
    status, res = call(f"{api.rstrip('/')}/billing/plans", timeout=10)
    if status == 0:
        detail = res.get("error") if isinstance(res, dict) else res
        raise BenchNotReady(f"the API at {api} is not answering ({detail}).\n"
                            f"  Start it in another window with  dev api")

    calls = len(present) * max(1, runs)
    print(f"  {DIM}{len(present)} photo(s) x {max(1, runs)} run(s) = {calls} model "
          f"call(s){OFF}\n")
    return present


def describe_failure(status: int, res) -> str:
    """Turn whatever came back into something a person can act on.

    THE ERROR REPORTER CRASHED ON ITS OWN ERROR PATH, which is the worst place
    for a bug because it destroys the message you needed. It assumed the API's
    shape -- {"error": {"message": ...}} -- and called .get on it. But `call`
    returns FOUR different shapes, and two of them put a plain string where
    that code expected a dict:

        {"error": {"message": "..."}}   the API said no
        {"error": "..."}                the request never left the machine
        {"raw": "..."}                  a reply that was not JSON at all
        None                            no body

    The second one is a connection failure, and status 0 says so. That is the
    single most likely thing to go wrong here -- running the bench without the
    API up -- and it was the one case that raised AttributeError instead of
    saying "the API is not running".
    """
    if status == 0:
        detail = res.get("error") if isinstance(res, dict) else res
        return (f"could not reach the API: {detail}. "
                f"Start it in another window with  dev api")

    detail = None
    if isinstance(res, dict):
        err = res.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("detail")
        elif err:
            detail = err
        if detail is None:
            detail = res.get("detail") or res.get("raw")
    if detail is None:
        detail = res
    text = str(detail).strip() or "no message"
    if status == 401:
        text += "  (the bench account could not sign in -- check SUPABASE_* keys)"
    elif status == 402:
        text += "  (out of scans on the bench account)"
    return f"HTTP {status}: {text[:300]}"


def verdict(mean_abs: float, values: list[float] | None = None) -> str:
    """Did this run meet the target?

    The line does not move -- but a point estimate is not evidence about a
    target it sits near, and saying so is not hedging. When the interval
    straddles 10% the honest answer is that this sample cannot tell, and the
    fix is more weighed items, not a rounder number.
    """
    ci = confidence_interval(values or [])
    band = f"  {DIM}95% CI {ci[0]:.1f}-{ci[1]:.1f}%{OFF}" if ci else ""
    if ci and ci[0] <= TARGET_ITEM_ERROR <= ci[1]:
        return (f"{YEL}TOO FEW ITEMS TO SAY{OFF}  {DIM}({mean_abs:.1f}% per item, "
                f"but the 95% interval {ci[0]:.1f}-{ci[1]:.1f}% straddles the "
                f"10% target -- this sample cannot decide it){OFF}")
    if mean_abs <= TARGET_ITEM_ERROR:
        return (f"{GRN}MEETS THE 10% TARGET{OFF}  {DIM}({mean_abs:.1f}% per item; "
                f"Nutrition5k puts RGB-only at 18.8% and nutritionists at 41%){OFF}"
                + band)
    gap = mean_abs - TARGET_ITEM_ERROR
    return (f"{YEL}{gap:.1f} points short of the 10% target{OFF}  "
            f"{DIM}({mean_abs:.1f}% per item){OFF}" + band)


# How far the printed run may sit from the all-runs mean before the bench says
# out loud that it is not representative. Three points, because the target it
# is being judged against is ten.
HEADLINE_DRIFT_POINTS = 3.0


def headline(printed: list[float], all_runs: list[float]
             ) -> tuple[list[str], list[float]]:
    """Which set of errors the verdict is taken over, and what to say about it.

    Returns the lines to print and THE ERRORS THE VERDICT SHOULD USE -- the
    second half is the point. The swap used to be two assignments buried in the
    middle of `main`, where nothing could test that it happened, and a bench
    that silently reverts to one run of three is the exact failure this whole
    section exists to prevent.

    All runs when there are more of them: each is a real scan, so nothing is
    invented, n triples and the interval shrinks by root three.
    """
    if len(all_runs) <= len(printed):
        return [], printed
    mean = statistics.mean(all_runs)
    ci = confidence_interval(all_runs)
    band = f"   {DIM}95% CI {ci[0]:.1f}-{ci[1]:.1f}%{OFF}" if ci else ""
    lines = [f"{HDR}Individual items, ALL runs{OFF}   n={len(all_runs)}   "
             f"mean absolute {mean:.1f}%{band}"]
    drift = abs(mean - statistics.mean(printed)) if printed else 0.0
    if drift > HEADLINE_DRIFT_POINTS:
        lines.append(f"  {YEL}the printed run sits {drift:.1f} points from the "
                     f"all-runs mean{OFF} {DIM}-- it is one sample, not the "
                     f"result{OFF}")
    return lines, all_runs


def score_items(items: list, actuals: dict) -> tuple[dict[int, tuple[str, float]],
                                                     list[str]]:
    """Pair each detected item with a weighed one. Returns scored and dropped.

    The DROPPED half is why this is a function rather than three lines inside
    the loop. It was three lines inside the loop, the drop was silent, and two
    items -- the two hardest on the bench -- were missing from the headline
    figure with nothing on screen saying so. A silent exclusion cannot be
    tested; a returned one can.

    Each weighed item is claimed at most once. Two detections of the same food
    used to match the same weighed row and both scored against its full weight,
    so a meal split in two by the model scored twice and each half read light.
    """
    scored: dict[int, tuple[str, float]] = {}
    dropped: list[tuple[int, str]] = []
    remaining = dict(actuals)
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "")
        m = match(name, remaining)
        if m:
            label, weighed = m
            remaining.pop(label, None)
            scored[i] = (label, weighed)
        else:
            dropped.append((i, name))

    # THE RESIDUAL PAIR, and why it is not cheating.
    #
    # When exactly one detection and exactly one weighed food are left over,
    # there is only one way they can correspond. Nothing is being guessed: the
    # plate held two foods, one pair is settled, and the other pairing is
    # forced by arithmetic.
    #
    # This is not a nicety. Without it the bench excluded the SAME dish from
    # every run -- a plate of rajas read as "creamy chicken", "creamy mushroom
    # sauce", "creamy pasta" and "creamy dish with mushrooms" across four
    # photographs, never matching the weighed name, never scored. The app
    # produced a number for that food every time and was never once held to it,
    # and the exclusion ran one way: toward the items hardest to identify.
    #
    # Deliberately only ONE-to-one. With two of each left over there are two
    # possible pairings and choosing between them would be inventing evidence,
    # which is what position-based matching did before it was removed.
    if len(dropped) == 1 and len(remaining) == 1:
        i, _name = dropped[0]
        label, weighed = next(iter(remaining.items()))
        scored[i] = (label, weighed)
        remaining.clear()
        dropped = []

    return scored, [n for _i, n in dropped]


def errors_over_all_runs(runs: list[dict], actuals: dict) -> list[float]:
    """Every run's per-item errors, not just the run that gets printed.

    The detail lines come from one real scan -- the median by meal total -- and
    that is right for reading. It is wrong for AVERAGING: it throws away two
    thirds of the evidence and hands the summary a single sample of a process
    the bench has just finished showing is noisy. Two runs of identical code
    over identical photographs returned 19.4% and 27.1% per item.

    Scoring every run instead triples n and shrinks the interval by root three,
    and invents nothing: each number is an error from a scan that happened.
    """
    out: list[float] = []
    for run in runs or []:
        items = (run or {}).get("items") or []
        scored, _ = score_items(items, actuals)
        for i, (_label, weighed) in scored.items():
            try:
                est = float(items[i].get("grams"))
            except (TypeError, ValueError, AttributeError, IndexError):
                continue
            if weighed:
                out.append(abs((est - weighed) / weighed * 100))
    return out


def sample_health(errors: list[float], unmatched: list[str],
                  meals: set[str] | None = None) -> list[str]:
    """What the reader needs in order to not over-read the mean above it.

    Two things make a bench figure a false reading, and both have happened here:

      NOT INDEPENDENT   ten items is not ten observations when three of them
                        are the same 77 g of rice, photographed twice and
                        scanned a third time with the plate withheld. The
                        number of distinct weighed MEALS is the honest n.

      NOT RANDOM        an item is scored only when its name can be paired with
                        a weighed name, so a dish the model could not identify
                        silently leaves the average -- and that is exactly the
                        dish whose density, height prior and nutrition row are
                        also wrong. Dropping the hardest items makes the mean
                        read better than the product is.

    Returned as lines rather than printed so this can be tested for what it
    SAYS, not for whether a string still appears in main().
    """
    out: list[str] = []
    n_meals = len(meals or ())
    if n_meals:
        line = f"{DIM}from {n_meals} distinct weighed meal(s){OFF}"
        if n_meals < len(errors) / 2:
            line += (f"  {YEL}-- items from one meal are not independent "
                     f"observations{OFF}")
        out.append(line)
    if unmatched:
        out.append(
            f"{YEL}{len(unmatched)} detected item(s) were NOT scored{OFF} "
            f"{DIM}-- no weighed item matched the name: "
            f"{', '.join(unmatched[:6])}{' ...' if len(unmatched) > 6 else ''}{OFF}")
        out.append(
            f"{DIM}Excluded from every figure above, and not at random: the "
            f"dish the model could not name is the one whose density, height "
            f"prior and nutrition row are wrong too, so the mean reads better "
            f"than the product is.{OFF}")
    return out


def ident_flag(it: dict) -> str:
    """Mark items the model described rather than recognised.

    Shown on the bench because the NAME is not a label -- it picks the density,
    the height prior and the nutrition lookup. Photos 15 and 16 are one plate
    called "creamy chicken" once and "creamy mushroom sauce" once, 315 g against
    186 g, on geometry that was within 12% both times.
    """
    state = str(it.get("identification") or "named")
    if state == "described":
        return f"  {YEL}described{OFF}"
    if state == "unsure":
        return f"  {RED}unsure{OFF}"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000/v1")
    ap.add_argument("--runs", type=int, default=1,
                    help="scan every photo this many times and report the spread. "
                         "One run cannot tell a real change from model variance: "
                         "measured on two consecutive runs of IDENTICAL code, the "
                         "per-photo error moved 9.3 points on average and 37.3 "
                         "points at worst, and the headline mean shifted 5.9 "
                         "points on noise alone.")
    ap.add_argument("--only", nargs="+", metavar="PREFIX",
                    help="run just the photos whose names start with these, e.g. "
                         "--only 07 08. Every scan costs a model call, so a "
                         "question about one meal should not be billed as "
                         "sixteen.")
    args = ap.parse_args()

    cases = CASES
    if args.only:
        wanted = tuple(str(o).lstrip("0").zfill(2) if str(o).isdigit() else str(o)
                       for o in args.only)
        cases = [c for c in CASES if c[0].startswith(wanted)]
        if not cases:
            print(f"{RED}nothing matching {' '.join(args.only)}{OFF}  "
                  f"{DIM}known: {', '.join(sorted({c[0][:2] for c in CASES}))}{OFF}")
            return 1

    subset = "" if cases is CASES else f"  {YEL}subset{OFF}"
    print(f"\n{HDR}Accuracy across every weighed photo{OFF}  "
          f"{DIM}{len(cases)} meal{'s' if len(cases) != 1 else ''}{OFF}{subset}")

    try:
        cases = preflight(cases, args.api, args.runs)
    except BenchNotReady as why:
        print(f"\n  {RED}stopping before spending anything{OFF}  {DIM}{why}{OFF}\n")
        return 1

    token, uid = token_for_bench()

    per_item_errors: list[float] = []
    # Every geometry number the model returned, so the bench can measure the
    # RESOLUTION of its own inputs. This is the thing that turned out to be the
    # entire error budget: the boxes arrived on a 0.05 grid, and one step of
    # that grid is 45% of the food's weight.
    geometry_values: list[float] = []
    # The shadow measurement, scored beside the shipped one.
    measured_errors: list[float] = []
    paired_errors: list[tuple[float, float]] = []
    measured_live = measured_shadow = 0
    unmeasured: list[str] = []
    total_errors: list[tuple[str, float, float, float]] = []
    fat_shares: list[tuple[str, float]] = []
    # (error %, weighed grams). An unweighted mean lets a 27 g garnish count
    # the same as a 335 g piece of meat: on one photo the tomatoes were 40 g
    # over, which is 9% of the meal and +147% of themselves, and that single
    # number moved the reported per-item figure by more than twenty points.
    per_item_weighted: list[tuple[float, float]] = []
    # WHAT THE SCORE LEAVES OUT, and it is not random.
    #
    # An item is scored only if `match` can pair its NAME with a weighed name.
    # A dish the model could not identify therefore drops out of the average --
    # and identification failure is exactly what predicts estimation failure,
    # because the name picks the density, the height prior and the nutrition
    # row. So the items missing from the mean are the ones most likely to be
    # worst, and a mean that does not say so reads better than the product is.
    #
    # Measured: one plate of rajas came back as "creamy chicken", "creamy
    # mushroom sauce" and "creamy chicken dish" across four photographs. None
    # matched. The two hardest items on the bench were absent from the headline
    # figure and nothing on screen said so.
    unmatched: list[str] = []
    # Errors from EVERY repeat, not only the run whose items get printed. The
    # printed run is one sample; the summary should not be.
    all_run_errors: list[float] = []
    # Which weighed MEALS the scored items came from. n=10 items reads as ten
    # independent observations; three of those ten were the same 77 g of rice,
    # photographed twice and scanned once more with the plate withheld.
    scored_meals: set[str] = set()
    failures: list[str] = []

    for case in cases:
        filename, plate, actual_str, kind = case[:4]
        distance = case[4] if len(case) > 4 else None
        actuals = parse_actuals(actual_str)
        # Two rows can now name the same photo -- 13 with its plate measured
        # and 13 without -- so the header has to say which, or the pair that
        # exists to be compared is indistinguishable in the output.
        scale_label = (f"plate {plate:.0f} mm" if plate
                       else ("camera distance" if distance else "no reference"))
        print(f"{HDR}{filename}{OFF}  {DIM}{kind}  ({scale_label}){OFF}")
        # Repeated, because a single scan of a photo is one sample from a noisy
        # process, not a measurement of it. The median across runs, not the
        # mean: one scan that misidentifies a food should not drag the number
        # it is being used to judge.
        attempts, errs = [], []
        for _ in range(max(1, args.runs)):
            r, e = run_one(args.api, token, uid, filename, plate, actual_str, distance)
            (errs if e else attempts).append(e or r)
        if attempts:
            res, err = _median_run(attempts), None
            if args.runs > 1:
                spread = _run_spread(attempts)
                if spread is not None:
                    print(f"  {DIM}{len(attempts)} runs, totals spread "
                          f"{spread:.0f} g{OFF}")
                for name, lo, hi in _item_spreads(attempts):
                    print(f"  {YEL}unstable{OFF} {DIM}{name[:28]:<28} "
                          f"{lo:.0f}-{hi:.0f} g across runs ({hi / lo:.1f}x){OFF}")
        else:
            res, err = None, (errs[0] if errs else "no result")
        if err:
            print(f"  {RED}failed{OFF}  {err}\n")
            failures.append(f"{filename}: {err}")
            continue

        items = res.get("items") or []
        est_total = sum(float(i["grams"]) for i in items)
        weighed_total = sum(actuals.values())

        # Paired once, up front, so a weighed row cannot be claimed by two
        # detections -- and so the items that pair with nothing come back as a
        # list rather than vanishing.
        paired, dropped_here = score_items(items, actuals)
        if kind == "per-item":
            unmatched.extend(f"{filename[:2]} {n[:28]}" for n in dropped_here)
            # Every run's errors, not just the printed one. The detail lines
            # below stay a single real scan; the summary uses all of them.
            all_run_errors.extend(errors_over_all_runs(attempts, actuals))

        for idx, it in enumerate(items):
            m = paired.get(idx)
            est = float(it["grams"])
            if m and kind == "per-item":
                label, weighed = m
                e = (est - weighed) / weighed * 100
                per_item_errors.append(abs(e))
                per_item_weighted.append((abs(e), weighed))
                scored_meals.add(f"{filename}|{actual_str}")
                colour = GRN if abs(e) < 15 else YEL if abs(e) < 30 else RED
                # What the PIXELS said, beside what the pipeline said. A shadow
                # measurement: it did not contribute to the number on the left.
                # This column is the whole reason to run this build -- it scores
                # the measurement against the scale on every weighed photo at
                # once, which three hand-measured plates could not settle.
                # The mask is scored on the SPLIT, not on grams. It is a
                # separator: it says where one food ends and the next begins.
                # Asking it for absolute weight scored 60% against the scale;
                # asking it which share of the meal each food is, is the
                # question it can answer, so it is the question the bench asks.
                sh = it.get("measured_share")
                shadow = ""
                # A share can only be compared with a share when both cover the
                # same meal. Photo 14 detected a "braised beef" where the scale
                # weighed a drumstick, so the pixel denominator counted a food
                # the weighed denominator did not, and the comparison was
                # scoring an identification difference as a split error.
                comparable = len(items) == len(actuals)
                if sh and weighed_total and comparable:
                    true_share = weighed / weighed_total
                    me = (float(sh) - true_share) / true_share * 100
                    mcol = GRN if abs(me) < 15 else YEL if abs(me) < 30 else RED
                    shadow = (f"   {DIM}split{OFF} {float(sh):>5.1%} vs "
                              f"{true_share:>5.1%} {mcol}{me:>+6.1f}%{OFF}")
                    measured_errors.append(abs(me))
                    # Paired against the SHIPPED split, so it is like for like:
                    # what the pipeline said this item's share of the meal was.
                    ship_share = est / est_total if est_total else None
                    if ship_share:
                        paired_errors.append((
                            abs((ship_share - true_share) / true_share * 100),
                            abs(me)))
                elif sh and not comparable:
                    shadow = (f"   {DIM}split {float(sh):>5.1%}  "
                              f"(not comparable: {len(items)} detected, "
                              f"{len(actuals)} weighed){OFF}")
                else:
                    shadow = f"   {DIM}split  not measured{OFF}"
                    unmeasured.append(f"{filename[:2]} {it['name'][:20]}")
                print(f"  {it['name'][:26]:<26}{est:>7.1f} g  vs {weighed:>5.0f} g  "
                      f"{colour}{e:>+6.1f}%{OFF}  {DIM}{it['estimation_method']}{OFF}"
                      f"{ident_flag(it)}"
                      f"{shadow}")
                # The inputs, in the order of how much they move the answer.
                # This bench used to print `area_ratio` prominently and neither
                # of the two numbers that actually decide the grams, which is
                # how four nights went into tuning the inert one.
                cov, box = it.get("plate_coverage_used"), it.get("box_area_ratio")
                rep, used = it.get("reported_area_ratio"), it.get("pixel_area_ratio")
                for v in (cov, rep):
                    if v:
                        geometry_values.append(float(v))
                bits = []
                if cov is not None:
                    bits.append(f"covers {float(cov):.0%} of plate {YEL}(live){OFF}")
                if box:
                    bits.append(f"box {float(box):.1%} {YEL}(live){OFF}")
                if rep is not None:
                    inert = " (inert here)" if cov is not None else ""
                    bits.append(f"said {float(rep):.0%} of frame{inert}")
                if used is not None:
                    bits.append(f"area used {float(used):.1%}")
                # WHERE the footprint came from, and whether it reached the
                # grams. The whole build turns on this distinction, so the bench
                # has to print it or the two sources are one blended number
                # nobody can act on.
                #
                # "measured (live)" means a segmenter's mask set this item's
                # area. "measured (shadow)" means the colour rule measured it
                # and it was correctly kept out, because a colour mask is fenced
                # by the plate box and only its SHARE survives.
                msrc, mused = it.get("measured_area_source"), it.get("measured_area_used")
                marea = it.get("measured_area_ratio")
                if marea is not None:
                    if mused is not None:
                        bits.append(
                            f"measured {float(marea):.1%} {GRN}({msrc}, live){OFF}")
                        measured_live += 1
                    else:
                        bits.append(
                            f"measured {float(marea):.1%} {DIM}({msrc}, shadow){OFF}")
                        measured_shadow += 1
                if bits:
                    print(f"      {DIM}inputs: " + "   ".join(bits) + OFF)
            else:
                print(f"  {it['name'][:26]:<26}{est:>7.1f} g  "
                      f"{DIM}{it['estimation_method']}{OFF}{ident_flag(it)}"
                      f"{'  ' + YEL + 'not scored' + OFF if kind == 'per-item' else ''}")

        terr = (est_total - weighed_total) / weighed_total * 100
        total_errors.append((filename, weighed_total, est_total, terr))
        colour = GRN if abs(terr) < 15 else YEL if abs(terr) < 30 else RED
        print(f"  {'MEAL TOTAL':<26}{est_total:>7.1f} g  vs {weighed_total:>5.0f} g  "
              f"{colour}{terr:>+6.1f}%{OFF}")
        # Print how each number was reached. A bench that only prints the miss
        # tells you that something is wrong; the notes tell you which stage.
        # Four rounds of this were spent re-running a scan just to see them.
        for n in (res.get("notes") or [])[:8]:
            print(f"    {DIM}- {n}{OFF}")
        for c in (res.get("corrections") or [])[:4]:
            print(f"    {YEL}! {c}{OFF}")

        totals = res.get("totals") or {}
        kcal = totals.get("kcal")
        if kcal:
            # Macros, not just energy.
            #
            # Until now this bench scored grams and kcal and nothing else, which
            # made the one failure the whole market shares invisible to it.
            # NIH/NIDDK tested four leading apps against 102 meals weighed to
            # 0.1 g: all four came in about a third light, and the researchers
            # put it largely down to undercounting FAT -- roughly 30 g a meal.
            # Oil is what a camera cannot see.
            #
            # There is no weighed macro truth here, so this does not score. It
            # shows the split, and fat as a share of energy, which is the number
            # that would sag if the same thing were happening.
            fat = float(totals.get("fat_g") or 0)
            pro = float(totals.get("protein_g") or 0)
            carb = float(totals.get("carbs_g") or 0)
            fat_share = (9.0 * fat / kcal * 100.0) if kcal else 0.0
            print(f"  {DIM}{kcal:.0f} kcal   P {pro:.0f} g  C {carb:.0f} g  "
                  f"F {fat:.0f} g   fat is {fat_share:.0f}% of energy{OFF}")
            fat_shares.append((filename, fat_share))
        print()

    print("=" * 66)
    if fat_shares:
        # A typical mixed Western meal runs 30-40% of energy from fat. A bench
        # that consistently lands well under that is the market's failure
        # showing up here too -- and it would have been invisible before.
        shares = [s for _, s in fat_shares if s > 0]
        if shares:
            mean_share = statistics.mean(shares)
            colour = YEL if mean_share < 25 else DIM
            print(f"{HDR}Fat{OFF}   n={len(shares)}   "
                  f"mean {mean_share:.0f}% of energy from fat   "
                  f"{colour}(a mixed meal is usually 30-40%; well under that is "
                  f"the undercount every photo app shares){OFF}")
            # No per-meal call-out. The first version flagged both bean soups
            # at 8% -- and bean soup really is about 8% fat, so the flag was
            # wrong twice out of two. A warning that fires on correct readings
            # teaches people to ignore it, which costs more than it ever saves.
    if total_errors:
        errs = [abs(e) for *_, e in total_errors]
        signed = [e for *_, e in total_errors]
        print(f"{HDR}Meal totals{OFF}   n={len(errs)}   "
              f"mean absolute {statistics.mean(errs):.1f}%   "
              f"bias {statistics.mean(signed):+.1f}%")
        worst = max(total_errors, key=lambda r: abs(r[3]))
        print(f"{DIM}  worst: {worst[0]} at {worst[3]:+.1f}%{OFF}")

        # Split by whether the photo contains a scale at all.
        #
        # Not to flatter the average -- these photos stay in the bench and every
        # one of them still prints. But mixing them makes the headline number
        # mean nothing. 01-cheesesteak has no plate, no card and no distance:
        # NOTHING in it says how big a pixel is, so the estimate is a typical
        # serving and no amount of geometry work can move it. Averaged in, it
        # hides real changes in the photos that geometry CAN reach; averaged
        # out, it would hide a case real users will hit constantly.
        #
        # So both numbers are printed. The measured group is the one to watch
        # when judging a geometry change; the assumed group is the argument for
        # asking people to put a card in the frame.
        measured, assumed = [], []
        for row in total_errors:
            (measured if MEASURED_SCALE.get(row[0], True) else assumed).append(row)
        for label, rows, note in (
            ("scale measured in the photo", measured, "plate, card or camera distance"),
            ("scale assumed", assumed, "no reference — a typical serving, and geometry cannot help"),
        ):
            if not rows:
                continue
            a = [abs(e) for *_, e in rows]
            g = [e for *_, e in rows]
            print(f"  {label:<28} n={len(rows)}   mean absolute {statistics.mean(a):5.1f}%   "
                  f"bias {statistics.mean(g):+6.1f}%   {DIM}{note}{OFF}")
    if per_item_weighted:
        total_g = sum(g for _, g in per_item_weighted)
        weighted = sum(e * g for e, g in per_item_weighted) / max(total_g, 1e-6)
        print(f"{HDR}Individual items, by weight{OFF}   n={len(per_item_weighted)}   "
              f"mean absolute {weighted:.1f}%   "
              f"{DIM}(each item counted in proportion to what it weighed){OFF}")

    if per_item_errors:
        _item_mean = statistics.mean(per_item_errors)
        ci = confidence_interval(per_item_errors)
        band = f"   {DIM}95% CI {ci[0]:.1f}-{ci[1]:.1f}%{OFF}" if ci else ""
        print(f"{HDR}Individual items{OFF}   n={len(per_item_errors)}   "
              f"mean absolute {_item_mean:.1f}%{band}")

        lines, per_item_errors = headline(per_item_errors, all_run_errors)
        for line in lines:
            print(line)
        _item_mean = statistics.mean(per_item_errors)

        # HOW SOLID IS THAT NUMBER. Printed with it, always, because a mean on
        # its own invites a decision the sample cannot support.
        for line in sample_health(per_item_errors, unmatched, scored_meals):
            print("  " + line)
        print(f"\n{'=' * 66}\n  {verdict(_item_mean, per_item_errors)}\n{'=' * 66}")

    # ---- how fine are the numbers we are given? ----
    #
    # Everything else on this bench scores what the pipeline DOES with the
    # model's geometry. This scores the geometry itself, and it is the number
    # that mattered most: an input quantised to 0.05 cannot support a 10-20%
    # answer, no matter what is done with it downstream.
    if geometry_values:
        on_grid = sum(1 for v in geometry_values if abs(v * 100 - round(v * 100 / 5) * 5) < 0.4)
        share = on_grid / len(geometry_values)
        colour = RED if share > 0.8 else YEL if share > 0.4 else GRN
        print(f"\n{HDR}Resolution of the model's geometry{OFF}   n={len(geometry_values)}   "
              f"{colour}{share:.0%} of values sit exactly on a 0.05 grid{OFF}")
        if share > 0.8:
            print(f"  {DIM}Chance alone would put 20% there. The model is picking round")
            print(f"  numbers, and one step of that grid is 45% of the food's weight --")
            print(f"  coarser than the accuracy we are aiming for. Until this figure")
            print(f"  drops, nothing downstream of it can be measured.{OFF}")
        elif share <= 0.4:
            print(f"  {DIM}Near the 20% that chance would give: the geometry is now")
            print(f"  continuous, and downstream changes are worth measuring again.{OFF}")

    # ---- the shadow measurement, and the decision it exists to settle ----
    if paired_errors:
        ship = statistics.mean(e for e, _ in paired_errors)
        pix = statistics.mean(m for _, m in paired_errors)
        better = sum(1 for e, m in paired_errors if m < e)
        print(f"\n{HDR}Splitting the meal, from the pixels{OFF}   n={len(paired_errors)}   "
              f"mean absolute {pix:.1f}%   {DIM}against {ship:.1f}% for the "
              f"shipped split on the same items{OFF}")
        print(f"  better on {better} of {len(paired_errors)} items"
              f"{'' if better * 2 != len(paired_errors) else ' (a tie)'}")
        split_verdict = (GRN + "the pixel split is the better one -- use it for the split" + OFF
                   if pix < ship - 2 else
                   RED + "the pixel split is worse -- do not switch; fix the masks first" + OFF
                   if pix > ship + 2 else
                   YEL + "too close to call on this sample -- neither, yet" + OFF)
        print(f"  {split_verdict}")
        print(f"  {DIM}This split did not contribute to anything above. The mask")
        print(f"  is a colour rule, and it fails visibly rather than quietly --")
        print(f"  run  dev mask --overlay  and look at the ones that scored badly.{OFF}")
    # Did the segmenter actually reach the grams on this run? The single line
    # that answers "is it wired up, or is it configured and being ignored" --
    # which look identical from the outside and have looked identical here for
    # weeks.
    print(f"\n  footprints: {GRN}{measured_live} live{OFF} (set the grams), "
          f"{DIM}{measured_shadow} shadow{OFF} (reported only)")
    if not measured_live:
        print(f"  {YEL}No footprint reached the weight path on this run.{OFF} "
              f"{DIM}Either no segmenter is configured (SEGMENTER_PROVIDER), or")
        print(f"  every mask was refused -- the per-item notes say which.{OFF}")
    if unmeasured:
        print(f"  {DIM}not measured: {', '.join(unmeasured[:8])}"
              f"{' ...' if len(unmeasured) > 8 else ''}"
              f"  (no plate box, or the mask refused){OFF}")

    if failures:
        print(f"\n{RED}{len(failures)} scan(s) failed{OFF}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\n{DIM}A meal total can be right while the split between items is wrong,")
    print(f"so the per-item figure is the one that matters. Only the 4-item")
    print(f"plate has a weighed split.{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
