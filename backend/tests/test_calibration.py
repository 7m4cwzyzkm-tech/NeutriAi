"""Learning vessel sizes from user corrections.

Measured across six weighed meals: a vessel the user measured gave 7% per-item
error, vessels the app guessed at gave -24% to -54%. Calibration is the biggest
remaining accuracy lever -- but asking people to measure their crockery does not
scale, so the app has to learn it from what they correct anyway.
"""
from __future__ import annotations

import math

import pytest

from app.services.calibration import (
    LEARNING_RATE, MAX_RATIO, MIN_RATIO, VESSEL_SCALED_METHODS, _implied_ratio,
)


class Item:
    def __init__(self, name, grams):
        self.name, self.grams = name, grams


def test_a_correction_reveals_the_size_error():
    """Grams scale with vessel area, so a 405/309 correction means the width
    was out by sqrt(405/309)."""
    original = [{"name": "bean soup", "grams": 309.1, "estimation_method": "vessel_reference"}]
    ratio = _implied_ratio(original, [Item("bean soup", 405)])
    assert ratio == pytest.approx(405 / 309.1, rel=1e-6)
    assert 190 * math.sqrt(ratio) == pytest.approx(217, abs=2)


def test_only_geometry_based_estimates_teach_anything():
    """Correcting an ai_prior item says nothing about a vessel -- no vessel was
    used to produce that number."""
    assert "ai_prior" not in VESSEL_SCALED_METHODS
    original = [{"name": "cheesesteak", "grams": 280, "estimation_method": "ai_prior"}]
    assert _implied_ratio(original, [Item("cheesesteak", 410)]) is None


def test_a_card_scaled_correction_never_touches_the_vessel():
    """The bug this replaced: `reference_object` was on the list, so correcting
    a card-scaled meal resized the user's bowl.

    A credit card is 85.6 mm on its long edge, the same for every bank on
    earth. When a card sets the scale the crockery plays no part in the answer,
    so a correction there means the food was identified wrongly or stood taller
    than assumed -- and the old list absorbed that into the bowl. Card-scaled
    photos are the most accurate rung on the weighed bench, which makes them
    exactly the ones whose corrections must not move a vessel.
    """
    assert "reference_object" not in VESSEL_SCALED_METHODS
    original = [{"name": "grilled meat skewer", "grams": 289,
                 "estimation_method": "reference_object"}]
    assert _implied_ratio(original, [Item("grilled meat skewer", 335)]) is None


def test_an_ambiguous_scale_teaches_nothing_either():
    """multi_image reconciles across angles and pixel_area used no vessel at
    all. Which term was wrong is a guess, and a guess is not evidence."""
    for method in ("multi_image", "pixel_area", "depth_model"):
        original = [{"name": "rice", "grams": 100, "estimation_method": method}]
        assert _implied_ratio(original, [Item("rice", 130)]) is None, method


def test_the_two_rungs_that_do_teach_still_do():
    """The fix must not stop the loop working where it is right."""
    for method in ("vessel_reference", "plate_reference"):
        original = [{"name": "rice", "grams": 150, "estimation_method": method}]
        assert _implied_ratio(original, [Item("rice", 200)]) == pytest.approx(200 / 150)


def test_a_wild_correction_is_a_misidentification_not_a_size():
    """A 10x change means the food was wrong, not the bowl. Acting on it would
    wreck a vessel size that was fine."""
    original = [{"name": "rice", "grams": 40, "estimation_method": "plate_reference"}]
    assert _implied_ratio(original, [Item("rice", 400)]) is None
    assert MIN_RATIO < 1 < MAX_RATIO


def test_the_median_ignores_one_wrong_item():
    """On a four-item plate, three sensible corrections and one bad one must
    leave the vessel size alone. The mean would not."""
    original = [
        {"name": "chicken", "grams": 142, "estimation_method": "plate_reference"},
        {"name": "beans", "grams": 76, "estimation_method": "plate_reference"},
        {"name": "rice", "grams": 80, "estimation_method": "plate_reference"},
        {"name": "spaghetti", "grams": 144, "estimation_method": "plate_reference"},
    ]
    corrected = [Item("chicken", 148), Item("beans", 79), Item("rice", 66),
                 Item("spaghetti", 300)]
    ratio = _implied_ratio(original, corrected)
    assert ratio == pytest.approx(1.04, abs=0.02), "one bad item moved the median"


def test_renamed_items_are_not_matched():
    """If the user renamed the food, we cannot attribute the change to sizing."""
    original = [{"name": "bean soup", "grams": 309, "estimation_method": "vessel_reference"}]
    assert _implied_ratio(original, [Item("lentil soup", 405)]) is None


def test_learning_is_damped_so_it_converges():
    """Full trust would make the size chase whichever meal was corrected last.
    Moving partway converges over a handful of corrections instead."""
    assert 0 < LEARNING_RATE < 1
    width, true_width = 190.0, 217.0
    for _ in range(6):
        estimated = 405 * (width / true_width) ** 2
        ratio = 405 / estimated
        implied = width * math.sqrt(ratio)
        width = math.exp((1 - LEARNING_RATE) * math.log(width)
                         + LEARNING_RATE * math.log(implied))
    assert width == pytest.approx(true_width, rel=0.05), width


def test_empty_and_missing_data_are_safe():
    assert _implied_ratio([], []) is None
    assert _implied_ratio([{"name": "x", "grams": 0, "estimation_method": "plate_reference"}],
                          [Item("x", 100)]) is None
    assert _implied_ratio([{"name": "x", "grams": 100, "estimation_method": "plate_reference"}],
                          [Item("x", 0)]) is None
