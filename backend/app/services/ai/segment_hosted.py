"""SAM2 over HTTP, and the one job that is blocking everything else.

TWO JOBS, NOT ONE

  food items   a point per food, a mask per point. This is what `segmenter.py`
               was built for: shares of the meal, not absolute area.

  THE PLATE    one mask of the whole plate, outer rim included. Nothing in this
               app can do this today, and it is the blocker: the depth scale
               rests on the plate's ellipse, and the measured footprint has been
               computed and deliberately kept out of the grams for weeks waiting
               for it.

Six hand-rolled methods have failed at the second job -- four recorded in
food_seg's docstring before this week, two more since. The last failure named
the specification exactly: it is not enough to find AN ellipse on the plate,
because a plate has TWO concentric edges and the inner one is often stronger.
A fit that locked onto the well reported 34.7 degrees of tilt for a plate
photographed from almost directly overhead, and passed every other check.

So the plate prompt here is deliberate: points on the bare RIM ANNULUS, not the
centre. A centre point is as much a prompt for the well, or for the food sitting
in it, as for the plate.

WHAT IT REFUSES

An overlay instead of a mask. Most segmentation endpoints will happily return
the photograph with the mask painted over it, and that decodes as a perfectly
valid image of the right size -- the same failure the depth provider guards
against with colour maps, in a different costume. A mask is bilevel; a
photograph is not.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import time

import httpx
import numpy as np
import structlog

from ...config import settings
from .segmenter import Segmentation

log = structlog.get_logger()

MAX_UPLOAD_PX = 1024
UPLOAD_QUALITY = 90
REPLICATE_URL = "https://api.replicate.com/v1/predictions"
REPLICATE_DONE = {"succeeded", "failed", "canceled", "aborted"}

# A mask is bilevel. Anything with a spread of intermediate values is a
# photograph with paint on it, and paint is not a measurement.
#
# Measured against the alternative: an overlay of a plate of food has 40-60% of
# its pixels strictly between the extremes. A real mask, after JPEG-free PNG
# transport, has almost none -- so the threshold has a wide margin either side.
MAX_MIDTONE_FRACTION = 0.08

# Where to sample the plate, as a fraction of the model's box radius. The same
# annulus the depth plane fit uses, for the same reason: the rim is the one part
# of a plate photograph reliably free of food.
PLATE_PROMPT_INNER, PLATE_PROMPT_OUTER = 0.80, 0.94
PLATE_PROMPT_POINTS = 8

# What a plate mask has to look like before it is believed. These are the checks
# the hand-rolled attempts needed and mostly failed, kept because they are about
# the shape of a plate rather than about how the mask was produced.
PLATE_MIN_AREA, PLATE_MAX_AREA = 0.05, 0.92
PLATE_MAX_SQUASH = 2.2
PLATE_MAX_BORDER = 0.02


def encode_image(rgb: np.ndarray) -> bytes | None:
    try:
        from PIL import Image
    except Exception:  # pragma: no cover
        return None
    if rgb is None or getattr(rgb, "ndim", 0) != 3:
        return None
    img = Image.fromarray(np.asarray(rgb).astype(np.uint8), "RGB")
    img.thumbnail((MAX_UPLOAD_PX, MAX_UPLOAD_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=UPLOAD_QUALITY)
    return buf.getvalue()


def is_overlay(arr: np.ndarray) -> bool:
    """Is this a mask, or a picture with a mask painted on it?

    The failure that does not announce itself: a valid image, the right size,
    decoding cleanly, and carrying no measurement at all. Judged on midtones,
    because a mask has two values and a photograph has all of them.
    """
    if arr is None or arr.size == 0:
        return True
    flat = arr.astype(np.float64)
    lo, hi = float(flat.min()), float(flat.max())
    if hi - lo <= 0:
        return True                      # a single flat value is not a mask
    norm = (flat - lo) / (hi - lo)
    midtone = float(np.mean((norm > 0.15) & (norm < 0.85)))
    return midtone > MAX_MIDTONE_FRACTION


# FINDING THE PLATE, SO IT CAN BE KEPT OUT OF THE FOOD.
#
# The box rule kept the plate out by SIZE -- drop a mask more than twice the
# box, or one that fills 95% of it. Both are ratios against the model's box,
# and that box is quantised to a 0.05 grid and moves between runs. Measured on
# the weighed zucchini, same photograph, same masks, only the box different:
#
#     box 16%   union 11.9% of frame   3 pieces   -> a single layer,  9.2 mm
#     box 25%   union 44.2% of frame   1 piece    -> a pile,         21.0 mm
#
# At the larger box the PLATE slipped into the union: 32% of frame, under the
# frame-share ceiling and only 1.3x the box, so every size test passed it. The
# food's measured weight then swung 3.1x across three runs of one photograph,
# on nothing but how the model rounded a bounding box.
#
# A plate is not a thing to be recognised by its size relative to a box that
# moves. It is recognised by what is missing from it: the plate has the FOOD
# punched out of it, several small holes; the tabletop has the PLATE punched
# out of it, one hole as big as the mask. Measured across every cached
# photograph, largest hole over its own mask: plates 0.05-0.32, tables
# 0.51-1.38. Nothing in between, so the cut is 0.35.
# A plate photographed from above fills a good share of the frame. Every plate
# correctly found across the cached photographs measured 22.5% to 39.4%. At the
# old floor of 0.08 a 10.2% BLOB OF SPAGHETTI was taken for the plate on photo
# 20, which then excluded every mask bigger than the spaghetti -- including the
# real plate -- and left no union at all.
PLATE_MIN_FRAME = 0.15
PLATE_MAX_FRAME = 0.60
PLATE_MAX_ASPECT = 1.25
PLATE_MAX_HOLE_OVER_MASK = 0.35


def fill_outer(mask: np.ndarray) -> np.ndarray:
    """The mask with its holes closed, by its outer contours."""
    import cv2
    u8 = mask.astype(np.uint8) * 255
    cnts, _ = cv2.findContours(u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = np.zeros_like(u8)
    cv2.drawContours(out, cnts, -1, 255, cv2.FILLED)
    return out > 0


def plate_among(masks: list, shape: tuple[int, int]):
    """The plate among a pile of masks, or None. See the note above.

    Lives here rather than in food_seg because both the box rule in this file
    and Rule 0 in that one need the same answer, and two copies of a rule this
    load-bearing would drift.
    """
    import cv2
    H, W = shape
    best = None
    for m in masks:
        f = float(m.mean())
        if not (PLATE_MIN_FRAME <= f <= PLATE_MAX_FRAME):
            continue
        ys, xs = np.nonzero(m)
        if not len(ys):
            continue
        hh = int(ys.max() - ys.min() + 1)
        ww = int(xs.max() - xs.min() + 1)
        if hh > 0.95 * H or ww > 0.95 * W:
            continue                                  # runs off the frame
        if max(hh, ww) / min(hh, ww) > PLATE_MAX_ASPECT:
            continue                                  # a plate is round
        holes = fill_outer(m) & ~m
        n, _l, stats, _c = cv2.connectedComponentsWithStats(
            holes.astype(np.uint8), 8)
        areas = [int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n)]
        if areas and max(areas) / float(m.sum()) > PLATE_MAX_HOLE_OVER_MASK:
            continue                                  # the tabletop
        if best is None or f > float(best.mean()):
            best = m
    return best


def decode_mask(raw: bytes, shape: tuple[int, int]) -> np.ndarray | None:
    """Bytes from the endpoint to a boolean mask the size of the photograph."""
    if not raw:
        return None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001
        log.warning("mask_unreadable", error=str(exc)[:120])
        return None

    arr = np.asarray(img.convert("L"), dtype=np.float64)
    if is_overlay(arr):
        log.warning("mask_looks_like_an_overlay", mode=img.mode)
        return None

    h, w = int(shape[0]), int(shape[1])
    if arr.shape[:2] != (h, w):
        # Resized here rather than left to the caller: a silent shape mismatch
        # means the endpoint is called, paid for, and never used.
        try:
            from PIL import Image as _I
            arr = np.asarray(
                _I.fromarray(arr.astype(np.float32), mode="F").resize((w, h), _I.NEAREST),
                dtype=np.float64)
        except Exception as exc:  # noqa: BLE001
            log.warning("mask_resample_failed", error=str(exc)[:120])
            return None
    mid = (arr.max() + arr.min()) / 2.0
    return arr > mid


def rim_points(plate_bbox: dict, shape: tuple[int, int]) -> list[tuple[float, float]]:
    """Points on the bare rim, in pixels.

    NOT the centre. A centre point prompts the well, or the food sitting in it,
    as readily as the plate -- and a mask of the well reports a tilt the plate
    does not have, because the diameter measured with a tape is the OUTER one.
    """
    h, w = shape[0], shape[1]
    try:
        x, y = float(plate_bbox["x"]), float(plate_bbox["y"])
        bw, bh = float(plate_bbox["w"]), float(plate_bbox["h"])
    except (KeyError, TypeError, ValueError):
        return []
    if not (bw > 0 and bh > 0):
        return []
    cx, cy = (x + bw / 2) * w, (y + bh / 2) * h
    rx, ry = bw * w / 2, bh * h / 2
    out: list[tuple[float, float]] = []
    mid = (PLATE_PROMPT_INNER + PLATE_PROMPT_OUTER) / 2
    for i in range(PLATE_PROMPT_POINTS):
        a = 2 * np.pi * i / PLATE_PROMPT_POINTS
        px, py = cx + mid * rx * np.cos(a), cy + mid * ry * np.sin(a)
        if 0 <= px < w and 0 <= py < h:
            out.append((float(px), float(py)))
    return out


def plate_is_plausible(mask: np.ndarray) -> tuple[bool, str]:
    """Does this look like a whole plate, or like the well, or like the table?"""
    if mask is None or not mask.any():
        return False, "empty"
    area = float(mask.mean())
    if not (PLATE_MIN_AREA <= area <= PLATE_MAX_AREA):
        return False, f"{area:.0%} of the frame"
    h, w = mask.shape[:2]
    border = int(mask[0, :].sum() + mask[-1, :].sum()
                 + mask[:, 0].sum() + mask[:, -1].sum())
    if border > PLATE_MAX_BORDER * 2 * (h + w):
        return False, "runs off the frame"
    ys, xs = np.nonzero(mask)
    pts = np.column_stack([xs, ys]).astype(np.float64)
    try:
        lam = np.linalg.eigvalsh(np.cov(pts - pts.mean(axis=0), rowvar=False))
    except np.linalg.LinAlgError:
        return False, "degenerate"
    if lam[0] <= 0:
        return False, "degenerate"
    if np.sqrt(lam[1] / lam[0]) > PLATE_MAX_SQUASH:
        return False, "too squashed to be a plate"
    return True, "ok"


# ---------------------------------------------------------------------------
# the masks production actually used
# ---------------------------------------------------------------------------

# WHY THIS EXISTS, AND WHY IT IS NOT HOUSEKEEPING.
#
# For a week the offline replays reasoned about production's unions from masks
# that `mask_stability` had REGENERATED: it encoded the local JPEG and called
# the model itself. Same photograph, same model, same count -- and not the same
# masks. Production uploads to storage, fetches back, decodes and re-encodes
# through the scan path, so the BYTES differ, and SAM2 is deterministic only for
# identical bytes. That is exactly what the probe measured: IoU 1.0000 across
# repeat calls on identical bytes, which says nothing at all about a different
# encoding of the same picture.
#
# The contradiction that exposed it: replaying production's OWN logged box over
# the cached 16 masks took 6 where production took 7. `_union_in_box` decides
# each mask on its own -- no mask's verdict depends on any other -- so a
# superset can never yield fewer takes than a subset. The sets were therefore
# different, and every number measured on the cache was measuring the cache.
#
# The lesson is structural and outlives this bug: DO NOT RECONSTRUCT WHAT THE
# PIPELINE CAN EMIT. A regenerated input is a hypothesis about production; an
# emitted one is production. Everything below exists so that an offline replay
# is exact by construction rather than by argument.
#
# Off unless NUTRIAI_MASK_DUMP names a directory, read at call time so a run can
# turn it on without a restart and a test can monkeypatch it. It never raises
# into the scan path: a debugging aid that can cost a user their scan is worse
# than no debugging aid.

MASK_DUMP_ENV = "NUTRIAI_MASK_DUMP"


def _plain(box):
    """A box as JSON can hold it, whatever numeric types it arrived carrying.

    Not paranoia: a numpy scalar in a box or a point raises inside json.dumps,
    the dump's own except clause swallows it, and the result is a scan that
    silently was not captured -- the exact failure this file exists to end,
    reintroduced by the safety net meant to protect it.
    """
    if not isinstance(box, dict):
        return None
    try:
        return {k: float(box[k]) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None


def dump_masks(image: bytes, shape: tuple[int, int], raw: list, pool: list,
               boxes, points, plate_hint=None) -> str | None:
    """Write this photograph's mask pool where a replay can read it exactly.

    `raw` is everything the model returned; `pool` is what survived the plate
    and frame-share exclusions and is the list `_union_in_box` iterates. Both
    are kept because a replay of the SELECTION needs `pool` and a replay of the
    EXCLUSIONS needs `raw`, and re-deriving either from the other offline is the
    same mistake this file was written to stop.

    Named by the digest of the bytes SENT TO THE MODEL -- the same key the memo
    uses and the same one `sam2_auto_masks` logs -- so a dump and a log line can
    be tied together without trusting a filename or a timestamp.

    Returns the path written, or None when dumping is off or failed.
    """
    import os

    dest = (os.environ.get(MASK_DUMP_ENV) or "").strip()
    if not dest:
        return None
    try:
        from pathlib import Path

        digest = hashlib.sha256(image).hexdigest()
        out = Path(dest)
        out.mkdir(parents=True, exist_ok=True)
        # ONE FILE PER SCAN, NEVER ONE PER DIGEST.
        #
        # The digest names the BYTES, and repeat scans of one photograph can
        # send identical bytes -- that is exactly what `--runs N` does, and the
        # memo makes it likelier still. Keyed on digest alone the second scan
        # overwrites the first, and the question those runs are asked to settle
        # is whether the pool differs BETWEEN them. The evidence would be
        # destroyed by the instrument built to collect it, silently, and the
        # run would have to be paid for twice.
        #
        # So the digest keeps naming the bytes (the tie to `sam2_auto_masks`
        # survives, and a single-scan dump keeps the plain name) and a counter
        # separates the scans. `box_replay` globs `masks-*.npz` and reads each.
        path = out / f"masks-{digest[:16]}.npz"
        n = 1
        while path.exists():
            n += 1
            path = out / f"masks-{digest[:16]}-{n}.npz"
        H, W = shape
        # packbits keeps a 1024x1024 bool from costing a megabyte each; the
        # shape travels alongside so unpacking cannot guess it wrong.
        def pack(ms):
            if not ms:
                return np.zeros((0, 0), dtype=np.uint8)
            return np.stack([np.packbits(m.astype(bool).ravel()) for m in ms])

        np.savez_compressed(
            path,
            raw=pack(raw), pool=pack(pool),
            raw_n=np.array(len(raw)), pool_n=np.array(len(pool)),
            shape=np.array([H, W]),
            digest=np.array(digest),
            boxes=np.array(json.dumps([_plain(b) for b in (boxes or [])])),
            points=np.array(json.dumps([[float(v) for v in p]
                                        for p in (points or [])])),
            plate_hint=(np.packbits(plate_hint.astype(bool).ravel())
                        if plate_hint is not None
                        else np.zeros(0, dtype=np.uint8)),
            has_plate_hint=np.array(bool(plate_hint is not None)),
        )
        log.info("mask_dump_written", path=str(path), raw=len(raw),
                 pool=len(pool), digest=digest[:12])
        return str(path)
    except Exception as exc:  # pragma: no cover - a dump must never cost a scan
        log.warning("mask_dump_failed", error=str(exc)[:200])
        return None


def load_mask_dump(path):
    """The other half of `dump_masks`, so replays share one format.

    Returns a dict with `raw` and `pool` as lists of bool arrays, plus the
    shape, digest, boxes, points and plate hint as they were at the moment the
    pipeline chose. Kept HERE rather than in a script so that a change to the
    dump cannot silently leave a reader behind.
    """
    with np.load(path, allow_pickle=False) as z:
        H, W = (int(z["shape"][0]), int(z["shape"][1]))

        def unpack(key, n):
            arr = z[key]
            if not n:
                return []
            return [np.unpackbits(arr[i])[:H * W].astype(bool).reshape(H, W)
                    for i in range(n)]

        hint = None
        if bool(z["has_plate_hint"]):
            hint = np.unpackbits(z["plate_hint"])[:H * W].astype(bool).reshape(H, W)
        return {
            "shape": (H, W),
            "digest": str(z["digest"]),
            "raw": unpack("raw", int(z["raw_n"])),
            "pool": unpack("pool", int(z["pool_n"])),
            "boxes": json.loads(str(z["boxes"])),
            "points": json.loads(str(z["points"])),
            "plate_hint": hint,
        }


# ---------------------------------------------------------------------------
# the provider
# ---------------------------------------------------------------------------

class HostedSegmenter:
    """SAM2 behind an HTTP endpoint. Implements `segmenter.Segmenter`.

    Provider-agnostic for the same reason the depth provider is: which host
    serves the weights is an operational choice, and the model's own input names
    belong in config rather than in this file.
    """

    def __init__(self, dialect: str = "", api_key: str = "", endpoint: str = "",
                 version: str = "", image_field: str = "image",
                 points_field: str = "point_coords", extra_input: dict | None = None,
                 output_field: str = "", timeout_s: float = 30.0, transport=None,
                 mode: str = "prompted"):
        self.dialect = dialect
        # "prompted": one call per point, the model returns that point's mask.
        # "auto":     one call per PHOTOGRAPH, the model returns every mask it
        #             can find and we choose. See _auto_masks.
        self.mode = (mode or "prompted").strip() or "prompted"
        self._auto_memo: tuple[str, list] | None = None
        # Why the last call produced nothing. A segmenter that fails is not
        # allowed to raise -- a failed measurement must cost a measurement and
        # not a user's scan -- but "no mask came back" and "the account has no
        # credit" are different problems and the tooling has to be able to tell
        # them apart. Set on every failure path, cleared on success.
        self.last_error: str | None = None
        self.api_key = api_key
        self.endpoint = endpoint
        self.version = version
        self.image_field = image_field or "image"
        self.points_field = points_field or "point_coords"
        self.extra_input = extra_input or {}
        self.output_field = output_field
        self.timeout_s = float(timeout_s or 30.0)
        self._transport = transport

    @property
    def name(self) -> str:
        return f"sam2:{self.dialect}" if self.dialect else "none"

    def available(self) -> bool:
        """Config only. Reaching out to find out would put a round trip in front
        of every photograph to learn something that cannot change between them."""
        if self.dialect == "replicate":
            return bool(self.api_key and self.version)
        if self.dialect == "http":
            return bool(self.endpoint)
        return False

    def _client(self, auth: bool) -> httpx.Client:
        headers = {"Authorization": f"Bearer {self.api_key}"} if (auth and self.api_key) else {}
        kw = dict(timeout=httpx.Timeout(self.timeout_s, connect=8.0), headers=headers)
        if self._transport is not None:
            kw["transport"] = self._transport
        return httpx.Client(**kw)

    # -- the Segmenter protocol ---------------------------------------------

    def segment(self, rgb, points):
        """One mask per point, None where it could not be measured.

        Never raises. A segmenter that fails costs a MEASUREMENT, and the
        estimator falls back to the model's own area claim -- which is exactly
        where it is today.
        """
        out: list[Segmentation | None] = [None] * len(points or [])
        if not self.available() or rgb is None or not points:
            return out
        try:
            image = encode_image(rgb)
            if not image:
                return out
            shape = (rgb.shape[0], rgb.shape[1])
            if self.mode == "auto":
                masks = self._auto_masks(image, shape)
                for i, point in enumerate(points):
                    mask = self._smallest_containing(masks, point)
                    if mask is not None and mask.any():
                        out[i] = Segmentation(mask=mask, score=None, source=self.name)
                return out
            for i, point in enumerate(points):
                raw = self._call(image, [point])
                mask = decode_mask(raw, shape) if raw else None
                if mask is not None and mask.any():
                    out[i] = Segmentation(mask=mask, score=None, source=self.name)
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("segmenter_failed", provider=self.name, error=str(exc)[:200])
            return [None] * len(points)

    # -- the job that is actually blocking us --------------------------------

    def plate_outline(self, rgb, plate_bbox: dict):
        """The whole plate, outer rim included, or None.

        None is a supported answer and the honest one: six pixel-only methods
        have failed at this, and a plate outline that is wrong is worse than
        none at all, because the depth scale and the footprint both rest on it.
        """
        if not self.available() or rgb is None or getattr(rgb, "ndim", 0) != 3:
            return None
        shape = (rgb.shape[0], rgb.shape[1])
        if self.mode != "auto":
            prompts = rim_points(plate_bbox or {}, shape)
            if len(prompts) < 4:
                log.info("plate_prompt_unusable", points=len(prompts))
                return None
        try:
            image = encode_image(rgb)
            if not image:
                return None
            if self.mode == "auto":
                # Every mask on the table is already in hand from the one call
                # this photograph costs. The plate is the LARGEST of them that
                # still looks like a plate -- plate_is_plausible is what says
                # so, and it is the same check the prompted path answers to, so
                # a plate that would have been refused there is refused here.
                mask = None
                for candidate in reversed(self._auto_masks(image, shape)):
                    good, _ = plate_is_plausible(candidate)
                    if good:
                        mask = candidate
                        break
            else:
                raw = self._call(image, prompts)
                mask = decode_mask(raw, shape) if raw else None
        except Exception as exc:  # noqa: BLE001
            log.warning("plate_outline_failed", error=str(exc)[:200])
            return None
        if mask is None:
            return None
        good, why = plate_is_plausible(mask)
        if not good:
            log.info("plate_outline_rejected", reason=why)
            return None
        log.info("plate_outline_measured", area=round(float(mask.mean()), 4))
        return mask

    # -- the automatic mask generator ----------------------------------------
    #
    # WHY THIS EXISTS AT ALL
    #
    # `meta/sam-2` on Replicate takes `points_per_side`, not point coordinates.
    # It is the AUTOMATIC mask generator: it segments everything it can find and
    # hands back a list. Prompting it with the box centres -- which is what this
    # class did, and what every other SAM2 integration assumes -- sends a field
    # the model has never heard of. The call succeeds, is billed, and comes back
    # without the mask that was asked for. `dev segcheck` caught that before a
    # single call was paid for; it would otherwise have looked exactly like the
    # "0 live" we have been reading for weeks.
    #
    # So: one call per PHOTOGRAPH rather than one per item, which is also
    # cheaper, and the choosing happens here.

    # Enough masks for a crowded plate, and a hard stop on a model that decides
    # to return two hundred. Each one is a separate download.
    MAX_AUTO_MASKS = 64

    def _auto_masks(self, image: bytes, shape: tuple[int, int]) -> list:
        """Every mask the model found, largest last. One call, memoised.

        Memoised because `segment` and `plate_outline` are separate entry
        points asking about the SAME photograph. Without this the plate outline
        doubles the bill for an answer already sitting in memory.

        THE BENCH'S `--runs N` DEPENDS ON THIS CACHE, AND NOTHING ELSE SAYS SO.

        The key is sha256 of the encoded image and `_SEGMENTER` is a process
        singleton, so N runs of one photograph share ONE segmenter call: runs
        two and three get the identical list object back. `--runs 3` therefore
        measures the VISION model's variance and not the segmenter's, and every
        spread the bench reports excludes the segmenter by construction.

        That is useful -- it isolates one variable cleanly, and it is why the
        3.8x footprint swing on photo 35 could be attributed to the model's box
        rather than to SAM2. It is also a FLOOR rather than a total: in
        production every scan is a fresh photograph with a fresh digest, so the
        segmenter runs cold every time and contributes whatever variance it
        has, which no bench run to date could have detected.

        `log.info("sam2_auto_masks", ...)` below fires only on a MISS, so the
        count of that line in a run's log is the count of real segmenter calls.

        Change the lifetime or the key here and you silently change what the
        bench measures. If this ever becomes per-request, the published spreads
        stop being comparable with anything measured before it.
        """
        digest = hashlib.sha256(image).hexdigest()
        if self._auto_memo and self._auto_memo[0] == digest:
            return self._auto_memo[1]
        body = {"version": self.version, "input": {
            self.image_field: "data:image/jpeg;base64," + base64.b64encode(image).decode(),
            **self.extra_input,
        }}
        payload = self._predict(body)
        masks: list = []
        items = self._individual(payload)
        if items is None:
            log.warning("sam2_auto_no_individual_masks",
                        keys=",".join(sorted(payload.keys()))[:120]
                        if isinstance(payload, dict) else type(payload).__name__)
        for entry in (items or [])[:self.MAX_AUTO_MASKS]:
            raw = self._fetch(entry)
            m = decode_mask(raw, shape) if raw else None
            if m is not None and m.any():
                masks.append(m)
        masks.sort(key=lambda m: int(m.sum()))
        self._auto_memo = (digest, masks)
        # AREAS, NOT JUST A COUNT. A count match is not an identity match.
        #
        # `mask_stability` caches masks by encoding the local file; production
        # uploads to storage, fetches back, decodes and re-encodes through the
        # scan path. Same picture, different bytes -- and SAM2 is deterministic
        # only for identical bytes, which is what that probe measured. The two
        # sets matched at 16 and were NOT the same 16: replaying production's
        # own logged box over the cached masks takes 6 where production took 7,
        # which is impossible under a per-mask rule from a superset, so the
        # masks must differ. Sorted areas make that visible in one line instead
        # of by deduction.
        log.info("sam2_auto_masks", found=len(masks),
                 areas=[int(m.sum()) for m in masks],
                 digest=digest[:12])
        return masks

    def _individual(self, payload):
        """The list of per-object masks, whatever this model calls it."""
        out = (payload or {}).get("output") if isinstance(payload, dict) else None
        if isinstance(out, dict):
            if self.output_field and self.output_field in out:
                got = out[self.output_field]
                return got if isinstance(got, (list, tuple)) else [got]
            for key in ("individual_masks", "masks", "segmentations"):
                if isinstance(out.get(key), (list, tuple)):
                    return list(out[key])
            return None
        if isinstance(out, (list, tuple)):
            return list(out)
        return None

    # A mask belongs to an item when its CENTRE is inside the item's box, and
    # it is not so large that it swallows the box whole.
    #
    # THE FIRST VERSION OF THIS RULE MEASURED AREA OVERLAP, AND INHERITED THE
    # BOX. It kept every mask with 60% of its area inside the box. That works,
    # and it is unstable: the model's boxes are quantised to a 0.05 grid, and
    # on the bench the zucchini's box stepped 16.0% -> 25.0% of frame between
    # two runs of identical code. Area overlap turns one grid step into a
    # different set of masks -- the measured footprint went 6.3% -> 11.9% and
    # the weight followed, 96 g to 146 g. A measurement that moves with the box
    # is the box wearing a measurement's clothes, which is the failure this
    # whole file exists to avoid.
    #
    # A centre is far steadier: a mask centred inside a 16% box is still
    # centred inside the 25% box that replaces it. Only masks sitting on the
    # boundary change hands.
    #
    # CONTAINS_BOX_LIMIT is what keeps the plate out, and it needs no plate
    # detection to do it. A food mask lies INSIDE the box drawn round that
    # food; it cannot cover the box's own corners. The plate covers the box
    # entirely. So: any mask covering essentially all of the box is not a thing
    # in the box, it is the thing the box is sitting on.
    # No single item is more than this share of the plate. A whole-plate
    # mask is the plate; measured on the weighed photographs the largest real
    # single food covered 41% of its plate.
    MAX_ITEM_OVER_PLATE = 0.60
    CONTAINS_BOX_LIMIT = 0.95

    # ...AND A SIZE CEILING, because "covers the box" is not enough on its own.
    #
    # Measured on the bench, live: the carrots came back as a union covering
    # 84.8% of the frame -- the plate AND the table, both counted as carrots --
    # with `swallowed_box=0`, meaning the covers-the-box test excluded nothing.
    # It could not: SAM2 had segmented the ten carrots separately, so its PLATE
    # mask has ten carrot-shaped holes punched in it and covers only ~90% of
    # the box. Ninety, not ninety-five, so the plate slipped through as food.
    #
    # A hole-punched plate is still a plate, and the giveaway is size, not
    # coverage: a mask five times the area of the box drawn round the food is
    # not that food.
    #
    # 2.0 and not lower, because this project's boxes are known to run SMALL.
    # Ruler-measured: a real 4.84% footprint boxed at 4.00% (1.21x), a real
    # 4.50% boxed at 3.00% (1.50x). A ceiling at 1.5 would throw away real
    # measurements. Two clears the widest verified undersizing and still
    # refuses a plate at 2.4x and a table at 5x.
    MAX_MASK_OVER_BOX = 2.0

    # A hard ceiling on what one item may be, as a share of the whole frame.
    #
    # The box-relative tests above cannot settle every case: a box tight enough
    # to sit entirely inside the food is "covered" by the food, exactly as it
    # is by a plate, and no rule keyed to the box can tell those apart. Frame
    # share can. On these photographs the plate itself is 39-45% of the frame
    # and the most spread-out single food measured -- spaghetti at 42% of its
    # plate -- is about 17%. A mask covering more than a third of the entire
    # photograph is a plate, a table, or the photograph; it is not one portion.
    #
    # This one is absolute. Even the last-resort fallback will return no
    # measurement rather than something over it, because no measurement is
    # honest and a plate reported as food is not.
    MAX_MASK_FRAME_SHARE = 0.35

    def segment_boxes(self, rgb, points, boxes, plate_hint=None):
        """Item masks, chosen by BOX rather than by a single point.

        WHY NOT THE POINT.

        A point picks the smallest mask containing it, which is right for a
        single lump and catastrophically wrong for scattered food. The model's
        box around eight baby carrots has its centre on BARE PLATE between two
        of them; the smallest mask containing that pixel is the plate. Measured
        on the bench: the carrots came back as 34.9% of the frame against a
        plate covering 41% -- 85% of the plate, published as 686 g of carrots
        that weighed 58 g.

        So: take every mask that lies mostly inside the item's box and union
        them. Eight carrot masks are all inside; the plate is not. Falling back
        to the point keeps the single-lump case working when a box is too tight
        for anything to sit inside it.
        """
        out: list = [None] * len(points or [])
        if not self.available() or rgb is None or not points:
            return out
        if self.mode != "auto":
            return self.segment(rgb, points)
        try:
            image = encode_image(rgb)
            if not image:
                return out
            H, W = rgb.shape[:2]
            masks = self._auto_masks(image, (H, W))
            raw_masks = masks
            # THE PLATE, BY NAME, ONCE PER PHOTOGRAPH.
            #
            # Found by its hole signature rather than by its size against the
            # model's box, because that box moves on a 0.05 grid and the plate
            # does not. Everything at least as large as the plate goes with it:
            # the tabletop, and any mask that has swallowed the plate.
            # THE PLATE HANDED IN, WHEN SAM2 DID NOT RETURN ONE.
            #
            # On four weighed photographs SAM2 returns no plate mask at all --
            # (see the note in the no-plate branch below: the cause of that
            # is not established, and two guesses at it have been wrong).
            # Without a plate the size tests fall back to ratios against the
            # model's box, which moves on a 0.05 grid, and the footprint ranged
            # 1.4x, 5.1x and on the rice 389x -- 0.09% of the frame to 33.72%.
            #
            # The colour rule finds a plate on ALL of them. It is not good
            # enough to MEASURE food with (tested against SAM2 on seven weighed
            # photographs it came in between 0.10x and 1.70x, so it is not an
            # area) but it is entirely good enough to say WHERE THE PLATE IS --
            # which is all that is needed here.
            #
            # Two bounds, both about the plate rather than the box:
            #   on the plate   a mask whose centre is off the plate is not food
            #   plate-sized    no single item is most of the plate
            if plate_hint is not None and plate_hint.any():
                hp = float(plate_hint.sum())
                bounded = []
                for m in masks:
                    if float(m.sum()) > self.MAX_ITEM_OVER_PLATE * hp:
                        continue
                    ys, xs = np.nonzero(m)
                    if not len(ys):
                        continue
                    cy, cx = int(ys.mean()), int(xs.mean())
                    if not plate_hint[cy, cx]:
                        continue
                    bounded.append(m)
                if bounded:
                    log.info("plate_hint_bound", before=len(masks),
                             after=len(bounded))
                    masks = bounded
            plate = plate_among(masks, (H, W))
            if plate is not None:
                # Everything at least as large as the plate goes with it: the
                # tabletop, and any mask that has swallowed the plate.
                floor = float(plate.mean())
                masks = [m for m in masks if float(m.mean()) < floor]
                log.info("plate_excluded", plate=round(floor, 4),
                         left=len(masks))
            else:
                # NO PLATE FOUND, AND THIS IS NOT SAFE -- IT IS ONLY UNCHANGED.
                #
                # Without the plate the size tests fall back to ratios against
                # the model's box, which moves on a 0.05 grid. On the four
                # On four of this user's weighed photographs SAM2 returns no
                # plate mask at all. WHY IS NOT ESTABLISHED, and two guesses have
                # already been wrong: a "blue plate vs white plate" distinction
                # that did not exist (every plate in this set is the same white
                # 9-inch plate -- the blue came from a debug overlay's own tint),
                # and then food-plate contrast, which was measured and does not
                # separate the cases either:
                #
                #     found     contrast 39, food covering  1% of the plate
                #     NOT found contrast 31, food covering 77%
                #     NOT found contrast 103, food covering 20%
                #     found     contrast 104, food covering 33%
                #
                # So this handles the SYMPTOM and does not claim to know the cause.
                # Without a plate the size tests fall back to ratios against the
                # model's box, which moves on a 0.05 grid, and the footprint on the
                # rice ranged 0.09% of the frame to 33.72% -- 389x on one photo.
                #
                # Refusing outright was tried and is the better rule, but it
                # also silences five scenes this file tests that have no plate
                # in them at all, and a change that large belongs on its own.
                # The real fix is to hand the plate in from the colour rule,
                # which finds one on every photograph including these four.
                log.info("no_plate_for_box_rule", masks=len(masks))
            # Nothing this big is one item. Applied once, here, so every path
            # below -- box rule and fallback alike -- inherits it.
            masks = [m for m in masks
                     if float(m.mean()) <= self.MAX_MASK_FRAME_SHARE]
            # EVERYTHING A REPLAY NEEDS, CAPTURED WHERE THE CHOOSING HAPPENS.
            #
            # Before the `not masks` return, because an empty pool is a result
            # and not a reason to write nothing: "production had nothing to
            # choose from" and "the dump is off" must not look alike offline.
            dump_masks(image, (H, W), raw_masks, masks, boxes, points,
                       plate_hint=plate_hint)
            if not masks:
                return out
            for i, point in enumerate(points):
                box = (boxes or [None] * len(points))[i] if boxes else None
                chosen = self._union_in_box(masks, box, (H, W), item=i)
                if chosen is None:
                    # THE FALLBACK OBEYS THE SAME EXCLUSIONS.
                    #
                    # It did not, and that was a second door onto the same bug:
                    # when the box rule found nothing, "the smallest mask
                    # containing the seed" happily returned the plate. Found by
                    # replaying a real pizza photograph -- the union came back
                    # empty and the fallback published 35% of the frame.
                    # Prefer a mask that is not the surface. If excluding the
                    # surface leaves nothing at all, take the smallest
                    # containing mask anyway rather than returning no
                    # measurement: a box tight enough to be "covered" by the
                    # food itself is pathological, and the plate guard in
                    # portion.py still refuses an implausible footprint
                    # downstream. Losing a good measurement to a pathological
                    # box would be a worse trade than one more thing for that
                    # guard to catch.
                    safe = self._not_the_surface(masks, box, (H, W))
                    chosen = (self._smallest_containing(safe, point)
                              if safe else None)
                    if chosen is None:
                        # LAST RESORT, ON A TIGHTER LEASH.
                        #
                        # Getting here means the box told us nothing usable --
                        # it is small enough to sit inside the food itself, so
                        # "covers the box" is true of the food as readily as of
                        # the plate and no box-relative test can separate them.
                        # With the box mute, size is the only evidence left, so
                        # the bar drops to half the frame-share ceiling. The
                        # largest single food ever measured here is spaghetti
                        # at about 17% of frame; a plate is 25-45%. Anything
                        # above the line is refused outright and the item goes
                        # unmeasured, which is the honest answer.
                        candidate = self._smallest_containing(masks, point)
                        limit = self.MAX_MASK_FRAME_SHARE / 2.0
                        chosen = (candidate if candidate is not None
                                  and float(candidate.mean()) <= limit else None)
                # THE UNION IS RE-CHECKED, NOT JUST ITS PARTS.
                #
                # The ceiling above filters individual masks. `_union_in_box`
                # then ORs several together and nothing looked at the result --
                # so a handful of legal masks could still add up past the line
                # that exists to stop exactly that. It is the 686 g carrot
                # returning through the front door instead of the back.
                if (chosen is not None
                        and float(chosen.mean()) > self.MAX_MASK_FRAME_SHARE):
                    log.info("sam2_union_over_ceiling",
                             area=round(float(chosen.mean()), 4))
                    chosen = None
                if chosen is not None and chosen.any():
                    out[i] = Segmentation(mask=chosen, score=None, source=self.name)
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("segmenter_failed", provider=self.name, error=str(exc)[:200])
            return [None] * len(points)

    def _not_the_surface(self, masks: list, box, shape) -> list:
        """The masks that could be an item in this box, not the thing under it.

        One definition of "is this the plate", used by the box rule and by the
        fallback, so the two cannot drift apart -- which they did once already,
        and it cost a bench run.
        """
        if not box:
            return list(masks)
        H, W = shape
        try:
            x = float(box.get("x", 0.0)); y = float(box.get("y", 0.0))
            w = float(box.get("w", 0.0)); h = float(box.get("h", 0.0))
        except (AttributeError, TypeError, ValueError):
            return list(masks)
        if not (w > 0 and h > 0):
            return list(masks)
        x0, x1 = int(max(0, x * W)), int(min(W, (x + w) * W))
        y0, y1 = int(max(0, y * H)), int(min(H, (y + h) * H))
        if x1 <= x0 or y1 <= y0:
            return list(masks)
        box_px = float((x1 - x0) * (y1 - y0)) or 1.0
        keep = []
        for m in masks:
            area = int(m.sum())
            if not area:
                continue
            inside = int(m[y0:y1, x0:x1].sum())
            if (inside / box_px >= self.CONTAINS_BOX_LIMIT
                    or area > box_px * self.MAX_MASK_OVER_BOX):
                continue
            keep.append(m)
        return keep

    def _union_in_box(self, masks: list, box, shape,
                      item: int | None = None) -> "np.ndarray | None":
        """Union of the masks that live inside this box. None if there are none."""
        if not box:
            return None
        H, W = shape
        try:
            x = float(box.get("x", 0.0)); y = float(box.get("y", 0.0))
            w = float(box.get("w", 0.0)); h = float(box.get("h", 0.0))
        except (AttributeError, TypeError, ValueError):
            return None
        if not (w > 0 and h > 0):
            return None
        x0, x1 = int(max(0, x * W)), int(min(W, (x + w) * W))
        y0, y1 = int(max(0, y * H)), int(min(H, (y + h) * H))
        if x1 <= x0 or y1 <= y0:
            return None
        box_px = float((x1 - x0) * (y1 - y0)) or 1.0
        union = np.zeros((H, W), bool)
        taken = dropped = 0
        for m in masks:
            area = int(m.sum())
            if not area:
                continue
            inside = int(m[y0:y1, x0:x1].sum())
            if (inside / box_px >= self.CONTAINS_BOX_LIMIT
                    or area > box_px * self.MAX_MASK_OVER_BOX):
                dropped += 1          # the plate, or the table under it
                continue
            ys, xs = np.nonzero(m)
            cx, cy = float(xs.mean()), float(ys.mean())
            if not (x0 <= cx < x1 and y0 <= cy < y1):
                continue
            # AND THE CENTRE HAS TO BE ON THE MASK.
            #
            # A table is a RING around the plate. Its centroid sits in the
            # middle of that ring -- on the plate, inside the item's box -- and
            # if the box is large enough the ring also slips under the size
            # ceiling. Both tests pass and the table joins the food. Found by
            # fuzzing 400 random layouts before this shipped: it leaked in 37
            # of them, roughly one in eleven.
            #
            # A piece of food is a blob and its centre is on it. A ring's
            # centre is in the hole. One lookup separates them.
            #
            # Applied only to masks at least as big as the box. A small mask is
            # a piece of food, and food is not always convex -- a pizza slice
            # read as a crescent had its own centroid fall just off itself, was
            # dropped, left the union empty, and sent the whole thing down the
            # fallback path onto the plate. Rings that matter are big ones.
            if area >= box_px and not m[int(round(cy)), int(round(cx))]:
                dropped += 1
                continue
            union |= m
            taken += 1
        if not taken or not union.any():
            return None
        # THE BOX IS LOGGED BECAUSE IT IS THE LAST UNMEASURED INPUT.
        #
        # Every other term in this union is now known deterministic: the mask
        # set (measured -- three cold SAM2 calls, 29 masks, IoU 1.0000), the
        # plate circle (identical across three scans of photo 35), the fence.
        # The box is the only thing left that can move, and we have never seen
        # it move: the bench prints `box 6.0%` for the printed run only, so
        # there is one sample of the quantity the whole diagnosis rests on.
        #
        # The offline replay perturbed it by one and two grid steps because
        # that is what quantisation alone would do, and got 1-6% drift against
        # the 3.8x observed. Either the box moves far more than that -- boxing
        # a whole pile on one call and half of it on the next -- or something
        # inside this rule is at fault. Three unions and three boxes side by
        # side answers it directly instead of by hypothesis.
        # `item` IS WHAT MAKES THREE RUNS READABLE.
        #
        # This fires once per item per scan. Photo 35 has two -- a burger and
        # fries -- so three runs produce six of these lines, and without an
        # index there is no way to say which box belongs to which food. Order
        # cannot be relied on to supply it: the detection order is one of the
        # things that may be varying between runs, which is the question.
        log.info("sam2_box_union", item=item, masks=taken, swallowed_box=dropped,
                 area=round(float(union.mean()), 4),
                 box=(x0, y0, x1, y1),
                 box_frac=round(box_px / float(H * W), 4))
        return union

    @staticmethod
    def _smallest_containing(masks: list, point) -> "np.ndarray | None":
        """The smallest mask this point falls inside.

        SMALLEST, deliberately. The automatic generator returns nested masks --
        a grain of rice, the pile of rice, the plate, and sometimes the whole
        photograph -- and every one of them contains the seed. Taking the
        largest would weigh the plate; taking the first would depend on the
        model's ordering, which is not a contract. The smallest region that
        contains the point is the item the point was aimed at.
        """
        x, y = int(point[0]), int(point[1])
        for m in masks:                       # already sorted small to large
            if 0 <= y < m.shape[0] and 0 <= x < m.shape[1] and m[y, x]:
                return m
        return None

    # -- dialects ------------------------------------------------------------

    def _call(self, image: bytes, points: list) -> bytes | None:
        coords = [[round(float(x), 1), round(float(y), 1)] for x, y in points]
        if self.dialect == "replicate":
            return self._replicate(image, coords)
        return self._http(image, coords)

    def _predict(self, body: dict) -> dict:
        """Submit a prediction and wait for it. {} on any failure.

        Shared by both call shapes -- prompted and automatic -- so that a fix
        to the polling is a fix to both. An empty dict rather than None so the
        callers can read it without a guard on every access.
        """
        deadline = time.monotonic() + self.timeout_s
        with self._client(auth=True) as client:
            resp = client.post(REPLICATE_URL, json=body, headers={"Prefer": "wait"})
            if resp.status_code >= 400:
                log.warning("sam2_http", status=resp.status_code, body=resp.text[:200])
                self.last_error = f"HTTP {resp.status_code}: {resp.text[:160]}"
                return {}
            payload = resp.json()
            while payload.get("status") not in REPLICATE_DONE:
                if time.monotonic() >= deadline:
                    log.warning("sam2_timeout", status=payload.get("status"))
                    self.last_error = f"timed out after {self.timeout_s:.0f}s"
                    return {}
                nxt = (payload.get("urls") or {}).get("get")
                if not nxt:
                    self.last_error = "the prediction gave no address to poll"
                    return {}
                time.sleep(0.5)
                got = client.get(nxt)
                if got.status_code >= 400:
                    self.last_error = f"HTTP {got.status_code} while polling"
                    return {}
                payload = got.json()
            if payload.get("status") != "succeeded":
                log.warning("sam2_failed", status=payload.get("status"))
                self.last_error = (f"the model reported {payload.get('status')}: "
                                   f"{str(payload.get('error'))[:120]}")
                return {}
            self.last_error = None
            return payload if isinstance(payload, dict) else {}

    def _replicate(self, image: bytes, coords: list) -> bytes | None:
        body = {"version": self.version, "input": {
            self.image_field: "data:image/jpeg;base64," + base64.b64encode(image).decode(),
            self.points_field: json.dumps(coords),
            **self.extra_input,
        }}
        payload = self._predict(body)
        if not payload:
            return None
        return self._fetch(payload.get("output"))

    def _http(self, image: bytes, coords: list) -> bytes | None:
        with self._client(auth=True) as client:
            resp = client.post(self.endpoint,
                               json={"image": base64.b64encode(image).decode(),
                                     "points": coords})
            if resp.status_code >= 400:
                log.warning("sam2_http", status=resp.status_code, body=resp.text[:200])
                return None
            if "json" not in resp.headers.get("content-type", ""):
                return resp.content
            return self._fetch(self._from_json(resp.json()))

    def _from_json(self, payload):
        if isinstance(payload, dict):
            if self.output_field:
                return payload.get(self.output_field)
            for key in ("mask", "masks", "output", "segmentation"):
                if key in payload:
                    return payload[key]
        return payload

    def _fetch(self, output) -> bytes | None:
        if isinstance(output, (list, tuple)):
            output = output[0] if output else None
        if isinstance(output, dict):
            output = self._from_json(output)
        if not isinstance(output, str) or not output:
            log.warning("sam2_output_unrecognised", kind=type(output).__name__)
            return None
        if output.startswith("data:"):
            return _b64(output.partition(",")[2])
        if output.startswith(("http://", "https://")):
            # No Authorization header: the file is on object storage, and
            # sending the model key to a CDN is how a credential travels.
            with self._client(auth=False) as client:
                got = client.get(output)
                return got.content if got.status_code < 400 else None
        return _b64(output)


def _b64(text: str) -> bytes | None:
    try:
        return base64.b64decode(text, validate=False)
    except Exception:  # noqa: BLE001
        return None


def from_settings(transport=None):
    """The configured segmenter, or NullSegmenter when none is."""
    from .segmenter import NullSegmenter

    dialect = (settings.segmenter_provider or "").strip()
    if not dialect:
        return NullSegmenter()
    extra = {}
    if settings.segmenter_model_input:
        try:
            parsed = json.loads(settings.segmenter_model_input)
            if isinstance(parsed, dict):
                extra = parsed
        except json.JSONDecodeError:
            log.warning("segmenter_model_input_not_json")
    seg = HostedSegmenter(
        dialect=dialect, api_key=settings.segmenter_api_key,
        endpoint=settings.segmenter_endpoint, version=settings.segmenter_model_version,
        image_field=settings.segmenter_image_field,
        points_field=settings.segmenter_points_field,
        extra_input=extra, output_field=settings.segmenter_output_field,
        timeout_s=settings.segmenter_timeout_s, transport=transport,
        mode=settings.segmenter_mode)
    if not seg.available():
        log.warning("segmenter_misconfigured", dialect=dialect)
        return NullSegmenter()
    return seg
