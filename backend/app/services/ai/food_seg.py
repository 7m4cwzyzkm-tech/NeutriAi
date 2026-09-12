"""Measure each food's footprint from the pixels, without trusting a box.

WHY THIS EXISTS, AND WHY THE OBVIOUS VERSION DOES NOT WORK
----------------------------------------------------------
The vision model reports two numbers about how big a food is, and both are
broken in the same direction. Across a weighed 11-meal bench its `area_ratio`
came back larger than the item's own bounding box on 20 of 22 items, by 2x to
6x, and never once smaller. So the box looked like the trustworthy one -- and it
is better, but it is not trustworthy. Measured twice against a ruler on the same
weighed 146 g chicken leg:

    photo 08    boxed at 4.00% of frame    real footprint 4.84%
    photo 14    boxed at 3.00% of frame    real footprint 4.50%

Food cannot exceed its own bounding box. Those boxes are not inaccurate, they
are impossible. The model draws them small.

The obvious fix -- hand each box to GrabCut and let it find the food inside --
was built and measured, and it LOST: 50.9% mean absolute against the model's own
38.3%. GrabCut only ever looks inside the rectangle it is given, so it inherits
the undersizing almost exactly:

    photo 14 drumstick, true footprint 4.50% of frame
      seeded with a good box                3.34%
      seeded with a box shrunk 20%          2.53%
      seeded with a box shrunk 35%          1.63%
      that same bad box, grown back 1.6x    3.36%

Every method that STARTS from the box inherits the box's error. That is the
whole lesson, and this module is built around avoiding it.

WHAT THIS DOES INSTEAD
----------------------
Two stages, and the boxes only enter the second one.

    1. Find all the food on the surface, using no boxes at all -- food is what
       is saturated or dark against a pale, uniform plate, paper or cloth. This
       stage already has a track record: given a trusted scale it reproduced
       weighed meals at -3.0% and -0.0%.

    2. Split that one region between the detected items by growing each from a
       seed at its box's CENTRE, letting the boundaries fall on real colour
       edges rather than on rectangle walls.

The box is thereby demoted from a measurement to a pointer. Its centre says
WHICH pile is the rice; its size says nothing. Position is the thing the model
is actually good at, and an undersized box still points at the right food.

The failure mode is honest: if the food mask misses a food entirely, that item
gets no measurement and the caller falls back rather than receiving a small
number that looks like a real one.
"""
from __future__ import annotations

import math

import cv2
import numpy as np
import structlog

log = structlog.get_logger()

# Food is saturated, or dark, or both. Plates, butcher paper, takeout boxes and
# tablecloths are all pale and washed out; cooked food is neither. These bounds
# come from segment_lab, where they were tuned against weighed meals.
FOOD_MIN_SAT = 70
FOOD_MIN_VAL = 35
FOOD_MAX_VAL = 245
FOOD_DARK_VAL = 80

# Specks are noise -- crumbs, a sauce smear, a pattern on the tablecloth. Real
# food arrives in a few large pieces.
MIN_BLOB_FRACTION = 0.002

# Below this share of the frame a measured region is more likely a segmentation
# failure than a real food, and None is returned instead.
MIN_REGION_FRACTION = 0.0015


# --- the segmenter, and what its answer is allowed to be used for -----------
#
# Built lazily and cached, not at import. `from_settings` reads config and
# constructs an HTTP client; doing that at import time means every test module
# that touches this file pays for it, and it fixes the configuration at import
# rather than at first use -- which is the one thing that makes a provider
# impossible to swap in a test.
from . import segmenter as segmenter_mod  # the Protocol this returns

_SEGMENTER = None


def segmenter() -> segmenter_mod.Segmenter:
    """The configured segmenter, built once. NullSegmenter when none is.

    The return type is the Protocol on purpose. It described the contract every
    segmenter here implements and was referenced by nothing, which is how a
    written-down interface drifts away from the code it describes.
    """
    global _SEGMENTER
    if _SEGMENTER is None:
        from . import segment_hosted
        _SEGMENTER = segment_hosted.from_settings()
    return _SEGMENTER


# WHICH PLATE SOURCES CARRY SHAPE.
#
# `plate_surface` returns a mask AND how it was arrived at, because two of the
# three ways carry no shape information at all -- see its docstring for the
# 41.4-degree survey that made this necessary. Anything resting on the plate's
# FORESHORTENING (the depth scale) must consult this set rather than comparing
# against a string, so that adding a fourth source cannot silently admit a
# box-derived ellipse by forgetting to update one comparison.
MEASURED_PLATE_SOURCES = frozenset({"pixels", "sam2"})

# "circle" IS A LOCATION, NOT A SHAPE, AND IS DELIBERATELY NOT IN THAT SET.
#
# The Hough path finds the plate on backgrounds every other method fails on,
# and what it returns is a PERFECT CIRCLE -- it votes for circles, so a circle
# is what comes back. Its axis ratio is exactly 1.0 by construction, which
# would report EVERY photograph as taken from directly overhead.
#
# That is the same defect as the 41.4-degree survey above wearing different
# clothes: a number that describes how the mask was DRAWN being read as a
# measurement of the plate. Labelling it "pixels" would have walked it straight
# past the one guard built to stop exactly this.
#
# So it is its own source. Everything that wants somewhere to look -- the food
# fence, the mask bound, Rule 0's holes -- takes it happily. Anything resting
# on foreshortening refuses it, and is no worse off than before, because
# before there was no plate at all.
CIRCLE_PLATE_SOURCE = "circle"


# HOW CLOSE TWO PLATES HAVE TO BE TO BE THE SAME PLATE.
#
# PROVISIONAL. This number has not been measured yet -- `dev platecheck`
# prints the agreement for every bench photograph and what each threshold
# would decide, and that is what sets it. It is written here rather than
# inline so there is one place to change when it is.
#
# What it is deciding: SAM2's outline is the only plate source that carries
# foreshortening, and it is also the one that has never been checked against a
# tape. Hough's circle has -- 30/30 -- but its axis ratio is 1.0 by
# construction, so it can say WHERE and never WHAT SHAPE. Agreement lets the
# measured-location detector vouch for the shape-bearing one: take SAM2's
# outline when the two describe the same plate, and Hough's circle when they
# do not, rather than trusting either alone.
PLATE_AGREEMENT_IOU = 0.70


def plates_agree(outline, circle) -> float:
    """How much two plate masks overlap, 0-1. Intersection over union.

    IoU rather than a centre distance or an area ratio because it catches both
    failures at once, and both happen: a mask centred correctly but 25% too
    large (the oversized outline), and a correctly sized mask centred on the
    wrong thing (the tablecloth).
    """
    if outline is None or circle is None:
        return 0.0
    a = np.asarray(outline, bool)
    b = np.asarray(circle, bool)
    if a.shape != b.shape or not a.any() or not b.any():
        return 0.0
    union = int((a | b).sum())
    return float((a & b).sum()) / union if union else 0.0


def plate_is_measured(source: str | None) -> bool:
    """True when this plate mask's SHAPE is a measurement of the plate itself."""
    return source in MEASURED_PLATE_SOURCES


# WHICH ITEM-MASK SOURCES CARRY ABSOLUTE AREA.
#
# The colour rule's masks are fenced by the plate box, so both terms of a share
# move together and the share survives while the absolute footprint does not --
# measured: shrinking the plate box 10% moved absolute footprints by 3-14% and
# the shares by 1-6%. A segmenter prompted with a POINT has no such dependency:
# the mask is the food, and the plate box never enters it. So a SAM2 footprint
# is an absolute measurement and a colour footprint is a share, and the weight
# path is allowed to use only the first.
ABSOLUTE_AREA_SOURCES = frozenset({"sam2"})


def area_is_absolute(source: str | None) -> bool:
    """True when this footprint may be used as an area rather than a share."""
    return source in ABSOLUTE_AREA_SOURCES


# A hole bigger than this share of the frame is not a gap in the food.
#
# MEASURED, not chosen. The unbounded version of this function returned
# 100.0% of the frame on 12 of 13 weighed photographs -- every gram on the
# plate, the plate itself, and the table. The mechanism: the tabletop trips
# the "saturated or dark" rule in food_mask, so the food blob is the whole
# BACKGROUND, the bright plate sitting in it is an enclosed hole, and the
# flood fills it. Plate, food and table came back as one region.
#
# On these photographs a plate is 39-44% of the frame and the widest real gap
# inside a piece of food -- between rice grains, between spaghetti strands --
# is under 0.05%. Two percent sits three orders of magnitude clear of the
# gaps it must fill and an order clear of the plate it must not.
MAX_HOLE_FRACTION = 0.02


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """Fill enclosed holes, OpenCV only. Small ones. See MAX_HOLE_FRACTION.

    Flood the background inwards from a border of zeros; whatever the flood
    never reaches is enclosed, and therefore inside the shape. A pile of rice
    has hundreds of tiny gaps between grains and they are all still rice --
    but a whole plate can be enclosed too, and it is not rice.
    """
    h, w = mask.shape
    padded = np.zeros((h + 2, w + 2), np.uint8)
    padded[1:-1, 1:-1] = mask
    flood = padded.copy()
    cv2.floodFill(flood, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 255)
    holes = (flood[1:-1, 1:-1] == 0).astype(np.uint8)
    if not holes.any():
        return (mask > 0).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(holes, connectivity=8)
    cap = MAX_HOLE_FRACTION * mask.size
    small = np.zeros_like(holes)
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] <= cap:
            small[labels == i] = 1
    return ((mask > 0) | small.astype(bool)).astype(np.uint8)


def food_mask(rgb: np.ndarray) -> np.ndarray:
    """Everything on the surface that is food. No boxes involved."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1].astype(int)
    val = hsv[:, :, 2].astype(int)
    mask = (
        ((sat > FOOD_MIN_SAT) & (val > FOOD_MIN_VAL) & (val < FOOD_MAX_VAL))
        | (val < FOOD_DARK_VAL)
    ).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    mask = _fill_holes(mask)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((11, 11), np.uint8))

    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.zeros_like(mask)
    floor = MIN_BLOB_FRACTION * mask.size
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] > floor:
            keep[labels == i] = 1
    return keep.astype(bool)


# A plate is the largest bright, near-elliptical region in the photo. Finding it
# matters more than it looks: `food_mask` keys on "saturated or dark", and a
# patterned tablecloth is both. On a plain paper wrapper the mask is clean and
# this stage's ancestor reproduced weighed meals at -3.0% and -0.0%; on a
# printed cloth it swallowed the whole table. Confining the search to the plate
# is what makes the same trick work on a plate.
PLATE_MIN_FRACTION = 0.06
PLATE_MAX_FRACTION = 0.90
PLATE_MAX_SQUASH = 2.2          # beyond this it is not a plate seen at an angle
PLATE_ELLIPSE_TOLERANCE = 0.35  # the fit must actually describe its own region


def _ellipse(shape, pb: dict, scale: float) -> np.ndarray:
    """The plate as a filled ellipse. Raises on anything unusable, so the one
    caller can turn that into "no measurement" rather than a wrong one."""
    H, W = shape[:2]
    x, y = float(pb["x"]), float(pb["y"])
    w, h = float(pb["w"]), float(pb["h"])
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        raise ValueError("non-finite plate box")
    if not (0.0 < w <= 4.0 and 0.0 < h <= 4.0):
        raise ValueError("implausible plate box")
    out = np.zeros((H, W), np.uint8)
    cv2.ellipse(out,
                (int(np.clip((x + w / 2) * W, -1e6, 1e6)),
                 int(np.clip((y + h / 2) * H, -1e6, 1e6))),
                (max(1, int(min(w * W / 2 * scale, 1e6))),
                 max(1, int(min(h * H / 2 * scale, 1e6)))),
                0, 0, 360, 1, -1)
    return out.astype(bool)

# How much of the shorter side of the frame a plate's RADIUS can be. A plate
# photographed from above fills a good part of the picture but never all of it;
# measured across fourteen photographs on two backgrounds the radius ran 0.26
# to 0.40 of the short side, so these bounds have room on both sides.
HOUGH_R_MIN, HOUGH_R_MAX = 0.22, 0.62
# Accumulator threshold. Scored identically at 40, 55 and 70, so the answer is
# not balanced on this number.
HOUGH_VOTES = 55


def _plate_by_hough(rgb: np.ndarray):
    """The plate's outer rim, found by circular agreement. See plate_surface."""
    H, W = rgb.shape[:2]
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Median rather than Gaussian: it flattens the cloth's printed pattern
    # without softening the rim, which is the edge being voted on.
    grey = cv2.medianBlur(grey, 9)
    short = min(H, W)
    found = cv2.HoughCircles(
        grey, cv2.HOUGH_GRADIENT, dp=1.4, minDist=short,
        param1=110, param2=HOUGH_VOTES,
        minRadius=int(short * HOUGH_R_MIN), maxRadius=int(short * HOUGH_R_MAX))
    if found is None or not len(found):
        return None
    # ONE CIRCLE COMES BACK, AND THAT IS THE POINT OF minDist.
    #
    # minDist is the whole short side, so two circles can never both be
    # returned -- concentric ones are zero apart. Hough therefore hands back
    # its single best-voted circle rather than a list to choose from.
    #
    # An earlier version of this took max(..., key=radius) and explained at
    # length that a plate has two concentric rims and the outer one is what a
    # tape measures. That reasoning was decoration: with one candidate, max and
    # min are the same call, and a mutation swapping them changed nothing and
    # no test noticed. Worse, it was wrong on its own terms -- relaxing minDist
    # on the empty foam plate offers radii 252, 251 and 179, and 179 is the one
    # that sits on the rim. Taking the largest would have been an error.
    #
    # What actually keeps this on the rim is the radius band below plus Hough's
    # own voting, verified by drawing the circles over all fourteen photographs
    # and looking at them.
    cx, cy, r = np.around(found[0])[0]
    out = np.zeros((H, W), np.uint8)
    cv2.circle(out, (int(cx), int(cy)), int(r), 1, -1)
    mask = out.astype(bool)
    return mask if mask.any() else None


def _box_ellipse(shape, plate_bbox: dict | None):
    """The model's plate box as an inset ellipse, or None if it is not usable.

    Factored out because two callers now need it and the validation must not be
    copied: everything the model sends can be a string, a null or a list, and a
    malformed box must cost nothing worse than the fence. One copy, so a fix is
    a fix in both places.
    """
    if not plate_bbox:
        return None
    try:
        x = float(plate_bbox.get("x", 0.0)); y = float(plate_bbox.get("y", 0.0))
        w = float(plate_bbox.get("w", 0.0)); h = float(plate_bbox.get("h", 0.0))
    except (AttributeError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        return None
    if not (w > 0.08 and h > 0.08):
        return None
    # Inset slightly: the rim's own shadow reads as food.
    try:
        return _ellipse(shape, {"x": x, "y": y, "w": w, "h": h}, 0.97)
    except (ValueError, OverflowError, cv2.error):
        return None


def plate_surface(rgb: np.ndarray, plate_bbox: dict | None = None):
    """The plate's pixels, AND HOW THEY WERE ARRIVED AT.

    The second half of that is not bookkeeping. Two very different things come
    back from here and they are not interchangeable:

      "box"     an axis-aligned ellipse drawn from the MODEL'S bounding box. It
                fences the food correctly, which is all it was ever built for.
                It carries no shape information at all -- its axis ratio is the
                box's, on the model's 0.05 grid, so for most photographs it is
                simply the ASPECT RATIO OF THE IMAGE wearing an ellipse.

      "pixels"  a real ellipse fitted to the plate's own rim.

    Anything measuring the plate's SHAPE -- its foreshortening, and so the depth
    scale that rests on it -- must accept only "pixels". Measured the hard way:
    a tilt survey over sixteen bench photographs reported eight of them at
    41.4 degrees, to one decimal place, because arccos(3/4) is 41.4 and these
    photographs are 3:4. It read the frame and called it the plate.

    The region food is allowed to be in: inside the plate, if there is one.

    None when there is no plate -- food on paper, on a board, on a bare table.
    There the whole frame is fair game, which is the case the food mask already
    handles well; it is the plate case that needs the fence.

    THE ORDER, WHICH IS NOT WHAT THIS DOCSTRING USED TO SAY.

    It said `plate_bbox` is tried FIRST. That stopped being true at the
    inversion and the sentence stayed, which is worse than never having written
    it -- the next reader trusts it. What actually happens:

        1. Hough votes for a circle. Always, before anything else.
        2. If a segmenter is configured, its outline is asked for and taken
           ONLY if it agrees with that circle (`plates_agree`). It is the sole
           source carrying foreshortening and the sole one never checked
           against a tape, so the located detector vouches for it.
        3. The circle, where there is one and nothing better agreed.
        4. The model's box, then the brightness fit -- reached only when the
           pixels found no circle at all.

    `plate_bbox` WAS tried first once, and correctly: finding a plate in the
    pixels turned out to be the hard part of this whole problem, and for six
    methods nothing in the pixels worked. Four pixel-only methods were measured on the
    weighed bench photos and all four failed on the same thing: the pale
    patterned tablecloth these were shot on is BRIGHTER and less saturated than
    the plate resting on it, so a brightness threshold merges the two into one
    region covering most of the frame, and Canny edges do not close a clean
    ellipse through the food sitting on the rim. The model, asked simply where
    the plate is, has no such difficulty -- which is the same division of labour
    as everywhere else here: it locates, the pixels measure.

    TWO MORE METHODS HAVE SINCE FAILED, WHICH MAKES SIX. Recorded so the
    seventh is not attempted by hand.

      radial brightness edge, seeded by the model's box
          Cast rays out from the box centre, take the strongest brightness
          gradient on each, fit an ellipse, throw out the rays that disagree.
          Perfect on 06 -- residual 0.001 -- because that plate sits on dark
          granite. On the pale patterned tablecloth it finds 53-67% of the rim
          and the rest is genuinely not there in brightness.

      radial colour boundary, seeded the same way
          Sample the plate's own Lab colour from the ring inside the rim, walk
          each ray out until the pixels stop matching it. Worse: 19-50%. The
          sampling ring lands on food often enough to poison the reference,
          which is the same contamination the food mask guards against with
          MIN_PLATE_SHARE.

      the two combined, colour weighted up
          Best of the three, and it exposed a problem none of the others did:
          a plate has TWO concentric edges, the well and the outer rim, and the
          inner one is often the stronger. The one photo that passed every
          check had locked onto the well and reported a 34.7 degree tilt for a
          plate photographed from almost directly above.

    That last point is the one that matters for whatever measures this next: it
    is not enough to find AN ellipse on the plate. It has to be the OUTER one,
    because the diameter the user measured with a tape is the outer diameter.
    """
    H, W = rgb.shape[:2]

    # HOUGH FIRST, AND ALWAYS.
    #
    # It used to be seventh, underneath a box branch that RETURNS
    # UNCONDITIONALLY whenever `plate_bbox` holds a sane box -- which every real
    # scan does, because the box is the model's own answer and locating is what
    # the model is good at. So on a live scan the order was SAM2, then the box,
    # and this detector was never reached. `dev replay` and `dev segcheck` both
    # call this with None, which is the only reason its 30/30 was ever seen:
    # certified through a door production does not use.
    circle = None
    try:
        circle = _plate_by_hough(rgb)
    except (cv2.error, ValueError, OverflowError):
        circle = None

    # The seventh method, and the first that is not hand-rolled.
    #
    # Still tried before the box, for the reason it always was: the box fences
    # food correctly and carries no shape, and everything needing the plate's
    # shape has been blocked on exactly that. What is new is that it no longer
    # wins on its own say-so. It is the only source carrying foreshortening AND
    # the only one never checked against a tape, so Hough vouches for it or it
    # does not run.
    #
    # Not eroded, unlike the two paths below. They inset by 3% so the rim's own
    # shadow is not read as food, which is worth it for a FENCE. Here the mask's
    # own axes become the plate's diameter in pixels, and a 3% erosion is a 3%
    # error in mm-per-pixel -- about 6% on every gram. A slightly generous fence
    # costs less than a scale that is wrong on purpose.
    outline = None
    if plate_bbox:
        try:
            seg = segmenter()
            if seg.available() and hasattr(seg, "plate_outline"):
                got = seg.plate_outline(rgb, plate_bbox)
                if got is not None and got.any():
                    outline = got.astype(bool)
        except Exception:  # noqa: BLE001
            # A measurement that fails is not a scan that fails.
            outline = None

    if circle is not None:
        # Corroborated: take the shape-bearing mask. Uncorroborated: take the
        # one whose location was measured. Never the unchecked one alone.
        if outline is not None:
            iou = plates_agree(outline, circle)
            if iou >= PLATE_AGREEMENT_IOU:
                return outline, "sam2"
            log.info("plate_outline_disagrees", iou=round(iou, 3),
                     threshold=PLATE_AGREEMENT_IOU)
        # THE CIRCLE IS NOT ERODED AND THE BOX WAS.
        #
        # `_ellipse(..., 0.97)` insets the box path by 3% so the rim's own
        # shadow is not read as food. Hough draws its circle at the radius it
        # voted for. So wherever this now serves where the box used to, the
        # fence is about 3% wider in radius and 6% in area.
        #
        # Benign as a FENCE -- a slightly generous one costs less than a tight
        # one that clips food at the rim. It is not only a fence: this same
        # mask reaches `segment_boxes` as `plate_hint`, where it BOUNDS the
        # segmenter's masks, so that bound is 6% looser too. Worth knowing when
        # reading a footprint that has grown slightly against yesterday's run.
        #
        # Not corrected here on purpose. Eroding the circle to match would make
        # the mask's own axes 3% short, and those axes are the plate's diameter
        # in pixels whenever anything measures with them -- a 3% error in
        # mm-per-pixel is about 6% on every gram. Wrong on purpose in the
        # direction that costs grams is worse than generous in the direction
        # that costs none.
        # WHAT THE MODEL'S BOX WOULD HAVE SAID. MEASURED, NOT ACTED ON.
        #
        # The circle wins this either way -- that is the decision and this does
        # not touch it. But nothing has ever recorded how OFTEN the two
        # disagree, and a guard built before that number exists is a threshold
        # picked to fit an argument. So the bench collects it, and whether
        # there is anything here gets decided from the collection.
        box_for_compare = _box_ellipse(rgb.shape, plate_bbox)
        if box_for_compare is not None:
            log.info("plate_box_vs_circle",
                     iou=round(plates_agree(box_for_compare, circle), 3),
                     box_area=round(float(box_for_compare.mean()), 4),
                     circle_area=round(float(circle.mean()), 4),
                     source=CIRCLE_PLATE_SOURCE)
        return circle, CIRCLE_PLATE_SOURCE

    # NO CIRCLE, SO NOTHING TO AGREE WITH. Prior behaviour exactly: an outline
    # cannot be held to a corroboration that was not available. Refusing it
    # here would lose the depth scale on every photograph Hough declines, which
    # is a real loss traded for no evidence.
    if outline is not None:
        return outline, "sam2"

    if plate_bbox:
        # Same helper as the comparison above, so a malformed box is rejected
        # by one piece of validation rather than two that can drift apart.
        box = _box_ellipse(rgb.shape, plate_bbox)
        if box is not None:
            return box, "box"
    # METHOD EIGHT: VOTE FOR THE CIRCLE INSTEAD OF SEGMENTING IT.
    #
    # The seven methods above all tried to SEPARATE the plate from its
    # background first -- brightness, colour, radial rays, the three combined.
    # Every one of them assumed the plate stands out from what it is resting
    # on. On plain wood it does: measured over seven bench photographs the
    # plate reads 169-197 in value against 82-111 for the table, and the wood
    # is heavily saturated besides.
    #
    # On a PATTERNED TABLECLOTH none of that holds. Measured over seven
    # photographs on a pale floral cloth: plate 135-196 against background
    # 100-159, and BOTH unsaturated. The brightness mask merges the two into
    # one region covering 84-92% of the frame, and `plate_surface` returned
    # None on SEVEN OF SEVEN -- including a photograph of the empty plate with
    # nothing on it at all. Every photograph in this bench until then had been
    # shot on bare wood, so this had never once been tested.
    #
    # Hough needs neither. It does not need the plate separated from anything
    # and it does not need a closed contour -- which is what killed the Canny
    # attempt. It needs arcs of the rim to agree on a centre, and a plate rim
    # is the strongest circular agreement in any of these photographs whatever
    # it is lying on. Scored on all fourteen:
    #
    #     plain wood        7/7   (the shipped method also gets 7/7)
    #     patterned cloth   7/7   (the shipped method gets 1/7)
    #
    # and the circles were LOOKED AT, drawn over the photographs, not trusted
    # from a count -- every one sits on the outer rim. Stable across param2 from
    # 40 to 70, so it is not balanced on a threshold.
    #
    # The well-versus-rim trap that killed an earlier attempt is handled by the
    # radius band and by voting, not by picking the biggest candidate -- see
    # the note in _plate_by_hough for why that idea was wrong here.
    #
    # The brightness method stays underneath as a fallback, so this can only
    # add answers where there were none.
    #
    # Returned as "circle", NOT "pixels" -- see CIRCLE_PLATE_SOURCE. What comes
    # back is a circle because Hough votes for circles, and calling that a
    # measured ellipse would report every photograph as shot from overhead.
    # Computed at the top of this function now, and consulted there. Reaching
    # this line means it returned None, so there is nothing to return here.

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1].astype(int)
    val = hsv[:, :, 2].astype(int)
    bright = ((val > 140) & (sat < 80)).astype(np.uint8) * 255
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    frame = float(W * H)
    best = None
    for c in contours:
        if len(c) < 20:
            continue
        area = cv2.contourArea(c)
        if not (PLATE_MIN_FRACTION * frame < area < PLATE_MAX_FRACTION * frame):
            continue
        (cx, cy), (d1, d2), angle = cv2.fitEllipse(c)
        major, minor = max(d1, d2), min(d1, d2)
        if minor <= 0 or major / minor > PLATE_MAX_SQUASH:
            continue
        if abs(np.pi / 4 * d1 * d2 - area) / max(area, 1) > PLATE_ELLIPSE_TOLERANCE:
            continue
        if best is None or major > best[0]:
            best = (major, (cx, cy), (d1, d2), angle)
    if best is None:
        return None, "none"
    _, centre, axes, angle = best
    out = np.zeros((H, W), np.uint8)
    # Slightly inside the rim, so the rim's own shadow is not read as food.
    cv2.ellipse(out, (int(centre[0]), int(centre[1])),
                (int(axes[0] / 2 * 0.97), int(axes[1] / 2 * 0.97)),
                angle, 0, 360, 1, -1)
    # HAZARD, PRE-EXISTING, WRITTEN DOWN RATHER THAN FIXED.
    #
    # This returns "pixels", which IS in MEASURED_PLATE_SOURCES, so the depth
    # scale will rest on it. It is the brightness method -- the one measured at
    # 0/7 on a patterned tablecloth, including on a photograph of the EMPTY
    # PLATE. Its shape is not reliable and this source says it is.
    #
    # It has always been this way. What changed is only how often it is
    # reached: Hough runs first now and finds a plate on backgrounds this
    # cannot, so arriving here means Hough declined AND the box was absent or
    # malformed, which on a real scan is close to never. Near-unreachable is
    # not unreachable, and the failure would be silent -- a plausible tilt
    # computed from a shape that is an artefact of brightness.
    #
    # The honest fix is its own source tag outside MEASURED_PLATE_SOURCES,
    # the way "circle" got one. Not bundled in here.
    return out.astype(bool), "pixels"


def surface_mask(rgb: np.ndarray, plate_bbox: dict | None = None) -> np.ndarray | None:
    """The plate's pixels, for callers that only need the fence.

    Every existing caller wants somewhere to look, not a shape to measure, so
    this keeps the old signature exactly. Use `plate_surface` when the
    DIFFERENCE between a box-derived ellipse and a measured one matters.
    """
    return plate_surface(rgb, plate_bbox)[0]


# Learning the plate's colour instead of assuming it.
#
# A fixed HSV threshold cannot separate food from plate across lighting, and
# this was measured rather than guessed: the same constants over-caught by 25%
# on one photo and under-caught by 17% on another photo OF THE SAME PLATE. Any
# value that fixed one broke the other.
#
# The rim fixes it. Food is served in the middle, so the outer annulus is almost
# always bare plate -- a free sample of THIS photo's plate under THIS photo's
# light. Food is then whatever does not look like it. Measured on two photos of
# one identical meal, the total food area went from 49% apart to 1% apart.
RIM_INNER = 0.78          # annulus sampled for the plate's own colour
RIM_OUTER = 0.97          # just inside the rim, to miss its shadow
PLATE_COLOUR_K = 4.5      # how many robust deviations from plate colour is food
MIN_RIM_PIXELS = 200
# Food must differ from the plate by this much in plain Lab units as well as
# by PLATE_COLOUR_K robust deviations. Roughly the point at which two colours
# are distinguishable side by side.
MIN_LAB_DISTANCE = 14.0
# How much of the plate its food may plausibly cover. Outside this the rim
# sample was contaminated and the answer is refused rather than reported.
MIN_PLATE_SHARE = 0.05
MAX_PLATE_SHARE = 0.85
FLATTEN_SIGMA = 81        # illumination is smooth; food is not
BACKGROUND_KERNEL = 121   # wider than any single piece of food


def flatten_illumination(rgb: np.ndarray) -> np.ndarray:
    """Divide out the lighting gradient before comparing colours.

    A plate lit from one side is measurably brighter on that side -- 37 levels
    of L across the rim on one bench photo -- and a single colour model cannot
    describe both halves, so the shadowed half reads as food. Subtracting a
    heavily blurred copy removes the gradient and leaves the food, which varies
    far too sharply to survive the blur.
    """
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    L = lab[:, :, 0]
    # The background is estimated by a grayscale CLOSING, not a blur.
    #
    # A blur is dragged downward by the dark food sitting in it, so the plate
    # just outside a drumstick comes out brighter than the background model
    # predicts and reads as food -- a halo of phantom portion around every real
    # one. A closing takes the local maximum first: dark food is filled in by
    # the plate around it, and what survives is the illumination alone.
    # The kernel is a fixed PIXEL count and stays one, which is not the obvious
    # choice and was tested rather than assumed. Scaling it with the image --
    # on the reasoning that "wider than any piece of food" is a fraction of the
    # frame -- was measured against the full-size answer on two plates and made
    # the drift WORSE, not better:
    #
    #     downscaled to 1024 px      rice     casserole
    #     kernel fixed at 121        +2%         +4%
    #     kernel scaled to 79       +19%        +19%
    #
    # It is faster scaled, and wrong. So: fixed, and anything that wants speed
    # pays for it in resolution instead, where the cost is visible.
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (BACKGROUND_KERNEL,) * 2)
    bg = cv2.morphologyEx(L, cv2.MORPH_CLOSE, k)
    bg = cv2.GaussianBlur(bg, (0, 0), FLATTEN_SIGMA)
    lab[:, :, 0] = np.clip(L - bg + float(np.median(bg)), 0, 255)
    return cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2RGB)




def food_on_plate(rgb: np.ndarray, plate_bbox: dict) -> np.ndarray | None:
    """Food, found by how far it sits from the plate's own colour.

    Returns None when the rim gives too small a sample to model -- better no
    measurement than one built on twenty pixels.
    """
    try:
        inner = _ellipse(rgb.shape, plate_bbox, RIM_OUTER)
        rim = inner & ~_ellipse(rgb.shape, plate_bbox, RIM_INNER)
    except (KeyError, TypeError, ValueError):
        return None
    if int(rim.sum()) < MIN_RIM_PIXELS:
        return None
    flat = flatten_illumination(rgb)
    lab = cv2.cvtColor(flat, cv2.COLOR_RGB2LAB).astype(np.float32)
    sample = lab[rim]
    med = np.median(sample, axis=0)
    # Median absolute deviation, not standard deviation: a bit of food touching
    # the rim must not drag the plate's own colour model onto the food.
    mad = np.maximum(np.median(np.abs(sample - med), axis=0) * 1.4826, 2.0)
    dist = np.sqrt((((lab - med) / mad) ** 2).sum(axis=2))
    # ...and how far in plain Lab units, which is the check that matters when
    # the plate is very uniform. A relative test alone divides by a spread that
    # has collapsed to nothing, so three levels of sensor noise on a smooth
    # plate reads as "many deviations from plate colour" and the whole plate
    # flips to food. A pixel a few Lab units from the plate is not a portion,
    # however consistent that plate is.
    absolute = np.sqrt(((lab - med) ** 2).sum(axis=2))

    food = ((dist > PLATE_COLOUR_K) & (absolute > MIN_LAB_DISTANCE)
            & inner).astype(np.uint8)
    food = cv2.morphologyEx(food, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    food = cv2.morphologyEx(food, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(food, connectivity=8)
    keep = np.zeros_like(food)
    floor = MIN_BLOB_FRACTION * food.size
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] > floor:
            keep[labels == i] = 1

    # Did the rim actually land on the plate?
    #
    # This whole method assumes the annulus samples bare plate. When the plate
    # box is wrong the annulus lands on FOOD instead, the model learns the
    # food's colour, and real food stops looking different from "plate" -- the
    # measurement does not degrade, it inverts. Measured on photo 14 by moving
    # a known-good box:
    #
    #     the correct box            44.2% of the plate came back as food
    #     shifted 15% across         31.8%
    #     30% too small               1.8%     <- rim now on the food
    #     50% too small               0.0%
    #
    # A plated meal covers a good fraction of its plate. One that covers almost
    # none of it is not a light meal, it is a failed measurement -- and on the
    # real bench that failure shipped a 146 g drumstick as 12 g. So refuse.
    # None means "not measured" and the caller falls back; a small number would
    # be a lie with a decimal point on it.
    share = float(keep.sum()) / max(float(inner.sum()), 1.0)
    if not (MIN_PLATE_SHARE <= share <= MAX_PLATE_SHARE):
        return None
    return keep.astype(bool)


def _seed_point(mask: np.ndarray, box: dict, W: int, H: int):
    """Where to start growing this item's region.

    The box's centre, when that lands on food. When it does not -- an undersized
    box centred on a gap, a drumstick whose centre is the plate showing through
    a bend -- the nearest food pixel inside the box is used instead, and failing
    that the item gets no seed rather than a wrong one.
    """
    x = float(box.get("x", 0.0)); y = float(box.get("y", 0.0))
    w = float(box.get("w", 0.0)); h = float(box.get("h", 0.0))
    if w <= 0 or h <= 0:
        return None
    cx, cy = int((x + w / 2) * W), int((y + h / 2) * H)
    cx = max(0, min(W - 1, cx)); cy = max(0, min(H - 1, cy))
    if mask[cy, cx]:
        return cx, cy
    x0, x1 = max(0, int(x * W)), min(W, int((x + w) * W))
    y0, y1 = max(0, int(y * H)), min(H, int((y + h) * H))
    if x1 <= x0 or y1 <= y0:
        return None
    sub = mask[y0:y1, x0:x1]
    ys, xs = np.nonzero(sub)
    if len(xs) == 0:
        return None
    d = (xs + x0 - cx) ** 2 + (ys + y0 - cy) ** 2
    i = int(np.argmin(d))
    return int(xs[i] + x0), int(ys[i] + y0)


def item_masks(rgb: np.ndarray, boxes: list,
               plate_bbox: dict | None = None) -> list:
    """Which pixels belong to which item, for callers that do not need to know
    HOW. Keeps the old signature exactly; see `item_masks_with_source` when the
    difference between a segmented mask and a colour-grown one matters -- and it
    matters for anything using the footprint as an AREA rather than a share.
    """
    return item_masks_with_source(rgb, boxes, plate_bbox)[0]


def _box_centres(boxes: list, W: int, H: int) -> list:
    """One prompt point per box, in pixels, or None where the box is unusable.

    The CENTRE, deliberately, and never the box itself. The model's box is
    quantised to a 0.05 grid -- measured, 82% of values sit exactly on it where
    chance would put 20% -- and at 3% of frame one grid step is 45% of the
    food's weight. The centre does not carry that error: it is the one thing the
    model is asked for that is a location rather than a size. Handing a
    segmenter the box would feed the quantisation straight back in.
    """
    from .portion import normalize_bbox

    out: list = []
    for b in boxes:
        nb = normalize_bbox(b)
        if not nb or nb["w"] <= 0 or nb["h"] <= 0:
            out.append(None)
            continue
        cx = float(np.clip((nb["x"] + nb["w"] / 2) * W, 0, W - 1))
        cy = float(np.clip((nb["y"] + nb["h"] / 2) * H, 0, H - 1))
        out.append((cx, cy))
    return out


# RULE 0 -- THE FOOD IS THE HOLE IN THE PLATE.
#
# SAM2 does not return "the plate". It returns the plate WITH THE FOOD PUNCHED
# OUT OF IT. So when the per-box masks come back unusable, the food's outline
# is still sitting there in the plate mask, as its holes, and reading it needs
# no second call and no seed that lands in the right place.
#
# Measured against the per-piece union on nine weighed photographs, the two
# agreed to 94, 96, 97, 97, 98, 98 and 100 per cent. They are read off
# different parts of the same answer, so agreement is real evidence.
#
# This matters because of what the bench does WITHOUT it: eight of fifteen
# scored items never got a measured footprint at all and fell back to the
# colour rule or to nothing, and those eight averaged 83.7% error against 28.5%
# for the seven that were measured. Every one of them was a plate SAM2 had
# already segmented.
#
# The plate is told from the tabletop by what each has a hole in it. The plate
# has the food punched out -- several small holes. The tabletop has the PLATE
# punched out -- one hole as big as the mask. Across every cached photograph
# plates topped out at 0.32 and tables started at 0.51, so the cut is 0.35.
# A hole smaller than this share of the plate is a nick in the rim, not food.
MIN_FOOD_HOLE_SHARE = 0.004


def _items_from_plate_holes(rgb: np.ndarray, boxes: list):
    """Each box's footprint taken from the holes in the plate. Rule 0.

    Returns one mask per box, or None if the plate cannot be found or no hole
    can be assigned. A hole belongs to the box whose area it falls inside; a
    hole inside no box is food nobody detected and is left out, exactly as the
    colour path leaves out a blob with no seed.

    Refuses, like the segmenter path, unless EVERY box gets something. A plate
    with one unmeasured item cannot produce an honest split.
    """
    from .portion import normalize_bbox
    from .segment_hosted import fill_outer, plate_among

    try:
        seg = segmenter()
        if not seg.available():
            return None
        # Only the automatic generator returns a plate to take holes from; a
        # point-prompted model answers about points and has no such mask.
        if getattr(seg, "mode", None) != "auto":
            return None
        auto = getattr(seg, "_auto_masks", None)
        encode = getattr(seg, "encode_image", None)
        if not callable(auto):
            return None
        H, W = rgb.shape[:2]
        if not callable(encode):
            from .segment_hosted import encode_image as encode
        image = encode(rgb)
        if not image:
            return None
        # Memoised on the image's digest, so this reads the answer the call
        # above already paid for rather than buying a second one.
        masks = auto(image, (H, W))
        if not masks:
            return None
        plate = plate_among(masks, (H, W))
        if plate is None:
            return None
        filled = fill_outer(plate)
        holes = filled & ~plate
        if not holes.any():
            return None
        plate_px = float(filled.sum()) or 1.0

        n, labels, stats, _c = cv2.connectedComponentsWithStats(
            holes.astype(np.uint8), 8)
        out = [np.zeros((H, W), bool) for _ in boxes]
        norm = [normalize_bbox(b) for b in boxes]
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] / plate_px < MIN_FOOD_HOLE_SHARE:
                continue                            # a nick in the rim
            blob = labels == i
            ys, xs = np.nonzero(blob)
            cy, cx = float(ys.mean()) / H, float(xs.mean()) / W
            owner = None
            for j, nb in enumerate(norm):
                if not nb:
                    continue
                if (nb["x"] <= cx <= nb["x"] + nb["w"]
                        and nb["y"] <= cy <= nb["y"] + nb["h"]):
                    # The tightest box wins, so a small item inside a big
                    # item's box is not swallowed by it.
                    if owner is None or (nb["w"] * nb["h"]
                                         < norm[owner]["w"] * norm[owner]["h"]):
                        owner = j
            if owner is not None:
                out[owner] |= blob
        if not all(m.any() for m in out):
            return None
        log.info("rule0_plate_holes", items=len(out),
                 share=round(float(holes.sum()) / plate_px, 4))
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("plate_holes_failed", error=str(exc)[:200])
        return None


def _segmented_items(rgb: np.ndarray, boxes: list, plate_hint=None):
    """Item masks from the configured segmenter, or None if it cannot be used.

    All of it or none of it, decided by `segmenter.usable`. A plate with one
    unmeasured item cannot produce a split -- the missing food lands on
    whichever items did work, which are precisely the ones that looked fine.
    Measured on photo 06: two seeds failed and the rice was published as 100% of
    a plate it was about a third of.

    Never raises. A segmenter that is down costs a measurement, never a scan.
    """
    from . import segmenter as seg_mod

    try:
        seg = segmenter()
        if not seg.available():
            return None
        H, W = rgb.shape[:2]
        points = _box_centres(boxes, W, H)
        if not all(points):
            # A box we could not read is an unmeasured item, and the gate
            # refuses those anyway. Refuse here rather than paying for a call.
            return None
        # Boxes when the segmenter can use them.
        #
        # A point picks the smallest mask containing it. For scattered food the
        # box centre is BARE PLATE between the pieces, and the smallest mask
        # containing bare plate is the plate itself -- measured on the bench,
        # eight baby carrots came back as 85% of the plate and 686 g against a
        # weighed 58 g. A box can be filled with the masks that sit inside it,
        # which is the same answer for one lump and the right answer for eight.
        by_box = getattr(seg, "segment_boxes", None)
        results = (by_box(rgb, points, boxes, plate_hint=plate_hint)
                   if callable(by_box)
                   else seg.segment(rgb, points))
        if not seg_mod.usable(results):
            return None
        return [r.mask.astype(bool) for r in results]
    except Exception as exc:  # noqa: BLE001
        log.warning("segmenter_items_failed", error=str(exc)[:200])
        return None


def item_masks_with_source(rgb: np.ndarray, boxes: list,
                           plate_bbox: dict | None = None) -> tuple[list, str]:
    """Which pixels belong to which item, AND how that was arrived at.

    The source is not bookkeeping. A colour-grown mask is fenced by the plate
    box, so its absolute footprint inherits the box's error while the SHARE
    survives -- measured, shrinking the plate box 10% moved footprints 3-14% and
    shares 1-6%. A point-prompted segmenter's mask never sees the plate box at
    all. Only the second may be used as an area; see `area_is_absolute`.

      "sam2"     the configured segmenter, past the gate in segmenter.py
      "colour"   the rule below, which is what ships today
      "none"     nothing could be measured

    One boolean mask per box, or None per item that could not be measured.

    Split out of measure_items so the footprint and the HEIGHT are measured over
    exactly the same region. Two separate assignments would drift -- and a
    height averaged over a slightly different set of pixels than the area it
    multiplies is a quiet error in a product, which is the hardest kind to find.

    On the COLOUR path, separation only. Which pixels are this food is a
    question colour can answer; how much food that is, is not -- the absolute
    footprint scored 60% against a kitchen scale while the SPLIT held to within
    a few points. Scale comes from the calibrated vessel, height from the depth
    map.
    """
    from .portion import normalize_bbox

    H, W = rgb.shape[:2]
    empty: list = [None] * len(boxes)
    if not len(boxes):
        return empty, "none"

    # The plate, from the colour rule, which finds one on every photograph
    # here including the four SAM2 cannot. Used to BOUND the segmenter's masks,
    # never to measure with -- see segment_boxes for why that line is drawn.
    plate_hint = None
    try:
        plate_hint, _src = plate_surface(rgb, plate_bbox)
    except Exception:                                 # noqa: BLE001
        plate_hint = None

    segmented = _segmented_items(rgb, boxes, plate_hint)
    if segmented is not None:
        return segmented, "sam2"

    # RULE 0. The per-box masks were unusable, but the plate SAM2 returned has
    # the food punched out of it. Same call, already paid for, second reading.
    # Still "sam2": it is the segmenter's own mask, never the plate box, so it
    # is an absolute area on the same terms as the path above.
    holes = _items_from_plate_holes(rgb, boxes)
    if holes is not None:
        return holes, "sam2"

    mask = None
    if plate_bbox:
        mask = food_on_plate(rgb, plate_bbox)
    if mask is None:
        mask = food_mask(rgb)
        plate = surface_mask(rgb, plate_bbox)
        if plate is not None:
            mask = mask & plate
    if mask is None or not mask.any():
        return empty, "none"

    seeds: list = []
    for b in boxes:
        nb = normalize_bbox(b)
        seeds.append(_seed_point(mask, nb, W, H) if nb else None)
    if not any(s for s in seeds):
        return empty, "none"

    # Divide the food between the seeds.
    #
    # NOT watershed, which was tried first and collapsed: labelling every
    # non-food pixel as one huge background region puts that region adjacent to
    # every food blob, and it wins most of the flood. On photo 14 the items
    # summed to 1.85% of frame out of the 12.06% of food actually present --
    # the background label ate six sevenths of the meal.
    #
    # The food mask is already correct by then; the only question left is which
    # pile each pixel belongs to. So take each connected blob and hand it to the
    # seeds inside it: one seed takes the whole blob, several split it by which
    # is nearest. A blob containing no seed is food nobody detected, and it is
    # left out rather than given to whichever item happens to be closest.
    n_cc, cc = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
    owner = np.zeros((H, W), np.int32)          # 0 = unassigned
    for c in range(1, n_cc):
        blob = cc == c
        inside = [i for i, sd in enumerate(seeds) if sd and blob[sd[1], sd[0]]]
        if not inside:
            continue
        if len(inside) == 1:
            owner[blob] = inside[0] + 1
            continue
        best_d = None
        best_i = None
        for i in inside:
            src = np.full((H, W), 255, np.uint8)
            src[seeds[i][1], seeds[i][0]] = 0
            d = cv2.distanceTransform(src, cv2.DIST_L2, 3)
            if best_d is None:
                best_d, best_i = d.copy(), np.full((H, W), i, np.int32)
            else:
                closer = d < best_d
                best_d[closer] = d[closer]
                best_i[closer] = i
        owner[blob] = best_i[blob] + 1

    out: list = []
    frame = float(W * H)
    for i, seed in enumerate(seeds):
        if seed is None:
            out.append(None)
            continue
        region = owner == i + 1
        out.append(region if region.sum() / frame >= MIN_REGION_FRACTION else None)
    return out, "colour"


def measure_items(rgb: np.ndarray, boxes: list,
                  plate_bbox: dict | None = None) -> list[float | None]:
    """Each item's real footprint, as a fraction of the frame.

    `boxes` are the model's normalised boxes, used ONLY for their centres.
    Returns one value per box, None where the item could not be measured --
    which the caller must treat as "no measurement", never as "a small food".
    """
    return measure_items_with_source(rgb, boxes, plate_bbox)[0]


def measure_items_with_source(rgb: np.ndarray, boxes: list,
                              plate_bbox: dict | None = None):
    """Footprints AND how they were measured. See `item_masks_with_source`.

    The source decides what the number may be used for: a colour footprint is
    only a share of the meal, a segmented one is an area. `area_is_absolute`
    is the one place that decision is written down.
    """
    masks, source = item_masks_with_source(rgb, boxes, plate_bbox)
    return [None if m is None else float(m.mean()) for m in masks], source


# WHEN A FOOTPRINT IS ONE THING AND WHEN IT IS SEVERAL.
#
# Separate pieces lying on a plate CANNOT STACK. A connected mass CAN be piled.
# That is a fact about gravity, not about food, and it is worth more than any
# shape word because it is visible in the mask and needs no guess about what
# the food is.
#
# Measured on this user's weighed photographs, the largest connected piece as a
# fraction of the whole footprint, against the height each food solves to:
#
#     carrots      4 pieces   largest 0.50    10.7 mm
#     zucchini     3 pieces   largest 0.49     7.8 mm
#     chicken      1 piece    largest 1.00    18.9 mm
#     roast beef   1 piece    largest 1.00    20.8 mm
#     macaroni     1 piece    largest 1.00    19.9 mm
#     potatoes     1 piece    largest 1.00    23.2 mm
#     pizza        1 piece    largest 1.00    22.3 mm
#
# Nothing lands between 0.50 and 1.00, so the cut sits at 0.80.
ONE_PIECE_SHARE = 0.80

# Specks are not pieces. Below this share of the footprint a blob is mask
# noise at an edge, and counting it would split a single pile into "several".
MIN_PIECE_SHARE = 0.02


def largest_piece_share(mask) -> float | None:
    """The biggest connected piece as a fraction of the whole footprint.

    1.0 means the footprint is one connected thing. Around 0.5 means it is
    several separate ones. Returns None when there is nothing to measure --
    never raises, because a topology that cannot be read must cost the height
    refinement and nothing else.
    """
    try:
        import cv2
        total = float(mask.sum())
        if total <= 0:
            return None
        n, _labels, stats, _c = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8), 8)
        areas = [int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n)]
        areas = [a for a in areas if a >= total * MIN_PIECE_SHARE]
        if not areas:
            return None
        return max(areas) / sum(areas)
    except Exception:                                 # noqa: BLE001
        return None


def measure_items_with_topology(rgb: np.ndarray, boxes: list,
                                plate_bbox: dict | None = None):
    """Footprints, their topology, and how they were measured.

    The topology is what separates a single layer of slices from a pile, and
    it is read off the same mask the footprint came from -- one segmentation,
    two facts, no extra call and no extra cost.
    """
    masks, source = item_masks_with_source(rgb, boxes, plate_bbox)
    areas = [None if m is None else float(m.mean()) for m in masks]
    shares = [None if m is None else largest_piece_share(m) for m in masks]
    return areas, shares, source




# Heights that go WITH a measured footprint, and only with one.
#
# HEIGHT_PRIORS_MM in portion.py were calibrated against RAILED areas -- areas
# the bounding-box rail had already cut down. Feed those same heights a measured
# footprint, which is larger and honest, and every item comes out 16% to 71%
# heavy. The heights were never wrong on their own; they were compensating.
#
# With the area finally measured, height is the only unknown left in
#
#     grams = area x height x profile x density
#
# so it can simply be solved for. Done on three weighed plates, the same food
# solved to the same height in every photo:
#
#     chicken drumstick   35, 38, 35 mm      (prior said 40)
#     mexican rice        22, 21, 24 mm      (prior said 32)
#     refried beans       29, 22, 23 mm      (prior said 32)
#     baked spaghetti     30 mm              (prior said 32)
#
# Rice is the headline: a mound of rice on a plate is nothing like 32 mm tall,
# and that single number is most of why spread foods have read heavy all along.
#
# Validated leave-one-out -- heights solved on two plates, used to predict the
# third, nothing from the held-out photo touching its own prediction:
#
#     mean absolute 10.3% over 10 weighed items
#     against 22.4% for the shipped pipeline on the same bench run
#
# Ten items on three plates is a small calibration and it is stated as one.
# These are NOT a drop-in replacement for HEIGHT_PRIORS_MM: swapping them into
# the railed-area path would make every estimate lighter and the bench worse.
# The pair only works together.
MEASURED_HEIGHTS_MM = {
    "chunky": 34.0,
    "mound": 24.0,
    "flat": 14.0,        # scaled from the mound result; no weighed flat item yet
    "loose": 26.0,       # ditto
    "cluster": 21.0,     # ditto
    "liquid": 25.0,      # unchanged; a vessel's walls set this, not the food
    "wrapped": 42.0,     # unchanged; never measured on a plate
    "topped_flat": 16.0,
    "default": 24.0,
}


def grams_from_measured_area(
    name: str,
    area_ratio: float,
    frame_mm2: float,
    shape_hint: str | None = None,
    density: float | None = None,
) -> float | None:
    """Grams from a MEASURED footprint, without the estimator around it.

    NOT the shipped path. `portion.estimate_grams` is, and since the measured
    branch there now reads MEASURED_HEIGHTS_MM above, the two agree on the term
    that matters. This is kept for one reason: the tests beside it hold weighed
    ground truth -- three foods on photo 14, a kitchen scale, footprints from
    `dev mask` -- and checking that arithmetic against a scale needs the
    arithmetic on its own, without occlusion, clamps, blending and confidence
    in the way.

    I deleted this once, as duplicate logic. Those tests failed immediately and
    they were right to: the duplication is the point of a reference
    implementation, and `dev dead` now reports it honestly as reachable only
    from tests rather than pretending it is wired.
    """
    from .portion import PROFILE_FACTORS, _classify_shape, density_for

    try:
        area = float(area_ratio)
        frame = float(frame_mm2)
    except (TypeError, ValueError):
        return None
    if not (area > 0 and frame > 0):
        return None

    shape = _classify_shape(name, shape_hint)
    height = MEASURED_HEIGHTS_MM.get(shape, MEASURED_HEIGHTS_MM["default"])
    profile = PROFILE_FACTORS.get(shape, PROFILE_FACTORS["mound"])
    dens = density if (density and density > 0) else density_for(name)
    grams = area * frame * height * profile * dens / 1000.0
    if not (0 < grams < 5000):
        return None
    return round(grams, 1)
