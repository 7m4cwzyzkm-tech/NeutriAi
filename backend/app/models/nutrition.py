from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from .common import Base, Macros, MealSlot


class ScanRequest(Base):
    """Client uploads to Storage first, then hands us the object keys."""

    image_paths: list[str] = Field(min_length=1, max_length=4)
    meal_slot: MealSlot | None = None
    calibration_id: str | None = None
    note: str | None = None
    # Optional hint from the phone's depth sensor / ARKit plane estimate.
    plate_diameter_mm: float | None = Field(None, ge=60, le=400)


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
    bbox: dict | None = None
    macros: Macros = Macros()
    food_fact_id: str | None = None


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


class MealItemIn(Base):
    name: str
    grams: float = Field(gt=0, le=5000)
    food_fact_id: str | None = None
    macros: Macros | None = None


class MealIn(Base):
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


class CalibrationIn(Base):
    label: str = Field(min_length=1, max_length=40)
    reference_kind: str = Field("plate", pattern="^(plate|card|coin|hand|utensil|custom)$")
    real_diameter_mm: float | None = Field(None, ge=10, le=500)
    real_area_mm2: float | None = None
    is_default: bool = False


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
