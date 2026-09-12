"""The sourced-constants folder, and the rules that keep it worth having.

A file called "references" that contains an unsourced guess is worse than no
file, because the next person trusts it. These tests enforce the thing that
makes the folder different from the hand-written table it supplements: every
number came from somewhere, and that somewhere is written down.
"""
from __future__ import annotations

import pytest

from app.services.nutrition import references

VALID_SOURCES = {"FAO2", "FAO1", "USDAcup", "BENCH"}


def test_every_value_carries_a_citation():
    """The whole point of the folder. A number with no citation is a guess
    wearing a source's clothes, and it is more dangerous here than in the code
    because here it looks checked."""
    uncited = [r.get("key") for r in references.reference_rows()
               if not (r.get("citation") or "").strip()]
    assert not uncited, f"rows with no citation: {uncited}"


def test_every_source_tag_is_one_we_can_follow():
    bad = [(r.get("key"), r.get("source")) for r in references.reference_rows()
           if (r.get("source") or "").strip() not in VALID_SOURCES]
    assert not bad, (
        f"unknown source tags {bad} -- add the source to the README and to "
        f"VALID_SOURCES, or the citation cannot be followed"
    )


def test_no_key_is_defined_twice():
    """Two rows for one food is two different answers, and which one wins is
    then decided by file order, which nobody is reading."""
    keys = [(r.get("key") or "").strip().lower() for r in references.reference_rows()]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"duplicate keys: {sorted(dupes)}"


@pytest.mark.parametrize("key,expected", [
    # Spot checks against the published figures, so a fat-fingered edit to the
    # CSV fails here rather than in somebody's dinner.
    ("cherry tomatoes", 0.63),     # USDA 149 g/cup whole
    ("refried beans", 1.02),       # USDA 242 g/cup
    ("bean soup", 1.054),          # FAO2
    ("potato", 0.59),              # FAO2 potato english boiled
    ("kidney beans", 0.79),        # FAO2
    ("milk", 1.04),                # FAO2
    ("olive oil", 0.918),          # FAO2
])
def test_the_published_numbers_are_the_numbers_in_the_file(key, expected):
    assert references.load_densities()[key] == pytest.approx(expected, abs=0.005)


def test_a_density_outside_physical_sense_is_refused():
    """A typo in a multiplier is a wrong meal on every scan of that food. Food
    is not lighter than foam and not denser than bone."""
    for value in (0.0, -1.0, 0.001, 5.0, 1000.0):
        assert not (references.MIN_DENSITY <= value <= references.MAX_DENSITY)
    for value in (0.28, 0.63, 1.054, 1.05):
        assert references.MIN_DENSITY <= value <= references.MAX_DENSITY


def test_the_references_actually_reach_something():
    """Otherwise this folder is the seventh thing built and not wired in -- the
    audit that produced test_wiring.py found six.

    Pinned by the VALUE that flows, not by an import, so renaming the module or
    moving the call cannot quietly sever it. The densities are deliberately not
    part of this: they are loaded and held, and their own test asserts they stay
    out of the weight formula.
    """
    from app.services.nutrition import references as R

    macros = R.load_macros()
    assert macros, "the macro reference file loaded nothing"
    beans = R.macros_for_name("refried beans")
    assert beans is not None
    assert beans["kcal_per_100g"] == pytest.approx(83)
    assert beans["source"] == "USDA 174296"

    densities = R.load_densities()
    assert densities, "the density reference file loaded nothing"
    import app.services.ai.portion as P
    assert P._SOURCED == densities, (
        "the estimator is no longer even loading the cited densities, so "
        "adopting them later would silently be a no-op"
    )


def test_the_bench_validated_value_is_not_overwritten_by_a_published_one():
    """Cooked rice is the live disagreement: FAO/INFOODS says 0.73, USDA's cup
    weight says 0.67, and the kitchen scale here supports 0.67 -- it fixed a
    +19% and a +22% over-read on two weighed meals.

    Both numbers stay visible in the file and the estimator keeps the one the
    scale supports. Splitting the difference would produce a value supported by
    neither source, which is how a compensating constant gets born.
    """
    from app.services.ai.portion import density_for
    assert density_for("rice", group="grain") == pytest.approx(0.67, abs=0.005)
    row = next(r for r in references.reference_rows()
               if (r.get("key") or "").strip() == "rice")
    assert "0.73" in (row.get("note") or ""), (
        "the disagreement with FAO/INFOODS is no longer recorded, so the next "
        "person will rediscover it"
    )


def test_a_broken_reference_file_does_not_stop_the_app():
    """A data file must never be load-bearing for startup. Without it the
    hand-written table stands, which is where this was before the folder."""
    original = references.DENSITY_FILE
    references.DENSITY_FILE = original.with_name("does-not-exist.csv")
    try:
        assert references.load_densities() == {}
        assert references.reference_rows() == []
    finally:
        references.DENSITY_FILE = original
    assert references.load_densities(), "the real file stopped loading"


# --- macros: the folder's actual job -----------------------------------------
#
# The weight of the food is decided by the geometry pipeline and by what the app
# learns from a user's own corrections. Nothing in this folder touches that.
# These tests pin that separation, because a lookup table that quietly moved a
# weight would undo days of measurement.

def test_the_reference_folder_does_not_touch_the_weight_path():
    """Densities are loaded, cited, and NOT applied. Adopting them is a decision
    somebody makes with a bench result in hand, not a side effect of adding a
    data file."""
    import app.services.ai.portion as P
    assert P.USE_SOURCED_DENSITIES is False, (
        "sourced densities are being applied to the weight formula -- if that "
        "is intended, it needs a benchall run behind it"
    )
    assert P._SOURCED, "the densities are not even being loaded"
    # The values the weighed bench validated are the values in force.
    assert P.density_for("cherry tomatoes", group="vegetable") == pytest.approx(0.55)
    assert P.density_for("refried beans", group="legume") == pytest.approx(1.06)
    assert P.density_for("rice", group="grain") == pytest.approx(0.67)


def test_a_sourced_macro_row_never_carries_a_density():
    """Belt and braces on the same rule: even the row the resolver builds from
    this folder leaves density empty, so it cannot reach the weight formula by
    the back door."""
    from app.services.nutrition import references as R
    assert "density" not in " ".join(next(iter(R.load_macros().values())).keys())


def test_energy_agrees_with_the_macros_that_carry_it():
    """Atwater: 4 kcal/g protein, 4 carbohydrate, 9 fat. Wide tolerance, because
    fibre and rounding move it legitimately -- this is here to catch a
    transposed column, not to referee nutrition science."""
    from app.services.nutrition import references as R
    for row in R.macro_rows():
        gap = R.macro_energy_gap(row)
        assert gap is not None, f"{row.get('key')} has unreadable numbers"
        assert gap <= R.ENERGY_TOLERANCE, (
            f"{row.get('key')}: stated {row.get('kcal')} kcal but its macros "
            f"imply {gap:.0%} different -- one of the columns is wrong"
        )


def test_a_row_with_a_transposed_column_is_refused():
    """The check has to actually reject something, or it is decoration."""
    from app.services.nutrition import references as R
    # protein and carbs swapped into fat: 30 g fat cannot sit in an 80 kcal food
    bad = {"key": "x", "kcal": "80", "protein_g": "5", "carbs_g": "13",
           "fat_g": "30", "code": "1"}
    assert R.macro_energy_gap(bad) > R.ENERGY_TOLERANCE


def test_every_macro_row_can_be_re_checked_by_a_stranger():
    from app.services.nutrition import references as R
    uncited = [r.get("key") for r in R.macro_rows()
               if not (r.get("code") or "").strip()]
    assert not uncited, f"macro rows with no USDA code: {uncited}"


def test_the_reference_table_is_consulted_before_the_model_is_asked():
    """The rung this exists to create. A cited row beats a recalled one even
    when the recalled one happens to be right, because only one of them can be
    audited."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "nutrition"
           / "resolver.py").read_text(encoding="utf-8")
    ref = src.index("references.macros_for_name")
    ai = src.index("_ai_estimate(name)", src.index("race_providers"))
    assert ref < ai, (
        "the model is asked to recall a number before the cited table is read"
    )


def test_the_longest_matching_name_wins():
    """"brown rice" must not be answered by a "rice" row when the specific one
    exists -- they differ by 15 kcal and 3 g of carbohydrate per 100 g."""
    from app.services.nutrition import references as R
    got = R.macros_for_name("cooked brown rice")
    assert got and got["source"] == "USDA 169704"


def test_an_unknown_food_falls_through_rather_than_guessing():
    from app.services.nutrition import references as R
    assert R.macros_for_name("mole poblano with sesame") is None
    assert R.macros_for_name("") is None
    assert R.macros_for_name(None) is None
