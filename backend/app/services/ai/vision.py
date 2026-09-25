"""The food-scan pipeline: photo in, logged meal out.

    images -> [GPT-4o Vision] -> detections with area ratios
           -> [portion estimator] -> grams with a confidence band
           -> [nutrition resolver] -> macros per item
           -> [Claude] -> sanity check, merges, corrections, final confidence
           -> meal + meal_items + intake assessment persisted

Every stage degrades rather than failing: if Claude is down we ship the
geometric answer; if the vision model is down we return a clear "add it
manually" result instead of a 500.
"""
from __future__ import annotations

import asyncio
import base64
import math
import io
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

import structlog

from ...config import settings
from ...db import maybe_one, service
from ...models.common import Macros
from ...models.nutrition import DetectedItem, ScanResult
from .. import food_identity, portion_learning, scale_learning
from ..nutrition import assessment, resolver
from .client import ask_reasoning, ask_vision, record_usage
from . import depth_hosted, depth_map, food_seg
from .portion import (
    _key,
    dish_head,
    railed_area,
    CONNECTED_PILE_HEIGHT_MM, SEPARATE_PIECES_HEIGHT_MM,
    GeometryHint, band_label, estimate_grams, food_group, group_density,
    implausible_energy, normalize_bbox, reconcile_multi_image,
)
from .prompts import (
    FOOD_REASONING_SYSTEM, FOOD_VISION_SYSTEM, FOOD_VISION_USER,
    IDENTIFY_CROP_SYSTEM, IDENTIFY_CROP_USER,
)
from .reference_cv import ReferenceFind, find_reference
from .serving_context import typical_for

log = structlog.get_logger()

MAX_IMAGE_EDGE = 1280      # anything larger is wasted tokens
JPEG_QUALITY = 82


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PreparedImage:
    """One photo, ready for the model, plus what we measured off it ourselves."""

    b64: str
    aspect: float | None                 # width / height, AFTER EXIF rotation
    reference: ReferenceFind | None      # a known object found in the pixels
    # The bytes as downloaded, kept so one item can be cut out and looked at
    # again close up. Held only for the life of the scan.
    raw: bytes | None = None


def downscale_jpeg(data: bytes) -> PreparedImage:
    """Resize and re-encode to base64 JPEG, and measure what we can from it.

    Two things come back besides the image. The aspect ratio is width/height of
    the frame THE MODEL ACTUALLY SEES -- after EXIF rotation, which is the whole
    point of measuring it here. The reference is any object of known real size
    found in the pixels, which is the only place in the pipeline holding real
    pixels to find it in.

    Why this is measured rather than assumed: every rung that turns one
    real-world length into a frame AREA needs to know how tall the frame is
    relative to its width. That used to fall back to 4:3 whenever the client
    did not report it, and a phone photo held upright is 3:4 -- so a portrait
    photo was read as landscape and the frame area came out 1.78x too large,
    carrying every gram with it. The image is right here; its shape is a fact,
    not a prior. The old default now only survives for an image we cannot
    even open.
    """
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)          # honour phone orientation
        img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
        w, h = img.size
        aspect = (w / h) if (w and h) else None
        # Look for a reference object while the decoded image is in hand: no
        # extra decode, no tokens, and the answer is the same on every run.
        reference = find_reference(img)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return PreparedImage(
            base64.b64encode(buf.getvalue()).decode(), aspect, reference, data
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("image_downscale_failed", error=str(exc)[:200])
        return PreparedImage(base64.b64encode(data).decode(), None, None, data)


# How much context to keep around a crop. Zero padding cuts a drumstick off at
# its own outline and throws away the plate edge, the bone and the sauce pooling
# beside it -- all of which are what tells meat from mashed beans.
CROP_PAD = 0.28
# Below this the crop is enlarged rather than sent as a postage stamp. The point
# of cropping is to spend pixels on one food; a 60x40 cut-out spends fewer than
# the full frame did.
CROP_MIN_EDGE = 448
CROP_MAX_EDGE = 896


def crop_b64(raw: bytes, bbox) -> str | None:
    """Cut one item out of the original photo, padded and enlarged.

    This is the whole trick behind the second look. A drumstick that covers 3%
    of the frame is about 130x100 pixels in the 1280px image the model was
    shown -- and the model must decide from those pixels whether it is meat,
    beans or potato. Cut the same drumstick out of the ORIGINAL file and blow it
    up and the model gets several times the detail on the one thing in question,
    for a fraction of the tokens a second full-frame pass would cost.

    Returns None rather than raising: a crop that cannot be cut simply means no
    second opinion, and the first one stands.
    """
    box = normalize_bbox(bbox)
    if not box:
        return None
    try:
        from PIL import Image, ImageOps

        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        W, H = img.size
        x = float(box.get("x", 0.0)); y = float(box.get("y", 0.0))
        w = float(box.get("w", 0.0)); h = float(box.get("h", 0.0))
        if w <= 0 or h <= 0:
            return None
        pad_x, pad_y = w * CROP_PAD, h * CROP_PAD
        left   = max(0.0, x - pad_x) * W
        top    = max(0.0, y - pad_y) * H
        right  = min(1.0, x + w + pad_x) * W
        bottom = min(1.0, y + h + pad_y) * H
        if right - left < 8 or bottom - top < 8:
            return None
        cut = img.crop((int(left), int(top), int(right), int(bottom)))
        cw, ch = cut.size
        longest = max(cw, ch)
        if longest < CROP_MIN_EDGE:
            # Upscale. It adds no information, but the model reads a larger
            # image at a finer patch grid, so the information already there
            # survives tokenisation instead of being averaged away.
            k = min(CROP_MIN_EDGE / longest, 4.0)
            cut = cut.resize((max(1, int(cw * k)), max(1, int(ch * k))), Image.LANCZOS)
        elif longest > CROP_MAX_EDGE:
            cut.thumbnail((CROP_MAX_EDGE, CROP_MAX_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        cut.save(buf, format="JPEG", quality=90, optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:  # noqa: BLE001
        log.warning("crop_failed", error=str(exc)[:200])
        return None


async def fetch_images(bucket: str, paths: list[str]) -> list[PreparedImage]:
    """Pull the uploaded objects out of Supabase Storage and prep them."""
    sb = service()

    def _one(path: str) -> PreparedImage | None:
        try:
            return downscale_jpeg(sb.storage.from_(bucket).download(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("image_fetch_failed", path=path, error=str(exc)[:200])
            return None

    results = await asyncio.gather(*(asyncio.to_thread(_one, p) for p in paths))
    return [r for r in results if r]


# ---------------------------------------------------------------------------
# Stage 1: recognition
# ---------------------------------------------------------------------------
# Long enough for "leftover teriyaki beef with rice and broccoli from Tuesday";
# anything past this is not a description of a plate.
USER_NOTE_MAX_CHARS = 200


def user_note_hint(note: str | None) -> str:
    """The person's own description of the plate, as one prompt sentence.

    The scan screen will not open the camera until they have typed one, and
    for a long time it went no further than the request body: the model never
    saw it. A person who typed "teriyaki beef" got "scallops".

    It is a HINT. The model is told to report what it actually sees and to say
    so in scene_notes when the two disagree, because a typo, a vague "dinner",
    or a description of a different plate must not be turned into an
    identification the photo does not support.

    Returns "" for no description, so the prompt is then byte-identical to the
    one sent before this existed. Quotes and line breaks are flattened so the
    person's text stays inside its own quoted sentence.
    """
    text = " ".join(str(note or "").split()).replace('"', "'")[:USER_NOTE_MAX_CHARS].strip()
    if not text:
        return ""
    return (
        f' The person who took the photo describes this plate as: "{text}". '
        "Use that as a hint about what to expect, but report what you actually "
        "see: if the photo does not match the description, name what is really "
        "there and say so in scene_notes rather than forcing a match."
    )


async def detect_foods(images: list[str], user_id: str, note: str | None = None) -> dict:
    multi = (
        f"There are {len(images)} photos of the SAME meal from different angles. "
        "Report the union of foods once, using the clearest view for each."
        if len(images) > 1
        else "There is one photo."
    )
    call = await ask_vision(
        pipeline="food_recognition",
        system=FOOD_VISION_SYSTEM,
        user_text=FOOD_VISION_USER.format(multi_note=multi, user_note=user_note_hint(note)),
        images=images,
        max_tokens=2000,
    )
    await record_usage(call, user_id)
    if not call.ok or not isinstance(call.payload, dict):
        return {"items": [], "plate_detected": False, "plate_area_ratio": 0.0,
                "_error": call.error or "vision returned nothing usable"}
    payload = dict(call.payload)
    payload["_call"] = call
    return payload


# ---------------------------------------------------------------------------
# Stage 2 + 3: geometry and macros
# ---------------------------------------------------------------------------
# Cooking methods worth carrying into the nutrition lookup. Fried and steamed
# versions of the same food differ by more than most portion errors, so
# discarding this was quietly costing accuracy on every fried item: the model
# reported it and only the bare name reached the database.
_PREPARATIONS = {
    "grilled", "fried", "deep_fried", "deep fried", "roasted", "baked",
    "steamed", "boiled", "sauteed", "sautéed", "raw", "smoked", "braised",
    "poached", "battered", "breaded",
}


# What the model reported about its own recognition of the DISH.
IDENTIFICATION_STATES = ("named", "described", "unsure")


def _identification_state(det: dict) -> str:
    """"named", "described" or "unsure" -- never anything else.

    A MISSING field reads as "named", and that is a deliberate choice against
    the safer-looking one. Treating absence as unsure would put "we could not
    identify this" on every item of every scan the moment the model dropped one
    field, or whenever an older cached detection came through -- a wall of
    questions that trains people to dismiss the question, which costs more than
    the occasional unflagged item.

    So absence is trusted and LOGGED instead, because how often the model omits
    it is a fact worth having rather than guessing at.
    """
    if "identification" not in det:
        log.info("identification_state_missing")
        return "named"
    raw = str(det.get("identification") or "").strip().lower()
    if raw in IDENTIFICATION_STATES:
        return raw
    log.info("identification_state_unrecognised", value=raw[:40])
    return "unsure"


def _identification_doubt(det: dict, chosen_group: str) -> str | None:
    """Is this food possibly something from a completely different group?

    Refried beans and ground meat look alike in a photograph and are nothing
    alike on a plate -- about 1.1 kcal per gram against more than double that,
    and a legume against a protein. Measured on a weighed plate: 109 g of
    refried beans and egg came back as "ground meat", and a 146 g drumstick in
    mole as "meat with sauce". Neither is a portion error. Both are the app
    confidently logging the wrong food, which is the one failure a calorie
    counter cannot absorb.

    A within-group alternative is not worth raising -- "drumstick" against
    "chicken leg" changes nothing anyone needs to act on. A CROSS-GROUP one is,
    because it changes the calories, and the honest move is to say so and let
    the person confirm rather than to pick one and sound certain.
    """
    alts = det.get("alternatives")
    if not isinstance(alts, list):
        return None
    for alt in alts:
        if not isinstance(alt, dict):
            continue
        name = _text(alt.get("name"))
        group = food_group(alt.get("food_group"))
        if not name or group == chosen_group:
            continue
        if (_num(alt.get("confidence"), 0.0) or 0.0) < 0.20:
            continue
        return (
            f"this might be {name} rather than what we logged — they are "
            f"different kinds of food ({group} vs {chosen_group}) and the "
            f"calories are not close. Worth confirming."
        )
    return None


# A primary identification at or above this is not a tie, and the surface is
# not consulted. The number is deliberately low-trust: the vision model reports
# 0.9 on foods it later turns out to have confused, so "confident" here means
# "confident enough that a napkin should not get a vote".
SURFACE_TIEBREAK_CEILING = 0.85
# How far below the primary an alternative may sit and still count as a tie.
SURFACE_TIEBREAK_GAP = 0.15
# Below this an alternative is noise, not a candidate.
SURFACE_TIEBREAK_FLOOR = 0.25


def _surface_tiebreak(det: dict, surface: str | None) -> str | None:
    """Let the serving surface settle a tie between two candidate foods.

    Rice, beans and a drumstick do not arrive loose on butcher paper; wrapped
    in a tortilla the same three things arrive on paper as a matter of course.
    That is real information and it is free -- the photo already contains it.

    The limits are the point, so they are enforced here rather than trusted to
    the caller:

      * it only runs when the model itself offered an alternative,
      * only when the primary is NOT confident (< SURFACE_TIEBREAK_CEILING),
      * only when the alternative is within SURFACE_TIEBREAK_GAP of it,
      * only when the surface has an opinion in BOTH directions -- the primary
        is not served this way AND the alternative is,
      * and it never removes an item, never touches a portion, and never
        invents a candidate the model did not itself put on the list.

    The item that loses becomes the alternative, so `_identification_doubt`
    still raises it and the person still gets to disagree. Mutates `det` in
    place and returns a note, or None when it changed nothing -- which is the
    overwhelmingly common case.
    """
    if not surface:
        return None
    alts = det.get("alternatives")
    if not isinstance(alts, list) or not alts:
        return None
    primary_name = _text(det.get("name"))
    if not primary_name:
        return None
    primary_conf = _num(det.get("confidence"), 0.6) or 0.6
    if primary_conf >= SURFACE_TIEBREAK_CEILING:
        return None
    if typical_for(surface, primary_name) is not False:
        return None

    best: dict | None = None
    best_conf = 0.0
    for alt in alts:
        if not isinstance(alt, dict):
            continue
        alt_name = _text(alt.get("name"))
        if not alt_name:
            continue
        alt_conf = _num(alt.get("confidence"), 0.0) or 0.0
        if alt_conf < SURFACE_TIEBREAK_FLOOR:
            continue
        if alt_conf < primary_conf - SURFACE_TIEBREAK_GAP:
            continue
        if typical_for(surface, alt_name) is not True:
            continue
        if alt_conf > best_conf:
            best, best_conf = alt, alt_conf
    if best is None:
        return None

    winner = _text(best.get("name"))
    loser_group = det.get("food_group")
    # The loser keeps its seat on the list, so the doubt note still fires and
    # the person can put it back. Nothing is deleted.
    remaining = [a for a in alts if a is not best]
    remaining.insert(0, {
        "name": primary_name,
        "food_group": loser_group,
        "confidence": round(primary_conf, 3),
    })
    det["name"] = winner
    if best.get("food_group"):
        det["food_group"] = best.get("food_group")
    det["alternatives"] = remaining
    # Identity changed, so an identity-derived prior that came with the winner
    # replaces the loser's. If the winner brought none, the loser's is kept --
    # the two candidates looked alike enough to be confused, so their typical
    # servings are closer to each other than to a generic default.
    if _num(best.get("typical_serving_g")):
        det["typical_serving_g"] = best.get("typical_serving_g")
    surface_label = str(surface).replace("_", " ")
    return (
        f"read as {winner} rather than {primary_name}: the two looked alike to "
        f"the camera, and {primary_name} is not normally served loose on "
        f"{surface_label}. If it was {primary_name}, correcting it here will "
        f"fix the calories."
    )


def _lookup_name(det: dict) -> str:
    """The name to look up nutrition facts under.

    Includes the cooking method when the model reported one and the name does
    not already carry it -- "fried chicken thigh" resolves to something quite
    different from "chicken thigh", and the difference is mostly fat.
    """
    name = str(det.get("name") or "food").strip().lower()
    prep = str(det.get("preparation") or "").strip().lower().replace("_", " ")
    if prep and prep not in {"unknown", "none", ""} and prep in _PREPARATIONS:
        if prep.split()[0] not in name:
            return f"{prep} {name}"[:120]
    return name


# Preparations that ADD fat the food did not start with. Frying, sauteing and
# battering put oil into the food; grilling and boiling do not.
#
# This distinction is the mechanism behind the single documented failure of
# every competing app. NIH/NIDDK tested MyFitnessPal, LoseIt!, CalAI and
# Appediet against 102 meals weighed to 0.1 g in a metabolic kitchen: all four
# underestimated energy by roughly a third -- 250 to 345 kcal a meal -- and the
# researchers attributed it largely to undercounting dietary FAT, about 30 g per
# meal. Oil is invisible in a photograph. It is the thing a camera cannot see.
FAT_ADDING_PREPARATIONS = frozenset({
    "fried", "deep_fried", "deep fried", "sauteed", "sautéed",
    "battered", "breaded",
})

# Words in a matched food's name that mean the fat is already accounted for.
COOKED_IN_FAT_WORDS = ("fried", "saute", "sauté", "batter", "bread", "crisp",
                       "tempura", "schnitzel", "fritter", "doughnut", "donut")


def _fat_preparation_lost(det: dict, fact: dict | None) -> str | None:
    """Did a fat-adding preparation survive into the food we matched?

    The pipeline folds the preparation into the lookup name, so "fried chicken
    thigh" is what gets searched -- and a USDA entry for fried chicken already
    contains the oil it absorbed. That works when the search finds one.

    When it does not, the request quietly degrades to the plain food and the oil
    vanishes with it, and nothing downstream notices: `implausible_energy` only
    fires below HALF a food group's floor, and a fried item matched to its
    roasted twin sits comfortably above that.

    So this compares what was ASKED for against what came BACK. No constant is
    invented and no fat is added -- adding some would double-count the entries
    that did resolve correctly, and there is no weighed macro truth to check it
    against. It reports the contradiction and lets the person decide, which is
    the same rule the energy check follows.
    """
    prep = str(det.get("preparation") or "").strip().lower().replace("_", " ")
    if prep not in FAT_ADDING_PREPARATIONS:
        return None
    matched = str((fact or {}).get("display_name") or "").strip().lower()
    if not matched:
        return None
    if any(w in matched for w in COOKED_IN_FAT_WORDS):
        return None
    return (
        f"looked up as {prep}, but matched \"{matched}\", which is not a "
        f"{prep} entry — any oil it was cooked in is probably missing from "
        f"these numbers. Photo-based apps undercount fat more than anything "
        f"else, so this is worth a look."
    )


def _calibration_fits(calibration: dict | None, detected_vessel: str | None) -> bool:
    """Does this saved measurement describe the vessel in THIS photo?

    A calibration with no vessel recorded is the user's general default and is
    accepted -- that is what it is for. One that names a vessel is only used on
    that vessel. Anything else applies a plate's real size to a bowl's outline.
    """
    if not calibration:
        return False
    saved = _key(calibration.get("vessel"))
    if not saved:
        return True
    return saved == _key(detected_vessel)


def _text(value) -> str | None:
    """A short string from a model response, or None. Never raises."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    out = str(value).strip()
    return out[:120] or None


def _num(value, default: float | None = None) -> float | None:
    """A number from a model response, or the default. Never raises.

    Every `float(...)` on model output in this file used to be unguarded, and
    each one is a 500 on an otherwise good photograph: a model that answers
    "15%" instead of 0.15, or null, or a list, took down the whole scan --
    after the meal row had already been written, leaving the scan stuck in
    'processing' and the user's AI quota spent.

    normalize_bbox already defends exactly this for one field. This is the same
    defence for the rest of them.
    """
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):   # NaN / inf
        return default
    return out


def _explicit_density(fact: dict) -> float | None:
    """The nutrition row's own density, but ONLY when it is not an
    ``ai_estimate`` row's guess.

    ``food_facts.source`` distinguishes ``usda``/``edamam``/``nutritionix``/
    ``user`` from ``ai_estimate`` -- a reasoning-model guess, cached from a
    moment a real provider failed or found nothing. This used to be passed
    through on presence alone (a comment right above the call site already
    claimed "ONLY a real measured density is passed as explicit", but the
    code never checked `source` to back that up), and `density_for` puts an
    explicit density at precedence rank 1, above its own dish-match table --
    so whenever USDA missed, an LLM's guess silently outranked the real
    table. Documented in HANDOFF.md ("LAUNCH BLOCKER: GRAMS DEPEND ON USDA
    UPTIME"): the identical cached photo logged 350g one time and 98g
    another for the same caesar salad, purely on a third party's uptime at
    scan time. Blocking `ai_estimate` here sends it through the exact same
    path as a food with no explicit density at all -- `density_for` falls
    through to its own dish/group table, rank 2 onward, unchanged.
    """
    if fact.get("source") == "ai_estimate":
        return None
    return _num(fact.get("density_g_ml"))


def _plate_ellipse(detection: dict, aspect: float | None) -> tuple[float, float] | None:
    """The vessel's apparent (w, h) as PER-AXIS fractions: w of the image's
    width, h of its height. That is what every consumer reads.

    It is NOT what the model reports. Measured on 26 bench photos, the model
    gives w and h both as fractions of the image's LONG side, portrait and
    landscape alike (mean |diff| to the pixel rim 0.099 long-side against 0.245
    per-axis). Read raw, a round plate shot straight down parsed as arccos(0.75)
    = 41.4 degrees on every 3:4 photo, and `scale_learning.observe_width_mm`
    multiplied a portrait frame's SHORT side by a long-side share: x0.75 on
    every learned width. Converted here, once, because this is the only place a
    model `plate_ellipse` becomes numbers -- `GeometryHint.tilt_deg` and
    `observe_width_mm` are then correct as written.

    `aspect` is width / height of the frame the model was shown. Without it the
    fractions cannot be converted and a guessed tilt is worse than none: None.
    Required, not defaulted, so a caller that forgets it fails loudly instead of
    silently reading the old convention.

    Not in this conversion, deliberately: the model's ~0.10 under-read of the
    plate. That is a measurement bias, and folding it into a unit conversion
    would hide it.

    A converted fraction can exceed 1.0 (a portrait plate spanning more than the
    frame's width). It is returned as is; `observe_width_mm` already refuses a
    share above 1.

    Same liberality as the item bounding boxes: a dict, a bare [w, h] list, or
    nulls. A malformed ellipse means no tilt reading, never a failed scan.
    """
    raw = detection.get("plate_ellipse")
    if isinstance(raw, dict):
        w, h = raw.get("w"), raw.get("h")
    elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
        w, h = raw[0], raw[1]
    else:
        return None
    try:
        w, h = float(w), float(h)
    except (TypeError, ValueError):
        return None
    if not (0 < w <= 1.0 and 0 < h <= 1.0):
        return None
    try:
        aspect = float(aspect)
    except (TypeError, ValueError):
        return None
    if not aspect > 0:
        return None
    if aspect < 1.0:
        # Portrait: the long side is the height. h is already height units;
        # w is a share of the height, so widen it into width units.
        return (w / aspect, h)
    # Landscape (or square): the long side is the width.
    return (w, h * aspect)


def _visible_fraction(det: dict) -> float | None:
    """How much of this item can be seen, 0-1.

    Prefers the fraction; falls back to the older boolean, where "occluded"
    with no number is treated as roughly a third hidden -- enough to correct
    upward without inventing precision.
    """
    vf = det.get("visible_fraction")
    if isinstance(vf, (int, float)) and 0.0 < float(vf) <= 1.0:
        return float(vf)
    if det.get("occluded") is True:
        return 0.65
    return None


# ---------------------------------------------------------------------------
# Stage 1b: the second look
# ---------------------------------------------------------------------------
# A first-pass identification at or above this is left alone. Everything below
# it that also has a usable box is a candidate for a closer look.
SECOND_LOOK_CONFIDENT = 0.72
# At most this many crops per scan. Each is small, but the point of this pass is
# that it is paid only where it is needed.
SECOND_LOOK_MAX_ITEMS = 3
# Below this the close-up did not settle anything either, and the first pass
# keeps its answer.
SECOND_LOOK_MIN_CONFIDENCE = 0.45


def _needs_second_look(det: dict) -> bool:
    """Is this identification worth spending a crop on?

    Two things earn one. A cross-group alternative, because the two candidates
    disagree about what kind of food this is and therefore about the calories.
    And a low-confidence primary with nothing offered against it, because the
    model saying 0.4 about a food it named alone is the same doubt with nobody
    to voice it.

    A confident, uncontested identification is left alone. Most items on most
    plates are exactly that, which is what keeps this pass cheap.
    """
    if not normalize_bbox(det.get("bbox")):
        return False
    conf = _num(det.get("confidence"), 0.6) or 0.6
    chosen = food_group(det.get("food_group"))
    alts = det.get("alternatives")
    if isinstance(alts, list):
        for alt in alts:
            if not isinstance(alt, dict):
                continue
            if not _text(alt.get("name")):
                continue
            if (_num(alt.get("confidence"), 0.0) or 0.0) < 0.20:
                continue
            if food_group(alt.get("food_group")) != chosen:
                return True
    return conf < SECOND_LOOK_CONFIDENT


async def second_look(
    detections: list[dict], raw: bytes | None, user_id: str
) -> list[str]:
    """Re-ask about the foods in doubt, one close crop each.

    The first pass looks at the whole meal at 1280px and has to name every food
    on it at once. A drumstick covering 3% of that frame gets roughly 130x100
    pixels, and from those pixels the model decides meat or bean or potato.
    Measured on the weighed bench: 08 and 14 are the SAME plate, and 14 called
    its refried beans "baked meatloaf" -- a legume logged as a meat, at more
    than double the calories per gram, with no doubt raised at all.

    So the doubtful items get looked at again, alone, enlarged, with the rest of
    the meal out of frame. One extra vision call, only when something is in
    doubt, and no question asked of the user.

    Mutates the detections it is confident about and returns notes. A second
    look that is not itself confident changes nothing -- being unsure twice is
    not evidence, and the first answer at least came with the whole plate for
    context.
    """
    notes: list[str] = []
    if not raw:
        return notes
    candidates = [d for d in detections if _needs_second_look(d)][:SECOND_LOOK_MAX_ITEMS]
    if not candidates:
        return notes

    crops: list[str] = []
    asked: list[dict] = []
    questions: list[str] = []
    for det in candidates:
        b64 = await asyncio.to_thread(crop_b64, raw, det.get("bbox"))
        if not b64:
            continue
        idx = len(asked)
        first = _text(det.get("name")) or "food"
        alt_names = [
            _text(a.get("name")) for a in (det.get("alternatives") or [])
            if isinstance(a, dict) and _text(a.get("name"))
        ][:2]
        if alt_names:
            questions.append(
                f"[{idx}] the first pass said \"{first}\" but also considered "
                f"{' or '.join(alt_names)}. Which is it?"
            )
        else:
            questions.append(
                f"[{idx}] the first pass said \"{first}\" and was not confident."
            )
        crops.append(b64)
        asked.append(det)
    if not crops:
        return notes

    try:
        call = await ask_vision(
            pipeline="food_identify_crop",
            system=IDENTIFY_CROP_SYSTEM,
            user_text=IDENTIFY_CROP_USER.format(
                count=len(crops), questions="\n".join(questions)
            ),
            images=crops,
            max_tokens=700,
        )
    except Exception as exc:  # noqa: BLE001
        # ask_vision already swallows upstream failures, so this is the second
        # net. It exists because everything below here is an OPINION on an
        # answer the scan already has, and an opinion must never be able to
        # cost the user the answer.
        log.warning("second_look_call_failed", error=str(exc)[:200])
        return notes
    await record_usage(call, user_id)
    if not call.ok or not isinstance(call.payload, dict):
        # The first pass stands. A failed second opinion is not a failed scan.
        return notes

    answers = call.payload.get("items")
    if not isinstance(answers, list):
        return notes
    for ans in answers:
        if not isinstance(ans, dict):
            continue
        i = _num(ans.get("index"), -1)
        i = int(i) if i is not None else -1
        if not (0 <= i < len(asked)):
            continue
        det = asked[i]
        name = _text(ans.get("name"))
        conf = _num(ans.get("confidence"), 0.0) or 0.0
        if not name or name.lower() in {"unsure", "unknown", "unclear", ""}:
            continue
        if conf < SECOND_LOOK_MIN_CONFIDENCE:
            continue
        was = _text(det.get("name")) or "food"
        was_group = food_group(det.get("food_group"))
        now_group = food_group(ans.get("food_group")) if ans.get("food_group") else was_group

        if _key(name) == _key(was) or dish_head(name) == dish_head(was):
            # The close-up agreed. That is worth more than it looks: it clears
            # the cross-group alternative that would otherwise have been shown
            # to the user as a doubt on a food we have now checked twice.
            det["confidence"] = max(_num(det.get("confidence"), 0.6) or 0.6,
                                    min(0.90, conf))
            det["alternatives"] = [
                a for a in (det.get("alternatives") or [])
                if isinstance(a, dict)
                and food_group(a.get("food_group")) == now_group
            ]
            continue

        # It disagreed. The close-up had several times the detail on this one
        # food, so it wins -- but the first answer stays on the list, and the
        # note says plainly what changed so a wrong swap is one tap to undo.
        det["alternatives"] = [{
            "name": was, "food_group": was_group,
            "confidence": round(_num(det.get("confidence"), 0.6) or 0.6, 3),
        }]
        det["name"] = name
        if ans.get("food_group"):
            det["food_group"] = ans.get("food_group")
        det["confidence"] = min(0.88, conf)
        # The old prior described the old food.
        det.pop("typical_serving_g", None)
        why = _text(ans.get("evidence"))
        if was_group != now_group:
            notes.append(
                f"{name}: first read as {was}, but a close-up of just this item "
                f"says {name} — {was_group} and {now_group} are not the same "
                f"kind of food and the calories are not close."
                + (f" ({why})" if why else "")
            )
        else:
            notes.append(
                f"{name}: first read as {was}; a close-up of just this item "
                f"says {name}." + (f" ({why})" if why else "")
            )
    return [n[:240] for n in notes]


# The ladder, best first. A meal reports the best rung ANY of its items
# reached: items can differ when one item's own geometry fails, and what we are
# describing is what the photograph supported, not the weakest item in it.
SCALE_LADDER = ("plate_reference", "reference_object", "multi_image",
                "depth_model", "vessel_reference", "pixel_area", "ai_prior")

# Rungs where something in the photograph really was measured.
#
# A whitelist, not a blacklist, and the difference is not pedantic: listing the
# BAD rungs means any method added later is silently treated as measured, and
# the one thing this field must never do is vouch for something it has not been
# told about. Adding a rung is a deliberate act.
#
# vessel_reference is the interesting exclusion. It looks like a measurement and
# is not: a takeout clamshell and a mixing bowl both answer to "bowl", so the
# scale is a guess about crockery. On the weighed bench those photos carried the
# same -26.8% bias as the ones with no reference at all, against -3.3% for the
# genuinely measured ones. That is the whole reason this field exists.
MEASURED_SCALES = frozenset({
    "plate_reference", "reference_object", "multi_image", "depth_model",
})


def scale_summary(items) -> tuple[str | None, bool]:
    """Which rung sized this meal, and whether that counts as measured.

    The second value decides whether the app tells the person their portion was
    estimated rather than measured, so it is deliberately strict: when in doubt
    it says "not measured", because a quiet 27% undercount is worse than an
    unnecessary prompt.
    """
    used = set()
    for it in items or []:
        m = getattr(it, "estimation_method", None)
        if isinstance(m, str) and m:
            used.add(m)
    source = next((m for m in SCALE_LADDER if m in used), None)
    if source is None and used:
        # A method nobody here has heard of. Report it, but do not vouch for it.
        source = sorted(used)[0]
    return source, source in MEASURED_SCALES


def _measured_plate_area_ratio(raw: bytes | None) -> float | None:
    """The share of the frame the plate actually covers, MEASURED.

    The model is asked this as `plate_area_ratio` and is bad at it. On photo 35
    it says 0.362 where the rim traces 0.5396 -- 1.49x too small, every run,
    while the Hough circle returned 0.5396 on all four scans in
    docs/evidence/. The consumer divides a disc area by this to recover
    mm-per-pixel, so a ratio 1.49x small makes every mm-per-pixel too large and
    every gram derived from it too heavy.

    AREA ONLY, AND THAT IS NOT A LIMITATION TO BE LIFTED LATER. A Hough circle
    has axis ratio 1.0 by construction, so it cannot report foreshortening and
    must never reach `plate_ellipse_wh` or anything reading the plate's SHAPE.
    That is also why `CIRCLE_PLATE_SOURCE` stays out of
    `food_seg.MEASURED_PLATE_SOURCES`: this function is a second, narrower door
    for the one quantity a circle can honestly supply.

    Never raises, and returns None rather than a guess. A measurement that
    fails leaves the model's own answer in place, which is exactly today's
    behaviour.
    """
    if not raw:
        return None
    try:
        import io

        import numpy as np
        from PIL import Image, ImageOps

        # The SAME decode as `_measured_areas`. A different resolution would
        # measure a different plate, and the ratio is scale-free only if the
        # frame it is a ratio OF is the same one.
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        img.thumbnail((1568, 1568), Image.LANCZOS)
        circle = food_seg._plate_by_hough(np.array(img))   # noqa: SLF001
        if circle is None or not circle.any():
            return None
        got = float(circle.mean())
        return got if 0.0 < got < 1.0 else None
    except Exception:                                      # noqa: BLE001
        return None


def _measured_areas(
    detections: list[dict], raw: bytes | None, plate_bbox: dict | None
) -> tuple[list[float | None], list[float | None], str]:
    """Each item's footprint measured from the pixels, and how it was measured.

    NO LONGER SHADOW ONLY -- but only for one of the two sources.

    A colour-grown footprint is fenced by the plate box, so its absolute value
    inherits the box's error while the share survives: measured, shrinking the
    plate box 10% moved footprints 3-14% and shares 1-6%. It stays out of the
    grams, exactly as it has been, and is reported so the bench keeps scoring it.

    A point-prompted segmenter's mask never sees the plate box. That one is an
    area, and it goes into the weight path -- which is the whole reason the
    segmenter was built. `food_seg.area_is_absolute` is where that line is drawn.

    Never raises. A measurement that fails is not a scan that fails.
    """
    if not raw or not detections:
        return [None] * len(detections), [None] * len(detections), "none"
    try:
        import io

        import numpy as np
        from PIL import Image, ImageOps

        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        # The same longest-edge the detector saw. Measuring a different
        # resolution than the boxes were drawn on would put every seed in the
        # wrong place.
        img.thumbnail((1568, 1568), Image.LANCZOS)
        rgb = np.array(img)
        return food_seg.measure_items_with_topology(
            rgb, [d.get("bbox") for d in detections], plate_bbox
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("measured_area_failed", error=str(exc)[:200])
        return [None] * len(detections), [None] * len(detections), "none"


# The depth model, if one is configured. NullDepth unless DEPTH_PROVIDER is set
# in the environment, so the app behaves exactly as it does today until a
# provider is configured -- a missing model costs a measurement, never a scan.
DEPTH_PROVIDER = depth_hosted.from_settings()

# What kind of depth the geometry in depth_map is written for.
#
# Every commercially-licensed monocular model returns RELATIVE INVERSE depth,
# and depth_map resolves its unknown gain from the plate's foreshortening. A
# METRIC map -- a phone's LiDAR, an ARKit frame -- is in millimetres already and
# needs different, simpler arithmetic that is not built yet. Declared and
# checked rather than assumed, so that path cannot be wired up quietly wrong.
DEPTH_UNITS_SUPPORTED = "relative_inverse"


def _measured_heights(detections: list[dict], raw: bytes | None,
                      plate_bbox: dict | None,
                      plate_diameter_mm: float | None) -> list[float | None]:
    """Each item's mean height in mm, measured from a depth map, or None.

    Requires all three: a depth model, a plate to fit a plane to, and that
    plate's real diameter. Missing any one of them is not a failure -- it is the
    ordinary case today, and every item falls back to its prior.

    Never raises, for the same reason the footprint measurement does not: a scan
    must not fail because the app was trying to measure something extra.
    """
    empty = [None] * len(detections)
    if not (raw and detections and plate_bbox and plate_diameter_mm):
        return empty
    if not DEPTH_PROVIDER.available():
        return empty
    units = getattr(DEPTH_PROVIDER, "units", DEPTH_UNITS_SUPPORTED)
    if units != DEPTH_UNITS_SUPPORTED:
        log.warning("depth_units_unsupported", provider=DEPTH_PROVIDER.name, units=units)
        return empty
    try:
        import io

        import numpy as np
        from PIL import Image, ImageOps

        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        img.thumbnail((1568, 1568), Image.LANCZOS)
        rgb = np.array(img)

        depth = DEPTH_PROVIDER.depth(rgb)
        if depth is None:
            return empty

        plate, plate_source = food_seg.plate_surface(rgb, plate_bbox)
        if plate is None or not plate.any():
            return empty

        # The scale rests on the plate's FORESHORTENING, so the plate's shape
        # has to have been measured, not drawn from the model's bounding box.
        # A box ellipse's axis ratio is the box's -- on the model's 0.05 grid --
        # and for most photographs that is the aspect ratio of the IMAGE. A
        # survey over sixteen bench photographs reported eight of them at
        # 41.4 degrees to one decimal place before this check existed, because
        # arccos(3/4) is 41.4 and the photographs are 3:4.
        if not food_seg.plate_is_measured(plate_source):
            log.info("depth_plate_not_measured", source=plate_source)
            return empty

        # Which way is up? Some models return depth, some inverse depth. Wrong
        # way round, every mound becomes a dent and the portion still looks
        # plausible -- which is what makes it worth one cheap check.
        if not depth_map.orientation_is_sane(depth, plate):
            log.warning("depth_orientation_rejected", provider=DEPTH_PROVIDER.name)
            return empty

        # Item regions come from the same colour mask the split uses. Its job
        # here is only to say WHICH pixels belong to this food -- separation,
        # which it is adequate at -- while the depth map supplies the height and
        # the calibrated plate supplies the scale.
        masks = food_seg.item_masks(rgb, [d.get("bbox") for d in detections], plate_bbox)
        return depth_map.measure_heights(depth, plate, masks, plate_diameter_mm)
    except Exception as exc:  # noqa: BLE001
        log.warning("measured_height_failed", error=str(exc)[:200])
        return empty


async def build_items(
    detections: list[dict],
    hint: GeometryHint,
    raw: bytes | None = None,
    plate_bbox: dict | None = None,
    user_id: str | None = None,
) -> tuple[list[DetectedItem], list[str]]:
    notes: list[str] = []
    # What this person has already told us their food is. Read once per scan,
    # never per item, and empty is the normal answer for a new user.
    learned_names = food_identity.aliases_for(user_id) if user_id else []
    # payload["items"] is whatever the model returned. A list of strings, or a
    # list with a null in it, used to AttributeError its way to a 500 on a
    # photo that had otherwise been read correctly.
    detections = [d for d in detections if isinstance(d, dict)]

    # What the food is sitting on, consulted BEFORE the nutrition lookup so a
    # settled tie is looked up as the food it settled on. Tie-breaking only:
    # see _surface_tiebreak for the limits, which exist because contextual
    # reasoning has already deleted a weighed portion from this pipeline once.
    for det in detections:
        swapped = _surface_tiebreak(det, hint.vessel)
        if swapped:
            notes.append(swapped[:220])

    # What this person has already told us these foods are.
    #
    # HERE, beside the tie-break and for the same reason: a renamed food has to
    # be renamed BEFORE `resolve_many`, or the nutrition comes back for the name
    # the camera guessed and the rename is only a relabel.
    #
    # That is precisely how the first version of this shipped, one turn old --
    # applied down in the item loop at line ~1016, after the lookup at 909 and
    # after the grams were already estimated. It changed what the user saw and
    # nothing else: same calories, same density, same weight, from "creamy
    # mushroom sauce". The feature existed to fix exactly that and did not.
    #
    # Last, so the user's own answer beats the surface tie-break above.
    for det in detections:
        renamed = food_identity.apply_to(det, learned_names)
        if renamed:
            notes.append(f"{det.get('name')}: {renamed}"[:240])

    names = [_lookup_name(d) for d in detections]
    facts = await resolver.resolve_many(names)

    # THE PLATE'S SHARE OF THE FRAME, MEASURED RATHER THAN ASKED FOR.
    #
    # Ranked work #1. Everything below that divides by this was dividing by a
    # number the model guesses on a 0.05 grid and gets wrong by 1.49x on the
    # one photograph where both are known. The rim is free to find.
    #
    # Replaced HERE rather than where the hint is built, because the hint is
    # constructed in `_run_scan` before any pixel has been looked at and is
    # passed in already finished. The alternative was to thread a measurement
    # back into its constructor, which means measuring the plate before
    # detection has said whether there IS one.
    #
    # In a thread: this decodes a full-size JPEG and runs OpenCV, and called
    # bare from an async function it stalls every other request on the process.
    # The same reason the footprint and the depth map below are wrapped.
    measured_plate = await asyncio.to_thread(_measured_plate_area_ratio, raw)
    if measured_plate:
        log.info("plate_area_ratio_measured",
                 model=(round(float(hint.plate_ellipse_area_ratio), 4)
                        if hint.plate_ellipse_area_ratio else None),
                 measured=round(measured_plate, 4),
                 ratio=(round(measured_plate / float(hint.plate_ellipse_area_ratio), 3)
                        if hint.plate_ellipse_area_ratio else None))
        # `replace` and not mutation: GeometryHint is slots=True and frozen in
        # spirit -- it is read in a dozen places below and a hint that changed
        # under half of them would be worse than either value used throughout.
        hint = replace(hint, plate_ellipse_area_ratio=measured_plate)

    # How much of the plate ALL of this food covers, measured the same way the
    # estimator measures each item -- after the bounding-box rail, because the
    # raw reported areas are exactly the numbers that rail exists to reject.
    plate_ratio = hint.plate_ellipse_area_ratio or 0.0
    plate_food_coverage = None
    if plate_ratio > 0:
        total_area = sum(
            railed_area(_num(d.get("area_ratio"), 0.0) or 0.0, d.get("bbox"))
            for d in detections if isinstance(d, dict)
        )
        plate_food_coverage = min(1.0, total_area / max(plate_ratio, 1e-4)) or None

    # Shadow measurement. Computed once for the whole photo, reported per item,
    # and deliberately not used for a single gram -- see DetectedItem.
    # Heights corrections have earned, fetched ONCE for the whole photo. Per
    # item would be one query per food on the plate, for a table that changes
    # a few times a day.
    learned = portion_learning.learned_heights()

    # OFF THE EVENT LOOP. This decodes a full-size JPEG, runs OpenCV, and makes
    # a blocking HTTP call to the segmenter with a sleep-poll loop that can run
    # to the full timeout. Called bare from an async function it stalls every
    # other request in the process for the whole segmentation -- a food scan
    # holding up someone else's login. The height measurement four lines down
    # was already wrapped for exactly this reason; the footprint was not.
    # `pieces` is each footprint's topology -- the largest connected piece as a
    # share of the whole. Read off the same masks, so it costs nothing extra,
    # and it is what tells a single layer of slices from a pile.
    measured, pieces, measured_source = await asyncio.to_thread(
        _measured_areas, detections, raw, plate_bbox)
    # Whether those footprints are AREAS or only shares of the meal. A colour
    # mask is fenced by the plate box, so its absolute value carries the box's
    # error; a point-prompted segmenter's mask never sees the box. Only the
    # second reaches the grams -- everything else here is unchanged, and on a
    # deployment with no segmenter configured this is False and the whole
    # measured path is exactly the shadow measurement it has been.
    area_is_absolute = food_seg.area_is_absolute(measured_source)
    # Heights measured from a depth map, when one is configured. NullDepth by
    # default, so this is a list of Nones and every item keeps its prior --
    # exactly today's behaviour.
    #
    # In a thread, because the provider is an HTTP round trip of a second or
    # two. Called inline it would block the event loop for the whole of it --
    # every other request on the process waits on one photograph's depth map.
    heights = await asyncio.to_thread(
        _measured_heights,
        detections, raw, plate_bbox,
        hint.plate_diameter_mm or (
            math.sqrt(hint.reference_area_mm2 * 4.0 / math.pi)
            if hint.reference_area_mm2 else None
        ),
    )
    # The denominator of the share. All the food the pixels found on this plate,
    # so each item's number is its portion OF THE MEAL rather than an absolute
    # area the mask is not good enough to give.
    #
    # ALL of it, or none of it. A share whose denominator is missing an item is
    # not a share, it is a bigger number wearing a percent sign -- and it lands
    # on the remaining foods, which are precisely the ones that looked fine.
    # Measured on photo 06: the mask placed a seed for the rice and failed on
    # the drumstick and the beans, so the rice was published as 100% of a plate
    # it was about a third of, and the bench scored that as a 158% error when
    # the real fault was two missing seeds.
    #
    # So a plate with any unmeasured item publishes no split at all. "Not
    # measured" is the honest answer and the caller already handles it.
    measured_total = (sum(measured) if measured and all(v for v in measured)
                      else None)

    items: list[DetectedItem] = []
    for det, name, measured_area, piece_share, measured_height in zip(
            detections, names, measured, pieces, heights):
        fact = facts.get(name) or {}
        # ONLY a real measured/provider/user density is passed as explicit --
        # see _explicit_density's own comment for why an ai_estimate row's
        # density must not reach here. Logged before the call, not after:
        # density_for (portion.py) is untouched by this fix -- its ranking
        # order and internals are out of scope -- but its own documented
        # rank-1 entry condition (`explicit and 0.05 < explicit < 3.0`) is
        # fully decided by what reaches it, which is exactly what this logs.
        explicit_density = _explicit_density(fact)
        raw_density = _num(fact.get("density_g_ml"))
        log.info(
            "density_provenance",
            food=name,
            explicit_reached=explicit_density is not None,
            density_source=(
                "ai_estimate_blocked" if raw_density is not None and fact.get("source") == "ai_estimate"
                else "provider_or_user" if raw_density is not None
                else "none"
            ),
            precedence_rank=(
                1 if explicit_density is not None and 0.05 < explicit_density < 3.0
                else "table"
            ),
        )
        est = estimate_grams(
            name=name,
            area_ratio=_num(det.get("area_ratio"), 0.0) or 0.0,
            hint=hint,
            # Asked relative to the plate rather than the frame. Optional: an
            # older model response, or food on paper with no vessel to measure
            # against, simply omits it and area_ratio is used as before.
            plate_coverage=_num(det.get("plate_coverage")) or None,
            shape_hint=det.get("shape"),
            # How tall the food stands, as a share of the vessel's width, from
            # an angled photo. None from a top-down shot, where it cannot be
            # seen and the prior is the honest answer.
            height_ratio=_num(det.get("height_ratio")) or None,
            bbox=det.get("bbox"),
            visible_fraction=_visible_fraction(det),
            plate_food_coverage=plate_food_coverage,
            # ONLY a real measured density is passed as explicit. Passing the
            # group density here instead meant it always won, and the specific
            # per-food table was never reached -- refried beans kept the
            # density of whole beans in broth. The group is now passed as the
            # group, so it acts as the fallback it was meant to be.
            density=explicit_density or None,
            food_group=det.get("food_group"),
            ai_prior_grams=_num(det.get("typical_serving_g")) or None,
            detection_confidence=max(0.0, min(1.0, _num(det.get("confidence"), 0.6) or 0.6)),
            learned_heights=learned,
            measured_height_mm=measured_height,
            # The footprint, when it was actually measured rather than grown
            # from a colour rule. See area_is_absolute above for the one reason
            # this is gated instead of always passed.
            measured_area_ratio=(measured_area if area_is_absolute else None),
            # Only alongside a footprint that reached the weight path. On its
            # own the topology says nothing about scale.
            largest_piece_share=(piece_share if area_is_absolute else None),
        )
        # THE HEIGHT BRANCH, EMITTED RATHER THAN RECONSTRUCTED.
        #
        # `portion.py` picks between CONNECTED_PILE_HEIGHT_MM (21.0) and
        # SEPARATE_PIECES_HEIGHT_MM (9.2) on whether this share reaches
        # ONE_PIECE_SHARE. That is a 2.28x step in the height that multiplies
        # straight into grams, and until now NOTHING recorded which side of it
        # a scan landed on -- not the bench, not the API log, not the response.
        #
        # So the standing account of photo 35 -- union area moved 1.29x while
        # grams moved 2.5x, and (1.29 x 2.28)^0.9 = 2.63x lands on the observed
        # value where area alone gives 1.26x -- rests on a branch nobody has
        # ever seen taken. It was once reported not to flip; that report came
        # from a replay over the wrong mask set and was withdrawn with it.
        #
        # One line per item per scan makes three runs of one photograph answer
        # it directly, with no replay and no reconstruction in between.
        # `piece_share` is logged raw as well as thresholded, because a value
        # sitting at 0.79 vs 0.81 is a different finding from one at 0.30.
        # `applied` IS THE WHOLE POINT, AND THE FIRST VERSION OF THIS LINE
        # LACKED IT AND MISLED ON THE FIRST PHOTOGRAPH IT SAW.
        #
        # `estimate_grams` reaches the topology branch only when the footprint
        # SURVIVED -- `elif measured_used and largest_piece_share is not None`
        # -- and a footprint far smaller than its own box is refused as a
        # fragment before that. On photo 35 the burger's mask was refused at
        # 6.7x under its box, so its share of 0.52 decided nothing; reported
        # beside the fries' 0.85, which DID decide a height, the two looked
        # like one finding and were not.
        #
        # `measured_area_used` is None exactly when the footprint was refused,
        # so the estimate itself says which happened rather than this line
        # guessing from inputs it cannot see the fate of.
        applied = est.measured_area_used is not None and piece_share is not None
        log.info("portion_height_branch", item=len(items), food=name,
                 piece_share=(None if piece_share is None
                              else round(float(piece_share), 4)),
                 applied=bool(applied),
                 one_mass=(bool(float(piece_share) >= food_seg.ONE_PIECE_SHARE)
                           if applied else None),
                 height_mm=((CONNECTED_PILE_HEIGHT_MM
                             if float(piece_share) >= food_seg.ONE_PIECE_SHARE
                             else SEPARATE_PIECES_HEIGHT_MM)
                            if applied else None),
                 area_absolute=bool(area_is_absolute),
                 measured_area=(None if measured_area is None
                                else round(float(measured_area), 5)),
                 measured_used=est.measured_area_used,
                 grams=round(float(est.grams), 1))
        notes.extend(est.notes)

        # The model's own observation about this item -- 'dressing visible',
        # 'partially hidden behind bread'. Often the one line that explains an
        # odd number.
        det_note = str(det.get("notes") or "").strip()
        if det_note:
            notes.append(f"{name}: {det_note}"[:200])

        # Say so when the FOOD is in doubt, not just the portion. A wrong
        # portion is a number to correct; a wrong food is a wrong meal.
        doubt = _identification_doubt(det, food_group(det.get("food_group")))
        if doubt:
            notes.append(f"{name}: {doubt}"[:220])

        # Did it recognise the dish, or only describe what it could see?
        #
        # Unknown is the default rather than "named", so a model that omits the
        # field, or an older prompt, does not silently claim recognition it
        # never reported. That direction of failure asks a question too many;
        # the other direction logs the wrong food forever.
        ident = _identification_state(det)
        if ident != "named":
            notes.append(
                f"{name}: this was described, not recognised — the app could "
                f"not put a dish name to it. Telling it what this is fixes the "
                f"calories as well as the label, and it will remember."
                if ident == "described" else
                f"{name}: not confidently identified. Worth telling the app "
                f"what this is — the name sets the calories, not just the label."
            )

        macros = resolver.macros_for(fact, est.grams) if fact else Macros()
        if not fact:
            # A lookup that failed produces an item with real grams and zero
            # macros, which reads on screen exactly like a food that has no
            # calories. resolve_many drops names whose lookup raised, so this
            # is reachable on any transient database error -- and the meal
            # total is then silently short by that food.
            notes.append(
                f"{name}: could not look up nutrition for this, so it is "
                f"logged with its weight but no calories. Worth adding by hand."
            )

        # Energy density is bounded by physics, so this has to come after the
        # macros exist -- it is a check ON them. A contradiction means the food
        # was matched wrongly or the portion is wrong, and either way the user
        # should see it rather than be handed a confident number.
        # Oil is the thing a camera cannot see, and it is what the whole market
        # gets wrong. Checked before the energy test because a missing oil is a
        # missing INGREDIENT, not an implausible density.
        fat_issue = _fat_preparation_lost(det, fact)
        if fat_issue:
            notes.append(f"{name}: {fat_issue}"[:240])

        energy_issue = implausible_energy(
            getattr(macros, "kcal", 0.0) or 0.0, est.grams, det.get("food_group")
        )
        if energy_issue:
            notes.append(f"{name}: {energy_issue}"[:200])
        items.append(
            DetectedItem(
                name=(fact.get("display_name") or name)[:120],
                # A list here raises ValidationError -> 500 on a good photo.
                # Same defence as normalize_bbox, for the same reason.
                cuisine=_text(det.get("cuisine")) or _text(fact.get("cuisine")),
                food_group=food_group(det.get("food_group")),
                identification=ident,
                grams=est.grams,
                grams_low=est.grams_low,
                grams_high=est.grams_high,
                estimation_method=est.method,
                pixel_area_ratio=est.pixel_area_ratio,
                reported_area_ratio=est.reported_area_ratio,
                plate_coverage_used=est.plate_coverage_used,
                box_area_ratio=est.box_area_ratio,
                depth_factor=est.depth_factor,
                confidence=est.confidence,
                # Normalised, not raw: DetectedItem declares bbox as a dict
                # and a list from the model raises ValidationError -> 500.
                bbox=normalize_bbox(det.get("bbox")),
                macros=macros,
                food_fact_id=fact.get("id"),
                measured_area_ratio=(
                    round(measured_area, 5) if measured_area else None
                ),
                measured_area_source=(
                    measured_source if measured_area else None
                ),
                measured_area_used=est.measured_area_used,
                measured_share=(
                    round(measured_area / measured_total, 4)
                    if (measured_area and measured_total) else None
                ),
            )
        )
    # Dedupe repeated notes while keeping order.
    seen: set[str] = set()
    unique_notes = [n for n in notes if not (n in seen or seen.add(n))]
    return items, unique_notes[:6]


# ---------------------------------------------------------------------------
# Stage 4: reasoning
# ---------------------------------------------------------------------------
async def refine(
    items: list[DetectedItem], profile: dict, day_totals: Macros, user_id: str, scene: str
) -> tuple[list[DetectedItem], dict]:
    if not items:
        return items, {"overall_confidence": 0.0, "needs_review": True, "corrections": []}

    call = await ask_reasoning(
        pipeline="food_reasoning",
        system=FOOD_REASONING_SYSTEM,
        user_text=(
            f"Scene: {scene}\n"
            f"User: goal={profile.get('goal')}, diet={profile.get('diet_mode')}, "
            f"weight={profile.get('weight_kg')} kg\n"
            f"Already eaten today: {day_totals.kcal:.0f} kcal\n"
            f"Detections (index, name, grams, method, confidence, kcal):\n"
            + "\n".join(
                f"{i}. {it.name} | {it.grams} g | {it.estimation_method} | "
                f"conf {it.confidence} | {it.macros.kcal:.0f} kcal"
                for i, it in enumerate(items)
            )
            + "\n\nReturn the corrected meal."
        ),
        max_tokens=2000,
    )
    await record_usage(call, user_id)

    if not call.ok or not isinstance(call.payload, dict):
        avg = sum(i.confidence for i in items) / len(items)
        return items, {
            "overall_confidence": round(avg * 0.9, 3),
            "needs_review": avg < 0.6,
            "corrections": [],
            "title": items[0].name.title() if items else "Meal",
        }

    p = call.payload
    corrections_from_clamp: list[str] = []
    forced_keeps: list[str] = []
    merged_into: dict[int, float] = {}
    _merge_target: dict[int, int] = {}   # source index -> the index it merged into

    # Specs are matched to detections BY INDEX, and every detection is kept
    # whether or not a spec came back for it.
    #
    # This used to walk the model's list positionally and stop at its end,
    # which lost food two ways. A response with fewer items than were detected
    # -- routine, because merging shortens it -- dropped every detection past
    # the end of that list, silently, along with its calories. And a response
    # that reordered its items applied one food's correction to another: on a
    # weighed four-item plate a drumstick and a spaghetti casserole came back
    # with identical grams to the tenth of a gram, which is what sent us
    # looking.
    #
    # The prompt hands the model an index per detection and now asks for it
    # back. Where it is missing we fall back to position, because that is what
    # the model was doing implicitly anyway -- but a spec that names an index
    # is believed over where it happens to sit.
    raw_specs = [s for s in (p.get("items") or []) if isinstance(s, dict)]
    by_item: dict[int, dict] = {}
    for position, spec in enumerate(raw_specs):
        idx = _num(spec.get("index"))
        target = int(idx) if idx is not None and 0 <= idx < len(items) else position
        if 0 <= target < len(items) and target not in by_item:
            by_item[target] = spec

    refined: list[DetectedItem] = []
    refined_at: dict[int, int] = {}     # detection index -> position in `refined`
    for i in range(len(items)):
        spec = by_item.get(i, {})
        item = items[i].model_copy()
        if spec.get("keep") is False:
            # The reasoning model may not delete food.
            #
            # Measured: on a weighed plate it removed a 133 g portion of fideo
            # -- the largest item there -- reasoning that pasta "does not fit
            # cuisine coherence" with the Mexican dishes beside it. The pasta
            # was in the photograph. That single deletion took the meal from
            # 426 g to 232 g, and the calorie and carb figures with it.
            #
            # Dropping an item is a claim that the vision model hallucinated
            # food that is not there. That is a far stronger claim than "this
            # portion looks wrong", and it is not one to act on silently in an
            # app whose purpose is noticing when someone has eaten more than
            # they meant to. Under-counting is the harmful direction.
            #
            # Merging into another item is legitimate -- the same food detected
            # twice, or a component better counted as part of a dish. That is
            # honoured. A bare removal is not: the item stays, carries the
            # disagreement in its notes, and the meal is flagged for review.
            target = spec.get("merge_into")
            # Two detections describe the same food only if they are the same
            # KIND of food. Without this a merge can quietly move grams between
            # unrelated items -- rice folded into pasta, a protein into a grain --
            # and the meal's macros go with them. Refusing the merge keeps both
            # items and their calories; the worst case is a duplicate the user
            # can see and delete, which beats food silently changing category.
            same_group = (
                isinstance(target, int) and 0 <= target < len(items)
                and items[target].food_group == item.food_group
            )
            if not same_group and isinstance(target, int):
                forced_keeps.append(
                    f"{item.name}: the model wanted to merge this into "
                    f"{items[target].name if 0 <= target < len(items) else 'another item'}, "
                    f"but they are different kinds of food "
                    f"({item.food_group} vs "
                    f"{items[target].food_group if 0 <= target < len(items) else '?'}). "
                    f"Kept separate."
                )
            if same_group and target != i:
                _merge_target[i] = target
                merged_into[target] = merged_into.get(target, 0.0) + item.grams
                corrections_from_clamp.append(
                    f"{item.name}: merged into {items[target].name}."
                )
                continue
            forced_keeps.append(
                f"{item.name} ({item.grams:.0f} g): the model wanted to drop this "
                f"item, but it was detected in the photo and removing it would "
                f"under-count the meal. Kept — check it if it is not yours."
            )
            item.confidence = round(item.confidence * 0.75, 3)
        new_grams = spec.get("grams")
        if isinstance(new_grams, (int, float)) and 1 <= float(new_grams) <= 3000:
            # The reasoning model may refine the measurement. It may not replace
            # it with a serving convention.
            #
            # Measured: letting it write any number it liked cost 8.4% -> 24.4%
            # mean error on single-item photos, and two shots of one bowl of
            # soup went from 4.7% apart to 27.6% apart -- because "normalise to
            # a typical serving" is a judgement, and judgements vary run to run
            # while geometry does not. Every meal also swung low, toward
            # standard servings and away from what was on the scale.
            #
            # So the estimator's band is the envelope. Inside it, the model
            # knows things geometry cannot -- that a drumstick includes bone,
            # that a sauce is heavier than it looks. Outside it, the model is
            # asserting the measurement is wrong, which is a claim about the
            # photo, not about serving sizes. That is allowed, but it costs
            # confidence and widens the band rather than narrowing it.
            # Rejecting the proposal means keeping the MEASUREMENT, not the
            # edge of the band.
            #
            # This used to snap to the nearest band edge, which quietly made
            # the edge the answer. Measured on one 335 g skewer photographed
            # three ways: the reasoning model proposed 150 g every time -- the
            # standard serving for "grilled meat skewer" -- and every time the
            # answer became grams_low. 391 g became 289 g, 349 g became 234 g,
            # 284 g became 230 g. Three photos of one piece of meat, and the
            # final number was whichever floor the band happened to have.
            #
            # That inverts the band's meaning. A wider band means the estimator
            # was LESS sure, and snapping to its edge moved the answer FURTHER
            # from the measurement the less sure it was. The note printed
            # alongside already claimed the measurement had been kept; it had
            # not been.
            #
            # An out-of-band proposal is the model asserting the photo was
            # mismeasured, with no evidence beyond a serving convention. So the
            # measurement stands, it costs confidence, and the band widens to
            # cover the disagreement.
            proposed = float(new_grams)
            lo = float(item.grams_low or item.grams * 0.7)
            hi = float(item.grams_high or item.grams * 1.3)
            # Preserve how uncertain the estimator actually was.
            rel_band = (hi - lo) / (2.0 * max(item.grams, 0.01))
            measured = float(item.grams)

            if lo <= proposed <= hi:
                accepted = proposed
            else:
                accepted = measured
                corrections_from_clamp.append(
                    f"{item.name}: kept the measured {measured:.0f} g rather than "
                    f"the suggested {proposed:.0f} g, which fell outside what the "
                    f"photo supports ({lo:.0f}-{hi:.0f} g)."
                )
                item.confidence = round(item.confidence * 0.9, 3)
                rel_band = min(0.45, rel_band * 1.25)
                # The band has to reach the rejected figure, or the next stage
                # sees a range that pretends the disagreement never happened.
                reach = abs(proposed - measured) / max(measured, 0.01)
                rel_band = min(0.45, max(rel_band, reach))

            factor = accepted / max(item.grams, 0.01)
            if abs(factor - 1.0) > 0.02:
                item.grams = round(accepted, 1)
                item.macros = item.macros.scaled(factor)
            item.grams_low = round(max(1.0, item.grams * (1 - rel_band)), 1)
            item.grams_high = round(item.grams * (1 + rel_band), 1)
        if spec.get("name"):
            # A rename may add detail. It may not change the food.
            #
            # The macros and the food_fact_id on this item were resolved for
            # the name the vision model gave. Renaming "chicken breast" to
            # "fried chicken thigh" leaves breast calories under a thigh label
            # -- and the name is what the user checks while the macros are what
            # count toward their day. So a rename is accepted when it describes
            # the same dish and refused when it does not.
            proposed_name = str(spec["name"])[:120]
            # Same dish, said shorter or longer, is still the same dish.
            # "boiled bean soup" -> "bean soup" is a shortening; requiring the
            # heads to match exactly refused it and told the user their soup
            # had been renamed to a different food. One containing the other is
            # the test that separates a rewording from a reclassification:
            # "chicken breast" and "fried chicken thigh" share neither
            # direction, and that is the case worth refusing.
            was, now = dish_head(item.name), dish_head(proposed_name)
            if was and now and (was in now or now in was):
                item.name = proposed_name
            else:
                forced_keeps.append(
                    f"{item.name}: the model wanted to call this "
                    f"\"{proposed_name}\", which is a different food from the "
                    f"one its nutrition was looked up for. Kept the original "
                    f"name — check it if it is wrong."
                )
                item.confidence = round(item.confidence * 0.85, 3)
        # Confidence is a 0-1 number written into numeric(4,3). A model that
        # answers 85 instead of 0.85 -- a well-known drift -- used to overflow
        # the column AFTER the meal row was committed, leaving an item-less
        # meal with full calorie totals already rolled into the day's summary.
        conf = _num(spec.get("confidence"))
        if conf is not None:
            item.confidence = round(max(0.0, min(1.0, conf)), 3)
        refined_at[i] = len(refined)
        refined.append(item)

    # Grams from anything merged away are added to their target, so a merge
    # moves food between items and never removes it from the meal.
    #
    # This used to look the target up by `id(items[i])` against a list holding
    # model_copy() objects -- different objects, different ids, so the lookup
    # missed every time and merged grams were simply discarded. A merge
    # therefore deleted food, which is precisely what the long comment above
    # says merging must never do.
    #
    # Chains are resolved too: if the model merges 0 into 1 and 1 into 2, the
    # old code credited a recipient that had itself been merged away, and item
    # 0's grams vanished. Following the chain to a survivor keeps the mass.
    for source, extra in merged_into.items():
        target = source
        seen = {source}
        while target not in refined_at:
            nxt = _merge_target.get(target)
            if nxt is None or nxt in seen:
                break
            seen.add(nxt)
            target = nxt
        position = refined_at.get(target)
        if position is None:
            # Every recipient in the chain was merged away. Keeping the grams
            # on any surviving item beats dropping them, so give them to the
            # largest one; under-counting is the harmful direction.
            if not refined:
                continue
            position = max(range(len(refined)), key=lambda k: refined[k].grams)
        item = refined[position]
        factor = (item.grams + extra) / max(item.grams, 0.01)
        item.grams = round(item.grams + extra, 1)
        item.macros = item.macros.scaled(factor)
        # The band described the pre-merge portion. Carry it with the grams,
        # or the stored range no longer contains the number it describes.
        if item.grams_low:
            item.grams_low = round(item.grams_low * factor, 1)
        if item.grams_high:
            item.grams_high = round(item.grams_high * factor, 1)

    if not refined:
        refined = items

    return refined, {
        "overall_confidence": round(float(p.get("overall_confidence") or 0.6), 3),
        # A kept-against-the-model's-wishes item means the meal total is
        # disputed, which the user should see rather than have decided for them.
        "needs_review": bool(p.get("needs_review")) or bool(forced_keeps),
        # Ordering is deliberate. A forced keep changes the meal total, a clamp
        # changes one number, and the model's own remarks are commentary.
        "corrections": (
            forced_keeps
            + corrections_from_clamp
            + [str(c)[:200] for c in (p.get("corrections") or [])]
        )[:6],
        "warnings": [str(c)[:200] for c in (p.get("warnings") or [])][:5],
        "title": str(p.get("title") or "Meal")[:120],
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
async def run_scan(*args, **kwargs) -> ScanResult:
    """Analyse a photo, and never leave a scan stranded.

    Every stage of this pipeline reads fields written by a model, and although
    each one is guarded now, the guarantee that matters is the one that holds
    when a guard is missed: `food_scans.status` must reach a terminal value.
    Without this the row sat at 'processing' forever, the user's AI-scan quota
    was spent, and the client polled a scan that would never finish.
    """
    scan_id = kwargs.get("scan_id")
    try:
        return await _run_scan(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.exception("scan_failed", scan_id=scan_id, error=str(exc)[:300])
        if scan_id:
            return await _fail(
                scan_id,
                "Something went wrong analysing this photo. Nothing was logged.",
                time.perf_counter(),
            )
        raise


async def _run_scan(
    *,
    user_id: str,
    scan_id: str,
    image_paths: list[str],
    meal_slot: str | None,
    calibration_id: str | None,
    plate_diameter_mm: float | None,
    camera_distance_mm: float | None = None,
    camera_fov_deg: float | None = None,
    camera_aspect_ratio: float | None = None,
    measure_footprints: bool = False,
    note: str | None = None,
) -> ScanResult:
    t0 = time.perf_counter()
    sb = service()
    sb.table("food_scans").update({"status": "processing"}).eq("id", scan_id).execute()

    profile = maybe_one(
        sb.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    ) or {}
    targets = maybe_one(
        sb.table("nutrition_targets").select("*").eq("user_id", user_id)
        .order("effective_from", desc=True).limit(1).execute()
    ) or {}
    today = date.today().isoformat()
    summary = maybe_one(
        sb.table("daily_summaries").select("*").eq("user_id", user_id).eq("day", today)
        .limit(1).execute()
    ) or {}
    day_totals = Macros(
        kcal=float(summary.get("kcal_in") or 0),
        protein_g=float(summary.get("protein_g") or 0),
        carbs_g=float(summary.get("carbs_g") or 0),
        fat_g=float(summary.get("fat_g") or 0),
        fiber_g=float(summary.get("fiber_g") or 0),
        sugar_g=float(summary.get("sugar_g") or 0),
    )

    calibration = None
    if calibration_id:
        calibration = maybe_one(
            sb.table("scan_calibrations").select("*").eq("id", calibration_id)
            .eq("user_id", user_id).limit(1).execute()
        )
    elif not plate_diameter_mm:
        calibration = maybe_one(
            sb.table("scan_calibrations").select("*").eq("user_id", user_id)
            .eq("is_default", True).limit(1).execute()
        )

    fetched = await fetch_images("meal-photos", image_paths)
    images = [p.b64 for p in fetched]
    # The shape of the frame the model was actually shown. Measured beats
    # reported: the client's number describes its camera, this one describes
    # this photograph, and the model's fractions are fractions of THIS.
    measured_aspect = next((p.aspect for p in fetched if p.aspect), None)
    # An object of known real size, located in the pixels. The model is never
    # asked about this and cannot override it.
    reference = next((p.reference for p in fetched if p.reference), None)
    if not images:
        return await _fail(scan_id, "Could not read the uploaded photo.", t0)

    detection = await detect_foods(images, user_id, note=note)
    raw_items = detection.get("items") or []
    second_look_notes: list[str] = []
    if raw_items and len(fetched) == 1:
        # One photo only. With several angles a bounding box does not say which
        # frame it was drawn on, and cropping the wrong one would cut out the
        # wrong food -- worse than not looking twice at all.
        try:
            second_look_notes = await second_look(
                [d for d in raw_items if isinstance(d, dict)],
                fetched[0].raw, user_id,
            )
        except Exception as exc:  # noqa: BLE001
            # Never let the extra opinion cost the scan the answer it has.
            log.warning("second_look_failed", error=str(exc)[:200])
    if not raw_items:
        return await _fail(
            scan_id,
            detection.get("scene_notes") or detection.get("_error")
            or "No food was recognised in this photo.",
            t0,
            status="needs_review",
        )

    # A calibration that describes the vessel actually in this photo beats the
    # user's general default. Someone who measured both a dinner plate and a
    # soup bowl should get the bowl's size when they photograph soup -- before
    # this, they got the plate's, which is worse than the generic prior.
    #
    # This runs after detection because the vessel is only known once the photo
    # has been looked at.
    detected_vessel = str(detection.get("container") or "").strip().lower() or None
    if detected_vessel and not plate_diameter_mm and not calibration_id:
        vessel_cal = maybe_one(
            sb.table("scan_calibrations").select("*")
            # Both sides are normalised the same way. The stored side is whatever
        # the user typed -- "Bowl", "soup bowl", "bowl " -- and the lookup side
        # is the model's enum value, so raw equality quietly failed and the
        # bowl's photo was sized against the default calibration, usually a
        # dinner plate: the roughly-2x error migration 0015 was written about,
        # with nothing telling the user their calibration went unused.
        .eq("user_id", user_id).eq("vessel", _key(detected_vessel))
        .limit(1).execute()
        )
        if vessel_cal:
            calibration = vessel_cal

    hint = GeometryHint(
        plate_ellipse_area_ratio=(float(detection.get("plate_area_ratio") or 0) or None),
        # The vessel's apparent width and height. A round plate flattens into an
        # ellipse as the camera tilts, and h/w is the cosine of that tilt — the
        # only free measurement of camera angle we have, and the thing that says
        # whether this photo contains any height information at all.
        plate_ellipse_wh=_plate_ellipse(detection, measured_aspect or camera_aspect_ratio),
        plate_diameter_mm=(
            plate_diameter_mm
            or (_num(calibration.get("real_diameter_mm"))
                if _calibration_fits(calibration, detected_vessel) else None)
        ),
        # A calibration only describes the vessel it was measured on.
        #
        # The default calibration is picked before the photo is analysed, so a
        # user who measured their 270 mm dinner plate and then photographed
        # soup had the PLATE's area divided by the BOWL's share of the frame --
        # and the result was reported as plate_reference, the highest-trust
        # rung in the table. A measurement of the wrong object is worse than no
        # measurement, because it is believed more.
        reference_area_mm2=(
            _num(calibration.get("real_area_mm2"))
            if _calibration_fits(calibration, detected_vessel) else None
        ),
        depth_mm=camera_distance_mm,
        camera_fov_deg=camera_fov_deg,
        aspect_ratio=(measured_aspect or camera_aspect_ratio),
        image_count=len(images),
        vessel=(str(detection.get("container")) if detection.get("container") else None),
        vessel_shape=(
            str(detection.get("container_shape")) if detection.get("container_shape") else None
        ),
        # An object of known real size, found in the pixels rather than
        # described by the model. This is the only scale a cheesesteak on
        # butcher paper has.
        reference_kind=(reference.kind if reference else None),
        reference_frame_width_mm=(reference.frame_width_mm if reference else None),
        reference_tilt_deg=(reference.tilt_deg if reference else None),
    )

    # THE FREE MEASUREMENT.
    #
    # A card was found in the pixels, so the frame's real width is known to
    # about 1%. The model has said how wide the vessel looks. Multiply, and this
    # photo has just measured the user's plate -- at no cost to them, from a
    # photo they were taking anyway.
    #
    # One of these is worth about 4% on that plate's width, because the model
    # rounds its geometry to a 0.05 grid. Twenty of them average to under 1%,
    # which is the whole design of the trial period: the app spends three weeks
    # measuring the user's kitchen while they get on with logging meals.
    if reference and detected_vessel and hint.plate_ellipse_wh:
        observed = scale_learning.observe_width_mm(
            reference.frame_width_mm, hint.plate_ellipse_wh[0]
        )
        if observed:
            scale_learning.record(
                user_id=user_id, vessel=_key(detected_vessel),
                width_mm=observed, source="reference_object", scan_id=scan_id,
                prior_mm=_num(calibration.get("real_diameter_mm")) if calibration else None,
            )

    items, geo_notes = await build_items(
        raw_items, hint,
        # One photo only, for the same reason second_look is: with several
        # angles a bounding box does not say which frame it was drawn on, and a
        # seed placed on the wrong frame lands on the wrong food.
        raw=(fetched[0].raw
             if (measure_footprints and len(fetched) == 1) else None),
        plate_bbox=(detection.get("plate_bbox")
                    if isinstance(detection.get("plate_bbox"), dict) else None),
        user_id=user_id,
    )
    items, meta = await refine(
        items, profile, day_totals, user_id, str(detection.get("scene_notes") or "")
    )

    totals = Macros()
    for it in items:
        totals = totals + it.macros

    # ---- persist meal ----
    now = datetime.now(timezone.utc)
    meal = sb.table("meals").insert({
        "user_id": user_id,
        "scan_id": scan_id,
        "eaten_at": now.isoformat(),
        "day": today,
        "meal_slot": meal_slot or _slot_for_hour(now.hour),
        "title": meta.get("title") or "Scanned meal",
        "kcal": round(totals.kcal, 2),
        "protein_g": round(totals.protein_g, 2),
        "carbs_g": round(totals.carbs_g, 2),
        "fat_g": round(totals.fat_g, 2),
        "fiber_g": round(totals.fiber_g, 2),
        "sugar_g": round(totals.sugar_g, 2),
        "sodium_mg": round(totals.sodium_mg, 2),
        "confidence": meta.get("overall_confidence"),
        "photo_path": image_paths[0],
    }).execute().data[0]

    if items:
        sb.table("meal_items").insert([
            {
                "meal_id": meal["id"],
                "food_fact_id": it.food_fact_id,
                "name": it.name,
                "cuisine": it.cuisine,
                "grams": it.grams,
                "grams_low": it.grams_low,
                "grams_high": it.grams_high,
                "estimation_method": it.estimation_method,
                "pixel_area_ratio": it.pixel_area_ratio,
                "depth_factor": it.depth_factor,
                "confidence": it.confidence,
                "kcal": round(it.macros.kcal, 2),
                "protein_g": round(it.macros.protein_g, 2),
                "carbs_g": round(it.macros.carbs_g, 2),
                "fat_g": round(it.macros.fat_g, 2),
                "fiber_g": round(it.macros.fiber_g, 2),
                "sugar_g": round(it.macros.sugar_g, 2),
                "sodium_mg": round(it.macros.sodium_mg, 2),
                "bbox": it.bbox,
            }
            for it in items
        ]).execute()

    # ---- intake assessment ----
    meals_today = len(
        sb.table("meals").select("id").eq("user_id", user_id).eq("day", today).execute().data or []
    )
    activity_kcal = int(summary.get("kcal_out") or 0)
    verdict = None
    if targets:
        verdict = await assessment.assess(
            user_id=user_id,
            profile=profile,
            targets=targets,
            day_totals=day_totals + totals,
            meal=totals,
            meal_slot=meal["meal_slot"],
            meals_today=meals_today,
            activity_kcal=activity_kcal,
        )
        sb.table("intake_assessments").insert({
            "user_id": user_id, "meal_id": meal["id"], "day": today,
            "severity": verdict["severity"], "kcal_over": verdict["kcal_over"],
            "pct_of_target": verdict["pct_of_target"],
            "carb_load_flag": verdict["carb_load_flag"],
            "meal_frequency": verdict["meal_frequency"],
            "headline": verdict["headline"], "detail": verdict["detail"],
            "portion_advice": verdict["portion_advice"],
            "macro_corrections": verdict["macro_corrections"],
            "next_meal": verdict["next_meal"],
        }).execute()

    latency = int((time.perf_counter() - t0) * 1000)
    confidence = float(meta.get("overall_confidence") or 0.5)
    sb.table("food_scans").update({
        # Recorded so a later correction can be attributed to the right vessel.
        "vessel": detected_vessel,
        "status": "complete",
        "vision_model": settings.vision_model,
        "reasoning_model": settings.reasoning_model,
        "overall_confidence": confidence,
        "confidence_band": band_label(confidence),
        "latency_ms": latency,
    }).eq("id", scan_id).execute()

    # Streak + motivation are side effects; never let them break the response.
    asyncio.create_task(_post_scan_effects(user_id, meal["id"], verdict, totals))

    scale_source, measured = scale_summary(items)

    return ScanResult(
        scan_id=scan_id,
        meal_id=meal["id"],
        status="complete",
        items=items,
        totals=totals,
        scale_source=scale_source,
        portion_measured=measured,
        overall_confidence=confidence,
        confidence_band=band_label(confidence),
        needs_review=bool(meta.get("needs_review")),
        # Identification changes lead, because a wrong food is a wrong meal
        # and the person needs to see that line before any portion arithmetic.
        notes=second_look_notes + (meta.get("corrections") or []) + geo_notes,
        latency_ms=latency,
        assessment=verdict,
    )


async def _post_scan_effects(user_id: str, meal_id: str, verdict: dict | None, totals: Macros):
    try:
        from ..motivation.engine import on_event

        service().rpc(
            "bump_streak",
            {"p_user": user_id, "p_kind": "log", "p_day": date.today().isoformat()},
        ).execute()
        trigger = "meal_logged"
        if verdict and verdict["severity"] in ("moderate", "severe"):
            trigger = "overate"
        await on_event(user_id, trigger, {"kcal": round(totals.kcal), "meal_id": meal_id})
    except Exception as exc:  # noqa: BLE001
        log.warning("post_scan_effects_failed", error=str(exc)[:200])


async def _fail(scan_id: str, message: str, t0: float, status: str = "failed") -> ScanResult:
    latency = int((time.perf_counter() - t0) * 1000)
    service().table("food_scans").update(
        {"status": status, "error": message[:400], "latency_ms": latency}
    ).eq("id", scan_id).execute()
    return ScanResult(
        scan_id=scan_id, status=status, items=[], totals=Macros(),
        overall_confidence=0.0, confidence_band="low", needs_review=True,
        notes=[message], latency_ms=latency,
    )


def _slot_for_hour(hour: int) -> str:
    if 4 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 15:
        return "lunch"
    if 17 <= hour < 22:
        return "dinner"
    return "snack"
