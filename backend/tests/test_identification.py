"""Naming the food, which matters more than weighing it.

A 20% portion error is a number the user can correct. A chicken drumstick
logged as beef, or refried beans logged as meatloaf, is a wrong meal -- and
that one happened, on the weighed bench, on a photo of a plate the pipeline
had already read correctly from a different angle:

    08-plate-mole-chicken-topdown.jpg   refried beans      95.9 g   -12.0%
    14-plate-mole-chicken-card.jpg      baked meatloaf    188.8 g

Same plate, same food. Legume at ~1.1 kcal/g logged as a meat at more than
double that, and no doubt raised about it at all.

Two defences are tested here, and the limits on each matter as much as the
behaviour, because contextual reasoning has already cost this pipeline a
weighed 133 g portion once:

  * `_surface_tiebreak` -- what the food is sitting on may settle a tie the
    model itself flagged, and may do nothing else.
  * `second_look` -- the doubtful items get cropped out and looked at again,
    alone and enlarged, in one extra call.
"""
from __future__ import annotations

import asyncio
import base64
import io

import pytest

from app.services.ai import vision


# ---------------------------------------------------------------------------
# The serving surface as a tie-breaker
# ---------------------------------------------------------------------------
def _det(name, group, conf, alts=None, **extra):
    d = {"name": name, "food_group": group, "confidence": conf,
         "alternatives": list(alts or [])}
    d.update(extra)
    return d


def test_surface_settles_a_genuine_tie():
    """Beans do not travel loose on paper. Wrapped in a tortilla they do."""
    det = _det("refried beans", "legume", 0.55,
               [{"name": "burrito", "food_group": "composite", "confidence": 0.50}])
    note = vision._surface_tiebreak(det, "paper")
    assert det["name"] == "burrito"
    assert det["food_group"] == "composite"
    assert note and "refried beans" in note
    # The loser keeps its seat, so the person can put it back in one tap and
    # `_identification_doubt` still has something to raise.
    assert det["alternatives"][0]["name"] == "refried beans"


def test_surface_does_not_override_a_confident_identification():
    det = _det("refried beans", "legume", 0.93,
               [{"name": "burrito", "food_group": "composite", "confidence": 0.90}])
    assert vision._surface_tiebreak(det, "paper") is None
    assert det["name"] == "refried beans"


def test_surface_does_not_promote_a_distant_second_place():
    det = _det("refried beans", "legume", 0.55,
               [{"name": "burrito", "food_group": "composite", "confidence": 0.20}])
    assert vision._surface_tiebreak(det, "paper") is None


def test_a_plate_has_no_opinion_about_anything():
    """The whole rule in one test: a plate can hold literally any food, so it
    is never allowed to argue with the photograph."""
    for surface in ("dinner_plate", "plate", "bowl", None, "", "unknown"):
        det = _det("refried beans", "legume", 0.55,
                   [{"name": "burrito", "food_group": "composite", "confidence": 0.55}])
        assert vision._surface_tiebreak(det, surface) is None
        assert det["name"] == "refried beans"


def test_surface_never_invents_a_candidate():
    """It can only pick from what the model itself put on the list. With no
    alternative offered there is no tie, and nothing to break."""
    det = _det("refried beans", "legume", 0.40, [])
    assert vision._surface_tiebreak(det, "paper") is None
    assert det["name"] == "refried beans"


def test_surface_stays_quiet_unless_it_knows_both_foods():
    # Rice: the surface has no view on it, so it cannot be displaced.
    det = _det("mexican rice", "grain", 0.55,
               [{"name": "burrito", "food_group": "composite", "confidence": 0.55}])
    assert vision._surface_tiebreak(det, "paper") is None
    # Beans -> mashed potatoes: neither travels loose on paper. No help either.
    det = _det("refried beans", "legume", 0.55,
               [{"name": "mashed potatoes", "food_group": "vegetable", "confidence": 0.55}])
    assert vision._surface_tiebreak(det, "paper") is None


def test_surface_never_removes_food():
    """The scar this whole module was built around."""
    dets = [_det("refried beans", "legume", 0.55,
                 [{"name": "burrito", "food_group": "composite", "confidence": 0.5}]),
            _det("mexican rice", "grain", 0.8),
            _det("chicken drumstick", "protein", 0.9)]
    for d in dets:
        vision._surface_tiebreak(d, "paper")
    assert len(dets) == 3
    assert all(d.get("name") for d in dets)


# ---------------------------------------------------------------------------
# Who is worth a second look
# ---------------------------------------------------------------------------
BOX = {"x": 0.30, "y": 0.30, "w": 0.20, "h": 0.20}


def test_a_cross_group_alternative_earns_a_second_look():
    """Meatloaf against refried beans is the case that started this."""
    assert vision._needs_second_look(_det(
        "refried beans", "legume", 0.80,
        [{"name": "baked meatloaf", "food_group": "protein", "confidence": 0.30}],
        bbox=BOX))


def test_a_within_group_alternative_does_not():
    """'drumstick' against 'chicken leg' changes nothing anyone must act on,
    and a crop is not free."""
    assert not vision._needs_second_look(_det(
        "chicken drumstick", "protein", 0.80,
        [{"name": "chicken leg", "food_group": "protein", "confidence": 0.40}],
        bbox=BOX))


def test_low_confidence_alone_earns_a_second_look():
    assert vision._needs_second_look(_det("beans", "legume", 0.50, bbox=BOX))


def test_the_common_case_is_left_alone():
    """Confident and uncontested. Most items on most plates are this, which is
    what keeps the pass cheap."""
    assert not vision._needs_second_look(_det("mexican rice", "grain", 0.85, bbox=BOX))


def test_no_box_means_no_crop():
    assert not vision._needs_second_look(_det("beans", "legume", 0.20))


# ---------------------------------------------------------------------------
# Cutting the crop
# ---------------------------------------------------------------------------
def _photo(w=3024, h=4032) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (w, h))
    for x in range(0, w, 8):
        for y in range(0, h, 8):
            img.paste((x % 256, y % 256, 90), (x, y, min(x + 8, w), min(y + 8, h)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def test_a_crop_spends_far_more_pixels_on_one_food():
    """The reason this pass exists.

    A drumstick covering 3% of the frame is about 160x220 pixels in the 1280px
    image the first pass is shown, and from those pixels it must decide meat
    or bean or potato. Cut out of the original file and enlarged, the same
    drumstick arrives as roughly 670x900 -- around seventeen times the pixels,
    on the one food actually in question.
    """
    from PIL import Image
    raw = _photo()
    b64 = vision.crop_b64(raw, {"x": 0.42, "y": 0.55, "w": 0.17, "h": 0.17})
    assert b64
    cut = Image.open(io.BytesIO(base64.b64decode(b64)))
    seen = Image.open(io.BytesIO(raw))
    seen.thumbnail((vision.MAX_IMAGE_EDGE, vision.MAX_IMAGE_EDGE))
    before = (0.17 * seen.size[0]) * (0.17 * seen.size[1])
    assert cut.size[0] * cut.size[1] > before * 8


def test_a_small_item_is_enlarged_rather_than_sent_as_a_postage_stamp():
    from PIL import Image
    b64 = vision.crop_b64(_photo(), {"x": 0.50, "y": 0.50, "w": 0.03, "h": 0.03})
    assert b64
    cut = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert max(cut.size) >= vision.CROP_MIN_EDGE


def test_a_crop_carries_context_around_the_food():
    """Zero padding cuts a drumstick off at its own outline and throws away the
    bone, the char and the sauce beside it -- which is what tells it from
    mashed beans."""
    from PIL import Image
    raw = _photo(2000, 2000)
    b64 = vision.crop_b64(raw, {"x": 0.40, "y": 0.40, "w": 0.20, "h": 0.20})
    cut = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert cut.size[0] > 0.20 * 2000


def test_a_crop_at_the_frame_edge_does_not_run_off_it():
    b64 = vision.crop_b64(_photo(), {"x": 0.0, "y": 0.90, "w": 0.20, "h": 0.10})
    assert b64


@pytest.mark.parametrize("raw,bbox", [
    (b"not a jpeg", {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}),
    (None, {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}),
])
def test_an_uncroppable_photo_returns_nothing_rather_than_raising(raw, bbox):
    """No second opinion is a fine outcome. A 500 on a good photo is not."""
    assert vision.crop_b64(raw or b"", bbox) is None


@pytest.mark.parametrize("bbox", [None, {}, {"x": 0.5, "y": 0.5, "w": 0, "h": 0}, [1, 2, 3]])
def test_a_useless_box_returns_nothing(bbox):
    assert vision.crop_b64(_photo(400, 400), bbox) is None


# ---------------------------------------------------------------------------
# The second look end to end
# ---------------------------------------------------------------------------
class _Call:
    ok = True

    def __init__(self, payload):
        self.payload = payload


def _second_look(dets, payload, raw=None):
    """Drive the real second_look() with a canned close-up response."""
    original_ask, original_record = vision.ask_vision, vision.record_usage
    seen: dict = {}

    async def fake_ask(**kw):
        seen.update(kw)
        if isinstance(payload, Exception):
            raise payload
        return _Call(payload)

    async def fake_record(*_a, **_kw):
        return None

    vision.ask_vision, vision.record_usage = fake_ask, fake_record
    try:
        notes = asyncio.run(vision.second_look(
            dets, _photo(1600, 1600) if raw is None else raw, "u"))
        return notes, seen
    finally:
        vision.ask_vision, vision.record_usage = original_ask, original_record


def test_the_close_up_overturns_a_wrong_food():
    """Photo 14's failure, and what should have happened to it."""
    dets = [_det("baked meatloaf", "protein", 0.62,
                 [{"name": "refried beans", "food_group": "legume", "confidence": 0.3}],
                 bbox=BOX, typical_serving_g=200)]
    notes, _ = _second_look(dets, {"items": [
        {"index": 0, "name": "refried beans", "food_group": "legume",
         "confidence": 0.86, "evidence": "smooth paste, no fibre or grain"}]})
    assert dets[0]["name"] == "refried beans"
    assert dets[0]["food_group"] == "legume"
    # The prior described the food we no longer think this is.
    assert "typical_serving_g" not in dets[0]
    assert notes and "baked meatloaf" in notes[0] and "refried beans" in notes[0]
    # And the overturned name stays reachable.
    assert dets[0]["alternatives"][0]["name"] == "baked meatloaf"


def test_the_close_up_confirming_the_first_pass_clears_the_doubt():
    """Worth as much as an overturn. Without this the user is shown 'this might
    be mashed potatoes' about a food that has now been checked twice."""
    dets = [_det("refried beans", "legume", 0.60,
                 [{"name": "mashed potatoes", "food_group": "vegetable", "confidence": 0.35}],
                 bbox=BOX)]
    notes, _ = _second_look(dets, {"items": [
        {"index": 0, "name": "refried beans", "food_group": "legume", "confidence": 0.88}]})
    assert dets[0]["name"] == "refried beans"
    assert dets[0]["confidence"] >= 0.60
    assert not any(vision.food_group(a["food_group"]) == "vegetable"
                   for a in dets[0]["alternatives"])
    assert notes == []          # nothing changed, so nothing to say


def test_an_unsure_close_up_changes_nothing():
    """Being unsure twice is not evidence, and the first answer at least had
    the whole plate for context."""
    dets = [_det("refried beans", "legume", 0.55,
                 [{"name": "ground beef", "food_group": "protein", "confidence": 0.4}],
                 bbox=BOX)]
    notes, _ = _second_look(dets, {"items": [
        {"index": 0, "name": "unsure", "food_group": "protein", "confidence": 0.0}]})
    assert dets[0]["name"] == "refried beans"
    assert notes == []


def test_a_hesitant_close_up_changes_nothing():
    dets = [_det("refried beans", "legume", 0.55,
                 [{"name": "ground beef", "food_group": "protein", "confidence": 0.4}],
                 bbox=BOX)]
    _second_look(dets, {"items": [
        {"index": 0, "name": "ground beef", "food_group": "protein", "confidence": 0.20}]})
    assert dets[0]["name"] == "refried beans"


def test_confident_items_are_never_cropped():
    """The cost control. Nothing in doubt means no extra call at all."""
    dets = [_det("mexican rice", "grain", 0.88, bbox=BOX),
            _det("chicken drumstick", "protein", 0.90, bbox=BOX)]
    notes, seen = _second_look(dets, {"items": []})
    assert notes == []
    assert seen == {}           # ask_vision was never reached


def test_at_most_three_crops_go_out():
    dets = [_det(f"mystery {i}", "composite", 0.30, bbox=BOX) for i in range(9)]
    _, seen = _second_look(dets, {"items": []})
    assert len(seen["images"]) <= vision.SECOND_LOOK_MAX_ITEMS


def test_a_failed_second_opinion_leaves_the_scan_intact():
    """A failed second opinion is not a failed scan."""
    for payload in (RuntimeError("vision down"), {"items": "nonsense"}, {},
                    {"items": [None, 5, "x", {"index": 0}]}):
        dets = [_det("refried beans", "legume", 0.50, bbox=BOX)]
        notes, _ = _second_look(dets, payload)   # must not raise
        assert notes == []
        assert dets[0]["name"] == "refried beans"


def test_an_out_of_range_index_is_ignored():
    """A model that answers about item 7 when it was shown one crop must not
    rename a food that was never asked about."""
    dets = [_det("refried beans", "legume", 0.50, bbox=BOX)]
    _second_look(dets, {"items": [
        {"index": 7, "name": "steak", "food_group": "protein", "confidence": 0.9},
        {"index": -1, "name": "steak", "food_group": "protein", "confidence": 0.9},
        {"index": "x", "name": "steak", "food_group": "protein", "confidence": 0.9}]})
    assert dets[0]["name"] == "refried beans"


def test_the_second_look_never_adds_or_drops_an_item():
    dets = [_det("refried beans", "legume", 0.50, bbox=BOX),
            _det("mexican rice", "grain", 0.55, bbox=BOX),
            _det("chicken drumstick", "protein", 0.92, bbox=BOX)]
    _second_look(dets, {"items": [
        {"index": 0, "name": "ground beef", "food_group": "protein", "confidence": 0.9}]})
    assert len(dets) == 3
    assert dets[1]["name"] == "mexican rice"
    assert dets[2]["name"] == "chicken drumstick"


# ---------------------------------------------------------------------------
# The oil a camera cannot see
# ---------------------------------------------------------------------------
def test_a_fat_adding_preparation_that_got_lost_is_reported():
    """The one failure every competing app shares.

    NIH/NIDDK tested MyFitnessPal, LoseIt!, CalAI and Appediet against 102
    meals weighed to 0.1 g in a metabolic kitchen. All four underestimated
    energy by roughly a third -- 250 to 345 kcal a meal -- and the researchers
    attributed it largely to undercounting fat, about 30 g per meal.

    The mechanism here is specific. The pipeline folds the preparation into the
    lookup name, so "fried chicken thigh" is what gets searched, and a USDA
    entry for fried chicken already contains the oil it absorbed. That works
    when the search finds one. When it does not, the request degrades to the
    plain food and the oil vanishes with it -- and nothing downstream notices,
    because `implausible_energy` only fires below HALF a food group's floor and
    a fried item matched to its roasted twin sits well above that.
    """
    note = vision._fat_preparation_lost(
        {"preparation": "fried"},
        {"display_name": "Chicken, broilers or fryers, roasted"},
    )
    assert note and "oil" in note


def test_a_preparation_that_survived_the_lookup_is_left_alone():
    """A fried entry already contains its oil. Saying so again would be noise,
    and adding fat on top of it would double-count."""
    for matched in ("Chicken, broilers, fried, batter",
                    "Potatoes, french fried, deep fried",
                    "Shrimp, breaded and fried",
                    "Spinach, sauteed with oil"):
        assert vision._fat_preparation_lost({"preparation": "fried"},
                                            {"display_name": matched}) is None, matched


def test_preparations_that_add_no_fat_are_never_flagged():
    """Grilling and boiling put no oil into anything. Flagging them would train
    people to dismiss the one message that matters."""
    for prep in ("grilled", "boiled", "steamed", "raw", "baked", "roasted",
                 "poached", "smoked", "unknown", ""):
        assert vision._fat_preparation_lost(
            {"preparation": prep},
            {"display_name": "Chicken, broilers or fryers, roasted"},
        ) is None, prep


def test_it_reports_rather_than_silently_adding_fat():
    """Deliberate: no constant is invented and no fat is added.

    Adding some would double-count every entry that resolved correctly, and
    there is no weighed macro truth on this bench to check it against. Tuning a
    fat constant blind is the trap this codebase keeps refusing. So it does what
    the energy check does -- reports the contradiction and lets the person
    decide which of the two inputs was wrong.
    """
    note = vision._fat_preparation_lost(
        {"preparation": "sauteed"}, {"display_name": "Spinach, cooked, boiled"})
    assert note is not None
    assert "worth a look" in note


@pytest.mark.parametrize("det,fact", [
    ({}, {}),
    ({"preparation": None}, None),
    ({"preparation": 5}, {"display_name": 7}),
    ({"preparation": "fried"}, None),
    ({"preparation": "fried"}, {"display_name": None}),
    ({"preparation": ["fried"]}, {"display_name": ["x"]}),
    ({"preparation": "fried"}, {"display_name": ""}),
])
def test_junk_never_raises_and_never_invents_a_warning(det, fact):
    got = vision._fat_preparation_lost(det, fact)
    assert got is None or isinstance(got, str)


# --- did it recognise the dish, or only describe it? --------------------------

def test_the_model_says_whether_it_recognised_the_dish():
    """The name is not a label -- it picks the density, the height prior and
    the nutrition lookup.

    One weighed plate, photographed twice a minute apart, came back as "creamy
    chicken" and then "creamy mushroom sauce". It was neither. That difference
    alone moved the meal from 186 g to 315 g and its energy by about 40%, on
    geometry that was within 12% both times. So the model is asked to say when
    it is describing rather than recognising, and the person gets asked.
    """
    from app.services.ai.vision import _identification_state as state

    assert state({"identification": "named"}) == "named"
    assert state({"identification": "described"}) == "described"
    assert state({"identification": "unsure"}) == "unsure"
    assert state({"identification": "DESCRIBED "}) == "described"


def test_a_missing_field_is_trusted_and_a_junk_one_is_not():
    """Absence reads as "named" on purpose, against the safer-looking choice.

    Treating a missing field as unsure would put "we could not identify this"
    on every item of every scan the moment the model dropped one field. A
    question on everything trains people to dismiss the question, which costs
    more than the occasional unflagged item. Absence is logged instead, so how
    often it happens is a fact rather than a guess.

    A field that is PRESENT and unrecognisable is different -- that is a model
    saying something we cannot read, and it gets the question.
    """
    from app.services.ai.vision import _identification_state as state

    assert state({}) == "named"
    for junk in (None, "", "probably chicken", ["unsure"], 3, {"a": 1}):
        assert state({"identification": junk}) == "unsure", junk


def test_the_prompt_asks_for_a_dish_name_and_offers_a_way_out():
    """A prompt that only says "be specific" is what produced "creamy mushroom
    sauce" -- specific, lookupable, and wrong. It needs both halves: name the
    dish, and an unpenalised way to say you cannot."""
    from app.services.ai.prompts import FOOD_VISION_SYSTEM as P

    assert '"identification": "named|described|unsure"' in P
    assert "Name the DISH" in P
    for state in ("named", "described", "unsure"):
        assert f'"{state}"' in P


def test_an_undescribed_dish_tells_the_user_it_will_remember():
    """The point of asking is that the answer is kept. A question that leads
    nowhere is worse than no question."""
    from app.services.ai.vision import _identification_state

    assert _identification_state({"identification": "described"}) == "described"
    # the note the user sees is built in build_items; this pins its promise
    import inspect
    from app.services.ai import vision
    src = inspect.getsource(vision)
    assert "it will remember" in src
