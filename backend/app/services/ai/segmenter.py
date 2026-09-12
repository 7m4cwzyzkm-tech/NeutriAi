"""Which segmenter measures the food, and whether to believe it.

WHY THIS EXISTS BEFORE SAM2 DOES

SAM2 is the decision; how it is wired in is the part that can go wrong quietly.
Two things need settling regardless of which SAM2 route we take, and neither of
them needs SAM2 present to be built or tested:

  1. A SEAM. `measure_items` currently calls the colour rule directly, so
     swapping in a real segmenter means surgery on the scan path -- exactly the
     kind of change that has been producing silent breakage all week. Behind
     this interface it is a config value.

  2. A GATE. The colour rule scores 54% on the split against 28% for the
     shipped estimate, and the reason it is not shipped is that nobody can tell
     a good mask from a bad one. SAM2 will be better, not perfect, and it will
     still need something deciding when to believe it. That logic is where the
     real engineering is, and it is testable today with a fake segmenter.

THE ROUTES, AND WHAT EACH COSTS

  local     torch + SAM2 weights in the image. CPU-only on python:3.12-slim
            with 2 uvicorn workers -- roughly +1 GB of image and 1-3 s of CPU
            per scan, on an app whose own Dockerfile says "every route here
            awaits network, not CPU". Blocks a worker for the duration.

  hosted    a segmentation endpoint over the network. No image weight, no CPU,
            and it matches the pattern every other model call in this app
            already uses. Costs a fraction of a cent per scan and adds a
            dependency that can be down.

  colour    what ships today. Free, no dependency, and not good enough.

The choice is a config value, not a rewrite, and `NullSegmenter` means the app
runs identically when whichever one is chosen is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class Segmentation:
    """One item's mask, and everything needed to decide whether to trust it."""

    # Boolean mask over the analysed image.
    mask: np.ndarray
    # The segmenter's own confidence, 0-1, where it reports one. None means it
    # does not, which is NOT the same as low -- the gate treats them apart.
    score: float | None = None
    # Which implementation produced this, so the bench can score them separately
    # rather than reporting one blended number nobody can act on.
    source: str = "unknown"

    @property
    def area_ratio(self) -> float:
        return float(self.mask.mean()) if self.mask.size else 0.0


class Segmenter(Protocol):
    """Point in, mask out.

    Deliberately a POINT and not a box. The model's box is quantised to a 0.05
    grid -- measured, 82% of values sit exactly on it where chance would put 20%
    -- and at 3% of frame one grid step is 45% of the food's weight. The box
    CENTRE does not carry that error: it is the one thing the model is asked for
    that is a location rather than a size, and locating is what it is good at.
    Passing SAM2 a box would feed the quantisation straight back in.
    """

    name: str

    def available(self) -> bool:
        """Can this run right now? Checked once per scan, never assumed."""
        ...

    def segment(self, rgb: np.ndarray,
                points: list[tuple[float, float]]) -> list[Segmentation | None]:
        """One mask per point, None where this segmenter could not measure it.

        None means "not measured" and the caller falls back. It must never be a
        small number standing in for a failure -- a mask that quietly returns
        almost nothing shipped a 146 g drumstick as 12 g once already.
        """
        ...


class NullSegmenter:
    """Measures nothing, and says so.

    The default, so that the app behaves identically when no segmenter is
    configured or the configured one cannot load. A missing dependency must cost
    a measurement, never a scan.
    """

    name = "none"

    def available(self) -> bool:
        return False

    def segment(self, rgb, points):
        return [None] * len(points)


# --- the gate ---------------------------------------------------------------
#
# A segmenter that is right most of the time still needs something deciding
# WHICH times. These thresholds are the ones the colour rule taught us, and they
# are about the shape of a plausible answer rather than about any particular
# implementation -- so they carry over to SAM2 rather than being thrown away
# with the thing that motivated them.

# Below this a "mask" is a few stray pixels, not a portion. Measured: when the
# rim sample landed on food instead of the plate, the mask inverted and returned
# under 2% of the plate -- and that failure shipped a 146 g drumstick as 12 g.
MIN_ITEM_AREA = 0.004
# Above this one item is claiming most of the picture, which on a plate holding
# several foods means the mask has merged them.
MAX_ITEM_AREA = 0.60
# A segmenter that reports its own confidence has to clear this. SAM2 does;
# the colour rule does not, and a segmenter that cannot say is not penalised for
# it -- it is judged on the shape checks alone.
MIN_SCORE = 0.50
# All the food on one plate. Under this the mask has found almost nothing;
# over it, it has swallowed the plate.
MIN_TOTAL_AREA, MAX_TOTAL_AREA = 0.02, 0.85


def usable(results: list[Segmentation | None]) -> bool:
    """All of it, or none of it.

    A plate with one unmeasured item cannot produce a split: the missing food
    lands on whichever items did work, which are precisely the ones that looked
    fine. Measured on photo 06 -- two seeds failed, and the rice was published
    as 100% of a plate it was about a third of.
    """
    if not results or any(r is None for r in results):
        return False
    for r in results:
        if not (MIN_ITEM_AREA <= r.area_ratio <= MAX_ITEM_AREA):
            return False
        if r.score is not None and r.score < MIN_SCORE:
            return False
    total = sum(r.area_ratio for r in results)
    return MIN_TOTAL_AREA <= total <= MAX_TOTAL_AREA


def shares(results: list[Segmentation | None]) -> list[float] | None:
    """Each item's share of the meal's food, or None if the plate fails the gate.

    A SHARE, because that is what a segmenter can be trusted for and an absolute
    footprint is not. Measured on the colour rule: shrinking the plate box 10%
    moved absolute footprints by 3-14% and the shares by 1-6%, because both
    terms of a ratio move together. Scale comes from the calibrated vessel,
    which is measured to under 1%; segmentation says only how the meal divides.
    """
    if not usable(results):
        return None
    total = sum(r.area_ratio for r in results)
    if total <= 0:
        return None
    return [round(r.area_ratio / total, 4) for r in results]
