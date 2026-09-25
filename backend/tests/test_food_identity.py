"""What the user says a food actually is, kept for next time.

The name is not a label. One plate of rajas, photographed twice a minute
apart, came back as "creamy chicken" and then "creamy mushroom sauce" -- 315 g
against 186 g and 40% apart on energy, on geometry that was within 12% both
times. The name picks the density, the height prior and the nutrition lookup.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import food_identity as FI

RAJAS = [{"described_as": "creamy mushroom sauce", "actual_name": "rajas con crema", "samples": 3},
         {"described_as": "poblano chiles in cream", "actual_name": "rajas con crema", "samples": 3},
         {"described_as": "red rice", "actual_name": "mexican rice", "samples": 5}]


@pytest.mark.parametrize("described", [
    "creamy mushroom sauce",          # the exact words, again
    "creamy dish with mushrooms",     # the same dish, worded differently
    "poblano chile in cream sauce",   # singular where the stored one was plural
    "cooked red rice",
])
def test_the_same_dish_described_differently_still_matches(described):
    """The model does not use the same words twice. Across six runs of one
    plate it produced "creamy mushroom sauce", "creamy dish with mushrooms"
    and "creamy chicken with mushrooms" for one dish."""
    assert FI.suggest(described, RAJAS) is not None, described


@pytest.mark.parametrize("described", [
    "mushroom soup",                  # shares a word, is a different meal
    "creamy chicken with mushrooms",  # names a protein that is not in it
    "creamy chicken",
    "white rice",
    "rice",
    "poblano peppers",
    "chiles rellenos",
    "",
])
def test_a_different_food_does_not_borrow_the_name(described):
    """A false match puts someone else's dinner in this meal; a miss just asks
    the question again. So this errs hard toward missing.

    "mushroom soup" is the case that broke an earlier version: it accepted any
    shared word of eight letters or more, and "mushroom" is eight letters."""
    assert FI.suggest(described, RAJAS) is None, described


def test_plurals_fold_but_not_at_the_cost_of_the_word():
    """Stripping "es" from everything turns "chiles" into "chil" while "chile"
    stays "chile", and the same dish written two ways stops matching itself."""
    for singular, plural in (("chile", "chiles"), ("mushroom", "mushrooms"),
                             ("tomato", "tomatoes"), ("potato", "potatoes"),
                             ("bean", "beans"), ("pepper", "peppers")):
        assert FI.tokens(singular) == FI.tokens(plural), (singular, plural)


def test_the_words_a_model_hedges_with_carry_no_identity():
    """"creamy", "dish", "sauce" are what gets written when a dish has not been
    recognised. Matching on them would join a creamy soup to a creamy curry."""
    assert FI.tokens("creamy dish with sauce") == frozenset()
    assert FI.tokens("some unidentified food") == frozenset()


def test_one_correction_cannot_override_a_confident_identification():
    """The guard that still bounds this feature. The model's confidence no
    longer shields a named dish (Gil, 25 Sep 2026: a confident "scallops" kept
    coming back over his teriyaki beef), but a SINGLE correction is one opinion
    about one plate: it is offered, never applied, over a confident name."""
    once = [{"described_as": "scallops", "actual_name": "teriyaki beef", "samples": 1}]
    det = {"name": "scallops", "identification": "named"}
    note = FI.apply_to(det, once)
    assert det["name"] == "scallops", "a confident name overridden on one answer"
    assert det["identification"] == "named"
    assert "identified_by" not in det
    assert "once" in note and "teriyaki beef" in note


@pytest.mark.parametrize("identification", ["named", None])
def test_a_twice_confirmed_correction_overrides_a_confident_identification(identification):
    """The same answer twice is a fact, whatever the model was sure of. A
    missing `identification` reads as named (vision._identification_state), so
    it is covered too."""
    det = {"name": "creamy dish with mushrooms"}
    if identification:
        det["identification"] = identification
    note = FI.apply_to(det, RAJAS)                    # samples: 3
    assert det["name"] == "rajas con crema"
    assert det["identification"] == "named"
    assert det["identified_by"] == "user"
    assert "rajas con crema" in note and "3 times" in note


def test_the_threshold_is_exactly_min_samples_to_apply():
    below = [{"described_as": "scallops", "actual_name": "teriyaki beef",
              "samples": FI.MIN_SAMPLES_TO_APPLY - 1}]
    at = [{**below[0], "samples": FI.MIN_SAMPLES_TO_APPLY}]
    det = {"name": "scallops", "identification": "named"}
    FI.apply_to(det, below)
    assert det["name"] == "scallops"
    FI.apply_to(det, at)
    assert det["name"] == "teriyaki beef"


def test_a_named_dish_with_nothing_learned_is_untouched():
    """Removing the confidence gate must not make a confident name pick up an
    alias for a different food: matching is as conservative as it was."""
    det = {"name": "mushroom soup", "identification": "named"}
    assert FI.apply_to(det, RAJAS) is None
    assert det == {"name": "mushroom soup", "identification": "named"}


def test_a_described_dish_takes_the_name_the_person_gave_it():
    det = {"name": "creamy dish with mushrooms", "identification": "described"}
    note = FI.apply_to(det, RAJAS)
    assert det["name"] == "rajas con crema"
    assert det["identification"] == "named"
    assert det["identified_by"] == "user"
    assert "rajas con crema" in note


def test_one_answer_is_an_offer_and_two_is_a_fact():
    """A single correction is one person's opinion about one plate -- the same
    rule portion_learning applies to heights. It is surfaced, not applied."""
    once = [{"described_as": "creamy mushroom sauce", "actual_name": "rajas", "samples": 1}]
    det = {"name": "creamy mushroom sauce", "identification": "described"}
    note = FI.apply_to(det, once)
    assert det["name"] == "creamy mushroom sauce", "applied on a single answer"
    assert "once" in note and "rajas" in note


def test_nothing_learned_means_nothing_changes():
    det = {"name": "creamy mushroom sauce", "identification": "described"}
    assert FI.apply_to(det, []) is None
    assert FI.apply_to(det, None) is None
    assert det["name"] == "creamy mushroom sauce"


def test_junk_in_never_raises():
    for det in (None, "string", 42, {}, {"name": None}, {"name": "x"}):
        FI.apply_to(det, RAJAS)
    for bad in ([{"nope": 1}], [None], ["string"], [{"described_as": None}]):
        assert FI.suggest("creamy mushroom sauce", bad) is None


def test_a_rename_reaches_the_estimator_rather_than_stopping_here():
    """portion_learning wrote heights for weeks that nothing read. The same
    shape of mistake here would be a table of the user's own food names that
    never changes a single meal."""
    import inspect
    from app.services.ai import vision
    src = inspect.getsource(vision)
    assert "food_identity.aliases_for" in src, "the aliases are never loaded"
    assert "food_identity.apply_to" in src, "the aliases are never applied"
    assert "user_id=user_id" in src, "build_items is never told whose aliases to load"

    from app.routers import scans
    assert "food_identity.learn_from_correction" in inspect.getsource(scans), (
        "a correction never records what the food actually was")


# --- the pairing is carried, never guessed ------------------------------------

def _det(name, grams=100.0):
    return {"name": name, "grams": grams,
            "estimation_method": "plate_reference", "shape": "flat"}


class _Corrected:
    def __init__(self, name, grams, source_index=None):
        self.name, self.grams, self.source_index = name, grams, source_index


def test_a_rename_teaches_only_when_the_client_says_what_it_renamed():
    """A correction cannot be matched back by name, because the name is what
    changed. Position was the obvious fallback and it is wrong: replacing rice
    with beans and renaming rice to mexican rice look identical from outside,
    and only one says anything about the food.

    So the client carries the pairing. Without it, a rename is indistinguishable
    from an item the user added, and an addition must teach nothing.
    """
    from app.services import portion_learning as PL

    was = [_det("creamy mushroom sauce", 113.0)]
    assert PL.implied_heights(was, [_Corrected("rajas", 98.0, source_index=0)])
    assert PL.implied_heights(was, [_Corrected("rajas", 98.0)]) == {}


def test_a_correction_records_what_the_food_actually_was(monkeypatch):
    seen = []
    monkeypatch.setattr(FI, "remember", lambda u, d, a, **_k: seen.append((u, d, a)))
    FI.learn_from_correction("u1", [_det("creamy mushroom sauce"), _det("rice")], [
        _Corrected("rajas con crema", 98.0, source_index=0),
        _Corrected("rice", 77.0, source_index=1),          # unchanged
        _Corrected("tortilla", 30.0),                       # added by the user
    ])
    assert seen == [("u1", "creamy mushroom sauce", "rajas con crema")]


def test_a_rename_is_learned_against_the_cameras_word_not_the_databases(monkeypatch):
    """meal_items.name is the nutrition database's row name. Learned under it,
    "scallops, grilled" shares one identity word with the next scan's
    "scallops" and never matches again, so the alias was kept and never used.
    The scan now records what the model said (0030) and that is the key."""
    seen = []
    monkeypatch.setattr(FI, "remember", lambda u, d, a, **_k: seen.append((u, d, a)))
    was = [{**_det("scallops, cooked"), "detected_name": "scallops"}]
    FI.learn_from_correction("u1", was, [_Corrected("beef", 140.0, source_index=0)])
    assert seen == [("u1", "scallops", "beef")]
    # ...and that key is one the next scan's model output actually finds.
    assert FI.looks_like("scallops", "scallops")
    assert not FI.looks_like("scallops, grilled", "scallops")


def test_a_row_without_a_recorded_camera_word_still_learns_from_its_name(monkeypatch):
    """Rows from before 0030 have detected_name null (or no key at all)."""
    seen = []
    monkeypatch.setattr(FI, "remember", lambda u, d, a, **_k: seen.append((u, d, a)))
    for blank in (None, "", "   "):
        FI.learn_from_correction(
            "u1", [{**_det("scallops, cooked"), "detected_name": blank}],
            [_Corrected("beef", 140.0, source_index=0)])
    assert seen == [("u1", "scallops, cooked", "beef")] * 3


def test_the_cameras_word_is_stored_and_read_back_for_learning():
    """Write and read, both halves: a column the scan writes and the
    correction never selects is the portion_learning failure again."""
    import inspect
    from app.routers import scans
    from app.services.ai import vision
    assert '"detected_name": it.detected_name' in inspect.getsource(vision)
    assert "detected_name=" in inspect.getsource(vision)
    assert "detected_name" in inspect.getsource(scans.correct_meal)


def test_learning_never_fails_a_users_edit(monkeypatch):
    """Saving the correction is the user's action. Learning from it is ours,
    and ours must not be able to lose theirs."""
    def boom(*a, **k):
        raise RuntimeError("database on fire")
    monkeypatch.setattr(FI, "remember", boom)
    FI.learn_from_correction("u1", [_det("x")], [_Corrected("y", 1.0, source_index=0)])


def test_the_rename_happens_before_the_nutrition_is_looked_up():
    """The whole point, and the first version got it backwards.

    `build_items` resolves nutrition from the detected names near the top, then
    estimates grams, then builds each item. Applying a stored alias down in that
    item loop -- which is where this shipped first -- renames the food AFTER its
    calories, its density and its weight have all been decided from the name the
    camera guessed. The label changes and nothing else does, which is precisely
    the failure the feature exists to remove.

    Pinned by source order rather than by behaviour because the two calls are
    forty lines and one await apart, and a future edit that moves either one is
    exactly the edit that would silently undo this.
    """
    import inspect

    from app.services.ai import vision

    src = inspect.getsource(vision.build_items)
    rename = src.index("food_identity.apply_to")
    lookup = src.index("resolver.resolve_many")
    assert rename < lookup, (
        "the alias is applied after the nutrition lookup, so a renamed food "
        "still gets the calories of whatever the camera guessed")


def test_a_weighed_correction_is_tagged_as_such(monkeypatch):
    """Tester corrections carry source="weighed" through to remember(); every
    other caller still gets the default "typed". The tag changes no learning."""
    seen = []
    monkeypatch.setattr(FI, "remember", lambda u, d, a, **k: seen.append(k.get("source")))
    FI.learn_from_correction("u1", [_det("x")], [_Corrected("y", 1.0, source_index=0)],
                             source="weighed")
    FI.learn_from_correction("u1", [_det("x")], [_Corrected("y", 1.0, source_index=0)])
    assert seen == ["weighed", "typed"]


def test_a_grams_only_edit_teaches_nothing_even_if_the_camera_word_differs(monkeypatch):
    """meal_items.name is the resolved database name, not the camera's raw
    word, and the two routinely differ in wording ("Scallops, grilled" vs the
    model's own "Scallops") for reasons that have nothing to do with a
    correction. Editing only the grams sends the SAME name back unchanged, and
    that must teach nothing -- the person never touched the name they were
    shown, whatever detected_name happens to read."""
    seen = []
    monkeypatch.setattr(FI, "remember", lambda u, d, a, **_k: seen.append((u, d, a)))
    original = [{**_det("Scallops, grilled"), "detected_name": "Scallops"}]
    FI.learn_from_correction(
        "u1", original, [_Corrected("Scallops, grilled", 205.0, source_index=0)])
    assert seen == []


def test_confirming_the_cameras_word_is_still_forwarded_to_remember(monkeypatch):
    """The item on screen shows the ALIASED name (whatever apply_to renamed it
    to), so typing the camera's own original word back IS a real edit to the
    name the person was shown, even though it happens to equal detected_name.
    remember() is what now decides that this means "undo", so
    learn_from_correction must still call it rather than treating was == now
    as nothing having changed."""
    seen = []
    monkeypatch.setattr(FI, "remember", lambda u, d, a, **_k: seen.append((u, d, a)))
    original = [{**_det("teriyaki beef"), "detected_name": "scallops"}]
    FI.learn_from_correction(
        "u1", original, [_Corrected("scallops", 140.0, source_index=0)])
    assert seen == [("u1", "scallops", "scallops")]


# --- undoing a learned rename (remember() against a fake table) ---------------

class _AliasQuery:
    def __init__(self, rows):
        self.rows, self.filters, self.op, self.payload = rows, {}, None, None

    def select(self, *_a):
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def limit(self, _n):
        return self

    def delete(self):
        self.op = "delete"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, patch):
        self.op, self.payload = "update", patch
        return self

    def execute(self):
        match = [r for r in self.rows
                 if all(r.get(k) == v for k, v in self.filters.items())]
        if self.op == "delete":
            for r in match:
                self.rows.remove(r)
            return SimpleNamespace(data=match)
        if self.op == "insert":
            new = dict(self.payload)
            new.setdefault("id", f"row{len(self.rows) + 1}")
            self.rows.append(new)
            return SimpleNamespace(data=[new])
        if self.op == "update":
            for r in match:
                r.update(self.payload)
        return SimpleNamespace(data=match)


class _AliasSB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == FI.TABLE
        return _AliasQuery(self.rows)


def test_a_third_correction_to_a_different_food_resets_samples_to_one(monkeypatch):
    rows = [{"id": "a1", "user_id": "u1", "described_as": "scallops",
             "actual_name": "teriyaki beef", "samples": 2}]
    monkeypatch.setattr(FI, "service", lambda: _AliasSB(rows))
    FI.remember("u1", "scallops", "chicken")
    assert len(rows) == 1
    assert rows[0]["actual_name"] == "chicken"
    assert rows[0]["samples"] == 1


def test_confirming_the_cameras_word_clears_a_stored_alias(monkeypatch):
    """Gil corrects "teriyaki beef" back to "scallops" -- the camera had it
    right, and this rename was wrong. The alias that produced it must be
    deleted outright, not reset to samples=1, so it does not fire once more
    before it would reset."""
    rows = [{"id": "a1", "user_id": "u1", "described_as": "scallops",
             "actual_name": "teriyaki beef", "samples": 2}]
    monkeypatch.setattr(FI, "service", lambda: _AliasSB(rows))
    FI.remember("u1", "scallops", "scallops")
    assert rows == []


def test_confirming_a_word_with_nothing_stored_is_still_a_no_op(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(FI, "service", lambda: _AliasSB(rows))
    FI.remember("u1", "scallops", "Scallops")
    assert rows == []


def test_after_clearing_an_alias_the_next_scan_is_not_renamed(monkeypatch):
    rows = [{"id": "a1", "user_id": "u1", "described_as": "scallops",
             "actual_name": "teriyaki beef", "samples": 2}]
    monkeypatch.setattr(FI, "service", lambda: _AliasSB(rows))
    FI.remember("u1", "scallops", "scallops")
    learned = FI.aliases_for("u1")
    assert learned == []
    det = {"name": "scallops", "identification": "named"}
    assert FI.apply_to(det, learned) is None
    assert det["name"] == "scallops"


def test_a_scanned_item_carries_the_models_word_past_the_database_rename(monkeypatch):
    """build_items end to end, stubbed lookup: the logged name is the database
    row's, detected_name is the model's -- without the preparation prefix
    _lookup_name adds, because apply_to() compares det["name"] alone."""
    import asyncio

    from app.models.common import Macros
    from app.services.ai import vision

    seen_keys = []

    async def resolve_many(names):
        seen_keys.extend(names)
        return {n: {"id": "f1", "display_name": "Scallops, grilled",
                    "density_g_ml": 1.0} for n in names}
    monkeypatch.setattr(vision.resolver, "resolve_many", resolve_many)
    monkeypatch.setattr(vision.resolver, "macros_for",
                        lambda fact, grams: Macros(kcal=grams))
    det = {"name": "Scallops", "preparation": "grilled", "area_ratio": 0.09,
           "confidence": 0.8, "food_group": "protein",
           "bbox": {"x": 0.3, "y": 0.3, "w": 0.2, "h": 0.2}}
    items, _ = asyncio.run(vision.build_items(
        [det], vision.GeometryHint(plate_ellipse_area_ratio=0.5, plate_diameter_mm=254)))

    assert seen_keys == ["grilled scallops"]
    assert items[0].name == "Scallops, grilled"
    assert items[0].detected_name == "Scallops"


def test_detected_name_is_captured_before_a_stored_alias_renames_it(monkeypatch):
    """apply_to() mutates det["name"] in place when a twice-confirmed alias
    fires. If detected_name were read after that, a correction against it
    would create a new alias chained off the RENAMED word ("teriyaki beef")
    rather than touching the alias that actually fired ("scallops"), and the
    original alias would keep renaming every future scan forever. So the
    camera's word has to be captured before apply_to() runs."""
    import asyncio

    from app.models.common import Macros
    from app.services.ai import vision

    async def resolve_many(names):
        return {n: {"id": "f1", "display_name": "Teriyaki Beef",
                    "density_g_ml": 1.0} for n in names}
    monkeypatch.setattr(vision.resolver, "resolve_many", resolve_many)
    monkeypatch.setattr(vision.resolver, "macros_for",
                        lambda fact, grams: Macros(kcal=grams))
    monkeypatch.setattr(
        vision.food_identity, "aliases_for",
        lambda user_id: [{"described_as": "scallops",
                          "actual_name": "teriyaki beef", "samples": 2}])
    det = {"name": "scallops", "identification": "named", "area_ratio": 0.09,
           "confidence": 0.8, "food_group": "protein",
           "bbox": {"x": 0.3, "y": 0.3, "w": 0.2, "h": 0.2}}
    items, _ = asyncio.run(vision.build_items(
        [det], vision.GeometryHint(plate_ellipse_area_ratio=0.5, plate_diameter_mm=254),
        user_id="u1"))

    assert det["name"] == "teriyaki beef", "the alias should still apply forward"
    assert items[0].detected_name == "scallops", (
        "detected_name must stay the pre-rename camera word")
