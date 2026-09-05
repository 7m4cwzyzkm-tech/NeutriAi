"""Ingredient parsing decides whether recipe macros are right, so it gets
tested against the messy strings real users actually type."""
import pytest

from app.services.ai.recipe_ai import parse_quantity, to_grams

CASES = [
    ("2 1/2 cups jasmine rice", 2.5, "cups", "jasmine rice"),
    ("1 tbsp olive oil", 1.0, "tbsp", "olive oil"),
    ("200 g chicken breast", 200.0, "g", "chicken breast"),
    ("8 oz ground beef", 8.0, "oz", "ground beef"),
    ("1/4 cup honey", 0.25, "cup", "honey"),
    ("½ onion, diced", 0.5, None, "onion, diced"),
    ("3 large eggs", 3.0, None, "large eggs"),
    ("2 cloves garlic", 2.0, "cloves", "garlic"),
    ("salt to taste", None, None, "salt to taste"),
]


@pytest.mark.parametrize("raw,qty,unit,name", CASES)
def test_parse(raw, qty, unit, name):
    q, u, n = parse_quantity(raw)
    assert q == qty
    assert u == unit
    assert n == name


@pytest.mark.parametrize(
    "raw,low,high",
    [
        ("3 large eggs", 120, 180),
        ("2 cloves garlic", 3, 12),
        ("1 tbsp olive oil", 10, 16),
        ("200 g chicken breast", 199, 201),
        ("1 can chickpeas", 350, 450),
        ("½ onion, diced", 60, 90),
    ],
)
def test_grams_are_realistic(raw, low, high):
    q, u, n = parse_quantity(raw)
    grams = to_grams(q, u, n, None)
    assert low <= grams <= high, f"{raw} -> {grams} g"


def test_volume_uses_density():
    q, u, n = parse_quantity("1 cup honey")
    dense = to_grams(q, u, n, 1.42)
    light = to_grams(q, u, n, 0.25)
    assert dense > light * 4


# ---------------------------------------------------------------------------
# Regressions found by scripts/recipe_lab.py
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    ["salt to taste", "black pepper to taste", "a pinch of cinnamon",
     "fresh basil for garnish", "salt and pepper"],
)
def test_unquantified_seasoning_is_a_trace_not_a_portion(raw):
    """100 g of salt is ~39,000 mg of sodium. Defaulting seasonings to a
    portion would wreck every recipe's numbers."""
    q, u, n = parse_quantity(raw)
    grams = to_grams(q, u, n, None)
    assert grams <= 5, f"{raw} resolved to {grams} g"


@pytest.mark.parametrize(
    "raw,low,high",
    [("a handful of spinach", 20, 40),
     ("an onion", 120, 180),
     ("a clove of garlic", 1, 6)],
)
def test_article_carries_a_quantity_of_one(raw, low, high):
    q, u, n = parse_quantity(raw)
    assert q == 1.0
    assert low <= to_grams(q, u, n, None) <= high


def test_real_food_without_a_quantity_still_gets_a_portion():
    """The trace rule must not swallow actual ingredients."""
    q, u, n = parse_quantity("chicken thigh")
    assert to_grams(q, u, n, None) == 100.0
