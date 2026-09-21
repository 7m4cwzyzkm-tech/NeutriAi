"""The 12-inch card gauge — a camera-screen GUIDE, not a measurement.

WHAT THIS IS FOR

`ScanScreen.tsx` already draws a fixed "card here" box (132 x 83 points,
hardcoded) so the user lays a bank card flat beside the plate. Fixed pixel
dimensions only match one phone at one distance from the screen; every other
device sees a box the wrong size for its own field of view. This module
computes that box from the phone's actual optics instead, so the same guide
works on every device, and gives the trial-photo bookkeeping (how far off 12
inches was this shot, is the phone level) that goes with it.

WHAT THIS IS NOT

The scale that feeds a gram estimate always comes from the card actually
FOUND in the photo, by `reference_cv.find_reference` and
`portion.mm2_per_frame` — never from this outline, and never from the 12-inch
target. A user who ignores the guide and shoots from 9 inches, or 20, still
gets a correctly-scaled estimate, because the real detector measures whatever
card is actually in the frame at whatever distance it actually was. This
module only helps the user land closer to a consistent, repeatable shot
before that detector ever runs. Nothing here multiplies a gram, an area, or
an mm² value — every function returns a distance, a pixel count, a state
label, or a percentage for a log line.

REUSED, NOT DUPLICATED

- Card size: `reference_cv.REFERENCE_RECTANGLES["credit_card"]` — the same
  ISO/IEC 7810 ID-1 tuple `find_reference` and `scale_audit.py` already use.
  Not re-typed as a second literal.
- Default field of view: `portion.DEFAULT_CAMERA_FOV_DEG` (68.0) — the same
  value `mm2_per_frame`'s depth rung falls back to when a phone does not
  report its own. Not a second default.

A NOTE ON WHICH AXIS

`portion.py`'s own comment on `DEFAULT_CAMERA_FOV_DEG` is explicit: "the
quoted field of view spans the LONG axis of the sensor, not the image
width. Those are the same thing only in landscape." `expected_card_px` and
`distance_from_card` below are deliberately axis-agnostic pinhole trig: pass
`image_width_px` and `fov_deg` for the SAME axis and the formula is exact,
whichever axis that is. `ScanScreen.tsx`'s existing card box is
132 x 83 points — width:height = 1.590, matching the card's own long:short
ratio (85.60 / 53.98 = 1.586) to within rounding — which is the evidence
used here to fix the pairing for the gauge specifically: the card lies with
its LONG edge horizontal, so `CARD_WIDTH_MM` below is the card's long edge,
compared against the frame's WIDTH axis (the sensor's SHORT physical axis in
portrait). `card_layout`, which needs both frame axes at once, gets the
long/short split right explicitly rather than leaning on that pairing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .portion import DEFAULT_ASPECT_RATIO, DEFAULT_CAMERA_FOV_DEG
from .reference_cv import REFERENCE_RECTANGLES

# The card's long and short edges, read from the SAME tuple find_reference
# uses — never re-typed as a literal 85.60 / 53.98 a second time.
CARD_LONG_MM, CARD_SHORT_MM = REFERENCE_RECTANGLES["credit_card"]

# The gauge's target distance: 12 inches, exactly. Not a prior — a fixed
# instruction to the user, so every trial photo is comparable to every other.
TARGET_DISTANCE_MM = 12.0 * 25.4  # 304.8

# +/-5% on the measured card WIDTH. Because card_px is linear in 1/distance
# (see expected_card_px), a +/-5% width reading is a photo taken at roughly
# +/-5% of the target distance (1/1.05 = 0.952, 1/0.95 = 1.053 -- close
# enough to "about 5%" that the asymmetry is not worth a second constant).
# Area, which is what actually reaches a gram estimate through mm2_per_frame,
# goes as the SQUARE of that: 1.05**2 - 1 = +10.25%, 1 - 0.95**2 = -9.75% --
# "about +/-10%" on area for a card this far out of tolerance.
GAUGE_WIDTH_TOLERANCE_FRAC = 0.05

# Straight down, +/-5 degrees. Matches the tilt this codebase already treats
# as "still usable" elsewhere (portion.GeometryHint.tilt_deg's own
# derivation assumes near-vertical; a card tilted more than this both
# foreshortens on camera, which corrupts the width reading this gauge itself
# depends on, and starts to violate the "straight down" precondition the
# 12-inch distance is measured under in the first place).
GAUGE_TILT_LIMIT_DEG = 5.0

GaugeState = Literal["too_far", "too_close", "tilted", "ok"]


def expected_card_px(image_width_px: float, fov_deg: float,
                      distance_mm: float = TARGET_DISTANCE_MM) -> float:
    """The card's expected pixel width, straight down, at `distance_mm`.

        focal_px = (image_width_px / 2) / tan(fov_deg / 2)
        card_px  = focal_px * CARD_LONG_MM / distance_mm

    `image_width_px` and `fov_deg` must describe the SAME axis (see the
    module docstring) -- this function does not know or care which one, it
    is exact for either as long as they agree.
    """
    focal_px = (image_width_px / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    return focal_px * CARD_LONG_MM / distance_mm


def distance_from_card(card_px: float, image_width_px: float, fov_deg: float) -> float:
    """The inverse of `expected_card_px`: given the card's MEASURED pixel
    width in an actual photo, the camera distance that photo was taken at.

    This is what feeds the estimate -- `mm2_per_frame`'s depth rung takes a
    `camera_distance_mm`, and for a card-rung photo this is how a client
    could report one from the SAME card the scale itself is measured from,
    rather than from the gauge's 12-inch target.
    """
    focal_px = (image_width_px / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    return focal_px * CARD_LONG_MM / card_px


def distance_error_pct(actual_mm: float, target_mm: float = TARGET_DISTANCE_MM) -> float:
    """How far a trial photo's actual distance departed from the 12-inch
    target, as a signed percentage. Positive: farther than 12 inches.

    For LOGGING a trial photo's scan metadata only -- a plain number beside
    whatever else the scan records. Never a reason to reject or re-scale an
    estimate: the real scale still comes from the card actually found in
    that photo, at whatever distance it actually was.
    """
    return (actual_mm / target_mm - 1.0) * 100.0


def gauge_state(measured_card_px: float, expected_px: float, tilt_deg: float) -> GaugeState:
    """One of "too_far", "too_close", "tilted", "ok" -- for the on-screen
    guide's live colour/label, nothing else.

    "ok" only when the measured width is within GAUGE_WIDTH_TOLERANCE_FRAC of
    expected AND tilt_deg is under GAUGE_TILT_LIMIT_DEG. Tilt is checked
    first: a tilted card foreshortens on camera, which corrupts the width
    reading this same gauge depends on, so a tilt problem is the more
    fundamental one to surface even when the (now-unreliable) width also
    happens to look out of range.
    """
    if tilt_deg >= GAUGE_TILT_LIMIT_DEG:
        return "tilted"
    ratio = measured_card_px / expected_px
    if ratio < 1.0 - GAUGE_WIDTH_TOLERANCE_FRAC:
        return "too_far"        # the card looks smaller than expected
    if ratio > 1.0 + GAUGE_WIDTH_TOLERANCE_FRAC:
        return "too_close"      # the card looks bigger than expected
    return "ok"


@dataclass(frozen=True)
class FrameCoverage:
    """The ground the camera frame actually covers, in millimetres, at a
    given distance -- portion.mm2_per_frame's own long-axis-then-aspect
    derivation (its depth rung), reused here rather than re-derived, since
    this module's job is the same trigonometry for a different purpose."""
    distance_mm: float
    fov_deg: float
    aspect_ratio: float           # frame width / height, portrait < 1.0
    long_axis_mm: float           # portrait: the frame's HEIGHT
    short_axis_mm: float          # portrait: the frame's WIDTH


def frame_ground_coverage_mm(fov_deg: float = DEFAULT_CAMERA_FOV_DEG,
                              aspect_ratio: float = 1.0 / DEFAULT_ASPECT_RATIO,
                              distance_mm: float = TARGET_DISTANCE_MM) -> FrameCoverage:
    """How much table the frame covers at `distance_mm`, given the LONG-axis
    FOV and the frame's aspect ratio (width / height) -- exactly
    `mm2_per_frame`'s depth rung: `long_side = 2 * D * tan(fov / 2)`, then the
    short axis via the aspect ratio, not a second trig pass.

    `aspect_ratio` defaults to the reciprocal of `DEFAULT_ASPECT_RATIO`
    (portion.py's own 4:3 constant, expressed portrait side up) rather than a
    second 0.75 literal -- this module's default frame is portrait, where
    that file's own default describes an unknown-orientation landscape
    fallback used for a different reason.
    """
    long_axis_mm = 2.0 * distance_mm * math.tan(math.radians(fov_deg) / 2.0)
    if aspect_ratio >= 1.0:
        short_axis_mm = long_axis_mm / aspect_ratio
    else:
        short_axis_mm = long_axis_mm * aspect_ratio
    return FrameCoverage(distance_mm, fov_deg, aspect_ratio, long_axis_mm, short_axis_mm)


CardSide = Literal["above_or_below", "insufficient_height", "does_not_fit_width"]


@dataclass(frozen=True)
class CardLayout:
    coverage: FrameCoverage
    plate_diameter_mm: float
    card_side: CardSide
    vertical_margin_mm: float     # spare room along the long (height) axis


def card_layout(plate_diameter_mm: float,
                 fov_deg: float = DEFAULT_CAMERA_FOV_DEG,
                 aspect_ratio: float = 1.0 / DEFAULT_ASPECT_RATIO,
                 distance_mm: float = TARGET_DISTANCE_MM) -> CardLayout:
    """Where the card should sit relative to the plate, for a portrait frame
    at `distance_mm`: ABOVE OR BELOW, never beside it.

    Beside is ruled out on the numbers, not just by style: the plate already
    needs most of the frame's WIDTH (the short axis), which is the tighter of
    the two dimensions in portrait, so there is no width budget left for the
    card next to it. The frame's HEIGHT (the long axis, where the FOV is
    measured directly with no aspect conversion needed) is the larger
    dimension in portrait and is where the card's own short edge -- it lies
    with its long edge horizontal, matching the existing on-screen guide,
    see the module docstring -- has to fit alongside the plate's own
    diameter.

    EXAMPLE, for a 26 mm-equivalent main lens (~69 deg horizontal / ~53 deg
    vertical on a 4:3 sensor -- the vertical figure is not used directly, see
    below) at 12 inches, an 11-inch plate:

        frame coverage   long axis (height) ~419 mm, short axis (width) ~314 mm
        plate             279.4 mm (11 in) -- fits the 314 mm width, ~35 mm spare
        plate + card      279.4 + 53.98 = 333.4 mm needed along the height axis
        margin            419 - 333.4 ~= 86 mm of headroom for the card, above
                          or below the plate

    These are computed from the LINEAR long-axis-then-aspect approximation
    `mm2_per_frame` itself uses (not the more exact tan(vFOV/2) relationship;
    69/53 deg cross-checks it to within about a degree, close enough that
    duplicating the estimator's own model was preferred over a second one).
    Gil: check this against a real photo before trusting the exact margin --
    this only says the two objects are predicted to fit, and by how much.
    """
    coverage = frame_ground_coverage_mm(fov_deg, aspect_ratio, distance_mm)
    if plate_diameter_mm > coverage.short_axis_mm:
        return CardLayout(coverage, plate_diameter_mm, "does_not_fit_width",
                           coverage.short_axis_mm - plate_diameter_mm)
    needed_height_mm = plate_diameter_mm + CARD_SHORT_MM
    margin = coverage.long_axis_mm - needed_height_mm
    side: CardSide = "above_or_below" if margin >= 0 else "insufficient_height"
    return CardLayout(coverage, plate_diameter_mm, side, margin)
