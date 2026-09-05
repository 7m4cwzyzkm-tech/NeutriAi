from __future__ import annotations

from datetime import date

from pydantic import Field

from .common import ActivityLevel, Base, DietMode, Goal, Sex


class ProfileIn(Base):
    handle: str | None = Field(None, min_length=3, max_length=24, pattern=r"^[a-zA-Z0-9_.]+$")
    display_name: str | None = Field(None, max_length=80)
    bio: str | None = Field(None, max_length=280)
    sex: Sex | None = None
    birth_date: date | None = None
    height_cm: float | None = Field(None, ge=60, le=260)
    weight_kg: float | None = Field(None, ge=20, le=400)
    target_weight_kg: float | None = Field(None, ge=20, le=400)
    activity_level: ActivityLevel | None = None
    goal: Goal | None = None
    diet_mode: DietMode | None = None
    unit_system: str | None = Field(None, pattern="^(metric|imperial)$")
    timezone: str | None = None
    is_private: bool | None = None
    push_token: str | None = None


class ProfileOut(Base):
    id: str
    handle: str
    display_name: str = ""
    avatar_url: str | None = None
    bio: str = ""
    sex: Sex | None = None
    birth_date: date | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    target_weight_kg: float | None = None
    activity_level: ActivityLevel = ActivityLevel.moderate
    goal: Goal = Goal.maintain
    diet_mode: DietMode = DietMode.balanced
    unit_system: str = "imperial"
    timezone: str = "UTC"
    is_private: bool = False
    onboarded_at: str | None = None


class RestrictionIn(Base):
    kind: str = Field("allergy", pattern="^(allergy|intolerance|avoid|religious|preference)$")
    label: str = Field(min_length=1, max_length=60)
    severity: str = Field("moderate", pattern="^(mild|moderate|severe|anaphylactic)$")


class TargetsOut(Base):
    bmr_kcal: int
    tdee_kcal: int
    target_kcal: int
    protein_g: int
    carbs_g: int
    fat_g: int
    fiber_g: int
    sugar_g_max: int
    water_ml: int
    rationale: dict = {}


class BodyMetricIn(Base):
    weight_kg: float | None = Field(None, ge=20, le=400)
    body_fat_pct: float | None = Field(None, ge=2, le=70)
    waist_cm: float | None = None
    chest_cm: float | None = None
    hip_cm: float | None = None
    arm_cm: float | None = None
    thigh_cm: float | None = None
    note: str | None = None
