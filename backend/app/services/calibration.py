"""Learn vessel sizes from what users correct.

Why this exists
---------------
Measured across six weighed meals: a vessel the user had measured with a tape
gave 7% per-item error. Vessels the app guessed at gave -24% to -54%. Vessel
size is the single biggest accuracy lever left in the portion estimator.

But asking people to measure their crockery does not scale, and an app that is
only accurate for the few who do is inaccurate for nearly everyone.

A correction is already a measurement. When someone changes 150 g to 200 g,
and we know which vessel was in the photo, the vessel's size can be recovered.
Grams scale with the vessel's AREA, and area with the square of its width, so:

    corrected / estimated = (true_width / assumed_width) ^ 2
    true_width = assumed_width * sqrt(corrected / estimated)

No extra work from the user, from data they were already giving.

How it avoids being worse than the guess it replaces
----------------------------------------------------
* A size the user measured themselves is never overwritten. A tape beats
  inference, always.
* Only geometry-based estimates teach it. Correcting an ``ai_prior`` item says
  nothing about a vessel, because no vessel was used to produce that number.
* The median ratio across the meal is used, not the mean. One misidentified
  food on a plate of four should not move the vessel size.
* Ratios outside 0.4-2.5x are ignored. Beyond that the food was probably
  identified wrongly rather than sized wrongly, and a 5x "correction" would
  wreck a vessel that was fine.
* Updates move only partway (in log space) toward the implied size, so the
  value converges over several corrections instead of chasing the last one.
"""
from __future__ import annotations

import math
import statistics

import structlog

from ..db import maybe_one, service

log = structlog.get_logger()

# Estimates where the VESSEL is what set the scale. Correcting anything else
# teaches nothing about vessel size, and crediting it to the vessel is worse
# than learning nothing -- it moves a number that was not at fault.
#
# reference_object is the one that had to go. A credit card is 85.6 mm on its
# long edge, the same for every bank on earth, and when a card sets the scale
# the crockery plays no part in the answer. A correction on that photo means the
# food was identified wrongly, or stood taller or shorter than assumed -- and
# the old list quietly resized the user's bowl to absorb it. Measured: on the
# weighed bench, card-scaled photos were the MOST accurate rung there is, so
# corrections on them are precisely the ones that must not touch a vessel.
#
# multi_image and pixel_area are excluded for the milder reason that what set
# their scale is ambiguous, and a guess about which term was wrong is not
# evidence about either.
VESSEL_SCALED_METHODS = {"plate_reference", "vessel_reference"}

# Outside this, treat the correction as a food-identification fix rather than a
# sizing one.
MIN_RATIO, MAX_RATIO = 0.4, 2.5

# How far to move toward the implied size on each correction. Full trust would
# make the size oscillate with every odd meal; this converges in a handful.
LEARNING_RATE = 0.45

# Plausible vessel widths. A learned value outside this is a bug, not a bowl.
MIN_WIDTH_MM, MAX_WIDTH_MM = 60.0, 500.0


def _implied_ratio(original: list[dict], corrected: list[dict]) -> float | None:
    """How far out the vessel size was, from what the user changed.

    Matches corrected items to originals by name, because the user may have
    added, removed or renamed items and only the ones present in both say
    anything about sizing.
    """
    # Each original may be claimed once. A dict keyed by name silently collapsed
    # duplicates, so a plate of three tortillas taught the vessel size from one
    # of them and counted it three times -- one item's correction weighted as
    # though three independent measurements agreed.
    unclaimed: dict[str, list[dict]] = {}
    for o in original:
        unclaimed.setdefault(str(o.get("name", "")).strip().lower(), []).append(o)
    ratios: list[float] = []

    for item in corrected:
        name = str(getattr(item, "name", "") or "").strip().lower()
        pool = unclaimed.get(name) or []
        if not pool:
            continue
        was = pool.pop(0)
        if str(was.get("estimation_method")) not in VESSEL_SCALED_METHODS:
            continue
        old_g = float(was.get("grams") or 0)
        new_g = float(getattr(item, "grams", 0) or 0)
        if old_g <= 0 or new_g <= 0:
            continue
        r = new_g / old_g
        if MIN_RATIO <= r <= MAX_RATIO:
            ratios.append(r)

    if not ratios:
        return None
    # Median: one wrongly identified food on a four-item plate must not move
    # the vessel size.
    return statistics.median(ratios)


def learn_from_correction(user_id: str, scan_id: str | None,
                          original_items: list[dict], corrected_items: list,
                          source: str = "typed") -> None:
    """Update the user's size for whichever vessel was in this photo.

    Never raises: a failure to learn must not fail the user's correction.

    `source` says where the corrected numbers came from: "typed" (the default,
    every ordinary correction) or "weighed" (a tester read them off a kitchen
    scale). It is recorded in the log line only; it does not change what is
    learned -- weighting by it is a separate, later decision.
    """
    try:
        if not scan_id:
            return
        sb = service()
        scan = maybe_one(
            sb.table("food_scans").select("vessel").eq("id", scan_id).limit(1).execute()
        )
        vessel = (scan or {}).get("vessel")
        if not vessel:
            return

        existing = maybe_one(
            sb.table("scan_calibrations").select("*")
            .eq("user_id", user_id).eq("vessel", vessel).limit(1).execute()
        )
        # A size the user measured themselves is never overwritten by inference.
        if existing and not existing.get("learned", False):
            return

        ratio = _implied_ratio(original_items, corrected_items)
        if ratio is None or abs(ratio - 1.0) < 0.05:
            return

        from .ai.portion import VESSEL_WIDTH_MM, vessel_area

        current = float(existing.get("real_diameter_mm") or 0) if existing else 0.0
        if current <= 0:
            current = VESSEL_WIDTH_MM.get(vessel, 0.0)
        if current <= 0:
            area = vessel_area(vessel)[0]
            if not area:
                return
            current = math.sqrt(area * 4 / math.pi)

        # width scales with the square root of the gram ratio, damped so the
        # value converges rather than chasing the most recent meal.
        implied = current * math.sqrt(ratio)
        updated = math.exp(
            (1 - LEARNING_RATE) * math.log(current) + LEARNING_RATE * math.log(implied)
        )
        if not (MIN_WIDTH_MM <= updated <= MAX_WIDTH_MM):
            log.warning("calibration_out_of_range", vessel=vessel, width=updated)
            return

        samples = int((existing or {}).get("samples") or 0) + 1
        row = {
            "user_id": user_id,
            "label": f"my {vessel.replace('_', ' ')}",
            "reference_kind": "custom",
            "vessel": vessel,
            "real_diameter_mm": round(updated, 1),
            "samples": samples,
            "learned": True,
            "is_default": False,
        }
        if existing:
            sb.table("scan_calibrations").update(row).eq("id", existing["id"]).execute()
        else:
            sb.table("scan_calibrations").insert(row).execute()

        log.info("calibration_learned", vessel=vessel, user_id=user_id,
                 ratio=round(ratio, 3), was=round(current, 1),
                 now=round(updated, 1), samples=samples, source=source)
    except Exception as exc:  # noqa: BLE001
        # Learning is a bonus. A user's correction must save regardless.
        log.warning("calibration_learning_failed", error=str(exc)[:200])
