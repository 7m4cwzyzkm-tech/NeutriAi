"""Measuring the user's own crockery, and the honesty of the progress it shows.

Scale multiplies every gram the app reports, so this is the one term worth
driving to 1% -- and unlike the rest of the pipeline, 1% is reachable, because a
plate has a fixed size and only has to be measured once.

The tests that matter here are not about the mean. They are about whether the
error the app SHOWS THE USER is one it can defend. A progress card is a promise;
if it says 1% it has to mean 1%.
"""
from __future__ import annotations

import random
import statistics

import pytest

from app.services import scale_learning as S

TRUE_MM = 254.0


def _card_photos(n, seed=0, true_mm=TRUE_MM):
    """n photos of one plate with a card in shot.

    The card measures the frame to about 1%. The model reports how wide the
    plate looks -- rounded to its 0.05 grid, which is the dominant error and the
    reason one photo is not enough.
    """
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        share = rng.uniform(0.45, 0.75)
        frame = true_mm / share * rng.gauss(1.0, 0.010)
        w = S.observe_width_mm(frame, round(share / 0.05) * 0.05)
        if w:
            out.append({"width_mm": w, "source": "reference_object"})
    return out


@pytest.mark.parametrize("n", [1, 4, 9, 16, 25, 40])
def test_the_error_it_publishes_is_one_it_can_defend(n):
    """The published error must never be smaller than the error it actually
    has. Over-promising here is worse than a wide band: the user is told their
    plate is measured when it is not, and every portion off it inherits that.

    The first version of this got it wrong -- weights were inverse variance in
    FRACTIONS while the result was divided by a width in millimetres, so at one
    photo it claimed 0.50% where the truth was 2.25%, and ERROR_FLOOR hid the
    discrepancy behind a floor that looked like caution.
    """
    claimed, actual = [], []
    for seed in range(120):
        est = S.estimate(_card_photos(n, seed))
        if est:
            claimed.append(est["error"])
            actual.append(abs(est["width_mm"] / TRUE_MM - 1))
    assert claimed, "no estimate at all"
    assert statistics.mean(claimed) >= statistics.mean(actual), (
        f"at {n} photos the app claims {statistics.mean(claimed):.2%} and "
        f"delivers {statistics.mean(actual):.2%} -- it is over-promising"
    )


def test_more_photos_narrow_the_answer():
    """1/sqrt(n), which is what makes a three-week trial worth anything: the
    twenty-fifth photo of a plate is still buying accuracy."""
    errs = [S.estimate(_card_photos(n, seed=3))["error"] for n in (4, 9, 16, 25, 40)]
    for tighter, looser in zip(errs[1:], errs):
        assert tighter < looser, f"more photos stopped helping: {errs}"
    assert errs[-1] < errs[0] / 1.8


def test_a_tape_measurement_is_never_diluted_by_photos():
    """Inverse-variance weighting says twenty photos at 4.2% outweigh one tape
    reading at 0.8%, and the first version duly dragged a correct 254 mm to
    273 mm on the strength of twenty bad ones.

    That is right for twenty INDEPENDENT readings and wrong here: the
    card-derived ones share the model's grid as a systematic term, so they do
    not average toward the truth the way the formula assumes. A tape beats
    inference, always.
    """
    tape = [{"width_mm": 254.0, "source": "tape"}]
    wrong = [{"width_mm": 300.0, "source": "reference_object"} for _ in range(20)]
    assert S.estimate(tape + wrong)["width_mm"] == pytest.approx(254.0)
    assert S.estimate(tape + wrong)["measured"] is True
    # And one tape reading is enough on its own -- it does not wait for four.
    assert S.estimate(tape)["usable"] is True


def test_two_photos_publish_nothing():
    """A width from two photos can agree closely by luck. A confident wrong
    width is worse than the generic prior it replaces, because it is believed."""
    assert S.estimate(_card_photos(2, seed=1))["usable"] is False
    assert S.estimate(_card_photos(S.MIN_OBSERVATIONS, seed=1))["usable"] is True


def test_a_second_bowl_does_not_corrupt_the_first():
    """Someone who buys a bigger bowl and photographs it as the same vessel
    should not slowly drag the measured one across to meet it."""
    good = _card_photos(12, seed=5)
    intruder = [{"width_mm": 520.0, "source": "reference_object"}]
    before = S.estimate(good)["width_mm"]
    after = S.estimate(good + intruder)["width_mm"]
    assert abs(after / before - 1) < 0.05, f"{before} -> {after}"


@pytest.mark.parametrize("frame,share", [
    (None, 0.6), (0.0, 0.6), (-1.0, 0.6), (400.0, None), (400.0, 0.0),
    (400.0, 1.4), (400.0, "junk"), (40.0, 0.6), (100000.0, 0.9),
])
def test_an_unusable_observation_is_refused_not_guessed(frame, share):
    """None means "not measured" and the caller falls back. A plate is not 4 cm
    across and not 90 cm across, and averaging one in would move a good
    estimate a long way."""
    assert S.observe_width_mm(frame, share) is None


def test_the_free_measurement_is_the_arithmetic_it_claims():
    """frame width x how wide the plate looks = the plate. That is the whole
    trick, and it is worth pinning because everything else rests on it."""
    assert S.observe_width_mm(400.0, 0.635) == pytest.approx(254.0, abs=0.01)


def test_photos_needed_counts_down_and_reaches_zero():
    """The user is being asked for effort, so the number quoted has to fall as
    they supply it, and has to actually end."""
    seen = []
    for n in (4, 9, 16, 25, 40):
        est = S.estimate(_card_photos(n, seed=11))
        seen.append(S.photos_needed(est))
    assert seen == sorted(seen, reverse=True), seen
    assert seen[-1] == 0, f"the target is never reached: {seen}"


def test_the_next_step_asks_for_the_thing_that_helps_most():
    """When a vessel is a long way off, ten seconds with a tape beats forty
    more photos, and the card should say so rather than quoting the forty."""
    far = [{"vessel": "bowl", "label": "bowl", "error_pct": 6.0,
            "at_target": False, "photos_to_target": 40}]
    assert S._next_step(far)["action"] == "measure_with_tape"

    near = [{"vessel": "bowl", "label": "bowl", "error_pct": 1.4,
             "at_target": False, "photos_to_target": 4}]
    assert S._next_step(near)["action"] == "card_in_shot"

    done = [{"vessel": "bowl", "label": "bowl", "error_pct": 0.8,
             "at_target": True, "photos_to_target": 0}]
    assert S._next_step(done)["action"] == "done"
    # And when it is done it stops asking. An app that keeps requesting photos
    # after the measurement is finished is spending the user's goodwill on
    # nothing.
    assert "measured" in S._next_step(done)["text"]


def test_the_target_is_the_one_that_makes_scale_stop_mattering():
    """1% on width is 2% on area, which is below every other error in the
    pipeline. Tightening it further would be measuring something that no longer
    limits the answer."""
    assert S.TARGET_WIDTH_ERROR == 0.01


def test_a_tape_reading_from_the_calibrate_tool_reaches_the_learning_side():
    """The two halves have to agree about the same measurement.

    `dev calibrate` wrote scan_calibrations -- which is what a scan reads, so
    the portion was right -- and filed no observation. But the progress card and
    the 1% target are computed from vessel_observations, so it would have
    reported "no measurement yet" for a plate the user had just put a tape
    across, which is the strongest observation there is.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts"
           / "calibrate.py").read_text(encoding="utf-8")
    assert "/calibrations/measure" in src, (
        "the calibrate tool no longer files a tape reading as an observation"
    )
    assert "if args.width:" in src, (
        "rectangular vessels must be skipped -- an observation records a WIDTH, "
        "and a box's width alone does not describe it"
    )
