"""Sourced constants, loaded from data/macro_references.

Density multiplies every gram the app reports, and it is the one term in the
pipeline that other people have already measured and published. Guessing at it
was indefensible; this reads the published numbers instead.

WHAT THIS DELIBERATELY DOES NOT DO

It does not overwrite a value the weighed bench has validated. Cooked rice is
the live case: FAO/INFOODS gives 0.73, USDA's cup weight gives 0.67, and the
kitchen scale here supports 0.67 -- moving to it fixed a +19% and a +22%
over-read on two meals. So the file records both numbers and the estimator keeps
the one the scale supports.

The rule is: sourced values FILL GAPS and are visible where they disagree. A
reference file that silently reassigned bench-validated constants would be a
fifth way to lose a measurement, and this codebase has had enough of those.
"""
from __future__ import annotations

import csv
import pathlib

import structlog

log = structlog.get_logger()

REFERENCE_DIR = pathlib.Path(__file__).resolve().parents[3] / "data" / "macro_references"
DENSITY_FILE = REFERENCE_DIR / "densities.csv"
MACRO_FILE = REFERENCE_DIR / "macros_per_100g.csv"

# A food is not lighter than aerated foam and not denser than bone. Anything
# outside this is a typo or a unit mix-up, and a typo in a multiplier is a wrong
# meal on every scan of that food.
MIN_DENSITY, MAX_DENSITY = 0.05, 2.0


def _read(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        log.warning("reference_file_missing", path=str(path))
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("reference_file_unreadable", path=str(path), error=str(exc)[:200])
        return []
    # Comments carry the sourcing rules and have to survive in the file, so they
    # are stripped here rather than banned there.
    body = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return list(csv.DictReader(body))


def _rows() -> list[dict]:
    return _read(DENSITY_FILE)


def macro_rows() -> list[dict]:
    return _read(MACRO_FILE)


def load_densities() -> dict[str, float]:
    """Sourced densities by food key, skipping anything that fails a check.

    Skips rather than raises. A malformed row is a bad line in a data file, not
    a reason a user's scan fails -- the estimator falls back to what it had.
    """
    out: dict[str, float] = {}
    for row in _rows():
        key = (row.get("key") or "").strip().lower()
        if not key:
            continue
        try:
            value = float(row.get("density_g_ml"))
        except (TypeError, ValueError):
            log.warning("density_reference_bad_number", key=key)
            continue
        if not (MIN_DENSITY <= value <= MAX_DENSITY):
            log.warning("density_reference_out_of_range", key=key, value=value)
            continue
        if not (row.get("citation") or "").strip():
            # The whole point of the folder. A number with no citation is a
            # guess wearing a source's clothes, and it is more dangerous here
            # than in the code, because here it looks checked.
            log.warning("density_reference_uncited", key=key)
            continue
        out[key] = value
    return out


def reference_rows() -> list[dict]:
    """The raw rows, for the tests and for anything that wants the citations."""
    return _rows()


# ---------------------------------------------------------------------------
# Macros
# ---------------------------------------------------------------------------
#
# The weight of the food is decided by the geometry pipeline and by what we
# learn from the user's own corrections. Nothing here touches that. This answers
# only the second question: given a weight, what are the macros?
#
# It sits between a live USDA lookup and the AI estimate, and it exists to push
# the AI estimate further down. Asking a model to recall "standard reference
# values" produces a number nobody can check, in a field where being a third
# light is the documented failure of every competing app.

# Energy has to agree with the macros that carry it, or one of them is wrong.
# Atwater: 4 kcal/g protein, 4 carbohydrate, 9 fat. The tolerance is wide because
# fibre, alcohol and rounding all move it legitimately; it is here to catch a
# transposed column, not to referee nutrition science.
ATWATER = (4.0, 4.0, 9.0)
ENERGY_TOLERANCE = 0.25


def macro_energy_gap(row: dict) -> float | None:
    """How far a row's stated energy is from the energy its macros imply."""
    try:
        kcal = float(row["kcal"])
        implied = (float(row["protein_g"]) * ATWATER[0]
                   + float(row["carbs_g"]) * ATWATER[1]
                   + float(row["fat_g"]) * ATWATER[2])
    except (TypeError, ValueError, KeyError):
        return None
    if kcal <= 0:
        return None
    return abs(implied - kcal) / kcal


def load_macros() -> dict[str, dict]:
    """Per-100 g macros by food key, skipping any row that fails its checks.

    Skips rather than raises, for the same reason the densities do: a bad line
    in a data file is not a reason a user's meal fails to log.
    """
    out: dict[str, dict] = {}
    for row in macro_rows():
        key = (row.get("key") or "").strip().lower()
        if not key:
            continue
        if not (row.get("code") or "").strip():
            log.warning("macro_reference_uncited", key=key)
            continue
        gap = macro_energy_gap(row)
        if gap is None or gap > ENERGY_TOLERANCE:
            log.warning("macro_reference_energy_mismatch", key=key, gap=gap)
            continue
        try:
            out[key] = {
                "kcal_per_100g": float(row["kcal"]),
                "protein_per_100g": float(row["protein_g"]),
                "carbs_per_100g": float(row["carbs_g"]),
                "fat_per_100g": float(row["fat_g"]),
                "source": f"USDA {row.get('code')}",
            }
        except (TypeError, ValueError, KeyError):
            log.warning("macro_reference_bad_row", key=key)
    return out


def macros_for_name(name: str) -> dict | None:
    """The sourced macros for a food name, matched the way foods are named.

    Longest key first, so "brown rice" is not answered by a "rice" row when the
    specific one exists.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    n = name.strip().lower()
    table = load_macros()
    for key in sorted(table, key=len, reverse=True):
        if key in n:
            return table[key]
    return None
