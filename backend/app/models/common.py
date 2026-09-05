from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Page(Base, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


class Ok(Base):
    ok: bool = True
    message: str | None = None


class Sex(StrEnum):
    male = "male"
    female = "female"
    other = "other"


class ActivityLevel(StrEnum):
    sedentary = "sedentary"
    light = "light"
    moderate = "moderate"
    active = "active"
    very_active = "very_active"
    athlete = "athlete"


class Goal(StrEnum):
    lose = "lose"
    maintain = "maintain"
    gain = "gain"
    recomp = "recomp"


class DietMode(StrEnum):
    balanced = "balanced"
    low_carb = "low_carb"
    keto = "keto"
    high_protein = "high_protein"
    athlete = "athlete"
    vegetarian = "vegetarian"
    vegan = "vegan"
    pescatarian = "pescatarian"
    paleo = "paleo"
    mediterranean = "mediterranean"


class MealSlot(StrEnum):
    breakfast = "breakfast"
    lunch = "lunch"
    dinner = "dinner"
    snack = "snack"
    pre_workout = "pre_workout"
    post_workout = "post_workout"


class Macros(Base):
    kcal: float = 0
    protein_g: float = 0
    carbs_g: float = 0
    fat_g: float = 0
    fiber_g: float = 0
    sugar_g: float = 0
    sodium_mg: float = 0

    def __add__(self, other: "Macros") -> "Macros":
        return Macros(
            kcal=self.kcal + other.kcal,
            protein_g=self.protein_g + other.protein_g,
            carbs_g=self.carbs_g + other.carbs_g,
            fat_g=self.fat_g + other.fat_g,
            fiber_g=self.fiber_g + other.fiber_g,
            sugar_g=self.sugar_g + other.sugar_g,
            sodium_mg=self.sodium_mg + other.sodium_mg,
        )

    def scaled(self, factor: float) -> "Macros":
        return Macros(**{k: v * factor for k, v in self.model_dump().items()})

    @classmethod
    def per_100g(cls, fact: dict) -> "Macros":
        return cls(
            kcal=float(fact.get("kcal_per_100g") or 0),
            protein_g=float(fact.get("protein_per_100g") or 0),
            carbs_g=float(fact.get("carbs_per_100g") or 0),
            fat_g=float(fact.get("fat_per_100g") or 0),
            fiber_g=float(fact.get("fiber_per_100g") or 0),
            sugar_g=float(fact.get("sugar_per_100g") or 0),
            sodium_mg=float(fact.get("sodium_mg_per_100g") or 0),
        )
