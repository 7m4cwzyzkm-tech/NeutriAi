"""height_fit.py's WEIGHED table must never carry its own plate diameters --
they belong to bench_all.py, looked up by filename, or the two files can
silently disagree the way they did for days on rows 40-45 (bench_all
corrected to 217 mm on 21 Sep 2026; height_fit stayed at a stale 222 mm until
the fix this test guards).

Offline only: reads two small .py files from disk and parses them with `ast`.
No import of either module's runtime code, no network, no photo, no .env.

PROCESS HYGIENE (added 21 Sep 2026, the hard way). One test here used to
check "does importing height_fit avoid app.config" by popping "app.config"
out of sys.modules in-process and never putting it back -- which silently
broke test_segment_hosted.py, ninety-some tests later in the same run, on
Gil's machine (never in this cloud container, which has no real .env to
expose the bug). That check now runs in an isolated subprocess instead (see
test_importing_height_fit_does_not_pull_in_env_or_network), and
`_guard_no_process_state_leaks`, an autouse fixture below, wraps every test
in this file and fails whichever one leaks os.environ, the cwd, or
app.config's identity, so the same class of bug cannot recur silently here
again.
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
BACKEND = SCRIPTS.parent


@pytest.fixture(autouse=True)
def _guard_no_process_state_leaks():
    """Wraps every test in this file. Fails the test that caused it if
    os.environ's keys, the cwd, or app.config's identity changed during it.

    THE BUG THIS GUARDS, EXACTLY. An earlier version of
    test_importing_height_fit_does_not_pull_in_env_or_network did
    `sys.modules.pop("app.config", None)` in-process and never put it back.
    The next `from app.config import settings` anywhere in the SAME pytest
    process -- conftest.py's autouse `providers_are_never_live` fixture,
    tests/conftest.py:43, which every other test depends on -- then had to
    re-run app/config.py's module top level from scratch, building a BRAND
    NEW Settings() instance (reading the real backend/.env on whoever's
    machine ran it). conftest's fixture patched that new instance's
    segmenter_provider to "" as always, but
    app/services/ai/segment_hosted.py's own module-level `settings` name --
    bound once, at its own first import, earlier in the process -- kept
    pointing at the ORPHANED old instance, which nothing was patching
    anymore. Reproduced on 21 Sep 2026 with a fake SEGMENTER_PROVIDER=
    replicate env file in /tmp (never touching backend/.env): running
    test_height_fit_plates_match_bench_all.py before test_segment_hosted.py
    turned "assert S.from_settings().name == 'none'" into
    "'sam2:replicate' == 'none'", a failure entirely caused by this file,
    surfacing in a different one, ninety-some tests later.

    Any test in this file that legitimately needs to touch os.environ, the
    cwd, or sys.modules must use `monkeypatch` (which undoes it automatically)
    or an isolated subprocess (which cannot touch this process's state at
    all) -- never a raw mutation -- or it fails here instead of silently
    poisoning whatever test happens to run after it.
    """
    environ_before = frozenset(os.environ.keys())
    cwd_before = os.getcwd()
    app_config_before = sys.modules.get("app.config")
    settings_id_before = id(app_config_before.settings) if app_config_before else None
    cache_before = (
        app_config_before.get_settings.cache_info() if app_config_before else None
    )

    yield

    assert frozenset(os.environ.keys()) == environ_before, (
        "this test added or removed an os.environ key and did not restore "
        "it -- use monkeypatch.setenv/delenv, or an isolated subprocess")
    assert os.getcwd() == cwd_before, (
        "this test changed the working directory and did not restore it "
        "-- use monkeypatch.chdir, or an isolated subprocess")

    app_config_after = sys.modules.get("app.config")
    assert (app_config_after is not None) == (app_config_before is not None), (
        "this test added or removed app.config from sys.modules -- this is "
        "the exact mechanism that broke test_segment_hosted.py on 21 Sep "
        "2026; see this fixture's own docstring")
    if app_config_after is not None and settings_id_before is not None:
        assert id(app_config_after.settings) == settings_id_before, (
            "app.config.settings is a DIFFERENT object after this test than "
            "before it -- something popped app.config out of sys.modules "
            "and forced it to rebuild, orphaning every other already-"
            "imported module's reference to the old settings instance "
            "(the settings object conftest.py's autouse fixture patches, "
            "which is now the WRONG one)")
        assert app_config_after.get_settings.cache_info() == cache_before, (
            "app.config.get_settings's lru_cache changed shape during this "
            "test -- get_settings() was called in a way that rebuilt or "
            "reset it")


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
    segment_hosted.

    Checked in a SEPARATE PROCESS, not by popping modules out of this one's
    sys.modules: an earlier version of this test did exactly that
    (`sys.modules.pop("app.config", None)`) and never restored it, which
    corrupted every later test's view of app.config for the rest of THIS
    pytest run -- see `_guard_no_process_state_leaks` above for the full
    story of what that broke. A subprocess cannot leak into this process no
    matter what it imports, which is a stronger guarantee than remembering
    to clean up after a same-process check. It is started from a temporary
    directory outside the repo, with a deliberately minimal environment (no
    inherited SEGMENTER_*/*_API_KEY/*_TOKEN variables), so a real .env or a
    developer's shell exports cannot make this check pass or fail for the
    wrong reason either.
    """
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(BACKEND)!r}); "
        "import scripts.height_fit; "
        "print('app.config' in sys.modules); "
        "print('httpx' in sys.modules)"
    )
    # PATH (and, on Windows, SYSTEMROOT) so the interpreter itself can start;
    # nothing else -- specifically no SEGMENTER_*, *_API_KEY, *_TOKEN, or any
    # other variable a real shell or .env might have set, so this check's
    # result depends only on what height_fit.py's own imports do.
    clean_env = {"PATH": os.environ.get("PATH", "")}
    for var in ("SYSTEMROOT", "windir"):
        if var in os.environ:
            clean_env[var] = os.environ[var]

    with tempfile.TemporaryDirectory() as tmp_cwd:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=tmp_cwd, env=clean_env,
            capture_output=True, text=True, timeout=30,
        )

    assert result.returncode == 0, (
        f"subprocess failed to import height_fit:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}")
    app_config_loaded, httpx_loaded = result.stdout.strip().splitlines()
    assert app_config_loaded == "False", (
        "importing height_fit pulled in app.config, which reads backend/.env "
        "at import time -- see bench_all_plate_diameters()'s docstring")
    assert httpx_loaded == "False", (
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
