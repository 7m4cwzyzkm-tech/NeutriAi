"""The portion estimator is the product's core claim. These tests pin the
behaviour that makes it trustworthy: plausible outputs, honest bands, and
confidence that degrades with the quality of the geometry."""
import pytest

from app.services.ai.portion import (
    DEFAULT_PLATE_DIAMETER_MM, GeometryHint, band_label, density_for,
    estimate_grams, mm2_per_frame, reconcile_multi_image,
)

PLATE = GeometryHint(plate_ellipse_area_ratio=0.55, plate_diameter_mm=270)


@pytest.mark.parametrize(
    "name,area,low,high",
    [
        ("jasmine rice", 0.14, 100, 350),
        ("grilled chicken breast", 0.10, 90, 300),
        ("caesar salad", 0.20, 40, 200),
        ("tomato soup", 0.18, 200, 600),
        ("french fries", 0.15, 60, 250),
        ("salmon fillet", 0.09, 70, 250),
    ],
)
def test_estimates_are_plausible(name, area, low, high):
    e = estimate_grams(name=name, area_ratio=area, hint=PLATE, detection_confidence=0.8)
    assert low <= e.grams <= high, f"{name} -> {e.grams} g is outside a realistic serving"


def test_band_contains_estimate():
    e = estimate_grams(name="rice", area_ratio=0.15, hint=PLATE)
    assert e.grams_low < e.grams < e.grams_high


def test_confidence_degrades_without_reference():
    good = estimate_grams(name="rice", area_ratio=0.15, hint=PLATE)
    blind = estimate_grams(name="rice", area_ratio=0.15, hint=GeometryHint(), ai_prior_grams=180)
    assert good.confidence > blind.confidence
    assert good.method == "plate_reference"
    assert blind.method == "ai_prior"


def test_band_widens_as_confidence_falls():
    good = estimate_grams(name="rice", area_ratio=0.15, hint=PLATE)
    blind = estimate_grams(name="rice", area_ratio=0.15, hint=GeometryHint(), ai_prior_grams=180)
    width = lambda e: (e.grams_high - e.grams_low) / e.grams
    assert width(blind) > width(good)


def test_area_scales_monotonically():
    small = estimate_grams(name="rice", area_ratio=0.05, hint=PLATE)
    large = estimate_grams(name="rice", area_ratio=0.25, hint=PLATE)
    assert large.grams > small.grams * 3


def test_clamped_at_upper_rail():
    """An absurd area ratio must not produce an absurd number."""
    e = estimate_grams(name="rice", area_ratio=0.99, hint=PLATE, detection_confidence=0.9)
    assert e.grams <= 1500
    assert e.confidence < 0.8


def test_density_lookup():
    assert density_for("jasmine rice") == pytest.approx(0.78)
    assert density_for("mixed greens salad") == pytest.approx(0.22)
    assert density_for("something unheard of") == pytest.approx(0.85)
    assert density_for("rice", explicit=0.9) == pytest.approx(0.9)


def test_geometry_ladder_order():
    assert mm2_per_frame(GeometryHint(reference_area_mm2=5000, plate_ellipse_area_ratio=0.5))[1] == "plate_reference"
    assert mm2_per_frame(GeometryHint(plate_diameter_mm=270, plate_ellipse_area_ratio=0.5))[1] == "plate_reference"
    assert mm2_per_frame(GeometryHint(depth_mm=400))[1] == "depth_model"
    assert mm2_per_frame(GeometryHint(plate_ellipse_area_ratio=0.5))[1] == "pixel_area"
    assert mm2_per_frame(GeometryHint())[1] == "ai_prior"


def test_blending_when_geometry_and_prior_disagree():
    e = estimate_grams(
        name="rice", area_ratio=0.40, hint=PLATE, ai_prior_grams=100, detection_confidence=0.9
    )
    assert any("disagreed" in n for n in e.notes)


def test_reconcile_agreeing_views_raises_confidence():
    a = estimate_grams(name="rice", area_ratio=0.15, hint=PLATE, detection_confidence=0.7)
    b = estimate_grams(name="rice", area_ratio=0.152, hint=PLATE, detection_confidence=0.7)
    merged = reconcile_multi_image([a, b])
    assert merged.confidence >= a.confidence
    assert merged.method == "multi_image"


def test_reconcile_disagreeing_views_lowers_confidence():
    a = estimate_grams(name="rice", area_ratio=0.08, hint=PLATE, detection_confidence=0.8)
    b = estimate_grams(name="rice", area_ratio=0.30, hint=PLATE, detection_confidence=0.8)
    merged = reconcile_multi_image([a, b])
    assert merged.confidence < a.confidence
    assert (merged.grams_high - merged.grams_low) / merged.grams > 0.3


def test_band_labels():
    assert band_label(0.9) == "high"
    assert band_label(0.6) == "medium"
    assert band_label(0.3) == "low"
