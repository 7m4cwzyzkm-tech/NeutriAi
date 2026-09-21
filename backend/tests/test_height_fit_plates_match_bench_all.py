"""height_fit.py's WEIGHED table must never carry its own plate diameters --
they belong to bench_all.py, looked up by filename, or the two files can
silently disagree the way they did for days on rows 40-45 (bench_all
corrected to 217 mm on 21 Sep 2026; height_fit stayed at a stale 222 mm until
the fix this test guards).

Offline only: reads two small .py files from disk and parses them with `ast`.
No import of either module's runtime code, no network, no photo, no .env.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _cases_diameters() -> dict[str, float | None]:
    """{filename: declared plate diameter mm}, parsed from bench_all.CASES's
    source -- the same technique height_fit.bench_all_plate_diameters() uses,
    reimplemented here rather than imported, so this test does not depend on
    height_fit.py being correct to check whether it is."""
    path = SCRIPTS / "bench_all.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CASES" for t in node.targets
        ):
            return {row[0]: row[1] for row in ast.literal_eval(node.value)}
    raise AssertionError("CASES not found in bench_all.py -- has it been renamed?")


def _height_fit_rows() -> list[tuple]:
    """height_fit._ROWS, parsed from source: (filename, food, grams) triples
    with NO diameter field -- see test_rows_carries_no_diameter_field below
    for why that shape is itself the thing being guarded."""
    path = SCRIPTS / "height_fit.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_ROWS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("_ROWS not found in height_fit.py -- has it been renamed?")


def _weighed_via_public_helper():
    """height_fit.WEIGHED, built the real way: import the module (safe --
    see bench_all_plate_diameters()'s own docstring for why) and read the
    list it actually built, rather than re-deriving it here."""
    import sys

    sys.path.insert(0, str(SCRIPTS.parent))
    from scripts import height_fit

    return height_fit.WEIGHED, height_fit.bench_all_plate_diameters


def test_importing_height_fit_does_not_pull_in_env_or_network():
    """The whole point of bench_all_plate_diameters() reading SOURCE rather
    than importing scripts.bench_all: importing height_fit must not read
    backend/.env or load an HTTP client. If a future change reintroduces
    `import scripts.bench_all` here, this is the test that would catch it --
    app.config's module-level `settings = get_settings()` reads .env the
    moment it is imported, transitively, through bench_all -> scan_bench /
    segment_hosted."""
    import sys

    for mod in ("app.config", "httpx", "scripts.bench_all", "scripts.scan_bench"):
        sys.modules.pop(mod, None)

    sys.path.insert(0, str(SCRIPTS.parent))
    import scripts.height_fit  # noqa: F401

    assert "app.config" not in sys.modules, (
        "importing height_fit pulled in app.config, which reads backend/.env "
        "at import time -- see bench_all_plate_diameters()'s docstring")
    assert "httpx" not in sys.modules, (
        "importing height_fit pulled in an HTTP client at import time")


def test_every_height_fit_row_has_a_diameter_bench_all_also_declares():
    """The core guard: every photo height_fit.WEIGHED scores has a plate
    diameter, and that diameter is bench_all's own declared value for the
    same filename -- not a second, independently-typed number that could
    silently drift the way rows 40-45 did (222 in height_fit, corrected to
    217 in bench_all, for days)."""
    cases = _cases_diameters()
    weighed, _ = _weighed_via_public_helper()

    assert weighed, "WEIGHED is empty -- nothing to check"
    for name, diameter, _food, _grams in weighed:
        assert name in cases, f"{name} is in height_fit.WEIGHED but not in bench_all.CASES"
        assert diameter == cases[name], (
            f"{name}: height_fit says {diameter} mm, bench_all says {cases[name]} mm -- "
            f"these must be the same number, read from the same place")


@pytest.mark.parametrize("name,expected_mm", [
    ("40-trailmix-spread.jpg", 217),
    ("41-trailmix-heaped.jpg", 217),
    ("42-chips-spread.jpg", 217),
    ("43-chips-heaped.jpg", 217),
    ("44-grapes-spread.jpg", 217),
    ("45-grapes-cluster.jpg", 217),
])
def test_the_foam_plate_rows_specifically_read_217_not_the_old_222(name, expected_mm):
    """The regression this file exists to pin: rows 40-45 must read 217 mm
    (measured 21 Sep 2026), never the old nominal 222 mm, through EITHER
    file's own view of the number."""
    cases = _cases_diameters()
    weighed, _ = _weighed_via_public_helper()
    weighed_by_name = {row[0]: row[1] for row in weighed}

    assert cases[name] == expected_mm, f"bench_all: {name} is {cases[name]} mm, not {expected_mm}"
    assert weighed_by_name[name] == expected_mm, (
        f"height_fit: {name} is {weighed_by_name[name]} mm, not {expected_mm}")


def test_rows_carries_no_diameter_field():
    """No diameter is hard-coded twice: height_fit._ROWS -- the literal list
    in the source, before bench_all's numbers are looked up and merged in --
    must be a bare (filename, food, grams) triple with no fourth element.
    A fourth element appearing here would be a second, hand-typed diameter
    sitting right next to the looked-up one, which is exactly the duplication
    this whole file exists to remove."""
    for row in _height_fit_rows():
        assert len(row) == 3, (
            f"{row!r} has {len(row)} fields, not 3 -- a diameter has crept "
            f"back into _ROWS instead of being looked up from bench_all")
        name, food, grams = row
        assert isinstance(name, str) and name.endswith(".jpg")
        assert isinstance(food, str) and food
        assert isinstance(grams, int) and grams > 0


def test_no_bare_222_or_217_literal_sits_next_to_a_row_40_to_45_filename():
    """Belt and braces on top of the structural check above: scan
    height_fit.py's raw source text for the old (222) or new (217) plate
    diameter appearing as a token anywhere in the same statement as one of
    rows 40-45's filenames. This is the failure mode a purely structural
    check (row length) cannot see -- someone adding a diameter back as a
    second, differently-named variable rather than a fourth tuple element."""
    path = SCRIPTS / "height_fit.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    photo_names = {
        "40-trailmix-spread.jpg", "41-trailmix-heaped.jpg",
        "42-chips-spread.jpg", "43-chips-heaped.jpg",
        "44-grapes-spread.jpg", "45-grapes-cluster.jpg",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple):
            consts = [n.value for n in node.elts if isinstance(n, ast.Constant)]
            if any(c in photo_names for c in consts):
                assert 222 not in consts, f"{node.lineno}: stale 222 beside a row 40-45 filename"
                assert 217 not in consts, (
                    f"{node.lineno}: 217 hard-coded beside a row 40-45 filename -- "
                    f"should come from bench_all_plate_diameters(), not a literal")
