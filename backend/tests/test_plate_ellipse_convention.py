"""The plate ellipse convention, pinned on REAL model responses.

What the model does, measured on 26 bench photos (MEASURED-HEIGHT-NOTES.md
section 4): it reports `plate_ellipse` w and h BOTH as fractions of the image's
LONG side, portrait and landscape alike. Every consumer -- `GeometryHint.tilt_deg`
and `scale_learning.observe_width_mm` -- reads them as per-axis fractions (w of
the width, h of the height). So a round plate shot straight down parsed as 41.4
degrees on every 3:4 photo (arccos 0.75), and scale learning multiplied a
portrait frame's SHORT side by a long-side fraction: x0.75 on every width.

`test_tilt_is_measured_in_one_set_of_units` passed throughout, because it fed
invented per-axis numbers. These fixtures are the cached gpt-4o responses for
five bench photos, with the scan path's decode size and the Hough rim diameter
(a size only -- a Hough circle is round by construction). Every bench photo was
shot top-down.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

import pytest

from app.services.ai import vision
from app.services.ai.portion import MIN_TILT_DEG, GeometryHint

FIXTURE = Path(__file__).parent / "fixtures" / "plate_ellipse_recorded.json"
PHOTOS = json.loads(FIXTURE.read_text(encoding="utf-8"))["photos"]
GRID_STEP = 0.05


def _normalised(p):
    return vision._plate_ellipse({"plate_ellipse": p["plate_ellipse"]}, p["width"] / p["height"])


def test_fixture_covers_both_orientations():
    assert {p["orientation"] for p in PHOTOS} == {"portrait", "landscape"}


@pytest.mark.parametrize("p", PHOTOS, ids=[p["photo"] for p in PHOTOS])
def test_a_top_down_round_plate_reads_square_in_pixels(p):
    w_axis, h_axis = _normalised(p)
    long_side = max(p["width"], p["height"])
    assert abs(w_axis * p["width"] - h_axis * p["height"]) <= GRID_STEP * long_side


@pytest.mark.parametrize("p", PHOTOS, ids=[p["photo"] for p in PHOTOS])
def test_a_top_down_photo_reads_near_zero_tilt(p):
    hint = GeometryHint(plate_ellipse_wh=_normalised(p), aspect_ratio=p["width"] / p["height"],
                        vessel_shape=p["container_shape"])
    assert hint.tilt_deg is not None
    assert hint.tilt_deg < MIN_TILT_DEG, f"{p['photo']} read {hint.tilt_deg:.1f} deg"


def test_portrait_and_landscape_widths_agree():
    """No x0.75 split: the normalised width over the rim's pixel share of the frame
    width must sit nearer the same value in both orientations than 0.75 apart."""
    def ratio(p):
        return _normalised(p)[0] / (p["hough_rim_diameter_px"] / p["width"])

    portrait = statistics.median(ratio(p) for p in PHOTOS if p["orientation"] == "portrait")
    landscape = statistics.median(ratio(p) for p in PHOTOS if p["orientation"] == "landscape")
    r = portrait / landscape
    assert abs(math.log(r)) < abs(math.log(r / 0.75)), (
        f"portrait/landscape width ratio {r:.3f} is nearer the 0.75 split than parity")


def test_no_frame_shape_means_no_reading():
    assert vision._plate_ellipse({"plate_ellipse": {"w": 0.5, "h": 0.5}}, None) is None


def test_the_parse_boundary_converts_long_side_fractions_to_per_axis():
    # portrait 3:4 -- w is widened into width units, h is already height units
    assert vision._plate_ellipse({"plate_ellipse": {"w": 0.6, "h": 0.6}}, 0.75) == pytest.approx((0.8, 0.6))
    # landscape 4:3 -- w is already width units, h is widened into height units
    assert vision._plate_ellipse({"plate_ellipse": [0.6, 0.6]}, 4 / 3) == pytest.approx((0.6, 0.8))
