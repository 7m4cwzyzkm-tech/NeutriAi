"""Measuring a food's footprint without trusting the model's box.

The reason this module exists is a negative result. Handing each box to GrabCut
scored 50.9% mean absolute against the model's own 38.5% -- WORSE -- because
GrabCut only looks inside the rectangle it is given, and the model's rectangles
are undersized. Measured twice on the same weighed 146 g chicken leg:

    photo 08   boxed at 4.00% of frame   real footprint 4.84%
    photo 14   boxed at 3.00% of frame   real footprint 4.50%

Food cannot exceed its own bounding box, so those are impossible, not merely
inaccurate. Anything that starts from the box inherits that. So this starts from
the pixels and uses the box only for WHERE, never HOW BIG -- and these tests
exist mostly to hold that line, because the tempting version of every one of
these functions quietly reads the box's size.
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.services.ai.food_seg import (
    flatten_illumination, food_mask, food_on_plate, measure_items, surface_mask,
)


def _plate_photo(W=600, H=600, foods=((0.35, 0.35, 90), (0.62, 0.60, 70))):
    """A pale plate on a pale patterned cloth, with saturated blobs on it.

    The cloth is deliberately BRIGHTER than the plate. That is not a quirk of
    the test: it is the property of the real bench photos that defeated four
    different pixel-only attempts to find the plate, and it is why the model is
    asked where the plate is instead.
    """
    img = np.full((H, W, 3), 232, np.uint8)                  # bright cloth
    # Its printed pattern is saturated enough to read as food, which is exactly
    # the trap: without a fence the mask swallows the whole table.
    for gx in range(0, W, 140):
        for gy in range(0, H, 140):
            cv2.circle(img, (gx, gy), 18, (120, 150, 190), -1)
    cv2.ellipse(img, (W // 2, H // 2), (int(W * 0.34), int(H * 0.32)),
                0, 0, 360, (208, 200, 188), -1)               # the plate, duller
    for cx, cy, hue in foods:
        cv2.circle(img, (int(cx * W), int(cy * H)), int(0.09 * W),
                   tuple(int(v) for v in cv2.cvtColor(
                       np.uint8([[[hue, 200, 150]]]), cv2.COLOR_HSV2RGB)[0][0]), -1)
    return img


PLATE = {"x": 0.16, "y": 0.18, "w": 0.68, "h": 0.64}
# A "plate" box that actually sits on one of the food blobs, so its rim samples
# food instead of plate. This is what a wrong plate_bbox does in the field.
ON_THE_FOOD = {"x": 0.2667, "y": 0.2667, "w": 0.1667, "h": 0.1667}


def test_it_finds_the_food_and_not_the_tablecloth():
    img = _plate_photo()
    fenced = food_mask(img) & surface_mask(img, PLATE)
    loose = food_mask(img)
    assert fenced.sum() < loose.sum(), "the plate fence did nothing"
    assert 0.005 < fenced.mean() < 0.25


def test_a_located_plate_overrules_the_model_box_for_the_fence():
    """THIS REPLACES "the fence comes from the model, not the pixels", AND THE
    REVERSAL WAS CHOSEN.

    That rule was written when no pixel method could find a plate -- six had
    failed -- so the model's box was the only answer available and deferring to
    it was correct. Hough changed the premise: 30/30 on plain wood, patterned
    cloth and the bench set, every circle drawn over the photograph and looked
    at, against a box quantised to a 0.05 grid where one step is 45% of the
    food's weight. So a wrong box now costs nothing, which is the point.

    TWO ASSERTIONS, AND THE SECOND IS THE ONE THAT MATTERS.

    Equality alone would pass on an inner-well lock -- a circle sitting on the
    plate's INNER rim is equally indifferent to the box, and equally wrong.
    That failure has already happened once here: the sixth method locked the
    well and reported a 34.7 degree tilt for a plate shot from overhead. So the
    mask is also held to the plate this fixture actually draws: an ellipse of
    0.34W x 0.32H, area pi * 0.34 * 0.32 = 0.342 of the frame. The well would
    come in near half that.
    """
    img = _plate_photo()
    tight = surface_mask(img, {"x": 0.40, "y": 0.40, "w": 0.20, "h": 0.20})
    wide = surface_mask(img, PLATE)
    assert tight is not None and wide is not None
    assert tight.sum() == wide.sum(), (
        "the model's box still moves the fence -- the located plate is not "
        "overruling it")
    share = wide.mean()
    assert 0.30 < share < 0.40, (
        f"the fence covers {share:.3f} of the frame against a drawn plate of "
        f"0.342 -- this is not the plate's outer rim")


def test_two_foods_are_split_and_nothing_is_lost():
    img = _plate_photo()
    boxes = [{"x": 0.26, "y": 0.26, "w": 0.18, "h": 0.18},
             {"x": 0.53, "y": 0.51, "w": 0.18, "h": 0.18}]
    got = measure_items(img, boxes, PLATE)
    assert all(g is not None for g in got)
    total = food_mask(img) & surface_mask(img, PLATE)
    assert sum(got) == pytest.approx(total.mean(), rel=0.02), (
        "every food pixel should belong to exactly one item"
    )


def test_the_box_size_does_not_change_the_answer():
    """The whole point. Shrink the box to a quarter of its area and the measured
    footprint must not move -- only the centre is read."""
    img = _plate_photo()
    big = [{"x": 0.20, "y": 0.20, "w": 0.30, "h": 0.30},
           {"x": 0.47, "y": 0.45, "w": 0.30, "h": 0.30}]
    small = [{"x": 0.305, "y": 0.305, "w": 0.15, "h": 0.15},
             {"x": 0.545, "y": 0.525, "w": 0.15, "h": 0.15}]
    a = measure_items(img, big, PLATE)
    b = measure_items(img, small, PLATE)
    assert all(x is not None and y is not None for x, y in zip(a, b))
    for x, y in zip(a, b):
        assert x == pytest.approx(y, rel=0.02)


def test_a_box_whose_centre_misses_the_food_still_finds_it():
    """An undersized box centred on a gap must not silently measure nothing."""
    img = _plate_photo(foods=((0.35, 0.35, 90),))
    off = [{"x": 0.20, "y": 0.20, "w": 0.34, "h": 0.34}]   # centre lands on plate
    got = measure_items(img, off, PLATE)
    assert got[0] is not None and got[0] > 0.005


def test_food_nobody_detected_is_left_out_not_given_away():
    """Two blobs, one detection. The undetected blob must not be handed to the
    item that happens to be nearest -- that is how a meal doubles."""
    img = _plate_photo()
    one = [{"x": 0.26, "y": 0.26, "w": 0.18, "h": 0.18}]
    got = measure_items(img, one, PLATE)
    total = (food_mask(img) & surface_mask(img, PLATE)).mean()
    assert got[0] is not None
    assert got[0] < total * 0.75


@pytest.mark.parametrize("boxes", [
    [], [None], [{}], [[1, 2, 3]], [{"x": 0, "y": 0, "w": 0, "h": 0}],
    [{"x": "junk", "y": None, "w": 1, "h": 1}],
])
def test_unusable_boxes_return_nothing_rather_than_a_small_number(boxes):
    """None means 'not measured'. A small number means 'a small food'. Confusing
    the two would log a 200 g portion as 20 g and call it measured."""
    got = measure_items(_plate_photo(), boxes, PLATE)
    assert len(got) == len(boxes)
    assert all(g is None for g in got)


def test_an_empty_scene_measures_nothing():
    blank = np.full((300, 300, 3), 250, np.uint8)
    got = measure_items(blank, [{"x": 0.3, "y": 0.3, "w": 0.3, "h": 0.3}], PLATE)
    assert got == [None]


def test_no_plate_box_is_allowed():
    """Food on paper or a board has no vessel, and that is not an error."""
    img = _plate_photo()
    got = measure_items(img, [{"x": 0.26, "y": 0.26, "w": 0.18, "h": 0.18}], None)
    assert got[0] is not None


# ---------------------------------------------------------------------------
# Learning the plate's colour instead of assuming it
# ---------------------------------------------------------------------------
def _lit_plate(tint=(208, 200, 188), gradient=0.0, W=600, H=600):
    """A plate of a given colour, optionally lit from one side, with food on it.

    `tint` moves the plate's colour around. A fixed threshold has to pick one
    answer for every plate ever photographed; this is the test that it cannot.
    """
    img = np.full((H, W, 3), 232, np.uint8)
    for gx in range(0, W, 140):
        for gy in range(0, H, 140):
            cv2.circle(img, (gx, gy), 18, (120, 150, 190), -1)
    cv2.ellipse(img, (W // 2, H // 2), (int(W * 0.34), int(H * 0.32)),
                0, 0, 360, tint, -1)
    for cx, cy, hue in ((0.35, 0.35, 90), (0.62, 0.60, 70)):
        colour = tuple(int(v) for v in cv2.cvtColor(
            np.uint8([[[hue, 200, 150]]]), cv2.COLOR_HSV2RGB)[0][0])
        cv2.circle(img, (int(cx * W), int(cy * H)), int(0.09 * W), colour, -1)
    if gradient:
        ramp = np.linspace(1.0 - gradient, 1.0 + gradient, W, dtype=np.float32)
        img = np.clip(img.astype(np.float32) * ramp[None, :, None], 0, 255).astype(np.uint8)
    # Sensor noise. Without it this fixture tests a regime that does not exist:
    # a perfectly uniform plate has no measurable spread at all, and every
    # method that scales by the plate's own variation divides by nothing.
    rng = np.random.default_rng(7)
    img = np.clip(img.astype(np.int16) + rng.normal(0, 3.0, img.shape), 0, 255)
    return img.astype(np.uint8)


def test_it_finds_food_on_plates_of_different_colours():
    """The failure that sent the fixed rule back to the drawing board: one set
    of constants over-caught by 25% on one photo and under-caught by 17% on
    another photo of the SAME plate. A rule that reads the plate off the rim
    has no constant to get wrong."""
    found = [food_on_plate(_lit_plate(tint=t), PLATE).mean()
             for t in ((208, 200, 188), (176, 172, 168), (230, 224, 205))]
    assert all(f > 0.005 for f in found), "lost the food on some plate colour"
    assert max(found) < min(found) * 1.6, (
        f"the answer swung with the plate's colour: {found}"
    )


def test_a_lighting_gradient_does_not_become_food():
    """A plate lit from one side measured 37 levels of L across its own rim on
    the bench photos, and the shadowed half read as food."""
    flat = food_on_plate(_lit_plate(), PLATE).mean()
    lit = food_on_plate(_lit_plate(gradient=0.22), PLATE).mean()
    assert lit < flat * 1.5, f"the shadow was counted as food: {flat} -> {lit}"


def test_flattening_leaves_the_food_alone():
    """It has to remove the gradient without removing what we are measuring."""
    img = _lit_plate()
    before = food_on_plate(img, PLATE).mean()
    after = food_on_plate(flatten_illumination(img), PLATE).mean()
    assert after == pytest.approx(before, rel=0.35)


@pytest.mark.parametrize("pb", [
    None, {}, {"x": "a", "y": None, "w": 1, "h": 1}, [1, 2, 3], "plate", 0,
    {"x": float("nan"), "y": 0, "w": 0.5, "h": 0.5},
    {"x": float("inf"), "y": 0, "w": 0.5, "h": 0.5},
    {"x": 0, "y": 0, "w": 1e300, "h": 1e300},
    {"x": 0.1, "y": 0.1, "w": -0.5, "h": 0.5},
])
def test_a_malformed_plate_box_costs_nothing_worse_than_the_fence(pb):
    """Every field the model sends can be a string, a null or an infinity. None
    of them may reach OpenCV, and none may become a measurement."""
    img = _lit_plate()
    assert food_on_plate(img, pb) is None or food_on_plate(img, pb).dtype == bool
    assert surface_mask(img, pb) is None or surface_mask(img, pb).dtype == bool
    got = measure_items(img, [{"x": 0.26, "y": 0.26, "w": 0.18, "h": 0.18}], pb)
    assert len(got) == 1


def test_a_rim_too_small_to_sample_returns_nothing():
    """Better no measurement than one modelled on twenty pixels."""
    assert food_on_plate(_lit_plate(), {"x": 0.49, "y": 0.49, "w": 0.02, "h": 0.02}) is None


def test_a_rim_that_landed_on_food_refuses_to_answer():
    """The failure that made this method lose on the real bench.

    The rim is only a sample of the plate if the plate box is right. When it is
    not, the annulus lands on FOOD, the model learns the food's colour, and real
    food stops looking different from "plate" -- the measurement does not
    degrade, it inverts. Measured on a bench photo by shrinking a known-good
    box: 44.2% of the plate came back as food with the correct box, 1.8% at 30%
    too small, 0.0% at 50% too small. On the live bench that shipped a weighed
    146 g drumstick as 12 g.

    None means "not measured" and the caller falls back to the fixed rule. A
    small number would be a lie with a decimal point on it.
    """
    img = _lit_plate()
    good = food_on_plate(img, PLATE)
    assert good is not None and good.mean() > 0.005

    # A box centred on the food itself: the rim samples food, not plate.
    assert food_on_plate(img, ON_THE_FOOD) is None, "reported a collapsed measurement"


def test_a_refused_plate_falls_back_rather_than_returning_nothing():
    """Refusing the adaptive answer must not cost the item its measurement --
    the fixed rule still works when there is no plate to confuse food with."""
    img = _lit_plate()
    got = measure_items(img, [{"x": 0.26, "y": 0.26, "w": 0.18, "h": 0.18}], ON_THE_FOOD)
    assert got[0] is not None and got[0] > 0.001


# --- the shadow measurement must never touch a gram ---------------------------

def test_a_measured_footprint_uses_the_measured_heights_not_the_priors():
    """The two height tables are not interchangeable and swapping them is a
    silent 24% error.

    HEIGHT_PRIORS_MM is calibrated to the RAILED area, which is smaller than the
    food's real footprint. MEASURED_HEIGHTS_MM goes with a true footprint.
    Pairing a true footprint with the priors counts the same correction twice.
    """
    from app.services.ai.food_seg import MEASURED_HEIGHTS_MM, grams_from_measured_area
    from app.services.ai.portion import HEIGHT_PRIORS_MM, PROFILE_FACTORS, density_for

    area, frame = 0.0449, 90800.0
    got = grams_from_measured_area("refried beans", area, frame)
    shape = "mound"
    expected = (area * frame * MEASURED_HEIGHTS_MM[shape]
                * PROFILE_FACTORS[shape] * density_for("refried beans") / 1000.0)
    assert got == pytest.approx(expected, abs=0.05)   # the helper rounds to 0.1 g
    assert MEASURED_HEIGHTS_MM[shape] != HEIGHT_PRIORS_MM[shape], (
        "if these ever converge, delete one of them"
    )


def test_a_measured_footprint_reproduces_the_weighed_plates():
    """Photo 14, whose mask is visibly correct: three foods, one kitchen scale.
    Footprints measured by `dev mask`."""
    from app.services.ai.food_seg import grams_from_measured_area
    frame = 148900.0
    for name, area, weighed, tolerance in (
        ("refried beans",     0.0425, 109, 0.15),
        ("mexican rice",      0.0460,  72, 0.15),
        ("chicken drumstick", 0.0374, 146, 0.20),
    ):
        got = grams_from_measured_area(name, area, frame)
        assert got is not None
        assert abs(got / weighed - 1) < tolerance, f"{name}: {got} g vs {weighed} g"


@pytest.mark.parametrize("area,frame", [
    (0.0, 90800.0), (-0.1, 90800.0), (0.05, 0.0), (0.05, -1.0),
    ("junk", 90800.0), (None, 90800.0), (0.05, None), (0.99, 1e9),
])
def test_a_footprint_that_cannot_be_used_returns_nothing(area, frame):
    """None means "not measured" and the caller falls back. A number here would
    be a lie with a decimal point on it -- the same rule food_on_plate follows
    when the rim lands on the food."""
    from app.services.ai.food_seg import grams_from_measured_area
    assert grams_from_measured_area("rice", area, frame) is None


# ---------------------------------------------------------------------------
# The segmenter, connected
#
# `segment_hosted.py` was written, tested with twenty tests, and called by
# nothing. That is the defect class this project keeps producing -- something
# built, tested, and never connected -- and these tests exist to make it a test
# failure rather than a discovery weeks later.
# ---------------------------------------------------------------------------

class _FakeSegmenter:
    """A segmenter that returns exactly the masks it is handed."""

    name = "sam2:fake"

    def __init__(self, masks=None, plate=None, up=True):
        self._masks = masks
        self._plate = plate
        self._up = up
        self.points_seen = None
        self.plate_calls = 0

    def available(self):
        return self._up

    def segment(self, rgb, points):
        from app.services.ai.segmenter import Segmentation
        self.points_seen = list(points)
        if self._masks is None:
            return [None] * len(points)
        return [None if m is None else Segmentation(mask=m, score=None,
                                                    source=self.name)
                for m in self._masks]

    def plate_outline(self, rgb, plate_bbox):
        self.plate_calls += 1
        return self._plate


def _blob(shape, cx, cy, r):
    H, W = shape
    yy, xx = np.ogrid[:H, :W]
    return ((xx - cx) ** 2 + (yy - cy) ** 2) <= r * r


def _use(monkeypatch, seg):
    import app.services.ai.food_seg as F
    monkeypatch.setattr(F, "_SEGMENTER", seg)
    return F


def test_the_segmenter_is_actually_called(monkeypatch):
    """The whole point. A segmenter that is configured must be reached."""
    rgb = _plate_photo()
    H, W = rgb.shape[:2]
    masks = [_blob((H, W), int(0.35 * W), int(0.35 * H), 60),
             _blob((H, W), int(0.62 * W), int(0.60 * H), 50)]
    seg = _FakeSegmenter(masks=masks)
    F = _use(monkeypatch, seg)
    boxes = [{"x": 0.30, "y": 0.30, "w": 0.10, "h": 0.10},
             {"x": 0.57, "y": 0.55, "w": 0.10, "h": 0.10}]
    out, source = F.item_masks_with_source(rgb, boxes, PLATE)
    assert source == "sam2"
    assert seg.points_seen is not None, "the segmenter was never called"
    assert [m.sum() for m in out] == [m.sum() for m in masks]


def test_it_is_prompted_with_centres_not_boxes(monkeypatch):
    """A POINT, deliberately.

    The model's box is quantised to a 0.05 grid -- 82% of values land exactly on
    it where chance would put 20% -- and at 3% of frame one grid step is 45% of
    the food's weight. The centre is a location, not a size, and locating is the
    thing the model is good at. Handing a segmenter the box feeds the
    quantisation straight back in.
    """
    rgb = _plate_photo()
    H, W = rgb.shape[:2]
    seg = _FakeSegmenter(masks=[_blob((H, W), 210, 210, 60)])
    F = _use(monkeypatch, seg)
    F.item_masks_with_source(rgb, [{"x": 0.30, "y": 0.30, "w": 0.10, "h": 0.10}],
                             PLATE)
    assert seg.points_seen == [(0.35 * W, 0.35 * H)]


def test_a_segmenter_that_misses_one_item_falls_back_whole(monkeypatch):
    """All of it or none of it.

    A plate with one unmeasured item cannot produce a split: the missing food
    lands on whichever items did work, which are precisely the ones that looked
    fine. Photo 06 -- two seeds failed and the rice was published as 100% of a
    plate it was about a third of.
    """
    rgb = _plate_photo()
    H, W = rgb.shape[:2]
    seg = _FakeSegmenter(masks=[_blob((H, W), 210, 210, 60), None])
    F = _use(monkeypatch, seg)
    boxes = [{"x": 0.30, "y": 0.30, "w": 0.10, "h": 0.10},
             {"x": 0.57, "y": 0.55, "w": 0.10, "h": 0.10}]
    _out, source = F.item_masks_with_source(rgb, boxes, PLATE)
    assert source != "sam2", "half a plate was published as a measurement"


def test_a_mask_that_returned_almost_nothing_is_refused(monkeypatch):
    """The failure that a missing mask does NOT cover, and the one that has
    already shipped.

    A segmenter can return a perfectly valid mask of almost nothing -- a few
    stray pixels where a drumstick is -- and that is not None, so nothing in the
    fallback path notices. It shipped a weighed 146 g drumstick as 12 g. The
    gate in segmenter.py is the only thing standing between that mask and a
    number on somebody's screen, so this test exists to fail if the gate is
    skipped, which a test built on a None mask cannot do.
    """
    rgb = _plate_photo()
    H, W = rgb.shape[:2]
    seg = _FakeSegmenter(masks=[_blob((H, W), 210, 210, 60),
                                _blob((H, W), 370, 360, 4)])   # ~0.0001 of frame
    F = _use(monkeypatch, seg)
    boxes = [{"x": 0.30, "y": 0.30, "w": 0.10, "h": 0.10},
             {"x": 0.57, "y": 0.55, "w": 0.10, "h": 0.10}]
    _out, source = F.item_masks_with_source(rgb, boxes, PLATE)
    assert source != "sam2", "a mask of almost nothing was published as a portion"


def test_masks_that_swallowed_the_whole_plate_are_refused(monkeypatch):
    """The other end of the same gate. One item claiming most of the picture on
    a plate holding several foods means the mask merged them, and a merged mask
    weighs the whole meal twice."""
    rgb = _plate_photo()
    H, W = rgb.shape[:2]
    huge = _blob((H, W), W // 2, H // 2, int(0.48 * W))
    F = _use(monkeypatch, _FakeSegmenter(masks=[huge, huge.copy()]))
    boxes = [{"x": 0.30, "y": 0.30, "w": 0.10, "h": 0.10},
             {"x": 0.57, "y": 0.55, "w": 0.10, "h": 0.10}]
    _out, source = F.item_masks_with_source(rgb, boxes, PLATE)
    assert source != "sam2", "two masks of the whole plate were believed"


def test_a_segmenter_that_is_down_costs_a_measurement_not_a_scan(monkeypatch):
    """Every failure here falls back to the colour rule, which is where every
    scan is today. A provider outage must never be a 500."""
    rgb = _plate_photo()

    class _Exploding(_FakeSegmenter):
        def segment(self, rgb, points):
            raise RuntimeError("provider down")

    F = _use(monkeypatch, _Exploding(masks=[]))
    boxes = [{"x": 0.30, "y": 0.30, "w": 0.10, "h": 0.10}]
    out, source = F.item_masks_with_source(rgb, boxes, PLATE)
    assert source != "sam2"
    assert len(out) == len(boxes)


def test_only_a_segmented_footprint_counts_as_an_area():
    """The line the whole wiring rests on, written down once."""
    from app.services.ai import food_seg as F
    from app.services.ai import segment_hosted as S
    assert F.area_is_absolute("sam2") is True
    assert F.area_is_absolute("colour") is False
    assert F.area_is_absolute("none") is False
    assert F.area_is_absolute(None) is False


def test_a_segmented_plate_carries_shape_and_a_box_does_not():
    """The depth scale rests on the plate's foreshortening, so it must accept
    only sources that measured the plate. A tilt survey over sixteen bench
    photographs reported eight of them at 41.4 degrees to one decimal place --
    arccos(3/4), the aspect ratio of the photographs -- because a box ellipse's
    axis ratio is the box's."""
    from app.services.ai import food_seg as F
    from app.services.ai import segment_hosted as S
    assert F.plate_is_measured("sam2") is True
    assert F.plate_is_measured("pixels") is True
    assert F.plate_is_measured("box") is False
    assert F.plate_is_measured("none") is False
    assert F.plate_is_measured(None) is False


def test_the_plate_outline_is_tried_before_the_box(monkeypatch):
    """The box is the FALLBACK, not the first answer.

    Six pixel-only methods have failed at finding a plate outline, and the box
    path has been the reason the depth scale is blocked: it fences food
    correctly and carries no shape at all. If the segmenter is only consulted
    after the box, it is never consulted.

    The outline now has to AGREE with the Hough circle to be taken, so this one
    is drawn on the plate this fixture actually paints -- semi-axes 204x192 at
    the centre. The old radius here was 0.42W = 252px, a quarter too large,
    which is a disagreement and is tested as one below.
    """
    rgb = _plate_photo()
    H, W = rgb.shape[:2]
    outline = _blob((H, W), W // 2, H // 2, int(0.33 * W))
    seg = _FakeSegmenter(plate=outline)
    F = _use(monkeypatch, seg)
    mask, source = F.plate_surface(rgb, PLATE)
    assert seg.plate_calls == 1
    assert source == "sam2"
    assert mask.sum() == outline.sum()


def test_the_plate_outline_is_not_eroded(monkeypatch):
    """The box and colour paths inset by 3% so the rim's shadow is not read as
    food. This one must not: the mask's own axes become the plate's diameter in
    pixels, and a 3% erosion is a 3% error in mm-per-pixel -- about 6% on every
    gram. A slightly generous fence costs less than a deliberately wrong scale.
    """
    rgb = _plate_photo()
    H, W = rgb.shape[:2]
    # Agreeing, so the outline is the mask that comes back -- otherwise this
    # would be asserting that the HOUGH circle is not eroded, which is a
    # different claim and one nothing rests on.
    outline = _blob((H, W), W // 2, H // 2, int(0.33 * W))
    F = _use(monkeypatch, _FakeSegmenter(plate=outline))
    mask, _ = F.plate_surface(rgb, PLATE)
    assert mask.sum() == outline.sum()


def test_a_refused_plate_outline_falls_back_to_the_circle(monkeypatch):
    """None is a supported answer, and the honest one. A wrong plate outline is
    worse than none at all, because both the depth scale and the footprint rest
    on it.

    It falls back to HOUGH now, not to the box. The box is a fence drawn from
    the model's 0.05-grid bounding box; the circle is a located plate, 30/30.
    Reaching the box at all means Hough declined too.
    """
    rgb = _plate_photo()
    F = _use(monkeypatch, _FakeSegmenter(plate=None))
    mask, source = F.plate_surface(rgb, PLATE)
    assert source == "circle"
    assert mask is not None and mask.any()


def test_the_box_is_reached_only_when_nothing_locates_the_plate(monkeypatch):
    """The box branch used to return UNCONDITIONALLY on any sane plate_bbox,
    which is every real scan -- so the Hough detector underneath it never ran
    on the path production takes, and its 30/30 was measured through
    plate_surface(rgb, None), a door only the tooling uses.

    Pure noise: no circle to find and no outline offered. Only then the box.
    """
    import numpy as np

    noise = np.random.RandomState(0).randint(0, 255, (600, 600, 3), dtype=np.uint8)
    F = _use(monkeypatch, _FakeSegmenter(plate=None))
    _mask, source = F.plate_surface(noise, PLATE)
    assert source == "box"


def test_an_outline_that_disagrees_with_the_circle_is_not_used(monkeypatch):
    """SAM2's outline is the only plate source carrying foreshortening AND the
    only one never checked against a tape. Hough's circle has been, 30/30, and
    can say WHERE but never WHAT SHAPE -- its axis ratio is 1.0 by
    construction. So the located detector vouches for the shape-bearing one.

    Both failures it has to catch, measured on this fixture, whose plate is
    204x192 at the centre:

      too large      the old 0.42W outline, IoU 0.636 against the circle
      displaced      right size, centred on the tablecloth instead
    """
    rgb = _plate_photo()
    H, W = rgb.shape[:2]

    oversized = _blob((H, W), W // 2, H // 2, int(0.42 * W))
    F = _use(monkeypatch, _FakeSegmenter(plate=oversized))
    _m, source = F.plate_surface(rgb, PLATE)
    assert source == "circle", "a quarter-too-large outline was taken as the plate"

    displaced = _blob((H, W), 120, 120, int(0.33 * W))
    F = _use(monkeypatch, _FakeSegmenter(plate=displaced))
    _m, source = F.plate_surface(rgb, PLATE)
    assert source == "circle", "an outline on the tablecloth was taken as the plate"


def test_an_outline_is_still_taken_when_there_is_no_circle_to_check_it(monkeypatch):
    """Agreement is corroboration, not a second detector. Requiring it where
    Hough declined would lose the depth scale on exactly the photographs that
    have no circle -- a real loss traded for no evidence, since there is
    nothing there to disagree with."""
    import numpy as np

    noise = np.random.RandomState(0).randint(0, 255, (600, 600, 3), dtype=np.uint8)
    outline = _blob((600, 600), 300, 300, 200)
    F = _use(monkeypatch, _FakeSegmenter(plate=outline))
    _m, source = F.plate_surface(noise, PLATE)
    assert source == "sam2"


def test_no_segmenter_configured_changes_nothing(monkeypatch):
    """The default. NullSegmenter is what ships until a provider is set, and on
    that deployment every one of these paths behaves exactly as it did before
    any of this existed."""
    import app.services.ai.food_seg as F
    from app.services.ai.segmenter import NullSegmenter

    rgb = _plate_photo()
    boxes = [{"x": 0.30, "y": 0.30, "w": 0.10, "h": 0.10}]
    monkeypatch.setattr(F, "_SEGMENTER", NullSegmenter())
    with_null, source_null = F.item_masks_with_source(rgb, boxes, PLATE)
    plate_null, plate_source = F.plate_surface(rgb, PLATE)
    assert source_null != "sam2"
    # The circle, not the box: Hough needs no segmenter and runs on every
    # deployment, configured or not.
    assert plate_source == "circle"
    assert plate_null is not None
    assert len(with_null) == 1


def test_a_hole_the_size_of_a_plate_is_not_filled_in_as_food():
    """The bug this pins cost every gram on 12 of 13 weighed photographs.

    food_mask calls a pixel "food" when it is saturated or dark. A wooden
    tabletop is both. So the food blob became the whole BACKGROUND, the bright
    plate sitting in the middle of it was an enclosed hole, and hole-filling
    swallowed it -- plate, food and table returned as one region covering
    100.0% of the frame. Every footprint measured from it was the whole photo.

    Small gaps must still fill: a pile of rice is hundreds of tiny holes and
    all of them are rice. So this asserts both directions. A cap that is
    removed, or raised past a plate, fails on the first assertion; a cap that
    is dropped to zero fails on the second.
    """
    import numpy as np
    from app.services.ai.food_seg import _fill_holes

    frame = np.ones((400, 400), np.uint8)          # "everything is food"
    frame[100:300, 100:300] = 0                    # ...around a plate-sized hole
    frame[20:40, 20:40] = 0                        # ...and a crumb-sized one
    out = _fill_holes(frame).astype(bool)

    plate = out[100:300, 100:300]
    assert not plate.any(), (
        f"a hole covering {200 * 200 / frame.size:.0%} of the frame was filled in "
        f"as food -- {plate.mean():.0%} of it came back solid")
    gap = out[20:40, 20:40]
    assert gap.all(), (
        "a gap covering 0.25% of the frame was left open -- gaps between rice "
        f"grains are still rice, and {1 - gap.mean():.0%} of this one is missing")


def _sam2_shaped_plate(H=400, W=400, holes=((150, 150, 40), (250, 250, 35))):
    """A plate the way SAM2 actually returns one: WITH THE FOOD PUNCHED OUT.

    Every fixture in this suite drew a plate as a solid ellipse, which is why
    a whole class of defect walked past a green suite twice. Rule 0 reads the
    holes, so a solid plate would test nothing at all.
    """
    import numpy as np

    yy, xx = np.ogrid[:H, :W]
    plate = ((yy - H / 2) ** 2 + (xx - W / 2) ** 2) <= (H * 0.42) ** 2
    for cy, cx, r in holes:
        plate &= ~(((yy - cy) ** 2 + (xx - cx) ** 2) <= r ** 2)
    return plate


def _tabletop(H=400, W=400):
    """The table: bigger, runs off the frame, and has the PLATE as one hole."""
    import numpy as np

    yy, xx = np.ogrid[:H, :W]
    table = np.ones((H, W), bool)
    table &= ~(((yy - H / 2) ** 2 + (xx - W / 2) ** 2) <= (H * 0.42) ** 2)
    return table


def test_rule_zero_finds_the_plate_and_never_the_tabletop():
    """The plate has small holes; the table has one hole the size of a plate."""
    from app.services.ai import food_seg as F
    from app.services.ai import segment_hosted as S

    plate = _sam2_shaped_plate()
    table = _tabletop()
    got = S.plate_among([table, plate], (400, 400))
    assert got is not None, "no plate found among a plate and a table"
    assert got is plate, "the tabletop was taken for the plate"

    # And with only a tabletop on offer it refuses rather than guessing.
    assert S.plate_among([table], (400, 400)) is None


def test_rule_zero_reads_the_food_out_of_the_plates_holes():
    """The holes ARE the food, and each goes to the box it falls inside."""
    import numpy as np

    from app.services.ai import food_seg as F
    from app.services.ai import segment_hosted as S

    plate = _sam2_shaped_plate()
    filled = S.fill_outer(plate)
    holes = filled & ~plate
    assert holes.any(), "the fixture has no holes to read"
    # Two foods, one per hole, boxes in normalised coordinates.
    boxes = [{"x": 0.30, "y": 0.30, "w": 0.16, "h": 0.16},
             {"x": 0.55, "y": 0.55, "w": 0.16, "h": 0.16}]

    from app.services.ai.portion import normalize_bbox
    H = W = 400
    n, labels, stats, _c = __import__("cv2").connectedComponentsWithStats(
        holes.astype(np.uint8), 8)
    assigned = [np.zeros((H, W), bool) for _ in boxes]
    for i in range(1, n):
        blob = labels == i
        ys, xs = np.nonzero(blob)
        cy, cx = float(ys.mean()) / H, float(xs.mean()) / W
        for j, b in enumerate(boxes):
            nb = normalize_bbox(b)
            if (nb["x"] <= cx <= nb["x"] + nb["w"]
                    and nb["y"] <= cy <= nb["y"] + nb["h"]):
                assigned[j] |= blob
    assert all(m.any() for m in assigned), (
        "a hole was not assigned to the box it sits in")
    # Each food is its own hole, not both of them.
    assert not (assigned[0] & assigned[1]).any()


def test_rule_zero_refuses_when_any_item_gets_nothing():
    """A plate with one unmeasured item cannot produce an honest split.

    The missing food would land on whichever items DID work -- precisely the
    ones that looked fine. The segmenter path already refuses for this reason;
    Rule 0 must refuse on the same terms.
    """
    from app.services.ai import food_seg as F
    from app.services.ai import segment_hosted as S

    class _Seg:
        mode = "auto"
        def available(self):
            return True
        def encode_image(self, rgb):
            return b"x"
        def _auto_masks(self, image, shape):
            return [_tabletop(), _sam2_shaped_plate()]

    import app.services.ai.food_seg as mod
    old = mod.segmenter
    mod.segmenter = lambda: _Seg()
    try:
        import numpy as np
        rgb = np.zeros((400, 400, 3), np.uint8)
        # Three boxes, but only two holes exist -- the third gets nothing.
        boxes = [{"x": 0.30, "y": 0.30, "w": 0.16, "h": 0.16},
                 {"x": 0.55, "y": 0.55, "w": 0.16, "h": 0.16},
                 {"x": 0.02, "y": 0.02, "w": 0.05, "h": 0.05}]
        assert F._items_from_plate_holes(rgb, boxes) is None, (
            "Rule 0 published a split with an item it never measured")
        # With only the two real items it must succeed.
        got = F._items_from_plate_holes(rgb, boxes[:2])
        assert got is not None and all(m.any() for m in got)
    finally:
        mod.segmenter = old


def _round_table(H=400, W=400):
    """A tabletop that is round and inside the frame, with the plate as a hole.

    The other fixture's table is rejected for running off the frame, so the
    hole test never actually fires on it -- a mutation that set the hole limit
    to 5.0 survived the whole suite. This one can ONLY be rejected by its hole:
    it is round, it fits, and it is the right size for a plate.
    """
    import numpy as np

    yy, xx = np.ogrid[:H, :W]
    outer = ((yy - H / 2) ** 2 + (xx - W / 2) ** 2) <= (H * 0.47) ** 2
    inner = ((yy - H / 2) ** 2 + (xx - W / 2) ** 2) <= (H * 0.30) ** 2
    return outer & ~inner


def test_a_round_tabletop_is_rejected_by_its_hole_alone():
    """One hole the size of a plate is what makes a tabletop a tabletop."""
    from app.services.ai import food_seg as F
    from app.services.ai import segment_hosted as S

    table = _round_table()
    plate = _sam2_shaped_plate()

    # It passes every other test: round, in frame, plate-sized.
    ys, xs = table.nonzero()
    hh, ww = ys.max() - ys.min() + 1, xs.max() - xs.min() + 1
    assert max(hh, ww) / min(hh, ww) <= S.PLATE_MAX_ASPECT
    assert hh <= 0.95 * 400 and ww <= 0.95 * 400
    assert S.PLATE_MIN_FRAME <= table.mean() <= S.PLATE_MAX_FRAME

    assert S.plate_among([table], (400, 400)) is None, (
        "a round tabletop with the plate cut out of it was taken for a plate")
    assert S.plate_among([table, plate], (400, 400)) is plate


def test_rule_zero_is_actually_reached_by_the_measuring_path():
    """Built, tested and never called is how this went wrong before.

    Every other Rule 0 test calls _items_from_plate_holes directly, and all of
    them stay green with nothing calling it. This one goes in the front door:
    the segmenter's per-box masks come back unusable, and the footprints must
    still arrive, marked "sam2", read out of the plate's holes.
    """
    import numpy as np

    from app.services.ai import food_seg as F
    from app.services.ai import segment_hosted as S

    class _Seg:
        mode = "auto"
        def available(self):
            return True
        def encode_image(self, rgb):
            return b"x"
        def segment(self, rgb, points):
            return []                      # unusable -> _segmented_items None
        def _auto_masks(self, image, shape):
            return [_tabletop(), _sam2_shaped_plate()]

    old = F.segmenter
    F.segmenter = lambda: _Seg()
    try:
        rgb = np.zeros((400, 400, 3), np.uint8)
        boxes = [{"x": 0.30, "y": 0.30, "w": 0.16, "h": 0.16},
                 {"x": 0.55, "y": 0.55, "w": 0.16, "h": 0.16}]
        masks, source = F.item_masks_with_source(rgb, boxes)
        assert source == "sam2", (
            f"Rule 0 was not reached: source came back {source!r}")
        assert len(masks) == 2 and all(m is not None and m.any() for m in masks)
        # And it is an ABSOLUTE area, on the same terms as the segmenter path.
        assert F.area_is_absolute(source)
    finally:
        F.segmenter = old


def test_food_seg_actually_hands_the_plate_to_the_segmenter():
    """Cutting this wire left the whole suite green. Again.

    Every test of the bound itself passes plate_hint in by hand, so all of them
    stay green with food_seg passing None. This one captures what food_seg
    really hands over.
    """
    import numpy as np

    from app.services.ai import food_seg as F

    seen = {}

    class _Seg:
        mode = "auto"
        def available(self):
            return True
        def segment_boxes(self, rgb, points, boxes, plate_hint=None):
            seen["hint"] = plate_hint
            return []                      # unusable, so the rest falls through
        def segment(self, rgb, points):
            return []

    old_seg = F.segmenter
    old_plate = F.plate_surface
    marker = np.zeros((40, 40), bool)
    marker[10:30, 10:30] = True
    F.segmenter = lambda: _Seg()
    F.plate_surface = lambda rgb, bbox=None: (marker, "pixels")
    try:
        rgb = np.zeros((40, 40, 3), np.uint8)
        F.item_masks_with_source(rgb, [{"x": 0.2, "y": 0.2, "w": 0.4, "h": 0.4}])
    finally:
        F.segmenter = old_seg
        F.plate_surface = old_plate

    assert "hint" in seen, "segment_boxes was never called"
    assert seen["hint"] is not None, (
        "food_seg found a plate and handed the segmenter None")
    assert bool((seen["hint"] & marker).any()), (
        "the mask handed over is not the plate food_seg found")


def _photo_plate(cloth: bool, H=600, W=600):
    """A plate on a background, drawn the way each surface actually behaves.

    Measured on the real photographs, and the two are opposite problems:
      wood    plate 169-197 in value against 82-111, background saturated
      cloth   plate 135-196 against 100-159, BOTH unsaturated, pattern on top

    The cloth case is the one seven methods failed on, so a fixture that only
    draws the wood case tests nothing that was ever broken.
    """
    import numpy as np

    rng = np.random.default_rng(7)
    img = np.zeros((H, W, 3), np.uint8)
    if cloth:
        img[:] = (205, 208, 205)
        # A printed pattern: pale, unsaturated, and everywhere.
        for _ in range(90):
            cy, cx = rng.integers(0, H), rng.integers(0, W)
            cv2 = __import__("cv2")
            cv2.circle(img, (int(cx), int(cy)), int(rng.integers(18, 46)),
                       (168, 182, 178), -1)
    else:
        img[:] = (120, 78, 40)                    # saturated wood
    cv2 = __import__("cv2")
    cv2.circle(img, (W // 2, H // 2), int(min(H, W) * 0.36), (238, 240, 240), -1)
    # A rim: the edge Hough votes on.
    cv2.circle(img, (W // 2, H // 2), int(min(H, W) * 0.36), (196, 198, 198), 5)
    return img


def test_the_plate_is_found_on_a_patterned_surface_too():
    """Seven methods failed here, and plate_surface returned None 7 times of 7.

    Every photograph in the bench was shot on bare wood, so the patterned case
    had never been tested. It is the top rung of the whole ladder: no plate
    means no scale, no bound, and Rule 0 cannot fire either.
    """
    from app.services.ai import food_seg as F

    for cloth in (False, True):
        rgb = _photo_plate(cloth)
        mask, src = F.plate_surface(rgb, None)
        where = "patterned cloth" if cloth else "plain wood"
        assert mask is not None and mask.any(), f"no plate found on {where}"
        assert src == F.CIRCLE_PLATE_SOURCE, f"{where} fell back to {src!r}"
        assert not F.plate_is_measured(src), (
            "a drawn circle must never count as a measured ellipse -- its axis\n"
            "            ratio is 1.0 by construction and would report every photo as overhead")
        share = float(mask.mean())
        assert 0.20 < share < 0.65, (
            f"{where}: plate came back as {share:.0%} of frame")
        # And it is round and centred, not some corner of the background.
        ys, xs = mask.nonzero()
        H, W = mask.shape
        assert abs(ys.mean() - H / 2) < H * 0.12
        assert abs(xs.mean() - W / 2) < W * 0.12


def test_a_small_round_object_is_not_taken_for_the_plate():
    """The radius band is what keeps this on a plate rather than a bottle cap.

    Every one of these photographs has a credit card in it, and plenty of food
    is round. Without the band, the strongest circular agreement in the frame
    is not necessarily the plate. Widening HOUGH_R_MIN/MAX must make this fail.
    """
    import cv2
    import numpy as np

    from app.services.ai import food_seg as F

    H = W = 700
    img = np.full((H, W, 3), 120, np.uint8)
    # A small, very high-contrast disc -- a far crisper circle than any plate.
    cv2.circle(img, (W // 2, H // 2), int(min(H, W) * 0.07), (255, 255, 255), -1)
    cv2.circle(img, (W // 2, H // 2), int(min(H, W) * 0.07), (0, 0, 0), 4)

    got = F._plate_by_hough(img)
    if got is not None:
        r = np.sqrt(float(got.sum()) / np.pi)
        assert r >= F.HOUGH_R_MIN * min(H, W) * 0.9, (
            f"a disc of radius {min(H, W) * 0.07:.0f} was taken for the plate")


def test_a_photograph_with_no_plate_still_gets_no_plate():
    """Voting for a circle must not invent one on a bare surface.

    None is a supported answer everywhere downstream, and a wrong plate is
    worse than none because the scale for every gram rests on it.
    """
    import numpy as np

    from app.services.ai import food_seg as F

    rng = np.random.default_rng(3)
    noise = rng.integers(90, 140, size=(600, 600, 3), dtype=np.uint8)
    mask, src = F.plate_surface(noise, None)
    assert mask is None or not mask.any(), f"invented a plate ({src}) in noise"
