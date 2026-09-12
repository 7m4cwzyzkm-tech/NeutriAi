"""Height measured from a depth map.

THESE SCENES ARE RAY-TRACED, AND THAT IS THE POINT.

The first version of this file built its scenes by inverting the very rule it
was testing -- food of `peak_mm / mm_per_unit()` depth units, where
`mm_per_unit` was the function under test. Any scale rule at all passes a test
built that way, because the fixture and the code share the same assumption.
A wrong one did, for as long as the file existed.

So a scene here is a pinhole camera, a plate lying on a tilted plane, and food
on the plate, rendered as the RELATIVE INVERSE DEPTH a real model returns:
an arbitrary linear function of 1/Z, normalised per image. Nothing in the
renderer knows what the code believes. Against the old rule these scenes come
out at 0.03-0.21 mm for a 20 mm mound; against the corrected one, 19-20 mm.

Shapes whose mean height over their own footprint is known from geometry --
hemisphere 2r/3, cone r/3, slab r -- so the assertion is against arithmetic
rather than against another estimate.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.services.ai import depth_map as D

SIDE = 480
PLATE_MM = 254.0       # the bench plate, measured three ways
# Comfortably inside MIN_TILT_DEG..MAX_TILT_DEG: a plate photographed at a
# normal three-quarter angle, which is the case this file is allowed to measure.
TILT_DEG = 35.0


def _scene(shape: str, peak_mm: float = 30.0, food_r_mm: float = 70.0,
           tilt_deg: float = TILT_DEG, plate_mm: float = PLATE_MM,
           z0_mm: float = 420.0, f_px: float = 760.0,
           gain: float = 3.7, offset: float = -1.2):
    """A ray-traced plate of food, as a normalised relative inverse-depth map.

    Camera at the origin looking down +z. The plate lies on a plane tilted
    `tilt_deg` from square-on. `gain` and `offset` are the model's arbitrary
    affine ambiguity -- the code must be invariant to both, and there is a test
    below that says so.
    """
    cx = cy = SIDE / 2.0
    ys, xs = np.mgrid[0:SIDE, 0:SIDE].astype(np.float64)
    d = np.dstack([(xs - cx) / f_px, (ys - cy) / f_px, np.ones_like(xs)])

    th = np.radians(tilt_deg)
    n = np.array([0.0, -np.sin(th), -np.cos(th)])       # normal, facing camera
    p0 = np.array([0.0, 0.0, z0_mm])
    t = (p0 @ n) / (d @ n)
    point = d * t[..., None]

    e1 = np.array([1.0, 0.0, 0.0])
    e2 = np.cross(n, e1)
    rel = point - p0
    r = np.hypot(rel @ e1, rel @ e2)                    # radius ON the plate

    plate = r <= plate_mm / 2.0
    food = r <= food_r_mm
    u = np.clip(r / food_r_mm, 0.0, 1.0)
    if shape == "hemisphere":
        h = peak_mm * np.sqrt(np.clip(1.0 - u ** 2, 0.0, 1.0))
    elif shape == "cone":
        h = peak_mm * (1.0 - u)
    elif shape == "slab":
        h = np.full_like(u, peak_mm)
    elif shape == "none":
        h = np.zeros_like(u)
    else:
        raise ValueError(shape)
    h = np.where(food, h, 0.0)

    # The food surface is the plate plane offset by h ALONG ITS NORMAL, so the
    # same camera ray meets it h/cos(tilt) nearer -- not h*cos(tilt).
    #
    # This line used to read `t + h * n[2]`, which is h*cos, and the estimator
    # carried the identical mistake, so every test agreed with the code and the
    # answer was out by 1/cos^2 -- 2x at 45 degrees, 5x at 64. Exactly the
    # circularity this file's docstring claims to have removed, one layer down:
    # the PLATE was ray-traced, the FOOD was not.
    surface = t + h / (d @ n)                           # food rises toward us
    inv = gain / surface + offset
    depth = (inv - inv.min()) / (inv.max() - inv.min())
    return depth, plate, food


def _measured(shape, **kw):
    depth, plate, food = _scene(shape, **kw)
    return D.measure_heights(depth, plate, [food], kw.get("plate_mm", PLATE_MM))[0]


@pytest.mark.parametrize("shape,expected_fraction", [
    # mean height over the footprint, as a fraction of the peak, from geometry
    ("hemisphere", 2 / 3),
    ("cone", 1 / 3),
    ("slab", 1.0),
])
def test_it_measures_the_mean_height_of_a_known_shape(shape, expected_fraction):
    """The whole claim. PROFILE_FACTORS guesses this fraction -- 0.68 for a
    mound -- and a depth map does not have to: it measures whatever shape the
    food actually is."""
    peak_mm = 30.0
    got = _measured(shape, peak_mm=peak_mm)
    assert got is not None, f"{shape} was not measured at all"
    expected_mm = peak_mm * expected_fraction
    assert got == pytest.approx(expected_mm, rel=0.10), (
        f"{shape}: measured {got:.1f} mm against a geometric {expected_mm:.1f} mm"
    )


def test_a_hemisphere_and_a_cone_of_the_same_peak_differ_the_way_geometry_says():
    """Two foods the same height and the same footprint hold different amounts.
    A prior cannot tell them apart; this must."""
    dome, cone = _measured("hemisphere"), _measured("cone")
    assert dome is not None and cone is not None
    assert dome / cone == pytest.approx(2.0, rel=0.10)


def test_the_arbitrary_gain_of_a_relative_model_changes_nothing():
    """A relative depth model returns SOME linear function of 1/Z -- the gain
    and offset differ per image and per model and are never reported.

    If any of them moved the answer, the number would be a property of the
    model's normalisation rather than of the food. This is the single check
    that says the plate, not the model, is setting the scale.
    """
    answers = [_measured("hemisphere", gain=g, offset=o)
               for g, o in ((1.0, 0.0), (3.7, -1.2), (17.0, 4.0), (0.4, 9.9))]
    assert all(a is not None for a in answers), answers
    assert max(answers) == pytest.approx(min(answers), rel=0.001), answers


def test_the_same_food_measures_the_same_from_different_camera_angles():
    """The tilt is what pins the scale, so it is also the obvious way for the
    scale to come out tilt-dependent. A dinner does not get heavier because the
    photographer leaned over."""
    got = [_measured("hemisphere", tilt_deg=t) for t in (25.0, 35.0, 45.0, 55.0)]
    assert all(g is not None for g in got), got
    assert max(got) / min(got) < 1.10, got


def test_a_plate_photographed_square_on_is_refused_not_guessed():
    """A square-on plate has no foreshortening, so the depth map carries no
    information about its own gain -- the height is then whatever the model's
    normalisation happened to be. Measured on a ray-traced plate: a 2% error in
    the ellipse takes a 10-degree answer to ZERO.

    So it is refused, and the estimator falls back to its priors. This costs
    real photographs -- overhead shots are common -- and that is the honest
    price of a relative model."""
    for tilt in (0.5, 5.0, 12.0, 19.0):
        assert _measured("hemisphere", tilt_deg=tilt) is None, tilt
    # and edge-on, where the food occludes its own footprint
    assert _measured("hemisphere", tilt_deg=78.0) is None


def test_the_scale_comes_from_the_plate_we_measured():
    """Halve the plate's real size and every height must halve with it. The
    user's tape measurement is the only absolute length in the picture."""
    depth, plate, food = _scene("hemisphere")
    big = D.measure_heights(depth, plate, [food], 254.0)[0]
    small = D.measure_heights(depth, plate, [food], 127.0)[0]
    assert big is not None and small is not None
    assert big / small == pytest.approx(2.0, rel=0.02)


def test_it_holds_up_across_camera_distance_and_lens():
    """Neither distance nor focal length is known to the app, so neither may
    appear in the answer."""
    for kw in (dict(z0_mm=420.0), dict(z0_mm=600.0), dict(z0_mm=900.0),
               dict(f_px=450.0), dict(f_px=650.0)):
        got = _measured("hemisphere", **kw)
        assert got is not None, kw
        assert got == pytest.approx(20.0, rel=0.10), (kw, got)


def test_a_plate_the_frame_cut_off_is_refused():
    """The scale rests entirely on the plate's ellipse, and a plate running off
    the edge of the photograph does not have one -- its minor axis is whatever
    the crop left behind. Found by a ray-traced plate at 300 mm, close enough
    that the plate overflowed the frame: a 20.0 mm mound measured 14.4 mm and
    every other check passed it."""
    assert _measured("hemisphere", z0_mm=300.0) is None


def test_a_few_stray_border_pixels_are_not_a_cut_plate():
    """The other half of the same guard. A colour-derived mask frays at its
    edge, and refusing on a single stray pixel would throw away good
    photographs to catch a fault that leaves a long run along the border."""
    depth, plate, food = _scene("hemisphere")
    frayed = plate.copy()
    frayed[0, 10:18] = True          # a few pixels of noise on the top edge
    frayed[-1, 40:45] = True
    got = D.measure_heights(depth, frayed, [food], PLATE_MM)[0]
    assert got is not None, "a frayed mask was mistaken for a cut plate"
    assert got == pytest.approx(20.0, rel=0.10), got


def test_a_plate_it_cannot_find_returns_nothing_rather_than_a_number():
    """A measurement built on a bad reference does not degrade, it inverts --
    the colour mask shipped a 146 g drumstick as 12 g learning that. None means
    "not measured" and the estimator falls back to its priors."""
    depth, plate, food = _scene("hemisphere")
    assert D.measure_heights(depth, None, [food], PLATE_MM) == [None]
    assert D.measure_heights(None, plate, [food], PLATE_MM) == [None]
    tiny = np.zeros_like(plate); tiny[0:6, 0:6] = True
    assert D.measure_heights(depth, tiny, [food], PLATE_MM) == [None]
    assert D.measure_heights(depth, plate, [food], 5.0) == [None]
    assert D.measure_heights(depth, plate, [food], 5000.0) == [None]


def test_a_rim_that_landed_on_food_is_refused():
    """If the annulus samples food instead of bare plate, the 'plane' tilts to
    follow the food and the whole field inverts. That is the failure mode that
    has to fail loudly."""
    depth, plate, _ = _scene("hemisphere", peak_mm=30.0, food_r_mm=200.0)
    got = D.measure_heights(depth, plate, [plate], PLATE_MM)[0]
    assert got is None, f"a plate-wide mound was measured anyway: {got}"


def test_a_dent_is_not_negative_food():
    """A pixel below the plate plane is a shadow or a fit error, not food of
    negative thickness. Letting it subtract would quietly lighten every portion
    with a dark edge."""
    depth, plate, food = _scene("slab", peak_mm=30.0)
    baseline = D.measure_heights(depth, plate, [food], PLATE_MM)[0]
    assert baseline is not None
    fit = D.fit_plate(depth, plate)
    relief = float(np.percentile(fit.field[food], 90))
    ys, xs = np.mgrid[0:SIDE, 0:SIDE]
    depth = depth.copy()
    depth[food & (xs < SIDE // 2)] -= 3.0 * relief       # a deep false shadow
    with_shadow = D.measure_heights(depth, plate, [food], PLATE_MM)[0]
    assert with_shadow is not None and with_shadow > 0
    assert with_shadow == pytest.approx(baseline / 2, rel=0.20)


def test_absurd_heights_are_refused():
    """A 400 mm tall dinner is a broken depth map, not a big meal."""
    assert _measured("slab", peak_mm=900.0) is None


def test_the_plate_reports_its_own_tilt_from_its_own_pixels():
    """cos(tilt) is the ellipse's minor axis over its major -- measured from the
    mask, not asked of the model, whose geometry arrives on a 0.05 grid that
    would be a 5% error in the ellipse and a 27% error in the height."""
    for tilt in (25.0, 35.0, 45.0, 55.0):
        depth, plate, _ = _scene("none", tilt_deg=tilt)
        fit = D.fit_plate(depth, plate)
        assert fit is not None
        assert fit.tilt_deg == pytest.approx(tilt, abs=2.0)


def test_the_error_bar_is_reported_rather_than_hidden():
    """A(tilt) = 1/tan^2 + 2 is how much an error in the ellipse is magnified in
    the height. It is the confidence attached to every number in this file."""
    depth, plate, _ = _scene("none", tilt_deg=45.0)
    assert D.fit_plate(depth, plate).error_amplification == pytest.approx(3.0, rel=0.15)
    depth, plate, _ = _scene("none", tilt_deg=30.0)
    assert D.fit_plate(depth, plate).error_amplification == pytest.approx(5.0, rel=0.25)


# --- the estimator's side of the wire ----------------------------------------

def _est(**over):
    from app.services.ai.portion import GeometryHint, estimate_grams
    kw = dict(name="mexican rice", area_ratio=0.15,
              hint=GeometryHint(plate_ellipse_area_ratio=0.35, plate_diameter_mm=254),
              plate_coverage=0.20, shape_hint="mound",
              bbox={"w": 0.45, "h": 0.45}, detection_confidence=0.8)
    kw.update(over)
    return estimate_grams(**kw)


def test_a_measured_height_replaces_the_prior_and_the_profile_factor():
    """Both, and this is the part that is easy to get wrong.

    PROFILE_FACTORS turns a PEAK height into a MEAN one -- 0.68 for a mound. A
    depth map returns the mean directly. Applying the factor as well counts the
    same correction twice and reads every measured portion about a third light.

    The check: feeding the measured height that the prior chain was ALREADY
    implying -- 32 mm x 0.68 = 21.76 -- must reproduce the prior answer exactly.
    If the factor were still being applied it would come out 32% lighter.
    """
    from app.services.ai.portion import HEIGHT_PRIORS_MM, PROFILE_FACTORS

    prior = _est().grams
    equivalent = HEIGHT_PRIORS_MM["mound"] * PROFILE_FACTORS["mound"]
    assert _est(measured_height_mm=equivalent).grams == pytest.approx(prior, rel=0.005)

    # and it is genuinely the effective height now, not the peak
    assert _est(measured_height_mm=equivalent * 2).grams == pytest.approx(prior * 2, rel=0.01)
    assert _est(measured_height_mm=equivalent / 2).grams == pytest.approx(prior / 2, rel=0.01)


def test_a_depth_height_beats_a_learned_one_which_beats_the_prior():
    """Ordered by what the number describes. A depth map measured THIS plate; a
    learned height is a statement about food in general; a prior is a guess
    about food in general. Specific beats general."""
    learned = _est(learned_heights={("mound", None): 24.0}).grams
    both = _est(learned_heights={("mound", None): 24.0},
                measured_height_mm=40.0).grams
    assert both != learned
    assert both == pytest.approx(_est(measured_height_mm=40.0).grams)


def test_an_unusable_measurement_changes_nothing():
    """None means "not measured" and the estimator falls back, which is exactly
    where it is today. A depth model that fails must cost a measurement, never
    a scan -- and never a silently different weight."""
    prior = _est().grams
    for bad in (None, "junk", -5.0, 0.0, 500.0, float("nan")):
        assert _est(measured_height_mm=bad).grams == pytest.approx(prior), bad


def test_a_measured_height_says_so_in_the_notes():
    est = _est(measured_height_mm=30.0)
    assert any("measured from the photo's depth" in n for n in est.notes), est.notes
    assert not any("measured from the photo's depth" in n for n in _est().notes)


# --- where the depth map comes from -------------------------------------------

def test_the_default_provider_measures_nothing_and_says_so():
    """A missing model costs a measurement, never a scan."""
    n = D.NullDepth()
    assert n.available() is False
    assert n.depth(np.zeros((10, 10, 3), dtype=np.uint8)) is None


def test_a_depth_map_the_wrong_way_round_is_caught():
    """Some models return depth (far is larger), some inverse depth (near is
    larger). Backwards, every mound becomes a dent -- and the resulting portion
    still looks plausible, which is what makes it dangerous.

    A plate of food should read nearer at its middle than at its bare rim.
    """
    depth, plate, _food = _scene("hemisphere", 100, 30.0)
    assert D.orientation_is_sane(depth, plate) is True
    assert D.orientation_is_sane(-depth, plate) is False
    assert D.orientation_is_sane(None, plate) is False
    assert D.orientation_is_sane(depth, None) is False
