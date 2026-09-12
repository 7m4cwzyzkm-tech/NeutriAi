"""The gate that decides whether to believe a segmenter.

Written before SAM2 arrives, on purpose. The gate is where the engineering is
-- a segmenter that is right most of the time still needs something deciding
WHICH times -- and every threshold here came from a failure the colour rule
actually produced, so they carry over to SAM2 rather than being discarded with
the thing that motivated them.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.services.ai import segmenter as S


def _seg(area_ratio: float, score: float | None = None, side: int = 100):
    """A mask covering `area_ratio` of the frame."""
    mask = np.zeros((side, side), dtype=bool)
    n = int(round(area_ratio * side * side))
    mask.flat[:n] = True
    return S.Segmentation(mask=mask, score=score, source="fake")


def test_a_plate_with_one_unmeasured_item_publishes_nothing():
    """The photo-06 failure. Two seeds failed, and the rice was published as
    100% of a plate it was about a third of -- the missing food lands on
    whichever items worked, which are the ones that looked fine."""
    good = [_seg(0.04), _seg(0.05), _seg(0.03)]
    assert S.shares(good) is not None
    assert S.shares([_seg(0.04), None, _seg(0.03)]) is None
    assert S.shares([None, None, None]) is None
    assert S.shares([]) is None


def test_a_mask_that_found_almost_nothing_is_refused():
    """When the rim sample landed on food instead of the plate, the mask
    inverted and returned under 2% of the plate. That failure shipped a 146 g
    drumstick as 12 g, and it did it silently."""
    assert S.shares([_seg(0.05), _seg(0.0001), _seg(0.04)]) is None


def test_a_mask_that_swallowed_the_plate_is_refused():
    assert S.shares([_seg(0.70), _seg(0.04)]) is None


def test_a_segmenter_that_reports_low_confidence_is_believed_about_that():
    assert S.shares([_seg(0.05, score=0.9), _seg(0.04, score=0.8)]) is not None
    assert S.shares([_seg(0.05, score=0.9), _seg(0.04, score=0.2)]) is None


def test_a_segmenter_that_cannot_report_confidence_is_not_punished_for_it():
    """SAM2 scores its masks; the colour rule cannot. Absent is not the same as
    low, and treating them alike would reject every mask from a segmenter whose
    only fault is not offering a number."""
    assert S.shares([_seg(0.05, score=None), _seg(0.04, score=None)]) is not None


def test_the_shares_are_shares():
    got = S.shares([_seg(0.06), _seg(0.03), _seg(0.03)])
    assert got is not None
    assert sum(got) == pytest.approx(1.0, abs=0.005)
    assert got[0] == pytest.approx(0.5, abs=0.01)


def test_the_default_segmenter_measures_nothing_and_says_so():
    """A missing dependency must cost a measurement, never a scan. The app has
    to behave identically when no segmenter is configured."""
    n = S.NullSegmenter()
    assert n.available() is False
    assert n.segment(np.zeros((10, 10, 3), dtype=np.uint8),
                     [(0.5, 0.5), (0.2, 0.2)]) == [None, None]
    assert S.shares(n.segment(np.zeros((10, 10, 3), dtype=np.uint8),
                              [(0.5, 0.5)])) is None


def test_the_interface_takes_points_not_boxes():
    """Deliberate, and the reason is measured. The model's box is quantised to a
    0.05 grid -- 82% of values sit exactly on it where chance would put 20% --
    and at 3% of frame one grid step is 45% of the food's weight. The box CENTRE
    does not carry that error. Passing SAM2 a box would feed the quantisation
    straight back into the thing meant to escape it.
    """
    import inspect
    sig = inspect.signature(S.Segmenter.segment)
    assert "points" in sig.parameters
    assert "box" not in sig.parameters and "bbox" not in sig.parameters
    assert "points" in inspect.signature(S.NullSegmenter.segment).parameters
