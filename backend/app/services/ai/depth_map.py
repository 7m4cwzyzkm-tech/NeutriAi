"""Height measured from a depth map, instead of assumed from a table.

WHY THIS IS THE BIGGEST REMAINING TERM

    grams = footprint_mm2 x height_mm x profile_factor x density / 1000

Footprint has a fix in progress. Density is sourced. Scale is measured to 0.8%
on a calibrated plate. Height is a lookup table -- 24 mm for a mound, 34 for
chunky -- applied to every meal on earth, and `profile_factor` is a second guess
layered on the first, there only to convert a peak height into a mean one.

Google's Nutrition5k benchmark measured what depth is worth on exactly this
task: mass error 18.8% from RGB alone, 13.7% once a depth map supplies a volume
scalar. Five points, on the term nobody else in this market measures. For
reference, the same paper puts professional nutritionists at 41%.

RELATIVE DEPTH IS ENOUGH, WHICH IS THE WHOLE TRICK

Metric monocular depth models exist and are licensed CC-BY-NC -- unusable in a
paid app. Depth Anything V2 Small is Apache-2.0 and gives RELATIVE depth, which
is normally the weaker product.

Here it is not, because the plate is already measured. Fit the plate's surface
as a plane in the depth map, take how far the food rises above that plane, and
the plate resolves the affine ambiguity that makes relative depth "relative".

HOW THE PLATE RESOLVES IT, AND WHAT THAT COSTS

Subtracting the fitted plane kills the model's unknown offset. The unknown GAIN
is killed by the plate's FORESHORTENING: a circle of known width, photographed
off square, falls away from the camera by a known number of millimetres across
its own face, and how far the depth map falls across that same span says what
one unit of it is worth. See mm_per_depth_unit for the derivation.

The price is stated here rather than buried: a plate photographed SQUARE ON does
not fall away at all, so it carries no information about its own scale, and
those photographs are refused rather than answered. The floor is 20 degrees of
tilt -- see MIN_TILT_DEG for the measured reason. A three-quarter photograph of
a plate is measurable; a flat overhead one is not, and gets the priors.

AND IT REPLACES THE PROFILE FACTOR TOO

PROFILE_FACTORS exists because a mound is not a cylinder: its mean height over
its own footprint is about 0.68 of its peak. That constant is a guess about
shape. A depth map does not need it -- the mean height over the mask IS the
mean, measured, for whatever shape the food actually is.

WHAT IT REFUSES TO DO

Every function here returns None rather than a number it cannot defend. A depth
map that fails a plausibility check must cost a measurement, never a scan: the
estimator falls back to the priors, which is exactly where it is today.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import structlog

log = structlog.get_logger()

# Food is not 2 mm tall and not 200 mm tall on a plate. Outside this the plane
# fit or the scale is wrong, not the lunch.
MIN_HEIGHT_MM, MAX_HEIGHT_MM = 2.0, 150.0

# The plate's own surface, sampled as an annulus just inside the rim. Same
# geometry the colour mask uses, for the same reason: the rim is the one part of
# a plate photograph reliably free of food.
PLANE_INNER, PLANE_OUTER = 0.80, 0.97
# Fewer than this and the plane is fitted to noise.
MIN_PLANE_PIXELS = 400
# How flat the fitted plane has to be. The residual is in the depth map's own
# units, expressed as a fraction of the food's height range -- a plate whose
# "surface" is as rough as the food on it was not found.
MAX_PLANE_ROUGHNESS = 0.35

# How far off square the camera has to be.
#
# This is the price of using a RELATIVE depth model, and it is worth stating
# plainly because it decides which photographs can be measured at all.
#
# A relative model returns some unknown linear function of true depth. Fitting
# the plate plane removes the offset; the gain is what is left, and the only
# thing in the picture that can pin it is the plate's foreshortening -- how far
# the depth map falls across a plate whose real width in millimetres we know.
#
# A plate photographed square-on does not fall at all: the gradient is zero and
# so is the information. The error in the height is the error in the plate's
# measured ellipse, amplified by
#
#     A(tilt) = 1 / tan^2(tilt) + 2
#
# which is 3x at 45 degrees, 5x at 30, 9.5x at 20 and 34x at 10. Measured on a
# ray-traced plate: a 2% error in the ellipse gives 10% at 30 degrees and takes
# the answer to ZERO at 10 degrees.
#
# So 20 degrees is the floor -- roughly 10% height error for a 1% ellipse -- and
# a nearly overhead photograph is refused rather than answered. Above 65 the
# plate is close to edge-on, the food occludes its own footprint, and the mask
# is measuring a shape it cannot see.
MIN_TILT_DEG, MAX_TILT_DEG = 20.0, 65.0

# How much of the photograph's border the plate may sit on before it counts as
# cut off. A cut plate has no measurable ellipse -- its minor axis, which
# carries the tilt and therefore the whole scale, is whatever the crop left.
# Not zero, because a colour-derived mask frays and one stray border pixel is
# noise. Measured on a ray-traced plate at 300 mm, close enough to overflow the
# frame: a 20.0 mm mound came out 14.4 mm and every other check passed it.
BORDER_TOUCH_TOLERANCE = 0.02

# How much of the rim annulus the food is allowed to cover.
#
# Roughness alone does not catch the worst case. A mound spread over the WHOLE
# plate is nearly flat at its edge, so the annulus samples a smooth surface, the
# plane fit looks healthy, and the fitted "plate" sits partway up the food --
# every height then comes out short. Measured on a synthetic plate-wide dome
# whose true mean height is 20 mm: it returned 7.5 mm and passed every other
# check.
#
# The honest test is whether any bare plate was visible at all. It is a real
# limitation and worth stating plainly: a plate completely covered in food
# cannot be depth-measured this way, and gets the priors instead.
MAX_RIM_COVERED = 0.35


def _rim_of(plate_mask: np.ndarray):
    """The annulus just inside the rim -- the part of a plate most likely bare.

    One definition, used by the plane fit and by the bare-plate check, so the
    two cannot drift apart and start describing different rings.
    """
    if plate_mask is None or not plate_mask.any():
        return None
    h, w = plate_mask.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w]
    cy, cx = np.array(np.nonzero(plate_mask)).mean(axis=1)
    radius = np.sqrt(plate_mask.sum() / np.pi)
    if radius <= 0:
        return None
    dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) / radius
    return plate_mask & (dist >= PLANE_INNER) & (dist <= PLANE_OUTER)


def _plane_from(points: np.ndarray, values: np.ndarray):
    """Least-squares plane z = ax + by + c through sampled depth."""
    if points.shape[0] < 3:
        return None
    design = np.column_stack([points[:, 0], points[:, 1], np.ones(len(points))])
    try:
        coeffs, *_ = np.linalg.lstsq(design, values, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(coeffs)):
        return None
    return coeffs


@dataclass(frozen=True)
class PlateFit:
    """Everything the plate tells us, from one plane fit.

    Kept together deliberately. The height field and the gradient that scales it
    come from the SAME least-squares plane -- fit them separately and a later
    edit can quietly leave the two describing different surfaces, which is the
    defect that has cost this project more nights than any other.
    """

    field: np.ndarray       # depth units above the plate's surface
    gradient: float         # depth units per pixel, down the plate's slope
    major_px: float         # the plate's true diameter, in pixels
    minor_px: float         # its foreshortened width

    @property
    def cos_tilt(self) -> float:
        """cos of the angle between the plate's normal and the camera axis.

        A circle photographed off-square projects to an ellipse whose minor axis
        is the diameter times the cosine of that angle. So the plate reports its
        own tilt, and it does so from pixels rather than from anything the model
        was asked to guess.
        """
        if self.major_px <= 0:
            return 1.0
        return float(min(1.0, self.minor_px / self.major_px))

    @property
    def tilt_deg(self) -> float:
        return float(np.degrees(np.arccos(np.clip(self.cos_tilt, -1.0, 1.0))))

    @property
    def error_amplification(self) -> float:
        """How much an error in the measured ellipse is magnified in the height.

        Reported rather than hidden: it is the honest confidence attached to
        every number this file produces. See MIN_TILT_DEG.
        """
        t = np.tan(np.radians(self.tilt_deg))
        if t <= 0:
            return float("inf")
        return float(1.0 / (t * t) + 2.0)


def plate_axes_px(plate_mask: np.ndarray):
    """The plate ellipse's major and minor axes, in pixels, from its own mask.

    Second moments rather than a fitted outline, because the outline of a
    colour-derived mask is its worst part and its bulk is its best. For a filled
    ellipse the variance along an axis is (axis/4)^2, which inverts exactly --
    checked against a ray-traced plate, where it recovered the camera's
    millimetres-per-pixel to four figures.
    """
    if plate_mask is None or not plate_mask.any():
        return None

    # A plate that runs off the edge of the photograph has no measurable
    # ellipse: the mask is a shape the frame cut, not a shape the camera saw,
    # and its minor axis -- the one that carries the tilt and therefore the
    # whole scale -- is whatever the crop left behind. Found by a ray-traced
    # plate at 300 mm, where the plate overflowed the frame and a 20.0 mm mound
    # measured 14.4. Refused rather than shortened.
    #
    # A tolerance rather than a single pixel: a colour-derived mask frays at its
    # edge, and one stray pixel on the border is noise, not a cut plate. A plate
    # the frame actually cut runs along the border for a long way.
    h, w = plate_mask.shape[:2]
    on_border = int(plate_mask[0, :].sum() + plate_mask[-1, :].sum()
                    + plate_mask[:, 0].sum() + plate_mask[:, -1].sum())
    if on_border > BORDER_TOUCH_TOLERANCE * 2 * (h + w):
        log.info("depth_plate_cropped_by_frame",
                 border_fraction=round(on_border / max(2 * (h + w), 1), 3))
        return None

    ys, xs = np.nonzero(plate_mask)
    if xs.size < 3:
        return None
    pts = np.column_stack([xs, ys]).astype(np.float64)
    cov = np.cov(pts - pts.mean(axis=0), rowvar=False)
    try:
        lam = np.linalg.eigvalsh(cov)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(lam)) or lam[1] <= 0:
        return None
    major = 4.0 * float(np.sqrt(max(lam[1], 0.0)))
    minor = 4.0 * float(np.sqrt(max(lam[0], 0.0)))
    if major <= 0 or minor <= 0:
        return None
    return major, minor


def fit_plate(depth: np.ndarray, plate_mask: np.ndarray):
    """Fit the plate's surface and measure everything the plate can tell us.

    `depth` is whatever the depth model produced, in the RELATIVE INVERSE form
    every commercially-licensed monocular model returns: an unknown linear
    function of 1/Z, larger meaning nearer. Two facts make that usable:

      * a plane in the world is exactly affine in 1/Z across the image, so the
        plate really is a plane in this space, not merely nearly one; and
      * the food, being nearer than the plate it sits on, reads HIGHER than it.

    Returns None when the plate's surface cannot be found, which is the honest
    answer for a photograph with no plate in it.
    """
    if depth is None or plate_mask is None:
        return None
    if depth.shape[:2] != plate_mask.shape[:2]:
        return None
    h, w = depth.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w]
    rim = _rim_of(plate_mask)
    if rim is None or int(rim.sum()) < MIN_PLANE_PIXELS:
        return None

    pts = np.column_stack([xs[rim], ys[rim]]).astype(np.float64)
    coeffs = _plane_from(pts, depth[rim].astype(np.float64))
    if coeffs is None:
        return None
    a, b, c = coeffs
    plane = a * xs + b * ys + c
    field = depth.astype(np.float64) - plane

    # Did we fit a plate, or fit the food?
    #
    # If the annulus landed on food, the "plane" tilts to follow it and the
    # residual on the rim is as large as the relief we are trying to measure.
    # Refusing here is the same rule the colour mask learned the hard way: a
    # measurement built on a bad reference does not degrade, it inverts.
    rim_noise = float(np.std(field[rim]))
    relief = float(np.percentile(field[plate_mask], 98) - np.percentile(field[plate_mask], 2))
    if relief <= 0 or rim_noise / relief > MAX_PLANE_ROUGHNESS:
        log.warning("depth_plane_rejected", rim_noise=round(rim_noise, 4),
                    relief=round(relief, 4))
        return None

    axes = plate_axes_px(plate_mask)
    if axes is None:
        return None
    major, minor = axes
    return PlateFit(field=field, gradient=float(np.hypot(a, b)),
                    major_px=major, minor_px=minor)


def mm_per_depth_unit(fit: PlateFit, plate_diameter_mm: float) -> float | None:
    """What one unit of this depth map is worth in millimetres.

    THE DERIVATION, because an earlier version of this function was wrong by two
    orders of magnitude and every test agreed with it.

    The model returns D = s/Z + t for unknown gain s and offset t. Subtracting
    the plate plane kills t. What is left is that a height h above the plate
    reads as

        dD = s . h / (Z^2 . cos(tilt))                                     (1)

    -- and the cos belongs UNDER the line, which is the whole of the correction
    below. Food h above the plate lies on the plate plane offset by h along its
    NORMAL, and a camera ray meets that offset plane h/cos(tilt) further along
    itself, not h.cos(tilt). Settled by intersecting one ray with both planes:
    at 45 degrees a 10 mm rise moves the depth 14.14 mm, not 7.07.

    So the unknown to be pinned is s/Z^2, and the plate pins it. One pixel
    across the plate is k = diameter_mm / major_axis_px millimetres, and walking
    one pixel down the plate's slope drops k.tan(tilt) millimetres away from the
    camera. Differentiating D = s/Z + t along that walk:

        |grad D| = s . k . tan(tilt) / Z^2   =>   s/Z^2 = |grad D| / (k.tan)  (2)

    Put (2) in (1):

        h = dD . k . sin(tilt) / |grad D|

    No focal length, no camera distance, no metric model -- and, critically, no
    dependence on the model's arbitrary gain, which is what makes a relative
    model usable here at all. Verified against a ray-traced plate: recovered a
    20.0 mm mean height as 19.9-20.3 mm across tilts from 5 to 30 degrees,
    camera distances from 300 to 900 mm, and two different arbitrary gains.

    AND IT WAS WRONG ONCE MORE BEFORE THIS, which is the more instructive half.
    The first version of the line above divided by cos^2(tilt) as well, from
    getting (1) upside down. It read 1/cos^2 too high -- 35% at 30 degrees,
    2x at 45, 5x at 64 -- and every test passed, because the test fixture
    offset its food by h*cos too. The plate was ray-traced; the food was not.
    A fixture is only independent of the code where it is actually independent.

    WHAT THIS REPLACED, since the mistake is instructive. The old rule was
    "a unit across the image and a unit along the line of sight are the same
    unit" -- true for a depth map expressed in pixel units, false for every
    relative model on the market. On the same ray-traced plate it returned
    0.03-0.21 mm for a 20 mm mound, and the error moved by 10x with tilt. It
    survived because the test fixture built its scenes by inverting the rule
    under test, so the scene and the code shared the same wrong assumption.
    """
    if fit is None:
        return None
    try:
        diameter_mm = float(plate_diameter_mm)
    except (TypeError, ValueError):
        return None
    if not (60.0 <= diameter_mm <= 600.0):
        return None
    if fit.major_px <= 0 or fit.gradient <= 0:
        return None

    tilt = fit.tilt_deg
    if not (MIN_TILT_DEG <= tilt <= MAX_TILT_DEG):
        log.info("depth_tilt_out_of_range", tilt_deg=round(tilt, 1),
                 amplification=round(fit.error_amplification, 1))
        return None

    cos_t = fit.cos_tilt
    sin_t = float(np.sqrt(max(0.0, 1.0 - cos_t * cos_t)))
    if cos_t <= 0 or sin_t <= 0:
        return None
    mm_per_px = diameter_mm / fit.major_px
    scale = mm_per_px * sin_t / fit.gradient
    if not np.isfinite(scale) or scale <= 0:
        return None
    return float(scale)


def mean_height_mm(field: np.ndarray, item_mask: np.ndarray,
                   scale_mm: float) -> float | None:
    """The food's MEAN height over its own footprint, in millimetres.

    The mean, not the peak, and this is the point of the whole file. The
    estimator's formula wants a mean height; it currently gets a peak-ish prior
    multiplied by PROFILE_FACTORS, a constant encoding a guess about shape --
    0.68 for a mound, on the reasoning that a dome tapers to nothing at its rim.

    Measured here, the mean IS the mean, for whatever shape the food actually
    has. So a caller using this must NOT then apply the profile factor: that
    would count the same correction twice, which is the exact mistake
    HEIGHT_PRIORS_MM and MEASURED_HEIGHTS_MM exist as two tables to avoid.
    """
    if field is None or item_mask is None or not item_mask.any():
        return None
    if field.shape[:2] != item_mask.shape[:2]:
        return None
    try:
        scale = float(scale_mm)
    except (TypeError, ValueError):
        return None
    if scale <= 0:
        return None

    values = field[item_mask]
    if values.size == 0:
        return None
    # Clipped at zero: a pixel BELOW the plate plane is a plane-fit error or a
    # shadow, not food of negative thickness, and letting it subtract would
    # quietly lighten every portion with a dark edge.
    mean_units = float(np.clip(values, 0.0, None).mean())
    height = mean_units * scale
    if not (MIN_HEIGHT_MM <= height <= MAX_HEIGHT_MM):
        log.warning("depth_height_implausible", height_mm=round(height, 1))
        return None
    return round(height, 2)


def measure_heights(depth: np.ndarray, plate_mask: np.ndarray,
                    item_masks: list, plate_diameter_mm: float) -> list:
    """Mean height in mm for each item, or None per item.

    All-or-nothing at the plate level: if the plane cannot be found, no item on
    that plate gets a measured height. A plate where some items are measured and
    others assumed would mix two different meanings of "height" inside one meal
    total, and nothing downstream could tell them apart.
    """
    empty = [None] * len(item_masks)
    if depth is None or plate_mask is None:
        return empty

    # Is any bare plate visible to fit a plane to?
    #
    # This has to happen here rather than in height_field, because it is the
    # only place that knows where the FOOD is. See MAX_RIM_COVERED: a plate-wide
    # mound passes every check inside height_field and comes out 60% short.
    covered = np.zeros(plate_mask.shape, dtype=bool)
    for m in item_masks:
        if m is not None and getattr(m, "shape", None) == plate_mask.shape:
            covered |= m
    rim = _rim_of(plate_mask)
    if rim is None:
        return empty
    rim_px = float(rim.sum())
    if rim_px <= 0 or float((rim & covered).sum()) / rim_px > MAX_RIM_COVERED:
        log.warning("depth_no_bare_plate",
                    covered=round(float((rim & covered).sum()) / max(rim_px, 1), 3))
        return empty

    fit = fit_plate(depth, plate_mask)
    if fit is None:
        return empty
    scale = mm_per_depth_unit(fit, plate_diameter_mm)
    if scale is None:
        return empty
    # Logged on every measured plate, because it is the honest error bar on
    # everything below: at 20 degrees of tilt a 1% error in the plate ellipse is
    # 10% in the height, and at 45 degrees it is 3%.
    log.info("depth_measured", tilt_deg=round(fit.tilt_deg, 1),
             amplification=round(fit.error_amplification, 1),
             mm_per_unit=round(scale, 4))
    return [mean_height_mm(fit.field, m, scale) if m is not None else None
            for m in item_masks]


# ---------------------------------------------------------------------------
# Where the depth map comes from
# ---------------------------------------------------------------------------
#
# Same shape as the segmenter seam, for the same reason: the choice of model is
# a config value, not surgery on the scan path. Everything above this line is
# the measurement and is fully tested; everything below is which model produces
# the input to it.
#
# The route:
#
#   Depth Anything V2 SMALL     Apache-2.0, 24.8M params. The Base, Large and
#                               Giant weights are CC-BY-NC and cannot be used in
#                               a paid app -- a licence check that is easy to
#                               skip and expensive to discover late.
#
# It returns RELATIVE depth, which is normally the weaker product and here is
# not: the plate's measured diameter resolves the scale, so a metric model (all
# of which are non-commercial) buys nothing.
#
# Phone depth -- ARKit, LiDAR, ARCore -- is strictly better where it exists and
# arrives through the same interface. It is the ceiling; monocular is the floor,
# and the floor works on Android, in Expo Go, and on photographs already taken.

from typing import Protocol


class DepthProvider(Protocol):
    """An image in, a depth map out, in whatever units the model likes."""

    name: str

    def available(self) -> bool:
        """Checked once per scan, never assumed."""
        ...

    def depth(self, rgb: np.ndarray) -> np.ndarray | None:
        """Depth for this image, or None. None is a supported answer."""
        ...


class NullDepth:
    """No depth. The default, so the app behaves identically without a model.

    A missing dependency costs a measurement, never a scan: every height falls
    back to the prior, which is exactly where the estimator is today.
    """

    name = "none"

    def available(self) -> bool:
        return False

    def depth(self, rgb):
        return None


def orientation_is_sane(depth: np.ndarray, plate_mask: np.ndarray) -> bool:
    """Does food read as ABOVE the plate, or below it?

    Depth models differ: some return depth (far is larger), some inverse depth
    (near is larger). Get it backwards and every mound becomes a dent -- and the
    result still looks like a plausible portion, which is what makes it
    dangerous. Cheap to check: the middle of a plate of food should sit nearer
    the camera than its bare rim.
    """
    if depth is None or plate_mask is None:
        return False
    if depth.shape[:2] != plate_mask.shape[:2]:
        return False
    rim = _rim_of(plate_mask)
    if rim is None or not rim.any():
        return False
    h, w = plate_mask.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w]
    cy, cx = np.array(np.nonzero(plate_mask)).mean(axis=1)
    radius = np.sqrt(plate_mask.sum() / np.pi)
    middle = plate_mask & (np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) / max(radius, 1e-6) < 0.5)
    if not middle.any():
        return False
    return float(np.median(depth[middle])) > float(np.median(depth[rim]))
