from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from .common import InputBase, Base


class WorkoutSetIn(InputBase):
    exercise_name: str
    exercise_slug: str | None = None
    set_index: int = 1
    reps: int | None = Field(None, ge=0, le=1000)
    weight_kg: float | None = Field(None, ge=0, le=1000)
    duration_s: int | None = Field(None, ge=0)
    distance_m: float | None = Field(None, ge=0)
    rpe: float | None = Field(None, ge=1, le=10)
    rest_s: int | None = None
    is_warmup: bool = False


class WorkoutIn(InputBase):
    title: str = "Workout"
    kind: str = "strength"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: int | None = None
    kcal: int | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    perceived_effort: int | None = Field(None, ge=1, le=10)
    notes: str | None = None
    plan_day_id: str | None = None
    sets: list[WorkoutSetIn] = []


class WorkoutOut(Base):
    id: str
    title: str
    kind: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_s: int | None = None
    kcal: int | None = None
    hr_zones: dict = {}
    sets: list[dict] = []
    new_prs: list[dict] = []


class EquipmentScanIn(InputBase):
    image_paths: list[str] = Field(min_length=1, max_length=4)
    space_note: str | None = Field(None, max_length=200)


class EquipmentScanOut(Base):
    id: str
    equipment: list[str]
    detected: list[dict]
    confidence: float
    fallback_to_calisthenics: bool


class PlanRequest(InputBase):
    goal: str = Field("build_muscle", max_length=40)
    days_per_week: int = Field(4, ge=1, le=7)
    weeks: int = Field(4, ge=1, le=12)
    session_minutes: int = Field(45, ge=10, le=180)
    equipment_scan_id: str | None = None
    equipment: list[str] | None = None
    experience: str = Field("intermediate", pattern="^(beginner|intermediate|advanced)$")
    limitations: list[str] = []


class PlanDayOut(Base):
    id: str
    week_index: int
    day_index: int
    title: str
    kind: str
    est_minutes: int
    blocks: list[dict]
    completed_at: datetime | None = None


class PlanOut(Base):
    id: str
    name: str
    goal: str
    days_per_week: int
    weeks: int
    equipment: list[str]
    is_calisthenics_fallback: bool
    safety_notes: list[str]
    progression: dict
    days: list[PlanDayOut] = []


class HealthDayIn(InputBase):
    """What the phone pushes up from HealthKit / Health Connect."""

    day: date
    provider: str
    steps: int | None = None
    distance_m: float | None = None
    active_kcal: int | None = None
    resting_kcal: int | None = None
    resting_hr: int | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    hrv_ms: float | None = None
    vo2max: float | None = None
    sleep_minutes: int | None = None
    sleep_deep_min: int | None = None
    sleep_rem_min: int | None = None
    raw: dict = {}
