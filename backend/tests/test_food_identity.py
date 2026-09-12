"""What the user says a food actually is, kept for next time.

The name is not a label. One plate of rajas, photographed twice a minute
apart, came back as "creamy chicken" and then "creamy mushroom sauce" -- 315 g
against 186 g and 40% apart on energy, on geometry that was within 12% both
times. The name picks the density, the height prior and the nutrition lookup.
"""
from __future__ import annotations

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


def test_a_dish_the_model_named_is_never_renamed():
    """The one guard that bounds this whole feature. A stored alias is only
    ever consulted on a food the model has already said it cannot name, so it
    can never overwrite a confident identification."""
    det = {"name": "creamy mushroom sauce", "identification": "named"}
    assert FI.apply_to(det, RAJAS) is None
    assert det["name"] == "creamy mushroom sauce"


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
    monkeypatch.setattr(FI, "remember", lambda u, d, a: seen.append((u, d, a)))
    FI.learn_from_correction("u1", [_det("creamy mushroom sauce"), _det("rice")], [
        _Corrected("rajas con crema", 98.0, source_index=0),
        _Corrected("rice", 77.0, source_index=1),          # unchanged
        _Corrected("tortilla", 30.0),                       # added by the user
    ])
    assert seen == [("u1", "creamy mushroom sauce", "rajas con crema")]


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
