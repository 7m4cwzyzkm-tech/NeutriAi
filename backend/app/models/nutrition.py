from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from .common import InputBase, Base, Macros, MealSlot


class ScanRequest(InputBase):
    """Client uploads to Storage first, then hands us the object keys."""

    image_paths: list[str] = Field(min_length=1, max_length=4)
    meal_slot: MealSlot | None = None
    calibration_id: str | None = None
    note: str | None = None
    # Optional hint from the phone's depth sensor / ARKit plane estimate.
    plate_diameter_mm: float | None = Field(None, ge=60, le=400)

    # Camera geometry, when the device can measure it. Together these turn the
    # portion estimate from a prior into trigonometry: the real-world size of
    # the frame is 2 * distance * tan(fov / 2), so a photo taken from a known
    # distance with a known lens has a known scale, whatever the food is
    # resting on. This is the only path that works on paper, a cutting board or
    # a restaurant table, where there is no plate to measure against.
    camera_distance_mm: float | None = Field(None, ge=80, le=2000)
    camera_fov_deg: float | None = Field(None, ge=40, le=100)
    camera_aspect_ratio: float | None = Field(None, ge=0.5, le=2.5)

    # The captured photo's own pixel dimensions, and EXIF orientation when
    # available. Not used by any estimation rung today -- added so the
    # mobile app's on-screen 12-inch card gauge (mobile/src/lib/cardGauge.ts,
    # ported from backend/app/services/ai/card_gauge.py) has somewhere to
    # log what it measured, without requiring it: absent and unused is the
    # same as every camera_* field above when a device cannot supply one.
    image_width_px: float | None = Field(None, ge=1, le=20000)
    image_height_px: float | None = Field(None, ge=1, le=20000)
    image_orientation: int | None = Field(None, ge=1, le=8)  # EXIF Orientation, 1-8
    # card_gauge.distance_error_pct's own output: how far off the gauge's
    # 12-inch target the phone measured itself to be at capture, when a
    # measured distance was available (mobile/src/native/depth.ts -- today,
    # never, since no native depth module ships; see docs/design/mobile-
    # camera-survey-2026-09-21.md). LOGGING only, same caveat as the Python
    # source: never a reason to reject or re-scale an estimate.
    distance_error_pct: float | None = Field(None, ge=-100, le=1000)
    # The on-screen gauge's own live accelerometer tilt reading at the
    # moment of capture (mobile/src/hooks/useTiltReading.ts), in degrees
    # from level. LOGGING only; the estimator does not read this.
    tilt_deg_at_capture: float | None = Field(None, ge=0, le=90)

    # Measure each food's footprint from the pixels and report it alongside the
    # estimate, without letting it change the estimate. Off by default and set
    # only by the bench: the measurement costs about 3 seconds of OpenCV on a
    # full-size photo, which is not a price a user should pay for a number
    # nothing reads yet. Delete this field on the day the measurement becomes
    # the estimate.
    measure_footprints: bool = False


class DetectedItem(Base):
    name: str
    cuisine: str | None = None
    grams: float
    grams_low: float | None = None
    grams_high: float | None = None
    estimation_method: str = "pixel_area"
    pixel_area_ratio: float | None = None
    depth_factor: float | None = None
    confidence: float = 0.5
    # What kind of food this is. Used to decide whether two detections may be
    # merged, to pick a density when the database has none, and to sanity-check
    # the calorie figure. Carried through the response, not persisted.
    food_group: str = "composite"
    # Whether the model NAMED this dish or only described what it could see.
    #
    # "named" | "described" | "unsure". Anything but "named" means the app
    # should ask the person what this is, because the name is not a label --
    # it picks the density, the height prior and the nutrition lookup. One
    # weighed plate photographed twice came back as "creamy chicken" and
    # "creamy mushroom sauce"; that alone moved the meal from 186 g to 315 g
    # and its energy by 40%, on geometry that was within 12% both times.
    identification: str = "named"
    bbox: dict | None = None
    macros: Macros = Macros()
    food_fact_id: str | None = None
    # What the pixels say this item's footprint is, as a share of the frame.
    #
    # A SHADOW measurement: computed, reported, and not used for the grams
    # above. It is here so one bench run scores it against a kitchen scale on
    # every weighed photo at once, instead of the three plates whose outlines
    # were measured by hand.
    #
    # Why it is worth the field: the measurement does not move when the model
    # redraws a box (quadruple every box on a photo and it changes by 0%, where
    # the shipped rule changes by 300%), and on the two photos whose masks are
    # visibly correct it scores 13.3% per item against the shipped 17.7%. On the
    # one whose mask is visibly wrong it scores far worse. Three photos is not
    # enough to know which is which, and guessing that from three photos is the
    # mistake this field exists to avoid repeating.
    # The two model outputs that actually control the grams, plus the one that
    # does not. Reported so the bench scores the live inputs instead of the
    # inert one -- see PortionEstimate for the sensitivity table.
    reported_area_ratio: float | None = None
    plate_coverage_used: float | None = None
    box_area_ratio: float | None = None
    measured_area_ratio: float | None = None
    # HOW that footprint was measured, which decides what it was allowed to do.
    #
    #   "sam2"     a point-prompted segmenter. The mask never sees the plate
    #              box, so the footprint is an AREA and it reaches the grams.
    #   "colour"   the colour rule. Fenced by the plate box, so the absolute
    #              value carries the box's error and only the SHARE survives --
    #              see measured_share below. Shadow only, as it has always been.
    #
    # Reported rather than inferred so that one bench run scores the two apart.
    # A single blended accuracy number over both sources would be exactly the
    # thing nobody can act on.
    measured_area_source: str | None = None
    # Whether that footprint actually SET the weight, or was only measured
    # alongside it. Not bookkeeping: the two are indistinguishable in every
    # other field, and the bench's "N live / N shadow" line -- the line that
    # answers "is the segmenter wired up or configured and ignored" -- reads
    # exactly this. It was computed in portion.py and never carried out here,
    # so that line could only ever say "shadow", whatever the estimator did.
    measured_area_used: float | None = None
    # This item's share of all the food the pixels found on the plate.
    #
    # A SHARE, not grams, and that is the whole design. Colour tells you where
    # one food ends and the next begins; it does not tell you how much there is,
    # and the bench said so plainly -- absolute footprints scored 60% against
    # the scale. But the SPLIT survives what the absolute number does not.
    # Measured, shrinking the plate box the mask depends on:
    #
    #     plate box    absolute footprint     share of the food
    #       -5%          -1% to -4%             +2% to -1%
    #      -10%          -3% to -14%            +6% to -1%
    #
    # Three to eight times steadier, because both terms of a ratio move
    # together. So colour separates and locates; the vessel's calibrated size
    # sets the scale. Neither does the other's job.
    measured_share: float | None = None


class ScanResult(Base):
    scan_id: str
    meal_id: str | None = None
    status: str
    items: list[DetectedItem] = []
    totals: Macros = Macros()
    overall_confidence: float = 0.0
    confidence_band: str = "low"
    needs_review: bool = False
    notes: list[str] = []
    latency_ms: int | None = None
    assessment: dict | None = None

    # Did anything in the photo actually set a scale?
    #
    # A separate field rather than something the app infers from the notes,
    # because it changes what the person should be shown, and prose is a bad
    # thing to branch a screen on. Measured against weighed meals, photos with
    # a plate, a card or a camera distance came in at -3.3% bias; photos with
    # none of those came in at -26.8%, systematically light, and no amount of
    # geometry work can reach them. That is not a number to show quietly.
    scale_source: str | None = None      # plate_reference | reference_object | ...
    portion_measured: bool = True        # False => a typical serving, not this plate


class MealItemIn(InputBase):
    name: str
    grams: float = Field(gt=0, le=5000)
    food_fact_id: str | None = None
    macros: Macros | None = None
    # Which detected item this correction is an edit OF.
    #
    # Without it, a correction can only be matched back to the scan by name --
    # and the name is exactly what a rename changes, so renames taught nothing
    # at all. Inferring the pairing from list position instead would be a guess:
    # replacing rice with beans and renaming rice to mexican rice look
    # identical from the outside, and only one of them says anything about how
    # tall the food stood.
    #
    # Optional, because an item the user ADDED is an edit of nothing, and that
    # is a real and common case that must stay learnable-from-nothing.
    source_index: int | None = Field(default=None, ge=0, le=99)


class MealIn(InputBase):
    title: str = ""
    meal_slot: MealSlot = MealSlot.snack
    eaten_at: datetime | None = None
    notes: str | None = None
    items: list[MealItemIn] = []
    recipe_id: str | None = None
    servings: float = 1.0


class MealOut(Base):
    id: str
    day: date
    meal_slot: MealSlot
    title: str
    eaten_at: datetime
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fiber_g: float
    sugar_g: float
    confidence: float | None = None
    is_verified: bool = False
    photo_path: str | None = None
    items: list[dict] = []


class CalibrationIn(InputBase):
    label: str = Field(min_length=1, max_length=40)
    reference_kind: str = Field("plate", pattern="^(plate|card|coin|hand|utensil|custom)$")
    real_diameter_mm: float | None = Field(None, ge=10, le=500)
    real_area_mm2: float | None = None
    # Which vessel this measurement describes, so a photographed bowl uses the
    # measured bowl rather than the measured dinner plate. Null = general
    # default, which is the old behaviour.
    vessel: str | None = Field(None, max_length=40)
    is_default: bool = False


class VesselMeasurementIn(InputBase):
    """One vessel measured with a tape.

    Bounds are physical, not defensive: a dinner plate is not 4 cm across and
    not 60 cm across, and a typo either side of that would otherwise become the
    scale for every meal the user eats off it.
    """

    vessel: str = Field(min_length=1, max_length=40)
    # Across the widest part, rim included -- the same thing the vision model
    # reports as the vessel's apparent width, or the two do not describe the
    # same quantity and the scale is wrong by the rim.
    width_mm: float = Field(ge=60, le=600)


class AssessmentOut(Base):
    severity: str
    kcal_over: float
    pct_of_target: float
    carb_load_flag: bool
    meal_frequency: int
    headline: str
    detail: str
    portion_advice: list[str] = []
    macro_corrections: dict = {}
    next_meal: dict = {}
