from __future__ import annotations

from pydantic import Field

from .common import Base, Macros


class IngredientIn(Base):
    raw_text: str = Field(min_length=1, max_length=200)
    name: str | None = None
    quantity: float | None = None
    unit: str | None = None
    grams: float | None = None
    is_optional: bool = False


class StepIn(Base):
    n: int
    text: str = Field(min_length=1, max_length=1000)
    minutes: int | None = None
    tip: str | None = None


class RecipeIn(Base):
    title: str = Field(min_length=2, max_length=120)
    summary: str = ""
    photo_paths: list[str] = []
    categories: list[str] = []
    cuisine: str | None = None
    servings: int = Field(1, ge=1, le=50)
    prep_minutes: int = 0
    cook_minutes: int = 0
    difficulty: int = Field(2, ge=1, le=5)
    tags: list[str] = []
    is_public: bool = True
    ingredients: list[IngredientIn] = Field(min_length=1)
    steps: list[StepIn] = Field(min_length=1)


class RecipeOut(Base):
    id: str
    author_id: str
    title: str
    summary: str = ""
    photo_paths: list[str] = []
    cuisine: str | None = None
    servings: int
    prep_minutes: int
    cook_minutes: int
    difficulty: int
    tags: list[str] = []
    steps: list[dict] = []
    ingredients: list[dict] = []
    per_serving: Macros = Macros()
    is_ai_generated: bool = False
    adaptation_note: str | None = None
    like_count: int = 0
    save_count: int = 0


class AdaptRequest(Base):
    """Ask the AI to rewrite a recipe for this user's constraints."""

    target_servings: int | None = Field(None, ge=1, le=50)
    max_kcal_per_serving: int | None = Field(None, ge=100, le=3000)
    max_carbs_g: int | None = None
    min_protein_g: int | None = None
    honor_allergies: bool = True
    honor_diet_mode: bool = True
    consider_fasting_window: bool = True
    consider_workout_load: bool = True
    extra_notes: str | None = Field(None, max_length=400)
    save_as_fork: bool = True


class AdaptedRecipe(Base):
    recipe_id: str | None = None
    title: str
    adaptation_note: str
    substitutions: list[dict] = []
    ingredients: list[dict] = []
    steps: list[dict] = []
    per_serving: Macros
    servings: int
    shopping_list: list[dict] = []
    warnings: list[str] = []
