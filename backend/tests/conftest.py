"""Test setup, and one guarantee the suite did not have: NO TEST TALKS TO A
NETWORK PROVIDER.

WHY THIS FIXTURE EXISTS

`dev test` was making live Replicate calls. Nothing declared that; it fell out
of `.env`. `food_seg.segmenter()` and `vision.DEPTH_PROVIDER` are both built
from settings at import, so any test that reached them inherited whatever the
developer happened to have configured -- and 24 tests reach them.

That makes the suite nondeterministic and paid. It reads as a plate appearing
or not appearing, a union succeeding or failing, and grams moving -- which is
exactly what it looked like: five tests in test_refine went red on a day
nothing in their path had changed, and the difference was a segmenter answering
where it had previously timed out. A suite that passes or fails on whether a
call times out is not a gate, and worse, it is a gate that reports the wrong
CAUSE: the natural reading was that the code under test had changed.

It also means CI and a developer's machine were running different suites. The
two tests asserting "no segmenter is configured by default" passed in CI and
failed locally, and both were correct about their own environment.

So: every test starts with the shipped defaults pinned. A test that wants a
provider installs a fake one itself -- `_FakeSegmenter`, a `MockTransport`, or
a stub on `_measured_areas` -- which is what the deterministic tests already
do and what the rest should have done.

The settings are cleared too, not just the built objects, so that
`from_settings()` cannot construct a live provider either. That is the seam the
two default tests above actually check.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def providers_are_never_live(monkeypatch):
    """The shipped default, pinned, for every test that does not opt out."""
    from app.config import settings
    from app.services.ai import depth_map, food_seg, vision
    from app.services.ai.segmenter import NullSegmenter

    # The objects already built at import time.
    monkeypatch.setattr(food_seg, "_SEGMENTER", NullSegmenter(), raising=False)
    monkeypatch.setattr(vision, "DEPTH_PROVIDER", depth_map.NullDepth(),
                        raising=False)
    # And the settings they are built FROM, so a fresh `from_settings()` inside
    # a test cannot reach past the two lines above.
    monkeypatch.setattr(settings, "segmenter_provider", "", raising=False)
    monkeypatch.setattr(settings, "depth_provider", "", raising=False)
