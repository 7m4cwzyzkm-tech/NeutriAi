from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# What a request body is allowed to contain.
#
# Not a style rule -- a cost ceiling. `MealIn.items` had no length limit and
# `MealItemIn.name` no size limit, and every unknown name misses the cache,
# races three nutrition providers, and then falls through to a Claude call. One
# request could buy ten thousand of those. The same shape appeared in recipe
# ingredients and steps, workout sets, health-day pushes, post media paths and
# the food search query -- six places, one omission repeated.
#
# So the limit lives on the BASE CLASS rather than on the fields. Field
# annotations have to be remembered on every new model by every future edit,
# and the evidence in this repo is that they will not be: this exact rule was
# written for one feature and missed the next six.
MAX_LIST_ITEMS = 200
# Generous on purpose. A pasted recipe is genuinely long, and rejecting a real
# one to save bytes would be a worse bug than the one this closes.
MAX_STRING_CHARS = 20_000
MAX_NESTING = 8


def _within_limits(value: Any, depth: int = 0) -> None:
    """Walk a request body and refuse the shapes that cost money.

    Depth-limited as well, because a deeply nested body is its own denial of
    service -- against this walk before it ever reaches the database.
    """
    if depth > MAX_NESTING:
        raise ValueError("This request is nested too deeply.")
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise ValueError(
                f"One of these values is too long "
                f"({len(value):,} characters; the limit is {MAX_STRING_CHARS:,})."
            )
    elif isinstance(value, (list, tuple)):
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(
                f"That is too many items at once "
                f"({len(value):,}; the limit is {MAX_LIST_ITEMS})."
            )
        for item in value:
            _within_limits(item, depth + 1)
    elif isinstance(value, dict):
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError("That is too many fields at once.")
        for key, item in value.items():
            _within_limits(key, depth + 1)
            _within_limits(item, depth + 1)


class InputBase(Base):
    """Anything a client sends. Bounded before it reaches the database.

    Separate from `Base` because responses are built by this app, not by a
    caller: a feed of 500 posts is a legitimate response and would be an
    illegitimate request, and putting the cap on the shared base would turn a
    long timeline into a 500.
    """

    @model_validator(mode="before")
    @classmethod
    def _bounded(cls, data: Any) -> Any:
        _within_limits(data)
        return data


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
