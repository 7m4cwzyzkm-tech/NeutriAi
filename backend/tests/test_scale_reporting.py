"""Telling the person when their portion was estimated rather than measured.

This is the largest remaining accuracy gap in the app and it is not an algorithm
problem. Across the weighed bench:

    photos with a plate, card or camera distance      -3.3% bias
    photos with none of those                        -26.8% bias

Every one of the second group is systematically light, and no estimator change
can reach them -- nothing in those photographs says how big a pixel is. The only
fix is to say so and offer the person a way to fix it, which is what these
fields drive.

Getting this wrong is worse than not having it. A false "measured" hides a 27%
undercount behind a confident number; a false "not measured" is a prompt nobody
needed. So the rule is: vouch only for what is on the list.
"""
from __future__ import annotations

import pytest

from app.services.ai.vision import MEASURED_SCALES, SCALE_LADDER, scale_summary


class _Item:
    def __init__(self, method):
        self.estimation_method = method


def _items(*methods):
    return [_Item(m) for m in methods]


@pytest.mark.parametrize("method,measured", [
    ("plate_reference", True),
    ("reference_object", True),
    ("multi_image", True),
    ("depth_model", True),
    ("vessel_reference", False),
    ("pixel_area", False),
    ("ai_prior", False),
])
def test_each_rung_reports_whether_it_measured_anything(method, measured):
    source, got = scale_summary(_items(method))
    assert source == method
    assert got is measured


def test_a_bowl_is_not_a_measurement():
    """The exclusion worth stating on its own, because it looks like one.

    A takeout clamshell and a mixing bowl both answer to "bowl", so the scale is
    a guess about crockery. Those photos carried the same -26.8% bias as the
    ones with nothing in frame at all.
    """
    assert scale_summary(_items("vessel_reference")) == ("vessel_reference", False)


def test_the_meal_reports_the_best_rung_any_item_reached():
    """Items differ when one item's own geometry fails. What we are describing
    is what the PHOTOGRAPH supported, not the weakest item in it."""
    assert scale_summary(_items("ai_prior", "plate_reference"))[0] == "plate_reference"
    assert scale_summary(_items("depth_model", "reference_object"))[0] == "reference_object"
    assert scale_summary(_items("pixel_area", "vessel_reference"))[0] == "vessel_reference"


def test_a_rung_added_later_is_not_vouched_for():
    """A whitelist, not a blacklist. Listing the bad rungs would mean any method
    added later is silently reported as measured -- the one thing this field
    must never do. This test caught exactly that bug."""
    source, measured = scale_summary(_items("some_future_method"))
    assert source == "some_future_method"
    assert measured is False


def test_every_measured_rung_is_on_the_ladder():
    """A rung that measures but is not ranked would never be selected."""
    assert MEASURED_SCALES <= set(SCALE_LADDER)


@pytest.mark.parametrize("items", [
    None, [], [None], [5], ["plate_reference"], [object()],
    [_Item(None)], [_Item(7)], [_Item("")], [_Item([])],
])
def test_junk_never_claims_a_measurement(items):
    """Everything here arrives from a model response. An empty or malformed
    item list must report "not measured" rather than raising or, far worse,
    quietly vouching."""
    source, measured = scale_summary(items)
    assert measured is False
    assert source is None or isinstance(source, str)


def test_nothing_detected_is_not_measured():
    assert scale_summary([]) == (None, False)
