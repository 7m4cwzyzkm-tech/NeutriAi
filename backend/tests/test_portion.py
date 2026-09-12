"""The portion estimator is the product's core claim. These tests pin the
behaviour that makes it trustworthy: plausible outputs, honest bands, and
confidence that degrades with the quality of the geometry."""
import math

import pytest

from app.services.ai.portion import (
    BBOX_FILL_CEILING, BLEND_FULL, DEFAULT_PLATE_DIAMETER_MM, GeometryHint,
    _classify_shape, band_label, density_for, estimate_grams, mm2_per_frame,
    reconcile_multi_image,
)
from app.services.ai.food_seg import MEASURED_HEIGHTS_MM

PLATE = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=270)


@pytest.mark.parametrize(
    "name,area,low,high",
    [
        ("jasmine rice", 0.14, 100, 350),
        ("grilled chicken breast", 0.10, 90, 300),
        ("caesar salad", 0.20, 40, 200),
        ("tomato soup", 0.18, 200, 600),
        ("french fries", 0.15, 60, 250),
        ("salmon fillet", 0.09, 70, 250),
    ],
)
def test_estimates_are_plausible(name, area, low, high):
    e = estimate_grams(name=name, area_ratio=area, hint=PLATE, detection_confidence=0.8)
    assert low <= e.grams <= high, f"{name} -> {e.grams} g is outside a realistic serving"


def test_band_contains_estimate():
    e = estimate_grams(name="rice", area_ratio=0.15, hint=PLATE)
    assert e.grams_low < e.grams < e.grams_high


def test_confidence_degrades_without_reference():
    good = estimate_grams(name="rice", area_ratio=0.15, hint=PLATE)
    blind = estimate_grams(name="rice", area_ratio=0.15, hint=GeometryHint(), ai_prior_grams=180)
    assert good.confidence > blind.confidence
    assert good.method == "plate_reference"
    assert blind.method == "ai_prior"


def test_band_widens_as_confidence_falls():
    good = estimate_grams(name="rice", area_ratio=0.15, hint=PLATE)
    blind = estimate_grams(name="rice", area_ratio=0.15, hint=GeometryHint(), ai_prior_grams=180)
    width = lambda e: (e.grams_high - e.grams_low) / e.grams
    assert width(blind) > width(good)


def test_compact_servings_scale_linearly_with_area():
    """Two regimes, and this is the lower one.

    Below the reference coverage the height prior stands unmodified, because a
    small footprint is evidence of a small portion -- not of a steeper pile.
    A modest scoop of rice and a larger scoop are the same depth; they differ
    by how much of the plate they cover. Linear is the correct law here.

    Asserting it matters: an earlier build inflated compact items by up to 60%
    on the theory that a small footprint meant a tall pile. On a plate holding
    three foods every item is compact, so all three inflated at once and the
    plate read 636 g against 312 g weighed.
    """
    small = estimate_grams(name="rice", area_ratio=0.05, hint=PLATE)
    large = estimate_grams(name="rice", area_ratio=0.10, hint=PLATE)
    assert large.grams / small.grams == pytest.approx(2.0, rel=0.15)


def test_spreading_food_scales_sub_linearly():
    """The upper regime, and the one with physics behind it.

    Past the reference coverage the food is a layer rather than a pile: a fixed
    volume over more area has to be shallower. Mass must therefore grow more
    slowly than footprint. This is also what damps vision noise -- the area
    estimate swung 20% between two calls on one photo, and sub-linear scaling
    is what stops that reaching the answer intact.

    A ratio at or above 1.8 means the damping has stopped working. At or below
    1.0 means area has stopped counting.
    """
    a = estimate_grams(name="rice", area_ratio=0.25, hint=PLATE)   # 45% coverage
    b = estimate_grams(name="rice", area_ratio=0.45, hint=PLATE)   # 82% coverage
    ratio = b.grams / a.grams
    assert 1.0 < ratio < 1.6, (
        f"1.8x the area produced {ratio:.2f}x the mass; expected clearly sub-linear"
    )


def test_clamped_at_upper_rail():
    """An absurd area ratio must not produce an absurd number."""
    e = estimate_grams(name="rice", area_ratio=0.99, hint=PLATE, detection_confidence=0.9)
    assert e.grams <= 1500
    assert e.confidence < 0.8


def test_density_lookup():
    # 0.67, not 0.78: USDA cooked white rice is 158 g per cup, and a cup is
    # 236.588 ml. The old 0.78 was near the UNCOOKED figure and this test pinned
    # it -- which is how a wrong constant survived every green run for weeks.
    # A test that asserts a value only proves it has not changed.
    assert density_for("jasmine rice") == pytest.approx(0.67)
    assert density_for("mixed greens salad") == pytest.approx(0.22)
    assert density_for("something unheard of") == pytest.approx(0.85)
    assert density_for("rice", explicit=0.9) == pytest.approx(0.9)

    # Specific beats generic: "refried beans" is a paste, not whole beans in
    # broth. Longest key wins, so table order cannot shadow it.
    # 1.06 stands, and the reason it stands is worth writing down.
    #
    # USDA FoodData Central 174296 gives refried beans at 242 g/cup, which is
    # 1.023 g/ml, and that row is in data/macro_references/densities.csv with
    # its citation. It is NOT applied: density sits in the weight formula, and
    # the weight path changes only on a bench result, never as a side effect of
    # adding a data file. USE_SOURCED_DENSITIES is the switch, and
    # test_macro_references pins it off.
    assert density_for("refried beans") == pytest.approx(1.06)
    assert density_for("black beans") == pytest.approx(0.72)

    # The coarse food group is a fallback, never an override.
    assert density_for("refried beans", group="legume") == pytest.approx(1.06)
    assert density_for("unheard-of pulse", group="legume") == pytest.approx(0.78)


def test_every_density_key_resolves_to_itself():
    """Guards against a whole class of silent breakage.

    Adding a trailing comment to a line that held five entries commented four of
    them out -- french fries silently fell back to the 0.85 default, a 2x error,
    and only a plausibility bound noticed. This asserts every key in the table
    is actually reachable, which turns that failure into an obvious one.
    """
    from app.services.ai.portion import DENSITY_G_ML

    unreachable = [
        key for key, value in DENSITY_G_ML.items()
        if key != "default" and density_for(key) != value
    ]
    assert not unreachable, f"keys shadowed or lost from the table: {unreachable}"


def test_geometry_ladder_order():
    assert mm2_per_frame(GeometryHint(reference_area_mm2=5000, plate_ellipse_area_ratio=0.5))[1] == "plate_reference"
    assert mm2_per_frame(GeometryHint(plate_diameter_mm=270, plate_ellipse_area_ratio=0.5))[1] == "plate_reference"
    assert mm2_per_frame(GeometryHint(depth_mm=400))[1] == "depth_model"
    assert mm2_per_frame(GeometryHint(plate_ellipse_area_ratio=0.5))[1] == "pixel_area"
    assert mm2_per_frame(GeometryHint())[1] == "ai_prior"


def test_blending_when_geometry_and_prior_disagree():
    e = estimate_grams(
        name="rice", area_ratio=0.40, hint=PLATE, ai_prior_grams=100, detection_confidence=0.9
    )
    assert any("disagreed" in n for n in e.notes)


def test_reconcile_agreeing_views_raises_confidence():
    a = estimate_grams(name="rice", area_ratio=0.15, hint=PLATE, detection_confidence=0.7)
    b = estimate_grams(name="rice", area_ratio=0.152, hint=PLATE, detection_confidence=0.7)
    merged = reconcile_multi_image([a, b])
    assert merged.confidence >= a.confidence
    assert merged.method == "multi_image"


def test_reconcile_disagreeing_views_lowers_confidence():
    a = estimate_grams(name="rice", area_ratio=0.08, hint=PLATE, detection_confidence=0.8)
    b = estimate_grams(name="rice", area_ratio=0.30, hint=PLATE, detection_confidence=0.8)
    merged = reconcile_multi_image([a, b])
    assert merged.confidence < a.confidence
    assert (merged.grams_high - merged.grams_low) / merged.grams > 0.3


def test_band_labels():
    assert band_label(0.9) == "high"
    assert band_label(0.6) == "medium"
    assert band_label(0.3) == "low"


# ---------------------------------------------------------------------------
# Coverage-dependent height, and a continuous prior blend.
#
# Both exist because of a real measurement: the same photo of 218 g of
# scrambled eggs, scanned twice, returned 322 g and then 353 g. Two separate
# faults produced that. Height did not depend on how far the food was spread,
# so grams scaled linearly with a noisy area estimate; and the blend toward
# the serving prior switched on abruptly at 2.5x disagreement, putting a 35%
# cliff in the middle of the operating range.
# ---------------------------------------------------------------------------
def _hint(plate_ratio=0.6):
    return GeometryHint(
        plate_ellipse_area_ratio=plate_ratio,
        plate_diameter_mm=None,
        reference_area_mm2=None,
        image_count=1,
    )


def _grams(area_ratio, *, prior=150.0, shape="mound", density=1.03):
    return estimate_grams(
        name="egg, whole, cooked, scrambled",
        area_ratio=area_ratio,
        hint=_hint(),
        shape_hint=shape,
        density=density,
        ai_prior_grams=prior,
        detection_confidence=0.8,
    ).grams


def test_no_cliff_between_adjacent_area_readings():
    """The old hard blend threshold made 0.22 -> 0.24 area drop the estimate
    from 372 g to 241 g. Ordinary vision noise straddling that line changed the
    answer by half.

    The bound is 3%, not 0%, and that is a deliberate statement about a real
    residual. Because the pull toward a fixed serving prior strengthens as the
    geometry moves away from it, a rising geometry can produce a slightly lower
    result inside the ramp. It is provably not removable while the blend can
    reach a full geometric mean: monotonicity would need the weight to grow
    more slowly than the geometry does, and at maximum pull no such ramp
    exists. Capping the pull near 0.35 would fix it and would also change every
    estimate, so it stays until there is weighed data to judge it against.
    Measured worst case is 2.3%.

    This swept ONE configuration before, and passed at a 1% bound while other
    configurations were already at 2.3% -- a test that reported comfort it had
    not earned. It now sweeps several.
    """
    configs = [
        ("assumed plate", GeometryHint(plate_ellipse_area_ratio=0.55)),
        ("measured plate", GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=269)),
        ("bowl", GeometryHint(plate_ellipse_area_ratio=0.60, vessel="bowl")),
        ("takeout box", GeometryHint(plate_ellipse_area_ratio=0.60, vessel="takeout_box")),
        ("depth", GeometryHint(depth_mm=350, camera_fov_deg=68)),
    ]
    for label, hint in configs:
        for i in range(2, 54):
            lo = estimate_grams(name="rice", area_ratio=i / 100, hint=hint,
                                shape_hint="mound", density=0.78, ai_prior_grams=150.0).grams
            hi = estimate_grams(name="rice", area_ratio=(i + 1) / 100, hint=hint,
                                shape_hint="mound", density=0.78, ai_prior_grams=150.0).grams
            drop = (lo - hi) / lo
            assert drop < 0.03, (
                f"{label}: area {i/100:.2f} -> {(i+1)/100:.2f} dropped {drop:.1%} "
                f"({lo:.1f} g -> {hi:.1f} g); the cliff is coming back"
            )


def test_more_camera_distance_never_means_less_food():
    """Further from the plate means a wider frame and more real-world area, so
    the estimate must not fall. Same residual bound and the same cause."""
    hint = lambda d: GeometryHint(depth_mm=d, camera_fov_deg=68)
    for d in range(150, 900, 10):
        lo = estimate_grams(name="rice", area_ratio=0.20, hint=hint(d),
                            shape_hint="mound", density=0.78, ai_prior_grams=150.0).grams
        hi = estimate_grams(name="rice", area_ratio=0.20, hint=hint(d + 10),
                            shape_hint="mound", density=0.78, ai_prior_grams=150.0).grams
        assert (lo - hi) / lo < 0.03, f"{d}mm -> {d+10}mm dropped {(lo-hi)/lo:.1%}"


def test_blend_still_reaches_the_geometric_mean_at_full_disagreement():
    """The ramp must not change what the old threshold produced where it fired
    -- only how it gets there. At >= BLEND_FULL the pull is the geometric mean."""
    prior = 150.0
    geometry_only = estimate_grams(
        name="egg", area_ratio=0.30, hint=_hint(), shape_hint="mound",
        density=1.03, ai_prior_grams=None, detection_confidence=0.8,
    ).grams
    assume = geometry_only / prior
    if assume >= BLEND_FULL:
        blended = _grams(0.30, prior=prior)
        assert blended == pytest.approx(math.sqrt(geometry_only * prior), rel=0.02)


def test_spread_damps_vision_noise():
    """A 20% swing in the detected area used to move the answer 20%. Grams now
    scale with the square root of area, so the same swing moves it far less."""
    lo, hi = _grams(0.40), _grams(0.48)
    swing = abs(hi - lo) / lo
    assert swing < 0.10, f"a 20% area swing still moves grams by {swing:.1%}"


def test_spread_factor_is_bounded_at_both_ends():
    """A tiny detection must not be inflated without limit, and a plate-filling
    one must not be crushed to nothing."""
    assert 40 < _grams(0.02) < 400
    assert 40 < _grams(0.59) < 600


def test_more_food_still_means_more_food():
    """Whatever the local wobble, the overall relationship must hold."""
    assert _grams(0.50) > _grams(0.10)


def test_wide_spread_is_treated_as_thinner():
    """Food covering most of the plate is a layer, not a pile. The note is part
    of the contract -- it is what the user sees explaining the number."""
    est = estimate_grams(
        name="egg", area_ratio=0.40, hint=_hint(), shape_hint="mound",
        density=1.03, ai_prior_grams=150.0, detection_confidence=0.8,
    )
    assert any("more spread out" in n for n in est.notes), est.notes


# ---------------------------------------------------------------------------
# Vessel-derived scale.
#
# The vision model was already reporting what the food was served in; nothing
# read it. Every surface was scaled as a 270 mm dinner plate, so a ~190 mm
# takeout clamshell -- most restaurant food -- came out roughly double.
# ---------------------------------------------------------------------------
def _by_vessel(vessel, *, ratio=0.83, area=0.55):
    return estimate_grams(
        name="fettuccine alfredo",
        area_ratio=area,
        hint=GeometryHint(plate_ellipse_area_ratio=ratio, vessel=vessel),
        shape_hint="mound", density=0.75, ai_prior_grams=300.0,
        detection_confidence=0.8,
    )


def test_vessel_changes_the_scale():
    """Identical pixels in a smaller vessel must mean less food."""
    plate = _by_vessel("dinner_plate").grams
    box = _by_vessel("takeout_box").grams
    bowl = _by_vessel("bowl").grams
    assert bowl < box < plate, (bowl, box, plate)


def test_takeout_box_is_not_scaled_as_a_dinner_plate():
    """The original defect: a clamshell read as a dinner plate roughly doubles.

    The assertion used to be `< 0.75x the dinner-plate result`, chosen when the
    only difference between the two was footprint. That is no longer true, and
    the change is deliberate: a takeout box has walls, so a full one is deep,
    while food spread across a flat plate is thin. Two real effects now pull in
    opposite directions and the raw gram ratio no longer isolates either.

    So this checks the thing it was always about -- the SCALE the estimate is
    built on -- and separately that the box still yields less food. Asserting
    on vessel_area tests the mechanism rather than a number that happens to
    fall out of it.
    """
    from app.services.ai.portion import vessel_area
    box_area = vessel_area("takeout_box")[0]
    plate_area = vessel_area("dinner_plate")[0]
    assert box_area < plate_area * 0.75, (box_area, plate_area)
    assert _by_vessel("takeout_box").grams < _by_vessel("dinner_plate").grams


def test_a_walled_vessel_is_not_thinned_but_a_plate_is():
    """The two effects that now coexist, pinned separately so a future change
    to either is visible."""
    box = _by_vessel("takeout_box")
    plate = _by_vessel("dinner_plate")
    assert any("walls" in n for n in box.notes), box.notes
    assert any("height was scaled" in n for n in plate.notes), plate.notes


def test_surfaces_with_no_standard_size_do_not_invent_one():
    """Paper, foil, a hand, a bare table. Falling back to a serving prior and
    saying so is correct; fabricating a plate is not."""
    for surface in ("paper", "foil", "hand", "table", "none"):
        est = estimate_grams(
            name="cheesesteak", area_ratio=0.35,
            hint=GeometryHint(plate_ellipse_area_ratio=0.0, vessel=surface),
            shape_hint="wrapped", density=0.9, ai_prior_grams=350.0,
            detection_confidence=0.8,
        )
        assert est.method == "ai_prior", f"{surface} produced {est.method}"
        assert est.grams == pytest.approx(350.0)


def test_loose_vessels_report_wider_bands():
    """A takeout box varies far more in size than a dinner plate. The estimate
    built on it must be less confident, not equally confident."""
    plate, box = _by_vessel("dinner_plate"), _by_vessel("takeout_box")
    assert box.confidence < plate.confidence
    assert (box.grams_high - box.grams_low) / box.grams > \
           (plate.grams_high - plate.grams_low) / plate.grams


def test_an_unrecognised_vessel_is_not_a_dinner_plate():
    """A banana leaf, a tiffin, an injera platter -- named, but with no size we
    know. This used to fall through to the 270 mm dinner-plate assumption,
    which is the exact failure the vessel table was built to end: a clamshell
    read as a dinner plate inflates the frame area about 2x and every gram
    with it. A named surface we cannot size is not a reference, so say so and
    let the serving prior admit it is guessing.
    """
    est = estimate_grams(
        name="rice", area_ratio=0.30,
        hint=GeometryHint(plate_ellipse_area_ratio=0.5, vessel="banana_leaf"),
        shape_hint="mound", density=0.78, ai_prior_grams=180.0,
        detection_confidence=0.8,
    )
    assert est.method == "ai_prior"
    assert est.grams > 0

    # A vessel we DO know is unaffected.
    known = estimate_grams(
        name="rice", area_ratio=0.30,
        hint=GeometryHint(plate_ellipse_area_ratio=0.5, vessel="bowl"),
        shape_hint="mound", density=0.78, ai_prior_grams=180.0,
        detection_confidence=0.8,
    )
    assert known.method == "vessel_reference"


def test_no_vessel_preserves_previous_behaviour():
    """Every existing caller passes no vessel. They must be unaffected."""
    est = estimate_grams(
        name="rice", area_ratio=0.25,
        hint=GeometryHint(plate_ellipse_area_ratio=0.55),
        shape_hint="mound", density=0.78, ai_prior_grams=180.0,
        detection_confidence=0.8,
    )
    assert est.method == "pixel_area"


def test_vessel_area_lookup_is_forgiving_about_formatting():
    """The model writes 'Takeout Box' or 'takeout-box' as readily as the enum."""
    from app.services.ai.portion import vessel_area
    canonical = vessel_area("takeout_box")[0]
    for variant in ("Takeout Box", "takeout-box", " TAKEOUT_BOX "):
        assert vessel_area(variant)[0] == canonical


# ---------------------------------------------------------------------------
# Bounding-box consistency.
#
# Measured against weighed meals: single-item vessels came in at 11.8% mean
# error, a plate holding three foods at +104%. Working back from the numbers,
# the vision model had claimed a chicken drumstick occupied a 120 x 120 mm
# footprint and a smear of refried beans 99 x 99 -- area over-reported by 2.4x
# to 4.3x. A shape cannot cover more area than its own bounding box, and a box
# is a linear judgement, which vision models make far better than area
# fractions. So the box is the more trustworthy of the two numbers.
# ---------------------------------------------------------------------------
def test_area_cannot_exceed_its_own_bounding_box():
    est = estimate_grams(
        name="mexican rice", area_ratio=0.30, hint=GeometryHint(plate_ellipse_area_ratio=0.55),
        shape_hint="mound", bbox={"x": 0.3, "y": 0.4, "w": 0.40, "h": 0.375},
        density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
    )
    assert any("bounding box" in n for n in est.notes), est.notes
    assert est.pixel_area_ratio <= 0.40 * 0.375


def test_consistent_bbox_is_left_alone():
    """The rail must only ever catch the impossible. A plausible claim passes
    through untouched -- otherwise it becomes a silent across-the-board shrink."""
    est = estimate_grams(
        name="mexican rice", area_ratio=0.10, hint=GeometryHint(plate_ellipse_area_ratio=0.55),
        shape_hint="mound", bbox={"x": 0.3, "y": 0.4, "w": 0.40, "h": 0.375},
        density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
    )
    assert not any("bounding box" in n for n in est.notes)
    assert est.pixel_area_ratio == pytest.approx(0.10)


def test_capping_costs_confidence():
    """If the two numbers contradict each other, we know less than we thought."""
    contradictory = estimate_grams(
        name="rice", area_ratio=0.30, hint=GeometryHint(plate_ellipse_area_ratio=0.55),
        shape_hint="mound", bbox={"x": 0.3, "y": 0.4, "w": 0.40, "h": 0.375},
        density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
    )
    consistent = estimate_grams(
        name="rice", area_ratio=0.10, hint=GeometryHint(plate_ellipse_area_ratio=0.55),
        shape_hint="mound", bbox={"x": 0.3, "y": 0.4, "w": 0.40, "h": 0.375},
        density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
    )
    assert contradictory.confidence < consistent.confidence


def test_bbox_is_accepted_in_the_shapes_models_actually_emit():
    """The prompt asks for {"x","y","w","h"}. Models return a bare [x,y,w,h]
    list often enough, and width/height keys sometimes. All are the same claim
    and all must cap."""
    for box in (
        {"x": 0.3, "y": 0.4, "w": 0.40, "h": 0.375},
        [0.3, 0.4, 0.40, 0.375],
        (0.3, 0.4, 0.40, 0.375),
        {"x": 0.3, "y": 0.4, "width": 0.40, "height": 0.375},
        {"w": "0.40", "h": "0.375"},
    ):
        est = estimate_grams(
            name="rice", area_ratio=0.30, hint=GeometryHint(plate_ellipse_area_ratio=0.55),
            shape_hint="mound", bbox=box, density=0.78, ai_prior_grams=150.0,
            detection_confidence=0.8,
        )
        assert any("bounding box" in n for n in est.notes), f"{box!r} was not capped"


def test_missing_or_malformed_bbox_is_ignored():
    """A box we cannot read must disable the ceiling, never fail the scan.

    This is not hypothetical: a list-shaped bbox raised AttributeError inside
    the rail and surfaced as a 500 on a real photo. The rail exists to catch
    impossible claims; an unreadable one should simply not constrain anything.
    """
    for box in (
        None, {}, "0.4x0.375", 0.15,
        {"w": "x", "h": None}, {"w": 0, "h": 0},
        {"w": "wide", "h": "tall"},
        {"w": 40, "h": 37},        # percentages, not ratios
        [0.3, 0.4],                # truncated
    ):
        est = estimate_grams(
            name="rice", area_ratio=0.25, hint=GeometryHint(plate_ellipse_area_ratio=0.55),
            shape_hint="mound", bbox=box, density=0.78, ai_prior_grams=180.0,
            detection_confidence=0.8,
        )
        assert est.grams > 0
        assert est.pixel_area_ratio == pytest.approx(0.25)


def test_small_footprint_is_not_read_as_a_tall_pile():
    """Asymmetry is deliberate. Spreading food thin has a mechanism behind it --
    fixed volume over more area. A small footprint has none: it almost always
    means a smaller portion, not a steeper pile. Inflation is capped tightly."""
    from app.services.ai.portion import SPREAD_FACTOR_MAX, SPREAD_FACTOR_MIN
    assert SPREAD_FACTOR_MAX <= 1.15, "compact food is being inflated too hard"
    assert SPREAD_FACTOR_MIN <= 0.50, "spread food must still be damped properly"


def test_every_bowl_label_gives_the_same_answer():
    """Two photos of one bowl, minutes apart, were labelled "large_bowl" and
    then "bowl". The priors differed by 1.78x, so the estimate moved 487.6 g to
    274.3 g on identical food -- 27.6% apart, from a single discrete flip.

    A size prior cannot be finer-grained than the classifier picking between
    them. These four labels share one prior until a classifier is measured to
    tell them apart.
    """
    grams = {
        v: estimate_grams(
            name="bean soup", area_ratio=0.50,
            hint=GeometryHint(plate_ellipse_area_ratio=0.60, vessel=v),
            shape_hint="liquid", density=1.0, ai_prior_grams=250.0,
            detection_confidence=0.8,
        ).grams
        for v in ("bowl", "soup_bowl", "large_bowl", "pasta_bowl")
    }
    assert len(set(grams.values())) == 1, grams


# ---------------------------------------------------------------------------
# The depth rung.
#
# It existed from the start and was never reachable: nothing set depth_mm,
# because the API had no field to carry it. It is the only rung that works
# with no reference object in the photo at all -- food on paper, a cutting
# board, a restaurant table -- which is exactly where the estimator was worst
# (a cheesesteak on its wrapper came in 39% low).
# ---------------------------------------------------------------------------
def test_distance_and_fov_give_real_scale():
    """frame_width = 2 * distance * tan(fov / 2). Trigonometry, not a prior."""
    area, method = mm2_per_frame(GeometryHint(depth_mm=350.0, camera_fov_deg=68.0))
    expected_w = 2 * 350.0 * math.tan(math.radians(68.0) / 2)
    assert method == "depth_model"
    assert area == pytest.approx(expected_w * (expected_w / (4 / 3)), rel=1e-6)


def test_depth_outranks_a_vessel_guess():
    """A measurement beats a category prior. If a photo carries both, the
    measured one must win -- otherwise adding a sensor changes nothing."""
    _, method = mm2_per_frame(
        GeometryHint(depth_mm=350.0, plate_ellipse_area_ratio=0.55, vessel="bowl")
    )
    assert method == "depth_model"


def test_calibrated_plate_still_outranks_depth():
    """A plate measured once by the user is better than any per-shot distance,
    so it stays at the top of the ladder."""
    _, method = mm2_per_frame(
        GeometryHint(depth_mm=350.0, plate_diameter_mm=270.0, plate_ellipse_area_ratio=0.55)
    )
    assert method == "plate_reference"


def test_field_of_view_actually_matters():
    """Area goes as the square of frame width, so a few degrees is a lot. This
    is why the client is asked for the lens's real FOV instead of assuming."""
    narrow, _ = mm2_per_frame(GeometryHint(depth_mm=350.0, camera_fov_deg=60.0))
    wide, _ = mm2_per_frame(GeometryHint(depth_mm=350.0, camera_fov_deg=75.0))
    assert wide > narrow * 1.5


def test_implausible_optics_are_ignored_not_clamped():
    """A 5 degree or 200 degree reading is a broken sensor, not a wide lens.
    Clamping it to the nearest bound would turn obvious nonsense into a
    confident wrong answer; falling back to the default fails the same way an
    unreported value does.
    """
    default, _ = mm2_per_frame(GeometryHint(depth_mm=350.0))
    for junk in (0, 5, 120, 200, -10, float("nan"), "wide", None):
        area, _ = mm2_per_frame(GeometryHint(depth_mm=350.0, camera_fov_deg=junk))
        assert area == pytest.approx(default), f"fov={junk!r} was not rejected"


def test_no_depth_changes_nothing():
    """Devices that cannot measure must behave exactly as before."""
    _, method = mm2_per_frame(GeometryHint(plate_ellipse_area_ratio=0.55, vessel="bowl"))
    assert method == "vessel_reference"


# ---------------------------------------------------------------------------
# Plate calibration.
#
# The estimator assumes a 270 mm plate when it has nothing better. Real plates
# run 220-300 mm, and area goes as the square of diameter, so the assumption is
# worth about 19% on a small plate before any food is looked at. The backend
# for this existed from the start; nothing ever wrote a calibration row.
# ---------------------------------------------------------------------------
def test_a_measured_plate_beats_the_assumed_one():
    """Same photo, same pixels. Only the known plate size differs."""
    small = estimate_grams(
        name="mexican rice", area_ratio=0.25,
        hint=GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=220),
        shape_hint="mound", density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
    )
    assumed = estimate_grams(
        name="mexican rice", area_ratio=0.25,
        hint=GeometryHint(plate_ellipse_area_ratio=0.55),
        shape_hint="mound", density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
    )
    assert small.method == "plate_reference"
    assert assumed.method == "pixel_area"

    # NOT `small.grams < assumed.grams`.
    #
    # That held by accident. The geometry does order correctly -- 257 g against
    # 388 g for these inputs -- but both estimates are then blended toward the
    # 150 g typical serving, and the assumed-plate one is blended HARDER because
    # it is less trustworthy. Pull a 388 g estimate halfway to 150 and it lands
    # below a 257 g estimate that barely moves. The blend doing its job broke a
    # test that was reading the wrong quantity.
    #
    # What "a measured plate beats an assumed one" actually means is below.
    assert small.confidence > assumed.confidence
    assert (small.grams_high - small.grams_low) < (assumed.grams_high - assumed.grams_low)


def test_measured_plate_orders_correctly_without_a_prior():
    """The geometry itself: a smaller plate means less food, full stop.

    Separated from the test above so the prior cannot confound it. With no
    typical-serving value there is nothing to blend toward, and the only thing
    left is the arithmetic.
    """
    def grams(diameter):
        hint = (GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=diameter)
                if diameter else GeometryHint(plate_ellipse_area_ratio=0.55))
        return estimate_grams(
            name="mexican rice", area_ratio=0.25, hint=hint, shape_hint="mound",
            density=0.78, detection_confidence=0.8,
        ).grams

    assert grams(220) < grams(270) < grams(320)


def test_plate_size_moves_the_answer():
    """If diameter did not matter, measuring it would be theatre."""
    a = estimate_grams(
        name="rice", area_ratio=0.25,
        hint=GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=220),
        shape_hint="mound", density=0.78, detection_confidence=0.8,
    ).grams
    b = estimate_grams(
        name="rice", area_ratio=0.25,
        hint=GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=300),
        shape_hint="mound", density=0.78, detection_confidence=0.8,
    ).grams
    assert b > a * 1.3


def test_calibration_units_round_trip():
    """The screen accepts inches or centimetres and stores millimetres. These
    are the values a real user types; a wrong unit must land outside the
    accepted 140-400 mm range rather than being quietly saved."""
    MM_PER_INCH = 25.4
    assert 140 <= 10.5 * MM_PER_INCH <= 400        # a US dinner plate
    assert 140 <= 27.0 * 10 <= 400                 # the same plate in cm
    assert not 140 <= 10.5 * 10 <= 400             # inches entered as cm
    assert not 140 <= 27.0 * MM_PER_INCH <= 400    # cm entered as inches


# ---------------------------------------------------------------------------
# Vessel shape.
#
# Every vessel was modelled as a circle, so a square plate -- same width, 27%
# more surface -- was read 27% light, along with every rectangular tray and
# cutting board. Shape is worth modelling for a reason size classes are not:
# a vision model flips between "bowl" and "large_bowl" on the same bowl, but
# it does not mistake a square plate for a round one.
# ---------------------------------------------------------------------------
def test_square_vessel_has_more_area_than_round():
    from app.services.ai.portion import vessel_area
    round_a = vessel_area("dinner_plate", "round")[0]
    square_a = vessel_area("dinner_plate", "square")[0]
    oval_a = vessel_area("dinner_plate", "oval")[0]
    assert square_a == pytest.approx(round_a * 4 / math.pi, rel=1e-6)   # +27%
    assert oval_a == pytest.approx(round_a * 0.75, rel=1e-6)            # -25%


def test_shape_moves_the_geometric_estimate():
    """Without a serving prior to blend against, the shape must come through
    at full strength -- otherwise modelling it is decoration."""
    def g(shape):
        return estimate_grams(
            name="rice", area_ratio=0.25,
            hint=GeometryHint(plate_ellipse_area_ratio=0.55, vessel="dinner_plate",
                              vessel_shape=shape),
            shape_hint="mound", density=0.78, ai_prior_grams=None,
            detection_confidence=0.8,
        ).grams
    assert g("square") == pytest.approx(g("round") * 4 / math.pi, rel=0.02)
    assert g("oval") < g("round") < g("square")


def test_vessel_defaults_match_their_real_geometry():
    """A clamshell is square and a tray is rectangular. Defaulting those to
    round would reintroduce the bug this fixes."""
    from app.services.ai.portion import vessel_area
    assert vessel_area("takeout_box")[0] == pytest.approx(190.0 ** 2)
    assert vessel_area("tray")[0] == pytest.approx(350.0 ** 2 * 0.75)
    assert vessel_area("dinner_plate")[0] == pytest.approx(math.pi * 135.0 ** 2)


def test_unreadable_shape_never_crashes():
    """Enum fields come back as numbers, lists and nulls often enough that a
    crash here would cost a user their scan for no benefit."""
    from app.services.ai.portion import vessel_area
    expected = vessel_area("dinner_plate", "round")[0]
    for junk in (None, "", "hexagonal", 42, 3.7, ["round"], {"s": 1}, "ROUND "):
        assert vessel_area("dinner_plate", junk)[0] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Occlusion.
#
# A photo cannot measure food it cannot see. When a plate is crowded -- which
# is exactly when it is most likely over-served -- food gets stacked, and the
# visible area of a buried item is a floor, not a measurement. The vision model
# was already reporting this and the pipeline discarded it, so those meals were
# silently under-counted. For an app built to flag overeating, and whose carb
# figures come from grams, under-counting is the harmful direction.
# ---------------------------------------------------------------------------
def _buried(visible):
    return estimate_grams(
        name="refried beans", area_ratio=0.09,
        hint=GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=269),
        shape_hint="mound", density=1.05, ai_prior_grams=100.0,
        detection_confidence=0.8, visible_fraction=visible,
    )


def test_hidden_food_raises_the_estimate():
    assert _buried(0.5).grams > _buried(1.0).grams
    assert _buried(0.5).occluded is True
    assert _buried(1.0).occluded is False


def test_more_hidden_never_means_less_food():
    """This failed when the correction was applied before the serving prior:
    90% visible gave 164 g and 75% visible gave 159 g. The prior describes a
    typical serving of what you can SEE, so it must be reconciled first and the
    burial applied to the result."""
    prev = None
    for vis in [1.0 - i / 100 for i in range(0, 70)]:
        g = _buried(vis).grams
        if prev is not None:
            assert g >= prev - 1e-9, f"visible={vis:.2f} produced less food than the step before"
        prev = g


def test_occluded_band_is_one_sided():
    """Hidden food can only add weight. A symmetric range would understate the
    risk of under-counting, which is the entire point of the correction."""
    e = _buried(0.5)
    below = 1 - e.grams_low / e.grams
    above = e.grams_high / e.grams - 1
    assert above > below * 1.5, (below, above)


def test_occlusion_costs_confidence():
    assert _buried(0.5).confidence < _buried(1.0).confidence


def test_extreme_occlusion_is_capped_not_extrapolated():
    """A model claiming 10% visible is guessing. Multiplying a guess by ten is
    worse than admitting the limit, so scaling stops and the range widens."""
    assert _buried(0.2).grams == pytest.approx(_buried(0.35).grams)
    assert _buried(0.05).grams <= _buried(0.35).grams * 1.01


def test_unreadable_visibility_is_ignored():
    baseline = _buried(1.0).grams
    for junk in (None, "half", -1, 0, 5, [0.5], float("nan")):
        e = _buried(junk)
        assert e.grams == pytest.approx(baseline)
        assert e.occluded is False


def test_occlusion_survives_reconciliation():
    """Averaging several views cannot reveal what none of them saw.

    A regression guard with history: adding the one-sided occlusion band to
    estimate_grams also injected its local variables into reconcile_multi_image,
    where they did not exist, and every multi-image scan raised NameError. The
    single-photo bench never touched that path, so only the suite caught it.
    """
    plate = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=270)
    hidden = estimate_grams(name="rice", area_ratio=0.15, hint=plate,
                            detection_confidence=0.7, visible_fraction=0.5)
    clear = estimate_grams(name="rice", area_ratio=0.15, hint=plate,
                           detection_confidence=0.7)

    merged = reconcile_multi_image([hidden, clear])
    assert merged.occluded is True
    above = merged.grams_high / merged.grams - 1
    below = 1 - merged.grams_low / merged.grams
    assert above > below, "an occluded reconciliation must stay one-sided"

    both_clear = reconcile_multi_image([clear, clear])
    assert both_clear.occluded is False


def test_the_rail_now_lands_on_the_box():
    """The box is the trustworthy number, and the rail is allowed to reach it.

    This test used to assert the opposite, on the inference that a drumstick
    lies diagonally so its box must exceed its area -- and that a rail which cut
    15% of frame to 5% had believed a wrong box. Two direct measurements say the
    box was right and the inference was wrong:

      14-plate-mole-chicken-card   the drumstick claimed 5% of frame, its box
          said 4%. Segmented against the plate at its measured 254 mm, the real
          footprint is 3.29% -- and box x 0.80 = 3.2%, right to within 3%.

      08c-plate-mole-chicken-tape  refried beans measured against a tape lying
          in the photo: 4145 mm2 of food inside an 855 x 840 px box. Fill 0.80,
          which is BBOX_FILL_CEILING exactly.

    Across the 11-meal bench, 20 of 22 items reported an area larger than their
    own box and not one reported a smaller area. A signal biased one way on 20
    of 22 samples is broken, not noisy, and blending it with the box carried
    half the break through.
    """
    hint = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=254)
    est = estimate_grams(
        name="chicken drumstick", area_ratio=0.15, hint=hint, shape_hint="mound",
        density=1.05, ai_prior_grams=150.0, detection_confidence=0.8,
        bbox={"w": 0.25, "h": 0.25},          # a 5% box against a 15% claim
    )
    # Railing ALL the way to the bare box was tried and measured: per-item error
    # fell 44.7% -> 25.0%, but the meal bias went -0.3% -> -15.2%, because the
    # boxes are themselves undersized (08: box 4.00% of frame against a real
    # 4.84%; 14: box 3.00% against 4.50%).
    #
    # The answer to that is BOX_TIGHTNESS -- one flat allowance on the ceiling,
    # the same for every item. It was a partway cut toward the reported area,
    # which made the answer climb with the exaggeration it was supposed to be
    # correcting. What it lands on must depend on the BOX and nothing else.
    from app.services.ai.portion import BOX_TIGHTNESS
    ceiling = 0.25 * 0.25 * BBOX_FILL_CEILING
    assert est.pixel_area_ratio == pytest.approx(ceiling * BOX_TIGHTNESS)
    assert est.pixel_area_ratio > ceiling
    assert any("bounding box" in n for n in est.notes), est.notes

    # The same box, told a wilder story, must not buy more food.
    louder = estimate_grams(
        name="chicken drumstick", area_ratio=0.45, hint=hint, shape_hint="mound",
        density=1.05, ai_prior_grams=150.0, detection_confidence=0.8,
        bbox={"w": 0.25, "h": 0.25},
    )
    assert louder.pixel_area_ratio == pytest.approx(est.pixel_area_ratio)
    assert louder.grams == pytest.approx(est.grams, rel=1e-6)
    # It costs confidence instead, which is where a self-contradiction belongs.
    assert louder.confidence < est.confidence


def test_a_fourfold_disagreement_hands_the_box_the_whole_answer():
    """The floor protects items whose two numbers roughly agree. It must not
    protect an item whose numbers cannot both be about the same food.

    Measured on the weighed bench: every item over 4x came back enormously
    heavy -- cherry tomatoes at +147% and +174%, roasted potatoes at +88% --
    while everything under 3.5x was within 30% and photo 07's four-item plate
    was within 6% per item. Nothing sat between 3.5x and 4.4x.
    """
    from app.services.ai.portion import (
        BBOX_FILL_CEILING, BOX_TIGHTNESS, railed_area,
    )
    box = {"w": 0.10, "h": 0.10}                     # 1% of frame
    ceiling = 0.01 * BBOX_FILL_CEILING * BOX_TIGHTNESS

    # 5% claimed against a 1% box: 6.2x. The cherry-tomato case. The box takes
    # the whole answer.
    assert railed_area(0.05, box) == pytest.approx(ceiling)
    # And so does a claim a hair on the other side of where the old cliff was.
    assert railed_area(0.04, box) == pytest.approx(ceiling)
    assert railed_area(0.03, box) == pytest.approx(ceiling)


def test_a_wilder_claim_never_buys_a_bigger_portion():
    """The refried-beans defect, in one line.

    The rail used to fall back to `area x 0.5` -- a fraction of the number it
    had just discarded -- so the answer CLIMBED with the model's exaggeration
    until the absurd line threw the claim out, at which point it fell by half.
    Two photos of one scoop of beans landed either side of that cliff, at
    ratios 3.4 and 4.4, and came back +89% and -20% against the same scale.

    The rail's answer must never increase when the only thing that increased is
    a claim the rail does not believe.
    """
    from app.services.ai.portion import railed_area
    for side in (0.05, 0.10, 0.20, 0.35, 0.50):
        box = {"w": side, "h": side}
        previous = None
        for step in range(1, 121):
            area = step * 0.005
            got = railed_area(area, box)
            if previous is not None:
                assert got >= previous - 1e-12, (
                    f"box {side}x{side}: claiming {area:.1%} instead of "
                    f"{area - 0.005:.1%} of frame LOWERED the answer "
                    f"{previous:.4%} -> {got:.4%}"
                )
                # ...and once the rail is on the box, it must stop moving at all.
                if previous == railed_area(area + 0.5, box):
                    assert got == pytest.approx(previous), (
                        f"box {side}x{side}: the answer is still tracking a "
                        f"claim the rail has already capped"
                    )
            previous = got


def test_the_rail_lands_on_the_box_whatever_the_disagreement():
    """One answer for every over-claim, so there is no threshold for two
    photographs of the same food to sit either side of."""
    from app.services.ai.portion import BBOX_FILL_CEILING, BOX_TIGHTNESS, railed_area
    for side in (0.10, 0.20, 0.30):
        box = {"w": side, "h": side}
        ceiling = side * side * BBOX_FILL_CEILING * BOX_TIGHTNESS
        landings = {
            round(railed_area(ceiling * r, box), 9)
            for r in (1.1, 1.5, 2.0, 2.5, 3.0, 3.5, 3.9, 4.0, 4.1, 5.0, 8.0, 25.0)
        }
        assert landings == {round(ceiling, 9)}, (
            f"box {side}x{side} produced {len(landings)} different answers "
            f"for the same box: {sorted(landings)}"
        )


def test_a_discarded_candidate_stays_inside_the_band():
    """A percentage band cannot say "this might be several times too small",
    and that silence blocked a correct rescue.

    Photo 12, cherry tomatoes: the model claimed 5% of frame and boxed them at
    0.4% -- a 25x disagreement -- so the box won and the estimate came out
    7.7 g against a weighed 27 g. The band was 5-10 g, and the reasoning stage,
    which had correctly asked for 25 g, was clamped down to 10 g BY that band.
    The pipeline distrusted the number enough to halve its confidence and still
    gave it a range tight enough to defend itself.
    """
    hint = GeometryHint(depth_mm=330.0, camera_fov_deg=68.0, aspect_ratio=0.75)
    est = estimate_grams(
        name="cherry tomatoes", area_ratio=0.05, hint=hint, shape_hint="cluster",
        density=0.6, ai_prior_grams=50.0, detection_confidence=0.7,
        bbox={"w": 0.065, "h": 0.065},
    )
    assert est.grams_high >= est.grams * 2.5, "the discarded answer is unreachable"
    assert est.grams_low <= est.grams


def test_an_ordinary_blend_keeps_an_ordinary_band():
    """The widening is only for the case where two answers disagreed several
    fold. A routine correction is not a coin toss, and putting a +/-100% band on
    a plate of rice the pipeline is fairly sure about would be worse than the
    bug it fixes."""
    hint = GeometryHint(plate_ellipse_area_ratio=0.42, plate_diameter_mm=254,
                        aspect_ratio=0.75)
    est = estimate_grams(
        name="mexican rice", area_ratio=0.09, hint=hint, shape_hint="mound",
        density=1.0, ai_prior_grams=150.0, detection_confidence=0.8,
        bbox={"w": 0.2, "h": 0.2},
    )
    assert est.grams_high < est.grams * 1.5, "an ordinary item got a panic band"


def test_the_rail_only_ever_cuts_however_big_the_disagreement():
    from app.services.ai.portion import railed_area
    for area in (0.001, 0.01, 0.05, 0.30, 0.90):
        for side in (0.05, 0.2, 0.5, 0.95):
            got = railed_area(area, {"w": side, "h": side})
            assert got <= area + 1e-12, "the rail inflated an estimate"
            assert got >= 0.0


def test_an_area_already_inside_its_box_is_left_alone():
    """The rail only ever cuts. A modest claim is not inflated up to the box."""
    hint = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=254)
    est = estimate_grams(
        name="rice", area_ratio=0.02, hint=hint, shape_hint="mound",
        density=1.05, ai_prior_grams=150.0, detection_confidence=0.8,
        bbox={"w": 0.30, "h": 0.30},          # an 9% ceiling against a 2% claim
    )
    assert est.pixel_area_ratio == pytest.approx(0.02)


def test_mild_bbox_excess_is_still_capped_to_the_box():
    """Within a factor of two the box is still the better number, and an item
    genuinely cannot exceed its own bounding box."""
    hint = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=269)
    est = estimate_grams(
        name="rice", area_ratio=0.30, hint=hint, shape_hint="mound",
        density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
        bbox={"w": 0.60, "h": 0.55},          # 33% box -> 26% ceiling
    )
    from app.services.ai.portion import BOX_TIGHTNESS
    assert est.pixel_area_ratio == pytest.approx(
        0.60 * 0.55 * BBOX_FILL_CEILING * BOX_TIGHTNESS, rel=1e-3)


def test_a_big_disagreement_costs_more_confidence_than_a_small_one():
    hint = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=269)
    mild = estimate_grams(name="rice", area_ratio=0.30, hint=hint, shape_hint="mound",
                          density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
                          bbox={"w": 0.60, "h": 0.55})
    severe = estimate_grams(name="rice", area_ratio=0.30, hint=hint, shape_hint="mound",
                            density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
                            bbox={"w": 0.20, "h": 0.20})
    assert severe.confidence < mild.confidence


def test_confidence_falls_all_the_way_down_the_disagreement_range():
    """Not just at the two points the test above happens to pick.

    The bug this guards against: when the rail was changed to always land on
    the box, the two flat penalties collapsed into one -- every disagreement
    cost exactly 0.85 regardless of size -- and a 9x self-contradiction came
    back MORE confident than a 1.1x one, because the only penalty still varying
    was an unrelated one about the serving prior. Two sample points caught it by
    luck. A sweep catches it on purpose.
    """
    hint = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=254)
    last = 1.0
    for side in (0.62, 0.58, 0.54, 0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15):
        est = estimate_grams(
            name="rice", area_ratio=0.30, hint=hint, shape_hint="mound",
            density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
            bbox={"w": side, "h": side},
        )
        assert est.confidence <= last + 1e-9, (
            f"a {0.30 / (side * side * 0.80):.1f}x disagreement came back more "
            f"confident than a smaller one"
        )
        last = est.confidence


def test_a_wild_disagreement_says_so_in_words():
    """The user is handed the box's answer either way. They should be able to
    tell from the note whether the photo agreed with itself."""
    hint = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=254)
    severe = estimate_grams(
        name="rice", area_ratio=0.30, hint=hint, shape_hint="mound", density=0.78,
        ai_prior_grams=150.0, detection_confidence=0.8, bbox={"w": 0.20, "h": 0.20},
    )
    mild = estimate_grams(
        name="rice", area_ratio=0.30, hint=hint, shape_hint="mound", density=0.78,
        ai_prior_grams=150.0, detection_confidence=0.8, bbox={"w": 0.60, "h": 0.55},
    )
    assert any("disagree by" in n and "something was misread" in n for n in severe.notes)
    assert any("capped to the box" in n for n in mild.notes)
    assert not any("something was misread" in n for n in mild.notes)


# ---------------------------------------------------------------------------
# Shape classification.
#
# Anything the keyword fallback misses lands on "default", a middling prior
# that is wrong for most specific foods. Auditing the meals photographed during
# testing, five of eight fell through -- scrambled eggs, refried beans,
# spaghetti, alfredo, a drumstick -- so every one of them was being sized by a
# generic guess.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("food,expected", [
    # structure beats ingredient -- both of these were misclassified when the
    # ingredient lists were tested first
    ("cheesesteak sandwich", "wrapped"),
    ("falafel wrap", "wrapped"),
    # ...but a taco is not a burrito. This line used to read "wrapped", which
    # is where the +88% on a weighed taco meal came from: 42 mm tall with a
    # 0.85 profile, priors built for a fat cylinder.
    ("beef tacos", "topped_flat"),
    # sauce-based dishes, however solid their contents
    ("chicken tikka masala", "liquid"),
    ("bean soup", "liquid"),
    ("greek yogurt", "liquid"),
    # long thin food that settles rather than heaps
    ("spaghetti with chicken", "loose"),
    ("fettuccine alfredo", "loose"),
    ("french fries", "loose"),
    # single solid pieces -- but a slab and a whole bone-in piece are not the
    # same solid. This line read "flat" until a drumstick was weighed at 146 g
    # and measured against a credit card: 4,600 mm2 of footprint and 30 mm of
    # mean height, where "flat" assumes an 18 mm slab and came out 46% light.
    ("chicken drumstick", "chunky"),
    ("grilled salmon", "flat"),
    ("pork chop", "flat"),
    # soft food that holds a heap
    ("scrambled eggs", "mound"),
    ("refried beans", "mound"),
    ("mexican rice", "mound"),
    ("hummus", "mound"),
    # many small separate pieces
    ("broccoli florets", "cluster"),
])
def test_shape_classification(food, expected):
    from app.services.ai.portion import _classify_shape
    assert _classify_shape(food, None) == expected


def test_the_models_own_shape_beats_the_keywords():
    """The keyword list is a fallback for foods whose NAME says nothing
    structural. If the model looked at the photo and said liquid, it saw
    something the name does not carry -- and for a pile of rice or a scatter of
    berries, that is exactly the judgement it is good at.

    The exception is structure, and this test used to assert the opposite:
    "cheesesteak sandwich" reported as liquid returned liquid. A sandwich is a
    sandwich whatever word came back, and the same rule is what stopped a taco
    reported as "flat" being sized like a steak.
    """
    from app.services.ai.portion import _classify_shape
    assert _classify_shape("cheesesteak sandwich", "liquid") == "wrapped"
    assert _classify_shape("anything at all", "cluster") == "cluster"


def test_unknown_food_still_falls_through_safely():
    from app.services.ai.portion import _classify_shape
    assert _classify_shape("zzzyx", None) == "default"


# ---------------------------------------------------------------------------
# The prior's pull scales with the quality of the geometry.
#
# It used to be a flat geometric mean regardless of where the scale came from.
# On a weighed plate that overruled a good measurement: with a calibrated
# 269 mm plate the geometry put the refried beans at 66 g against a weighed
# 79 g, and the blend dragged it to 92 g toward a generic 150 g serving prior --
# turning a 16% under-read into a 17% over-read for no new information.
# ---------------------------------------------------------------------------
def _blend_result(method_hint, geom_area, prior):
    """Drive a real estimate onto a chosen rung and read the outcome."""
    hints = {
        "plate_reference": GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=269),
        "pixel_area": GeometryHint(plate_ellipse_area_ratio=0.55),
        "vessel_reference": GeometryHint(plate_ellipse_area_ratio=0.55, vessel="bowl"),
    }
    return estimate_grams(
        name="refried beans", area_ratio=geom_area, hint=hints[method_hint],
        shape_hint="mound", density=1.05, ai_prior_grams=prior,
        detection_confidence=0.8,
    )


def test_a_measured_plate_resists_the_serving_prior():
    """A prior substitutes for information. The more real information the
    geometry carries, the less substitution is warranted."""
    from app.services.ai.portion import BLEND_MAX_WEIGHT_BY_METHOD as W
    assert W["plate_reference"] < W["vessel_reference"] < W["pixel_area"]
    assert W["depth_model"] < W["pixel_area"]


def test_weak_geometry_still_gets_rescued_by_the_prior():
    """The blend must not be gutted. Where the scale is an assumption, an
    absurd geometric answer should still be pulled back hard."""
    absurd = estimate_grams(
        name="rice", area_ratio=0.52, hint=GeometryHint(plate_ellipse_area_ratio=0.55),
        shape_hint="mound", density=0.78, ai_prior_grams=150.0, detection_confidence=0.8,
    )
    assert absurd.grams < 600, "a wild pixel_area estimate should still be reined in"


def test_good_geometry_is_no_longer_non_monotone():
    """A side effect of the weaker pull: below about 0.36 the blend becomes
    monotone, so the 'more food reads as less food' artifact disappears on the
    trustworthy rungs and survives only where the geometry was weak anyway."""
    hint = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=269)
    for i in range(2, 54):
        lo = estimate_grams(name="rice", area_ratio=i / 100, hint=hint, shape_hint="mound",
                            density=0.78, ai_prior_grams=150.0).grams
        hi = estimate_grams(name="rice", area_ratio=(i + 1) / 100, hint=hint, shape_hint="mound",
                            density=0.78, ai_prior_grams=150.0).grams
        assert hi >= lo - 1e-9, f"plate_reference dipped at area {i/100:.2f}"


# ---------------------------------------------------------------------------
# Food groups.
#
# Knowing what KIND of food something is does three jobs a name cannot: it
# stops wrong merges, it supplies a density when the database has none, and it
# makes the calorie figure checkable against physics. A cheesesteak came back
# at 5 kcal/g during testing -- denser than sugar -- and nothing noticed,
# because there was nothing to compare it against.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label,kcal,grams,group", [
    ("a sandwich matched as crackers", 1748, 350, "composite"),
    ("the same after correction", 1254, 250, "composite"),
    ("lettuce matched as nuts", 240, 40, "vegetable"),
    ("denser than pure fat", 1000, 100, "protein"),
])
def test_impossible_energy_density_is_flagged(label, kcal, grams, group):
    from app.services.ai.portion import implausible_energy
    assert implausible_energy(kcal, grams, group) is not None, label


@pytest.mark.parametrize("label,kcal,grams,group", [
    ("grilled chicken", 165, 100, "protein"), ("bacon", 540, 100, "protein"),
    ("cooked rice", 130, 100, "grain"), ("white bread", 265, 100, "grain"),
    ("cheddar", 400, 100, "dairy"), ("olive oil", 884, 100, "fat_oil"),
    ("butter", 717, 100, "fat_oil"), ("potato crisps", 536, 100, "snack"),
    ("almonds", 579, 100, "nuts_seeds"), ("dark chocolate", 600, 100, "sweet"),
    ("pizza", 270, 100, "composite"), ("lasagna", 150, 100, "composite"),
    ("cheeseburger", 250, 100, "composite"), ("lettuce", 15, 100, "vegetable"),
    ("banana", 89, 100, "fruit"), ("hummus", 170, 100, "legume"),
    ("orange juice", 45, 100, "beverage"), ("french fries", 312, 100, "composite"),
])
def test_ordinary_food_is_not_flagged(label, kcal, grams, group):
    """A checker that cries wolf gets ignored. These are all real foods at
    their real energy densities."""
    from app.services.ai.portion import implausible_energy
    assert implausible_energy(kcal, grams, group) is None, label


def test_group_density_beats_one_number_for_everything():
    from app.services.ai.portion import group_density
    assert group_density("vegetable") < group_density("grain") < group_density("protein")
    assert group_density(None) == group_density("composite")
    assert group_density("not_a_group") == group_density("composite")


def test_unknown_group_falls_back_without_crashing():
    from app.services.ai.portion import food_group, implausible_energy
    for junk in (None, "", "zzz", 42, ["protein"]):
        assert food_group(junk) == "composite"
        implausible_energy(200, 100, junk)   # must not raise


def test_energy_check_ignores_missing_data():
    from app.services.ai.portion import implausible_energy
    assert implausible_energy(0, 100, "protein") is None
    assert implausible_energy(200, 0, "protein") is None


def test_energy_check_runs_after_macros_exist():
    """A guard against the ordering mistake that shipped a 500.

    The energy check was inserted above the line that computes `macros`, so
    every scan raised UnboundLocalError. The unit tests all passed -- they
    exercise implausible_energy directly and never run build_items -- so the
    failure only appeared against a live photo. This pins the invariant that
    the check reads a value which is already defined.
    """
    import inspect
    from app.services.ai import vision
    src = inspect.getsource(vision.build_items)
    assert src.index("macros = resolver.macros_for") < src.index("implausible_energy("), (
        "the energy check must come after macros are computed"
    )


# ---------------------------------------------------------------------------
# Vessels with walls.
#
# The spread correction assumes food spreads across a flat surface: cover more
# area, be shallower. In a bowl the walls hold it up, so filling 80% of the rim
# means the vessel is FULL -- deep, not thin. Measured on a 152 mm bowl holding
# 405 g of soup, the estimator needed an average depth of 26.8 mm and used
# 15.4 mm after damping a full bowl as though it were a puddle: -42% from that
# factor alone, and the reason every bowl and container read low while the
# flat plates were fine.
# ---------------------------------------------------------------------------
def test_a_full_bowl_is_deep_not_thin():
    hint = GeometryHint(plate_ellipse_area_ratio=0.60, plate_diameter_mm=152.0,
                        vessel="bowl")
    est = estimate_grams(name="bean soup", area_ratio=0.50, hint=hint,
                         shape_hint="liquid", density=1.0, ai_prior_grams=250.0,
                         detection_confidence=0.75)
    # "height was scaled" is emitted only when the correction actually fires.
    # Testing for the absence of "spread thin" fails on the note that explains
    # why it did not -- the phrase appears in both.
    assert not any("height was scaled" in n for n in est.notes), est.notes
    assert any("walls" in n for n in est.notes), est.notes


def test_walled_vessels_are_all_covered():
    """Bowls, takeout containers and cups all have walls. A plate does not."""
    from app.services.ai.portion import WALLED_VESSELS
    for v in ("bowl", "large_bowl", "takeout_box", "clamshell", "cup", "mug"):
        assert v in WALLED_VESSELS
    for v in ("dinner_plate", "side_plate", "tray", "cutting_board"):
        assert v not in WALLED_VESSELS


def test_a_plate_still_gets_the_spread_correction():
    """The correction is right on a flat surface and must survive there."""
    hint = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=269)
    est = estimate_grams(name="mexican rice", area_ratio=0.40, hint=hint,
                         shape_hint="mound", density=0.78, ai_prior_grams=150.0,
                         detection_confidence=0.8)
    assert any("height was scaled" in n for n in est.notes), est.notes


def test_liquid_anywhere_is_never_thinned():
    """Soup is deep wherever it is. The shape alone is enough to disable it."""
    hint = GeometryHint(plate_ellipse_area_ratio=0.60, plate_diameter_mm=200)
    est = estimate_grams(name="tomato soup", area_ratio=0.45, hint=hint,
                         shape_hint="liquid", density=1.0, ai_prior_grams=300.0,
                         detection_confidence=0.8)
    # "height was scaled" appears only when the correction actually fires. The
    # phrase "spread thin" also appears in the note explaining why it did not.
    assert not any("height was scaled" in n for n in est.notes), est.notes


# ---------------------------------------------------------------------------
# Reference objects, measured.
#
# Two meals never improved through any change to densities, heights, shape
# factors or the blend: a cheesesteak on butcher paper and pasta in a takeout
# container. Neither has a plate, and a plate outline is where every other rung
# gets its scale.
#
# Asking the vision model to measure a credit card failed three ways on one
# photo -- reported length 0.18 against a true 0.362, a box half the right
# size, food area 35% of frame against a measured 12.8%. The card is now found
# in the pixels instead (reference_cv.py) and this module is handed a frame
# width in millimetres. These tests cover what portion.py does with it.
# ---------------------------------------------------------------------------
def test_a_measured_reference_sizes_a_photo_with_no_plate():
    """Food on paper has no vessel to measure. A card found in the pixels is
    the only real dimension in the photo -- and an exact one."""
    nothing, method = mm2_per_frame(GeometryHint(vessel="paper"))
    assert nothing is None and method == "ai_prior"

    area, method = mm2_per_frame(GeometryHint(
        vessel="paper", reference_kind="credit_card",
        reference_frame_width_mm=236.0, aspect_ratio=0.75,
    ))
    assert method == "reference_object"
    assert area == pytest.approx(236.0 * 236.0 / 0.75, rel=0.01)


def test_a_measurement_outranks_a_vessel_prior():
    """A takeout clamshell's size is 'whatever the supplier sells'. A credit
    card is 85.60 mm by international standard, and we found it ourselves."""
    hint = GeometryHint(
        vessel="clamshell", vessel_shape="square", plate_ellipse_area_ratio=0.5,
        reference_kind="credit_card", reference_frame_width_mm=300.0,
        aspect_ratio=0.75,
    )
    assert mm2_per_frame(hint)[1] == "reference_object"


@pytest.mark.parametrize("width_mm", [95.0, 1250.0])
def test_an_impossible_frame_is_refused_not_clamped(width_mm):
    """A phone cannot focus closer than a ~140 mm frame, and past a metre you
    are photographing a table rather than a meal. Clamping junk into range
    would turn obvious nonsense into a confident wrong answer -- and here that
    answer is the scale of everything in the photo."""
    assert mm2_per_frame(GeometryHint(
        vessel="paper", reference_kind="credit_card",
        reference_frame_width_mm=width_mm, aspect_ratio=0.75,
    ))[1] == "ai_prior"


def test_the_taco_photo_end_to_end():
    """The meal this rung exists for, with its real numbers.

    Two breakfast tacos on a paper towel, 133 g on a kitchen scale, a credit
    card lying flat beside them. The detector measures the frame at 252 mm
    across (236 hand-measured off the pixels). Before this rung the same photo
    had no scale at all and fell to a typical-serving guess.
    """
    area, method = mm2_per_frame(GeometryHint(
        vessel="paper", reference_kind="credit_card",
        reference_frame_width_mm=252.2, aspect_ratio=0.75,
    ))
    assert method == "reference_object"
    # A frame 252 mm across and 336 tall is about 848 cm2 of tabletop.
    assert area / 100.0 == pytest.approx(848.0, rel=0.05)


def test_the_new_rung_is_in_every_ladder_table():
    """Three tables are keyed by method name and two are indexed directly. A
    method missing from any one of them is a KeyError on a real photograph, not
    a test failure."""
    from app.services.ai.portion import (
        _METHOD_BAND, _METHOD_CEILING, BLEND_MAX_WEIGHT_BY_METHOD as W,
    )
    assert set(_METHOD_CEILING) == set(_METHOD_BAND) == set(W)
    assert _METHOD_CEILING["vessel_reference"] < _METHOD_CEILING["reference_object"] < _METHOD_CEILING["plate_reference"]
    assert W["reference_object"] < W["vessel_reference"]


# ---------------------------------------------------------------------------
# Open flatbreads are not wraps.
#
# "taco" matched the wrapped list, which is built for burritos: a 42 mm tall
# cylinder with a 0.85 profile. An open taco is a 2 mm disc of tortilla with a
# mound on part of it. Measured on two breakfast tacos weighing 133 g, with the
# footprint read off the photograph at 22.7% of a frame scaled by a credit card
# in the shot, the estimator was using height x profile x density of 36.8 where
# the physics asks for 6.9 -- and the meal read +88%.
# ---------------------------------------------------------------------------
def test_a_taco_is_not_a_burrito():
    """The classification, not the constant. No number tuned here fixes a food
    being put in the wrong category."""
    assert _classify_shape("taco with scrambled eggs and vegetables", None) == "topped_flat"
    assert _classify_shape("chicken burrito", None) == "wrapped"


@pytest.mark.parametrize("name", [
    "tostada", "pizza slice", "avocado toast", "open-faced sandwich", "nachos",
])
def test_open_flatbreads_are_flat(name):
    assert _classify_shape(name, None) == "topped_flat"


@pytest.mark.parametrize("name", [
    "chicken burrito", "turkey sandwich", "cheesesteak sandwich",
    "california roll", "falafel wrap",
])
def test_closed_things_stay_tall(name):
    """Two slices and a filling really is a tall object. Only the OPEN ones
    moved."""
    assert _classify_shape(name, None) == "wrapped"


def test_the_flatbread_prior_matches_a_weighed_taco():
    """Built from its parts -- a ~2 mm tortilla and a ~14 mm filling mound,
    with most of the disc bare -- and checked against the scale afterwards,
    not solved backwards from it."""
    from app.services.ai.portion import HEIGHT_PRIORS_MM, PROFILE_FACTORS
    derived = HEIGHT_PRIORS_MM["topped_flat"] * PROFILE_FACTORS["topped_flat"] * 1.03
    assert derived == pytest.approx(6.9, rel=0.15)


def test_every_shape_exists_in_every_shape_table():
    """Three tables keyed by shape, all indexed directly. A shape missing from
    one of them is a KeyError on a real photograph."""
    from app.services.ai.portion import (
        HEIGHT_PRIORS_MM, PROFILE_FACTORS, SHAPE_FACTORS,
    )
    assert set(SHAPE_FACTORS) == set(PROFILE_FACTORS) == set(HEIGHT_PRIORS_MM)


# ---------------------------------------------------------------------------
# A dish is not its ingredients.
#
# Density was matched anywhere in the reported name, so a composite dish
# inherited whichever ingredient it happened to mention:
#
#   "taco with scrambled eggs"     matched "egg"    -> 1.03 g/ml
#   "baked spaghetti with cheese"  matched "cheese" -> 1.05 g/ml
#
# 1.03 is denser than water, for a food that is corn tortilla and fluffy egg.
# On the bench, "baked spaghetti with cheese" read +20.9% against a weighed
# 133 g. Whatever follows "with" or "in" is a filling or a sauce; it is not
# what the dish weighs per millilitre.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("taco with scrambled eggs and vegetables", "taco"),
    ("chicken drumstick in mole", "chicken drumstick"),
    ("baked spaghetti with cheese", "baked spaghetti"),
    ("pancakes topped with berries", "pancakes"),
    ("rice, beans, salsa", "rice"),
    # No separator: the whole label is the dish.
    ("refried beans", "refried beans"),
    ("boiled bean soup", "boiled bean soup"),
])
def test_the_dish_is_the_head_of_the_name(name, expected):
    from app.services.ai.portion import dish_head
    assert dish_head(name) == expected


def test_and_is_not_a_separator():
    """'steak and cheese sandwich' is a sandwich. Cutting at 'and' would leave
    the head as 'steak' and make the reading worse, not better."""
    from app.services.ai.portion import dish_head
    assert dish_head("steak and cheese sandwich") == "steak and cheese sandwich"


def test_a_composite_dish_does_not_inherit_an_ingredient_density():
    """The bug, with the two real names it was found on."""
    assert density_for("taco with scrambled eggs and vegetables", None, "composite") < 1.0
    assert density_for("baked spaghetti with cheese", None, "composite") < 1.0


def test_a_sauce_does_not_replace_the_food_it_is_on():
    """A drumstick in mole is a drumstick. This one was already right and must
    stay right -- the fix must not move foods whose head does match."""
    assert density_for("chicken drumstick in mole", None, "protein") == pytest.approx(
        density_for("chicken drumstick", None, "protein")
    )


def test_the_group_answers_when_the_dish_is_unknown():
    """Not the trailing ingredient. 'Composite' is an honest mixture density;
    'egg' is a wrong one that looks precise."""
    from app.services.ai.portion import group_density
    assert density_for("taco with scrambled eggs", None, "composite") == pytest.approx(
        group_density("composite")
    )


def test_with_no_group_a_name_match_still_beats_the_default():
    """Last resort. Without a group there is nothing better, and an ingredient
    match is still more informative than the global default."""
    assert density_for("something with rice", None, None) == pytest.approx(
        density_for("rice", None, None)
    )


# ---------------------------------------------------------------------------
# THE DENSITY LOOKUP ACCEPTANCE TABLE -- docs/HANDOFF.md, 12 Sep 2026.
#
# Every food name the bench and the weighed-plate fits use. Checked with no
# group, which is how the heights were fitted. The old rule failed 18 of these.
#
# Where this departs from the brief's table, it is on purpose:
#   * brussels sprouts, grapes, trail mix, spaghetti squash take USDA cup
#     weights (the table's own rule) rather than the brief's reasoned values
#   * steamed carrots and steamed zucchini stay at the 0.85 default: they are
#     the two foods SEPARATE_PIECES_HEIGHT_MM was solved against, and it is
#     frozen. See test_the_calibration_foods_keep_their_fitted_densities.
#   * the squash row expects 0.655, not pasta's 0.65, so it can only pass
#     through the squash key. It is the case a later-word rule gets wrong.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("bbq chicken thigh", 1.05),
    ("beef posole", 1.02),
    ("brussels sprouts", 0.659),
    ("caesar salad", 0.22),
    ("cheese and broccoli soup", 1.00),
    ("cheeseburger slider", 0.55),
    ("chicken drumstick with mole sauce", 1.05),
    ("chicken drumstick, grilled with sauce", 1.05),
    ("chicken drumstick, rotisserie", 1.05),
    ("chicken noodle soup", 1.00),
    ("dinner roll", 0.28),
    ("egg, whole, cooked, scrambled", 1.03),
    ("fried french fries", 0.42),
    ("grapes", 0.638),
    ("grilled cheeseburger", 0.55),
    ("grilled chicken drumstick", 1.05),
    ("macaroni salad", 0.85),
    ("mexican rice", 0.67),
    ("pizza slice", 0.55),
    ("pot roast", 1.05),
    ("potato, french fries, from fresh, fried", 0.42),
    ("refried beans", 1.06),
    ("roast beef", 1.05),
    ("smashed potatoes", 1.04),
    ("spaghetti", 0.65),
    ("spaghetti with chicken", 0.65),
    ("spaghetti with sauce", 0.65),
    ("squash, winter, spaghetti, cooked, boiled, drained, or baked, with salt", 0.655),
    ("steamed carrots", 0.85),
    ("steamed zucchini", 0.85),
    ("tortilla chips", 0.18),
    ("trail mix", 0.634),
    ("white rice", 0.67),
    ("whole plate", 0.85),
])
def test_density_lookup_acceptance(name, expected):
    assert density_for(name) == pytest.approx(expected)


@pytest.mark.parametrize("name,key", [
    ("blueberries", "berries"),          # the end of a word is its head
    ("catfish", "fish"),
    ("refried pinto beans", "refried"),  # a preparation beats a commodity
    ("potato soup", "soup"),             # the later word is the head
    ("chocolate cake", "cake"),
    ("potatoes, mashed", "mashed potato"),
])
def test_the_head_of_the_name_decides_the_density(name, key):
    from app.services.ai.portion import DENSITY_G_ML
    assert density_for(name) == pytest.approx(DENSITY_G_ML[key])


@pytest.mark.parametrize("name", ["eggplant", "cheesecake sampler plate"])
def test_a_modifier_inside_a_word_is_not_the_food(name):
    """'egg' in 'eggplant' and 'cheese' in 'cheesecake' are modifiers."""
    from app.services.ai.portion import DENSITY_G_ML
    assert density_for(name) not in (DENSITY_G_ML["egg"], DENSITY_G_ML["cheese"])


def test_the_comma_still_cuts_the_dish_for_everything_else():
    """density_for no longer cuts at the comma; dish_head must still, because
    shape classification and the rename guard read it and were not re-tested."""
    from app.services.ai.portion import dish_head
    assert dish_head("potato, french fries, from fresh, fried") == "potato"


def test_the_calibration_foods_keep_their_fitted_densities():
    """The two height constants are MEANS of grams / (area x density) over the
    weighed footprints, at the densities these names resolve to. Move one of
    those densities and the frozen height silently stops describing its own
    evidence -- the trap that caught the mask cache and the shape factors.
    Refit and change the density in the SAME commit, or neither."""
    from app.services.ai import portion as P
    for one_mass, height in ((True, P.CONNECTED_PILE_HEIGHT_MM),
                             (False, P.SEPARATE_PIECES_HEIGHT_MM)):
        implied = [grams / density_for(name) * 1000.0 / area
                   for name, grams, area, o in WEIGHED_FOOTPRINTS if o == one_mass]
        mean = sum(implied) / len(implied)
        assert mean == pytest.approx(height, abs=0.05), (
            f"{'pile' if one_mass else 'pieces'}: fitted {height} mm, the "
            f"densities now imply {mean:.2f} mm")


def test_structure_beats_the_models_shape_word():
    """The model picks from six words and none of them is 'open flatbread'.

    Asked about two open tacos it answered "flat" -- not unreasonable, still
    wrong: "flat" is the prior for a steak, a slab of near-uniform height.
    Measured, that put a 133 g taco meal at 233 g. It cost a whole test cycle
    because the shape class was added but the hint still won.
    """
    for said in (None, "flat", "mound", "wrapped", "loose", "cluster"):
        assert _classify_shape("taco with scrambled eggs", said) == "topped_flat", said
    for said in (None, "flat", "loose"):
        assert _classify_shape("chicken burrito", said) == "wrapped", said


@pytest.mark.parametrize("name,said,expected", [
    ("mexican rice", "mound", "mound"),
    ("mexican rice", "loose", "loose"),
    ("grilled steak", "flat", "flat"),
    ("scrambled eggs", "mound", "mound"),
])
def test_the_model_still_decides_where_the_name_says_nothing(name, said, expected):
    """Only STRUCTURE overrides it. How high a pile of rice sits is exactly the
    kind of thing the photo answers and a keyword cannot."""
    assert _classify_shape(name, said) == expected


# ---------------------------------------------------------------------------
# Findings from the full audit. Each of these was a live defect.
# ---------------------------------------------------------------------------
def test_a_measured_reference_outranks_depth():
    """The ceiling table puts reference_object at 0.88 and depth_model at 0.84.
    The rung order contradicted it, so any phone reporting an ARKit distance
    silently beat a measured credit card -- 3.5x in frame area."""
    both = GeometryHint(depth_mm=350.0, reference_kind="credit_card",
                        reference_frame_width_mm=252.0, aspect_ratio=0.75)
    assert mm2_per_frame(both)[1] == "reference_object"
    assert mm2_per_frame(GeometryHint(depth_mm=350.0, aspect_ratio=0.75))[1] == "depth_model"


def test_tilt_is_measured_in_one_set_of_units():
    """`plate_ellipse` w and h are fractions of image WIDTH and HEIGHT. A round
    plate shot straight down in a 4:3 frame therefore reports h/w = 0.75, and
    reading that as cos(tilt) claimed 41 degrees of tilt on an overhead photo."""
    # A 240 mm plate in a 400x300 frame: w = 240/400 = 0.60, h = 240/300 = 0.80.
    # Straight down, so the answer must be zero tilt.
    overhead = GeometryHint(plate_ellipse_wh=(0.60, 0.80), aspect_ratio=4 / 3)
    assert overhead.tilt_deg == pytest.approx(0.0, abs=1.0)

    # Same plate at 45 degrees: its vertical extent shrinks by cos(45), so
    # h = 240 x 0.707 / 300 = 0.566.
    tilted = GeometryHint(plate_ellipse_wh=(0.60, 0.566), aspect_ratio=4 / 3)
    assert tilted.tilt_deg == pytest.approx(45.0, abs=3.0)

    # A portrait phone photo, straight down: 1176x1568, 600 px plate.
    portrait = GeometryHint(plate_ellipse_wh=(600 / 1176, 600 / 1568),
                            aspect_ratio=1176 / 1568)
    assert portrait.tilt_deg == pytest.approx(0.0, abs=1.0)

    # With no frame shape the two numbers are not comparable, so no answer.
    assert GeometryHint(plate_ellipse_wh=(0.60, 0.80)).tilt_deg is None


def test_more_hidden_food_never_means_less_food():
    """A visible_fraction below the floor was replaced by the DEFAULT of 1.0 --
    no occlusion at all -- so 6% visible inflated 2.9x while 2% visible was
    left alone."""
    def grams(vf):
        return estimate_grams(
            name="white rice", area_ratio=0.10,
            hint=GeometryHint(plate_ellipse_area_ratio=0.5, plate_diameter_mm=270),
            shape_hint="mound", visible_fraction=vf, detection_confidence=0.8,
        ).grams
    assert grams(0.02) >= grams(0.06) >= grams(1.0)


def test_a_sauce_does_not_decide_the_shape():
    """Matching the whole label let a side capture the shape: chicken with
    gravy became `liquid`, a 25 mm height with the spread correction off."""
    assert _classify_shape("grilled chicken with mashed potato and gravy", None) != "liquid"
    assert _classify_shape("grilled chicken breast with gravy", None) == "flat"
    # The sauce still decides when the sauce IS the dish.
    assert _classify_shape("gravy", None) == "liquid"
    assert _classify_shape("chicken curry", None) == "liquid"


def test_rolled_oats_are_not_a_burrito():
    """`roll` was matched as a bare substring."""
    assert _classify_shape("rolled oats with berries", None) != "wrapped"
    assert _classify_shape("california roll", None) == "wrapped"
    assert _classify_shape("spring roll", None) == "wrapped"


def test_the_shape_hint_is_normalised_like_every_other_label():
    """It was the only model-supplied label read raw, so "Mound" was dropped
    where "mound" was honoured -- and "default" is a table fallback, not a
    shape the model may claim."""
    assert _classify_shape("white rice", "Mound") == "mound"
    assert _classify_shape("white rice", " mound ") == "mound"
    assert _classify_shape("white rice", "default") == "mound"   # keyword ladder, not "default"


def test_multi_image_cannot_exceed_its_own_ceiling():
    """"An estimate can never be more certain than the scale it was built on"
    is enforced in estimate_grams and was not here: two confident plate
    estimates reconciled to 0.975 and were reported as "high"."""
    from app.services.ai.portion import PortionEstimate, _METHOD_CEILING
    e = PortionEstimate(grams=200.0, grams_low=180.0, grams_high=220.0,
                        method="plate_reference", confidence=0.92,
                        pixel_area_ratio=0.1, depth_factor=1.0, notes=[])
    out = reconcile_multi_image([e, e])
    assert out.confidence <= _METHOD_CEILING["multi_image"]


# ---------------------------------------------------------------------------
# A food is not tall because other foods are sharing its plate.
#
# REFERENCE_COVERAGE describes a PLATE -- "about a third of the surface is
# covered by food". Comparing one ITEM against it only asks the same question
# when that item is alone. Measured on the bench: with four foods sharing a
# plate each covered 8-12%, so every one was read as piled and inflated by the
# capped 1.10 -- on every item, on every plate photo, compounding into the
# meal total.
# ---------------------------------------------------------------------------
def _plate_item(area, plate_total=None):
    return estimate_grams(
        name="mexican rice", area_ratio=area,
        hint=GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=267),
        shape_hint="mound", food_group="grain", ai_prior_grams=150.0,
        detection_confidence=0.8, plate_food_coverage=plate_total,
    )


def test_sharing_a_plate_does_not_make_food_taller():
    """Four foods each covering a tenth of a normally-full plate. Judged item
    by item every one is 'piled'; judged as a plate, none is."""
    alone = _plate_item(0.055)                      # 10% of the plate, no total given
    sharing = _plate_item(0.055, plate_total=0.38)  # ...but the plate is 38% covered
    assert sharing.grams < alone.grams

    # A materially full plate says so out loud. (A plate close to normal moves
    # the number a little and stays quiet, which is the note threshold doing
    # its job rather than a missing correction.)
    full = _plate_item(0.055, plate_total=0.55)
    assert any("this plate covers" in n for n in full.notes)
    assert full.grams < sharing.grams


def test_a_single_item_is_unaffected():
    """Alone on the plate, the item's coverage IS the plate's coverage, which
    is what the constant always meant. This must not move."""
    solo_area = 0.19          # 35% of a plate at 0.55 of frame
    without = _plate_item(solo_area)
    with_total = _plate_item(solo_area, plate_total=solo_area / 0.55)
    assert with_total.grams == pytest.approx(without.grams, rel=0.001)


def test_a_genuinely_bare_plate_still_reads_as_piled():
    """The correction is not removed, only asked the right question. One small
    portion on a large plate is still a small footprint for its mass."""
    bare = _plate_item(0.055, plate_total=0.10)
    normal = _plate_item(0.055, plate_total=0.35)
    assert bare.grams > normal.grams


def test_the_rail_is_the_same_arithmetic_in_both_places():
    """The plate total is summed with railed_area so it matches what the
    estimator actually uses. Summing the RAW reported areas would total the
    very numbers the rail exists to reject."""
    from app.services.ai.portion import railed_area
    box = {"x": 0.1, "y": 0.1, "w": 0.20, "h": 0.15}
    assert railed_area(0.30, box) < 0.30          # cut toward the box
    assert railed_area(0.001, box) == 0.001       # already inside it
    assert railed_area(0.30, None) == 0.30        # no box, no ceiling
    assert railed_area("junk", box) == 0.0


def test_a_calibration_only_describes_its_own_vessel():
    """A plate's measured area divided by a bowl's share of the frame, reported
    as plate_reference -- the highest-trust rung in the table. A measurement of
    the wrong object is worse than no measurement, because it is believed
    more. (The guard itself lives in vision.py; this pins the consequence.)"""
    from app.services.ai.vision import _calibration_fits
    plate_cal = {"vessel": "dinner_plate", "real_area_mm2": 56000}
    assert _calibration_fits(plate_cal, "dinner_plate") is True
    assert _calibration_fits(plate_cal, "bowl") is False
    assert _calibration_fits(plate_cal, None) is False
    # A calibration with no vessel recorded is the user's general default.
    assert _calibration_fits({"real_area_mm2": 56000}, "bowl") is True
    assert _calibration_fits(None, "bowl") is False


def test_the_card_measures_the_camera_angle_when_no_plate_can():
    """reference_cv reports the tilt it measured from the card's own outline,
    and nothing read it. On the photos the card exists for -- paper, a bare
    table, a square container -- there is no round plate to measure it from."""
    no_plate = GeometryHint(reference_kind="credit_card",
                            reference_frame_width_mm=252.0, reference_tilt_deg=15.0)
    assert no_plate.tilt_deg is None
    assert no_plate.camera_tilt_deg == 15.0

    # Where both exist the plate wins: bigger outline, measured over more pixels.
    both = GeometryHint(plate_ellipse_wh=(0.60, 0.566), aspect_ratio=4 / 3,
                        reference_tilt_deg=5.0)
    assert both.camera_tilt_deg == pytest.approx(45.0, abs=3.0)


def test_a_refused_reference_says_so():
    """Found in the pixels, then rejected by the frame-size rail. Landing
    silently on the 270 mm dinner-plate assumption is indistinguishable from an
    ordinary pixel_area estimate."""
    est = estimate_grams(
        name="white rice", area_ratio=0.10,
        hint=GeometryHint(plate_ellipse_area_ratio=0.5, reference_kind="credit_card",
                          reference_frame_width_mm=1250.0, aspect_ratio=0.75),
        shape_hint="mound", detection_confidence=0.8,
    )
    assert est.method == "pixel_area"
    assert any("was not used for scale" in n for n in est.notes), est.notes


def test_a_name_that_is_not_a_string_never_crashes():
    """Every other label read back from a model is guarded. These were not."""
    assert _classify_shape(None, None) == "default"
    assert _classify_shape(12345, None) == "default"
    assert density_for(None, None, "grain") > 0
    assert density_for(12345, None, None) > 0


def test_a_quoted_field_of_view_spans_the_long_axis():
    """Measured against a card, on a real photo.

    One meal shot from about 13 inches, portrait, with a credit card in frame.
    The card puts the frame at 319 mm across. Treating the quoted 68 degree
    field of view as spanning the image WIDTH computes 445 mm -- and frame area
    goes as the square of width, so that is roughly 95% too much food on every
    photo a phone takes upright while reporting its distance.
    """
    portrait = mm2_per_frame(GeometryHint(depth_mm=330.0, aspect_ratio=0.75))
    assert portrait[1] == "depth_model"
    width = (portrait[0] * 0.75) ** 0.5
    assert width == pytest.approx(319.0, rel=0.08)   # the card's measurement

    # Landscape is the case the old formula was right for, and must not move.
    landscape = mm2_per_frame(GeometryHint(depth_mm=330.0, aspect_ratio=4 / 3))
    lw = (landscape[0] * (4 / 3)) ** 0.5
    assert lw == pytest.approx(2 * 330 * math.tan(math.radians(68) / 2), rel=0.01)

    # The same scene either way up covers the same tabletop, give or take the
    # crop. Before this fix the two differed by 78%.
    assert portrait[0] == pytest.approx(landscape[0], rel=0.02)


# ---------------------------------------------------------------------------
# Whole solid pieces are not plated food lying flat.
#
# Measured on 428 g of beef, potatoes and tomatoes weighed to the gram and
# photographed three ways. On the plate shot the area was read correctly --
# 40% of a 10 inch plate, against 19,628 mm2 measured off a credit card in
# another shot of the same meal, agreeing within 3% -- and the answer was still
# -29.9%. Right scale, right area, a third light.
#
# Every height prior in that table was calibrated on food spread on a plate. A
# skewer of beef cubes is a stack of 40 mm objects.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    "grilled meat skewer", "grilled meat skewers", "beef kebab", "chicken satay",
    "swedish meatballs", "chicken nuggets", "roasted potatoes", "boiled potatoes",
    "baby potatoes",
])
def test_whole_pieces_are_chunky(name):
    assert _classify_shape(name, None) == "chunky"


@pytest.mark.parametrize("name,expected", [
    # Potato, but not in whole pieces.
    ("mashed potato", "mound"),
    ("french fries", "loose"),
    ("potato chips", "loose"),
    # Everything else on the bench must be untouched.
    ("mexican rice", "mound"),
    ("refried beans", "mound"),
    ("bean soup", "liquid"),
    ("grilled steak", "flat"),
    ("baked spaghetti casserole", "loose"),
    ("taco with scrambled eggs", "topped_flat"),
    ("chicken burrito", "wrapped"),
])
def test_the_chunky_class_touches_nothing_else(name, expected):
    assert _classify_shape(name, None) == expected


def test_the_chunky_numbers_come_from_the_pieces():
    """40 mm is how thick the pieces are; 0.62 is a sphere's mean height over
    its own footprint, which is 2/3, less a little for settling. Neither was
    solved backwards from the meal -- and they land within 8% of what it needs."""
    from app.services.ai.portion import HEIGHT_PRIORS_MM, PROFILE_FACTORS
    beef = HEIGHT_PRIORS_MM["chunky"] * PROFILE_FACTORS["chunky"] * density_for(
        "grilled meat skewer", None, "protein")
    assert beef == pytest.approx(27.5, rel=0.12)


# ---------------------------------------------------------------------------
# A serving convention must not overrule a measurement.
# ---------------------------------------------------------------------------
def test_a_measured_scale_resists_the_prior_far_harder_than_an_assumed_one():
    """Photo 11, the beef: 335 g weighed, geometry 344 g, and the blend pulled
    it to 287. Photo 05, the soup: geometry 638 g against 405 g weighed, and
    the blend rescued it to 438. Same mechanism, opposite verdicts, and the
    difference is whether the scale was measured or assumed."""
    from app.services.ai.portion import BLEND_MAX_WEIGHT_BY_METHOD as W
    measured = ("plate_reference", "reference_object", "depth_model", "multi_image")
    assumed = ("vessel_reference", "pixel_area", "ai_prior")
    assert max(W[m] for m in measured) < min(W[a] for a in assumed)


def test_the_weighed_skewer_survives_the_blend():
    """Same food, same disagreement with the prior, two different scales.

    The prior here is 150 g for a skewer that weighed 335 g -- a serving
    convention that is simply wrong for this food. On a measured scale it may
    barely move the answer; on an assumed one it must still be able to rescue
    it, because that is where a wild geometric answer actually comes from.
    """
    def run(hint):
        return estimate_grams(
            name="grilled meat skewer", area_ratio=0.09, hint=hint,
            food_group="protein", ai_prior_grams=150.0, detection_confidence=0.8,
        )

    hint = GeometryHint(vessel="paper", reference_kind="credit_card",
                        reference_frame_width_mm=318.9, aspect_ratio=0.75)
    measured = run(hint)
    assert measured.method == "reference_object"

    # The geometry this hint produces, computed independently of the estimator.
    frame_mm2 = 318.9 * (318.9 / 0.75)
    from app.services.ai.portion import HEIGHT_PRIORS_MM, PROFILE_FACTORS
    geometry = (frame_mm2 * 0.09 * HEIGHT_PRIORS_MM["chunky"]
                * PROFILE_FACTORS["chunky"] * density_for(
                    "grilled meat skewer", None, "protein") / 1000.0)

    # On a measured scale the prior may nudge, not overrule. It used to take
    # 16% off a figure that was within 3% of the kitchen scale.
    assert abs(measured.grams - geometry) / geometry < 0.08


def test_a_drumstick_is_a_whole_piece_not_a_slab():
    """Measured: a drumstick in mole weighed at 146 g, photographed with a
    credit card in frame. 130 x 56 mm, 4,600 mm2 of footprint, stable across
    three darkness thresholds -- so it needs height x profile x density of
    31.7. It was classifying as `flat`, the prior for a steak, and using 17.0.

    A chicken BREAST really is a slab and must stay flat. It is the bone-in
    pieces that are chunky, which is why they are named rather than matched on
    "chicken".
    """
    for name in ("chicken drumstick in mole", "chicken drumstick", "drumstick",
                 "mole chicken leg", "chicken wings", "bone-in thigh"):
        assert _classify_shape(name, None) == "chunky", name
    for name in ("chicken breast", "grilled steak", "fish fillet", "pork chop"):
        assert _classify_shape(name, None) == "flat", name


def test_the_measured_fill_ratio_is_under_the_ceiling():
    """The other thing that photo settled. A long diagonal drumstick fills 0.62
    of its own bounding box, measured. BBOX_FILL_CEILING is 0.80, and it is
    meant to be an upper bound that only catches impossible claims -- so 0.62
    passing under it is the ceiling behaving correctly."""
    from app.services.ai.portion import BBOX_FILL_CEILING
    assert 0.62 < BBOX_FILL_CEILING


# --- garnish-sized items: widen the range, never move the number ------------
#
# Photo 13's cherry tomatoes: identical code, two consecutive runs, the box
# moved from 1% of frame to 2% and the estimate moved 24.6 g -> 47.3 g. One
# percentage point of frame IS the whole answer for a 27 g object. We do not
# know a better number, so the number stays and the range says so.

def _band_width(e):
    """Total span as a multiple of the estimate."""
    return (e.grams_high - e.grams_low) / e.grams


def test_widening_a_garnish_band_does_not_move_its_grams(monkeypatch):
    """The whole point: this changes the honesty of the range, not the
    estimate. A constant that quietly moved grams would be exactly the
    compensating-error trap this file keeps refusing."""
    import app.services.ai.portion as P
    for area in (0.004, 0.008, 0.015, 0.019, 0.03):
        on = estimate_grams(name="cherry tomatoes", area_ratio=area, hint=PLATE,
                            detection_confidence=0.8)
        monkeypatch.setattr(P, "SMALL_ITEM_AREA", 0.0)
        off = estimate_grams(name="cherry tomatoes", area_ratio=area, hint=PLATE,
                             detection_confidence=0.8)
        monkeypatch.setattr(P, "SMALL_ITEM_AREA", 0.02)
        assert on.grams == off.grams, f"area {area:.1%} moved {off.grams} -> {on.grams}"


def test_a_smaller_item_gets_a_wider_band():
    """Monotone, with no cliff: two items a hair either side of the threshold
    must not get wildly different ranges."""
    from app.services.ai.portion import SMALL_ITEM_AREA
    areas = [0.004, 0.008, 0.012, 0.016, SMALL_ITEM_AREA]
    widths = [
        _band_width(estimate_grams(name="cherry tomatoes", area_ratio=a, hint=PLATE,
                                   detection_confidence=0.8))
        for a in areas
    ]
    for smaller, larger in zip(widths, widths[1:]):
        assert smaller > larger, f"band did not widen as the item shrank: {widths}"
    # No cliff at the boundary itself.
    just_under = _band_width(estimate_grams(name="cherry tomatoes",
                                            area_ratio=SMALL_ITEM_AREA - 1e-4,
                                            hint=PLATE, detection_confidence=0.8))
    assert abs(just_under - widths[-1]) < 0.01


def test_a_normal_serving_keeps_its_band():
    """Rice at 8% of frame is comfortably above the resolution limit. It must
    be untouched -- the earlier version of this widening hit every railed item
    and put +/-100% on an ordinary plate of rice."""
    import app.services.ai.portion as P
    for area in (0.02, 0.05, 0.08, 0.15):
        wide = _band_width(estimate_grams(name="jasmine rice", area_ratio=area,
                                          hint=PLATE, detection_confidence=0.8))
        P.SMALL_ITEM_AREA = 0.0
        try:
            plain = _band_width(estimate_grams(name="jasmine rice", area_ratio=area,
                                               hint=PLATE, detection_confidence=0.8))
        finally:
            P.SMALL_ITEM_AREA = 0.02
        assert wide == pytest.approx(plain), f"{area:.0%} of frame should be untouched"


def test_a_garnish_band_stays_inside_the_cap():
    """Widened, but still a range a person can act on."""
    from app.services.ai.portion import BAND_MAX_SPREAD
    e = estimate_grams(name="cherry tomatoes", area_ratio=0.002, hint=PLATE,
                       detection_confidence=0.8)
    assert e.grams_low > 0
    # <=, not <, and the reason matters. MIN_GRAMS floors both the estimate and
    # the bottom of its band, so for an item small enough to hit the floor the
    # two are equal -- "at least 3 g, possibly 4.3 g". That is the honest
    # statement: we cannot claim less than the floor, so the band is one-sided
    # there rather than wrong.
    assert e.grams_low <= e.grams <= e.grams_high
    if e.grams_low == e.grams:
        from app.services.ai.portion import MIN_GRAMS
        assert e.grams == pytest.approx(MIN_GRAMS), (
            "the band collapsed for a reason other than the floor"
        )
    assert e.grams_high > e.grams, "the upper edge must always have room"
    assert e.grams_high / e.grams <= BAND_MAX_SPREAD
    assert e.grams_low / e.grams >= 0.2


def test_a_garnish_says_why_its_range_is_wide():
    e = estimate_grams(name="cherry tomatoes", area_ratio=0.008, hint=PLATE,
                       detection_confidence=0.8)
    assert any("limit of what a photo can size" in n for n in e.notes)
    big = estimate_grams(name="jasmine rice", area_ratio=0.12, hint=PLATE,
                         detection_confidence=0.8)
    assert not any("limit of what a photo can size" in n for n in big.notes)


# --- which inputs actually reach the grams -----------------------------------
#
# These exist because four nights went into tuning an input that turns out not
# to reach the answer at all on a plate photo. Nothing here asserts a constant.
# They assert WHICH KNOB IS CONNECTED TO WHAT, so the next person to open this
# file -- including me -- cannot spend another week on the disconnected one.

_LIVE = GeometryHint(plate_ellipse_area_ratio=0.35, plate_diameter_mm=254)


def _g(**kw):
    kw.setdefault("name", "mexican rice")
    kw.setdefault("shape_hint", "mound")
    kw.setdefault("detection_confidence", 0.8)
    kw.setdefault("hint", _LIVE)
    return estimate_grams(**kw).grams


def test_the_reported_area_is_inert_once_coverage_is_available():
    """The finding that ended four nights of circling.

    When the model reports plate_coverage, the estimator overwrites area_ratio
    with coverage x plate_ratio before anything looks at it. The rail, the fill
    ceiling, BOX_TIGHTNESS, the disagreement penalty -- all of them operate on a
    number the model's own frame-share claim never reaches.

    So the model can claim this food is 8% of the photo or 99% of it, and the
    answer does not move by a gram.
    """
    grams = {
        area: _g(area_ratio=area, plate_coverage=0.20, bbox={"w": 0.30, "h": 0.30})
        for area in (0.08, 0.15, 0.30, 0.50, 0.99)
    }
    assert len(set(grams.values())) == 1, (
        f"area_ratio moved the answer: {grams} -- if this now FAILS, the "
        f"coverage path has changed and the sensitivity table in "
        f"PortionEstimate needs remeasuring before anyone tunes the rail again"
    )


def test_the_two_live_inputs_really_are_live():
    """...and the corollary: the box and plate_coverage are what move it, so
    they are what the bench has to score."""
    by_box = [_g(area_ratio=0.15, plate_coverage=0.20, bbox={"w": s, "h": s})
              for s in (0.15, 0.20, 0.30, 0.45, 0.60)]
    assert max(by_box) / min(by_box) > 2.0, f"the box stopped mattering: {by_box}"

    by_cov = [_g(area_ratio=0.15, plate_coverage=c, bbox={"w": 0.45, "h": 0.45})
              for c in (0.10, 0.15, 0.20, 0.30, 0.40)]
    assert max(by_cov) / min(by_cov) > 2.0, f"coverage stopped mattering: {by_cov}"
    # Monotone, both of them: more coverage is more food, a bigger box permits
    # more food. A non-monotone response here is the saw-tooth defect returning.
    assert by_cov == sorted(by_cov), by_cov
    assert by_box == sorted(by_box), by_box


def test_the_estimate_says_which_inputs_it_used():
    """The bench cannot score an input it is not shown. It printed area_ratio --
    the inert one -- and neither of the live two, for four nights."""
    e = estimate_grams(name="mexican rice", area_ratio=0.12, hint=_LIVE,
                       plate_coverage=0.20, shape_hint="mound",
                       bbox={"w": 0.30, "h": 0.30}, detection_confidence=0.8)
    assert e.reported_area_ratio == pytest.approx(0.12)
    assert e.plate_coverage_used == pytest.approx(0.20)
    assert e.box_area_ratio == pytest.approx(0.09)
    # ...and the number actually used is neither of the first two.
    assert e.pixel_area_ratio != pytest.approx(e.reported_area_ratio)

    blind = estimate_grams(name="mexican rice", area_ratio=0.12, hint=_LIVE,
                           shape_hint="mound", bbox={"w": 0.30, "h": 0.30},
                           detection_confidence=0.8)
    assert blind.plate_coverage_used is None, "coverage reported when none was given"


def test_a_note_never_calls_a_derived_number_the_models_claim():
    """The instrument that caused the circling.

    With coverage present, area_ratio has already been overwritten by the time
    the disagreement note is built, so the note printed `coverage x plate_ratio`
    under the words "reported area (18% of frame)" when the model had said 12%.
    Every bench line I read for four nights described the pipeline's own
    arithmetic back to me as if it were evidence about the model.
    """
    e = estimate_grams(name="refried beans", area_ratio=0.12, hint=_LIVE,
                       plate_coverage=0.50, shape_hint="mound",
                       bbox={"w": 0.30, "h": 0.30}, detection_confidence=0.8)
    box_notes = [n for n in e.notes if "bounding box" in n]
    assert box_notes, e.notes
    note = box_notes[0]
    assert "of the plate" in note, f"the note hides that coverage was used: {note}"
    assert "reported area" not in note, (
        f"a derived figure is still being labelled as the model's report: {note}"
    )


def test_the_answer_survives_the_plate_looking_bigger_or_smaller():
    """Coverage is a RATIO inside the plate, so the plate's apparent size
    cancels out of the grams -- until the box rail bites. Worth pinning: it is
    the one place the ladder's headline input genuinely does not matter, and not
    knowing that sent a night into measuring plate diameters."""
    same = [_g(area_ratio=0.15, plate_coverage=0.20, bbox={"w": 0.45, "h": 0.45},
               hint=GeometryHint(plate_ellipse_area_ratio=p, plate_diameter_mm=254))
            for p in (0.20, 0.30, 0.35)]
    assert len(set(same)) == 1, f"the plate's apparent size leaked in: {same}"


def test_one_step_of_the_models_grid_costs_more_than_the_accuracy_target():
    """The finding that explains four nights of no progress.

    Every geometry number on the weighed bench arrived on a 0.05 grid --
    sixteen plate_coverage values in a row, all multiples of 5%, and every
    bounding box a product of two multiples of 0.05. Chance would put 20% of
    them there.

    A box is two of those numbers multiplied, so one step of the grid is a
    large multiple of the answer. This measures it on the real bench items. If
    it ever drops below the target band, the input finally has the resolution
    to support the answer and downstream tuning is worth doing again.
    """
    items = [  # name, plate ratio, coverage, box w, h -- as the model returned them
        ("chicken drumstick in mole", 0.60, 0.25, 0.40, 0.30),
        ("refried beans",             0.60, 0.20, 0.25, 0.30),
        ("mexican rice",              0.50, 0.15, 0.20, 0.20),
        ("chicken drumstick in sauce",0.45, 0.20, 0.20, 0.20),
        ("refried beans",             0.35, 0.30, 0.25, 0.20),
    ]

    def grams(name, p, cov, bw, bh):
        return estimate_grams(
            name=name, area_ratio=cov * p, plate_coverage=cov,
            hint=GeometryHint(plate_ellipse_area_ratio=p, plate_diameter_mm=254),
            bbox={"w": bw, "h": bh}, detection_confidence=0.8,
        ).grams

    costs = []
    for name, p, cov, bw, bh in items:
        base = grams(name, p, cov, bw, bh)
        for dw, dh in ((-0.05, -0.05), (0.05, 0.05)):
            moved = grams(name, p, cov, max(0.05, bw + dw), max(0.05, bh + dh))
            costs.append(abs(moved / base - 1))

    mean = sum(costs) / len(costs)
    assert mean > 0.20, (
        f"one grid step now costs only {mean:.0%} -- if this is real, the input "
        f"resolution has improved and the sensitivity table in PortionEstimate "
        f"should be remeasured"
    )
    # And the point of recording it: the target is 10-20%, and this is worse.
    assert mean > 0.20, mean


def test_the_prompt_asks_for_numbers_finer_than_the_grid():
    """The one change aimed at the actual bottleneck. Every geometry
    placeholder in the schema showed `0.0` -- one decimal -- and the model
    mirrored that precision back."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "app" / "services" / "ai" / "prompts.py").read_text(encoding="utf-8")
    for field in ('"area_ratio"', '"plate_coverage"', '"plate_area_ratio"'):
        line = next(ln for ln in src.splitlines() if field in ln and ":" in ln)
        assert "0.000" in line, f"{field} still shows one-decimal precision: {line.strip()}"
    assert '"bbox": {"x":0.000' in src, "the item bbox still shows 0.0"
    assert "THREE DECIMAL PLACES" in src
    assert "0.05 grid" in src, "the prompt does not say why the precision matters"


# ---------------------------------------------------------------------------
# The measured footprint in the weight path
# ---------------------------------------------------------------------------

def _measured_hint():
    from app.services.ai.portion import GeometryHint
    return GeometryHint(plate_diameter_mm=254.0, plate_ellipse_area_ratio=0.40)


def _est(**kw):
    from app.services.ai.portion import estimate_grams
    base = dict(
        name="white rice", area_ratio=0.15, hint=_measured_hint(),
        plate_coverage=0.30, bbox={"x": 0.30, "y": 0.30, "w": 0.20, "h": 0.20},
        food_group="grain",
    )
    base.update(kw)
    return estimate_grams(**base)


def test_the_bounding_box_rail_is_not_applied_to_a_measurement():
    """The single most important line in the wiring.

    The rail caps the area at a fraction of the model's own box, which is right
    for a CLAIM -- a claim cannot beat geometry. It is exactly wrong for a
    measurement, because the boxes were measured against a ruler and found
    SMALL: a real 4.84% footprint boxed at 4.00%, a real 4.50% boxed at 3.00%.
    Capping the measurement at the box would put the box's error straight back
    into the grams, silently, under a note saying the number had been made more
    physical.

    Same lesson as GrabCut, which lost 50.9% to 38.3% for exactly one reason:
    every method that starts from the box inherits the box.
    """
    # A box of 0.20 x 0.20 = 4% of frame. The rail's ceiling sits below that.
    # The measurement says 8.5%, which is what a ruler keeps finding.
    got = _est(measured_area_ratio=0.085)
    assert got.pixel_area_ratio == pytest.approx(0.085, abs=1e-4), (
        "the measured footprint was railed back to the model's undersized box"
    )
    assert got.measured_area_used == pytest.approx(0.085, abs=1e-4)


def test_a_measurement_outranks_the_plate_coverage_claim():
    """Both of the model's answers are claims about SIZE, which is the one thing
    this whole file exists because the model cannot do."""
    claimed = _est()
    measured = _est(measured_area_ratio=0.085)
    assert measured.grams != claimed.grams
    assert measured.plate_coverage_used is None, (
        "coverage was still converted over the top of a measurement"
    )
    assert claimed.measured_area_used is None


def test_a_footprint_past_the_plate_is_refused_not_capped():
    """A mask on the tablecloth is the one segmenter failure that produces a
    large, confident, WRONG weight rather than a missing one.

    Refused rather than capped, deliberately: a cap turns a leaked mask into a
    plausible-looking number, which is the failure mode that is impossible to
    spot afterwards.
    """
    leaked = _est(measured_area_ratio=0.90)
    claimed = _est()
    assert leaked.measured_area_used is None
    assert leaked.grams == claimed.grams
    assert any("leaked past the plate" in n for n in leaked.notes), leaked.notes


def test_overhanging_the_plate_a_little_is_still_believed():
    """Food really does hang over a rim -- a chop, a slice of toast, a taco
    shell -- and the plate ratio being compared against is itself the model's
    bounding-box ellipse on a 0.05 grid. Refusing at exactly the plate would
    throw away good measurements to defend a number that is not measured."""
    from app.services.ai.portion import MEASURED_OVER_PLATE_LIMIT
    plate = _measured_hint().plate_ellipse_area_ratio
    just_over = plate * (MEASURED_OVER_PLATE_LIMIT - 0.05)
    got = _est(measured_area_ratio=just_over)
    assert got.measured_area_used is not None, (
        "an ordinary overhang was thrown away"
    )


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5, None, "", "eight percent",
                                 float("nan"), float("inf"), [0.08]])
def test_an_unusable_measurement_leaves_the_estimate_alone(bad):
    """A measurement that cannot be read is 'not measured', never 'a small
    food'. A mask that quietly returned almost nothing shipped a weighed 146 g
    drumstick as 12 g once already."""
    claimed = _est()
    got = _est(measured_area_ratio=bad)
    assert got.grams == claimed.grams, f"{bad} moved the weight"
    assert got.measured_area_used is None


def test_a_non_finite_gram_figure_is_refused_not_clamped_to_the_maximum():
    """nan does not clamp -- it survives as the largest portion allowed.

    min(MAX_GRAMS, nan) returns MAX_GRAMS, because every comparison with nan is
    False and min() keeps its first argument. The difference test that follows
    is also False, so the "hit a plausibility limit" note and the confidence
    penalty never fire. A scan that computed nothing publishes 1500 g on a
    measured rung with ordinary confidence.
    """
    import math
    from app.services.ai import portion as P

    got = P.estimate_grams(
        name="white rice", area_ratio=0.15,
        hint=P.GeometryHint(plate_diameter_mm=229.0,
                            plate_ellipse_area_ratio=float("nan")),
        plate_coverage=0.30,
        bbox={"x": 0.30, "y": 0.30, "w": 0.20, "h": 0.20},
        food_group="grain",
    )
    assert math.isfinite(got.grams), "grams came back non-finite"
    assert got.grams < P.MAX_GRAMS, (
        f"a non-finite calculation published {got.grams:.0f} g -- the maximum "
        f"portion this module allows, reported as if it were measured")
    assert got.confidence <= 0.5, (
        f"confidence {got.confidence} on a figure that could not be computed")


def test_soup_is_sized_from_the_bowl_not_from_its_own_outline():
    """A liquid has no edges worth finding, and the vessel's are already known.

    Segmenting broth goes wrong in a way no mask rule fixes: on the weighed
    chicken noodle the segmenter returned 2.5% of the frame, having found the
    NOODLES. The three weighed soups came out at -74.8%, -64.8% and -43.4%.

    The surface of a liquid IS the vessel's mouth. Sized that way, and with a
    depth solved on those same three bowls, they land at +4.3%, -13.5% and
    -11.2%.
    """
    from app.services.ai import portion as P

    hint = P.GeometryHint(plate_diameter_mm=114.0, plate_ellipse_area_ratio=0.09,
                          vessel="soup_bowl")
    # A tiny footprint, as a segmenter reports when it finds only the noodles.
    got = P.estimate_grams(name="chicken noodle soup", area_ratio=0.025,
                           hint=hint, bbox={"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2})
    assert 120 <= got.grams <= 190, (
        f"{got.grams:.0f} g for a bowl of soup — sized from the noodles rather "
        f"than the bowl (weighed: 151 g)")
    assert any("bowl" in n for n in got.notes), "the note does not say where the size came from"


def test_a_bowl_of_soup_is_not_a_bowl_of_dry_florets():
    """One word in a name was a threefold error.

    density_for matches the most specific token it finds, and in "broccoli
    cheese soup" that is "broccoli" — 0.35 g/ml, dry florets in a colander.
    The bowl came out at 52.5 g against 152 g weighed. Chicken noodle, whose
    name happens to contain no vegetable, was unaffected at 1.05.
    """
    from app.services.ai import portion as P

    # The name no longer reproduces it: the lookup reads the head of the
    # phrase now, and a broccoli cheese soup is soup. The clamp still guards a
    # low density arriving any other way, so the florets' 0.35 is handed in
    # directly -- the fixture must keep exercising the clamp, not the name.
    assert P.density_for("broccoli cheese soup") == pytest.approx(P.DENSITY_G_ML["soup"])
    assert 0.35 < P.LIQUID_DENSITY_MIN, "the fixture no longer reaches the clamp"
    hint = P.GeometryHint(plate_diameter_mm=114.0, plate_ellipse_area_ratio=0.16,
                          vessel="soup_bowl")
    got = P.estimate_grams(name="broccoli cheese soup", area_ratio=0.025,
                           density=0.35, hint=hint,
                           bbox={"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2})
    assert got.grams > 100, (
        f"{got.grams:.0f} g against 152 g weighed — an ingredient's density was "
        f"used for the dish")


def test_the_soups_this_list_did_not_know_were_soups():
    """"beef posole" came back as shape "default" on the bench and was sized as
    food lying on a plate. It is a bowl of broth. Ramen sat with the noodles —
    which is what it is made of, not what it is."""
    from app.services.ai.portion import _classify_shape

    for name in ("beef posole", "pozole rojo", "menudo", "ramen", "minestrone",
                 "miso soup", "clam chowder"):
        assert _classify_shape(name, None) == "liquid", (
            f"{name!r} classified as {_classify_shape(name, None)!r}")


def test_a_plate_sized_ellipse_is_not_a_bowl_of_soup():
    """The bowl branch must not believe a dinner plate is a bowl's mouth.

    Sizing soup from the vessel means trusting whatever ellipse the pipeline
    found. On a bare 270 mm dinner plate that ellipse is the PLATE, and a
    270 mm disc of liquid 15 mm deep is 841 g of tomato soup -- which is what
    this pipeline produced the first time the bowl branch ran against the
    plausibility cases.

    A mouth wider than any bowl soup is served from is a rim we mistook for
    one. Past the ceiling the answer becomes the largest bowl that is real,
    is marked down in confidence, and says so in the notes.
    """
    import math
    from app.services.ai import portion as P

    plate = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=270)
    est = estimate_grams(name="tomato soup", area_ratio=0.18, hint=plate,
                         detection_confidence=0.8)

    # The specific number this regressed to, so the guard names its own bug.
    assert est.grams < 600, (
        f"a 270 mm plate was read as a bowl mouth: {est.grams:.1f} g")

    # It lands at the ceiling, not at some arbitrary smaller number.
    ceiling_mm2 = math.pi * (P.MAX_BOWL_MOUTH_MM / 2.0) ** 2
    implied_mm2 = est.grams / (P.SOUP_DEPTH_MM * 1.0 / 1000.0)
    assert implied_mm2 <= ceiling_mm2 * 1.35, (
        f"implied mouth {implied_mm2:,.0f} mm2 exceeds the "
        f"{ceiling_mm2:,.0f} mm2 ceiling")

    # A floor is not a measurement and must not be sold as one.
    assert est.confidence <= 0.55, est.confidence
    assert any("plate rather than a bowl" in n for n in est.notes), est.notes


def test_the_ceiling_does_not_touch_a_real_bowl():
    """The weighed evidence sits far inside the ceiling and must be untouched.

    The three soups the depth was fitted on were poured into a 114 mm crock --
    10,261 mm2 against a 26,880 mm2 ceiling. If the cap ever reaches them the
    depth constant is being fitted against a clamp, not against the bowls.
    """
    crock = GeometryHint(plate_ellipse_area_ratio=0.30, plate_diameter_mm=114.3,
                         vessel="bowl")
    for name, weighed in (("chicken noodle soup", 151),
                          ("beef posole", 182),
                          ("broccoli cheese soup", 152)):
        est = estimate_grams(name=name, area_ratio=0.025, hint=crock,
                             detection_confidence=0.8)
        err = abs(est.grams - weighed) / weighed
        assert err < 0.20, f"{name}: {est.grams:.1f} g against {weighed} g"
        assert not any("plate rather than a bowl" in n for n in est.notes), (
            f"{name} tripped the ceiling; the crock is not plate-sized")


def test_a_bound_salad_is_not_a_bowl_of_leaves():
    """"salad" at 0.22 is loose lettuce, and it was reaching bound dishes.

    Longest-match handed the leafy-greens density to every dish with the word
    "salad" in it. Weighed on this user's scale, 89 g of macaroni salad
    measured 5,252 mm2 of footprint -- at 0.22 that is a pile 77 mm tall. At
    the composite density it is 19.9 mm, which is what a scoop looks like.

    The leafy ones must keep 0.22, because for them it is right. This is the
    same defect as "broccoli cheese soup" resolving to broccoli, and it is
    guarded the same way: by what the number does to a weighed photograph.
    """
    from app.services.ai.portion import FOOD_GROUPS

    composite = FOOD_GROUPS["composite"]["density"]
    for bound in ("macaroni salad", "potato salad", "egg salad",
                  "tuna salad", "fruit salad", "three bean salad"):
        got = density_for(bound)
        assert got == pytest.approx(composite), (
            f"{bound!r} resolved to {got}, not the composite density")

    for leafy in ("caesar salad", "garden salad", "mixed greens salad",
                  "chef salad"):
        assert density_for(leafy) == pytest.approx(0.22), (
            f"{leafy!r} is leaves and must stay at 0.22, got "
            f"{density_for(leafy)}")

    # The weighed macaroni salad, carried all the way to a height.
    area_mm2, grams = 5252.0, 89.0
    implied_mm = grams / density_for("macaroni salad") * 1000.0 / area_mm2
    assert 12.0 <= implied_mm <= 32.0, (
        f"a scoop of macaroni salad is not {implied_mm:.0f} mm tall")


# The seven weighed single-food photographs, measured by dev heights:
# (food, weighed grams, footprint mm2, one connected mass?)
WEIGHED_FOOTPRINTS = [
    ("steamed carrots",    58,  6402, False),
    ("steamed zucchini",   74, 11194, False),
    ("bbq chicken thigh", 159,  8027, True),
    ("roast beef",         83,  3809, True),
    ("macaroni salad",     89,  5252, True),
    ("smashed potatoes",  136,  5639, True),
    ("pizza slice",       120,  9797, True),
]


def test_separate_pieces_are_a_single_layer_and_one_mass_is_a_pile():
    """Pieces on a plate cannot stack. A connected mass can be piled.

    The height must come from the footprint's topology, not from the shape
    word, and the two must differ by roughly the ratio the scale measured.
    """
    from app.services.ai import portion as P

    hint = GeometryHint(plate_ellipse_area_ratio=0.35, plate_diameter_mm=229)
    common = dict(name="steamed carrots", area_ratio=0.05, hint=hint,
                  measured_area_ratio=0.05, detection_confidence=0.8)

    pile = estimate_grams(**common, largest_piece_share=1.0)
    layer = estimate_grams(**common, largest_piece_share=0.5)

    assert pile.grams > layer.grams, (
        f"a pile must outweigh a single layer: {pile.grams} vs {layer.grams}")
    ratio = pile.grams / layer.grams
    expected = P.CONNECTED_PILE_HEIGHT_MM / P.SEPARATE_PIECES_HEIGHT_MM
    assert ratio == pytest.approx(expected, rel=0.05), (
        f"the two heights should set the ratio, got {ratio:.2f} "
        f"expecting {expected:.2f}")
    assert any("cannot stack" in n for n in layer.notes), layer.notes
    assert not any("cannot stack" in n for n in pile.notes), pile.notes


def test_the_topology_heights_beat_the_shape_table_on_the_scale():
    """Leave-one-out over the weighed photographs, and it must WIN.

    Each food is predicted from the OTHER foods in its topology group --
    nothing from the held-out photograph touches its own prediction. Measured
    when this shipped: shape table 58.4% mean absolute, these two heights
    15.0%. If a change to either number, to the density table, or to the
    classifier pushes this back above the table, it is not an improvement.
    """
    from app.services.ai import portion as P

    solved = []
    for name, grams, area_mm2, one_mass in WEIGHED_FOOTPRINTS:
        dens = density_for(name)
        solved.append((name, grams, area_mm2, one_mass, dens,
                       grams / dens * 1000.0 / area_mm2))

    table_err, fitted_err = [], []
    for i, (name, grams, area_mm2, one_mass, dens, _h) in enumerate(solved):
        shape = _classify_shape(name, None)
        table_h = MEASURED_HEIGHTS_MM.get(shape, MEASURED_HEIGHTS_MM["default"])
        table_err.append(abs(area_mm2 * table_h * dens / 1000.0 - grams) / grams)

        peers = [r[5] for j, r in enumerate(solved)
                 if j != i and r[3] == one_mass]
        assert peers, f"{name} has no peer to be predicted from"
        fitted_err.append(
            abs(area_mm2 * (sum(peers) / len(peers)) * dens / 1000.0 - grams)
            / grams)

    table = 100.0 * sum(table_err) / len(table_err)
    fitted = 100.0 * sum(fitted_err) / len(fitted_err)
    assert fitted < table / 2.0, (
        f"topology heights {fitted:.1f}% must clearly beat the shape table "
        f"{table:.1f}% on the weighed photographs")
    assert fitted < 20.0, f"{fitted:.1f}% mean absolute is worse than it shipped"

    # And the constants must still be the ones that were solved.
    one = [r[5] for r in solved if r[3]]
    sep = [r[5] for r in solved if not r[3]]
    assert P.CONNECTED_PILE_HEIGHT_MM == pytest.approx(
        sum(one) / len(one), rel=0.05)
    assert P.SEPARATE_PIECES_HEIGHT_MM == pytest.approx(
        sum(sep) / len(sep), rel=0.05)


def test_topology_is_ignored_without_a_measured_footprint():
    """These heights were solved against MEASURED areas and mean nothing else.

    An item with no measured footprint must come out exactly as it did before
    any of this existed -- the railed path is calibrated against railed areas.
    """
    hint = GeometryHint(plate_ellipse_area_ratio=0.35, plate_diameter_mm=229)
    base = estimate_grams(name="roast beef", area_ratio=0.05, hint=hint,
                          detection_confidence=0.8)
    with_topology = estimate_grams(name="roast beef", area_ratio=0.05,
                                   hint=hint, detection_confidence=0.8,
                                   largest_piece_share=1.0)
    assert with_topology.grams == pytest.approx(base.grams, rel=1e-9), (
        "topology moved a gram on an item with no measured footprint")


def test_a_fragment_of_the_food_is_not_the_food():
    """A mask far smaller than its own box found a piece, not the whole.

    The leak guard caught masks that were too BIG and nothing watched the
    opposite. On the weighed caesar salad the segmenter returned 4.3% of the
    frame inside a 25% box -- it had segmented croutons -- and that was
    believed and published as 28 g against 123 g weighed, the worst item in
    the bench.

    Scattered food is legitimately smaller than its box: eight baby carrots
    measured 4.8% inside a 16% box and that measurement is correct. Both cases
    are here, because a guard that fails either way is worthless.
    """
    hint = GeometryHint(plate_ellipse_area_ratio=0.45, plate_diameter_mm=229)

    crouton = estimate_grams(
        name="caesar salad", area_ratio=0.20, hint=hint,
        bbox={"x": 0.2, "y": 0.2, "w": 0.5, "h": 0.5},
        measured_area_ratio=0.043, detection_confidence=0.8)
    assert crouton.measured_area_used is None, (
        "a footprint 5.8x smaller than its own box was believed")
    assert any("rather than the whole" in n for n in crouton.notes), crouton.notes

    scattered = estimate_grams(
        name="steamed carrots", area_ratio=0.10, hint=hint,
        bbox={"x": 0.3, "y": 0.3, "w": 0.4, "h": 0.4},
        measured_area_ratio=0.048, detection_confidence=0.8)
    assert scattered.measured_area_used == pytest.approx(0.048), (
        "scattered carrots at 3.3x their box must still be measured")


def test_the_two_measured_footprint_guards_face_opposite_ways():
    """One catches a leak, the other a fragment. Neither may catch the other."""
    from app.services.ai import portion as P

    assert P.MEASURED_OVER_PLATE_LIMIT > 1.0
    assert P.MEASURED_UNDER_BOX_LIMIT > 1.0
    hint = GeometryHint(plate_ellipse_area_ratio=0.30, plate_diameter_mm=229)

    # Bigger than the plate: leaked.
    leaked = estimate_grams(name="rice", area_ratio=0.10, hint=hint,
                            bbox={"x": 0.1, "y": 0.1, "w": 0.6, "h": 0.6},
                            measured_area_ratio=0.90, detection_confidence=0.8)
    assert leaked.measured_area_used is None
    assert any("leaked past the plate" in n for n in leaked.notes), leaked.notes

    # A sensible footprint passes both.
    fine = estimate_grams(name="rice", area_ratio=0.10, hint=hint,
                          bbox={"x": 0.1, "y": 0.1, "w": 0.35, "h": 0.35},
                          measured_area_ratio=0.08, detection_confidence=0.8)
    assert fine.measured_area_used == pytest.approx(0.08)
