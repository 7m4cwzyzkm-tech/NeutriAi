"""Learn how tall food actually is, from what people correct.

The counterpart to `calibration.py`, and the two are deliberately disjoint.

    calibration.py     corrections on VESSEL-scaled photos  ->  vessel size
    this module        corrections on MEASURED-scale photos ->  food height

That split is the whole design. A correction says a number was wrong; it does
not say which term was wrong. Attributing it to the wrong one is worse than
learning nothing, because it moves a value that was fine -- which is exactly the
bug that had card-scaled corrections resizing people's bowls. So each module
takes only the corrections where its own term is the plausible suspect:

  * A credit card is 85.6 mm on its long edge, for every bank on earth. When a
    card set the scale, the scale is not the suspect. Neither is the vessel,
    which played no part. What is left is how tall the food stood.
  * When an assumed bowl size set the scale, the bowl is the suspect and this
    module keeps out of it.

WHAT A CORRECTION MEANS HERE
----------------------------
Grams are linear in height, so the reading is direct -- no square root, no area:

    corrected / estimated = true_height / assumed_height

Done by hand on three weighed plates this recovered the same height for the same
food in every photo, 9-15% spread, and predicted a held-out plate at 10.3% mean
absolute against the shipped estimator's 22.4%.

HOW IT AVOIDS BECOMING WORSE THAN THE GUESS IT REPLACES
-------------------------------------------------------
* Only measured-scale photos teach it. Everything else is thrown away.
* Ratios outside 0.5-2.0 are read as a misidentification, not a height. A
  drumstick corrected to a quarter of its weight was not 10 mm tall; it was
  something else.
* The median across a meal, so one wrong item on a plate of four moves nothing.
* Updates move partway in log space, so a value converges over several
  corrections instead of chasing the last one.
* Learned heights stay inside a physical range. Food is not 2 mm or 200 mm tall.
* And the estimator ignores a row until it has MIN_SAMPLES behind it, so no
  single user can move a global prior at all.
"""
from __future__ import annotations

import math
import statistics

import structlog

from ..db import maybe_one, service

log = structlog.get_logger()

# Rungs where the photo's scale was genuinely measured, so a correction is about
# the food rather than the yardstick. Mirrors MEASURED_SCALES in vision.py and
# is deliberately a whitelist: a rung added later must be opted in by hand.
MEASURED_SCALE_METHODS = frozenset({
    "plate_reference", "reference_object", "multi_image", "depth_model",
})

# Beyond this a correction is a different food, not a different height.
MIN_RATIO, MAX_RATIO = 0.5, 2.0

# Corrections smaller than this are rounding and tidying, not information.
MIN_MEANINGFUL_CHANGE = 0.05

# The slowest this is ever allowed to learn. See learning_rate(): the rate
# decays as 1/(n+1) so noise averages out, and this floor stops it reaching
# zero -- crockery, portions and recipes drift over years, and a value that can
# no longer move is one that can never be corrected.
MIN_LEARNING_RATE = 0.04

# Food is not 2 mm tall and it is not 200 mm tall. A value outside this is a
# bug in the arithmetic, not a discovery about lunch.
MIN_HEIGHT_MM, MAX_HEIGHT_MM = 5.0, 90.0

# Before the estimator will use a learned height at all.
MIN_SAMPLES = 12
# Before a specific food gets its own height rather than its shape's.
MIN_FOOD_SAMPLES = 25


def implied_heights(original: list[dict], corrected: list) -> dict[tuple[str, str], float]:
    """What each corrected item says its height should have been multiplied by.

    Keyed by (shape, food name) so a correction can teach the shape and, once
    there is enough of it, the food. Returns ratios, not heights: the caller
    holds the prior each ratio applies to.

    Matches corrected items to originals by name, and each original may be
    claimed only once -- a plate of three tortillas must not count one
    correction as three agreeing measurements.

    AND BY `source_index` WHEN THE NAME CHANGED, which is the whole point.

    Matching on name alone quietly threw away every correction that RENAMED a
    food -- and renaming is the most valuable correction there is, because the
    name picks the density, the height prior and the nutrition lookup. Measured
    before the fix:

        fix only the weight, keep the wrong name -> {('flat', 'x'): 0.87}
        fix the name as well                     -> {}

    So the app asked people to tell it what a food was, and learned nothing
    from the ones who did.

    The pairing is CARRIED rather than inferred. An earlier attempt fell back to
    list POSITION, and position cannot tell a rename from a replacement:
    swapping rice for beans and renaming rice to mexican rice look identical
    from the outside, and only one of them says anything about how tall the food
    stood. An item the user ADDED is an edit of nothing and has to keep teaching
    nothing -- there is a test for exactly that, and the position version broke
    it, which is how the guess got caught.

    So the client says which detected item each correction edits. An item with
    no `source_index` is matched by name, exactly as before.
    """
    unclaimed: dict[str, list[dict]] = {}
    originals = [o for o in (original or []) if isinstance(o, dict)]
    for o in originals:
        unclaimed.setdefault(str(o.get("name", "")).strip().lower(), []).append(o)
    claimed: set[int] = set()

    out: dict[tuple[str, str], list[float]] = {}
    for item in corrected or []:
        name = str(getattr(item, "name", "") or "").strip().lower()
        idx = getattr(item, "source_index", None)
        pool = unclaimed.get(name) or []
        if pool:
            was = pool.pop(0)
            try:
                claimed.add(originals.index(was))
            except ValueError:
                pass
        elif (isinstance(idx, int) and 0 <= idx < len(originals)
              and idx not in claimed):
            # Renamed. The client said which item this edits, so no guessing.
            was = originals[idx]
            claimed.add(idx)
            same = str(was.get("name", "")).strip().lower()
            if was in (unclaimed.get(same) or []):
                unclaimed[same].remove(was)
        else:
            continue
        if str(was.get("estimation_method")) not in MEASURED_SCALE_METHODS:
            continue
        shape = str(was.get("shape") or "").strip().lower()
        if not shape:
            continue
        try:
            old_g = float(was.get("grams") or 0)
            new_g = float(getattr(item, "grams", 0) or 0)
        except (TypeError, ValueError):
            continue
        if old_g <= 0 or new_g <= 0:
            continue
        ratio = new_g / old_g
        if not (MIN_RATIO <= ratio <= MAX_RATIO):
            continue
        if abs(ratio - 1.0) < MIN_MEANINGFUL_CHANGE:
            continue
        out.setdefault((shape, name), []).append(ratio)

    # Median per key: several corrections of the same food in one meal are one
    # opinion about that food, not several.
    return {k: statistics.median(v) for k, v in out.items() if v}


def shape_ratios(food_ratios: dict[tuple[str, str], float]) -> dict[str, float]:
    """One ratio per shape, the median across the meal's items.

    Without this, a plate of four items applied four separate updates to the
    shape prior -- including the one item that was misidentified. The whole
    point of a median is lost if the outlier still gets its own turn.

    A test asserting the documented behaviour is what caught it: three items
    corrected 0.9x and one 1.9x moved the shape prior four times, and the 1.9x
    landed in full.
    """
    by_shape: dict[str, list[float]] = {}
    for (shape, _food), ratio in food_ratios.items():
        by_shape.setdefault(shape, []).append(ratio)
    return {k: statistics.median(v) for k, v in by_shape.items() if v}


def learning_rate(samples: int) -> float:
    """How far this correction may move the value.

    Decaying, not fixed, and simulation says the difference is large. A fixed
    rate can never average noise out: every correction keeps yanking the value
    around, so it wanders forever instead of settling. Against a true 22 mm with
    users 25% out in either direction, a fixed 0.35 landed anywhere from 17.1 to
    27.8 mm -- and a prior that was ALREADY right at 32 mm ranged 24.8 to 40.4.

    1/(n+1) is a running mean in disguise: the first correction moves the value
    almost entirely, the hundredth barely nudges it. Same conditions, the range
    tightened to 20.2-24.4 with a mean of 22.0, and the correct prior held at
    29.5-35.5.
    """
    return max(MIN_LEARNING_RATE, 1.0 / (max(0, int(samples)) + 1))


def blend(current_mm: float, ratio: float, samples: int = 0) -> float | None:
    """Move partway toward what this correction implies.

    LINEAR, not log space, and that choice is load-bearing.

    Log space is the tidier-looking option -- a height is a scale factor, so
    halving and doubling ought to be equal and opposite moves. But people's
    corrections are unbiased in GRAMS, not in log grams, and E[log(1+noise)] is
    negative for symmetric noise. Blending in log space therefore drifts
    downward on noise alone. Simulated against a true 22 mm with users 25% off
    in either direction, log blending settled anywhere from 17.9 to 23.3 mm, and
    a prior that was ALREADY correct at 32 mm wandered down to 26.1.

    In a food app a downward drift is not a neutral inaccuracy: it means the
    thing quietly tells people they ate less than they did, forever, and gets
    more confident about it as it goes.

    Returns None when the result is not a height any food has.
    """
    if current_mm <= 0 or ratio <= 0:
        return None
    rate = learning_rate(samples)
    updated = current_mm * (1.0 - rate + rate * ratio)
    if not (MIN_HEIGHT_MM <= updated <= MAX_HEIGHT_MM):
        return None
    return updated


def _apply(sb, shape: str, food: str | None, ratio: float, priors: dict,
           source: str = "typed") -> None:
    """Fold one ratio into one row, creating it if this is the first."""
    q = sb.table("portion_learning").select("*").eq("shape", shape)
    q = q.is_("food_name", "null") if food is None else q.eq("food_name", food)
    row = maybe_one(q.limit(1).execute())

    current = float((row or {}).get("height_mm") or 0)
    if current <= 0:
        current = float(priors.get(shape, priors.get("default", 28.0)))
    if current <= 0:
        return

    samples_before = int((row or {}).get("samples") or 0)
    updated = blend(current, ratio, samples=samples_before)
    if updated is None:
        log.warning("height_learning_out_of_range", shape=shape, food=food,
                    ratio=round(ratio, 3), current=round(current, 1))
        return

    samples = samples_before + 1
    payload = {"shape": shape, "food_name": food,
               "height_mm": round(updated, 2), "samples": samples}
    if row:
        sb.table("portion_learning").update(payload).eq("id", row["id"]).execute()
    else:
        sb.table("portion_learning").insert(payload).execute()
    log.info("height_learned", shape=shape, food=food, was=round(current, 1),
             now=round(updated, 1), ratio=round(ratio, 3), samples=samples,
             source=source)


def learn_from_correction(scan_id: str | None, original_items: list[dict],
                          corrected_items: list, source: str = "typed") -> None:
    """Fold this correction into the learned heights. Never raises.

    `source` says where the corrected numbers came from: "typed" (the default,
    every ordinary correction) or "weighed" (a tester read them off a kitchen
    scale). It is recorded in the log line only; it does not change what is
    learned -- weighting by it is a separate, later decision.
    """
    try:
        if not scan_id:
            return
        ratios = implied_heights(original_items, corrected_items)
        if not ratios:
            return

        from .ai.portion import HEIGHT_PRIORS_MM

        sb = service()
        # The shape gets ONE update per meal, from the median across its items.
        # The specific foods each get their own, because those are separate
        # claims about separate foods. Rice and refried beans are both "mound"
        # and solved to 22 and 25 mm by hand, so the shape carries most of the
        # signal and the food refines it once it has enough of its own.
        for shape, ratio in shape_ratios(ratios).items():
            _apply(sb, shape, None, ratio, HEIGHT_PRIORS_MM, source)
        for (shape, food), ratio in ratios.items():
            if food:
                _apply(sb, shape, food, ratio, HEIGHT_PRIORS_MM, source)
    except Exception as exc:  # noqa: BLE001
        # Learning is a bonus. A user's correction must save regardless.
        log.warning("height_learning_failed", error=str(exc)[:200])


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------
#
# This half did not exist, and its absence was invisible: the module learned
# from every correction, wrote rows, logged "height_learned", passed twenty-five
# tests, and the estimator never once looked at the result. `portion.py` read
# HEIGHT_PRIORS_MM unconditionally. Nothing outside this file touched the table.
#
# A write-only learning loop is worse than no learning loop, because it reports
# progress. Every correction a user made was recorded as teaching the app
# something, and taught it nothing.
#
# MIN_FOOD_SAMPLES was the tell -- a constant defined, documented as "before a
# specific food gets its own height rather than its shape's", and referenced
# nowhere. A gate on a path that was never walked.

def learned_heights() -> dict[tuple[str, str | None], float]:
    """Heights that have earned the right to override the prior.

    Keyed (shape, food_name) with food_name None for the shape-level answer, so
    a caller can look up the specific food first and fall back to its shape --
    which is the order the two thresholds are built around.

    The floors are the point. A height from one correction is one person's
    opinion about one plate; MIN_SAMPLES worth of them is a measurement of how
    that shape is actually served, and MIN_FOOD_SAMPLES is where a food has
    earned a height distinct from its shape. Below those the prior stands,
    because the prior is at least an honest guess rather than a confident echo
    of whoever corrected first.

    Never raises. A scan must not fail because the learning table is unreachable
    -- it falls back to the priors, which is exactly where it was before.
    """
    try:
        res = (service().table("portion_learning")
               .select("shape,food_name,height_mm,samples").execute())
        rows = list(getattr(res, "data", None) or [])
    except Exception as exc:  # noqa: BLE001
        log.warning("learned_heights_read_failed", error=str(exc)[:200])
        return {}

    out: dict[tuple[str, str | None], float] = {}
    best_samples: dict[tuple[str, str | None], int] = {}
    for r in rows:
        try:
            height = float(r.get("height_mm"))
            samples = int(r.get("samples") or 0)
        except (TypeError, ValueError):
            continue
        food = r.get("food_name") or None
        floor = MIN_FOOD_SAMPLES if food else MIN_SAMPLES
        if samples < floor:
            continue
        if not (MIN_HEIGHT_MM <= height <= MAX_HEIGHT_MM):
            # The writer already bounds this. Checked again on the way out
            # because a bad row reaching the estimator is a wrong meal, and a
            # row can be edited by hand in the SQL console.
            continue
        shape = str(r.get("shape") or "").strip()
        if not shape:
            continue
        # Best-evidenced row wins, and ties break on nothing -- the first one
        # stays. Assigning blindly would make the answer depend on the order
        # Postgres happened to return rows in.
        #
        # This matters because the unique constraint in 0021 does not stop
        # duplicate shape-level rows: NULL is not equal to NULL in Postgres, so
        # `unique (shape, food_name)` never restricted the rows where food_name
        # is null, which is every shape-level row. Verified against Postgres 16
        # -- two (mound, null) rows both inserted. 0023 adds the partial index
        # that actually enforces it, and this makes the reader safe regardless,
        # so a database that has not had 0023 applied yet cannot produce two
        # different weights for the same photograph.
        key = (shape, food)
        if key in out and samples <= best_samples.get(key, -1):
            continue
        out[key] = height
        best_samples[key] = samples
    return out


def height_for(shape: str, food: str | None,
               learned: dict[tuple[str, str | None], float] | None,
               prior_mm: float) -> tuple[float, str]:
    """The height to use, and where it came from.

    Specific food, then its shape, then the prior. Returns the source too, so
    the estimate can say so in its notes -- a number that quietly changed
    because of other people's corrections should be able to explain itself.
    """
    if learned:
        if food:
            hit = learned.get((shape, food.strip().lower()))
            if hit:
                return hit, "learned_food"
        hit = learned.get((shape, None))
        if hit:
            return hit, "learned_shape"
    return prior_mm, "prior"
