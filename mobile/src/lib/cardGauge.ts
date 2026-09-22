/**
 * TypeScript port of backend/app/services/ai/card_gauge.py (branch
 * camera-gauge-axis-fix -- NOT yet on main in this repo; see this task's
 * own report for that discrepancy). Pure geometry only: every function here
 * takes plain numbers and returns a number or a state label, exactly like
 * its Python source. Nothing here reads a sensor or the camera -- callers
 * pass it whatever they measured.
 *
 * WHAT THIS IS NOT (same caveat as the Python module's own docstring): the
 * scale that feeds a gram estimate always comes from the card actually
 * FOUND in the photo, on the backend, via reference_cv.find_reference --
 * never from this on-screen guide. A user who ignores the guide still gets
 * a correctly-scaled estimate from whatever the backend finds. Nothing here
 * decides a gram.
 *
 * PORTED FAITHFULLY, NOT REIMPLEMENTED FROM MEMORY: every constant and
 * formula below is copied from the Python source with the same rounding and
 * the same names (camelCased), each with a citation. `cardLayout` was not
 * one of the four functions explicitly named for this port, but this
 * screen's card-position logic needs exactly what it computes, so it is
 * ported too, with the same citation discipline. `expectedCardBoxPoints` at
 * the bottom is NOT a port -- it is new glue code for this screen's own use
 * case, composed from the ported primitives, and is documented as such.
 */

// --- Constants, mirrored with a citation to their Python source. ---

/** backend/app/services/ai/reference_cv.py:37 --
 * REFERENCE_RECTANGLES["credit_card"] = (85.60, 53.98). ISO/IEC 7810 ID-1,
 * identical worldwide. Mirrored rather than imported, because this module
 * cannot import Python -- if this card size is ever wrong here, it is wrong
 * in exactly one place to fix, cited from here. */
export const CARD_LONG_MM = 85.6;
export const CARD_SHORT_MM = 53.98;

/** card_gauge.py:97 -- TARGET_DISTANCE_MM = 12.0 * 25.4. */
export const TARGET_DISTANCE_MM = 304.8;

/** card_gauge.py:106 -- GAUGE_WIDTH_TOLERANCE_FRAC. +/-5% on the measured
 * card width; see the Python constant's own comment for the distance/area
 * implications (+/-5% width is about +/-5% distance, about +/-10% area). */
export const GAUGE_WIDTH_TOLERANCE_FRAC = 0.05;

/** card_gauge.py:114 -- GAUGE_TILT_LIMIT_DEG. Straight down, +/-5 degrees. */
export const GAUGE_TILT_LIMIT_DEG = 5.0;

/** backend/app/services/ai/portion.py:47 -- DEFAULT_CAMERA_FOV_DEG = 68.0.
 * A LONG-AXIS value -- card_gauge.py's own module docstring, "A NOTE ON
 * WHICH AXIS," is explicit that pairing this with the frame's SHORT axis
 * (a portrait frame's on-screen width) silently under-reads by the
 * short/long pixel ratio. camera-gauge-axis-fix exists on the backend
 * because of exactly that mistake; expectedCardBoxPoints below pairs this
 * FOV with the screen's LONG axis (height, in this portrait-only screen)
 * for the same reason. */
export const DEFAULT_CAMERA_FOV_DEG = 68.0;

/** backend/app/services/ai/portion.py:48 -- DEFAULT_ASPECT_RATIO = 4/3. */
export const DEFAULT_ASPECT_RATIO = 4 / 3;

export type GaugeState = 'too_far' | 'too_close' | 'tilted' | 'ok';

/**
 * Port of card_gauge.py:119-139, `expected_card_px`. The card's expected
 * pixel (or, as used by this screen, POINT -- see expectedCardBoxPoints)
 * width, straight down, at `distanceMm`.
 *
 *     focalPx = (axisPx / 2) / tan(axisFovDeg / 2)
 *     cardPx  = focalPx * CARD_LONG_MM / distanceMm
 *
 * `axisPx` and `axisFovDeg` MUST describe the SAME axis -- this function
 * does not know or care which one, exactly like its Python source. Getting
 * that pairing wrong is the bug the Python module's `*_portrait` functions
 * exist to make impossible; this port keeps only the single-axis form,
 * because this screen is always portrait and always pairs `axisPx` with the
 * frame's LONG axis and the LONG-axis default FOV (see
 * expectedCardBoxPoints) -- the orientation-independent variant was not
 * needed for a screen that is never rotated.
 */
export function expectedCardPx(
  axisPx: number,
  axisFovDeg: number,
  distanceMm: number = TARGET_DISTANCE_MM,
): number {
  const focalPx = axisPx / 2 / Math.tan(((axisFovDeg * Math.PI) / 180) / 2);
  return (focalPx * CARD_LONG_MM) / distanceMm;
}

/**
 * Port of card_gauge.py:224-233, `distance_error_pct`. How far a trial
 * photo's actual distance departed from the 12-inch target, as a signed
 * percentage. Positive: farther than the target.
 *
 * For LOGGING a trial photo's scan metadata only -- same caveat as the
 * Python docstring: never a reason to reject or re-scale an estimate. The
 * real scale still comes from the card actually found in the photo.
 */
export function distanceErrorPct(
  actualMm: number,
  targetMm: number = TARGET_DISTANCE_MM,
): number {
  return (actualMm / targetMm - 1) * 100;
}

/**
 * Port of card_gauge.py:236-254, `gauge_state`. One of "too_far",
 * "too_close", "tilted", "ok" -- for the on-screen guide's live colour/
 * label, nothing else.
 *
 * Tilt is checked FIRST, same precedence as the Python source: a tilted
 * card foreshortens on camera, which corrupts the width reading this same
 * gauge depends on, so a tilt problem is surfaced even when the
 * (now-unreliable) width also happens to look out of range.
 */
export function gaugeState(
  measuredCardPx: number,
  expectedPx: number,
  tiltDeg: number,
): GaugeState {
  if (tiltDeg >= GAUGE_TILT_LIMIT_DEG) return 'tilted';
  const ratio = measuredCardPx / expectedPx;
  if (ratio < 1 - GAUGE_WIDTH_TOLERANCE_FRAC) return 'too_far';
  if (ratio > 1 + GAUGE_WIDTH_TOLERANCE_FRAC) return 'too_close';
  return 'ok';
}

export type FrameCoverage = {
  distanceMm: number;
  fovDeg: number;
  aspectRatio: number; // frame width / height, portrait < 1.0
  longAxisMm: number; // portrait: the frame's HEIGHT
  shortAxisMm: number; // portrait: the frame's WIDTH
};

/**
 * Port of card_gauge.py:257-289, `FrameCoverage`/`frame_ground_coverage_mm`.
 * How much table the frame covers at `distanceMm`, given the LONG-axis FOV
 * and the frame's aspect ratio -- exactly the backend's `mm2_per_frame`
 * depth rung: `longSide = 2 * D * tan(fov / 2)`, then the short axis via the
 * aspect ratio, not a second trig pass.
 *
 * `aspectRatio` defaults to the reciprocal of DEFAULT_ASPECT_RATIO, exactly
 * as the Python default does, because this screen's frame is portrait.
 */
export function frameGroundCoverageMm(
  fovDeg: number = DEFAULT_CAMERA_FOV_DEG,
  aspectRatio: number = 1 / DEFAULT_ASPECT_RATIO,
  distanceMm: number = TARGET_DISTANCE_MM,
): FrameCoverage {
  const longAxisMm = 2 * distanceMm * Math.tan(((fovDeg * Math.PI) / 180) / 2);
  const shortAxisMm = aspectRatio >= 1 ? longAxisMm / aspectRatio : longAxisMm * aspectRatio;
  return { distanceMm, fovDeg, aspectRatio, longAxisMm, shortAxisMm };
}

export type CardSide = 'above_or_below' | 'insufficient_height' | 'does_not_fit_width';

export type CardLayoutResult = {
  coverage: FrameCoverage;
  plateDiameterMm: number;
  cardSide: CardSide;
  verticalMarginMm: number; // spare room along the long (height) axis
};

/**
 * Port of card_gauge.py:295-344, `CardLayout`/`card_layout`. Where the card
 * should sit relative to the plate, for a portrait frame at `distanceMm`:
 * ABOVE OR BELOW, never beside it -- the plate already needs most of the
 * frame's WIDTH (the short axis, the tighter dimension in portrait), so
 * there is no width budget left for the card next to it. Not one of the
 * four functions named for this port, but this screen's "card position
 * above or below the plate" requirement is exactly this function's job.
 */
export function cardLayout(
  plateDiameterMm: number,
  fovDeg: number = DEFAULT_CAMERA_FOV_DEG,
  aspectRatio: number = 1 / DEFAULT_ASPECT_RATIO,
  distanceMm: number = TARGET_DISTANCE_MM,
): CardLayoutResult {
  const coverage = frameGroundCoverageMm(fovDeg, aspectRatio, distanceMm);
  if (plateDiameterMm > coverage.shortAxisMm) {
    return {
      coverage,
      plateDiameterMm,
      cardSide: 'does_not_fit_width',
      verticalMarginMm: coverage.shortAxisMm - plateDiameterMm,
    };
  }
  const neededHeightMm = plateDiameterMm + CARD_SHORT_MM;
  const margin = coverage.longAxisMm - neededHeightMm;
  return {
    coverage,
    plateDiameterMm,
    cardSide: margin >= 0 ? 'above_or_below' : 'insufficient_height',
    verticalMarginMm: margin,
  };
}

/**
 * NOT a port -- new glue code for this screen's specific use case, built
 * from the ported primitives above.
 *
 * The on-screen guide box's size, in POINTS (the unit React Native layout
 * styles use), not raw camera pixels. This calls `expectedCardPx` with the
 * frame's LONG axis -- this screen's own rendered HEIGHT, in points --
 * paired with the LONG-axis default FOV, which is the correct pairing (see
 * `expectedCardPx`'s own doc and card_gauge.py's "A NOTE ON WHICH AXIS").
 *
 * This works because `expectedCardPx` is a pure ratio of angular subtense
 * to axis unit count: it does not know or care whether "axisPx" means
 * camera sensor pixels or on-screen render points, only that the FOV
 * describes that same axis. Feeding it the screen's own point-dimensions
 * therefore returns the card's expected size in THAT SAME unit -- points --
 * which is exactly what an RN View's width/height style needs. This is the
 * generalisation of how the original hardcoded 132 x 83 pt box was sized in
 * the first place (card_gauge.py's own docstring: "132 x 83 points --
 * width:height = 1.590, matching the card's own long:short ratio... to
 * within rounding").
 */
export function expectedCardBoxPoints(
  screenWidthPoints: number,
  screenHeightPoints: number,
  fovDeg: number = DEFAULT_CAMERA_FOV_DEG,
  distanceMm: number = TARGET_DISTANCE_MM,
): { widthPoints: number; heightPoints: number } {
  const longAxisPoints = Math.max(screenWidthPoints, screenHeightPoints);
  const cardLongEdgePoints = expectedCardPx(longAxisPoints, fovDeg, distanceMm);
  // The card lies with its LONG edge horizontal -- the same evidence
  // card_gauge.py's own docstring cites (the original box's width:height
  // ratio, 1.590, matches CARD_LONG_MM/CARD_SHORT_MM, 1.586).
  return {
    widthPoints: cardLongEdgePoints,
    heightPoints: cardLongEdgePoints * (CARD_SHORT_MM / CARD_LONG_MM),
  };
}
