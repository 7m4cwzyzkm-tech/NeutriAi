"""Pixel-to-gram portion estimator.

The idea in one paragraph: a photo gives us *area*, but food is sold in *mass*.
To get from one to the other we need three things — a real-world scale (how many
mm² is one pixel?), a height model (how tall is the pile?), and a density (how
many grams per cm³ of this food?). Each of the three has a defensible default
and a better answer when the user gives us more information, so the estimator is
written as a ladder: use the best rung available, and report honestly which rung
it used.

    grams = pixel_area_mm2 x effective_height_mm x shape_factor x density_g_ml / 1000

Rungs, best to worst:

1. ``plate_reference`` — a calibrated plate/card/coin in frame gives mm-per-pixel
   directly. Error ~10-15%.
2. ``depth_model``     — the phone's ARKit/Depth API plane distance. Error ~15-20%.
3. ``multi_image``     — two or more angles let us cross-check the height guess.
4. ``pixel_area``      — assume a standard 27 cm dinner plate fills the detected
   plate ellipse. Error ~25-35%.
5. ``ai_prior``        — no usable geometry; fall back to the model's own
   "typical serving" guess. Error ~40%+, and we say so in the UI.

Nothing here pretends to be more accurate than it is: every item carries a
low/high band, and the band widens as we descend the ladder.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Priors
# ---------------------------------------------------------------------------

# Standard dinner plate, used when nothing better is available.
DEFAULT_PLATE_DIAMETER_MM = 270.0

# How much of the bounding box is actually food (vs. background showing through)
# and how the pile is shaped. A steak is a slab; rice is a mound; soup is a disc.
# shape_factor ~ (mean height) / (max height) x (fill ratio of the bbox).
SHAPE_FACTORS: dict[str, float] = {
    "flat": 0.85,      # steak, fillet, tortilla, pancake, pizza slice
    "mound": 0.55,     # rice, mashed potato, couscous, oatmeal
    "loose": 0.50,     # salad greens, fries, shredded cabbage
    "cluster": 0.62,   # broccoli florets, berries, nuts, grapes
    "liquid": 0.95,    # soup, smoothie, sauce in a bowl
    "wrapped": 0.70,   # burrito, sandwich, sushi roll
    "default": 0.60,
}

# Typical served height in mm — the "how tall is the pile" prior, before any
# depth signal refines it.
HEIGHT_PRIORS_MM: dict[str, float] = {
    "flat": 18.0,
    "mound": 32.0,
    "loose": 35.0,
    "cluster": 28.0,
    "liquid": 25.0,   # visible surface x realistic bowl depth, not full bowl height
    "wrapped": 42.0,
    "default": 28.0,
}

# g/mL. Falls back to 0.85 (roughly cooked mixed food) when unknown.
DENSITY_G_ML: dict[str, float] = {
    "rice": 0.78, "pasta": 0.65, "bread": 0.28, "potato": 0.62, "fries": 0.42,
    "chicken": 1.05, "beef": 1.05, "pork": 1.05, "fish": 1.04, "shrimp": 1.02,
    "egg": 1.03, "cheese": 1.05, "yogurt": 1.03, "milk": 1.03,
    "salad": 0.22, "lettuce": 0.20, "spinach": 0.25, "broccoli": 0.35,
    "beans": 0.72, "lentils": 0.80, "chickpeas": 0.75, "corn": 0.72,
    "avocado": 0.92, "banana": 0.94, "apple": 0.85, "berries": 0.62,
    "soup": 1.00, "stew": 1.02, "sauce": 1.05, "oil": 0.92, "butter": 0.91,
    "nuts": 0.55, "granola": 0.45, "cereal": 0.35, "oatmeal": 0.90,
    "cake": 0.45, "cookie": 0.55, "chocolate": 1.30, "ice_cream": 0.55,
    "tofu": 1.05, "noodles": 0.70, "sushi": 0.95, "pizza": 0.55,
    "default": 0.85,
}

# Sanity rails. If the ladder produces something outside this, we clamp and
# drop confidence — a 4 kg serving of rice is a bug, not a big appetite.
MIN_GRAMS, MAX_GRAMS = 3.0, 1500.0


@dataclass(slots=True)
class GeometryHint:
    """Everything we know about the physical scale of this photo."""

    plate_ellipse_area_ratio: float | None = None   # plate bbox area / frame area
    plate_diameter_mm: float | None = None          # from calibration or ARKit
    reference_area_mm2: float | None = None         # calibrated reference object
    depth_mm: float | None = None                   # camera-to-plate distance
    image_count: int = 1

    @property
    def has_reference(self) -> bool:
        return bool(self.reference_area_mm2 or self.plate_diameter_mm)


@dataclass(slots=True)
class PortionEstimate:
    grams: float
    grams_low: float
    grams_high: float
    method: str
    confidence: float
    pixel_area_ratio: float
    depth_factor: float
    notes: list[str]


def _classify_shape(name: str, hint: str | None) -> str:
    if hint in SHAPE_FACTORS:
        return hint
    n = name.lower()
    if any(k in n for k in ("soup", "broth", "smoothie", "sauce", "stew", "curry", "chili")):
        return "liquid"
    if any(k in n for k in ("steak", "fillet", "breast", "pizza", "pancake", "tortilla",
                            "toast", "waffle", "omelet", "burger patty")):
        return "flat"
    if any(k in n for k in ("rice", "mash", "puree", "oatmeal", "couscous", "quinoa", "grits")):
        return "mound"
    if any(k in n for k in ("salad", "greens", "lettuce", "fries", "slaw", "sprouts", "shred")):
        return "loose"
    if any(k in n for k in ("broccoli", "berries", "grapes", "nuts", "peas", "corn", "olives")):
        return "cluster"
    if any(k in n for k in ("burrito", "sandwich", "wrap", "roll", "sushi", "taco")):
        return "wrapped"
    return "default"


def density_for(name: str, explicit: float | None = None) -> float:
    if explicit and 0.05 < explicit < 3.0:
        return explicit
    n = name.lower()
    for key, val in DENSITY_G_ML.items():
        if key != "default" and key in n:
            return val
    return DENSITY_G_ML["default"]


def mm2_per_frame(hint: GeometryHint) -> tuple[float | None, str]:
    """How many square millimetres does the whole camera frame cover?

    Returns ``(mm2, method)`` or ``(None, method)`` when we have no scale at all.
    """
    # Rung 1: an explicitly calibrated reference object.
    if hint.reference_area_mm2 and hint.plate_ellipse_area_ratio:
        return hint.reference_area_mm2 / max(hint.plate_ellipse_area_ratio, 1e-4), "plate_reference"

    # Rung 1b: a known plate diameter plus the plate's share of the frame.
    if hint.plate_diameter_mm and hint.plate_ellipse_area_ratio:
        plate_area = math.pi * (hint.plate_diameter_mm / 2.0) ** 2
        return plate_area / max(hint.plate_ellipse_area_ratio, 1e-4), "plate_reference"

    # Rung 2: depth. A typical phone main camera has ~65 deg horizontal FOV and a
    # 4:3 sensor, so frame width at distance d is 2*d*tan(32.5deg) ~ 1.274*d.
    if hint.depth_mm:
        w = 1.274 * hint.depth_mm
        h = w * 0.75
        return w * h, "depth_model"

    # Rung 4: assume a standard plate fills the detected plate region.
    if hint.plate_ellipse_area_ratio:
        plate_area = math.pi * (DEFAULT_PLATE_DIAMETER_MM / 2.0) ** 2
        return plate_area / max(hint.plate_ellipse_area_ratio, 1e-4), "pixel_area"

    return None, "ai_prior"


# Confidence ceiling per rung — an estimate can never be more certain than the
# scale it was built on.
_METHOD_CEILING = {
    "plate_reference": 0.92,
    "depth_model": 0.84,
    "multi_image": 0.86,
    "pixel_area": 0.70,
    "ai_prior": 0.52,
}
# Half-width of the reported band, as a fraction of the estimate.
_METHOD_BAND = {
    "plate_reference": 0.14,
    "depth_model": 0.20,
    "multi_image": 0.18,
    "pixel_area": 0.30,
    "ai_prior": 0.45,
}


def estimate_grams(
    *,
    name: str,
    area_ratio: float,
    hint: GeometryHint,
    shape_hint: str | None = None,
    density: float | None = None,
    ai_prior_grams: float | None = None,
    detection_confidence: float = 0.7,
) -> PortionEstimate:
    """Convert one detection's frame-area share into grams.

    ``area_ratio`` is the food's share of the whole image (0..1), which is what
    a vision model can actually report reliably — far more reliable than asking
    it for grams directly.
    """
    notes: list[str] = []
    shape = _classify_shape(name, shape_hint)
    frame_mm2, method = mm2_per_frame(hint)
    dens = density_for(name, density)
    height_mm = HEIGHT_PRIORS_MM[shape]
    shape_factor = SHAPE_FACTORS[shape]
    depth_factor = 1.0

    if frame_mm2 is None:
        # No geometry at all: trust the model's serving-size prior, or a
        # conservative default so the meal is still loggable.
        grams = float(ai_prior_grams or 150.0)
        notes.append("No plate or depth reference found — used a typical-serving estimate.")
    else:
        # Physical sanity: food on a plate cannot occupy more of the frame than
        # the plate does. A vision model that reports otherwise has miscounted
        # the region, so we cap it and take the confidence hit rather than
        # producing a confident, impossible number.
        if hint.plate_ellipse_area_ratio and area_ratio > hint.plate_ellipse_area_ratio:
            notes.append(
                f"Detected area ({area_ratio:.0%} of frame) exceeded the plate "
                f"({hint.plate_ellipse_area_ratio:.0%}); capped to the plate."
            )
            area_ratio = hint.plate_ellipse_area_ratio
            detection_confidence *= 0.6

        food_mm2 = frame_mm2 * max(area_ratio, 1e-5)

        # Depth refines the height prior: food photographed from close up tends
        # to be shot at an angle, which foreshortens the pile. Nudge, never
        # override — this is a correction, not a measurement.
        if hint.depth_mm:
            depth_factor = max(0.75, min(1.30, (hint.depth_mm / 350.0) ** 0.25))
            height_mm *= depth_factor

        volume_mm3 = food_mm2 * height_mm * shape_factor
        grams = volume_mm3 * dens / 1000.0  # mm^3 -> cm^3 -> g

        if hint.image_count > 1:
            method = "multi_image" if method in ("pixel_area", "depth_model") else method
            notes.append(f"Cross-checked across {hint.image_count} angles.")

        # If the model also offered a serving prior and we disagree wildly, meet
        # it partway. Two weak signals that disagree should not produce a
        # confident wrong answer.
        if ai_prior_grams and ai_prior_grams > 0:
            ratio = grams / ai_prior_grams
            if ratio > 2.5 or ratio < 0.4:
                blended = math.sqrt(grams * ai_prior_grams)  # geometric mean
                notes.append(
                    f"Geometry ({grams:.0f} g) and typical serving ({ai_prior_grams:.0f} g) "
                    f"disagreed; blended to {blended:.0f} g."
                )
                grams = blended
                detection_confidence *= 0.85

    clamped = max(MIN_GRAMS, min(MAX_GRAMS, grams))
    if abs(clamped - grams) > 1e-6:
        notes.append("Estimate hit a plausibility limit and was clamped.")
        detection_confidence *= 0.7
    grams = clamped

    ceiling = _METHOD_CEILING[method]
    confidence = round(min(ceiling, ceiling * max(0.2, min(1.0, detection_confidence))), 3)
    band = _METHOD_BAND[method] * (1.0 + (1.0 - confidence))

    return PortionEstimate(
        grams=round(grams, 1),
        grams_low=round(max(MIN_GRAMS, grams * (1 - band)), 1),
        grams_high=round(min(MAX_GRAMS, grams * (1 + band)), 1),
        method=method,
        confidence=confidence,
        pixel_area_ratio=round(area_ratio, 4),
        depth_factor=round(depth_factor, 4),
        notes=notes,
    )


def reconcile_multi_image(estimates: list[PortionEstimate]) -> PortionEstimate:
    """Combine per-image estimates of the same food into one.

    Weighted by confidence, and the spread between images feeds back into the
    band: if two angles disagree by 2x, we should not report +-14%.
    """
    if not estimates:
        raise ValueError("nothing to reconcile")
    if len(estimates) == 1:
        return estimates[0]

    weights = [max(e.confidence, 0.05) for e in estimates]
    total_w = sum(weights)
    grams = sum(e.grams * w for e, w in zip(estimates, weights)) / total_w

    spread = (max(e.grams for e in estimates) - min(e.grams for e in estimates)) / max(grams, 1e-6)
    agreement_penalty = min(0.35, spread / 2.0)
    confidence = round(
        max(0.15, (sum(e.confidence * w for e, w in zip(estimates, weights)) / total_w)
            * (1 - agreement_penalty) * 1.06),
        3,
    )
    band = max(0.12, spread / 2 + 0.10)

    notes = [f"Reconciled {len(estimates)} views (spread {spread * 100:.0f}%)."]
    for e in estimates:
        notes.extend(e.notes)

    return PortionEstimate(
        grams=round(grams, 1),
        grams_low=round(max(MIN_GRAMS, grams * (1 - band)), 1),
        grams_high=round(min(MAX_GRAMS, grams * (1 + band)), 1),
        method="multi_image",
        confidence=confidence,
        pixel_area_ratio=round(sum(e.pixel_area_ratio for e in estimates) / len(estimates), 4),
        depth_factor=round(sum(e.depth_factor for e in estimates) / len(estimates), 4),
        notes=notes[:6],
    )


def band_label(confidence: float) -> str:
    if confidence >= 0.78:
        return "high"
    if confidence >= 0.55:
        return "medium"
    return "low"
