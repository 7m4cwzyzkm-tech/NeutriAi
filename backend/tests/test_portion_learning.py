"""Learning how tall food is, from what people correct.

The counterpart to test_calibration.py, and the split between them is the point:

    calibration        corrections on VESSEL-scaled photos  ->  vessel size
    portion_learning   corrections on MEASURED-scale photos ->  food height

A correction says a number was wrong. It does not say WHICH term was wrong, and
crediting it to the wrong one is worse than learning nothing, because it moves a
value that was fine. So each module takes only the corrections where its own
term is the plausible suspect, and these tests hold that line from this side.

The prize, measured by hand on three weighed plates before any of this existed:
the same food solved to the same height in every photo (rice 22/21/24 mm against
a prior of 32), and heights solved on two plates predicted a held-out third at
10.3% mean absolute against the shipped estimator's 22.4%.
"""
from __future__ import annotations

import random

import pytest

from app.services.portion_learning import (
    MAX_HEIGHT_MM, MEASURED_SCALE_METHODS, MIN_HEIGHT_MM, MIN_MEANINGFUL_CHANGE,
    MIN_RATIO, MAX_RATIO, blend, implied_heights, learning_rate, shape_ratios,
)


class Item:
    def __init__(self, name, grams):
        self.name = name
        self.grams = grams


def _was(name="mexican rice", grams=100.0, shape="mound",
         method="reference_object"):
    return {"name": name, "grams": grams, "shape": shape,
            "estimation_method": method}


# ---------------------------------------------------------------------------
# which corrections are allowed to teach
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,teaches", [
    ("reference_object", True),
    ("plate_reference", True),
    ("multi_image", True),
    ("depth_model", True),
    ("vessel_reference", False),
    ("pixel_area", False),
    ("ai_prior", False),
])
def test_only_measured_scale_corrections_teach_a_height(method, teaches):
    """When an assumed bowl size set the scale, the bowl is the suspect and
    this module keeps out of it -- calibration.py has that one."""
    got = implied_heights([_was(method=method)], [Item("mexican rice", 70)])
    assert bool(got) is teaches


def test_a_wild_correction_is_a_different_food_not_a_different_height():
    """A drumstick corrected to a quarter of its weight was not 10 mm tall. It
    was something else, and acting on it would wreck a height that was fine."""
    assert implied_heights([_was(grams=200)], [Item("mexican rice", 40)]) == {}
    assert implied_heights([_was(grams=40)], [Item("mexican rice", 200)]) == {}


def test_tidying_up_is_not_information():
    """Rounding 101 g to 100 g says nothing about how tall rice is."""
    assert implied_heights([_was(grams=100)], [Item("mexican rice", 101)]) == {}


def test_one_correction_teaches_the_shape_and_the_food():
    got = implied_heights([_was()], [Item("mexican rice", 70)])
    assert got == {("mound", "mexican rice"): pytest.approx(0.7)}


def test_the_median_protects_the_shape_from_one_wrong_item():
    """Four items on a plate, one misidentified. The shape prior is global --
    every user of the app shares it -- so one bad item must not move it.

    This test caught the bug it now guards: the code applied each item's ratio
    to the shape separately, so the outlier still got its own full turn and the
    median protected nothing.
    """
    original = [_was(name=f"item {i}", grams=100) for i in range(4)]
    corrected = [Item("item 0", 90), Item("item 1", 90),
                 Item("item 2", 90), Item("item 3", 190)]
    per_food = implied_heights(original, corrected)
    assert shape_ratios(per_food) == {"mound": pytest.approx(0.9)}


def test_each_food_still_keeps_its_own_claim():
    """The median is protection for the SHARED value, not censorship of the
    individual ones. Item 3 may well be a food that really is heavier."""
    original = [_was(name=f"item {i}", grams=100) for i in range(4)]
    corrected = [Item("item 0", 90), Item("item 1", 90),
                 Item("item 2", 90), Item("item 3", 190)]
    per_food = implied_heights(original, corrected)
    assert per_food[("mound", "item 3")] == pytest.approx(1.9)
    assert per_food[("mound", "item 0")] == pytest.approx(0.9)


def test_a_repeated_name_is_claimed_once_each():
    """Three tortillas must not count one correction as three agreeing
    measurements -- the bug that already bit the vessel loop."""
    original = [_was(name="taco", grams=100), _was(name="taco", grams=100)]
    got = implied_heights(original, [Item("taco", 130)])
    assert got == {("mound", "taco"): pytest.approx(1.3)}


def test_an_item_the_user_added_teaches_nothing():
    assert implied_heights([_was(name="rice")], [Item("beans", 90)]) == {}


# ---------------------------------------------------------------------------
# how the value moves
# ---------------------------------------------------------------------------
def test_the_rate_decays_with_evidence():
    """The first correction moves the value almost entirely; the hundredth
    barely nudges it. That is what lets noise average out instead of yanking
    the value around forever."""
    assert learning_rate(0) == pytest.approx(1.0)
    assert learning_rate(1) == pytest.approx(0.5)
    assert learning_rate(9) == pytest.approx(0.1)
    rates = [learning_rate(n) for n in range(0, 60)]
    assert all(b <= a + 1e-12 for a, b in zip(rates, rates[1:])), "rate went up"


def test_the_rate_never_reaches_zero():
    """Crockery, portions and recipes drift over years. A value that can no
    longer move is one that can never be corrected."""
    assert learning_rate(10 ** 9) > 0.0


@pytest.mark.parametrize("ratio", [0.0, -1.0, -0.5])
def test_an_impossible_ratio_is_refused(ratio):
    assert blend(32.0, ratio) is None


def test_a_height_no_food_has_is_refused():
    assert blend(32.0, 100.0) is None      # 3.2 metres of rice
    assert blend(32.0, 0.001) is None      # 32 microns of rice
    assert blend(0.0, 1.2) is None


def test_the_learned_height_stays_physical():
    for ratio in (0.5, 0.9, 1.1, 2.0):
        for samples in (0, 5, 50):
            got = blend(32.0, ratio, samples)
            assert got is None or MIN_HEIGHT_MM <= got <= MAX_HEIGHT_MM


# ---------------------------------------------------------------------------
# the property that actually matters
# ---------------------------------------------------------------------------
def _converge(true_h, prior, noise, seed, n=40):
    rnd = random.Random(seed)
    h = prior
    for i in range(n):
        ratio = (true_h / h) * (1.0 + rnd.gauss(0, noise))
        ratio = max(MIN_RATIO, min(MAX_RATIO, ratio))
        if abs(ratio - 1.0) < MIN_MEANINGFUL_CHANGE:
            continue
        moved = blend(h, ratio, samples=i)
        if moved:
            h = moved
    return h


def test_it_converges_on_the_truth_from_a_wrong_prior():
    """The rice case. The shipped prior says 32 mm; three weighed plates said
    22. Users who never touch a ruler should get it there anyway."""
    got = [_converge(22.0, 32.0, noise=0.25, seed=s) for s in range(12)]
    assert 21.0 <= sum(got) / len(got) <= 23.0
    assert max(got) - min(got) < 6.0


def test_a_prior_that_was_already_right_does_not_wander():
    """The failure mode that matters more than converging: an app that drifts
    off a correct value because people are imprecise."""
    got = [_converge(32.0, 32.0, noise=0.25, seed=s) for s in range(12)]
    assert 31.0 <= sum(got) / len(got) <= 33.0


def test_it_does_not_drift_downward_on_noise_alone():
    """Log-space blending was tried first and failed exactly here: E[log(1+e)]
    is negative for symmetric noise, so heights sank on noise alone. In a food
    app a downward drift is not a neutral inaccuracy -- it quietly tells people
    they ate less than they did, and grows more confident as it goes.
    """
    for noise in (0.25, 0.40):
        got = [_converge(32.0, 32.0, noise=noise, seed=s) for s in range(16)]
        mean = sum(got) / len(got)
        assert mean > 30.0, f"drifted down to {mean:.1f} at {noise:.0%} noise"


def test_measured_scales_match_the_ones_the_scan_result_reports():
    """These two lists have to agree or the app tells the person their portion
    was measured while this module decides it was not."""
    from app.services.ai.vision import MEASURED_SCALES
    assert set(MEASURED_SCALE_METHODS) == set(MEASURED_SCALES)


# --- the half that was missing: reading it back -------------------------------
#
# The module learned from every correction, wrote rows, logged "height_learned"
# and passed its tests, and the estimator never looked at the result --
# portion.py took HEIGHT_PRIORS_MM unconditionally and nothing outside this
# module touched the table. A write-only learning loop is worse than none,
# because the user is told their corrections matter.
#
# The tell was MIN_FOOD_SAMPLES: a constant defined, documented as "before a
# specific food gets its own height", and referenced nowhere.

def test_a_learned_height_reaches_the_estimate():
    """The connection that did not exist."""
    from app.services.ai.portion import (
        GeometryHint, HEIGHT_PRIORS_MM, estimate_grams,
    )
    hint = GeometryHint(plate_ellipse_area_ratio=0.35, plate_diameter_mm=254)
    kw = dict(name="mexican rice", area_ratio=0.15, hint=hint, plate_coverage=0.20,
              shape_hint="mound", bbox={"w": 0.45, "h": 0.45},
              detection_confidence=0.8)

    prior = estimate_grams(**kw)
    taught = estimate_grams(**kw, learned_heights={("mound", None): 24.0})
    assert taught.grams != prior.grams, "a learned height changed nothing"
    assert taught.grams / prior.grams == pytest.approx(
        24.0 / HEIGHT_PRIORS_MM["mound"], rel=0.01)


def test_a_food_that_earned_its_own_height_beats_its_shape():
    from app.services.ai.portion import GeometryHint, estimate_grams
    hint = GeometryHint(plate_ellipse_area_ratio=0.35, plate_diameter_mm=254)
    kw = dict(name="mexican rice", area_ratio=0.15, hint=hint, plate_coverage=0.20,
              shape_hint="mound", bbox={"w": 0.45, "h": 0.45},
              detection_confidence=0.8)
    shape_only = estimate_grams(**kw, learned_heights={("mound", None): 24.0})
    both = estimate_grams(**kw, learned_heights={
        ("mound", None): 24.0, ("mound", "mexican rice"): 40.0})
    assert both.grams > shape_only.grams
    assert both.grams / shape_only.grams == pytest.approx(40.0 / 24.0, rel=0.01)


def test_a_height_that_has_not_earned_its_place_is_ignored():
    """The floors are the point. One correction is one person's opinion about
    one plate; the prior is at least an honest guess rather than a confident
    echo of whoever corrected first."""
    from app.services import portion_learning as PL
    rows = [
        {"shape": "mound", "food_name": None, "height_mm": 24.0,
         "samples": PL.MIN_SAMPLES - 1},
        {"shape": "chunky", "food_name": None, "height_mm": 34.0,
         "samples": PL.MIN_SAMPLES},
        {"shape": "mound", "food_name": "mexican rice", "height_mm": 40.0,
         "samples": PL.MIN_FOOD_SAMPLES - 1},
        {"shape": "flat", "food_name": "steak", "height_mm": 20.0,
         "samples": PL.MIN_FOOD_SAMPLES},
        {"shape": "mound", "food_name": None, "height_mm": 900.0,
         "samples": 999},                                   # out of bounds
    ]

    class _Res:
        data = rows

    class _T:
        def select(self, *_a, **_k): return self
        def execute(self): return _Res()

    class _SB:
        def table(self, *_a, **_k): return _T()

    original = PL.service
    PL.service = lambda: _SB()
    try:
        got = PL.learned_heights()
    finally:
        PL.service = original

    assert ("chunky", None) in got, "a shape past the floor was dropped"
    assert ("flat", "steak") in got, "a food past the floor was dropped"
    assert ("mound", None) not in got, "a height below MIN_SAMPLES was published"
    assert ("mound", "mexican rice") not in got, (
        "a food below MIN_FOOD_SAMPLES got its own height")


def test_the_lookup_order_is_food_then_shape_then_prior():
    from app.services.portion_learning import height_for
    learned = {("mound", None): 24.0, ("mound", "mexican rice"): 40.0}
    assert height_for("mound", "mexican rice", learned, 32.0) == (40.0, "learned_food")
    assert height_for("mound", "jasmine rice", learned, 32.0) == (24.0, "learned_shape")
    assert height_for("chunky", "drumstick", learned, 34.0) == (34.0, "prior")
    assert height_for("mound", "rice", None, 32.0) == (32.0, "prior")
    assert height_for("mound", "rice", {}, 32.0) == (32.0, "prior")


def test_a_learned_height_says_so():
    """A number that quietly changed because of other people's corrections
    should be able to explain itself."""
    from app.services.ai.portion import GeometryHint, estimate_grams
    est = estimate_grams(
        name="mexican rice", area_ratio=0.15,
        hint=GeometryHint(plate_ellipse_area_ratio=0.35, plate_diameter_mm=254),
        plate_coverage=0.20, shape_hint="mound", bbox={"w": 0.45, "h": 0.45},
        detection_confidence=0.8, learned_heights={("mound", None): 24.0},
    )
    assert any("learned from corrections" in n for n in est.notes), est.notes


def test_duplicate_shape_rows_cannot_change_the_weight_between_scans():
    """The constraint in 0021 does not do what it looks like it does.

    Postgres never treats NULL as equal to NULL, so `unique (shape, food_name)`
    places no restriction on rows where food_name is null -- which is every
    shape-level row. Verified against a real Postgres 16: two `(mound, null)`
    rows were both accepted, while a duplicate named food was correctly
    rejected.

    With two rows for one shape, a reader that assigns blindly gets whichever
    Postgres returned last, and the same photograph can weigh differently on two
    scans with nothing in the output saying why. 0023 adds the partial index
    that enforces it; this makes the reader safe on a database that has not had
    0023 applied yet.
    """
    from app.services import portion_learning as PL

    duplicates = [
        {"shape": "mound", "food_name": None, "height_mm": 24.0, "samples": 12},
        {"shape": "mound", "food_name": None, "height_mm": 40.0, "samples": 30},
    ]

    def _read(rows):
        class _Res:
            data = rows

        class _T:
            def select(self, *_a, **_k): return self
            def execute(self): return _Res()

        class _SB:
            def table(self, *_a, **_k): return _T()

        original = PL.service
        PL.service = lambda: _SB()
        try:
            return PL.learned_heights()
        finally:
            PL.service = original

    forward = _read(duplicates)
    backward = _read(list(reversed(duplicates)))
    assert forward == backward, (
        f"row order changed the learned height: {forward} vs {backward}"
    )
    # And the winner is the one the most corrections went into, not the last one
    # read -- 30 samples beats 12.
    assert forward[("mound", None)] == pytest.approx(40.0)
