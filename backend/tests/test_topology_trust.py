"""THE TOPOLOGY INVERSION (docs/HANDOFF.md): the area guard's own
fragmentation read now also gates whether the topology branch's confident
CONNECTED_PILE/SEPARATE_PIECES call is trusted at all -- direction 2 from
the doc's own two stated options, not a move of ONE_PIECE_SHARE (ruled out
in the doc) and not a new, independently-tuned threshold (the class of fix
this task's report explains was rejected and why).
"""
from pathlib import Path

import numpy as np
import pytest

from app.services.ai.portion import (
    CONNECTED_PILE_HEIGHT_MM, MEASURED_UNDER_BOX_LIMIT,
    SEPARATE_PIECES_HEIGHT_MM, TOPOLOGY_TRUST_SHRINK_LIMIT, GeometryHint,
    estimate_grams,
)
from app.services.ai.food_seg import ONE_PIECE_SHARE, largest_piece_share
from app.services.ai.segment_hosted import load_mask_dump
from scripts.box_replay import MAX_MASK_FRAME_SHARE, _prefilter

HINT = GeometryHint(plate_ellipse_area_ratio=0.35, plate_diameter_mm=229)
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_branch_constants_are_untouched():
    """This task's hard rule: gate which topologies reach the branch, never
    retune the branch's own numbers."""
    assert ONE_PIECE_SHARE == pytest.approx(0.80)
    assert CONNECTED_PILE_HEIGHT_MM == pytest.approx(21.0)
    assert SEPARATE_PIECES_HEIGHT_MM == pytest.approx(9.2)


def test_trust_limit_is_derived_not_a_fresh_guess():
    """TOPOLOGY_TRUST_SHRINK_LIMIT is a margin on the EXISTING, already-
    calibrated MEASURED_UNDER_BOX_LIMIT (80% of it), not a second
    independently-chosen number -- see its own comment for why a fresh
    threshold here would repeat ONE_PIECE_SHARE's mistake."""
    assert TOPOLOGY_TRUST_SHRINK_LIMIT == pytest.approx(MEASURED_UNDER_BOX_LIMIT * 0.8)


def test_carrots_shrink_stays_trusted():
    """Regression control: MEASURED_UNDER_BOX_LIMIT's own comment records
    carrots at 3.3x shrink as the one documented CORRECT high-shrink case --
    genuinely scattered pieces, not a broken mask. This fix must not
    exclude it. own_box=0.16, measured_area_ratio=0.048 -> shrink 3.33x."""
    result = estimate_grams(
        name="steamed carrots", area_ratio=0.048, hint=HINT,
        measured_area_ratio=0.048, bbox={"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4},
        detection_confidence=0.8, largest_piece_share=0.50,
    )
    layer = estimate_grams(
        name="steamed carrots", area_ratio=0.048, hint=HINT,
        measured_area_ratio=0.048, detection_confidence=0.8,
        largest_piece_share=0.50,
    )
    # Same conclusion (separate pieces) whether or not the box-derived shrink
    # is available -- carrots must not be the case this fix silently changes.
    assert result.grams == pytest.approx(layer.grams, rel=1e-6)
    assert any("cannot stack" in n for n in result.notes), result.notes


def test_moderately_fragmented_mask_no_longer_gets_a_confident_topology_call():
    """Positive control: the doc's own described latent hazard -- a mask
    shrunk enough to be suspicious (between TOPOLOGY_TRUST_SHRINK_LIMIT and
    MEASURED_UNDER_BOX_LIMIT) but not enough to trip the area guard's own
    outright refusal. Before this fix this reached a confident separate-
    pieces call; after, it must fall through to the neutral measured-height
    table instead, the same path a food with no piece_share at all takes.
    own_box=0.45, measured_area_ratio=0.10 -> shrink 4.5x (within the new
    band: 4.0 < 4.5 <= 5.0)."""
    with_topology = estimate_grams(
        name="roast beef", area_ratio=0.10, hint=HINT,
        measured_area_ratio=0.10, bbox={"x": 0.1, "y": 0.1, "w": 0.75, "h": 0.6},
        detection_confidence=0.8, largest_piece_share=0.50,
    )
    no_topology_signal = estimate_grams(
        name="roast beef", area_ratio=0.10, hint=HINT,
        measured_area_ratio=0.10, bbox={"x": 0.1, "y": 0.1, "w": 0.75, "h": 0.6},
        detection_confidence=0.8,
    )
    assert not any("cannot stack" in n for n in with_topology.notes), with_topology.notes
    assert any("too fragmented to trust its shape" in n for n in with_topology.notes), \
        with_topology.notes
    # Falls to exactly the same neutral height a food with NO piece_share
    # reading gets -- confirming it lands on the existing fallback rung,
    # not a new one.
    assert with_topology.grams == pytest.approx(no_topology_signal.grams, rel=1e-6)


def test_clearly_over_the_area_limit_is_still_refused_outright_first():
    """A shrink well past MEASURED_UNDER_BOX_LIMIT (the burger's own 6.7x)
    is excluded by the EXISTING area guard before this fix's new check is
    ever consulted -- confirms the two checks compose rather than either
    one masking a change to the other."""
    result = estimate_grams(
        name="cheeseburger", area_ratio=0.09, hint=HINT,
        measured_area_ratio=0.0135, bbox={"x": 0.3, "y": 0.25, "w": 0.3, "h": 0.3},
        detection_confidence=0.8, largest_piece_share=0.5185,
    )
    assert not any("too fragmented to trust its shape" in n for n in result.notes), result.notes
    assert any("refused" in n for n in result.notes), result.notes


# ---------------------------------------------------------------------------
# Photo 35, from the actual cached production masks -- zero network, zero
# model calls. See this task's report for the full before/after numbers.
# ---------------------------------------------------------------------------
def _union_mask(masks, box, shape):
    H, W = shape
    x0, x1 = int(max(0, box["x"] * W)), int(min(W, (box["x"] + box["w"]) * W))
    y0, y1 = int(max(0, box["y"] * H)), int(min(H, (box["y"] + box["h"]) * H))
    box_px = float((x1 - x0) * (y1 - y0)) or 1.0
    union = np.zeros((H, W), bool)
    for m, _area, _inside in _prefilter(masks, x0, x1, y0, y1, box_px):
        if m.mean() > MAX_MASK_FRAME_SHARE:
            continue
        ys, xs = np.nonzero(m)
        cx, cy = float(xs.mean()), float(ys.mean())
        if x0 <= cx < x1 and y0 <= cy < y1:
            union |= m
    return union


@pytest.fixture(scope="module")
def photo35_masks():
    path = REPO_ROOT / "docs" / "evidence" / "2026-09-12-photo35-production-masks.npz"
    if not path.exists():
        pytest.skip("photo 35's cached production masks are not present")
    return load_mask_dump(path)


def test_photo35_burger_still_refused_by_the_area_guard_alone():
    """The burger's fragmented-bun mask never reaches the topology branch
    either before or after this fix -- MEASURED_UNDER_BOX_LIMIT alone
    already excludes it (shrink ~6.7x), confirming this fix changes nothing
    for the one documented case where the topology reading (0.52, "wrongly"
    separate) would otherwise have mattered."""
    path = REPO_ROOT / "docs" / "evidence" / "2026-09-12-photo35-production-masks.npz"
    if not path.exists():
        pytest.skip("photo 35's cached production masks are not present")
    d = load_mask_dump(path)
    masks, shape, boxes = d["pool"], d["shape"], d["boxes"]
    burger_box = boxes[0]
    union = _union_mask(masks, burger_box, shape)
    measured_area_ratio = float(union.mean())
    own_box = burger_box["w"] * burger_box["h"]
    shrink = own_box / max(measured_area_ratio, 1e-9)
    share = largest_piece_share(union.astype(np.uint8))

    assert shrink > MEASURED_UNDER_BOX_LIMIT, (
        f"this test's premise (burger already refused) needs shrink > "
        f"{MEASURED_UNDER_BOX_LIMIT}, got {shrink:.2f}")

    result = estimate_grams(
        name="cheeseburger", area_ratio=own_box, hint=HINT,
        measured_area_ratio=measured_area_ratio, bbox=burger_box,
        detection_confidence=0.8, largest_piece_share=share,
    )
    assert not any("too fragmented to trust its shape" in n for n in result.notes), \
        "the burger should be refused by the existing area guard, not this fix's new branch"
    assert any("refused" in n for n in result.notes), result.notes


def test_photo35_fries_unaffected_by_this_fix_because_the_mask_is_complete():
    """The fries' actual problem (documented in HANDOFF.md) is a COMPLETE
    mask that is UNDER-separated -- several real pieces merged into one
    connected blob -- not a fragmented one. This fix only ever distrusts a
    topology reading when the area guard's own signal is elevated; the
    fries' shrink (~1.3x on the cached production masks) is nowhere near
    either limit, so this fix provably cannot and does not change their
    classification. Reproduces the exact photo-35 finding in this task's
    report as a standing regression test."""
    path = REPO_ROOT / "docs" / "evidence" / "2026-09-12-photo35-production-masks.npz"
    if not path.exists():
        pytest.skip("photo 35's cached production masks are not present")
    d = load_mask_dump(path)
    masks, shape, boxes = d["pool"], d["shape"], d["boxes"]
    fries_box = boxes[1]
    union = _union_mask(masks, fries_box, shape)
    measured_area_ratio = float(union.mean())
    own_box = fries_box["w"] * fries_box["h"]
    shrink = own_box / max(measured_area_ratio, 1e-9)
    share = largest_piece_share(union.astype(np.uint8))

    assert shrink < TOPOLOGY_TRUST_SHRINK_LIMIT, (
        f"this test's premise (fries' mask is not fragmented) needs shrink "
        f"well under {TOPOLOGY_TRUST_SHRINK_LIMIT}, got {shrink:.2f}")

    with_fix = estimate_grams(
        name="french fries", area_ratio=own_box, hint=HINT,
        measured_area_ratio=measured_area_ratio, bbox=fries_box,
        detection_confidence=0.8, largest_piece_share=share,
    )
    # This fix's new note must NOT be the reason for whatever height was
    # chosen -- the topology branch still fires normally either way.
    assert not any("too fragmented to trust its shape" in n for n in with_fix.notes), \
        with_fix.notes
