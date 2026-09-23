"""An ai_estimate row's density must not silently outrank the real
nutrition table. See docs/HANDOFF.md, "LAUNCH BLOCKER: GRAMS DEPEND ON
USDA UPTIME" for the full research trail this locks down -- documented
symptom: the same cached vision response logged a caesar salad at 350g one
time and 98g another, purely on whether USDA happened to answer at scan
time, because a cached ai_estimate row's LLM-guessed density silently
outranked the density table whenever a real provider missed.

No fixture for that specific photo exists under tests/fixtures/ (checked:
only plate_ellipse_recorded.json and usda_bench_fdc.json, neither carries a
food_facts row) -- these tests construct food_facts-shaped dicts directly,
per this task's own instruction not to invent new photo data.
"""
import pytest

from app.services.ai.portion import GeometryHint, density_for, estimate_grams
from app.services.ai.vision import _explicit_density

PLATE = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=270)


def test_ai_estimate_density_is_blocked():
    """The exact bug: an ai_estimate row's density_g_ml must not reach
    _explicit_density -- it is a reasoning-model guess, cached from a
    moment a real provider failed or found nothing, not a measurement."""
    fact = {"source": "ai_estimate", "density_g_ml": 0.85}
    assert _explicit_density(fact) is None


def test_usda_density_reaches_explicit():
    """A real provider's density (on the rare row that carries one) is not
    swept up by the gate -- it is source-specific, not a blanket block."""
    fact = {"source": "usda", "density_g_ml": 1.05}
    assert _explicit_density(fact) == pytest.approx(1.05)


def test_user_entered_density_reaches_explicit():
    """Judgment call, stated here and in this task's report: a user's own
    typed-in measurement is real data, not a guess, so it is treated the
    same as a provider row and reaches _explicit_density -- only the
    ai_estimate SOURCE is blocked, not "anything that isn't USDA"."""
    fact = {"source": "user", "density_g_ml": 0.95}
    assert _explicit_density(fact) == pytest.approx(0.95)


def test_usda_row_with_no_density_is_unaffected():
    """The common case today (33 of 116 food_facts rows, per HANDOFF.md) --
    confirms the gate changes nothing for a usda row that already carries
    no density_g_ml at all."""
    fact = {"source": "usda", "density_g_ml": None}
    assert _explicit_density(fact) is None


def test_blocked_density_does_not_win_at_density_for_rank_1():
    """End-to-end through density_for (untouched by this fix -- its own
    ranking order and internals are out of scope). Blocking the guess at
    the source means density_for falls through to its own dish/group
    table instead of returning the LLM's number verbatim at rank 1."""
    ai_estimate_fact = {"source": "ai_estimate", "density_g_ml": 0.30}
    gated = _explicit_density(ai_estimate_fact)
    assert gated is None

    # Had the guess NOT been blocked (the bug), it would win at rank 1 and
    # come back unchanged -- density_for's own documented behaviour,
    # verified here so the next assertion's contrast is meaningful.
    if_unblocked = density_for("an unusual dish name nothing else matches", 0.30)
    assert if_unblocked == pytest.approx(0.30)

    # With the gate applied, the same name falls through to the table.
    with_gate = density_for("an unusual dish name nothing else matches", gated)
    assert with_gate != pytest.approx(0.30)


def test_grams_differ_only_because_of_the_gate():
    """Reproduces the documented symptom's SHAPE directly: the identical
    detection (same name, same area_ratio, same everything) produces
    different grams depending only on whether the ai_estimate row's density
    was allowed to reach estimate_grams as explicit -- exactly the "350g one
    time, 98g another" nondeterminism for one photo that HANDOFF.md
    documents, driven by a third party's uptime rather than the food."""
    kwargs = dict(name="caesar salad", area_ratio=0.20, hint=PLATE, detection_confidence=0.8)
    ai_estimate_fact = {"source": "ai_estimate", "density_g_ml": 0.30}

    if_unblocked = estimate_grams(density=ai_estimate_fact["density_g_ml"], **kwargs)  # the bug
    with_gate = estimate_grams(density=_explicit_density(ai_estimate_fact), **kwargs)  # this fix

    assert if_unblocked.grams != pytest.approx(with_gate.grams)
