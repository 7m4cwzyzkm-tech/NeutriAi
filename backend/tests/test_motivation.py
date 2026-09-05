"""The no-repeat guarantee and the tone filter."""
from app.services.motivation.engine import (
    BANNED, DEFAULT_TEMPLATES, TEMPLATES, TRIGGERS, _acceptable, _fill,
    hash_body, normalize,
)


def test_hash_ignores_case_and_punctuation():
    assert hash_body("Great job!") == hash_body("great job")
    assert hash_body("Day 5. Nice.") == hash_body("day 5 nice")


def test_hash_distinguishes_real_differences():
    assert hash_body("Day 5 done") != hash_body("Day 6 done")


def test_shaming_language_is_rejected():
    for bad in [
        "You earned those calories!",
        "Time to burn it off.",
        "That was a cheat meal.",
        "Don't feel guilty about it.",
        "Bad food choices today.",
        "You were lazy this week.",
    ]:
        assert not _acceptable(bad), f"should have been filtered: {bad}"


def test_good_messages_pass():
    for good in [
        "Day 12 unbroken. The habit is doing the work now.",
        "You're 640 kcal over. Worth noticing, not worth carrying around.",
        "New PR on Deadlift: 140kg.",
    ]:
        assert _acceptable(good)


def test_overlong_messages_rejected():
    assert not _acceptable("x" * 250)
    assert not _acceptable("")


def test_every_trigger_has_a_template_floor():
    for trigger in TRIGGERS:
        pool = TEMPLATES.get(trigger, []) + DEFAULT_TEMPLATES
        assert pool, f"{trigger} has no fallback template"


def test_templates_are_all_acceptable():
    ctx = {"streak": 5, "kcal": 600, "meals": 3, "minutes": 45, "workouts": 4,
           "ml": 3785, "hours": 16, "over": 400, "steps": 4200,
           "exercise": "Deadlift", "value": 140, "unit": "kg"}
    for trigger, templates in TEMPLATES.items():
        for t in templates:
            filled = _fill(t, ctx)
            assert "{" not in filled, f"unfilled placeholder in {trigger}: {filled}"
            assert _acceptable(filled), f"template fails the tone filter: {filled}"


def test_templates_are_unique_within_a_trigger():
    for trigger, templates in TEMPLATES.items():
        hashes = {hash_body(t) for t in templates}
        assert len(hashes) == len(templates), f"duplicate templates in {trigger}"


def test_fill_survives_missing_context():
    assert "{" not in _fill(TEMPLATES["pr_hit"][0], {})
