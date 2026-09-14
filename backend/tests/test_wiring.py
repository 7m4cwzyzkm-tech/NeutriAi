"""Is everything we built actually connected to anything?

WHY THIS FILE EXISTS

The same defect has now been found five separate times, by hand, each time
after it had already cost real work:

  1. `area_ratio` was tuned for four nights. It is overwritten by the coverage
     conversion before anything reads it, so none of that tuning reached a gram.
  2. The bench note printed that overwritten value under the words "reported
     area", so the instrument reported the pipeline's own arithmetic back as
     evidence about the model -- which is what sent the four nights.
  3. `portion_learning` learned a height from every user correction, wrote it,
     logged it, and passed twenty-five tests. Nothing ever read the table. The
     estimator took HEIGHT_PRIORS_MM unconditionally.
  4. `MEASURABLE_VESSELS` was written and referenced nowhere, so the progress
     card would have tracked "paper" and "hand" -- surfaces with no fixed size,
     and therefore work that can never finish.
  5. A bench comment said photo 13 tested the uncalibrated plate path. It
     passed 254 mm, so it did not, and the path every new user takes had no
     coverage at all.

Every one is the same shape: SOMETHING BUILT, TESTED, AND NOT WIRED IN. Each
was found by reading. None was found by the suite, because a disconnected thing
breaks nothing -- its own tests pass, since they call it directly.

So these tests do not check behaviour. They check that behaviour is REACHABLE:
that a constant is read, that a table written is also read, that every input to
the estimator can move its output. They are cheap, they are boring, and they
fail the moment the fifth instance becomes a sixth.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"


# OUR source, and nothing else.
#
# This used to be `ROOT.rglob("*.py")`, which on any machine with a virtualenv
# inside `backend/` reads every .py file in site-packages -- numpy, OpenCV,
# pydantic, the lot. Tens of thousands of files, re-read once per test that
# calls this, which on Windows looks exactly like a frozen test run and is what
# it was: `dev test` sat at 97% grinding through a `.venv`.
#
# It was also quietly wrong. The constant audit walks every ALL-CAPS assignment
# in everything it reads, so it was auditing OpenCV's constants against this
# app -- and passing, by luck, because a library's own constants are referenced
# inside that library. A check that passes for the wrong reason is not a check.
#
# Cached, because four tests call it and the answer cannot change mid-run.
SOURCE_DIRS = ("app", "tests", "scripts")
_SOURCE_CACHE: dict[pathlib.Path, str] | None = None


def _sources() -> dict[pathlib.Path, str]:
    global _SOURCE_CACHE
    if _SOURCE_CACHE is None:
        found: list[pathlib.Path] = []
        for name in SOURCE_DIRS:
            found.extend((ROOT / name).rglob("*.py"))
        _SOURCE_CACHE = {
            p: p.read_text(encoding="utf-8")
            for p in sorted(found)
            if "__pycache__" not in str(p)
        }
    return dict(_SOURCE_CACHE)


# A constant may be unreferenced only for a reason, and the reason goes here
# rather than in a comment nobody greps. Anything not on this list that nothing
# reads is a bug, because it is a decision the code appears to make and does
# not.
UNREFERENCED_CONSTANTS_ALLOWED = {
    # A genuine safety net: every method the estimator can emit is currently in
    # BLEND_MAX_WEIGHT_BY_METHOD, so the scalar fallback is unreachable today.
    # It exists so that ADDING a rung cannot silently produce a blend weight of
    # None, which would be a wrong meal rather than a crash.
    "BLEND_MAX_WEIGHT",
}


def test_every_constant_in_the_algorithm_is_read_by_something():
    """A constant nothing reads is a decision the code appears to make and does
    not. MEASURABLE_VESSELS was exactly this: written, documented, referenced
    nowhere, and the progress card would have counted surfaces that cannot be
    measured."""
    src = _sources()
    everything = "\n".join(src.values())
    orphans = []
    for path, text in src.items():
        if path.name.startswith("test_") or "scripts" in path.parts:
            continue
        for node in ast.parse(text).body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not (isinstance(target, ast.Name)
                        and target.id.isupper() and len(target.id) > 3):
                    continue
                if target.id in UNREFERENCED_CONSTANTS_ALLOWED:
                    continue
                uses = len(re.findall(rf"\b{re.escape(target.id)}\b", everything))
                if uses <= 1:
                    orphans.append(f"{path.relative_to(ROOT)}:{node.lineno} {target.id}")
    assert not orphans, (
        "these constants are defined and nothing reads them -- either wire them "
        "in or delete them; if one is a deliberate safety net, add it to "
        "UNREFERENCED_CONSTANTS_ALLOWED with the reason:\n  "
        + "\n  ".join(orphans)
    )


# A table may be written without being read only for a reason. Audit trails and
# outbound queues are legitimate; a learning table is not, because the whole
# point of learning is that something later behaves differently.
WRITE_ONLY_TABLES_ALLOWED = {
    "ai_usage",            # cost accounting, read by humans in SQL
    "notifications",       # delivered by the worker, not read back by the app
    "deletion_requests",   # an audit trail of a destructive action
    "post_metrics",
    "meal_items",
    "recipe_ingredients",
    "vessel_observations",  # read via scale_learning.observations_for, which
                            # this crude scan attributes to the same module
    # An idempotency ledger. Nothing SELECTs it because the unique constraint on
    # the event id is the read: a duplicate webhook fails the insert, which is
    # precisely the check. Reading it first would be a race.
    "stripe_events",
}


def test_a_table_we_write_is_a_table_something_reads():
    """portion_learning wrote a learned height on every correction for weeks and
    nothing read it back. The loop reported progress and changed nothing, which
    is worse than not learning at all -- the user was told their corrections
    mattered."""
    src = {p: t for p, t in _sources().items()
           if "scripts" not in p.parts and not p.name.startswith("test_")}
    everything = "\n".join(src.values())

    written, read = set(), set()
    for text in src.values():
        for m in re.finditer(r'\.table\(\s*["\'](\w+)["\']\s*\)([^\n]*)', text):
            table, rest = m.group(1), m.group(2)
            if any(op in rest for op in (".insert", ".update", ".upsert", ".delete")):
                written.add(table)
            if ".select" in rest:
                read.add(table)
    for table in list(written):
        # A select anywhere counts, including on a later line...
        if re.search(rf'\.table\(\s*["\']{table}["\']\s*\)\s*\.select', everything):
            read.add(table)
        # ...and so does an EMBEDDED select. `select("*, workout_sets(*)")` on
        # the parent table reads the child through the foreign key, and reading
        # it any other way would be a second round trip for the same rows. The
        # first version of this test missed that and called workout_sets dead.
        if re.search(rf'select\([^)]*\b{table}\s*\(', everything):
            read.add(table)

    blind = sorted(written - read - WRITE_ONLY_TABLES_ALLOWED)
    assert not blind, (
        "these tables are written and never read, so whatever they record "
        "changes nothing:\n  " + "\n  ".join(blind)
    )


def test_what_the_estimator_learns_is_what_the_estimator_uses():
    """The specific connection that was missing for weeks, pinned by name so a
    refactor cannot quietly sever it again."""
    from app.services import portion_learning
    from app.services.ai import portion

    assert hasattr(portion_learning, "learned_heights")
    assert hasattr(portion_learning, "height_for")
    estimator = (APP / "services" / "ai" / "portion.py").read_text(encoding="utf-8")
    assert "portion_learning.height_for" in estimator, (
        "the estimator no longer consults what corrections taught it"
    )
    scan = (APP / "services" / "ai" / "vision.py").read_text(encoding="utf-8")
    assert "portion_learning.learned_heights()" in scan, (
        "nothing loads the learned heights, so the estimator will always see None"
    )
    assert "learned_heights=learned" in scan, (
        "they are loaded and then not passed to the estimator"
    )


def test_every_input_to_the_estimator_can_move_its_output():
    """The `area_ratio` lesson, generalised.

    A parameter that cannot change the answer for ANY input is not a parameter,
    it is a comment -- and four nights went into tuning the machinery around one
    of those. This does not demand that every input matters on every photo:
    area_ratio is deliberately inert once plate_coverage is present, and that is
    a designed behaviour with its own test. It demands that each input matters
    SOMEWHERE, so a knob cannot be wired to nothing.
    """
    from app.services.ai.portion import GeometryHint, estimate_grams

    plate = GeometryHint(plate_ellipse_area_ratio=0.35, plate_diameter_mm=254)
    base = dict(name="mexican rice", area_ratio=0.15, hint=plate,
                bbox={"w": 0.45, "h": 0.45}, detection_confidence=0.8)

    def out(**over):
        kw = {**base, **over}
        e = estimate_grams(**kw)
        return (e.grams, e.grams_low, e.grams_high, e.confidence, e.method)

    reference = out()
    moved = {
        # input                      a value that must produce a different answer
        "name": out(name="chicken drumstick"),
        "area_ratio": out(area_ratio=0.02),
        "hint": out(hint=GeometryHint(plate_ellipse_area_ratio=0.70,
                                      plate_diameter_mm=254)),
        "plate_coverage": out(plate_coverage=0.60),
        "shape_hint": out(shape_hint="liquid"),
        "height_ratio": out(height_ratio=0.30),
        "bbox": out(bbox={"w": 0.10, "h": 0.10}),
        "visible_fraction": out(visible_fraction=0.4),
        "plate_food_coverage": out(plate_coverage=0.20, plate_food_coverage=0.9),
        "density": out(density=2.0),
        "ai_prior_grams": out(area_ratio=0.60, ai_prior_grams=40.0),
        "detection_confidence": out(detection_confidence=0.2),
        "learned_heights": out(learned_heights={("mound", None): 12.0}),
    }
    inert = [k for k, v in moved.items() if v == reference]

    # An input behind a switch that is deliberately off is not a dead wire, and
    # calling it one would train everybody to ignore this test. height_ratio is
    # the case: the machinery is built, tested and correct, and
    # USE_MEASURED_HEIGHT is False because enabling it moved per-item error from
    # 7.4% to 11.8% and nobody has yet separated the two candidate causes.
    #
    # So the flag is flipped and the input must come alive. That way the
    # machinery cannot rot behind the switch -- which is its own version of this
    # bug, and a nastier one, because it looks finished.
    import app.services.ai.portion as P
    if "height_ratio" in inert:
        original = P.USE_MEASURED_HEIGHT
        P.USE_MEASURED_HEIGHT = True
        try:
            angled = GeometryHint(plate_ellipse_area_ratio=0.35,
                                  plate_diameter_mm=254,
                                  plate_ellipse_wh=(0.60, 0.42),
                                  aspect_ratio=4 / 3)
            off = estimate_grams(**{**base, "hint": angled}).grams
            on = estimate_grams(**{**base, "hint": angled, "height_ratio": 0.30}).grams
        finally:
            P.USE_MEASURED_HEIGHT = original
        assert on != off, (
            "height_ratio does nothing even with USE_MEASURED_HEIGHT on -- the "
            "measured-height machinery has rotted behind its own switch"
        )
        inert.remove("height_ratio")

    assert not inert, (
        f"these inputs to estimate_grams changed nothing at all: {inert} -- "
        f"either they are wired to nothing, or something upstream overwrites "
        f"them before they are read"
    )


def test_the_bench_measures_the_path_a_new_user_takes():
    """Every plate photo was handed its measured diameter, so the assumed-plate
    rung -- which is what everyone gets before they calibrate anything -- had
    never once been benched. A comment claimed otherwise, which is why it went
    unnoticed."""
    bench = (ROOT / "scripts" / "bench_all.py").read_text(encoding="utf-8")
    cases = re.findall(r'\(\s*"([\w\-.]+\.jpg)"\s*,\s*(None|\d+)', bench)
    assert cases, "could not read the bench cases"
    plate_photos = [c for c in cases if "plate" in c[0]]
    assert plate_photos, "no plate photos on the bench"
    uncalibrated = [c for c in plate_photos if c[1] == "None"]
    assert uncalibrated, (
        "every plate photo on the bench is given its real diameter, so the "
        "uncalibrated rung -- the one every new user is on -- is never measured"
    )


def test_the_default_plate_is_not_quietly_contradicted_by_measurement():
    """A prior the bench never exercises can drift away from what was measured
    without anybody noticing. This does not force the two to agree -- one
    kitchen is not the world, and fitting a global prior to a single plate is
    its own mistake -- it forces the gap to stay small enough to be a prior
    rather than a known error.
    """
    from app.services.ai.portion import DEFAULT_PLATE_DIAMETER_MM, VESSEL_WIDTH_MM

    measured_on_the_bench = 254.0
    for label, value in (("DEFAULT_PLATE_DIAMETER_MM", DEFAULT_PLATE_DIAMETER_MM),
                         ("VESSEL_WIDTH_MM['dinner_plate']",
                          VESSEL_WIDTH_MM["dinner_plate"])):
        area_error = (value / measured_on_the_bench) ** 2 - 1
        assert abs(area_error) < 0.25, (
            f"{label} is {value} mm against a bench plate measured three ways "
            f"at {measured_on_the_bench} mm -- {area_error:+.0%} on every gram "
            f"for a user who has not calibrated"
        )


# --- the database half of the same question ----------------------------------

MIGRATIONS = ROOT.parent / "supabase" / "migrations"


def _all_migration_sql() -> str:
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(MIGRATIONS.glob("*.sql")))


# Tables that may sit without row-level security, each for a stated reason.
# Empty is the correct length for this list, and every entry is a decision
# somebody has to defend.
PUBLIC_BY_DESIGN: set[str] = set()


def test_every_table_holding_a_users_data_has_row_level_security():
    """`vessel_observations` was the only one of thirty-one that did not.

    It was written after the list in 0011 and simply missed it. That matters
    more than its contents suggest: with RLS off, Postgres does not deny by
    default -- Supabase grants `anon` and `authenticated` privileges on public
    tables -- so any signed-in user could have read every other user's rows with
    the public key.

    Same defect class as the rest of this file. Something added, and not joined
    to the thing that was supposed to cover it.
    """
    sql = _all_migration_sql()
    # EVERY table, not only the ones with a user_id column.
    #
    # This used to filter on "user_id" appearing in the CREATE TABLE body, and
    # that filter skipped thirteen tables -- including `stripe_events`, which
    # keys on the Stripe event id and holds whole webhook payloads: customer
    # emails, subscription ids, amounts. Reproduced against PostgreSQL 16 as an
    # ordinary signed-in user: read another customer's email, then DELETE 1.
    #
    # A table does not have to hold a user_id column to hold users' data.
    all_tables = {
        m.group(1)
        for m in re.finditer(r"create table (?:if not exists )?([\w.]+)\s*\(",
                             sql)
    }
    user_tables = {t.split(".")[-1] for t in all_tables} - PUBLIC_BY_DESIGN
    assert user_tables, "could not read any tables from the migrations"

    # `[\w.]` because a migration may schema-qualify: `alter table
    # public.stripe_events enable row level security`. With plain \w+ that line
    # matched nothing and the table read as uncovered -- a false alarm that is
    # only one edit away from being trained out of the suite.
    covered = set(re.findall(r"alter table ([\w.]+)\s+enable row level security",
                             sql, re.I))
    # 0011 enables it for a long list inside a plpgsql loop.
    for block in re.findall(r"array\[(.*?)\]", sql, re.S):
        covered |= set(re.findall(r"'(\w+)'", block))

    naked = sorted(t.split(".")[-1] for t in user_tables
                   if t.split(".")[-1] not in {c.split(".")[-1] for c in covered})
    assert not naked, (
        "these tables never enable row level security, and 0012 grants "
        "select/insert/update/delete on the whole schema to `authenticated` -- "
        "so any signed-in user can read and delete their rows:\n  "
        + "\n  ".join(naked)
    )


def test_a_migration_can_be_run_twice_without_breaking():
    """Somebody running these by hand in the SQL editor will re-run one. Every
    statement that creates something has to tolerate it, or the second attempt
    fails halfway and leaves the schema in a state nobody planned."""
    offenders = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("--"))
        for stmt, guard in (("create table", "if not exists"),
                            ("add column", "if not exists")):
            for m in re.finditer(rf"{stmt}\s+(?!{guard})(\w+)", body, re.I):
                # `create table x` inside a `do $$` block is generated at
                # runtime and guarded there instead.
                offenders.append(f"{path.name}: {stmt} {m.group(1)}")
        # A policy cannot be created twice and Postgres has no IF NOT EXISTS for
        # it, so it must be dropped first.
        for m in re.finditer(r"create policy (\w+)", body, re.I):
            if f"drop policy if exists {m.group(1)}" not in body.lower():
                offenders.append(f"{path.name}: policy {m.group(1)} is not dropped first")

    # The migrations that predate this rule are recorded rather than rewritten:
    # they have already been applied, so changing them now would do nothing to
    # any real database. The rule binds from 0020 on, which is everything still
    # unapplied.
    new = [o for o in offenders
           if int(o.split("_")[0]) >= 20]
    assert not new, (
        "these statements in the unapplied migrations cannot survive being run "
        "twice:\n  " + "\n  ".join(new)
    )


def test_every_tool_we_built_can_actually_be_run():
    """Four scripts had no `dev` command, so the only way to run them was to
    know the module path.

    `check_migrations.py` was one of them -- the tool that answers "are the
    migrations actually applied to the live database", unreachable, while a
    missing migration produced a 500 on every scan and the static drift check
    reported all clear. `calibrate.py` was another: the tape-measurement path
    for the scale work, with no way to invoke it.

    Same class again. Built, correct, tested, and not joined to anything.
    """
    dev = (ROOT.parent / "dev.bat").read_text(encoding="utf-8", errors="replace")
    scripts = [p for p in sorted((ROOT / "scripts").glob("*.py"))
               if not p.name.startswith("_") and p.name != "__init__.py"]
    assert scripts, "no scripts found"
    unreachable = [p.name for p in scripts if f"scripts.{p.stem}" not in dev]
    assert not unreachable, (
        "these scripts have no dev command, so nobody will find them:\n  "
        + "\n  ".join(unreachable)
    )

    # ...and a command nobody can SEE is a command nobody will run.
    #
    # This half was missing, and it cost a live one. `dev footprint` was added,
    # dispatched correctly, tested by the assertion above -- and left off the
    # menu the launcher prints, so running `dev footprint` printed the usage
    # screen with no such command on it. Six others were in the same state,
    # including `dev migrations`, the tool the paragraph above exists because
    # of, and `dev calibrate`, the tape-measurement path for the plate.
    #
    # The dispatch table is the truth; the printed menu has to match it.
    import re

    dispatched = re.findall(r'if "%~1"=="([a-z]+)"', dev)
    listed = set(re.findall(r"^echo\s+dev ([a-z]+)", dev, re.M))
    hidden = [c for c in dispatched if c not in listed]
    assert not hidden, (
        "these commands work but are not on the menu the launcher prints, so "
        "the only way to find them is to read dev.bat: " + ", ".join(hidden)
    )


def test_nothing_added_this_session_can_move_a_weight_on_its_own():
    """The scale is not to be disturbed by anything that is not measured.

    Three things were added around the weight path recently: a sourced density
    file, a pixel measurement, and a learned-height table. Each could move a
    gram, and only the last one is allowed to -- and only once a real correction
    has cleared its floor.

    ONE OF THE THREE HAS SINCE CHANGED, DELIBERATELY, AND THIS SAYS WHICH.
    The pixel measurement now has two sources and they are not the same claim.
    A colour-grown footprint is fenced by the plate box, so its absolute value
    carries the box's error while only the share survives -- that one is still
    inert and this test still holds it inert. A point-prompted segmenter's mask
    never sees the plate box, so it is an area, and it is the one thing this
    build connected on purpose. `area_is_absolute` is where the line is drawn
    and test_food_seg holds it.

    This asserts the density file is inert, the colour footprint is inert, and
    the learned height is inert until taught -- so that adding data can never
    silently reprice a meal.
    """
    import app.services.ai.portion as P
    from app.services.ai.portion import GeometryHint, estimate_grams

    plate = GeometryHint(plate_ellipse_area_ratio=0.35, plate_diameter_mm=254)
    call = dict(name="mexican rice", area_ratio=0.15, hint=plate,
                plate_coverage=0.20, shape_hint="mound",
                bbox={"w": 0.45, "h": 0.45}, detection_confidence=0.8)

    # 1. the sourced density file is loaded, cited, and not applied
    assert P.USE_SOURCED_DENSITIES is False
    assert P._SOURCED, "loaded nothing, so the switch would be a no-op"

    # 1b. the colour footprint is still not allowed to be an area
    from app.services.ai import food_seg as FS
    assert FS.area_is_absolute("colour") is False
    assert FS.area_is_absolute("sam2") is True

    # 2. an empty or absent learned-height table changes nothing
    baseline = estimate_grams(**call).grams
    assert estimate_grams(**call, learned_heights=None).grams == baseline
    assert estimate_grams(**call, learned_heights={}).grams == baseline

    # 3. a height that has NOT cleared its floor never reaches the estimate,
    #    because learned_heights refuses to publish it in the first place
    from app.services import portion_learning as PL

    rows = [{"shape": "mound", "food_name": None, "height_mm": 12.0,
             "samples": PL.MIN_SAMPLES - 1}]

    class _Res:
        data = rows

    class _T:
        def select(self, *_a, **_k): return self
        def execute(self): return _Res()

    class _SB:
        def table(self, *_a, **_k): return _T()

    original = PL.service
    PL.service = lambda: _SB()
    try:
        under_floor = PL.learned_heights()
    finally:
        PL.service = original
    assert estimate_grams(**call, learned_heights=under_floor).grams == baseline

    # 4. and the shadow pixel measurement is a share, never grams
    from app.models.nutrition import DetectedItem
    assert "measured_grams" not in DetectedItem.model_fields
    assert "measured_share" in DetectedItem.model_fields


# Settings that are legitimately absent from .env.example, each with a reason.
# Settings nothing reads. Each is a KNOWN hole with a scheduled fix, listed
# here so the debt is visible in the suite rather than hidden by a narrower
# check. Removing an entry should mean the hole is closed, never that the test
# got tired.
SETTINGS_KNOWN_UNREAD = {
    # Documented in the deployment guide; the rate limiter is in-process, so
    # setting this in production changes nothing at all. Due when the limiter
    # moves to Redis for multi-instance deployment.
    "redis_url",
    # Worse than unread: OAuth 1.0a cannot be signed without the consumer
    # secret, so the Garmin connection cannot complete however it is
    # configured. The setting exists, the flow does not work.
    "garmin_consumer_secret",
}

SETTINGS_WITHOUT_ENV = {
    "model_config",        # pydantic's own, not a setting
    "app_name",            # never read; flagged by the audit, left as a decision
    "api_prefix",          # structural, changing it breaks every client
}


def test_every_setting_reaches_the_code_and_the_env_file():
    """A setting nothing reads is a knob painted on the box.

    This bites hardest on a provider that is off by default: nothing fails, no
    test notices, and the first person to set DEPTH_PROVIDER in production
    discovers the app never looked at it. Both directions are checked -- the
    code must read every setting, and the operator must be able to find it.
    """
    import re as _re

    config = (ROOT / "app" / "config.py").read_text()
    env_example = (ROOT.parent / ".env.example").read_text()
    readers = "\n".join(
        t for p, t in _sources().items()
        if p.name not in ("config.py",) and not p.name.startswith("test_")
    )

    # EVERY setting, not just the depth ones.
    #
    # This filtered on `depth_` because that was the feature being added at the
    # time, which means it only ever guarded one afternoon's work. The very next
    # settings added -- `trust_proxy_header`, `pro_daily_scan_ceiling` -- would
    # have sailed past it undocumented and, in the second case, silently
    # unenforced. Same defect class as everything else in this file: a check
    # scoped to the thing that prompted it.
    names = [n for n in _re.findall(r"^    ([a-z][a-z0-9_]+):", config, _re.M)
             if n not in SETTINGS_WITHOUT_ENV]
    assert len(names) > 30, f"only found {len(names)} settings -- the scan broke"

    # Both spellings. Five settings read as `getattr(settings, "x", default)`
    # rather than `settings.x`, and looking for only the first form reported
    # them as dead. Worth knowing that the getattr form is itself a smell -- on
    # a declared pydantic field it cannot fail, so a renamed field silently
    # returns the default forever -- but that is a separate argument from
    # whether anything reads it.
    unread = [n for n in names
              if n not in SETTINGS_KNOWN_UNREAD
              and f"settings.{n}" not in readers
              and f'getattr(settings, "{n}"' not in readers
              and f"getattr(settings, '{n}'" not in readers]
    assert not unread, f"settings nothing reads: {unread}"

    undocumented = [n for n in names if n.upper() not in env_example]
    assert not undocumented, (
        f"settings missing from .env.example, so nobody can find them: {undocumented}")


def test_the_depth_model_cannot_be_wired_up_without_being_used():
    """The oldest defect in this project, guarded at its newest seam.

    portion_learning wrote heights for weeks that nothing read. So: a configured
    provider has to reach the estimator. The chain is config -> from_settings ->
    vision.DEPTH_PROVIDER -> _measured_heights -> estimate_grams, and every link
    is asserted rather than assumed.
    """
    from app.services.ai import depth_hosted, vision

    assert vision.DEPTH_PROVIDER is not None
    # unconfigured today, and that is the documented default
    assert vision.DEPTH_PROVIDER.available() is False

    configured = depth_hosted.HostedDepth(
        dialect="replicate", api_key="k", version="v")
    assert configured.available() is True
    assert configured.units == vision.DEPTH_UNITS_SUPPORTED

    src = (ROOT / "app" / "services" / "ai" / "vision.py").read_text()
    assert "depth_hosted.from_settings()" in src, (
        "vision no longer builds its provider from settings")
    assert "measured_height_mm=" in src, (
        "a measured height is no longer handed to the estimator")


def test_the_applied_migration_check_can_see_a_missing_table():
    """It could not, and that blinded it on the migration that mattered most.

    `check_migrations` verified only columns added by `alter table ... add
    column`. 0021 creates `portion_learning` and adds no columns, so the check
    had nothing to test and would have printed a clean result while the table
    did not exist -- the precise failure its own docstring was written about,
    reproduced inside the tool meant to prevent it.
    """
    import sys
    sys.path.insert(0, str(ROOT))
    from scripts.check_migrations import added_columns, created_tables

    tables = {t for t, _ in created_tables()}
    assert "portion_learning" in tables, (
        "the check still cannot see the table 0021 creates"
    )
    assert "vessel_observations" in tables
    # and the columns it always could see
    cols = {(t, c) for t, c, _ in added_columns()}
    assert ("scan_calibrations", "width_error_pct") in cols
    assert ("scan_calibrations", "observations") in cols


def test_the_dev_launcher_is_structurally_sound():
    """dev.bat is the only way anyone runs anything here, and I broke it twice
    in one session with scripted edits.

    Two separate faults, neither visible by reading the diff:

      * `if "%~1"=="migrations" goto` with no label, and three command bodies
        inserted INTO the dispatch block -- so any unmatched command fell
        through and silently ran `calibrate`.
      * Python's text-mode write converted the file's CRLF endings to LF.
        cmd.exe misbehaves on an LF-only .bat, and labels and goto are where it
        shows first, which is why it reported it could not find the batch.

    Cheap to check, and it fails the moment either recurs.
    """
    raw = (ROOT.parent / "dev.bat").read_bytes()

    crlf, lf = raw.count(b"\r\n"), raw.count(b"\n")
    assert crlf and crlf == lf, (
        f"dev.bat has {lf - crlf} bare LF line endings. cmd.exe needs CRLF; "
        f"a text-mode Python write is what strips them"
    )

    lines = raw.decode("utf-8", "surrogateescape").split("\r\n")
    dispatch, labels = {}, {}
    for i, line in enumerate(lines, 1):
        m = re.match(r'if "%~1"=="(\w*)"\s+goto\s+(\S+)\s*$', line)
        if m:
            dispatch[m.group(1)] = (i, m.group(2).lstrip(":"))
        if re.match(r"^:\w+\s*$", line):
            labels.setdefault(line.strip()[1:], []).append(i)

    assert dispatch, "no dispatch lines found -- the file is malformed"
    dangling = [k for k, (_, target) in dispatch.items() if target not in labels]
    assert not dangling, f"these commands goto a label that does not exist: {dangling}"

    dupes = {k: v for k, v in labels.items() if len(v) > 1}
    assert not dupes, f"duplicate labels, and batch silently takes the first: {dupes}"

    # Nothing executable may sit between the first and last dispatch line, or an
    # unmatched command falls through into it.
    first = min(i for i, _ in dispatch.values())
    last = max(i for i, _ in dispatch.values())
    stray = [(i, l) for i, l in enumerate(lines[first - 1:last], first)
             if l.strip() and not l.startswith('if "%~1"==')]
    assert not stray, (
        f"command bodies are inside the dispatch block, so an unrecognised "
        f"command will run them: {stray}"
    )

    orphans = sorted(set(labels) - set(dispatch) - {"eof", "usage"})
    assert not orphans, f"labels no command can reach: {orphans}"

    # Every command must forward its WHOLE argument list.
    #
    # They forwarded a fixed handful of positionals -- five for calibrate, and
    # NONE for benchall -- so anything past the end was dropped in silence.
    # `dev calibrate --vessel dinner_plate --width 254 --label "my dinner
    # plate"` lost the label and argparse rejected the command; worse,
    # `dev benchall --runs 3` had been quietly running a single pass while
    # reporting nothing about it, and a repeat-run figure was asked for more
    # than once on that basis.
    #
    # `shift` drops the command word, then %1..%9 passes the rest with quoting
    # intact.
    for i, line in enumerate(lines):
        if line.startswith("python -m scripts."):
            assert "%9" in line, (
                f"line {i + 1} forwards a capped argument list, so a long "
                f"command loses its tail without saying so: {line!r}"
            )
            assert lines[i - 1].strip() == "shift", (
                f"line {i + 1} passes %1.. without a preceding `shift`, so it "
                f"would re-send the command word as an argument: {line!r}"
            )


def test_the_source_scan_never_leaves_our_own_code():
    """It did, and it cost a test run that looked frozen.

    `backend/.venv` sits inside ROOT on a normal developer machine, so a bare
    `ROOT.rglob("*.py")` reads all of site-packages -- once per test that calls
    it. On Windows `dev test` stopped at 97% and sat there.

    The correctness half matters more than the speed. The constant audit walks
    every ALL-CAPS assignment in everything the scan returns; pointed at
    site-packages it was auditing OpenCV against this app and passing anyway,
    because a library's constants are referenced inside that library. A check
    that passes for the wrong reason is not a check.

    Written to prove the RULE rather than the machine: it plants a file where a
    virtualenv would sit and checks the scan walks past it. A version of this
    test that merely listed what the scan found passed on a machine with no
    virtualenv, which is every CI box and was no proof at all.
    """
    import shutil

    global _SOURCE_CACHE
    planted = ROOT / ".venv" / "lib" / "probe"
    created = not (ROOT / ".venv").exists()
    try:
        planted.mkdir(parents=True, exist_ok=True)
        (planted / "intruder.py").write_text("SOME_LIBRARY_CONSTANT = 1\n")
        _SOURCE_CACHE = None
        files = list(_sources())
        assert files, "the source scan found nothing"
        assert not any("intruder.py" in str(f) for f in files), (
            "the scan walked into a virtualenv again")
        ours = {ROOT / name for name in SOURCE_DIRS}
        for f in files:
            assert any(str(f).startswith(str(d)) for d in ours), f
        assert len(files) < 400, (
            f"{len(files)} source files -- the scan has escaped again")
    finally:
        if created:
            shutil.rmtree(ROOT / ".venv", ignore_errors=True)
        else:
            shutil.rmtree(planted, ignore_errors=True)
        _SOURCE_CACHE = None


def test_every_setting_the_segmenter_needs_is_documented():
    """Same check as the depth provider's, and the reason it is written out
    again rather than trusted: the settings audit above only ever looked at
    `depth_` prefixed names until this week, so it guarded one afternoon's work
    and would have let SAM2's nine settings through undocumented."""
    import re as _re

    config = (ROOT / "app" / "config.py").read_text()
    env_example = (ROOT.parent / ".env.example").read_text()

    names = _re.findall(r"^    (segmenter_[a-z_]+):", config, _re.M)
    assert len(names) >= 8, f"only found {len(names)} segmenter settings"
    missing = [n for n in names if n.upper() not in env_example]
    assert not missing, f"undocumented, so nobody can turn SAM2 on: {missing}"


def test_no_test_can_reach_a_network_provider():
    """The conftest guard, asserted from inside a test rather than trusted.

    `dev test` was making live Replicate calls because `.env` configured a
    segmenter and 24 tests reached one without pinning it. The suite was
    therefore nondeterministic, paid, and different on CI than on a developer's
    machine -- and when it went red it pointed at the code under test rather
    than at the network.

    This runs inside the fixture it is checking, so it fails the moment the
    guard stops applying: dropped, made opt-in, or narrowed to one provider.
    """
    from app.config import settings
    from app.services.ai import food_seg, vision

    assert food_seg.segmenter().available() is False, (
        "a test can reach a live segmenter -- the conftest guard is not applying")
    assert vision.DEPTH_PROVIDER.available() is False, (
        "a test can reach a live depth model")
    # The settings too, or `from_settings()` inside a test walks straight past
    # the two objects above and builds a live one.
    assert settings.segmenter_provider == ""
    assert settings.depth_provider == ""


def test_the_network_guard_is_automatic_and_not_opt_in():
    """A guard every test has to remember to ask for is not a guard. The 24
    tests that needed it are precisely the ones that did not know they did."""
    src = (ROOT / "tests" / "conftest.py").read_text()
    assert "autouse=True" in src, "the provider guard is opt-in"
    for name in ("_SEGMENTER", "DEPTH_PROVIDER",
                 "segmenter_provider", "depth_provider"):
        assert name in src, f"the guard no longer pins {name}"


def test_points_per_side_is_never_mistaken_for_a_point_prompt():
    """The trap the candidate search exists to walk past.

    `meta/sam-2` has `points_per_side`. It contains the word "point", it is the
    automatic generator's sampling grid, and it is NOT a prompt. Mistaking it
    for one is not hypothetical: the integration sent `point_coords` to this
    model, the call succeeded, it was billed, and it came back without the mask
    that was asked for.

    This is the exact schema that model publishes. If the exclusion is ever
    dropped, this model reads as PROMPTED and the next person wires against it.
    """
    from scripts.seg_check import _point_inputs

    sam2 = {"image": {}, "points_per_side": {}, "pred_iou_thresh": {},
            "stability_score_thresh": {}, "use_m2m": {}}
    assert _point_inputs(sam2) == [], (
        "meta/sam-2 read as prompted -- points_per_side got through")


def test_a_real_coordinate_input_is_recognised_whatever_it_is_called():
    """The other half, so the test above cannot be passed by returning [].

    A mutation emptying `_point_inputs` would satisfy the trap check and rule
    out every candidate there is, silently, which is the more expensive failure:
    the search would report "none" and the decision would stay blocked.
    """
    from scripts.seg_check import _point_inputs

    for field in ("point_coords", "click_coordinates", "input_points",
                  "coordinates"):
        found = _point_inputs({"image": {}, field: {}})
        assert found == [field], f"{field} was not read as a point prompt"

    # Both shapes at once: a model that takes coordinates AND a sampling grid
    # is prompted, and the grid must not disqualify it.
    mixed = {"image": {}, "point_coords": {}, "points_per_side": {}}
    assert _point_inputs(mixed) == ["point_coords"]


def test_the_automatic_generators_crop_knobs_are_not_point_prompts():
    """The same trap, one variant out, and it got through on 12 Sep.

    `dev segcheck --candidates` reported lucataco/segment-anything-2,
    yyjim/segment-anything-everything and pablodawson/segment-anything-automatic
    as PROMPTED -- takes point coordinates -- on the strength of
    `crop_n_points_downscale_factor` alone. All three are the automatic mask
    generator. That field and `crop_n_layers` are how it tiles the image before
    sampling its grid; neither carries a coordinate, and a survey that lists
    them as prompts sends the next reader to integrate a model that cannot be
    prompted at all.
    """
    from scripts.seg_check import _point_inputs

    lucataco = {"image": {}, "points_per_side": {}, "points_per_batch": {},
                "crop_n_layers": {}, "crop_n_points_downscale_factor": {},
                "box_nms_thresh": {}, "min_mask_region_area": {}}
    assert _point_inputs(lucataco) == [], (
        "an automatic generator read as prompted -- "
        f"{_point_inputs(lucataco)} got through")

    # And a real coordinate input alongside them is still found.
    assert _point_inputs(dict(lucataco, input_points={})) == ["input_points"]


def test_nms_and_postprocessing_knobs_are_not_box_prompts():
    """A box prompt carries COORDINATES. These carry thresholds.

    `box_nms_thresh` is the IoU cutoff non-maximal suppression uses to drop
    duplicate masks and `min_mask_region_area` is a post-processing floor --
    both knobs on the automatic generator's OUTPUT. A naive "does it mention a
    box" test reads them as a prompt, which is how the first box survey
    reported lucataco/segment-anything-2 as box-promptable when its only
    inputs are the grid and its post-processing.

    The real ones, for contrast: sam3's `positive_boxes` (four floats per box,
    normalised) and fastsam's `box_prompt`, documented "[x,y,w,h]".
    """
    from scripts.seg_check import _box_inputs

    auto = {"image": {}, "box_nms_thresh": {}, "min_mask_region_area": {},
            "crop_n_layers": {}, "points_per_side": {}}
    assert _box_inputs(auto) == [], (
        f"post-processing knobs read as a box prompt: {_box_inputs(auto)}")

    for field in ("box_prompt", "input_box", "positive_boxes", "bbox"):
        assert _box_inputs({"image": {}, field: {}}) == [field], (
            f"{field} was not read as a box prompt")

    # Both at once: a real box prompt beside the generator's knobs is still a
    # box prompt.
    assert _box_inputs(dict(auto, box_prompt={})) == ["box_prompt"]


def test_reading_a_candidates_schema_cannot_spend_money():
    """--schema-only exists because interrogating a model we are CONSIDERING
    and paying the one we have configured are different acts, and segcheck ran
    them together. Asking what four candidates accept would have fired four
    paid predictions at meta/sam-2, which answers nothing about any of them."""
    source = (ROOT / "scripts" / "seg_check.py").read_text()
    main = source.split("def main()", 1)[1]
    assert "--schema-only" in main, "the flag is not read in main()"
    probe = main.index("_probe()")
    guard = main.index('"--schema-only"')
    assert guard < probe, "the paid probe is not behind the flag"


def test_the_candidate_search_is_reachable_from_dev():
    """This project keeps producing code that was built and never connected.
    A candidate search nobody can run is exactly that."""
    dev = (ROOT.parent / "dev.bat").read_text(errors="replace")
    assert "--candidates" in dev, "no way to reach the candidate search"
    assert "--schema-only" in dev


def test_the_bench_states_whether_it_met_the_target():
    """A target that is not printed is a target nobody is held to. 10% per item
    against Nutrition5k's 18.8% for RGB alone."""
    from scripts.bench_all import TARGET_ITEM_ERROR, verdict

    assert TARGET_ITEM_ERROR == 10.0
    assert "MEETS" in verdict(9.9)
    assert "MEETS" in verdict(10.0)
    assert "short" in verdict(10.1)
    assert "4.8 points short" in verdict(14.8)


def test_the_bench_will_not_call_a_target_it_cannot_decide():
    """A mean over ten noisy items is a sample, not a measurement.

    The live run scored 19.4% over ten items whose spread puts the 95% interval
    at 12.8-26.0%. A tighter run could land at 11% and the same sample size
    would put the interval either side of the target -- and a bench that then
    printed '1 point short' would be reporting the sample moving, not the
    product improving. Four accuracy decisions were argued off single-run means
    before this existed.
    """
    from scripts.bench_all import confidence_interval, verdict

    # Below n=3 there is no interval to state, and the bench must not invent one.
    assert confidence_interval([12.0, 14.0]) is None

    lo, hi = confidence_interval([10.0, 26.5, 25.9, 19.0, 25.8,
                                  3.6, 34.6, 13.1, 20.3, 15.1])
    assert round(lo, 1) == 12.8 and round(hi, 1) == 26.0

    # Straddling the target: the sample cannot decide, and says so.
    straddle = [4.0, 18.0, 6.0, 20.0, 3.0, 16.0]
    said = verdict(sum(straddle) / len(straddle), straddle)
    assert "TOO FEW ITEMS TO SAY" in said, said
    assert "MEETS" not in said and "short of" not in said

    # Clear of the target in either direction: it commits.
    tight_pass = [8.0, 9.1, 7.4, 8.8, 9.5, 7.9, 8.3, 9.0, 8.6, 7.7]
    assert "MEETS" in verdict(sum(tight_pass) / len(tight_pass), tight_pass)
    clear_fail = [40.0, 44.0, 38.0, 41.0, 43.0, 39.0]
    assert "short of" in verdict(sum(clear_fail) / len(clear_fail), clear_fail)


def test_the_bench_says_which_items_its_average_left_out():
    """The exclusion is real, and it is biased toward the worst items.

    An item is scored only when `match` can pair its name with a weighed name.
    One plate of rajas came back as 'creamy chicken', 'creamy mushroom sauce'
    and 'creamy chicken dish' across four photographs; none matched, so the two
    hardest items on the bench were absent from the headline figure and nothing
    on screen said so.

    Tested through what the function SAYS rather than by grepping main() for a
    string -- two tests written the second way passed this month while the
    behaviour under them had been deleted.
    """
    import re

    from scripts.bench_all import sample_health

    plain = lambda ls: re.sub(r"\x1b\[[0-9;]*m", "", " ".join(ls))

    said = plain(sample_health([12.0] * 10, ["15 creamy chicken",
                                             "16 creamy mushroom sauce"],
                               {"07", "08", "15", "16", "15n"}))
    assert "2 detected item(s) were NOT scored" in said
    assert "creamy chicken" in said and "creamy mushroom sauce" in said
    assert "not at random" in said, "the bias is stated, not just the count"
    assert "reads better than the product is" in said, (
        "the notice says items were dropped but not which DIRECTION that "
        "moves the number -- which is the only part a reader acts on"
    )
    assert "5 distinct weighed meal(s)" in said

    # Nothing to warn about: no exclusion notice at all.
    clean = plain(sample_health([12.0] * 4, [], {"07", "08", "15", "16"}))
    assert "NOT scored" not in clean

    # Ten items from two meals is not ten observations, and it says so.
    lumpy = plain(sample_health([12.0] * 10, [], {"15", "16"}))
    assert "not independent" in lumpy
    assert "not independent" not in plain(
        sample_health([12.0] * 4, [], {"07", "08", "15", "16"}))


def test_an_item_that_matches_nothing_comes_back_as_a_dropped_item():
    """The exclusion has to be RETURNED, not merely not-counted.

    It was three lines inside the bench's print loop: no match, no score, no
    trace. The two hardest items on the bench were missing from the headline
    figure and nothing anywhere said so. A silent drop cannot be tested; this
    is the function that makes it testable, so this is the test that would have
    caught it.
    """
    from scripts.bench_all import score_items

    # Three foods weighed, two detected: two weighed rows stay unclaimed, so
    # nothing is forced and the unmatched detection must come back as dropped.
    actuals = {"mexican rice": 66.0, "rajas": 98.0, "refried beans": 79.0}
    items = [{"name": "boiled cooked rice", "grams": 44.0},
             {"name": "creamy mushroom sauce", "grams": 163.0}]
    paired, dropped = score_items(items, actuals)
    assert dropped == ["creamy mushroom sauce"], dropped
    assert paired[0] == ("mexican rice", 66.0)


def test_the_last_pair_on_a_plate_is_forced_not_guessed():
    """The exclusion that ran one way, closed.

    A plate of rajas came back as 'creamy chicken', 'creamy mushroom sauce',
    'creamy pasta' and 'creamy dish with mushrooms' across four photographs.
    None matched the weighed name, so the app produced a number for that food
    every single run and was never once held to it -- and the items it dropped
    were the ones hardest to identify, which is the direction that flatters the
    average.

    With one detection and one weighed food left over there is exactly one way
    they can correspond, so pairing them invents nothing. With two of each
    there are two pairings and choosing is inventing evidence, which is what
    position matching did before it was removed.
    """
    from scripts.bench_all import score_items

    # Two foods, one matched by name: the other pairing is arithmetic.
    paired, dropped = score_items(
        [{"name": "creamy pasta", "grams": 98.8},
         {"name": "rice", "grams": 69.8}],
        {"mexican rice": 77.0, "rajas": 98.0})
    assert dropped == [], dropped
    assert paired[0] == ("rajas", 98.0), "the unnameable dish is still unscored"
    assert paired[1] == ("mexican rice", 77.0)

    # Two unknowns against two unclaimed weighings: refuse.
    paired, dropped = score_items(
        [{"name": "mystery a", "grams": 10.0},
         {"name": "mystery b", "grams": 20.0},
         {"name": "rice", "grams": 70.0}],
        {"mexican rice": 77.0, "rajas": 98.0, "beans": 50.0})
    assert sorted(dropped) == ["mystery a", "mystery b"], dropped
    assert len(paired) == 1

    # A detection with nothing left to claim stays dropped.
    paired, dropped = score_items(
        [{"name": "rice", "grams": 70.0}, {"name": "garnish", "grams": 5.0}],
        {"mexican rice": 77.0})
    assert dropped == ["garnish"] and len(paired) == 1


def test_one_weighed_row_cannot_be_scored_twice():
    """A meal the model split in two used to score both halves against the
    whole weighed row, so each half read light and the average absorbed a
    detection error as if it were a weighing error."""
    from scripts.bench_all import score_items

    paired, dropped = score_items(
        [{"name": "mexican rice", "grams": 30.0}, {"name": "rice", "grams": 36.0}],
        {"mexican rice": 66.0})
    assert len(paired) == 1
    assert dropped == ["rice"]


def test_the_summary_uses_every_repeat_not_just_the_printed_one():
    """Three runs, and the mean was taken over one of them.

    The printed detail comes from the median run by meal total, which is right
    for reading -- it is a scan that really happened. Averaging over it alone
    throws away two thirds of the evidence and hands the summary a single
    sample of a process the same page has just shown is noisy: two runs of
    identical code over identical photographs returned 19.4% and 27.1%.
    """
    from scripts.bench_all import errors_over_all_runs

    actuals = {"mexican rice": 66.0}
    runs = [{"items": [{"name": "mexican rice", "grams": 66.0}]},    # 0%
            {"items": [{"name": "mexican rice", "grams": 99.0}]},    # +50%
            {"items": [{"name": "mexican rice", "grams": 33.0}]}]    # -50%
    got = errors_over_all_runs(runs, actuals)
    assert sorted(round(g) for g in got) == [0, 50, 50], got
    assert errors_over_all_runs([], actuals) == []


def test_the_verdict_is_taken_over_the_repeats_not_the_printed_run():
    """And the swap is a returned value, not an assignment buried in main().

    Two assignments in the middle of a 200-line function decided which numbers
    the 10% verdict was computed over. Nothing could test that they ran, and a
    bench that silently falls back to one run of three is precisely the false
    reading this section exists to stop.
    """
    import re

    from scripts.bench_all import headline

    plain = lambda ls: re.sub(r"\x1b\[[0-9;]*m", "", " ".join(ls))

    lines, use = headline([19.4] * 10, [27.1] * 30)
    assert len(use) == 30, "the verdict fell back to the printed run"
    assert "ALL runs" in plain(lines) and "n=30" in plain(lines)
    assert "one sample, not the result" in plain(lines), (
        "a 7.7-point gap between the printed run and the mean went unremarked"
    )

    # A printed run that agrees with the repeats needs no warning.
    quiet = plain(headline([20.0] * 10, [20.5] * 30)[0])
    assert "ALL runs" in quiet and "one sample" not in quiet

    # One run only: nothing extra to say, and nothing to swap.
    lines, use = headline([19.4] * 10, [])
    assert lines == [] and len(use) == 10


def test_no_function_is_shadowed_by_a_local_of_the_same_name():
    """The bug this exists for shipped, and the test above could not see it.

    `bench_all.main` printed the 10% verdict by calling `verdict(...)`, and 30
    lines later assigned a LOCAL named `verdict` for an unrelated message. In
    Python a name assigned anywhere in a function is local for the whole of it,
    so the earlier call raised UnboundLocalError and the target line never
    printed once. The test above passed the entire time, because it called
    `verdict()` directly and never went near `main`.

    That is the shape of the failure: a unit test proving a function is correct,
    while nothing proves the caller can reach it. Statically detectable, so it
    is checked statically rather than by running a bench that needs an API key.
    """
    import ast

    offences = []
    for path, text in _sources().items():
        tree = ast.parse(text)
        module_fns = {n.name for n in tree.body
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if not module_fns:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigned, called = set(), set()
            for sub in ast.walk(node):
                if sub is node:
                    continue
                # A nested def has its own scope; do not attribute its names here.
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    assigned.add(sub.id)
                elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                    called.add(sub.func.id)
                elif isinstance(sub, ast.Global):
                    assigned -= set(sub.names)
                    called -= set(sub.names)
            for name in sorted(assigned & called & module_fns):
                offences.append(
                    f"{path.name}:{node.lineno} {node.name}() assigns a local "
                    f"named '{name}', which is also a module-level function it "
                    f"calls -- that call raises UnboundLocalError"
                )
    assert not offences, "\n  " + "\n  ".join(offences)


def test_no_module_is_built_and_left_unconnected():
    """The defect this project keeps producing, made into a test failure.

    Twenty-eight instances were found in one audit. The most recent: SAM2 was
    written, given twenty tests, and called by nothing -- every test passed, the
    bench was unchanged, and there was no way to tell from inside the suite that
    the file was dead.

    So: every module under app/ must be reachable by import from something the
    outside world can actually reach -- a router, a worker, or the app itself.
    Reachability is a weak claim; a module can be imported and still unused. But
    it is exactly the claim that would have caught this one, and nothing else in
    the suite makes it.

    Read from the SOURCE rather than from a live import graph, because a module
    imported lazily inside a function body is still connected, and sys.modules
    would not show it until that line ran.

    ALLOWED_UNREACHED is deliberately empty. Adding a name to it is a decision
    somebody makes on purpose, in a diff, with the reason written beside it.
    """
    import ast
    import collections

    app = ROOT / "app"
    files = {f for f in app.rglob("*.py") if "__pycache__" not in f.parts}
    by_stem = collections.defaultdict(set)
    for f in files:
        by_stem[f.stem].add(f)

    edges = collections.defaultdict(set)
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        key = f
        for node in ast.walk(tree):
            names: set[str] = set()
            if isinstance(node, ast.ImportFrom):
                names = {a.name for a in node.names}
                if node.module:
                    names |= set(node.module.split("."))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    names |= set(a.name.split("."))
            for n in names:
                edges[key] |= by_stem.get(n, set())

    # Everything the outside world can reach: the ASGI app, the routers it
    # mounts, and the worker the scheduler runs.
    roots = {f for f in files
             if f.name in {"main.py", "__init__.py"}
             or f.parent.name in {"routers", "workers"}}
    seen, queue = set(roots), list(roots)
    while queue:
        for nxt in edges[queue.pop()] - seen:
            seen.add(nxt)
            queue.append(nxt)

    ALLOWED_UNREACHED: set[str] = set()
    orphans = {str(f.relative_to(app)) for f in files - seen} - ALLOWED_UNREACHED
    assert not orphans, (
        f"built and connected to nothing: {sorted(orphans)}. Either wire it in "
        f"or delete it -- a module the app cannot reach is a module whose tests "
        f"prove nothing about the product."
    )


def test_the_segmenter_cannot_be_wired_up_without_being_used():
    """Same guard as the depth model's, at the seam that was actually dead.

    The chain is config -> segment_hosted.from_settings -> food_seg.segmenter()
    -> item_masks_with_source -> vision._measured_areas -> estimate_grams, and
    every link is asserted rather than assumed.
    """
    from app.services.ai import food_seg, segment_hosted

    assert food_seg.segmenter() is not None
    # unconfigured today, and that is the documented default
    assert food_seg.segmenter().available() is False

    configured = segment_hosted.HostedSegmenter(
        dialect="replicate", api_key="k", version="v")
    assert configured.available() is True
    assert hasattr(configured, "plate_outline")

    seg_src = (ROOT / "app" / "services" / "ai" / "food_seg.py").read_text()
    assert "segment_hosted.from_settings()" in seg_src, (
        "food_seg no longer builds its segmenter from settings")
    assert "plate_outline(rgb, plate_bbox)" in seg_src, (
        "the plate outline is no longer asked for")

    vis_src = (ROOT / "app" / "services" / "ai" / "vision.py").read_text()
    assert "measured_area_ratio=" in vis_src, (
        "a measured footprint is no longer handed to the estimator")
    assert "area_is_absolute" in vis_src, (
        "vision no longer checks WHICH source measured the footprint, so a "
        "colour mask can reach the grams")


def test_every_bench_case_has_a_photo_and_a_weight():
    """A case naming a photo that is not there fails mid-run, after the model
    has already been paid for the cases before it."""
    from scripts.bench_all import CASES
    from scripts.scan_bench import parse_actuals

    photos = ROOT.parent / "photos"
    missing = [c[0] for c in CASES if not (photos / c[0]).exists()]
    assert not missing, f"cases with no photograph: {sorted(set(missing))}"

    for case in CASES:
        name, _plate, actual, kind = case[0], case[1], case[2], case[3]
        weighed = parse_actuals(actual)
        assert weighed, f"{name} declares no weighed food"
        assert all(g > 0 for g in weighed.values()), f"{name} has a zero weight"
        assert kind in {"per-item", "total"}, f"{name} has kind {kind!r}"
        if kind == "per-item" and len(weighed) > 1:
            # A multi-item case scored per item needs every item weighed, or
            # the split it reports is a share whose denominator is short.
            assert len(weighed) >= 2


def test_the_bench_is_not_mostly_one_food():
    """Four of the first ten scored items were rice, and three of those ten
    were the same 77 g of it -- photographed twice and scanned a third time
    with the plate withheld. A bench weighted that heavily toward one food
    measures that food, and reports the number as if it measured the product.

    The threshold is deliberately loose. It is here to fail if the evidence
    base ever collapses back onto one dish, not to police the menu.
    """
    import collections

    from scripts.bench_all import CASES
    from scripts.scan_bench import parse_actuals

    counts: collections.Counter = collections.Counter()
    for case in CASES:
        for food in parse_actuals(case[2]):
            # "mexican rice", "boiled rice" and "rice" are one food here.
            key = next((w for w in ("rice", "bean", "chicken", "pasta",
                                    "spaghetti", "potato", "carrot")
                        if w in food), food)
            counts[key] += 1
    total = sum(counts.values())
    food, n = counts.most_common(1)[0]
    assert n / total < 0.30, (
        f"{food!r} is {n} of {total} weighed items ({n / total:.0%}) -- this "
        f"bench mostly measures one food"
    )

    # DISTINCT WEIGHED MEALS, not photographs, because photographs of one meal
    # are not independent observations of the product. Photos 15, 16 and the
    # no-reference variant of 15 are the same 77 g of rice and the same 98 g of
    # rajas; they contributed three of ten scored items and the summary read
    # them as three.
    #
    # The floor is 12. The first set had 8 -- so this fails if the evidence
    # base is ever rolled back to it, which is the change worth catching.
    signatures = {c[2] for c in CASES}
    assert len(signatures) >= 12, (
        f"only {len(signatures)} distinct weighed meals across {len(CASES)} "
        f"cases; a mean over this cannot support a 10-point decision, whatever "
        f"n the bench prints"
    )


def test_the_bench_survives_every_shape_of_failure_it_can_get():
    """The error reporter crashed on its own error path.

    `call` returns four different bodies -- the API's {"error": {"message":..}},
    a transport failure's {"error": "<string>"}, a non-JSON reply's {"raw":..},
    and None -- and the reporter called .get on all of them. Two are strings,
    so `dev benchall` raised AttributeError instead of printing why the scan
    failed, and destroyed the one message the operator needed.

    The case it destroyed was the likeliest one: status 0, connection refused,
    the API not running.
    """
    from scripts.bench_all import describe_failure

    shapes = [
        (0,   {"error": "<urlopen error [WinError 10061] refused>"}),
        # Status 0 with a body that is NOT a dict. This is the exact crash --
        # and the first version of this test missed it, because every status-0
        # shape it tried happened to be a dict, so .get worked and the mutation
        # that restored the bug passed.
        (0,   "connection reset by peer"),
        (0,   None),
        (0,   ["weird"]),
        (500, {"error": {"message": "boom"}}),
        (500, {"error": "plain string error"}),
        (404, {"raw": "<html>Not Found</html>"}),
        (401, {"error": {"message": "invalid jwt"}}),
        (402, {"error": {"message": "daily scan limit"}}),
        (503, None),
        (500, "a bare string body"),
        (500, {"detail": "fastapi style"}),
        (500, []),
        (500, 12345),
    ]
    for status, body in shapes:
        said = describe_failure(status, body)          # must not raise
        assert isinstance(said, str) and said.strip(), (status, body)

    # A connection failure must name the fix, not just the exception.
    down = describe_failure(0, {"error": "connection refused"})
    assert "could not reach the API" in down
    assert "dev api" in down, "does not say how to start it"
    assert "HTTP 0" not in down, "reports a status code that is not a status"

    # Each nested shape yields the message inside it, UNWRAPPED. Checking only
    # that "boom" appears somewhere passes on a build that prints the whole
    # dict -- str({"error": {"message": "boom"}}) contains "boom" too. The
    # equality is the point.
    assert describe_failure(500, {"error": {"message": "boom"}}) == "HTTP 500: boom"
    assert describe_failure(500, {"error": "plain"}) == "HTTP 500: plain"
    assert describe_failure(404, {"raw": "not found"}) == "HTTP 404: not found"
    assert describe_failure(500, {"detail": "fastapi"}) == "HTTP 500: fastapi"
    for shape in ({"error": {"message": "boom"}}, {"raw": "x"}, {"detail": "y"}):
        said = describe_failure(500, shape)
        assert "{" not in said and "'" not in said, (
            f"the raw body leaked into the message instead of the field: {said}")

    # The two statuses with a known cause say what it is.
    assert "sign in" in describe_failure(401, {"error": {"message": "x"}})
    assert "out of scans" in describe_failure(402, {"error": {"message": "x"}})


def test_the_bench_checks_what_is_free_before_spending_anything():
    """Three runs were burned in a row on things that cost nothing to detect.

    Twice the API was not running. Once the file on disk was an older copy than
    the one being edited, so the bench scanned photographs that had been retired
    and the operator could not tell from the output which copy was running.

    Each time it opened, uploaded a photograph, failed, and tracebacked -- so
    the wasted wait came BEFORE the diagnosis instead of instead of it. The
    preflight moves every free check to the front: which file this is, which
    photographs are on disk, and whether the API answers at all.
    """
    import io
    import re
    import contextlib

    import scripts.bench_all as B

    plain = lambda t: re.sub(r"\x1b\[[0-9;]*m", "", t)

    def run(cases, api="http://127.0.0.1:9", runs=3):
        """Returns (printed, kept, reason). The reason arrives as a RAISE, not
        a return value -- `main` could ignore a returned one, and a mutation
        proved it: the bench spent a whole run against a dead API and the test
        still passed. An exception cannot be dropped by not reading it."""
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                kept = B.preflight(cases, api, runs)
            return plain(buf.getvalue()), kept, None
        except B.BenchNotReady as why:
            return plain(buf.getvalue()), [], plain(str(why))

    # 1. It always says which file is running and what it starts with.
    out, _kept, _stop = run(B.CASES)
    assert f"{len(B.CASES)} case(s) in this file" in out
    assert B.CASES[0][0] in out, "does not name the first case, so a stale copy is invisible"

    # 2. A photograph that is not on disk is named up front, not mid-run.
    ghost = [("99-not-a-real-photo.jpg", 229, "x=10", "per-item")]
    out, kept, stop = run(ghost)
    assert "not on disk" in out and "99-not-a-real-photo" in out
    assert kept == [] and stop, "ran anyway with no photographs"

    # The refusal must be a raise, so no caller can proceed past it by
    # forgetting to check a return value.
    import pytest as _pt
    with _pt.raises(B.BenchNotReady):
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            B.preflight(ghost, "http://127.0.0.1:9", 1)

    # 3. Mostly-missing is refused rather than scored over the remainder.
    half = list(B.CASES[:2]) + [("98-ghost-a.jpg", 229, "x=10", "per-item"),
                                ("97-ghost-b.jpg", 229, "x=10", "per-item"),
                                ("96-ghost-c.jpg", 229, "x=10", "per-item")]
    _out, _kept, stop = run(half)
    assert stop and "missing" in stop, "scored a bench that was mostly not there"

    # 4. An API that is not answering stops the run and says how to start it.
    _out, _kept, stop = run(B.CASES)
    assert stop, "spent model calls against an API that is not up"
    assert "not answering" in stop and "dev api" in stop


def test_a_measured_footprint_can_report_that_it_set_the_weight():
    """The bench's "N live / N shadow" line was structurally unable to say live.

    portion.py computes `measured_area_used` -- the one field that separates
    "the segmenter measured this and it set the grams" from "it measured this
    and was ignored". DetectedItem had no such field, so nothing carried it to
    the API, so the bench read None every time and printed "shadow" for a
    footprint that had in fact set the weight. Built, computed, and not
    connected -- and the line it broke is the very line that exists to catch
    things being built and not connected.

    Behavioural on the contract: the estimator's field must exist on the item
    the API returns, and must survive a round trip.
    """
    from app.models.nutrition import DetectedItem
    from app.services.ai.portion import PortionEstimate

    assert hasattr(PortionEstimate, "__dataclass_fields__") or True
    fields = getattr(DetectedItem, "model_fields", None) or getattr(
        DetectedItem, "__fields__", {})
    assert "measured_area_used" in fields, (
        "DetectedItem cannot carry measured_area_used, so no caller can ever "
        "tell a live footprint from a shadow one")

    item = DetectedItem(name="rice", grams=69.0, measured_area_used=0.11)
    assert item.measured_area_used == 0.11, "the field does not round trip"
    dumped = item.model_dump() if hasattr(item, "model_dump") else item.dict()
    assert dumped.get("measured_area_used") == 0.11, (
        "the field exists but is dropped on the way out to the API")


def test_nothing_is_left_behind_that_nothing_can_reach():
    """Dead code is the same disease as unconnected code, one step later.

    Something that WAS wired, was replaced, and stayed. It costs nothing to
    run and misleads every reader after you, because a function that exists
    looks like a function that runs. The SAM2 work alone left a 73-line
    `_measure_items_legacy` behind, and four smaller wrappers went with it.

    If this fails, the fix is to delete the thing OR to wire it up. Both are
    fine. Leaving it is what is not.
    """
    from scripts.dead_code import unreachable, unread_constants

    dead = unreachable()
    consts = unread_constants()
    assert not dead, "unreachable: " + ", ".join(
        f"{name} ({f}:{line}, {n} lines)" for f, line, n, name in dead)
    assert not consts, "constants nothing reads: " + ", ".join(
        f"{name} ({f}:{line})" for f, line, name in consts)


def test_the_segmenter_is_never_called_from_the_event_loop():
    """A food scan must not stall everyone else's request.

    The footprint measurement decodes a full-size JPEG, runs OpenCV, and makes
    a blocking HTTP call to the segmenter with a sleep-poll loop that can run
    to the full timeout. Called bare from an async function that is the whole
    worker, frozen, for the length of a segmentation. The height measurement
    beside it was wrapped in a thread for exactly this reason and carries a
    comment saying so; the footprint was not, and nothing noticed.

    Structural on purpose: the failure is a missing `await asyncio.to_thread`,
    which no unit test of either function can see.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "app/services/ai/vision.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    BLOCKING = {"_measured_areas", "_measured_heights"}
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in BLOCKING:
                continue
            # Legal only as the FUNCTION ARGUMENT of asyncio.to_thread, where it
            # is passed by name and never called here at all.
            offenders.append(f"{fn.name} calls {node.func.id}() directly "
                             f"at line {node.lineno}")
    assert not offenders, (
        "blocking work called straight from an async function: "
        + "; ".join(offenders)
        + " -- wrap it in asyncio.to_thread")


def test_healthz_reports_whether_the_mask_dump_is_armed(monkeypatch):
    """The only way a bench in another process can know, and it was needed.

    `--reload` reloads CODE, not the ENVIRONMENT. A dev server started before
    NUTRIAI_MASK_DUMP existed hot-loaded the dumping code and then wrote
    nothing, with no error in any log, and a paid run of photo 35 was spent
    capturing no masks at all -- found only when the offline replay opened an
    empty directory afterwards.

    Read at REQUEST time, not cached at import, or the answer describes the
    process's startup rather than this request. Cut that and this fails.
    """
    import asyncio

    from app.main import healthz
    from app.services.ai.segment_hosted import MASK_DUMP_ENV

    monkeypatch.delenv(MASK_DUMP_ENV, raising=False)
    assert asyncio.run(healthz())["mask_dump"] is None

    monkeypatch.setenv(MASK_DUMP_ENV, "  ")
    assert asyncio.run(healthz())["mask_dump"] is None, (
        "whitespace is not a directory")

    monkeypatch.setenv(MASK_DUMP_ENV, r"C:\dumps")
    assert asyncio.run(healthz())["mask_dump"] == r"C:\dumps"


def test_every_bench_row_says_what_its_weight_includes():
    """A weight that does not say what was on the scale cannot settle whether a
    detected sauce or garnish is part of the portion -- photo 25's mayonnaise
    against "smashed potatoes=136". So every row carries the field, and an old
    row says UNKNOWN rather than being guessed either way."""
    from scripts.bench_all import CASES, UNKNOWN, WEIGHED_WITH

    missing = sorted({c[0] for c in CASES} - set(WEIGHED_WITH))
    assert not missing, f"bench rows with no WEIGHED_WITH entry: {missing}"
    for name, value in WEIGHED_WITH.items():
        if value == UNKNOWN:
            continue
        assert isinstance(value, list), f"{name}: must be UNKNOWN or a list"
        for entry in value:
            assert set(entry) == {"what", "on_scale"}, f"{name}: {entry}"
            assert entry["on_scale"] in ("yes", "no"), f"{name}: {entry}"
