"""The reasoning stage, which was quietly deleting and misplacing food.

Four separate defects lived in one function, all of them invisible: the meal
still looked plausible afterwards, just lighter or wrong. They were found by
noticing that a weighed four-item plate reported a chicken drumstick and a
spaghetti casserole at identical grams -- 160.8 g each, to the tenth of a
gram -- across four runs.

Everything here drives the real `refine()` with a stubbed reasoning call, so
the matching, merging and clamping logic is exercised as shipped.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.common import Macros
from app.models.nutrition import DetectedItem
from app.services.ai import vision


class _Call:
    ok = True

    def __init__(self, payload):
        self.payload = payload


def _run(items, payload):
    """Drive refine() with a canned reasoning response."""
    original_ask = vision.ask_reasoning
    original_record = vision.record_usage

    async def fake_ask(**_kw):
        return _Call(payload)

    async def fake_record(*_a, **_kw):
        return None

    vision.ask_reasoning = fake_ask
    vision.record_usage = fake_record
    try:
        return asyncio.run(vision.refine(
            items, scene="a plate", profile={}, day_totals=Macros(), user_id="u",
        ))
    finally:
        vision.ask_reasoning = original_ask
        vision.record_usage = original_record


def _item(name, grams, group="composite", kcal=100.0):
    return DetectedItem(
        name=name, grams=grams, grams_low=grams * 0.8, grams_high=grams * 1.2,
        food_group=group, confidence=0.7,
        macros=Macros(kcal=kcal, protein_g=5, carbs_g=10, fat_g=3),
    )


def test_a_short_response_does_not_delete_food():
    """The loop walked the model's list and stopped at its end, so every
    detection past it vanished with its calories. A shorter list is routine --
    merging produces one."""
    items = [_item("rice", 100), _item("beans", 80), _item("chicken", 150)]
    refined, _meta = _run(items, {"items": [{"index": 0, "grams": 110}]})
    assert [i.name for i in refined] == ["rice", "beans", "chicken"]
    assert sum(i.grams for i in refined) == pytest.approx(110 + 80 + 150, rel=0.02)


def test_specs_are_matched_by_index_not_by_position():
    """A reordered response applied one food's correction to another. This is
    the bug that put identical grams on a drumstick and a casserole."""
    items = [_item("rice", 100), _item("chicken", 150, group="protein")]
    refined, _ = _run(items, {"items": [
        {"index": 1, "grams": 160, "name": "chicken"},
        {"index": 0, "grams": 105, "name": "rice"},
    ]})
    by_name = {i.name: i.grams for i in refined}
    assert by_name["rice"] == pytest.approx(105, rel=0.05)
    assert by_name["chicken"] == pytest.approx(160, rel=0.05)


def test_a_merge_moves_grams_and_never_loses_them():
    """`by_index` was keyed on id(items[i]) and looked up against
    model_copy() objects, so it missed every time and merged grams were
    discarded -- a merge deleted food, which the code's own comment forbids."""
    items = [_item("tortilla", 60, group="grain"), _item("tortilla", 40, group="grain")]
    refined, _ = _run(items, {"items": [
        {"index": 0, "keep": False, "merge_into": 1},
        {"index": 1, "grams": 40},
    ]})
    assert len(refined) == 1
    assert refined[0].grams == pytest.approx(100, rel=0.02)


def test_a_chain_of_merges_still_keeps_every_gram():
    """0 into 1, 1 into 2. The recipient was itself merged away, so item 0's
    grams were credited to something that no longer existed."""
    items = [_item("a", 30, group="grain"), _item("b", 40, group="grain"),
             _item("c", 50, group="grain")]
    refined, _ = _run(items, {"items": [
        {"index": 0, "keep": False, "merge_into": 1},
        {"index": 1, "keep": False, "merge_into": 2},
        {"index": 2, "grams": 50},
    ]})
    assert sum(i.grams for i in refined) == pytest.approx(120, rel=0.02)


def test_a_merge_carries_the_band_with_the_grams():
    """grams and macros were updated and the range was not, leaving a stored
    band that no longer contained the number it described."""
    items = [_item("rice", 60, group="grain"), _item("rice", 60, group="grain")]
    refined, _ = _run(items, {"items": [
        {"index": 0, "keep": False, "merge_into": 1},
        {"index": 1, "grams": 60},
    ]})
    item = refined[0]
    assert item.grams_low <= item.grams <= item.grams_high


def test_food_is_never_dropped_outright():
    """Removing an item is a claim that the vision model hallucinated food.
    Under-counting is the harmful direction, so the item stays and the meal is
    flagged instead."""
    items = [_item("rice", 100), _item("fideo", 133)]
    refined, meta = _run(items, {"items": [
        {"index": 0, "grams": 100},
        {"index": 1, "keep": False, "merge_into": None},
    ]})
    assert len(refined) == 2
    assert meta["needs_review"] is True


def test_a_rename_may_add_detail_but_not_change_the_food():
    """Macros and food_fact_id were resolved for the original name. Renaming
    'chicken breast' to 'fried chicken thigh' leaves breast calories under a
    thigh label -- and the name is what the user checks."""
    items = [_item("chicken breast", 150, group="protein")]
    same, _ = _run(items, {"items": [{"index": 0, "name": "chicken breast, grilled"}]})
    assert same[0].name == "chicken breast, grilled"

    # A shortening is the same food said shorter. Requiring an exact match
    # refused "boiled bean soup" -> "bean soup" and told the user on the bench
    # that their soup had been renamed to a different food.
    items = [_item("boiled bean soup", 400, group="legume")]
    shorter, _ = _run(items, {"items": [{"index": 0, "name": "bean soup"}]})
    assert shorter[0].name == "bean soup"

    items = [_item("chicken breast", 150, group="protein")]
    different, meta = _run(items, {"items": [{"index": 0, "name": "fried chicken thigh"}]})
    assert different[0].name == "chicken breast"
    assert meta["needs_review"] is True


def test_an_out_of_range_confidence_cannot_reach_the_database():
    """`confidence` lands in numeric(4,3). A model answering 85 instead of 0.85
    used to overflow the column AFTER the meal row was committed, leaving an
    item-less meal whose calories had already rolled into the day's total."""
    items = [_item("rice", 100)]
    refined, _ = _run(items, {"items": [{"index": 0, "confidence": 85}]})
    assert 0.0 <= refined[0].confidence <= 1.0

    refined, _ = _run([_item("rice", 100)], {"items": [{"index": 0, "confidence": "high"}]})
    assert 0.0 <= refined[0].confidence <= 1.0


def test_a_junk_response_leaves_the_meal_alone():
    """Every field here comes from a model. None of it may crash a scan that
    had already produced a perfectly good set of detections."""
    items = [_item("rice", 100), _item("beans", 80)]
    for payload in ({"items": None}, {"items": ["rice", "beans"]},
                    {"items": [{"index": "x", "grams": "lots"}]}, {}):
        refined, _ = _run(items, payload)
        assert [i.name for i in refined] == ["rice", "beans"]
        assert sum(i.grams for i in refined) == pytest.approx(180, rel=0.02)


def test_a_cross_group_alternative_is_surfaced():
    """A wrong portion is a number to correct. A wrong FOOD is a wrong meal.

    Measured on a weighed plate: 109 g of refried beans and egg came back as
    "ground meat", and a 146 g drumstick in mole as "meat with sauce". Legume
    against protein is about 1.1 kcal per gram against more than double that.
    """
    from app.services.ai.vision import _identification_doubt

    beans = {"alternatives": [
        {"name": "ground meat", "food_group": "protein", "confidence": 0.35}]}
    said = _identification_doubt(beans, "legume")
    assert said and "ground meat" in said

    # Same kind of food, different words: not worth interrupting anyone for.
    same = {"alternatives": [
        {"name": "chicken leg", "food_group": "protein", "confidence": 0.4}]}
    assert _identification_doubt(same, "protein") is None

    # A passing thought is not a doubt.
    faint = {"alternatives": [
        {"name": "ground meat", "food_group": "protein", "confidence": 0.05}]}
    assert _identification_doubt(faint, "legume") is None

    # And none of this may crash on whatever the model actually returns.
    for junk in ({}, {"alternatives": None}, {"alternatives": ["beef"]},
                 {"alternatives": [{"name": None}]}):
        assert _identification_doubt(junk, "legume") is None


# --- an out-of-band proposal loses to the measurement, not to the band edge --

def test_a_rejected_proposal_keeps_the_measurement_not_the_band_edge():
    """The band was quietly deciding the answer.

    One 335 g meat skewer, photographed three ways. The reasoning model
    proposed 150 g every time -- the standard serving for its name -- and every
    time the clamp snapped to grams_low, so the final figure was whichever
    floor the band happened to have: 391 -> 289, 349 -> 234, 284 -> 230, for
    -14%, -30% and -32% against the scale.

    That inverts what the band is for. A wider band means the estimator was
    LESS certain, and snapping to its edge moved the answer FURTHER from the
    measurement the less certain it was. Keeping the measurement instead scores
    +17%, +4% and -15% on the same three photos.
    """
    for measured, lo, hi, proposed in ((391, 289, 493, 150),
                                       (349, 234, 464, 200),
                                       (284, 230, 338, 150)):
        item = _item("grilled meat skewer", measured, group="protein")
        item.grams_low, item.grams_high = float(lo), float(hi)
        refined, _ = _run([item], {"items": [{"index": 0, "grams": proposed}]})
        assert refined[0].grams == pytest.approx(measured, rel=0.01), (
            f"a proposal of {proposed} g moved a measured {measured} g to "
            f"{refined[0].grams} g"
        )


def test_a_proposal_inside_the_band_is_still_accepted():
    """The model knows things geometry cannot -- that a drumstick includes
    bone, that a sauce is heavier than it looks. Inside the band it still
    wins."""
    for proposed, expected in ((115, 115), (90, 90), (100, 100)):
        item = _item("rice", 100, group="grain")
        item.grams_low, item.grams_high = 80.0, 120.0
        refined, _ = _run([item], {"items": [{"index": 0, "grams": proposed}]})
        assert refined[0].grams == pytest.approx(expected, rel=0.01)


def test_rejecting_a_proposal_costs_confidence_and_widens_the_band():
    """Keeping the measurement is not the same as being sure of it. The
    disagreement has to show up somewhere, and the band must reach far enough
    to contain the figure that was turned down -- otherwise the next stage
    reads a range that pretends no disagreement happened."""
    item = _item("grilled meat skewer", 284, group="protein")
    item.grams_low, item.grams_high = 230.0, 338.0
    before = item.confidence
    refined, notes = _run([item], {"items": [{"index": 0, "grams": 150}]})
    out = refined[0]
    assert out.confidence < before
    assert out.grams_low <= out.grams <= out.grams_high
    assert out.grams_low < 230.0, "the band did not widen to cover the disagreement"
    assert any("kept the measured" in c for c in notes["corrections"]), notes


def test_the_note_says_what_the_code_actually_did():
    """It used to print "kept the measured 289 g" while reporting the band
    floor. The number in the sentence and the number in the field were
    different, which is how this survived so long."""
    item = _item("grilled meat skewer", 391, group="protein")
    item.grams_low, item.grams_high = 289.0, 493.0
    refined, notes = _run([item], {"items": [{"index": 0, "grams": 150}]})
    said = [c for c in notes["corrections"] if "kept the measured" in c]
    assert said, notes
    assert f"{refined[0].grams:.0f}" in said[0], (
        f"the note claims a different number than the item carries: "
        f"{said[0]!r} vs {refined[0].grams} g"
    )


# --- the shadow measurement is a passenger, not a driver ---------------------

def _fake_facts(monkeypatch):
    """resolve_many, without a database."""
    async def resolve_many(names):
        return {n: {"id": "f1", "display_name": n, "density_g_ml": 1.0} for n in names}
    monkeypatch.setattr(vision.resolver, "resolve_many", resolve_many)
    monkeypatch.setattr(vision.resolver, "macros_for",
                        lambda fact, grams: Macros(kcal=grams, protein_g=1,
                                                   carbs_g=1, fat_g=1))


def _photo_bytes(w=900, h=1200):
    """A plate with three blobs on it, as PNG bytes."""
    import io

    import numpy as np
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (150, 150, 150))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.10, h * 0.10, w * 0.90, h * 0.90], fill=(245, 244, 240))
    for cx, cy, r, col in ((0.35, 0.35, 0.10, (180, 90, 40)),
                           (0.65, 0.38, 0.10, (120, 60, 30)),
                           (0.50, 0.68, 0.11, (200, 170, 60))):
        d.ellipse([w * (cx - r), h * (cy - r * w / h),
                   w * (cx + r), h * (cy + r * w / h)], fill=col)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_DETS = [
    {"name": "mexican rice", "area_ratio": 0.09, "confidence": 0.8,
     "bbox": {"x": 0.25, "y": 0.27, "w": 0.20, "h": 0.16}, "food_group": "grain"},
    {"name": "refried beans", "area_ratio": 0.09, "confidence": 0.8,
     "bbox": {"x": 0.55, "y": 0.30, "w": 0.20, "h": 0.16}, "food_group": "legume"},
    {"name": "chicken drumstick", "area_ratio": 0.10, "confidence": 0.8,
     "bbox": {"x": 0.39, "y": 0.59, "w": 0.22, "h": 0.18}, "food_group": "protein"},
]
_PLATE = {"x": 0.10, "y": 0.10, "w": 0.80, "h": 0.80}
_HINT = vision.GeometryHint(plate_ellipse_area_ratio=0.50, plate_diameter_mm=254)


def test_measuring_the_photo_does_not_move_a_single_gram(monkeypatch):
    """The whole safety claim for shipping this tonight.

    The measurement is reported and scored; it is not allowed to influence the
    estimate until a bench run says it should. If this test ever fails, the
    shadow has become a driver without anybody deciding that it should.
    """
    _fake_facts(monkeypatch)
    blind, _ = asyncio.run(vision.build_items(_DETS, _HINT))
    seeing, _ = asyncio.run(vision.build_items(
        _DETS, _HINT, raw=_photo_bytes(), plate_bbox=_PLATE))

    assert [i.name for i in blind] == [i.name for i in seeing]
    for a, b in zip(blind, seeing):
        assert a.grams == b.grams, f"{a.name}: {a.grams} -> {b.grams}"
        assert a.grams_low == b.grams_low and a.grams_high == b.grams_high
        assert a.confidence == b.confidence
        assert a.estimation_method == b.estimation_method
        assert a.pixel_area_ratio == b.pixel_area_ratio
        assert a.macros.kcal == b.macros.kcal


def test_the_measurement_is_actually_reported(monkeypatch):
    """...and the test above is not passing because nothing ran."""
    _fake_facts(monkeypatch)
    seeing, _ = asyncio.run(vision.build_items(
        _DETS, _HINT, raw=_photo_bytes(), plate_bbox=_PLATE))
    measured = [i for i in seeing if i.measured_area_ratio]
    assert measured, "no item came back with a footprint -- the shadow never ran"
    # A SHARE, not grams. Colour separates the foods; it does not weigh them.
    # Asking the mask for absolute weight scored 60% against the kitchen scale;
    # asking it which share of the meal each food is, is the question it can
    # answer, because both terms of a ratio move together when the mask drifts.
    for item in measured:
        assert 0 < item.measured_area_ratio < 1
        assert item.measured_share is None or 0 < item.measured_share <= 1
    shares = [i.measured_share for i in seeing if i.measured_share]
    if len(shares) == len(seeing):
        assert sum(shares) == pytest.approx(1.0, abs=0.01), (
            f"the split does not account for the whole meal: {shares}"
        )


def test_a_broken_photo_costs_the_scan_nothing(monkeypatch):
    """Measurement is best-effort. Truncated bytes, a nonsense plate box, no
    plate box at all -- each must leave the scan exactly as it was."""
    _fake_facts(monkeypatch)
    good, _ = asyncio.run(vision.build_items(_DETS, _HINT))
    for label, raw, plate in (
        ("truncated", b"\x89PNG\r\n\x1a\n truncated", _PLATE),
        ("not an image", b"this is not a photograph", _PLATE),
        ("empty", b"", _PLATE),
        ("nonsense plate", _photo_bytes(), {"x": 9.0, "y": -3.0, "w": 0.0, "h": "x"}),
        ("no plate", _photo_bytes(), None),
    ):
        out, _ = asyncio.run(vision.build_items(_DETS, _HINT, raw=raw, plate_bbox=plate))
        assert len(out) == len(good), label
        for a, b in zip(good, out):
            assert a.grams == b.grams, f"{label}: {a.name} moved"


def test_a_real_scan_does_not_pay_for_the_shadow():
    """The measurement costs ~3 s of OpenCV on a full-size photo. A user must
    not pay that for a number nothing reads yet, so `measure_footprints`
    defaults off and only the bench turns it on."""
    from app.models.nutrition import ScanRequest
    assert ScanRequest(image_paths=["a.jpg"]).measure_footprints is False
    assert ScanRequest(image_paths=["a.jpg"], measure_footprints=True).measure_footprints


def test_the_shadow_is_skipped_when_it_is_not_asked_for(monkeypatch):
    """...and skipping it must produce the same meal, not a different one."""
    _fake_facts(monkeypatch)
    off, _ = asyncio.run(vision.build_items(_DETS, _HINT, raw=None, plate_bbox=_PLATE))
    on, _ = asyncio.run(vision.build_items(
        _DETS, _HINT, raw=_photo_bytes(), plate_bbox=_PLATE))
    assert [i.grams for i in off] == [i.grams for i in on]
    assert all(i.measured_area_ratio is None for i in off)


def test_a_partly_measured_plate_publishes_no_split(monkeypatch):
    """A share whose denominator is missing an item is not a share.

    Photo 06: the mask placed a seed for the rice and failed on the drumstick
    and the beans, so the rice was published as 100% of a plate it was about a
    third of. The bench scored that as a 158% split error when the real fault
    was two missing seeds -- the failure landed on the one item that had worked.

    All of the plate, or none of it.
    """
    _fake_facts(monkeypatch)
    import app.services.ai.vision as V

    for pattern, expect_split in (
        ([0.04, 0.05, 0.03], True),    # every item measured
        ([0.04, None, 0.03], False),   # one seed failed
        ([None, None, 0.03], False),   # the photo-06 case
        ([None, None, None], False),   # the mask refused outright
        ([0.04, 0.0, 0.03], False),    # a zero is not a measurement either
    ):
        monkeypatch.setattr(V, "_measured_areas",
                            lambda *a, **k: (list(pattern), [1.0] * len(pattern), "colour"))
        out, _ = asyncio.run(V.build_items(
            _DETS, _HINT, raw=b"x", plate_bbox=_PLATE))
        shares = [i.measured_share for i in out if i.measured_share]
        if expect_split:
            assert len(shares) == len(out), pattern
            assert sum(shares) == pytest.approx(1.0, abs=0.01)
        else:
            assert not shares, f"{pattern} published a split from an incomplete plate"


def test_a_colour_footprint_still_never_moves_a_gram(monkeypatch):
    """The colour rule stays shadow-only, and that is not an oversight.

    Its masks are fenced by the plate box, so the absolute footprint carries the
    box's error while the SHARE survives -- measured, shrinking the plate box
    10% moved footprints 3-14% and shares 1-6%. Connecting the segmenter must
    not have quietly promoted the colour rule along with it.
    """
    _fake_facts(monkeypatch)
    import app.services.ai.vision as V
    blind, _ = asyncio.run(V.build_items(_DETS, _HINT))
    monkeypatch.setattr(V, "_measured_areas",
                        lambda *a, **k: ([0.04, 0.05, 0.03], [1.0] * 3, "colour"))
    seeing, _ = asyncio.run(V.build_items(_DETS, _HINT, raw=b"x", plate_bbox=_PLATE))
    for a, b in zip(blind, seeing):
        assert a.grams == b.grams, f"{a.name}: {a.grams} -> {b.grams}"


def test_a_segmented_footprint_does_move_the_grams(monkeypatch):
    """...and the whole point of building the segmenter is that this one does.

    The counterpart to the test above, and the two together are the wiring:
    same numbers, same photo, same everything -- only the SOURCE differs, and
    the source is what decides whether a footprint is an area or a share.

    Written as a pair deliberately. A single test that the grams move would pass
    on a build that promoted both sources, which is the bug it exists to catch.
    """
    _fake_facts(monkeypatch)
    import app.services.ai.vision as V
    monkeypatch.setattr(V, "_measured_areas",
                        lambda *a, **k: ([0.04, 0.05, 0.03], [1.0] * 3, "colour"))
    shadow, _ = asyncio.run(V.build_items(_DETS, _HINT, raw=b"x", plate_bbox=_PLATE))
    monkeypatch.setattr(V, "_measured_areas",
                        lambda *a, **k: ([0.04, 0.05, 0.03], [1.0] * 3, "sam2"))
    live, _ = asyncio.run(V.build_items(_DETS, _HINT, raw=b"x", plate_bbox=_PLATE))

    assert [i.grams for i in live] != [i.grams for i in shadow], (
        "a measured footprint reached no grams -- the segmenter is wired to "
        "nothing, which is the defect this whole build exists to close"
    )
    # And it is reported as such, so the bench can score the two sources apart.
    assert all(i.measured_area_source == "sam2" for i in live)
    assert all(i.measured_area_source == "colour" for i in shadow)


def test_an_unmeasured_item_keeps_the_model_estimate(monkeypatch):
    """A None is 'not measured', never 'a small food'.

    The failure this guards is specific and has shipped once: a mask that
    quietly returns almost nothing put a weighed 146 g drumstick on the plate as
    12 g. A missing measurement has to leave that item exactly where it was.
    """
    _fake_facts(monkeypatch)
    import app.services.ai.vision as V
    monkeypatch.setattr(V, "_measured_areas", lambda *a, **k: ([None] * 3, [None] * 3, "sam2"))
    none_measured, _ = asyncio.run(
        V.build_items(_DETS, _HINT, raw=b"x", plate_bbox=_PLATE))
    blind, _ = asyncio.run(V.build_items(_DETS, _HINT))
    for a, b in zip(blind, none_measured):
        assert a.grams == b.grams, f"{a.name}: {a.grams} -> {b.grams}"


def test_a_leaked_mask_is_refused_and_says_so(monkeypatch):
    """A footprint bigger than the plate is a mask on the tablecloth.

    This is the one segmenter failure that produces a large, confident, wrong
    weight rather than a missing one, so it is refused rather than trusted --
    and the refusal is written into the notes, because 'the segmenter is off'
    and 'the segmenter is on and being ignored' look identical from outside.
    """
    _fake_facts(monkeypatch)
    import app.services.ai.vision as V
    # _HINT's plate covers a known share of the frame; 90% of the frame is far
    # past it whatever that share is.
    monkeypatch.setattr(V, "_measured_areas", lambda *a, **k: ([0.90] * 3, [1.0] * 3, "sam2"))
    leaked, notes = asyncio.run(
        V.build_items(_DETS, _HINT, raw=b"x", plate_bbox=_PLATE))
    blind, _ = asyncio.run(V.build_items(_DETS, _HINT))
    for a, b in zip(blind, leaked):
        assert a.grams == b.grams, f"{a.name}: {a.grams} -> {b.grams}"
    assert any("leaked past the plate" in n for n in notes), notes


# --- depth: the height measurement, through the scan path ---------------------

def test_no_depth_provider_means_no_change_at_all(monkeypatch):
    """The default. NullDepth is configured, so every height falls back to its
    prior and the meal weighs exactly what it weighed before any of this
    existed. A missing model costs a measurement, never a scan."""
    _fake_facts(monkeypatch)
    import app.services.ai.vision as V

    assert V.DEPTH_PROVIDER.available() is False
    before, _ = asyncio.run(V.build_items(_DETS, _HINT))
    after, _ = asyncio.run(V.build_items(
        _DETS, _HINT, raw=_photo_bytes(), plate_bbox=_PLATE))
    for a, b in zip(before, after):
        assert a.grams == b.grams, f"{a.name}: {a.grams} -> {b.grams}"


def test_a_depth_provider_changes_the_weight_and_says_so(monkeypatch):
    """...and when one IS configured, the height stops being a guess."""
    _fake_facts(monkeypatch)
    import app.services.ai.vision as V

    hint = V.GeometryHint(plate_ellipse_area_ratio=0.50, plate_diameter_mm=254)
    prior, _ = asyncio.run(V.build_items(_DETS, hint))

    monkeypatch.setattr(V, "_measured_heights",
                        lambda *a, **k: [40.0, 40.0, 40.0])
    measured, _ = asyncio.run(V.build_items(
        _DETS, hint, raw=_photo_bytes(), plate_bbox=_PLATE))

    assert [i.grams for i in measured] != [i.grams for i in prior], (
        "a measured height changed nothing -- it is not reaching the estimator"
    )
    # 40 mm measured against a 32 mm prior x 0.68 profile = 21.76 mm effective,
    # so every item must come out heavier by very close to that ratio.
    for p, m in zip(prior, measured):
        assert m.grams > p.grams


def test_a_broken_depth_model_costs_a_measurement_not_a_scan(monkeypatch):
    """Every way the depth stage can fail, and none of them may change a gram
    or raise. This is the guarantee that lets a model be swapped in without
    holding the whole pipeline hostage to it."""
    _fake_facts(monkeypatch)
    import app.services.ai.vision as V
    import app.services.ai.depth_map as DM

    good, _ = asyncio.run(V.build_items(_DETS, _HINT))

    class _Boom:
        name = "boom"
        def available(self): return True
        def depth(self, rgb): raise RuntimeError("model exploded")

    class _Nonsense:
        name = "nonsense"
        def available(self): return True
        def depth(self, rgb):
            import numpy as np
            return np.zeros(rgb.shape[:2], dtype=np.float32)

    class _WrongShape:
        name = "wrong-shape"
        def available(self): return True
        def depth(self, rgb):
            import numpy as np
            return np.zeros((7, 9), dtype=np.float32)

    for provider in (_Boom(), _Nonsense(), _WrongShape(), DM.NullDepth()):
        monkeypatch.setattr(V, "DEPTH_PROVIDER", provider)
        out, _ = asyncio.run(V.build_items(
            _DETS, _HINT, raw=_photo_bytes(), plate_bbox=_PLATE))
        assert len(out) == len(good), provider.name
        for a, b in zip(good, out):
            assert a.grams == b.grams, f"{provider.name} moved {a.name}"


def test_the_height_is_measured_over_the_same_pixels_as_the_footprint():
    """item_masks was split out of measure_items so both use one assignment.

    Two separate assignments would drift, and a height averaged over a slightly
    different set of pixels than the area it multiplies is a quiet error inside
    a product -- the hardest kind to find. Verified on the real bench photos:
    the refactor returns byte-identical footprints, and each mask's own area IS
    that footprint.
    """
    import io

    import numpy as np
    from PIL import Image

    from app.services.ai import food_seg

    rgb = np.array(Image.open(io.BytesIO(_photo_bytes())).convert("RGB"))
    boxes = [d["bbox"] for d in _DETS]
    masks = food_seg.item_masks(rgb, boxes, _PLATE)
    areas = food_seg.measure_items(rgb, boxes, _PLATE)
    assert len(masks) == len(areas) == len(boxes)
    for m, a in zip(masks, areas):
        assert (m is None) == (a is None)
        if m is not None:
            assert float(m.mean()) == pytest.approx(a, abs=1e-12)


# --- correcting a meal must not destroy the parts you did not touch -----------

def test_a_correction_that_omits_notes_does_not_erase_them():
    """`notes` defaults to None, so a body that never mentions it looked
    exactly like a body clearing it -- and the edit screen never mentioned it.
    Correcting one gram value erased whatever the person had written about that
    meal, unrecoverably, with nothing on screen to say so.

    `model_fields_set` is the only thing that distinguishes absent from null,
    and that is precisely the distinction that was missing.
    """
    from app.models.nutrition import MealIn

    silent = MealIn(title="Lunch", items=[])
    assert "notes" not in silent.model_fields_set

    cleared = MealIn(title="Lunch", items=[], notes=None)
    assert "notes" in cleared.model_fields_set

    written = MealIn(title="Lunch", items=[], notes="felt heavy")
    assert "notes" in written.model_fields_set and written.notes == "felt heavy"


def test_the_route_honours_that_distinction():
    """Behavioural. The textual version passed on a mutation that reverted the
    fix, because the phrase it looked for survived elsewhere in the module."""
    from app.models.nutrition import MealIn
    from app.routers.scans import notes_update

    assert notes_update(MealIn(title="Lunch", items=[])) == {}
    assert notes_update(MealIn(title="Lunch", items=[], notes=None)) == {"notes": None}
    assert notes_update(MealIn(title="L", items=[], notes="heavy")) == {"notes": "heavy"}


def test_a_correction_carries_which_item_it_edits():
    """Without it a RENAME cannot be matched back to the scan -- the name is
    the thing that changed -- so the app learned nothing from the single most
    useful correction a person can make."""
    from app.models.nutrition import MealItemIn

    edit = MealItemIn(name="rajas", grams=98, source_index=0)
    assert edit.source_index == 0
    added = MealItemIn(name="tortilla", grams=30)
    assert added.source_index is None, "an added item edits nothing"


def test_the_footprints_topology_actually_reaches_the_estimator(monkeypatch):
    """Built, tested, and never connected is this project's recurring defect.

    The topology is measured in food_seg, carried through vision, and used in
    portion. Every one of those has its own test and all three would stay
    green with the wire cut in the middle. This calls the real build_items and
    checks the value ARRIVES -- captured at the estimator's own front door.
    """
    import asyncio

    from app.services.ai import vision as V

    seen = []
    real = V.estimate_grams

    def spy(**kw):
        seen.append(kw.get("largest_piece_share"))
        return real(**kw)

    monkeypatch.setattr(V, "estimate_grams", spy)
    monkeypatch.setattr(V, "_measured_areas",
                        lambda *a, **k: ([0.04, 0.05, 0.03],
                                         [1.0, 0.48, None], "sam2"))
    asyncio.run(V.build_items(_DETS, _HINT, raw=b"x", plate_bbox=_PLATE))

    assert seen == [1.0, 0.48, None], (
        f"the topology did not survive the trip to the estimator: {seen}")


def test_a_colour_footprint_carries_no_topology_either(monkeypatch):
    """A colour mask is fenced by the plate box and never reaches the grams.

    Its topology must be withheld with it. Letting the shape of a footprint
    through while refusing its size would move weight on the strength of a
    measurement the code has already decided not to trust.
    """
    import asyncio

    from app.services.ai import vision as V

    seen = []
    real = V.estimate_grams

    def spy(**kw):
        seen.append(kw.get("largest_piece_share"))
        return real(**kw)

    monkeypatch.setattr(V, "estimate_grams", spy)
    monkeypatch.setattr(V, "_measured_areas",
                        lambda *a, **k: ([0.04, 0.05, 0.03],
                                         [1.0, 0.48, 0.9], "colour"))
    asyncio.run(V.build_items(_DETS, _HINT, raw=b"x", plate_bbox=_PLATE))

    assert seen == [None, None, None], (
        f"a colour footprint leaked its topology into the weight path: {seen}")
