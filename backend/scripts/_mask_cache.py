"""One namespace per writer, and the resolution recorded inside. A LIVE BUG FIX.

WHAT WAS WRONG

Three scripts read and wrote `mask_overlays/masks-<stem>.npz` and they do not
mean the same thing by it:

    height_fit.py       decodes at longest edge 1280
    mask_stability.py   decodes at longest edge 1568, the SCAN PATH's size,
                        and says so: "AND UNDER THE NAME THE REPLAY READS"
    box_replay.py       reads whatever is there

Same filename, different resolutions, last writer wins, no error. The cache on
disk today proves it happened -- twenty-four files at FOUR different
resolutions:

    17-34 (the photos the height constants were fitted on)   1280x960
    41-45 (the weighed spread/heaped pairs)                   640x480
    35    (slider-fries)                                     1568x1176

That last one is `mask_stability`'s write, at the scan path's size, sitting
under the name `height_fit` reads at 1280. Whichever ran last silently
redefined the other's input.

WHY IT MATTERS MORE THAN A TIDY-UP

SAM2's automatic generator samples a `points_per_side` grid OVER THE IMAGE, so
resolution changes the mask set directly -- and we measured that merely
re-encoding at the SAME size swaps one mask of sixteen and shifts every area by
up to 0.7%. `CONNECTED_PILE_HEIGHT_MM` and `SEPARATE_PIECES_HEIGHT_MM` were
solved against footprints from this cache, so they rest on inputs the pipeline
never produces, at a resolution it never uses.

THE FIX

A filename that cannot collide, and metadata that cannot be mistaken:

    <writer>-<stem>-le<long_edge>.npz      e.g. heightfit-17-carrots-plate-le1280.npz

plus `long_edge`, `shape` and `writer` stored INSIDE, so a reader that opens a
file by any route still knows what it is holding. `load` refuses a file whose
recorded long edge is not the one asked for, rather than returning masks that
look fine and mean something else.

Legacy `masks-*.npz` files are NOT read. They carry no writer and no recorded
resolution, so there is no way to know which script wrote them or at what size
-- which is the bug. They are reported, by name, with what to do instead.
"""
from __future__ import annotations

import pathlib

import numpy as np

# Every writer that may put masks in this directory. Adding one here is how it
# gets a namespace; picking a name at a call site is how the collision came
# back.
HEIGHT_FIT = "heightfit"
STABILITY = "stability"
WRITERS = (HEIGHT_FIT, STABILITY)

LEGACY_GLOB = "masks-*.npz"


def path_for(root, writer: str, stem: str, long_edge: int) -> pathlib.Path:
    """Where this writer's masks for this photo at this resolution live."""
    if writer not in WRITERS:
        raise ValueError(f"unknown writer {writer!r}; add it to WRITERS")
    return pathlib.Path(root) / f"{writer}-{stem}-le{int(long_edge)}.npz"


def save(path, masks, *, writer: str, long_edge: int, shape, seed=None) -> None:
    """Masks plus enough metadata that a reader cannot misinterpret them."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {f"m{i}": np.asarray(m) for i, m in enumerate(masks)}
    np.savez_compressed(
        path,
        writer=np.array(writer),
        long_edge=np.array(int(long_edge)),
        shape=np.array([int(shape[0]), int(shape[1])]),
        seed=np.asarray(seed if seed is not None else shape),
        **payload,
    )


def load(path, *, expect_long_edge: int | None = None):
    """Masks and metadata, or (None, reason). Never guesses.

    A file with no recorded `long_edge` predates the namespace and cannot be
    trusted -- see the module docstring. Reported rather than read.
    """
    path = pathlib.Path(path)
    if not path.exists():
        return None, f"{path.name} does not exist"
    with np.load(path, allow_pickle=False) as z:
        if "long_edge" not in z.files:
            return None, (
                f"{path.name} records no resolution -- written before the "
                f"writers were namespaced, so which script made it and at what "
                f"size is unknowable. Regenerate it, or use `dev dumpmask` for "
                f"production's own masks."
            )
        le = int(z["long_edge"])
        if expect_long_edge is not None and le != int(expect_long_edge):
            return None, (
                f"{path.name} holds masks at longest edge {le}, not "
                f"{int(expect_long_edge)} -- a different segmentation, not a "
                f"different file format"
            )
        keys = sorted((k for k in z.files if k.startswith("m")),
                      key=lambda s: int(s[1:]))
        meta = {"writer": str(z["writer"]) if "writer" in z.files else "?",
                "long_edge": le,
                "shape": (int(z["shape"][0]), int(z["shape"][1]))
                if "shape" in z.files else None}
        return [z[k] for k in keys], meta


def legacy_files(root) -> list[pathlib.Path]:
    """The un-namespaced files still on disk, so a caller can say so out loud."""
    return sorted(pathlib.Path(root).glob(LEGACY_GLOB))
