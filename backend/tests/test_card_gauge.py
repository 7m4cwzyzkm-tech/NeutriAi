"""The 12-inch card gauge (backend/app/services/ai/card_gauge.py) -- a
camera-screen GUIDE, never the measurement. These tests guard the pure trig
and the state machine, and that the gauge cannot reach a gram estimate.

Offline only: no model, no network, no photo. Pure functions and one
monkeypatched-in-spirit check that the estimator ignores this module
entirely (it never imports it).
"""
from __future__ import annotations

import math

import pytest

from app.services.ai.card_gauge import (
    CARD_LONG_MM,
    GAUGE_TILT_LIMIT_DEG,
    GAUGE_WIDTH_TOLERANCE_FRAC,
    TARGET_DISTANCE_MM,
    card_layout,
    distance_error_pct,
    distance_from_card,
    distance_from_card_portrait,
    expected_card_px,
    expected_card_px_portrait,
    frame_ground_coverage_mm,
    gauge_state,
)
from app.services.ai.portion import DEFAULT_CAMERA_FOV_DEG, GeometryHint, mm2_per_frame
from app.services.ai.reference_cv import REFERENCE_RECTANGLES


def test_expected_card_px_and_distance_from_card_are_exact_inverses():
    """A round trip through the forward and inverse formula must return the
    distance it started from -- if these ever drift apart (a sign flip, a
    swapped numerator), the gauge would report a distance that does not
    match what it just displayed as expected."""
    for width_px, fov, distance in [(1080, 68.0, 304.8), (720, 60.0, 200.0), (4032, 78.0, 500.0)]:
        px = expected_card_px(width_px, fov, distance)
        recovered = distance_from_card(px, width_px, fov)
        assert recovered == pytest.approx(distance, rel=0.005), (
            f"round trip drifted more than 0.5% at width={width_px} fov={fov} d={distance}")


def test_doubling_distance_halves_the_expected_card_width():
    """card_px is linear in 1/distance -- the gauge's whole premise (a fixed
    on-screen size means a fixed distance) fails if this is not exact."""
    near = expected_card_px(1080, 68.0, 200.0)
    far = expected_card_px(1080, 68.0, 400.0)
    assert far == pytest.approx(near / 2.0, rel=1e-9)


def test_a_card_five_percent_too_wide_reads_too_close():
    """Bigger on screen than expected means the phone is nearer than the
    target -- 'too close', not 'too far'. Getting this backwards would tell
    a user to move the wrong direction."""
    expected = expected_card_px(1080, 68.0)
    assert gauge_state(expected * 1.06, expected, tilt_deg=0.0) == "too_close"


def test_a_card_five_percent_too_narrow_reads_too_far():
    expected = expected_card_px(1080, 68.0)
    assert gauge_state(expected * 0.94, expected, tilt_deg=0.0) == "too_far"


@pytest.mark.parametrize("ratio", [1.0, 1.0 + GAUGE_WIDTH_TOLERANCE_FRAC - 1e-6,
                                    1.0 - GAUGE_WIDTH_TOLERANCE_FRAC + 1e-6])
def test_widths_inside_tolerance_read_ok(ratio):
    """Exactly at the boundary and dead-on both count as ok -- a gauge that
    rejects the centre of its own tolerance band is not usable."""
    expected = expected_card_px(1080, 68.0)
    assert gauge_state(expected * ratio, expected, tilt_deg=0.0) == "ok"


def test_tilt_above_the_limit_reads_tilted_even_with_the_right_size():
    """A correctly-sized but tilted card must not read 'ok': tilt
    foreshortens the card on camera, which is exactly what corrupts the
    width reading this same gauge relies on."""
    expected = expected_card_px(1080, 68.0)
    assert gauge_state(expected, expected, tilt_deg=GAUGE_TILT_LIMIT_DEG) == "tilted"
    assert gauge_state(expected, expected, tilt_deg=GAUGE_TILT_LIMIT_DEG + 10) == "tilted"


def test_tilt_is_checked_before_size_when_both_are_off():
    """Documented precedence: a tilt problem is surfaced even when the
    (unreliable, because tilted) width also looks out of range, since
    releveling the phone is the more fundamental fix."""
    expected = expected_card_px(1080, 68.0)
    assert gauge_state(expected * 1.5, expected, tilt_deg=45.0) == "tilted"


def test_distance_error_pct_is_a_plain_signed_percentage():
    """For logging only -- no rounding surprises, no absolute value hiding
    direction (farther vs nearer must stay distinguishable in the log)."""
    assert distance_error_pct(TARGET_DISTANCE_MM) == pytest.approx(0.0)
    assert distance_error_pct(TARGET_DISTANCE_MM * 1.1) == pytest.approx(10.0)
    assert distance_error_pct(TARGET_DISTANCE_MM * 0.9) == pytest.approx(-10.0)


def test_the_five_percent_width_tolerance_is_about_five_percent_distance_and_ten_percent_area():
    """The docstring's own claim, checked as arithmetic: card_px ~ 1/distance,
    so a +/-5% width band is a +/-~5% distance band, and since area (what
    actually reaches a gram estimate) goes as width squared, the same band is
    about +/-10% on area."""
    lo, hi = 1.0 - GAUGE_WIDTH_TOLERANCE_FRAC, 1.0 + GAUGE_WIDTH_TOLERANCE_FRAC
    # width ratio -> implied distance ratio is the reciprocal (card_px ~ 1/D)
    distance_ratio_far = 1.0 / lo    # reading 5% narrow implies ~5% farther
    distance_ratio_near = 1.0 / hi   # reading 5% wide implies ~5% nearer
    assert (distance_ratio_far - 1.0) * 100 == pytest.approx(5.26, abs=0.1)
    assert (1.0 - distance_ratio_near) * 100 == pytest.approx(4.76, abs=0.1)
    assert (hi ** 2 - 1.0) * 100 == pytest.approx(10.25, abs=0.01)
    assert (1.0 - lo ** 2) * 100 == pytest.approx(9.75, abs=0.01)


def test_the_gauge_never_changes_an_estimate():
    """The gauge's whole design constraint, checked behaviourally: calling
    every gauge function first must not change what mm2_per_frame returns
    for the same hint. If the gauge ever started influencing the scale, this
    is the test that would catch it."""
    hint = GeometryHint(reference_frame_width_mm=320.0, aspect_ratio=0.75, vessel="paper")
    baseline, _ = mm2_per_frame(hint)

    # Exercise every gauge function first, with the same hint's own numbers.
    expected = expected_card_px(1080, 68.0)
    distance_from_card(expected, 1080, 68.0)
    gauge_state(expected, expected, tilt_deg=2.0)
    distance_error_pct(310.0)
    frame_ground_coverage_mm()
    card_layout(279.4)

    after, method = mm2_per_frame(hint)
    assert after == baseline
    assert method == "reference_object"


def test_card_layout_recommends_above_or_below_never_beside():
    """The task's own conclusion, checked against real geometry: an 11-inch
    plate and the card both fit stacked along the frame's long (height)
    axis, at the example lens/distance the module's docstring uses."""
    layout = card_layout(plate_diameter_mm=279.4, fov_deg=69.0, aspect_ratio=3.0 / 4.0)
    assert layout.card_side == "above_or_below"
    assert layout.vertical_margin_mm > 0


def test_card_layout_says_so_when_the_plate_does_not_fit():
    """A plate wider than the frame's own width axis cannot be rescued by
    stacking the card differently -- the function must say so plainly
    rather than silently returning a placement that cannot work."""
    layout = card_layout(plate_diameter_mm=1000.0, fov_deg=69.0, aspect_ratio=3.0 / 4.0)
    assert layout.card_side == "does_not_fit_width"


def test_frame_ground_coverage_matches_mm2_per_frames_own_formula():
    """Not a second geometry model: the long axis here must equal
    mm2_per_frame's own `2 * D * tan(fov / 2)` exactly, for the same inputs."""
    fov, distance = 68.0, 304.8
    coverage = frame_ground_coverage_mm(fov_deg=fov, aspect_ratio=0.75, distance_mm=distance)
    expected_long = 2.0 * distance * math.tan(math.radians(fov) / 2.0)
    assert coverage.long_axis_mm == pytest.approx(expected_long, rel=1e-9)
    assert coverage.short_axis_mm == pytest.approx(expected_long * 0.75, rel=1e-9)


def test_the_card_constant_is_the_same_object_the_detector_uses():
    """Wiring: CARD_LONG_MM must trace back to reference_cv's own
    REFERENCE_RECTANGLES, not a second 85.60 literal that could silently
    drift from the real detector's card size."""
    assert CARD_LONG_MM == REFERENCE_RECTANGLES["credit_card"][0]


def test_the_fov_default_is_the_same_object_the_estimator_uses():
    """Wiring: the gauge's own default FOV, used nowhere by name in this
    file's tests above but exercised via card_layout's default parameter,
    must be portion.py's own constant."""
    from app.services.ai.card_gauge import card_layout
    import inspect
    default_fov = inspect.signature(card_layout).parameters["fov_deg"].default
    assert default_fov is DEFAULT_CAMERA_FOV_DEG


# ---------------------------------------------------------------------------
# expected_card_px_portrait / distance_from_card_portrait -- the fix for the
# axis defect: portion.DEFAULT_CAMERA_FOV_DEG is the sensor's LONG axis, but
# a portrait frame's card guide is drawn on its WIDTH, the SHORT axis.
# ---------------------------------------------------------------------------
def test_expected_card_px_portrait_matches_the_worked_example():
    """A 1080x1440 portrait frame at 68 degrees LONG-axis FOV:
    focal_px = (1440/2)/tan(34deg) ~= 1067.5, card_px ~= 1067.5*85.6/304.8
    ~= 300 px -- not the ~25% smaller value the axis-confused single-axis
    call would give (the next test)."""
    got = expected_card_px_portrait(short_px=1080, long_px=1440, long_fov_deg=68.0)
    assert got == pytest.approx(300.0, abs=1.0)

    focal_px = (1440 / 2.0) / math.tan(math.radians(68.0) / 2.0)
    assert focal_px == pytest.approx(1067.5, abs=0.1)
    assert got == pytest.approx(focal_px * CARD_LONG_MM / TARGET_DISTANCE_MM, rel=1e-9)


def test_using_the_short_axis_with_the_long_axis_fov_is_wrong_by_the_pixel_ratio():
    """The defect itself, pinned as a regression: calling the single-axis
    expected_card_px with the frame's WIDTH (short axis) and a LONG-axis FOV
    -- an easy mistake, since portion.DEFAULT_CAMERA_FOV_DEG IS a long-axis
    value -- disagrees with the correct portrait function by exactly
    short_px / long_px, because focal_px is linear in the pixel count. At
    1080 x 1440 that is a clean 0.75, a 25% miss, not an approximation."""
    correct = expected_card_px_portrait(short_px=1080, long_px=1440, long_fov_deg=68.0)
    axis_confused = expected_card_px(1080, 68.0)  # WRONG: short px, long fov
    assert axis_confused == pytest.approx(correct * (1080.0 / 1440.0), rel=1e-9)
    assert axis_confused == pytest.approx(correct * 0.75, rel=1e-9)
    assert axis_confused < correct


def test_expected_card_px_portrait_and_distance_from_card_portrait_are_exact_inverses():
    for short_px, long_px, fov, distance in [
        (1080, 1440, 68.0, 304.8), (720, 960, 60.0, 200.0), (1170, 2532, 71.0, 350.0),
    ]:
        px = expected_card_px_portrait(short_px, long_px, fov, distance)
        recovered = distance_from_card_portrait(px, short_px, long_px, fov)
        assert recovered == pytest.approx(distance, rel=0.005)


def test_portrait_functions_reject_swapped_short_and_long():
    """short_px larger than long_px is not a valid portrait frame -- almost
    certainly the two arguments swapped, and silently accepting it would
    reproduce the exact axis-confusion bug this function exists to prevent,
    just moved one argument over. Must raise, not guess."""
    with pytest.raises(ValueError):
        expected_card_px_portrait(short_px=1440, long_px=1080, long_fov_deg=68.0)
    with pytest.raises(ValueError):
        distance_from_card_portrait(300.0, short_px=1440, long_px=1080, long_fov_deg=68.0)


def test_the_gauge_still_never_changes_an_estimate_with_the_portrait_functions():
    """Same load-bearing guarantee as the single-axis functions, re-checked
    for the two new ones: calling them first must not move mm2_per_frame's
    output for the same hint."""
    hint = GeometryHint(reference_frame_width_mm=320.0, aspect_ratio=0.75, vessel="paper")
    baseline, _ = mm2_per_frame(hint)

    px = expected_card_px_portrait(1080, 1440, 68.0)
    distance_from_card_portrait(px, 1080, 1440, 68.0)

    after, method = mm2_per_frame(hint)
    assert after == baseline
    assert method == "reference_object"
