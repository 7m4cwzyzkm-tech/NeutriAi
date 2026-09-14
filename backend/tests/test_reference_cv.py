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
    MIN_SETTING_CONSENSUS, REFERENCE_RECTANGLES, _find_on_channel, find_reference,
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


# ---------------------------------------------------------------------------
# Chroma first, grey as the fallback.
#
# Grey edges found the card on 5 of 28 bench photographs: on the wood table the
# card and the table are near-isoluminant. These scenes pin the three cases the
# channel order exists for.
# ---------------------------------------------------------------------------
def _coloured_scene(bg_rgb, card_rgb, blobs=None, card=True, width=900, height=1200):
    rng = np.random.default_rng(11)
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:] = bg_rgb
    # Texture in the background's own colour family, so edges exist to trip on.
    for _ in range(120):
        cx, cy = int(rng.integers(0, width)), int(rng.integers(0, height))
        jitter = rng.integers(-12, 13)
        col = tuple(int(np.clip(v + jitter, 0, 255)) for v in bg_rgb)
        cv2.circle(img, (cx, cy), int(rng.integers(15, 60)), col, -1)
    for colour in blobs or []:
        for _ in range(6):
            cx, cy = int(rng.integers(80, width - 80)), int(rng.integers(80, height - 80))
            cv2.circle(img, (cx, cy), int(rng.integers(30, 70)), colour, -1)
    if card:
        L, S = 300.0, 300.0 / (CARD_MM_LONG / CARD_MM_SHORT)
        cx, cy = width / 2.0, height / 2.0
        pts = np.array([[cx - L / 2, cy - S / 2], [cx + L / 2, cy - S / 2],
                        [cx + L / 2, cy + S / 2], [cx - L / 2, cy + S / 2]], dtype=np.int32)
        cv2.fillPoly(img, [pts], card_rgb)
    return Image.fromarray(img)


def _grey_luminance(rgb):
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def test_a_card_as_bright_as_a_wood_table_is_found_by_its_colour():
    """The bench failure: a green card on orange-brown wood, near-isoluminant."""
    wood, green = (140, 98, 58), (55, 130, 70)
    assert abs(_grey_luminance(wood) - _grey_luminance(green)) < 8
    scene = _coloured_scene(wood, green)
    grey = cv2.cvtColor(np.asarray(scene), cv2.COLOR_RGB2GRAY)
    assert _find_on_channel(grey) is None, "the scene must defeat the grey channel"
    found = find_reference(scene)
    assert found is not None
    assert found.mm_per_px == pytest.approx(CARD_MM_LONG / 300.0, rel=0.04)


def test_a_grey_card_on_a_grey_table_falls_back_to_grey_edges():
    """Chroma sees no edge between two neutrals; the grey channel must still work."""
    found = find_reference(_coloured_scene((190, 190, 190), (60, 60, 60)))
    assert found is not None
    assert found.mm_per_px == pytest.approx(CARD_MM_LONG / 300.0, rel=0.04)


def test_colourful_food_with_no_card_is_not_a_card():
    """Chroma makes coloured food visible too: round blobs must not become a card."""
    scene = _coloured_scene((140, 98, 58), None, blobs=[(60, 140, 60), (220, 120, 40), (200, 40, 40)],
                            card=False)
    assert find_reference(scene) is None
