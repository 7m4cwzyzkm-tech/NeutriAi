"""Measure the user's own crockery from their own photos, and say how well.

WHAT THIS IS FOR

Scale multiplies every gram the app reports. A vessel whose width is 10% wrong
makes every portion off it 21% wrong in area, before the food has even been
named. It is also the one term in the pipeline where 1% is actually reachable,
because a dinner plate has a fixed size and only has to be measured once.

THE FREE MEASUREMENT

A credit card is 85.60 x 53.98 mm, the same for every bank on earth, and
`reference_cv` finds one in the pixels to about 1%. The vision model reports how
wide the vessel looks as a fraction of the frame. Multiply:

    vessel_width_mm = frame_width_mm * plate_ellipse_w

That is a measurement of the user's plate, from a photo they were taking anyway.

WHY IT TAKES MANY PHOTOS, AND WHY THAT IS THE POINT

The card side is precise; the plate side is not. The model returns geometry on a
0.05 grid -- measured, 100% of values on the weighed bench sat exactly on it
where chance would put 20% -- so a plate that looks 0.60 wide is reported 0.60
whether it is 0.58 or 0.62. About +/-4% from one photo, and nothing recovers it
from that one photo.

Across photos it does recover, because the rounding falls in different
directions as the camera moves:

    photos of one plate      1      4      9     16     25
    error on its width     4.2%   2.1%   1.4%   1.1%   0.8%

Twenty-odd photos of one plate gets under 1%. Three weeks of someone
photographing their meals produces that without asking them for anything. That
is what the trial period is: not a trial of the app, a period in which the app
measures the user's kitchen.

WHAT IT REFUSES TO DO

* It never overwrites a width the user measured with a tape. A tape beats
  inference, always, and the confidence maths says so too: one tape reading is
  worth about seventy card-derived ones.
* It reports the error it can defend, from the observed spread, not a number
  chosen to look reassuring. If twelve photos disagree wildly, the error stays
  large and the progress card says so.
* Below MIN_OBSERVATIONS it publishes nothing. A width from two photos is a
  coin toss with a decimal point on it, and shipping it would be worse than the
  generic prior it replaced, because it would be believed.
"""
from __future__ import annotations

import math
import statistics

import structlog

from ..db import maybe_one, service

log = structlog.get_logger()

# What we are aiming at, on the vessel's WIDTH. Area error is twice this, so 1%
# here is 2% on every portion the vessel scales -- which is the point at which
# scale stops being the thing limiting the answer.
TARGET_WIDTH_ERROR = 0.01

# Single-observation error by source, as a fraction of the width. These set the
# weights, so they have to be the real numbers rather than flattering ones.
#
#   tape             a person reading a tape measure against a plate rim:
#                    +/-2 mm on 254 mm.
#   reference_object the card is ~1%, and the model's 0.05 grid on a typical
#                    0.6-wide plate is +/-0.025/0.6 = 4.2%. The grid dominates,
#                    so this is the grid.
#   correction       recovered from a portion the user retyped, which also
#                    absorbs every other error in the estimate. Weak on purpose.
SOURCE_ERROR = {
    "tape": 0.008,
    "reference_object": 0.042,
    "correction": 0.150,
}
DEFAULT_SOURCE_ERROR = 0.20

# Below this the answer is not published at all. Two photos can agree closely by
# luck, and a confident wrong width is worse than an honest generic prior.
MIN_OBSERVATIONS = 4
# ...unless a tape reading is among them, which stands on its own.
TAPE_IS_ENOUGH = True

# A plate is not 40 mm across and not 700 mm across. Anything outside this was a
# misread frame or a misread plate, and averaging it in would move a good
# estimate a long way.
MIN_WIDTH_MM, MAX_WIDTH_MM = 60.0, 600.0

# How far from the running estimate a new observation may sit before it is
# treated as a different vessel rather than a noisy reading of this one. Someone
# who buys a second bowl should not slowly corrupt the first one's size.
OUTLIER_RATIO = 1.8

# Sanity bound on the published error. An estimate from a handful of photos that
# happen to agree exactly is not better than the method that produced them, and
# claiming 0.1% would be a lie the progress card then shows the user.
ERROR_FLOOR = 0.005


def observe_width_mm(frame_width_mm: float | None,
                     plate_ellipse_w: float | None) -> float | None:
    """One observation of a vessel's real width, from a reference in the photo.

    `frame_width_mm` comes from an object of known size found in the pixels.
    `plate_ellipse_w` is how wide the vessel looks, as a fraction of the frame.

    Width, not area, and deliberately: both inputs are linear, and squaring here
    would square their errors before they have had a chance to average out.
    """
    try:
        frame = float(frame_width_mm)
        share = float(plate_ellipse_w)
    except (TypeError, ValueError):
        return None
    if not (frame > 0 and 0 < share <= 1.0):
        return None
    width = frame * share
    if not (MIN_WIDTH_MM <= width <= MAX_WIDTH_MM):
        return None
    return round(width, 2)


def _weighted(observations: list[dict]) -> tuple[float, float, int] | None:
    """Best width, its expected error, and how many rows it rests on.

    Inverse-variance weighting, which is the correct combination for readings of
    different precision: a tape reading at 0.8% carries about 28x the weight of
    a card-derived one at 4.2%, because weight goes as 1/error^2. That is why a
    single tape measurement is not diluted by a pile of noisier photos -- a
    thing a plain average gets badly wrong.
    """
    rows = []
    for o in observations:
        try:
            w = float(o.get("width_mm"))
        except (TypeError, ValueError):
            continue
        if not (MIN_WIDTH_MM <= w <= MAX_WIDTH_MM):
            continue
        err = SOURCE_ERROR.get(str(o.get("source")), DEFAULT_SOURCE_ERROR)
        rows.append((w, err))
    if not rows:
        return None

    # A first pass to find the centre, so outliers can be judged against
    # something rather than against a mean they are already dragging.
    centre = statistics.median(w for w, _ in rows)
    kept = [(w, e) for w, e in rows
            if 1.0 / OUTLIER_RATIO <= w / centre <= OUTLIER_RATIO]
    if not kept:
        return None

    # A tape reading is not averaged with inference -- it replaces it.
    #
    # Inverse-variance weighting says otherwise: twenty card photos at 4.2% each
    # carry, in aggregate, more weight than one tape reading at 0.8%, and the
    # first version of this function duly pulled a correct 254 mm tape
    # measurement to 273 mm on the strength of twenty bad photos. That is the
    # right answer for twenty INDEPENDENT readings of the same quantity and the
    # wrong answer here, because the card-derived readings share a systematic
    # term the tape does not have -- the model's grid -- so they do not average
    # toward the truth the way the formula assumes. The docstring already
    # promised the tape wins. Now it does.
    tape = [(w, e) for w, e in kept if e <= SOURCE_ERROR["tape"]]
    if tape:
        kept = tape

    # Weights are inverse variance in MILLIMETRES, not in fractions. The first
    # version weighted by 1/err^2 with err a fraction, then divided the result
    # by the mean -- which made the published error smaller than the truth by
    # roughly the width of the plate. At one photo it claimed 0.50% where the
    # real error was 2.25%, and ERROR_FLOOR hid the discrepancy. A progress card
    # is a promise to the user; it has to be arithmetic, not decoration.
    sigmas = [max(e * w, 0.1) for w, e in kept]
    weights = [1.0 / (s * s) for s in sigmas]
    total = sum(weights)
    mean = sum(w * k for (w, _), k in zip(kept, weights)) / total

    # Two estimates of the error, and we publish the WORSE of them.
    #
    #   formal   what the sources claim, if they are as good as advertised
    #   spread   what the readings actually did, which is the only one that
    #            notices when the claim is optimistic
    #
    # Taking the max means a set of photos that disagree cannot report a small
    # error just because there were many of them.
    formal = math.sqrt(1.0 / total) / mean
    if len(kept) >= 3:
        sd = statistics.stdev(w for w, _ in kept)
        spread = (sd / math.sqrt(len(kept))) / mean
    else:
        spread = formal
    error = max(formal, spread, ERROR_FLOOR)
    return round(mean, 2), round(error, 5), len(kept)


def estimate(observations: list[dict]) -> dict | None:
    """The current answer for one vessel, or None when there is not enough.

    Returns width_mm, error (a fraction), observations, and whether the answer
    is publishable. A tape reading publishes immediately; inferred widths wait
    for MIN_OBSERVATIONS, because a width from two photos is a coin toss.
    """
    combined = _weighted(observations)
    if not combined:
        return None
    width, error, n = combined
    has_tape = any(str(o.get("source")) == "tape" for o in observations)
    usable = bool(n >= MIN_OBSERVATIONS or (has_tape and TAPE_IS_ENOUGH))
    return {
        "width_mm": width,
        "error": error,
        "error_pct": round(error * 100, 2),
        "observations": n,
        "measured": has_tape,
        "usable": usable,
        "at_target": bool(usable and error <= TARGET_WIDTH_ERROR),
    }


def photos_needed(current: dict | None, target: float = TARGET_WIDTH_ERROR) -> int:
    """How many more photos of this vessel to reach the target.

    From 1/sqrt(n): to cut the error by a factor k you need k^2 times the
    observations. Honest about its own assumption -- it presumes the next photos
    are as good as the ones so far, which is why the progress card recomputes it
    every time rather than counting down from a promise made at the start.
    """
    if not current:
        return max(MIN_OBSERVATIONS, 1)
    err, n = float(current["error"]), int(current["observations"])
    if err <= target or n <= 0:
        return 0
    needed = math.ceil(n * (err / target) ** 2) - n
    # A tape measurement is one action worth dozens of photos; never quote a
    # number so large that the honest advice is drowned by it.
    return int(min(needed, 999))


def record(user_id: str, vessel: str, width_mm: float, source: str,
           scan_id: str | None = None, prior_mm: float | None = None) -> None:
    """Store one observation and refresh the vessel's published size.

    Best-effort throughout. A scan must never fail because the app was trying to
    learn something from it -- the user's meal is the product; this is a side
    effect of it.
    """
    if not (user_id and vessel) or width_mm is None:
        return
    # A surface with no fixed size cannot be measured, and filing observations
    # of one fills the history with rows no estimate can ever use.
    if vessel not in MEASURABLE_VESSELS:
        return
    if not (MIN_WIDTH_MM <= float(width_mm) <= MAX_WIDTH_MM):
        return
    try:
        sb = service()
        sb.table("vessel_observations").insert({
            "user_id": user_id,
            "vessel": vessel,
            "width_mm": round(float(width_mm), 2),
            "source": source,
            "prior_mm": (round(float(prior_mm), 2) if prior_mm else None),
            "scan_id": scan_id,
        }).execute()
        refresh(user_id, vessel)
    except Exception as exc:  # noqa: BLE001
        log.warning("vessel_observation_failed",
                    vessel=vessel, source=source, error=str(exc)[:200])


def observations_for(user_id: str, vessel: str, limit: int = 200) -> list[dict]:
    try:
        res = (service().table("vessel_observations").select("*")
               .eq("user_id", user_id).eq("vessel", vessel)
               .order("created_at", desc=True).limit(limit).execute())
        return list(getattr(res, "data", None) or [])
    except Exception as exc:  # noqa: BLE001
        log.warning("vessel_observations_read_failed", error=str(exc)[:200])
        return []


def refresh(user_id: str, vessel: str) -> dict | None:
    """Recompute one vessel's size and write it where the scan path reads it.

    The scan path is deliberately left alone: it goes on reading
    scan_calibrations exactly as before, and this keeps that row current. A
    learning system that required the estimator to change shape would be a
    second thing to get wrong.
    """
    obs = observations_for(user_id, vessel)
    current = estimate(obs)
    if not current or not current["usable"]:
        return current
    width = current["width_mm"]
    try:
        sb = service()
        existing = maybe_one(
            sb.table("scan_calibrations").select("*")
            .eq("user_id", user_id).eq("vessel", vessel).limit(1).execute()
        )
        # A width the user measured with a tape is never replaced by inference.
        if existing and not existing.get("learned") and not current["measured"]:
            return current
        payload = {
            "real_diameter_mm": width,
            "real_area_mm2": round(math.pi * (width / 2.0) ** 2, 2),
            "width_error_pct": current["error_pct"],
            "observations": current["observations"],
            "learned": not current["measured"],
        }
        if existing:
            sb.table("scan_calibrations").update(payload).eq(
                "id", existing["id"]).execute()
        else:
            sb.table("scan_calibrations").insert({
                **payload,
                "user_id": user_id,
                "vessel": vessel,
                "label": vessel.replace("_", " "),
                "reference_kind": "plate",
                "is_default": False,
            }).execute()
    except Exception as exc:  # noqa: BLE001
        log.warning("vessel_refresh_failed", vessel=vessel, error=str(exc)[:200])
    return current


# Which vessels are worth measuring at all. A hand or a bare table has no fixed
# size to learn, and collecting observations of one would fill the progress card
# with work that can never finish.
MEASURABLE_VESSELS = (
    "dinner_plate", "side_plate", "bowl", "large_bowl",
    "takeout_box", "tray", "cutting_board", "skillet", "cup", "mug",
)


def progress(user_id: str) -> dict:
    """Everything the progress card needs, computed rather than promised.

    The headline is deliberately the ACCURACY, not a count of photos. "You are
    12 of 20 photos through" measures the user's compliance; "your dinner plate
    is measured to 2.4%" measures what they are getting for it, and it is the
    number that decides whether their portions are right.
    """
    vessels: list[dict] = []
    try:
        res = (service().table("vessel_observations")
               .select("vessel").eq("user_id", user_id).execute())
        # MEASURABLE_VESSELS is the filter. Without it the card tracks "paper",
        # "hand" and "table" -- surfaces with no fixed size -- and shows the
        # user progress toward a measurement that can never be finished. The
        # constant was written and then never referenced; an audit for exactly
        # this kind of dead reference is what found it.
        seen = {r["vessel"] for r in (getattr(res, "data", None) or [])
                if r.get("vessel") and r["vessel"] in MEASURABLE_VESSELS}
    except Exception as exc:  # noqa: BLE001
        log.warning("vessel_progress_read_failed", error=str(exc)[:200])
        seen = set()

    for vessel in sorted(seen):
        current = estimate(observations_for(user_id, vessel))
        if not current:
            continue
        vessels.append({
            "vessel": vessel,
            "label": vessel.replace("_", " "),
            **current,
            "photos_to_target": photos_needed(current),
        })

    at_target = [v for v in vessels if v["at_target"]]
    # Progress is the share of the user's OWN vessels that are measured well
    # enough, not a share of some list they never chose. Someone who eats off
    # one plate is finished when that plate is done.
    pct = round(100.0 * len(at_target) / len(vessels)) if vessels else 0
    best = min((v["error_pct"] for v in vessels), default=None)
    return {
        "vessels": vessels,
        "complete_pct": pct,
        "at_target": len(at_target),
        "tracked": len(vessels),
        "target_pct": round(TARGET_WIDTH_ERROR * 100, 2),
        "best_error_pct": best,
        "next_step": _next_step(vessels),
    }


def _next_step(vessels: list[dict]) -> dict:
    """One concrete action, chosen by what would help most.

    Ordered by how much accuracy each buys, not by what is easiest to ask for.
    A tape reading ends the work for that vessel immediately; a card in shot is
    the no-effort path; and when everything is at target the honest answer is to
    say so and stop asking.
    """
    if not vessels:
        return {"action": "photograph_a_meal",
                "text": "Take a photo of a meal with a bank card lying flat "
                        "beside it. That one photo starts measuring your plate."}
    worst = max(vessels, key=lambda v: v["error_pct"])
    if worst["at_target"]:
        return {"action": "done",
                "text": "Your dishes are measured. Portions off them are as "
                        "accurate as this app can make them."}
    need = worst["photos_to_target"]
    if need > 12:
        return {"action": "measure_with_tape", "vessel": worst["vessel"],
                "text": f"Measuring your {worst['label']} across with a tape "
                        f"takes ten seconds and does the work of {need} photos."}
    return {"action": "card_in_shot", "vessel": worst["vessel"],
            "text": f"About {need} more photos of your {worst['label']} with a "
                    f"card in shot brings it under "
                    f"{round(TARGET_WIDTH_ERROR * 100)}%."}
