"""The card detector, on images whose truth we construct.

These use synthetic scenes rather than photographs on purpose: a test that
needs a JPEG in the repo stops running the moment someone moves the file, and
a scene we build ourselves has an exact answer to check against. The real
photographs are the acceptance test and their numbers are recorded in the
comments in portion.py.
"""
from __future__ import annotations

import math

import cv2
import numpy as np
import pytest
from PIL import Image

from app.services.ai.reference_cv import (
    MIN_SETTING_CONSENSUS, REFERENCE_RECTANGLES, find_reference,
)

CARD_MM_LONG, CARD_MM_SHORT = REFERENCE_RECTANGLES["credit_card"]


def _scene(width=900, height=1200, card_long_px=None, angle_deg=0.0, card=True,
           card_ratio=None, noise=True):
    """A card-ish rectangle on a textured background, built to order."""
    rng = np.random.default_rng(7)
    bg = np.full((height, width, 3), 190, dtype=np.uint8)
    if noise:
        # A patterned tablecloth, so a clean detection is not just luck.
        for _ in range(90):
            cx, cy = rng.integers(0, width), rng.integers(0, height)
            cv2.circle(bg, (int(cx), int(cy)), int(rng.integers(20, 70)),
                       (int(rng.integers(150, 210)),) * 3, -1)

    if card:
        ratio = card_ratio or (CARD_MM_LONG / CARD_MM_SHORT)
        L = card_long_px or 300.0
        S = L / ratio
        t = math.radians(angle_deg)
        cx, cy = width / 2.0, height / 2.0
        corners = []
        for dx, dy in ((-L / 2, -S / 2), (L / 2, -S / 2), (L / 2, S / 2), (-L / 2, S / 2)):
            corners.append([
                cx + dx * math.cos(t) - dy * math.sin(t),
                cy + dx * math.sin(t) + dy * math.cos(t),
            ])
        cv2.fillPoly(bg, [np.array(corners, dtype=np.int32)], (40, 45, 120))

    return Image.fromarray(bg)


def test_it_finds_a_card_and_gets_the_scale_right():
    """A 300 px card in a 900 px frame is 0.333 of the width, so the frame is
    85.60 / 0.333 = 257 mm across. That is arithmetic, not a tolerance to
    negotiate -- a few percent covers the pixel quantisation of the edges."""
    found = find_reference(_scene(card_long_px=300.0))
    assert found is not None
    assert found.kind == "credit_card"
    assert found.length_ratio == pytest.approx(1.0 / 3.0, rel=0.05)
    assert found.frame_width_mm == pytest.approx(CARD_MM_LONG * 3.0, rel=0.05)
    assert found.consensus >= MIN_SETTING_CONSENSUS


@pytest.mark.parametrize("angle", [0, 20, 45, 70, 90])
def test_rotation_does_not_change_the_answer(angle):
    """The card lies flat however it is turned, so the scale must not move.
    An earlier version measured the bounding box instead of the card and got
    this badly wrong -- that failure is what this test is for."""
    found = find_reference(_scene(card_long_px=300.0, angle_deg=angle))
    assert found is not None, angle
    assert found.frame_width_mm == pytest.approx(CARD_MM_LONG * 3.0, rel=0.08), angle


def test_no_card_means_no_answer():
    """The common case. Most photos have no reference, and inventing one sets
    the scale for the whole meal -- measured at worse than having none."""
    assert find_reference(_scene(card=False)) is None


@pytest.mark.parametrize("ratio", [1.0, 1.05, 3.0, 4.0])
def test_a_rectangle_of_the_wrong_shape_is_not_a_card(ratio):
    """Tables, napkins, place mats, phone screens and takeout lids are all
    rectangles. Only 1.586 is a credit card."""
    assert find_reference(_scene(card_long_px=300.0, card_ratio=ratio)) is None


def test_a_card_too_small_to_measure_is_ignored():
    """Below a couple of thousand pixels there are not enough edge samples to
    fit a rectangle, and a length read short inflates the frame AREA by its
    square. Refusing is cheaper than being confidently wrong."""
    assert find_reference(_scene(card_long_px=25.0)) is None


def test_a_detector_failure_never_takes_a_scan_down():
    """No card is always a valid answer, so anything unexpected in here must
    come back as None rather than as a 500 on a photo that was fine."""
    class NotAnImage:
        size = (10, 10)

        def convert(self, _mode):
            raise RuntimeError("decode exploded")

    assert find_reference(NotAnImage()) is None
