"""Find a reference object in the pixels, instead of asking a model about it.

Why this module exists, in one measurement. A credit card lying flat beside two
breakfast tacos, photographed on a phone. Its true length is 0.362 of the frame
width -- measured off the pixels by hand, and cross-checked by the tortillas
coming out at street-taco size. Asked how long that card was, the vision model
answered 0.18: the WIDTH of the box around a card standing vertically, not the
card's length. Asked instead for a box, it drew one about half the right size.
Both readings put the photograph at roughly 460 mm across against a true 236,
and because frame AREA goes as the square of width, the meal came out +50%
where having no reference at all gave +12.8%.

That is not a prompting failure to be argued with. These models read RELATIVE
geometry well -- which box is bigger, what fraction of the plate a food covers
-- and absolute geometry badly. Locating a known rectangle in an image is the
other kind of problem, and it is one classical computer vision has solved:
edges, closed quadrilaterals, a side-ratio test. It is deterministic, it costs
no tokens, and it returns the same answer on every run, which also means it
cannot move the noise floor.

The four corners are the real prize. They are not just a length -- they are a
homography between the photograph and the tabletop, which is what will let the
food's real footprint be measured rather than estimated.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

log = structlog.get_logger()

# Length / width for each rectangle we know how to recognise.
#   credit_card  ISO/IEC 7810 ID-1, 85.60 x 53.98 mm, identical worldwide --
#                the only object most people carry that is a precise size.
REFERENCE_RECTANGLES: dict[str, tuple[float, float]] = {
    "credit_card": (85.60, 53.98),
}

# How far the observed side ratio may sit from the true one. A card viewed at
# an angle foreshortens along one axis, so some tolerance is required; too much
# and any pale rectangle on the table qualifies. 0.18 accepts roughly up to a
# 30 degree tilt and rejects the 4:3 and 1:1 rectangles that furniture, tiles
# and napkins produce.
ASPECT_TOLERANCE = 0.18

# A card smaller than this is too few pixels to fit a rectangle to; larger than
# this and it is not a card, it is the table.
MIN_AREA_FRACTION = 0.002
MAX_AREA_FRACTION = 0.25

# Opposite sides of a rectangle stay close under mild perspective. Far apart
# means the quadrilateral is a trapezoid -- a place mat, a book, a shadow.
OPPOSITE_SIDE_TOLERANCE = 0.25

# A rectangle's corners stay near 90 degrees under mild perspective. This is
# what separates a card from a plate rim or a fold of paper that happens to
# have four corners.
CORNER_ANGLE_TOLERANCE_DEG = 25.0

# How many DIFFERENT edge settings must find the same quadrilateral.
#
# This is the discriminator that made the detector safe to ship, and it was
# measured rather than reasoned. Across ten card-free bench photos the earlier
# version claimed a card in two of them -- and a phantom card sets the scale
# for the entire meal, which is worse than any error it was meant to fix. With
# consensus counted, the real card was found by 4 of the 6 settings and BOTH
# false positives by exactly 1.
#
# The principle generalises past this bench: a real edge survives being looked
# at different ways, and a coincidence does not. Two is the minimum that means
# anything, and it already separated every photo we have; asking for more would
# be tuning to a sample of one real card.
MIN_SETTING_CONSENSUS = 2

# Quads agree when they sit in the same place at the same size.
CONSENSUS_CENTRE_FRACTION = 0.03
CONSENSUS_LENGTH_TOLERANCE = 0.12


@dataclass(frozen=True)
class ReferenceFind:
    """A reference object located in the image, with its scale."""

    kind: str
    corners: list[list[float]]      # 4 (x, y) in pixels of the analysed image
    long_px: float                  # its long side, in pixels
    image_size: tuple[int, int]     # (w, h) of the analysed image
    observed_aspect: float          # its side ratio as photographed
    mm_per_px: float
    consensus: int                  # how many edge settings agreed

    @property
    def length_ratio(self) -> float:
        return self.long_px / max(self.image_size[0], 1)

    @property
    def frame_width_mm(self) -> float:
        """How wide the whole photograph is, in mm, at the reference's plane."""
        return self.image_size[0] * self.mm_per_px

    @property
    def tilt_deg(self) -> float:
        """How far the camera is off perpendicular, from the card's own shape.

        A rectangle photographed square-on shows its true side ratio; tilted,
        the ratio changes. This is the same trick the plate ellipse plays, and
        it is free here.
        """
        import math
        true_r = REFERENCE_RECTANGLES[self.kind][0] / REFERENCE_RECTANGLES[self.kind][1]
        ratio = min(self.observed_aspect / true_r, true_r / self.observed_aspect)
        return math.degrees(math.acos(max(0.05, min(1.0, ratio))))


def _worst_corner_error(pts) -> float:
    """How far the most skewed corner is from a right angle, in degrees."""
    import math

    import numpy as np

    worst = 0.0
    for i in range(4):
        a = pts[(i - 1) % 4] - pts[i]
        b = pts[(i + 1) % 4] - pts[i]
        denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1e-9
        cos = float(np.dot(a, b) / denom)
        angle = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
        worst = max(worst, abs(angle - 90.0))
    return worst


def _best_by_consensus(found: list[dict], width: int) -> tuple[int, dict] | None:
    """The quad that the most edge settings independently agreed on.

    A real object survives being looked at several ways; a coincidence of one
    threshold does not. Ties break on the closest side ratio.
    """
    if not found:
        return None
    centre_tol = CONSENSUS_CENTRE_FRACTION * width
    best: tuple[int, float, dict] | None = None
    for candidate in found:
        settings = {
            other["setting"]
            for other in found
            if abs(other["cx"] - candidate["cx"]) < centre_tol
            and abs(other["cy"] - candidate["cy"]) < centre_tol
            and abs(other["long_px"] - candidate["long_px"])
            / max(other["long_px"], candidate["long_px"]) < CONSENSUS_LENGTH_TOLERANCE
        }
        score = (len(settings), -candidate["miss"], candidate)
        if best is None or (score[0], score[1]) > (best[0], best[1]):
            best = score
    if best is None or best[0] < MIN_SETTING_CONSENSUS:
        return None
    return best[0], best[2]


def find_reference(img) -> ReferenceFind | None:
    """Locate a known rectangle in a PIL image. None when there isn't one.

    None is a perfectly good answer and the common one -- most photos have no
    card in them. The caller falls back to the rest of the ladder.

    Deliberately not tunable from outside: a scale that changes with a
    parameter is not a measurement.
    """
    try:
        import cv2
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        log.warning("reference_cv_unavailable", error=str(exc)[:200])
        return None

    try:
        w, h = img.size
        rgb = np.asarray(img.convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        frame_area = float(w * h)

        found: list[dict] = []

        # Several edge thresholds rather than one. A card can be dark on a pale
        # cloth or pale on a dark table, and no single Canny pair finds both.
        # This is not a parameter to tune -- every candidate still has to pass
        # the same geometric tests, and how MANY settings agree is itself the
        # evidence that a quad is real rather than a coincidence of one
        # threshold.
        for blur in (3, 5):
            blurred = cv2.GaussianBlur(gray, (blur, blur), 0)
            for lo, hi in ((30, 90), (50, 150), (75, 200)):
                edges = cv2.Canny(blurred, lo, hi)
                edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
                contours, _ = cv2.findContours(
                    edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
                )
                for contour in contours:
                    area = cv2.contourArea(contour)
                    if not (MIN_AREA_FRACTION * frame_area < area < MAX_AREA_FRACTION * frame_area):
                        continue
                    perimeter = cv2.arcLength(contour, True)
                    quad = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
                    if len(quad) != 4 or not cv2.isContourConvex(quad):
                        continue

                    pts = quad.reshape(4, 2).astype(float)
                    sides = [
                        float(np.linalg.norm(pts[i] - pts[(i + 1) % 4]))
                        for i in range(4)
                    ]
                    if min(sides) < 1e-6:
                        continue
                    # A rectangle's opposite sides stay close under mild
                    # perspective. A trapezoid is a place mat, not a card.
                    if abs(sides[0] - sides[2]) / max(sides[0], sides[2]) > OPPOSITE_SIDE_TOLERANCE:
                        continue
                    if abs(sides[1] - sides[3]) / max(sides[1], sides[3]) > OPPOSITE_SIDE_TOLERANCE:
                        continue
                    # ...and its corners stay near 90 degrees. This is the test
                    # that rejects a plate rim or a fold of paper with four
                    # corners: on the bench it removed most false candidates
                    # before consensus had to.
                    if _worst_corner_error(pts) > CORNER_ANGLE_TOLERANCE_DEG:
                        continue

                    long_px = (sides[0] + sides[2]) / 2.0
                    short_px = (sides[1] + sides[3]) / 2.0
                    if short_px > long_px:
                        long_px, short_px = short_px, long_px
                    observed = long_px / short_px

                    for kind, (mm_long, mm_short) in REFERENCE_RECTANGLES.items():
                        true_r = mm_long / mm_short
                        miss = abs(observed - true_r) / true_r
                        if miss > ASPECT_TOLERANCE:
                            continue
                        cx, cy = pts.mean(axis=0)
                        found.append({
                            "kind": kind, "miss": miss, "pts": pts,
                            "long_px": long_px, "observed": observed,
                            "cx": float(cx), "cy": float(cy),
                            "setting": (blur, lo, hi),
                            "mm_per_px": mm_long / long_px,
                        })

        best = _best_by_consensus(found, w)
        if best is None:
            return None
        agreed, pick = best
        found_ref = ReferenceFind(
            kind=pick["kind"],
            corners=pick["pts"].tolist(),
            long_px=pick["long_px"],
            image_size=(w, h),
            observed_aspect=pick["observed"],
            mm_per_px=pick["mm_per_px"],
            consensus=agreed,
        )
        found = found_ref
        log.info(
            "reference_found",
            kind=found.kind,
            length_ratio=round(found.length_ratio, 4),
            frame_width_mm=round(found.frame_width_mm, 1),
            observed_aspect=round(found.observed_aspect, 3),
            tilt_deg=round(found.tilt_deg, 1),
            consensus=found.consensus,
        )
        return found
    except Exception as exc:  # noqa: BLE001
        # A detector that throws must not take a scan down with it. No card is
        # always a valid answer.
        log.warning("reference_cv_failed", error=str(exc)[:200])
        return None
