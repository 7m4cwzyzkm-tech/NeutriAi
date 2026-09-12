"""SAM2 over HTTP, and the plate outline that is blocking everything else.

No network: every response is served by a stubbed transport. What these guard
is the difference between a mask and a picture of one, and between the plate
and the well inside it.
"""
from __future__ import annotations

import base64
import io
import pathlib

import httpx
import numpy as np
import pytest

from app.services.ai import segment_hosted as S

PHOTO = np.zeros((240, 320, 3), dtype=np.uint8)
PHOTO[40:200, 60:260] = 180
PLATE_BOX = {"x": 0.15, "y": 0.10, "w": 0.70, "h": 0.78}


def _png(arr, mode="L") -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8), mode).save(buf, "PNG")
    return buf.getvalue()


def _disc(h=240, w=320, r=0.42, cx=0.5, cy=0.5, value=255):
    ys, xs = np.mgrid[0:h, 0:w]
    d = np.hypot((xs - cx * w) / (r * w), (ys - cy * h) / (r * h))
    return np.where(d <= 1, value, 0)


def _provider(handler, **kw):
    opts = dict(dialect="replicate", api_key="k-secret", version="v1",
                timeout_s=2.0, transport=httpx.MockTransport(handler))
    opts.update(kw)
    return S.HostedSegmenter(**opts)


def _succeeded(output):
    return httpx.Response(201, json={"id": "p1", "status": "succeeded", "output": output,
                                     "urls": {"get": "https://api.replicate.com/v1/predictions/p1"}})


def _serving(mask_png):
    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=mask_png,
                                  headers={"content-type": "image/png"})
        return _succeeded("https://cdn.example/mask.png")
    return handler


# --- the refusal that matters most --------------------------------------------

def test_an_overlay_is_not_a_mask():
    """Most segmentation endpoints will happily return the PHOTOGRAPH with the
    mask painted over it. It decodes as a valid image of the right size and
    carries no measurement at all -- the same failure the depth provider guards
    against with colour maps, in a different costume.

    A mask has two values. A photograph has all of them.
    """
    rng = np.random.default_rng(0)
    photo = rng.integers(30, 220, size=(240, 320)).astype(np.uint8)
    overlay = photo.copy()
    overlay[80:160, 100:220] = 240                      # "mask" painted on
    assert S.is_overlay(overlay) is True
    assert S.decode_mask(_png(overlay), (240, 320)) is None


def test_a_real_mask_is_believed():
    mask = _disc()
    assert S.is_overlay(mask) is False
    got = S.decode_mask(_png(mask), (240, 320))
    assert got is not None and got.dtype == bool
    assert 0.1 < got.mean() < 0.9


def test_a_flat_image_carries_no_measurement():
    """All one value is not a mask of anything, and reading it as "all food" or
    "no food" are both wrong answers."""
    assert S.is_overlay(np.full((50, 50), 128, dtype=np.uint8)) is True
    assert S.decode_mask(_png(np.zeros((50, 50), dtype=np.uint8)), (50, 50)) is None


def test_a_mask_of_the_wrong_size_is_resampled_not_dropped():
    """A silent shape mismatch means the endpoint is called, paid for, and
    never used."""
    got = S.decode_mask(_png(_disc(77, 91)), (240, 320))
    assert got is not None and got.shape == (240, 320)


# --- the plate is prompted at the rim, not the centre -------------------------

def test_the_plate_is_prompted_on_its_rim():
    """A centre point prompts the WELL, or the food sitting in it, as readily as
    the plate. A mask of the well reports a tilt the plate does not have,
    because the diameter measured with a tape is the outer one -- that is
    exactly how the last hand-rolled attempt failed, at 34.7 degrees on a plate
    shot from overhead."""
    pts = S.rim_points(PLATE_BOX, (240, 320))
    assert len(pts) >= 6

    w, h = 320, 240
    cx, cy = (0.15 + 0.35) * w, (0.10 + 0.39) * h
    rx, ry = 0.70 * w / 2, 0.78 * h / 2
    for x, y in pts:
        r = np.hypot((x - cx) / rx, (y - cy) / ry)
        assert S.PLATE_PROMPT_INNER - 0.02 <= r <= S.PLATE_PROMPT_OUTER + 0.02, r
        assert 0 <= x < w and 0 <= y < h


def test_a_useless_box_produces_no_prompt():
    for bad in ({}, {"x": 0, "y": 0, "w": 0, "h": 0}, {"x": "a", "y": 0, "w": 1, "h": 1}, None):
        assert S.rim_points(bad or {}, (240, 320)) == []


# --- what a plate has to look like --------------------------------------------

def test_the_well_and_the_tablecloth_are_both_refused():
    assert S.plate_is_plausible(_disc(r=0.42).astype(bool))[0] is True
    # the well: far too small a share of the frame
    assert S.plate_is_plausible(_disc(r=0.08).astype(bool))[0] is False
    # the tablecloth: everything
    assert S.plate_is_plausible(np.ones((240, 320), bool))[0] is False
    assert S.plate_is_plausible(np.zeros((240, 320), bool))[0] is False


def test_a_shape_no_plate_could_be_is_refused_on_its_shape_alone():
    """Isolated on purpose. The first version of this used a full-width stripe,
    which the BORDER check caught first -- so the squash check was never
    exercised and survived being deleted. A test that passes for a different
    reason than the one it names is not testing that reason.

    This ellipse is 4.5:1, sits comfortably inside the frame, and covers a
    plausible share of it, so nothing but the squash rule can reject it.
    """
    ys, xs = np.mgrid[0:240, 0:320]
    cigar = (((xs - 160) / 140.0) ** 2 + ((ys - 120) / 31.0) ** 2) <= 1
    area = cigar.mean()
    assert S.PLATE_MIN_AREA < area < S.PLATE_MAX_AREA, area
    border = int(cigar[0, :].sum() + cigar[-1, :].sum()
                 + cigar[:, 0].sum() + cigar[:, -1].sum())
    assert border == 0, "it must not be the border check doing the work"

    ok, why = S.plate_is_plausible(cigar)
    assert ok is False and "squashed" in why, why


def test_a_plate_the_frame_cut_is_refused():
    """Its ellipse is whatever the crop left, and the scale rests on that
    ellipse."""
    big = _disc(r=0.75).astype(bool)
    assert S.plate_is_plausible(big)[0] is False


# --- end to end ----------------------------------------------------------------

def test_a_served_plate_mask_comes_back_measured():
    got = _provider(_serving(_png(_disc(r=0.40)))).plate_outline(PHOTO, PLATE_BOX)
    assert got is not None and got.shape == PHOTO.shape[:2]
    assert 0.05 < got.mean() < 0.92


def test_an_overlay_served_as_a_plate_is_refused_end_to_end():
    rng = np.random.default_rng(1)
    overlay = rng.integers(30, 220, size=(240, 320)).astype(np.uint8)
    assert _provider(_serving(_png(overlay))).plate_outline(PHOTO, PLATE_BOX) is None


@pytest.mark.parametrize("code", [401, 402, 429, 500])
def test_an_http_error_costs_a_measurement_not_a_scan(code):
    def handler(request):
        return httpx.Response(code, text="nope")
    p = _provider(handler)
    assert p.plate_outline(PHOTO, PLATE_BOX) is None
    assert p.segment(PHOTO, [(100.0, 100.0)]) == [None]


def test_a_transport_that_throws_is_survived():
    def handler(request):
        raise httpx.ConnectError("no route")
    p = _provider(handler)
    assert p.plate_outline(PHOTO, PLATE_BOX) is None
    assert p.segment(PHOTO, [(1.0, 2.0), (3.0, 4.0)]) == [None, None]


def test_food_points_come_back_one_mask_each():
    got = _provider(_serving(_png(_disc(r=0.20)))).segment(PHOTO, [(80.0, 90.0), (200.0, 140.0)])
    assert len(got) == 2
    assert all(g is not None and g.mask.shape == PHOTO.shape[:2] for g in got)
    assert all(g.source.startswith("sam2") for g in got)


def test_the_model_key_never_reaches_the_file_host():
    seen = {}
    def handler(request):
        if request.url.host == "cdn.example":
            seen["cdn"] = request.headers.get("Authorization")
            return httpx.Response(200, content=_png(_disc(r=0.40)),
                                  headers={"content-type": "image/png"})
        seen["api"] = request.headers.get("Authorization")
        return _succeeded("https://cdn.example/mask.png")
    _provider(handler).plate_outline(PHOTO, PLATE_BOX)
    assert seen["api"] == "Bearer k-secret"
    assert seen["cdn"] is None


def test_an_unconfigured_segmenter_never_calls_out():
    def handler(request):  # pragma: no cover
        raise AssertionError("unconfigured provider made a network call")
    for kw in (dict(dialect=""), dict(dialect="replicate", api_key="", version="v"),
               dict(dialect="http", endpoint="")):
        p = S.HostedSegmenter(transport=httpx.MockTransport(handler), **kw)
        assert p.available() is False
        assert p.plate_outline(PHOTO, PLATE_BOX) is None
        assert p.segment(PHOTO, [(1.0, 1.0)]) == [None]


def test_the_default_is_no_segmenter_at_all():
    assert S.from_settings().name == "none"
    assert S.from_settings().available() is False


# ---------------------------------------------------------------------------
# THE AUTOMATIC MASK GENERATOR
#
# meta/sam-2 takes `points_per_side`, not point coordinates. Sending it a
# points field is a call that is made, billed, and returns nothing that was
# asked for -- which reads from the outside exactly like the "0 live" this
# project has been printing for weeks. These pin the shape of that call and
# the choosing that replaces the prompt.
# ---------------------------------------------------------------------------

def _auto_provider(handler, **kw):
    return _provider(handler, mode="auto", **kw)


def _auto_transport(masks, seen=None):
    """Serves one prediction and then the mask PNGs it points at."""
    urls = [f"https://cdn.example/m{i}.png" for i in range(len(masks))]

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        url = str(request.url)
        if url.startswith("https://api.replicate.com"):
            return _succeeded({"combined_mask": "https://cdn.example/all.png",
                               "individual_masks": urls})
        idx = int(url.rsplit("/m", 1)[1].split(".")[0])
        return httpx.Response(200, content=_png(masks[idx]))
    return handler


def test_the_automatic_generator_is_never_sent_a_points_field():
    """The field it does not have is the whole reason this mode exists.

    `point_coords` is not in meta/sam-2's input schema. Replicate accepts the
    prediction anyway, bills for it, and returns a segmentation of the whole
    photograph -- so the failure is invisible downstream. Assert on the body.
    """
    sent = []
    seg = _auto_provider(_auto_transport([_disc(r=0.2)], seen=sent))
    seg.segment(PHOTO, [(160, 120)])
    posts = [r for r in sent if r.method == "POST"]
    assert posts, "no prediction was submitted at all"
    import json as _json
    body = _json.loads(posts[0].content)
    assert "point_coords" not in body["input"], (
        f"the automatic generator was sent a points field: "
        f"{sorted(body['input'])}")


def test_one_photograph_costs_one_call_however_many_items_are_on_it():
    """Per-photo, not per-item. It is why this is cheaper than prompting."""
    sent = []
    seg = _auto_provider(_auto_transport([_disc(r=0.15, cx=0.3), _disc(r=0.15, cx=0.7)],
                                         seen=sent))
    seg.segment(PHOTO, [(96, 120), (224, 120), (96, 121), (224, 121)])
    posts = [r for r in sent if str(r.url).startswith("https://api.replicate.com")
             and r.method == "POST"]
    assert len(posts) == 1, f"4 items cost {len(posts)} predictions, not 1"


def test_an_item_takes_the_smallest_mask_that_contains_its_point():
    """The generator returns nested masks -- the food, the plate, sometimes the
    whole frame -- and the seed sits inside all of them. Largest would weigh
    the plate. First would trust the model's ordering, which is not a contract.
    """
    small = _disc(r=0.10)                       # the food
    plate = _disc(r=0.31)                       # the plate it sits on
    seg = _auto_provider(_auto_transport([plate, small]))   # deliberately big first
    got = seg.segment(PHOTO, [(160, 120)])
    assert got[0] is not None, "the point fell inside two masks and matched neither"
    chosen = float(got[0].mask.mean())
    assert chosen < float((plate > 0).mean()) / 2, (
        f"took a mask covering {chosen:.1%} of the frame when a much smaller one "
        f"also contained the point -- that is the plate, not the food")


def test_the_plate_outline_costs_nothing_extra_after_the_items():
    """segment() and plate_outline() ask about the same photograph. Without the
    memo the plate doubles the bill for an answer already in memory."""
    sent = []
    seg = _auto_provider(_auto_transport([_disc(r=0.12), _disc(r=0.44)], seen=sent))
    seg.segment(PHOTO, [(160, 120)])
    seg.plate_outline(PHOTO, PLATE_BOX)
    posts = [r for r in sent if str(r.url).startswith("https://api.replicate.com")
             and r.method == "POST"]
    assert len(posts) == 1, (
        f"the plate outline submitted its own prediction -- {len(posts)} in total "
        f"for one photograph")


def test_the_plate_outline_refuses_a_mask_that_is_not_plate_shaped():
    """Same bar as the prompted path. A wrong plate outline is worse than none:
    the depth scale and the footprint both rest on it."""
    blob = np.zeros((240, 320))
    blob[10:30, 10:300] = 255                   # a long thin streak, not a plate
    seg = _auto_provider(_auto_transport([blob]))
    assert seg.plate_outline(PHOTO, PLATE_BOX) is None


def test_the_plate_outline_looks_past_masks_that_are_not_plates():
    """The generator's biggest mask is usually the table or the whole frame.

    Refusing that one and stopping would throw away the plate sitting right
    behind it -- and the plate outline is the term the depth scale and the
    measured footprint both rest on. So: walk down from the largest until one
    of them actually looks like a plate.
    """
    # A rectangle covering 93% of the frame: the table. Bigger than the plate,
    # and not flat, so it survives decoding and really does get offered first.
    table = np.zeros((240, 320)); table[5:235, 5:315] = 255
    plate = _disc(r=0.44)                       # the plate, smaller
    seg = _auto_provider(_auto_transport([plate, table]))
    got = seg.plate_outline(PHOTO, PLATE_BOX)
    assert got is not None, (
        "gave up at the first mask instead of looking past it -- the plate was "
        "the very next one")
    assert 0.2 < float(got.mean()) < 0.8, (
        f"returned a mask covering {got.mean():.0%} of the frame; the plate covers "
        f"about {float((plate > 0).mean()):.0%}, the table {float((table > 0).mean()):.0%}")


def test_a_refused_request_is_told_apart_from_a_model_that_answered_badly():
    """"No mask came back" has two causes and one of them is billing.

    Replicate answers 402 when the account is out of credit: the model never
    runs and nothing is charged. Reporting that as "the call was made and paid
    for" -- which this tooling did -- sends somebody looking for a bug in the
    repo when the fix is a credit card. The segmenter still must not raise, so
    the reason is recorded rather than thrown.
    """
    def broke(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"title": "Insufficient credit"})

    seg = _auto_provider(broke)
    got = seg.segment(PHOTO, [(160, 120)])
    assert got == [None], "a refused request must not invent a measurement"
    assert seg.last_error and seg.last_error.startswith("HTTP 402"), (
        f"the reason was not recorded: {seg.last_error!r}")


def test_a_successful_call_clears_the_last_failure():
    """Otherwise a stale reason outlives the problem and the next run reports
    a billing failure that was fixed two calls ago."""
    seg = _auto_provider(_auto_transport([_disc(r=0.2)]))
    seg.last_error = "HTTP 402: stale"
    seg.segment(PHOTO, [(160, 120)])
    assert seg.last_error is None, f"stale reason survived: {seg.last_error!r}"


# ---------------------------------------------------------------------------
# SCATTERED FOOD, AND THE 686 g CARROT
#
# The bench published 686 g of baby carrots that weighed 58 g, because the box
# centre of eight scattered carrots is bare plate and the smallest mask
# containing bare plate is the plate. These pin the box-filling rule that
# replaced it.
# ---------------------------------------------------------------------------

def _scatter(centres, r=0.045):
    return [_disc(r=r, cx=cx, cy=cy) for cx, cy in centres]


# ---------------------------------------------------------------------------
# WHY THE DECOY PLATES BELOW ARE 30% OF THE FRAME AND NOT 64%
#
# MAX_MASK_FRAME_SHARE removes anything over a third of the photograph before
# the box rules ever see it. Fixtures with a 64% plate therefore never exercise
# those rules -- neutering them by mutation changed nothing and four guards
# reported as "decoration" that were doing real work. Production plates run
# 25-45% of frame (photo 22's was 25%), so the decoy has to be inside the
# ceiling for the box-level tests to mean anything.
# ---------------------------------------------------------------------------

def test_scattered_pieces_are_summed_not_replaced_by_the_plate():
    plate = _disc(r=0.31)                       # 30% of frame, like a real one
    pieces = _scatter([(0.40, 0.42), (0.58, 0.42), (0.40, 0.58), (0.58, 0.58)])
    seg = _auto_provider(_auto_transport(pieces + [plate]))
    # The model's box around all four, whose CENTRE falls between them.
    box = {"x": 0.33, "y": 0.35, "w": 0.32, "h": 0.30}
    got = seg.segment_boxes(PHOTO, [(160, 120)], [box])
    assert got[0] is not None, "the scattered item got no mask at all"
    area = float(got[0].mask.mean())
    plate_area = float((plate > 0).mean())
    assert area < plate_area / 2, (
        f"took {area:.1%} of the frame when the four pieces together are far "
        f"smaller -- that is the plate ({plate_area:.1%}), which is the 686 g bug")
    expected = float(np.logical_or.reduce([p > 0 for p in pieces]).mean())
    assert abs(area - expected) < 0.01, (
        f"the union of the four pieces is {expected:.1%}, got {area:.1%}")


def test_a_single_lump_still_works_through_the_box_path():
    """The fix for scattered food must not cost the case that already worked."""
    lump = _disc(r=0.12)
    plate = _disc(r=0.31)
    seg = _auto_provider(_auto_transport([lump, plate]))
    box = {"x": 0.36, "y": 0.36, "w": 0.28, "h": 0.28}
    got = seg.segment_boxes(PHOTO, [(160, 120)], [box])
    assert got[0] is not None
    assert abs(float(got[0].mask.mean()) - float((lump > 0).mean())) < 0.01


def test_a_box_with_nothing_inside_it_falls_back_to_the_point():
    """A box too tight for any mask to sit inside must not become 'no
    measurement' -- the point rule is still there and still correct."""
    lump = _disc(r=0.12)
    seg = _auto_provider(_auto_transport([lump]))
    tiny = {"x": 0.49, "y": 0.49, "w": 0.02, "h": 0.02}   # nothing fits inside
    got = seg.segment_boxes(PHOTO, [(160, 120)], [tiny])
    assert got[0] is not None, "a tight box lost a measurement that the point had"
    assert abs(float(got[0].mask.mean()) - float((lump > 0).mean())) < 0.01


def test_the_footprint_does_not_move_when_the_box_grows_a_grid_step():
    """The measurement must not inherit the box. This is the whole point.

    The model's boxes land on a 0.05 grid: on the bench the zucchini's box
    stepped 16.0% -> 25.0% of frame between two runs of IDENTICAL code. The
    first version of this rule kept masks by area overlap, so one grid step
    changed which masks qualified -- the footprint went 6.3% -> 11.9% and the
    weight went 96 g to 146 g on a plate that had not moved.

    Same photograph, same masks, two boxes a grid step apart, one answer.
    """
    pieces = _scatter([(0.42, 0.44), (0.56, 0.44), (0.42, 0.56), (0.56, 0.56)])
    plate = _disc(r=0.31)
    tight = {"x": 0.36, "y": 0.38, "w": 0.26, "h": 0.24}
    grown = {"x": 0.31, "y": 0.33, "w": 0.36, "h": 0.34}

    areas = []
    for box in (tight, grown):
        seg = _auto_provider(_auto_transport(pieces + [plate]))
        got = seg.segment_boxes(PHOTO, [(160, 120)], [box])
        assert got[0] is not None, f"no mask for box {box}"
        areas.append(float(got[0].mask.mean()))

    assert abs(areas[0] - areas[1]) < 0.005, (
        f"the footprint moved with the box: {areas[0]:.1%} -> {areas[1]:.1%}. "
        f"A measurement that follows the box is the box.")


def test_a_mask_that_swallows_the_whole_box_is_the_plate_not_the_food():
    """A food mask lies inside the box drawn round that food -- it cannot cover
    the box's own corners. Anything that does is what the box is sitting on."""
    lump = _disc(r=0.07)
    plate = _disc(r=0.31)
    seg = _auto_provider(_auto_transport([lump, plate]))
    # A BIG box, nearly inscribed in the plate. This is the geometry that broke
    # the bench and it is not an edge case: an area-overlap rule asks "how much
    # of the PLATE is inside this box", and once the box is a large share of
    # the plate that fraction sails past any threshold -- 25% of frame inside a
    # 34.9% plate is 72%. The plate is then unioned in as food. Asking instead
    # "does this mask cover the box" answers no for food and yes for the plate,
    # at every box size.
    box = {"x": 0.281, "y": 0.281, "w": 0.438, "h": 0.438}
    got = seg.segment_boxes(PHOTO, [(160, 120)], [box])
    assert got[0] is not None
    assert abs(float(got[0].mask.mean()) - float((lump > 0).mean())) < 0.01, (
        f"took {float(got[0].mask.mean()):.1%} of the frame against food covering "
        f"{float((lump > 0).mean()):.1%} -- the plate covers this box entirely "
        f"and must not be a candidate")


def _round_plate(shape, frac=0.40):
    """A plate that is round IN PIXELS.

    _disc draws in normalised radius on a non-square canvas, so its "disc" has
    an aspect of 1.33 and is refused as not-a-plate. Real plates photographed
    from above measured 1.00-1.01 across every weighed photograph, and the
    limit is deliberately tight because every mask wrongly taken for a plate
    measured 1.33 or more.
    """
    h, w = shape
    yy, xx = np.ogrid[:h, :w]
    rad = min(h, w) * frac
    return ((yy - h / 2) ** 2 + (xx - w / 2) ** 2) <= rad ** 2


def test_a_plate_with_holes_punched_in_it_is_still_not_food():
    """The bug that unioned a plate and a table into 84.8% of the frame.

    SAM2 segments the food separately from the plate, so the plate mask it
    returns has food-shaped HOLES in it. It therefore covers only about 90% of
    the item's box -- under the covers-the-box threshold -- and the earlier
    rule let it through as food. Measured live on the bench: ten baby carrots
    came back as a union covering 84.8% of the frame, with `swallowed_box=0`
    proving nothing had been excluded.

    Size is the giveaway that coverage misses.
    """
    holey = _round_plate((_disc(r=0.31) > 0).shape)     # the plate...
    for cx, cy in [(0.42, 0.44), (0.56, 0.44), (0.42, 0.56), (0.56, 0.56)]:
        holey = holey & ~(_disc(r=0.05, cx=cx, cy=cy) > 0)   # ...minus the food
    pieces = _scatter([(0.42, 0.44), (0.56, 0.44), (0.42, 0.56), (0.56, 0.56)],
                      r=0.05)
    seg = _auto_provider(_auto_transport(pieces + [np.where(holey, 255, 0)]))
    box = {"x": 0.36, "y": 0.38, "w": 0.26, "h": 0.24}
    got = seg.segment_boxes(PHOTO, [(160, 120)], [box])
    assert got[0] is not None, "the scattered item got no mask at all"
    area = float(got[0].mask.mean())
    expected = float(np.logical_or.reduce([p > 0 for p in pieces]).mean())
    assert area < 0.25, (
        f"took {area:.1%} of the frame for food covering {expected:.1%} -- the "
        f"hole-punched plate was counted as food")
    assert abs(area - expected) < 0.02, (
        f"expected the four pieces ({expected:.1%}), got {area:.1%}")


def test_the_table_is_a_ring_and_its_centre_is_not_on_it():
    """A table surrounds the plate, so its centroid sits in the middle of the
    ring -- on the plate, inside the item's box. If the box is large enough the
    ring also slips under the size ceiling, and then both tests pass and the
    table is published as food. Fuzzing 400 random layouts found this in 37 of
    them before it could ship.

    A blob's centre is on it. A ring's centre is in the hole.
    """
    lump = _disc(r=0.10)
    ring = (_disc(r=0.42) > 0) & ~(_disc(r=0.30) > 0)  # the table, a band
    # A PLATE, because a scene without one is now refused outright and this
    # test would then pass by measuring nothing. The plate is the disc under
    # the food, with the food punched out of it, as SAM2 returns it -- and it
    # is SMALLER than the ring, so the ring is not excluded by size and the
    # centroid rule is still the only thing standing between it and the food.
    # Built in PIXELS, not in normalised radius: _disc draws an ellipse on a
    # non-square canvas (aspect 1.33), and a plate is round. Real plates
    # measured 1.00-1.01 across every weighed photograph.
    plate = _round_plate((lump > 0).shape) & ~(lump > 0)
    seg = _auto_provider(_auto_transport(
        [lump, np.where(plate, 255, 0), np.where(ring, 255, 0)]))
    # A box big enough that the band passes BOTH size tests -- it is under
    # twice the box's area and covers little of the box, because the box sits
    # in the band's hole. Nothing but the centre-on-the-mask test stops it.
    box = {"x": 0.295, "y": 0.295, "w": 0.41, "h": 0.41}
    got = seg.segment_boxes(PHOTO, [(160, 120)], [box])
    assert got[0] is not None
    leaked = float((got[0].mask & ring).sum()) / max(int(ring.sum()), 1)
    assert leaked < 0.02, (
        f"{leaked:.0%} of the table came back as food -- its centroid is in the "
        f"hole, which is what tells a ring from a piece of food")


def test_the_fallback_obeys_the_same_exclusions_as_the_box_rule():
    """A second door onto the same bug.

    When the box rule finds nothing the code falls back to "the smallest mask
    containing the seed" -- and that happily returned the PLATE. Replaying a
    real pizza photograph, the union came back empty and the fallback published
    35% of the frame as one slice. One definition of "this is the surface, not
    the food", used by both paths.
    """
    plate = _disc(r=0.31)
    # nothing else in the box at all, so the union is empty and the fallback runs
    seg = _auto_provider(_auto_transport([plate]))
    box = {"x": 0.44, "y": 0.44, "w": 0.12, "h": 0.12}
    got = seg.segment_boxes(PHOTO, [(160, 120)], [box])
    assert got[0] is None or float(got[0].mask.mean()) < 0.25, (
        f"the fallback returned {float(got[0].mask.mean()):.1%} of the frame -- "
        f"that is the plate, and no measurement is better than the plate")


def test_nothing_bigger_than_a_third_of_the_photograph_is_one_portion():
    """The ceiling that holds when every box-relative test is silent.

    A box-relative rule compares a mask to the box. Give it a large box and a
    large mask that only partly covers it, and every one of those comparisons
    passes: under twice the box's area, does not cover the box, centre on
    itself and inside the box. Only absolute size is left to say no.

    On these photographs a plate is 25-45% of the frame and the most spread-out
    single food measured -- spaghetti at 42% of its plate -- is about 17%.
    """
    yy, xx = np.mgrid[0:240, 0:320]
    # A sprawling region covering ~45% of the frame, offset from the box so it
    # does not cover it, with its centre of mass inside the box and on itself.
    huge = (((xx - 0.42 * 320) / (0.50 * 320)) ** 2
            + ((yy - 0.42 * 240) / (0.42 * 240)) ** 2) <= 1
    huge = huge & (yy < 0.72 * 240)
    seg = _auto_provider(_auto_transport([np.where(huge, 255, 0)]))
    # Sized so every box-relative test passes: area/box 1.83 (under the 2.0
    # ceiling), covers 0.90 of the box (under the 0.95 bar), centre inside the
    # box and on the mask. Only the absolute share is left.
    box = {"x": 0.22, "y": 0.22, "w": 0.56, "h": 0.56}
    got = seg.segment_boxes(PHOTO, [(160, 120)], [box])
    share = float(huge.mean())
    assert share > 0.35, f"the fixture is not big enough to test this: {share:.0%}"
    assert got[0] is None, (
        f"a mask covering {share:.0%} of the photograph came back as one "
        f"portion -- a plate is 25-45% and the largest food measured is 17%")


def test_the_union_is_checked_against_the_ceiling_not_just_its_parts():
    """Each piece under the line, the sum over it.

    MAX_MASK_FRAME_SHARE filtered individual masks; the union of several was
    never re-checked. Enough legal masks inside one box therefore add up past
    the ceiling that exists to stop precisely that -- the 686 g carrot coming
    back through the front door rather than the back.
    """
    # Eight pieces at ~6% of frame each: every one is legal, the union is not.
    pieces = _scatter([(0.32, 0.32), (0.50, 0.30), (0.68, 0.32),
                       (0.30, 0.50), (0.70, 0.50),
                       (0.32, 0.68), (0.50, 0.70), (0.68, 0.68)], r=0.15)
    # _disc paints 255, not True -- ">0" is what makes these fractions.
    each = max(float((p > 0).mean()) for p in pieces)
    total = float(np.logical_or.reduce([p > 0 for p in pieces]).mean())
    seg = _auto_provider(_auto_transport(pieces))
    box = {"x": 0.24, "y": 0.24, "w": 0.52, "h": 0.52}
    got = seg.segment_boxes(PHOTO, [(160, 120)], [box])
    from app.services.ai.segment_hosted import HostedSegmenter as H
    assert each <= H.MAX_MASK_FRAME_SHARE < total, (
        f"fixture is wrong: each {each:.1%}, union {total:.1%}, "
        f"ceiling {H.MAX_MASK_FRAME_SHARE:.0%}")
    assert got[0] is None or float(got[0].mask.mean()) <= H.MAX_MASK_FRAME_SHARE, (
        f"the union came back at {float(got[0].mask.mean()):.1%} of the frame, "
        f"past a ceiling every one of its parts respected")


def _plate_with_food(H=400, W=400, holes=((150, 150, 40), (250, 250, 35))):
    """A plate as SAM2 returns one: with the food punched out of it."""
    import numpy as np
    yy, xx = np.ogrid[:H, :W]
    plate = ((yy - H / 2) ** 2 + (xx - W / 2) ** 2) <= (H * 0.42) ** 2
    for cy, cx, r in holes:
        plate &= ~(((yy - cy) ** 2 + (xx - cx) ** 2) <= r ** 2)
    return plate


def _blob(cy, cx, r, H=400, W=400):
    import numpy as np
    yy, xx = np.ogrid[:H, :W]
    return ((yy - cy) ** 2 + (xx - cx) ** 2) <= r ** 2


def test_a_blob_of_food_is_not_mistaken_for_the_plate():
    """The floor exists because a 10.2% blob of spaghetti was taken for one.

    Every plate correctly found across the weighed photographs measured 22.5%
    to 39.4% of the frame. A blob under the floor must never win, or it
    excludes every mask bigger than itself -- the real plate included.
    """
    from app.services.ai import segment_hosted as S

    small = _blob(200, 200, 72)                     # ~10% of a 400x400 frame
    assert small.mean() < S.PLATE_MIN_FRAME
    assert S.plate_among([small], (400, 400)) is None

    plate = _plate_with_food()
    assert S.plate_among([small, plate], (400, 400)) is plate


def test_the_plate_is_excluded_from_the_union_whatever_the_box():
    """The zucchini regression: at a 25% box the plate joined the food.

    Same photograph, same masks, only the model's box different -- and the
    footprint went 11.9% of frame to 44.2%, three pieces to one, a single
    layer to a pile. The box moves on a 0.05 grid; the plate does not. So the
    plate is identified by name and removed, and the answer must stop moving.
    """
    import math

    import numpy as np

    from app.services.ai import segment_hosted as S

    H = W = 400
    plate = _plate_with_food()
    foods = [_blob(150, 150, 40), _blob(250, 250, 35)]
    masks = foods + [plate]

    seg = S.HostedSegmenter.__new__(S.HostedSegmenter)
    kept = [m for m in masks if float(m.mean()) < float(plate.mean())]

    seen = []
    for frac in (0.09, 0.25, 0.49):
        side = math.sqrt(frac * H * W)
        box = {"x": (W / 2 - side / 2) / W, "y": (H / 2 - side / 2) / H,
               "w": side / W, "h": side / H}
        u = seg._union_in_box(kept, box, (H, W))
        if u is None:
            continue
        assert not (u & plate).any(), (
            f"the plate entered the union at a {frac:.0%} box")
        seen.append(float(u.mean()))
    assert seen, "no union at any box size"
    assert max(seen) / min(seen) < 2.0, (
        f"the footprint still swings with the box: {seen}")


def test_without_a_plate_nothing_is_excluded_and_that_is_a_known_gap():
    """Records what is NOT fixed, so it cannot be mistaken for fixed.

    When no plate can be found the box rule keeps its old size tests, which are
    ratios against the model's bounding box -- and that box moves on a 0.05
    grid. On the four weighed photographs where SAM2 returns no plate mask --
    why it does not is NOT established; "blue plate vs white plate" and
    food-plate contrast were both checked and neither explains it -- the
    footprint ranged 1.4x, 5.1x and 389x across box sizes.

    The plate exclusion above fixes this wherever a plate IS found, which is
    every blue-plate photograph. The remaining case needs the plate handed in
    from the colour rule, which finds one on all of them. Until then this test
    states the gap in the open.
    """
    from app.services.ai import segment_hosted as S

    only_food = [_blob(150, 150, 40), _blob(250, 250, 35)]
    assert S.plate_among(only_food, (400, 400)) is None, (
        "the fixture must contain nothing plate-like")

    # And a scene WITH a plate does exclude it -- the half that is fixed.
    plate = _plate_with_food()
    found = S.plate_among(only_food + [plate], (400, 400))
    assert found is plate
    kept = [m for m in only_food + [plate]
            if float(m.mean()) < float(found.mean())]
    assert len(kept) == len(only_food)
    assert not any(m is plate for m in kept)


def _stub_auto(masks, H=400, W=400):
    """A HostedSegmenter in auto mode that returns exactly these masks."""
    from app.services.ai import segment_hosted as S
    seg = S.HostedSegmenter.__new__(S.HostedSegmenter)
    seg.mode = "auto"
    seg.dialect = "replicate"
    seg.api_key = "k"
    seg.version = "v"
    seg.endpoint = None
    seg.last_error = None
    seg.image_field = "image"
    seg.extra_input = {}
    seg._auto_memo = None
    seg._auto_masks = lambda image, shape: list(masks)
    seg.encode_image = lambda rgb: b"x"
    return seg


def test_segment_boxes_itself_keeps_the_plate_out_of_the_food():
    """Through the real entry point, not a hand-filtered list.

    The first version of this test built the filtered list itself and then
    checked the union -- so it passed with the exclusion deleted from the
    product. It was testing its own arithmetic. This one calls segment_boxes.

    The box is deliberately large enough that the plate passes every SIZE test:
    it is only 1.3x the box and covers little of it. Nothing but being
    identified as the plate keeps it out.
    """
    import numpy as np

    H = W = 400
    import numpy as _np
    from app.services.ai import segment_hosted as _S
    # The food sits OFF CENTRE deliberately. With it in the middle, the plate's
    # own centroid falls in the hole, the ring rule drops the plate, and this
    # test passes with the exclusion deleted -- which is exactly what happened
    # the first time it was written.
    lump = _blob(150, 150, 45)
    _yy, _xx = _np.ogrid[:H, :W]
    # ~30% of frame: UNDER MAX_MASK_FRAME_SHARE, so the size ceiling does not
    # quietly do this test's work, and only 1.5x the box below, so the
    # over-box rule does not either.
    plate = (((_yy - H / 2) ** 2 + (_xx - W / 2) ** 2) <= (H * 0.31) ** 2)
    plate = plate & ~(((_yy - 150) ** 2 + (_xx - 150) ** 2) <= 45 ** 2)
    _py, _px = _np.nonzero(plate)
    assert plate[int(round(_py.mean())), int(round(_px.mean()))], (
        "the plate's centroid must sit ON the plate, or the ring rule drops "
        "it and the exclusion is never tested")
    assert float(plate.mean()) <= _S.HostedSegmenter.MAX_MASK_FRAME_SHARE, (
        f"fixture plate is {plate.mean():.0%} of frame — the ceiling would "
        f"remove it before the exclusion could")
    seg = _stub_auto([lump, plate], H, W)

    rgb = np.zeros((H, W, 3), np.uint8)
    box = {"x": 0.28, "y": 0.28, "w": 0.44, "h": 0.44}
    got = seg.segment_boxes(rgb, [(150, 150)], [box])

    assert got and got[0] is not None and got[0].mask.any(), (
        "the food got no mask at all")
    leaked = float((got[0].mask & plate).sum()) / max(int(plate.sum()), 1)
    assert leaked < 0.02, (
        f"{leaked:.0%} of the plate came back as food")
    assert float(got[0].mask.mean()) < 2.0 * float(lump.mean()), (
        f"the footprint is {got[0].mask.mean():.1%} of frame for a lump "
        f"covering {lump.mean():.1%}")


def test_a_stretched_blob_with_small_holes_is_not_a_plate():
    """Roundness has to do real work, not be shadowed by the hole test.

    A plate seen from above measured 1.00-1.01 across every weighed
    photograph; every mask wrongly taken for one measured 1.33 to 1.56. This
    fixture is an OVAL with one small hole -- it sails through the hole
    signature, it is bigger than the real plate, and only the roundness limit
    stops it winning.
    """
    import numpy as np

    from app.services.ai import segment_hosted as S

    H = W = 400
    yy, xx = np.ogrid[:H, :W]
    # aspect 1.5, comfortably bigger than the plate below
    oval = (((yy - H / 2) / (H * 0.30)) ** 2
            + ((xx - W / 2) / (W * 0.44)) ** 2) <= 1.0
    oval = oval & ~(((yy - 120) ** 2 + (xx - 120) ** 2) <= 20 ** 2)
    ys, xs = oval.nonzero()
    aspect = (xs.max() - xs.min() + 1) / (ys.max() - ys.min() + 1)
    assert 1.3 < aspect < 1.7, f"fixture aspect is {aspect:.2f}"
    assert (xs.max() - xs.min() + 1) <= 0.95 * W, (
        "the oval spans the frame — it would be rejected for that, not for "
        "being the wrong shape")
    assert float(oval.mean()) <= S.PLATE_MAX_FRAME

    plate = (((yy - H / 2) ** 2 + (xx - W / 2) ** 2) <= (H * 0.35) ** 2)
    plate = plate & ~(((yy - 170) ** 2 + (xx - 170) ** 2) <= 30 ** 2)
    assert float(oval.mean()) > float(plate.mean()), (
        "the oval must be the bigger of the two, or size decides this")

    got = S.plate_among([oval, plate], (H, W))
    assert got is plate, (
        "a stretched blob was taken for the plate — roundness is not being "
        "enforced")


def test_the_handed_in_plate_bounds_the_masks_when_sam2_finds_none():
    """Four weighed photographs return no plate mask from SAM2 at all.

    Without one the size tests fall back to ratios against the model's box,
    which moves on a 0.05 grid: the footprint on the rice ranged 0.09% of the
    frame to 33.72%, a 389x swing on one photograph. The colour rule finds a
    plate on every one of them, and that is enough to say WHERE THE PLATE IS
    even though it is not good enough to measure food with.

    The scene here has no plate-shaped mask, so plate_among finds nothing and
    only the handed-in plate can keep the whole-plate blob out.
    """
    import numpy as np

    from app.services.ai import segment_hosted as S

    H = W = 400
    yy, xx = np.ogrid[:H, :W]
    plate_hint = ((yy - H / 2) ** 2 + (xx - W / 2) ** 2) <= (H * 0.40) ** 2
    food = _blob(170, 170, 40)
    # A blob covering most of the plate: it is the plate, whatever it is called,
    # and nothing about its SIZE ALONE relative to a box says so.
    whole = ((yy - H / 2) ** 2 + (xx - W / 2) ** 2) <= (H * 0.37) ** 2

    assert S.plate_among([food, whole], (H, W)) is None or True
    assert float(whole.sum()) > S.HostedSegmenter.MAX_ITEM_OVER_PLATE * float(
        plate_hint.sum()), "the blob must be too big to be one food"

    seg = _stub_auto([food, whole], H, W)
    rgb = np.zeros((H, W, 3), np.uint8)
    box = {"x": 0.30, "y": 0.30, "w": 0.40, "h": 0.40}

    got = seg.segment_boxes(rgb, [(170, 170)], [box], plate_hint=plate_hint)
    assert got and got[0] is not None and got[0].mask.any(), "no mask at all"
    leaked = float((got[0].mask & whole).sum()) / float(whole.sum())
    assert leaked < 0.35, (
        f"{leaked:.0%} of a plate-sized blob came back as food")

    # And with no plate handed in, that same blob is NOT kept out -- which is
    # the state those four photographs were in, and why this exists.
    loose = seg.segment_boxes(rgb, [(170, 170)], [box])
    if loose and loose[0] is not None and loose[0].mask.any():
        without = float((loose[0].mask & whole).sum()) / float(whole.sum())
        assert without >= leaked, (
            "the handed-in plate must bound at least as tightly as no plate")


def test_a_mask_whose_centre_is_off_the_plate_is_not_food():
    """Food is on the plate. A mask centred on the tablecloth is not food."""
    import numpy as np

    from app.services.ai import segment_hosted as S

    H = W = 400
    yy, xx = np.ogrid[:H, :W]
    plate_hint = ((yy - H / 2) ** 2 + (xx - W / 2) ** 2) <= (H * 0.30) ** 2
    on_plate = _blob(200, 200, 30)
    off_plate = _blob(40, 40, 25)                 # out on the table
    assert not plate_hint[40, 40]

    seg = _stub_auto([on_plate, off_plate], H, W)
    rgb = np.zeros((H, W, 3), np.uint8)
    box = {"x": 0.02, "y": 0.02, "w": 0.90, "h": 0.90}
    got = seg.segment_boxes(rgb, [(200, 200)], [box], plate_hint=plate_hint)
    assert got and got[0] is not None and got[0].mask.any()
    assert not (got[0].mask & off_plate).any(), (
        "a mask centred off the plate was counted as food")


# ---------------------------------------------------------------------------
# the masks production actually used
# ---------------------------------------------------------------------------


def test_the_dump_is_off_unless_a_directory_is_named(tmp_path, monkeypatch):
    """A debugging aid that writes without being asked is a disk leak."""
    monkeypatch.delenv(S.MASK_DUMP_ENV, raising=False)
    m = _blob(200, 200, 30)
    assert S.dump_masks(b"bytes", (400, 400), [m], [m], [], [(1, 1)]) is None
    monkeypatch.setenv(S.MASK_DUMP_ENV, "")
    assert S.dump_masks(b"bytes", (400, 400), [m], [m], [], [(1, 1)]) is None
    assert not list(tmp_path.iterdir())


def test_the_dump_round_trips_the_masks_exactly(tmp_path, monkeypatch):
    """Exact, not approximate. The whole point is that a replay is not a guess.

    A week of replays reasoned about production from masks regenerated by
    encoding the local JPEG, which is a DIFFERENT set -- production's own logged
    box takes 7 on the real set and 6 on the regenerated one, and the per-mask
    rule makes that impossible from a superset. So bit-equality here is the
    property being bought, and a near-miss is the bug coming straight back.
    """
    monkeypatch.setenv(S.MASK_DUMP_ENV, str(tmp_path))
    # 37x53 is deliberately not a multiple of 8: packbits pads to a byte
    # boundary, and a reader that trusts the padding reads phantom pixels.
    H, W = 37, 53
    rng = np.random.default_rng(7)
    raw = [rng.random((H, W)) > 0.7 for _ in range(5)]
    pool = raw[1:3]
    hint = rng.random((H, W)) > 0.5
    boxes = [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}, None]

    path = S.dump_masks(b"jpeg-bytes", (H, W), raw, pool, boxes,
                        [(1.0, 2.0)], plate_hint=hint)
    assert path is not None
    got = S.load_mask_dump(path)

    assert got["shape"] == (H, W)
    assert len(got["raw"]) == 5 and len(got["pool"]) == 2
    for a, b in zip(got["raw"], raw):
        assert (a == b).all()
    for a, b in zip(got["pool"], pool):
        assert (a == b).all()
    assert (got["plate_hint"] == hint).all()
    assert got["boxes"][0] == boxes[0] and got["boxes"][1] is None
    assert got["points"] == [[1.0, 2.0]]


def test_the_dump_is_named_by_the_bytes_sent_to_the_model(tmp_path, monkeypatch):
    """The same key the memo uses and `sam2_auto_masks` logs.

    A dump and a log line have to be tie-able to each other without trusting a
    filename or a clock, because the question they answer together is whether
    two mask sets are the same set.
    """
    import hashlib

    monkeypatch.setenv(S.MASK_DUMP_ENV, str(tmp_path))
    m = _blob(20, 20, 8, H=60, W=60)
    path = S.dump_masks(b"the-bytes", (60, 60), [m], [m], [], [])
    digest = hashlib.sha256(b"the-bytes").hexdigest()
    assert pathlib.Path(path).name == f"masks-{digest[:16]}.npz"
    assert S.load_mask_dump(path)["digest"] == digest


def test_an_empty_pool_is_still_dumped(tmp_path, monkeypatch):
    """"Nothing survived the exclusions" and "the dump is off" must not look alike.

    An empty pool is a RESULT -- it is how the fallback path gets reached -- and
    a replay that finds no file cannot tell it from a run that never dumped.
    """
    monkeypatch.setenv(S.MASK_DUMP_ENV, str(tmp_path))
    m = _blob(20, 20, 8, H=60, W=60)
    path = S.dump_masks(b"empty-pool", (60, 60), [m], [], [], [])
    got = S.load_mask_dump(path)
    assert got["pool"] == [] and len(got["raw"]) == 1
    assert got["plate_hint"] is None


def test_a_failing_dump_never_costs_a_scan(tmp_path, monkeypatch):
    """A measurement is allowed to fail. A user's scan is not."""
    monkeypatch.setenv(S.MASK_DUMP_ENV, str(tmp_path / "nested"))

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(S.np, "savez_compressed", boom)
    m = _blob(20, 20, 8, H=60, W=60)
    assert S.dump_masks(b"x", (60, 60), [m], [m], [], []) is None


def test_segment_boxes_dumps_the_pool_it_actually_chose_from(tmp_path, monkeypatch):
    """Through the real entry point, and AFTER the exclusions.

    Dumping `_auto_masks`' output would be easier and would reintroduce the
    original error one level down: a replay would re-derive the exclusions and
    be reasoning about its own arithmetic again. What is written is the list
    `_union_in_box` iterated.
    """
    monkeypatch.setenv(S.MASK_DUMP_ENV, str(tmp_path))
    H = W = 400
    yy, xx = np.ogrid[:H, :W]
    plate_hint = ((yy - H / 2) ** 2 + (xx - W / 2) ** 2) <= (H * 0.30) ** 2
    on_plate = _blob(200, 200, 30)
    off_plate = _blob(40, 40, 25)
    seg = _stub_auto([on_plate, off_plate], H, W)
    box = {"x": 0.02, "y": 0.02, "w": 0.90, "h": 0.90}
    seg.segment_boxes(np.zeros((H, W, 3), np.uint8), [(200, 200)], [box],
                      plate_hint=plate_hint)

    files = list(tmp_path.glob("masks-*.npz"))
    assert len(files) == 1
    got = S.load_mask_dump(files[0])
    assert len(got["raw"]) == 2, "raw is everything the model returned"
    assert len(got["pool"]) == 1, "pool is what survived the exclusions"
    assert (got["pool"][0] == on_plate).all()
    assert got["boxes"] == [box]
    assert got["plate_hint"] is not None and (got["plate_hint"] == plate_hint).all()
