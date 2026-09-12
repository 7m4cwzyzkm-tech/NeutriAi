"""Pixel-to-gram portion estimator.

The idea in one paragraph: a photo gives us *area*, but food is sold in *mass*.
To get from one to the other we need three things — a real-world scale (how many
mm² is one pixel?), a height model (how tall is the pile?), and a density (how
many grams per cm³ of this food?). Each of the three has a defensible default
and a better answer when the user gives us more information, so the estimator is
written as a ladder: use the best rung available, and report honestly which rung
it used.

    grams = pixel_area_mm2 x effective_height_mm x shape_factor x density_g_ml / 1000

Rungs, best to worst:

1. ``plate_reference`` — a calibrated plate/card/coin in frame gives mm-per-pixel
   directly. Error ~10-15%.
2. ``depth_model``     — the phone's ARKit/Depth API plane distance. Error ~15-20%.
3. ``multi_image``     — two or more angles let us cross-check the height guess.
4. ``vessel_reference`` — the vision model named the vessel (bowl, takeout box,
   tray, cutting board) and we know its typical size. Error ~18-25%.
5. ``pixel_area``      — a dish was seen but not identified; assume a standard
   27 cm dinner plate. Error ~25-35%.
6. ``ai_prior``        — no usable geometry; fall back to the model's own
   "typical serving" guess. Error ~40%+, and we say so in the UI.

Nothing here pretends to be more accurate than it is: every item carries a
low/high band, and the band widens as we descend the ladder.
"""
from __future__ import annotations

import math
import re

from .. import portion_learning
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Priors
# ---------------------------------------------------------------------------

# Standard dinner plate, used when nothing better is available.
DEFAULT_PLATE_DIAMETER_MM = 270.0

# Used only when the phone does not report its own optics. A mid-range
# value across current handsets; the real number is always better, which is
# why the client is asked for it.
DEFAULT_CAMERA_FOV_DEG = 68.0
DEFAULT_ASPECT_RATIO = 4.0 / 3.0

# How much of the bounding box is actually food (vs. background showing through)
# and how the pile is shaped. A steak is a slab; rice is a mound; soup is a disc.
# shape_factor ~ (mean height) / (max height) x (fill ratio of the bbox).
SHAPE_FACTORS: dict[str, float] = {
    "flat": 0.85,      # steak, fillet, tortilla, pancake, pizza slice
    "mound": 0.55,     # rice, mashed potato, couscous, oatmeal
    "loose": 0.50,     # salad greens, fries, shredded cabbage
    "cluster": 0.62,   # broccoli florets, berries, nuts, grapes
    "liquid": 0.95,    # soup, smoothie, sauce in a bowl
    "wrapped": 0.70,   # burrito, sandwich, sushi roll -- CLOSED, so it is tall
    "topped_flat": 0.42,  # taco, tostada, pizza slice -- OPEN, so it is not
    "chunky": 0.55,       # whole solid pieces with gaps between them
    "default": 0.60,
}

# Mean height as a share of PEAK height — the pile's profile alone.
#
# SHAPE_FACTORS above bundles two different corrections: this profile ratio AND
# how much of a bounding box the food fills. That was right when the area came
# from a bbox. It is double-counting once the area is the food's true footprint
# and the height is measured rather than assumed, and applying it to a measured
# height roughly halves the answer.
#
# So a measured height uses these instead: purely how the pile tapers from its
# peak to its edges. A dome averages about two thirds of its peak; a slab is
# nearly all of it.
PROFILE_FACTORS: dict[str, float] = {
    "flat": 0.90,      # a steak is almost the same height all over
    "mound": 0.68,     # a dome of rice tapers to nothing at the rim
    "loose": 0.60,     # fries and greens are mostly air near the top
    "cluster": 0.70,
    "liquid": 0.98,    # a level surface
    "wrapped": 0.85,
    # Most of an open flatbread's footprint is bare bread; the filling sits on
    # part of it and tapers. Mean height over the whole disc is a small share
    # of the peak -- lower than any pile, because a pile covers its own base.
    "topped_flat": 0.45,
    # A sphere's mean height over its own footprint is exactly 2/3 of its
    # diameter. A bed of whole rounded pieces is a little less, because they
    # settle into each other. Geometry, not a fitted number.
    "chunky": 0.62,
    "default": 0.72,
}

# Typical served height in mm — the "how tall is the pile" prior, before any
# depth signal refines it.
HEIGHT_PRIORS_MM: dict[str, float] = {
    "flat": 18.0,
    "mound": 32.0,
    "loose": 35.0,
    "cluster": 28.0,
    "liquid": 25.0,   # visible surface x realistic bowl depth, not full bowl height
    "wrapped": 42.0,  # a burrito is a fat cylinder
    # An open flatbread with something on it, built from its parts rather than
    # fitted: a corn tortilla is about 2 mm, and a filling mound stands maybe
    # 14 mm above it at its peak. Nothing here was chosen to land on a
    # particular meal -- see the note on `topped_flat` in _classify_shape.
    "topped_flat": 16.0,
    # Whole solid pieces: cubes threaded on a skewer, roast potatoes,
    # meatballs, wings. They stand as tall as the pieces are thick and they
    # stack, where every other prior in this table was calibrated on plated
    # food lying flat.
    #
    # Measured: 428 g of beef, potatoes and tomatoes on a 10 inch plate, with
    # the area read correctly -- 40% of the plate, against 19,628 mm2 measured
    # off a credit card in another shot of the same meal, agreeing within 3% --
    # came out at -29.9%. Right scale, right area, still a third light. A 40 mm
    # piece is what that shortfall is made of.
    "chunky": 40.0,
    "default": 28.0,
}

# ---------------------------------------------------------------------------
# Food groups.
#
# Knowing WHAT KIND of food something is does three things a name alone cannot:
#
#   1. It stops wrong merges. Two detections only describe the same food if
#      they are the same kind of food -- rice must never be folded into pasta
#      because their names both mention a plate.
#   2. It gives a density when the database has none, and a group-specific
#      guess beats one number for everything.
#   3. It makes calories checkable. Energy density is bounded by physics: pure
#      fat is about 9 kcal/g and nothing edible exceeds it. A cheesesteak came
#      back at 5 kcal/g during testing -- denser than sugar -- and nothing in
#      the pipeline noticed, because there was nothing to compare against.
#
# The ranges below are generous on purpose. They exist to catch the impossible,
# not to second-guess an unusual dish.
FOOD_GROUPS: dict[str, dict] = {
    #                     density   kcal/g plausible      what it covers
    "protein":    {"density": 1.03, "kcal_g": (0.6, 4.5)},   # meat, fish, eggs, tofu
    "grain":      {"density": 0.75, "kcal_g": (0.7, 4.0)},   # rice, pasta, bread
    "vegetable":  {"density": 0.55, "kcal_g": (0.1, 1.5)},
    "fruit":      {"density": 0.85, "kcal_g": (0.2, 3.5)},   # dried fruit runs high
    "legume":     {"density": 0.78, "kcal_g": (0.6, 4.0)},   # beans, lentils, hummus
    "dairy":      {"density": 1.02, "kcal_g": (0.3, 4.5)},   # milk through hard cheese
    "fat_oil":    {"density": 0.92, "kcal_g": (3.0, 9.2)},   # oil, butter, dressing
    "sweet":      {"density": 0.80, "kcal_g": (1.5, 6.0)},   # cake, chocolate, syrup
    "beverage":   {"density": 1.00, "kcal_g": (0.0, 1.2)},
    # Dry, fatty snack food is genuinely energy-dense -- crisps are ~5.5 kcal/g
    # and nuts ~6.5. They need their own groups, because the alternative is a
    # composite range so wide it catches nothing.
    "snack":      {"density": 0.35, "kcal_g": (2.0, 6.0)},   # crisps, crackers, pretzels
    "nuts_seeds": {"density": 0.55, "kcal_g": (4.0, 7.2)},
    # A cooked mixed dish. Pizza is about 2.7 kcal/g, lasagna 1.5, a burger 2.5;
    # almost nothing assembled from real ingredients passes 3.5. Kept tight
    # deliberately: a sandwich reported at 5.0 kcal/g -- denser than sugar --
    # went unremarked during testing because this range used to reach 5.0.
    "composite":  {"density": 0.85, "kcal_g": (0.3, 3.5)},
}
DEFAULT_FOOD_GROUP = "composite"

# Nothing edible carries more energy than pure fat. This is a floor on
# nonsense, applied even when the group is unknown.
MAX_KCAL_PER_GRAM = 9.4


def food_group(name: str | None) -> str:
    """Normalise a reported group, falling back to 'composite'."""
    key = _key(name)
    return key if key in FOOD_GROUPS else DEFAULT_FOOD_GROUP


def group_density(group: str | None) -> float:
    return FOOD_GROUPS.get(food_group(group), FOOD_GROUPS[DEFAULT_FOOD_GROUP])["density"]


def implausible_energy(kcal: float, grams: float, group: str | None) -> str | None:
    """Is this item's energy density physically possible for its kind of food?

    Returns a human-readable reason, or None when it is fine. Deliberately
    one-sided in tone: this reports a contradiction, it does not silently
    "correct" the number, because we do not know which of the two inputs is
    wrong -- the portion or the food it was matched to.
    """
    if grams <= 0 or kcal <= 0:
        return None
    per_g = kcal / grams
    if per_g > MAX_KCAL_PER_GRAM:
        return (f"{per_g:.1f} kcal per gram is denser than pure fat, which is "
                f"not physically possible -- the food match or the portion is wrong.")
    lo, hi = FOOD_GROUPS.get(food_group(group), FOOD_GROUPS[DEFAULT_FOOD_GROUP])["kcal_g"]
    if per_g > hi * 1.35:
        return (f"{per_g:.1f} kcal per gram is far above the range for "
                f"{food_group(group)} ({lo:.1f}-{hi:.1f}) -- worth checking the "
                f"food was identified correctly.")
    if per_g < lo * 0.5:
        return (f"{per_g:.1f} kcal per gram is far below the range for "
                f"{food_group(group)} ({lo:.1f}-{hi:.1f}) -- worth checking.")
    return None


# g/mL. Falls back to 0.85 (roughly cooked mixed food) when unknown.
# Density in g/ml.
#
# HOW TO ADD OR CORRECT A VALUE -- do not reason one out.
#
#   density = (published grams per US cup) / 236.588
#
# Cup weights are measured and published; densities are not. Every value marked
# [src] below came through that formula from a cited source. Values marked [est]
# are still reasoned estimates and should be replaced the same way when a meal
# containing them shows a consistent error.
#
# This matters more than it looks. Two constants here were wrong by a third:
#   legume 0.78     -- the density of whole beans sitting in broth, applied to
#                      refried beans, a paste with no gaps. Cost 18-22% on two
#                      separate weighed meals.
#   rice 0.78       -- close to UNCOOKED rice (~0.85). Cooked white rice is
#                      158 g/cup = 0.668. We only ever scan cooked food. Read
#                      +19.3% and +22.4% on two weighed meals before this.
#
# Both looked plausible. Neither survived a cup and a scale.
DENSITY_G_ML: dict[str, float] = {
    # [src] USDA cooked white rice, 158 g/cup -> 0.67. Was 0.78, which is close
    # to UNCOOKED rice; we only ever scan cooked food.
    "rice": 0.67,
    "pasta": 0.65, "bread": 0.28, "potato": 0.62, "fries": 0.42,
    "chicken": 1.05, "beef": 1.05, "pork": 1.05, "fish": 1.04, "shrimp": 1.02,
    "egg": 1.03, "cheese": 1.05, "yogurt": 1.03, "milk": 1.03,
    "salad": 0.22, "lettuce": 0.20, "spinach": 0.25, "broccoli": 0.35,

    # A BOUND SALAD IS NOT A LEAFY ONE.
    #
    # "salad" at 0.22 is the density of loose lettuce leaves, and longest-match
    # handed it to every dish with the word in its name. A weighed 89 g of
    # macaroni salad measured 5,252 mm2 of footprint on this user's scale, and
    # at 0.22 that is a pile of macaroni salad SEVENTY-SEVEN MILLIMETRES tall.
    # At the composite density it is 19.9 mm, which is what a scoop on a plate
    # looks like. Same defect as "broccoli cheese soup" resolving to broccoli.
    #
    # These are set to FOOD_GROUPS["composite"]["density"] -- 0.85, the
    # mixture density this codebase already uses for a dish that is not one
    # ingredient -- rather than to per-food numbers invented here. Only the
    # scale can say what each of these really is, and it has said so for
    # exactly one of them. Leafy salads keep 0.22, which is correct for them.
    "macaroni salad": 0.85, "pasta salad": 0.85, "potato salad": 0.85,
    "egg salad": 0.85, "tuna salad": 0.85, "chicken salad": 0.85,
    "fruit salad": 0.85, "bean salad": 0.85, "three bean salad": 0.85,
    "beans": 0.72, "lentils": 0.80, "chickpeas": 0.75, "corn": 0.72,
    "avocado": 0.92, "banana": 0.94, "apple": 0.85, "berries": 0.62,
    "soup": 1.00, "stew": 1.02, "sauce": 1.05, "oil": 0.92, "butter": 0.91,
    "nuts": 0.55, "granola": 0.45, "cereal": 0.35, "oatmeal": 0.90,
    "cake": 0.45, "cookie": 0.55, "chocolate": 1.30, "ice_cream": 0.55,
    "tofu": 1.05, "noodles": 0.70, "sushi": 0.95, "pizza": 0.55,

    # Mashed and pureed preparations are a different food from the thing they
    # are made of. Whole beans cooked in liquid are 0.72 because the gaps
    # between them hold air and broth; refried beans are a smooth paste with no
    # gaps at all, nearer 1.05. Measured on two weighed meals, refried beans
    # came in 22.5% and 18.0% light -- both under, both by about the amount
    # this density error predicts.
    #
    # These are matched ahead of their base ingredient by longest-key wins,
    # below.
    "refried beans": 1.06, "refried": 1.06,   # [src] canned, ~252 g/cup
    "mashed potato": 1.04, "mashed": 1.00,
    "hummus": 1.06, "puree": 1.05, "pureed": 1.05,
    "guacamole": 0.95, "gravy": 1.05, "risotto": 0.95,
    "default": 0.85,
}

# ---------------------------------------------------------------------------
# Reference objects — scale from what is lying next to the food.
#
# The measured problem: every meal with a plate lands inside 10%, and every
# meal without one is 30-35% out. A cheesesteak on its wrapper and pasta in a
# takeout box have no reference at all, and no central prior can supply one,
# because containers are not standard.
#
# A credit card is not a prior. It is 85.60 x 53.98 mm by international
# standard, identical worldwide, and most people carry one. That makes it the
# only everyday object whose size can be used as a measurement rather than an
# assumption -- and the sizes themselves live in reference_cv.py, next to the
# code that finds them, because a size is useless here without a way to
# locate the thing it describes.
#
# The reference object is now MEASURED, not described.
#
# The history is worth keeping, because it is the argument for the whole
# approach. A credit card lying flat beside two 133 g breakfast tacos. Its true
# length is 0.362 of the frame width -- measured off the pixels, cross-checked
# by the tortillas coming out at street-taco size. Asked how long the card was,
# the vision model said 0.18: the WIDTH of the box around a card standing
# vertically. Asked for a box instead, it drew one half the right size. Both
# put the photo at ~460 mm across against a true 236, and since frame AREA goes
# as the square of width, the meal read +50% where no reference at all gave
# +12.8%.
#
# So the model is no longer asked. reference_cv.py finds the card in the pixels
# -- edges, closed quadrilaterals, corner angles, side ratio, and agreement
# across several edge settings -- and hands over a frame width in millimetres.
# On the same photo that gives 252 mm against 236 hand-measured, and zero false
# positives across ten card-free bench photos.
#
# What survives from the old path is this rail, because a measurement can still
# be wrong and a scale is the one number that multiplies everything:
#
#   lower bound, optics  -- a phone cannot focus nearer than a ~140 mm frame
#   upper bound, usage   -- past a metre you are photographing a table, not a
#                           meal; a 1000 mm frame already puts a dinner plate
#                           at a quarter of the width
REFERENCE_MIN_FRAME_MM = 120.0
REFERENCE_MAX_FRAME_MM = 1000.0

# How tightly each reference's real size clusters in the world -- the same idea
# as VESSEL_CERTAINTY, and what decides which rung wins when a photo offers
# both. A credit card is exact by international standard, so it beats every
# vessel prior except a plate the user measured themselves.
REFERENCE_CERTAINTY: dict[str, float] = {
    "credit_card": 0.98,
}
DEFAULT_REFERENCE_CERTAINTY = 0.80


# Vessel size priors.
#
# Scale has to come from something of known real-world size. A dinner plate was
# the only reference the estimator understood, so every surface was treated as
# one -- and a takeout clamshell roughly 190 mm across, read as a 270 mm plate,
# inflates the frame area by about 2x and every gram with it. Most photographed
# food is not on a dinner plate.
#
# Areas are the vessel's footprint in mm^2, not a diameter, because half of
# these are rectangular and pi/4 on a clamshell is simply wrong.
#
# `certainty` is how tightly that size clusters in the real world. Dinner plates
# are near-standard; takeout containers come in every size a supplier sells. It
# multiplies detection confidence, so a guess built on a loose reference reports
# a wider band instead of false precision.
# Stored as a WIDTH and a SHAPE rather than a finished area.
#
# Everything was previously a fixed area computed as a circle, so a square
# plate -- same width, 27% more surface -- was read 27% light, and so was every
# rectangular takeout tray and cutting board. Shape is also the one attribute
# a vision model reads reliably: it flips between "bowl" and "large_bowl" on
# the same bowl, but it does not mistake a square plate for a round one. That
# makes shape worth modelling and fine size classes not worth it.
VESSEL_WIDTH_MM: dict[str, float] = {
    "dinner_plate":    270.0,
    "plate":           270.0,
    "side_plate":      200.0,
    "salad_plate":     200.0,
    # One prior for every bowl, deliberately. These started split -- 165 mm for
    # a cereal bowl, 220 for a pasta bowl. Measurement killed that: two photos
    # of THE SAME bowl, minutes apart, came back "large_bowl" then "bowl", and
    # the estimate moved 487.6 g -> 274.3 g on identical food. A size prior
    # cannot be finer-grained than the classifier choosing between them.
    "bowl":            190.0,
    "soup_bowl":       190.0,
    "large_bowl":      190.0,
    "pasta_bowl":      190.0,
    "takeout_box":     190.0,
    "box":             190.0,
    "clamshell":       190.0,
    "tray":            350.0,
    "cutting_board":   400.0,
    "board":           400.0,
    "skillet":         260.0,
    "pan":             260.0,
    "cup":              80.0,
    "mug":              90.0,
    "glass":            80.0,
}

# What each vessel usually is, when the model does not say.
VESSEL_DEFAULT_SHAPE: dict[str, str] = {
    "takeout_box": "square", "box": "square", "clamshell": "square",
    "tray": "rectangular", "cutting_board": "rectangular", "board": "rectangular",
}

# Footprint area as a fraction of width squared. Rectangular and oval assume a
# 4:3 proportion, which is what serving ware overwhelmingly is.
SHAPE_AREA_FACTOR: dict[str, float] = {
    "round": math.pi / 4.0,        # 0.785
    "circle": math.pi / 4.0,
    "oval": math.pi / 4.0 * 0.75,  # 0.589
    "square": 1.0,
    "rectangular": 0.75,
    "rectangle": 0.75,
}
DEFAULT_VESSEL_SHAPE = "round"

# Below this, the "vessel" is not a size reference at all. Paper, foil, a hand,
# a napkin and a bare table have no standard dimension, so claiming one would be
# inventing scale. These fall through to the model's serving prior, which is
# honest about being a guess.
VESSEL_NOT_A_REFERENCE = {
    "none", "paper", "wrapper", "foil", "napkin", "hand", "table", "counter",
    "unknown", "",
}

VESSEL_CERTAINTY: dict[str, float] = {
    "dinner_plate": 1.00, "plate": 1.00, "side_plate": 0.92, "salad_plate": 0.92,
    # One shared prior across a 1.8x real-world spread -- say so.
    "bowl": 0.68, "soup_bowl": 0.68, "large_bowl": 0.68, "pasta_bowl": 0.68,
    "takeout_box": 0.68, "box": 0.68, "clamshell": 0.68,
    "tray": 0.72, "cutting_board": 0.62, "board": 0.62,
    "skillet": 0.80, "pan": 0.80,
    "cup": 0.85, "mug": 0.85, "glass": 0.85,
}
DEFAULT_VESSEL_CERTAINTY = 0.70


def _key(name) -> str:
    """Normalise a label from the model. Anything not a string is not a label --
    models return numbers and nulls in enum fields often enough that treating
    that as a crash would cost a user their scan."""
    if not isinstance(name, str):
        return ""
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def vessel_area(vessel: str | None, shape: str | None = None) -> tuple[float | None, float]:
    """Footprint area in mm^2 for a named vessel, plus how much to trust it.

    Shape comes from the photo when the model reports it, and falls back to
    whatever that vessel usually is -- square for a clamshell, rectangular for
    a tray, round for everything else.
    """
    key = _key(vessel)
    if not key or key in VESSEL_NOT_A_REFERENCE:
        return None, DEFAULT_VESSEL_CERTAINTY
    width = VESSEL_WIDTH_MM.get(key)
    if width is None:
        return None, DEFAULT_VESSEL_CERTAINTY

    shape_key = _key(shape) or VESSEL_DEFAULT_SHAPE.get(key, DEFAULT_VESSEL_SHAPE)
    factor = SHAPE_AREA_FACTOR.get(
        shape_key, SHAPE_AREA_FACTOR[VESSEL_DEFAULT_SHAPE.get(key, DEFAULT_VESSEL_SHAPE)]
    )
    return width * width * factor, VESSEL_CERTAINTY.get(key, DEFAULT_VESSEL_CERTAINTY)


# Coverage-dependent height.
#
# The height priors above describe a *typical* serving -- one covering roughly
# a third of the plate. Food spread wider than that is not taller, it is
# thinner: the same 200 g of scrambled egg is a deep pile on a small plate and
# a thin layer on a large one. Treating height as independent of coverage
# makes grams scale linearly with detected area, which is both physically
# wrong and maximally sensitive to a noisy area estimate.
#
#   effective_height = height_prior x (REFERENCE_COVERAGE / coverage) ^ SPREAD_EXPONENT
#
# The exponent picks how much of the spread is absorbed by height:
#   0.0  height fixed; grams proportional to area          (the old behaviour)
#   1.0  volume conserved; grams independent of area       (area tells us nothing)
# Neither extreme is right. Detected area does carry real information about
# portion size, but not proportionally. 0.5 sits deliberately between them and
# makes grams scale with the square root of area, which also halves how much
# vision noise reaches the output.
#
# The correction is deliberately ASYMMETRIC, because only one direction has a
# mechanism behind it:
#
#   spread wide  -> genuinely thinner. A fixed volume over more area must be
#                   shallower. Physics. Damp hard.
#   small area   -> NOT necessarily taller. A small footprint almost always
#                   means a smaller portion, not a steeper pile. Barely inflate.
#
# Treating those symmetrically was a real defect, and a measured one. On a plate
# holding a drumstick, refried beans and rice, every item covers a small share
# of the plate, so all three were scaled UP (x1.18, x1.45, x1.45) and the errors
# compounded: 636 g estimated against 312 g weighed, +104%. The same build was
# within 12% on single-item vessels. A smear of refried beans is thin, and
# reading it as a tall pile because it is small is exactly backwards.
# The largest share of its bounding box a real food occupies. A rectangle
# is never filled: a round mound tops out near pi/4 = 0.79, an irregular
# drumstick or a scatter of florets far less. 0.80 is a ceiling, not an
# estimate -- it only ever catches claims that are physically impossible.
BBOX_FILL_CEILING = 0.80

# How far the bounding-box rail is allowed to cut an area estimate.
#
# The rail exists to catch physically impossible claims -- an item cannot cover
# more area than its own box. It was written on the assumption that a box is
# the more trustworthy of the two numbers, and on a weighed plate that
# assumption failed: a chicken drumstick's area was cut from 15% of frame to
# 5%, threefold, and the portion came out 38% light. A drumstick is long and
# lies diagonally, so its box should exceed its area, not be a third of it.
# The box was wrong and the rail believed it.
#
# Capping the cut at half kept this a correction rather than a substitution,
# for as long as "the box was wrong" was an inference. It is not any more.
#
# MEASURED, on photos with a ruler in them:
#
#   14-plate-mole-chicken-card, drumstick   the model claimed 5% of frame, its
#       own box said 4%. Segmented against the plate at its measured 254 mm,
#       the drumstick's real footprint is 3.29% of frame. box x 0.80 = 3.2%.
#       The box was right to within 3%; the area claim was 52% high.
#
#   08c-plate-mole-chicken-tape, refried beans   segmented against a tape
#       measure lying in the photo: 4145 mm2 of footprint inside an 855 x 840
#       px box. Fill = 0.80, which is BBOX_FILL_CEILING to two decimals.
#
# So the ceiling constant is right and the box is the trustworthy number. The
# floor is what stops the rail reaching it, and it is doing real damage: across
# the 11-meal bench, 20 of 22 items reported an area LARGER than their own box,
# most by 2x to 5x, and never once smaller. A signal that is biased in one
# direction on 20 of 22 samples is not a noisy measurement to be averaged with
# the box -- it is a broken measurement, and averaging carries half the break
# through. Every badly overestimated small item on the bench shows the floor
# winning: cherry tomatoes at area 5% / box 1% land on 2.5%, not 0.8%, and come
# out +147%.
#
# At 0.0 the rail landed on box x BBOX_FILL_CEILING whenever the box was
# smaller. The bench said the old caution was earned after all, so it is back --
# but for a reason nobody had before, and the reason is the important part.
#
# RUN AT 0.0:  per-item error 44.7% -> 25.0%, the best split this bench has seen.
#              But meal bias went -0.3% -> -15.2%: everything came out light.
#
# WHY. The model's boxes are UNDERSIZED, measured twice on the same 146 g leg:
#
#   photo 08   box 4.00% of frame   real footprint 4.84%
#   photo 14   box 3.00% of frame   real footprint 4.50%
#
# Food cannot exceed its own bounding box, so those boxes are not merely
# inaccurate, they are impossible. Multiplying an already-short box by 0.80
# shortened it again, and that product IS the -15.2%.
#
# It also sinks the obvious next idea. GrabCut seeded by the box measures the
# food properly -- but only inside the rectangle it is given, so it inherits the
# undersizing almost linearly (photo 14's drumstick, truth 4.50% of frame):
#
#   seeded with a good box                  3.34%
#   seeded with a box shrunk 20%            2.53%
#   seeded with a box shrunk 35%            1.63%
#   that same bad box, grown 1.6x again     3.36%   <- recovers
#
# Run across the real detector it scored 50.9% against the model's 38.3%: worse,
# because it measured the undersizing more precisely. Every method that STARTS
# from the box inherits the box's error. The fix has to not start there --
# segment the plate's food without boxes (which already reproduced weighed meals
# at -3.0% and -0.0% in `dev seg`) and use boxes only to say which region is
# which, since position is the thing they are actually good at.
#
# Until that exists, the rail lands on the box ceiling, lifted by one flat
# allowance -- see BOX_TIGHTNESS -- and never on a fraction of the area.
#
# The partway cut, `max(ceiling, area x 0.5)`, was the mistake. Its floor was a
# fraction of the AREA: the number the rail had just decided not to trust. So
# the wilder the model's claim, the bigger the portion, right up to the absurd
# line, where the claim was thrown out and the portion collapsed back to the
# ceiling. Measured on a 4%-of-frame box:
#
#   model claims   ratio   answer as a multiple of the box ceiling
#      6.4%         2.0x        1.00
#      9.6%         3.0x        1.50
#     12.8%         4.0x        2.00   <- peak
#     13.2%         4.1x        1.00   <- cliff
#
# Two photographs of one scoop of refried beans landed either side of that
# cliff -- ratio 3.4 and ratio 4.4 -- and came back +89% and -20% against the
# same kitchen scale. Nothing about the beans changed; a rounding in a number
# we had already discarded did.
# How much bigger the food is than the ceiling its own box implies.
#
# Two effects pull opposite ways and this is their net. Food does not fill its
# bounding rectangle -- BBOX_FILL_CEILING, ruler-checked twice at 0.80 -- and
# the boxes themselves are drawn slightly inside the food, measured at 4.00%
# of frame against a photographed 4.84%.
#
# Fitted, not derived, over all fourteen railed items on the weighed bench:
#
#   lift    mean abs   bias    worst item
#   1.00      15.3%    -6.9%     34.3%
#   1.05      14.2%    -2.3%     36.9%   <-
#   1.10      14.6%    +2.4%     43.4%
#   1.20      18.7%   +11.7%     56.5%
#   saw       16.6%    +3.4%     89.3%   the shape this replaced
#
# Read that table carefully, because it says the lift is nearly worthless and
# the SHAPE is where the whole win is. Every setting from 1.00 to 1.10 lands
# within 1.1 points of the others on average error, while all of them cut the
# worst item from 89% to under 45%. The level is a coin toss; the flatness is
# not.
#
# 1.05 is chosen over 1.00 for its bias, not its average: -2.3% against -6.9%.
# A systematic undercount is the specific failure every other food app has --
# the metabolic-kitchen study put four of them 33% light -- and it is worth
# 2.6 points of worst case to not inherit it.
#
# The ruler disagrees with this number and that is not resolved. Two boxes
# measured against their real footprints (4.00% of frame against 4.84%, 3.00%
# against 4.50%) imply a lift of 1.21 and 1.50, and the bench will not tolerate
# anything near either. Most likely BBOX_FILL_CEILING already absorbs part of
# the same effect, in which case these two constants are not independent and
# should eventually become one measured quantity. Until that is settled this
# stays at the smallest defensible value.
BOX_TIGHTNESS = 1.05

# How much confidence a self-contradiction costs.
#
# The rail always lands on the box now, so the LANDING no longer records how
# badly the model disagreed with itself. This does. Confidence is multiplied by
# 1 - PENALTY x (excess - 1), floored, where excess is the reported area over
# what the box allows: 1.0x costs nothing, 2x costs 18%, 3x costs 36%, and
# anything past about 3.5x is floored. Continuous on purpose -- a threshold
# would make two nearly identical photos land either side of a cliff.
# Past this the reported area is not a bad measurement of the food, it is a
# measurement of something else, and the box takes over completely.
#
# The evidence that produced this table was read the wrong way once. Both
# settings of the old partway cut were right about half the bench and wrong
# about the other half, and that was taken as a missing variable -- so the
# disagreement ratio was made to move the GRAMS. It should only ever have moved
# the confidence and the band, which is all it does now. The reading below
# stands; what changed is what the ratio is allowed to do with it:
#
#   item            area   box   disagreement   error with the floor
#   07 drumstick     14%    9%       1.9x             +2%
#   07 beans          8%    4%       2.5x             -4%
#   07 rice           8%    4%       2.5x             -6%
#   08 rice           7%    4%       2.2x             -1%
#   08 drumstick      9%    4%       2.8x            -14%
#   11 meat          25%    9%       3.5x            -14%
#   12 meat          25%    9%       3.5x            -30%
#   -------------------------------------------------------------
#   13 tomatoes       3%    1%       4.4x            +70%
#   11 potatoes      10%    2%       6.2x            +88%
#   11 tomatoes       5%    1%       6.2x           +147%
#   12 tomatoes       5%    1%       6.2x           +174%
#
# Under 3.5x the blend is right and the floor earns its place -- photo 07, the
# only fully weighed four-item plate, came in at -0.7% on the meal with that
# floor doing the work. Over 4x every single item is enormously over, and the
# box would have been close. Nothing sits in between on this bench, and the gap
# between 3.5x and 4.4x is where the line goes.
#
# Honest about its status: calibrated on 15 items from one bench. The SHAPE is
# principled -- two numbers that disagree fourfold are not both about the same
# object -- but the exact value is not, and it should move if more weighed
# photos say so.
# How far above the estimate the range may reach when the rail had to choose
# between two badly disagreeing answers. 3.0 means "this could be up to three
# times what we said" -- wide enough to keep the discarded candidate reachable,
# narrow enough that the number still means something.
# Below this share of the frame, a bounding box stops being a measurement.
#
# Not a guess. Photo 13's cherry tomatoes, weighed at 27 g: across three runs of
# identical code the model's box moved from 1% of frame to 2%, and the estimate
# moved with it, 24.6 g to 47.3 g. The same tomatoes on photos 11 and 12 came
# back 23.0 and 25.2 g in EVERY run, because their box happened to stay at 1%.
#
# The geometry is not wrong. One percentage point of frame is simply the entire
# answer for a 27 g object, and nothing downstream can recover a number the box
# never had the resolution to carry.
#
# So the grams are left exactly as they are -- we do not know a better value,
# and inventing one would be the compensating-constant trap this file keeps
# refusing -- and the RANGE is widened to say so. A garnish gets an honest wide
# band instead of a confident wrong number.
SMALL_ITEM_AREA = 0.02
# How much wider, at the extreme. Scales in between, so there is no cliff for
# two nearly identical items to fall either side of.
SMALL_ITEM_BAND_FACTOR = 2.2

BAND_MAX_SPREAD = 4.0

BBOX_ABSURD_DISAGREEMENT = 4.0

BBOX_DISAGREEMENT_PENALTY = 0.18
BBOX_DISAGREEMENT_FLOOR = 0.55
# Past this the note says plainly that something was misread, rather than
# describing it as a cap.
BBOX_SEVERE_DISAGREEMENT = 2.0

# Measured height, from an angled photo.
#
# HEIGHT_PRIORS_MM is a guess about how tall food usually is. It is the largest
# remaining guess in the pipeline: measured against a weighed plate, the area
# was right to within 3% while the height carried the entire error.
#
# A photo taken at an angle contains the answer. The plate's own outline gives
# the camera tilt (see GeometryHint.tilt_deg), and the model can judge how tall
# a pile stands against the plate's width -- the same relative-to-a-big-object
# judgement that it does well on area and badly in the abstract.
#
# Below MIN_TILT_DEG the photo is effectively top-down and contains no height
# information; a reported height there is invention and is ignored. Between
# there and FULL_TRUST_TILT_DEG the measurement is blended into the prior in
# proportion to how much of the food's side is actually visible.
# OFF, and here is the evidence for why.
#
# Enabled, this moved per-item error from 7.4% to 11.8% across the bench. It
# fired on the two angled meals and lowered every item on both: meal 08 went
# -23.1%, -11.1%, -21.7% from a starting point that was already good, while
# meal 06 improved only because it was over-estimated to begin with. A change
# that helps overestimates and hurts accurate ones is not measuring anything --
# it is a downward nudge wearing a measurement's clothes.
#
# The machinery below is correct and tested: fed the true peak height it lands
# within 5% of a weighed plate, and it degrades honestly in both directions.
# What is unknown is the ONE input it depends on -- what the model actually
# reports for height_ratio. Either it under-reports the peak, or PROFILE_FACTORS
# is too low. Those need opposite fixes and a single bench run cannot separate
# them.
#
# To turn back on: run `dev scandebug` on an angled photo of a weighed meal,
# compare the reported height_ratio against the real peak height measured with
# a ruler, and fix whichever term is wrong. Not before.
USE_MEASURED_HEIGHT = False

MIN_TILT_DEG = 20.0
FULL_TRUST_TILT_DEG = 45.0
HEIGHT_MM_MIN = 3.0      # thinner than a tortilla
HEIGHT_MM_MAX = 120.0    # taller than any plated food that is not a layer cake

# Occlusion.
#
# A single photo cannot measure food it cannot see. When rice sits on beans, or
# a plate is stacked because there was no room, the hidden item's visible area
# is a LOWER BOUND on its real footprint -- and the estimator was treating it as
# the whole thing, silently under-counting exactly the meals most likely to be
# over-served.
#
# For an app whose job is flagging overeating, and whose carb figures are
# derived from grams, under-counting is the harmful direction. So a partly
# hidden item is scaled up by the reciprocal of its visible fraction, the band
# widens upward, and it is flagged for the user to confirm rather than reported
# as though it were measured.
#
# The inflation is capped hard. A model reporting 10% visible is guessing, and
# 10x on a guess is worse than admitting the limit -- past the cap the honest
# output is a wide range and a request to confirm.
OCCLUSION_MIN_VISIBLE = 0.35      # below this we stop scaling and widen instead
OCCLUSION_MAX_INFLATE = 1.0 / OCCLUSION_MIN_VISIBLE   # ~2.9x
OCCLUSION_IGNORE_ABOVE = 0.92     # essentially unobstructed; do nothing

# Vessels with walls, where coverage does NOT mean thinness.
#
# The spread correction assumes food spreads across a flat surface: cover more
# area, be shallower. That holds on a plate. In a bowl or a deep container the
# walls hold the food up, so filling 80% of the rim means the vessel is FULL,
# which is deep -- the opposite of what the correction infers.
#
# Measured on a 152 mm bowl holding 405 g of soup: the estimator wanted an
# average depth of 26.8 mm and, after damping a full bowl as though it were a
# spreading puddle, used 15.4 mm. That single misapplied factor was -42% on
# its own, and it is why every bowl and container read low while the flat
# plates read fine.
WALLED_VESSELS = {
    "bowl", "soup_bowl", "large_bowl", "pasta_bowl",
    "takeout_box", "box", "clamshell", "cup", "mug", "glass",
}

# HOW DEEP A SERVED BOWL OF SOUP ACTUALLY IS.
#
# Liquid in a walled vessel is the one case where the food's footprint is not
# a thing to be found in the pixels: it is the vessel's mouth, because that is
# what a liquid does. Segmenting it goes wrong in a way no mask rule can fix --
# on the weighed chicken noodle SAM2 returned 2.5% of the frame, having found
# the NOODLES. Broth is not an object.
#
# So the area comes from the vessel and only the depth is unknown. Solved on
# three weighed soups in this user's 114 mm crock:
#
#     chicken noodle   151 g -> 14.7 mm
#     beef posole      182 g -> 17.4 mm
#     broccoli cheese  152 g -> 14.1 mm
#
# Leave-one-out, each predicted from the other two: mean absolute 12.6%, worst
# 17.1%, against -74.8%, -64.8% and -43.4% for the shipped pipeline on the same
# three photographs.
#
# WHAT THIS NUMBER IS AND IS NOT. The vessel's mouth is geometry and holds for
# anyone. Fifteen millimetres is a serving habit -- three bowls poured by one
# person on one evening -- so it is a prior like any other and belongs in the
# learning loop as soon as that loop works. It is stated in PRE-PROFILE units:
# PROFILE_FACTORS["liquid"] is applied after it, as for every other shape.
SOUP_DEPTH_MM = 15.0

# WHAT A MEASURED FOOTPRINT IS ACTUALLY TALL, SOLVED AGAINST THE SCALE.
#
# MEASURED_HEIGHTS_MM sorts food into eight shape words and gives each a
# height. Solved against this user's weighed photographs it is doing worse
# than TWO numbers, because the words scatter foods that behave identically:
# chicken 14 mm, pizza 16 mm, beef 24 mm, macaroni 26 mm -- and all four
# solve to between 18.9 and 23.2.
#
# What separates them is not what the food is called. It is whether the
# footprint is one connected thing. Separate pieces cannot stack; a connected
# mass can be piled. See food_seg.ONE_PIECE_SHARE for the measurement.
#
#     one connected mass   n=5   21.0 mm   spread 1.23x
#     separate pieces      n=2    9.2 mm   spread 1.37x
#
# Leave-one-out over all seven, each food predicted from the others in its
# group with nothing from the held-out photo touching its own prediction:
#
#     shape table   58.4% mean absolute
#     these two     15.0%
#
# HOW MUCH TO TRUST EACH. The pile number rests on five foods that agree to
# 1.23x and is worth as much as any number here. The separate-pieces number
# rests on TWO, and is thin -- kept because the table does 97% and 209% to
# those same two foods, so thin and right beats confident and wrong. It should
# be refitted the moment more weighed single-layer photographs exist.
#
# Applied ONLY where the footprint was actually measured. An item with no
# measured footprint keeps the shape table untouched: these heights were
# solved against measured areas and mean nothing against a railed one.
CONNECTED_PILE_HEIGHT_MM = 21.0
SEPARATE_PIECES_HEIGHT_MM = 9.2

# What a bowl of soup can weigh per millilitre. Broth is water; a thick chowder
# with cream and potato reaches about 1.10. Nothing served as soup is 0.35,
# which is what "broccoli cheese soup" resolved to before this existed.
LIQUID_DENSITY_MIN, LIQUID_DENSITY_MAX = 0.90, 1.10

# THE WIDEST MOUTH A BOWL OF SOUP ACTUALLY HAS.
#
# The branch above takes the vessel's mouth from plate_ellipse_area_ratio,
# which is whatever ellipse the pipeline found. On a dinner plate that ellipse
# is the PLATE -- 270 mm of it -- and a 270 mm disc of liquid 15 mm deep is
# 841 g of tomato soup. That is what this pipeline produced before this
# ceiling existed; the plausibility test caught it.
#
# So: a mouth wider than any bowl a person is served soup from is not a bowl's
# mouth, it is a rim we mistook for one. 185 mm is a large restaurant soup
# bowl and, at the depth above, about 400 g -- the biggest bowl of soup that
# is real. Past that the number stops being a measurement, so it is marked
# down to a floor and says so.
#
# The three weighed soups sit at a 114 mm crock, far inside this, so the
# ceiling does not touch the evidence the depth was fitted on.
MAX_BOWL_MOUTH_MM = 185.0
MAX_BOWL_MOUTH_MM2 = math.pi * (MAX_BOWL_MOUTH_MM / 2.0) ** 2

REFERENCE_COVERAGE = 0.35
SPREAD_EXPONENT = 0.5
SPREAD_FACTOR_MIN, SPREAD_FACTOR_MAX = 0.45, 1.10

# Blending geometry with the model's serving prior. Disagreement is measured
# symmetrically (3x too high and 3x too low pull equally hard). Below
# BLEND_START the geometry stands on its own; at BLEND_FULL the pull reaches
# BLEND_MAX_WEIGHT, and 0.5 there is exactly the geometric mean of the two --
# the same answer the old hard threshold gave, without the cliff.
BLEND_START = 1.5
BLEND_FULL = 2.5

# How hard the serving prior may pull, by how good the geometry is.
#
# This was a flat 0.5 -- a full geometric mean -- no matter where the scale
# came from. On a weighed plate that overruled a correct measurement: with a
# calibrated 269 mm plate the geometry put the rice at 66 g, the scale said
# 66 g, and the blend dragged it to 92 g toward a generic 150 g "typical
# serving". Every other item on that plate was within 17%; rice was +38%
# entirely because an assumption outvoted a measurement.
#
# A prior is a substitute for information. The better the geometry, the less
# substitute is warranted, so the pull now scales with the rung it is arguing
# with. A serving guess should barely move a measured plate and may reasonably
# dominate a bare pixel-area estimate.
#
# A side effect worth noting: below about 0.36 the blend also becomes monotone,
# so the 2.3% "more food reads as less food" artifact disappears on the good
# rungs and survives only where the geometry was weak anyway.
# Measured, on one meal weighed to the gram and photographed three ways.
#
# Photo 11, the beef: 335 g on a kitchen scale. The geometry, scaled from a
# credit card found in the pixels, said 344 g -- +2.7%, essentially correct.
# The blend then pulled it to 287 g, the reasoning model proposed 120 g, and
# the clamp stopped that at 219 g. What got logged was -34.7%. Photo 12, the
# same food from a measured distance: geometry 310 g, logged 273 g. Both times
# the RAW GEOMETRY was the best number in the chain and everything after it
# made the answer worse.
#
# And where the blend earns its keep, on the same bench: the soups, sized from
# a BOWL PRIOR. Geometry 453 g -> 415 g, and 638 g -> 438 g, against 405 g
# weighed. It rescued both.
#
# So the split is real, and it is the one the comment below already claims. A
# prior substitutes for information. Where the scale was MEASURED -- a card in
# the pixels, a real camera distance, a plate the user put a ruler across --
# there is little missing information to substitute for, and a serving
# convention is the weaker of the two numbers. Where the scale was ASSUMED, the
# prior is the only thing between the user and a wild answer.
#
# The measured rungs are cut hard. The assumed ones are untouched.
BLEND_MAX_WEIGHT_BY_METHOD: dict[str, float] = {
    # --- scale measured. The geometry is the evidence. ---
    "plate_reference": 0.10,   # a plate the user put a ruler across
    "reference_object": 0.10,  # an object of known size, found in the pixels
    "depth_model": 0.12,       # a real camera distance
    "multi_image": 0.15,       # several views agreeing
    # --- scale assumed. The prior is doing real work. ---
    "vessel_reference": 0.40,  # a typical size for a named vessel
    "pixel_area": 0.50,        # assume a standard plate; the prior earns its keep
    "ai_prior": 0.50,          # no geometry at all -- the prior IS the estimate
}
BLEND_MAX_WEIGHT = 0.5         # fallback for an unrecognised method

# Known, bounded artifact: because the pull toward a fixed prior grows with
# disagreement, and disagreement grows with detected area, there is a band
# (~15-25% coverage for a dense food) where a larger detection yields a
# very slightly smaller estimate. It is under 0.5% per step and ~3.6%
# across the band -- far inside the reported confidence interval, and the
# mechanism behind it (more disagreement, less trust in geometry) is the
# behaviour we want. test_portion.py pins the per-step drop at 1% so this
# stays an artifact and never grows back into the 35% cliff it replaced.

# Sanity rails. If the ladder produces something outside this, we clamp and
# drop confidence — a 4 kg serving of rice is a bug, not a big appetite.
MIN_GRAMS, MAX_GRAMS = 3.0, 1500.0


@dataclass(slots=True)
class GeometryHint:
    """Everything we know about the physical scale of this photo."""

    plate_ellipse_area_ratio: float | None = None   # plate bbox area / frame area
    plate_diameter_mm: float | None = None          # from calibration or ARKit
    reference_area_mm2: float | None = None         # calibrated reference object
    depth_mm: float | None = None                   # camera-to-subject distance
    camera_fov_deg: float | None = None             # horizontal field of view
    aspect_ratio: float | None = None               # frame width / height
    image_count: int = 1
    vessel: str | None = None                       # what the food is served on/in
    vessel_shape: str | None = None                 # round | square | rectangular | oval
    plate_ellipse_wh: tuple[float, float] | None = None  # apparent (w, h) of the vessel
    reference_kind: str | None = None                # what was found in the pixels
    reference_frame_width_mm: float | None = None    # frame width it measures, in mm
    reference_tilt_deg: float | None = None          # camera tilt, from its own shape

    @property
    def tilt_deg(self) -> float | None:
        """Camera tilt away from straight-down, from the plate's own outline.

        A circular plate photographed from directly above projects to a circle.
        Photographed at an angle it projects to an ellipse whose minor axis is
        shortened by exactly cos(tilt). So the plate measures the camera angle
        for free -- no sensor, no metadata, no native module -- and the angle is
        what decides whether a photo contains height information at all.

        None when the vessel is not round: a square plate's outline says
        nothing about tilt this way.
        """
        if not self.plate_ellipse_wh:
            return None
        if self.vessel_shape and self.vessel_shape not in ("round", "oval"):
            return None
        w, h = self.plate_ellipse_wh
        if not (w and h) or w <= 0 or h <= 0:
            return None
        # w and h are fractions of the image WIDTH and HEIGHT respectively --
        # the prompt asks for "a fraction of the image" for each. For a round
        # plate photographed straight down that makes h/w the frame's own
        # aspect ratio rather than 1, so an overhead 4:3 shot reported 41
        # degrees of tilt. Convert h into width-units before comparing.
        aspect = self.aspect_ratio
        if not aspect or aspect <= 0:
            # Without the frame's shape the two numbers are not comparable, and
            # a wrong tilt is worse than none: it decides whether a photo is
            # treated as carrying height information at all.
            return None
        h_in_width_units = h / aspect
        ratio = min(h_in_width_units, w) / max(h_in_width_units, w)
        return math.degrees(math.acos(max(0.05, min(1.0, ratio))))

    @property
    def camera_tilt_deg(self) -> float | None:
        """Camera tilt, from whichever object in the photo can measure it.

        A round plate's outline gives this, but the photos the reference object
        exists for -- food on paper, on a bare table, in a square container --
        have no round plate in them. The card is a rectangle of known
        proportions, so its own outline measures the same angle, and
        reference_cv already reports it. It was being computed, passed in, and
        then read by nothing.

        The plate wins where both exist: it is larger, so its outline is
        measured over more pixels.
        """
        return self.tilt_deg if self.tilt_deg is not None else self.reference_tilt_deg

    @property
    def has_reference(self) -> bool:
        return bool(
            self.reference_area_mm2
            or self.plate_diameter_mm
            or vessel_area(self.vessel, self.vessel_shape)[0]
            or self.reference_frame_width_mm
        )


@dataclass(slots=True)
class PortionEstimate:
    grams: float
    grams_low: float
    grams_high: float
    method: str
    confidence: float
    pixel_area_ratio: float
    depth_factor: float
    notes: list[str]
    # True when part of this food was hidden behind something else, so the
    # figure is a corrected minimum rather than a measurement.
    occluded: bool = False
    # The model outputs that actually reach the grams, carried out so the bench
    # can score THEM rather than a number that never gets there.
    #
    # Measured, one input moved at a time on a plate photo with coverage
    # present, everything else held:
    #
    #     input                     range tried     grams        swing
    #     area_ratio                  8% -> 99%     162 - 162        0%
    #     plate_area_ratio           20% -> 45%     136 - 162       19%
    #     bounding box            0.15 -> 0.60 sq    44 - 162      270%
    #     plate_coverage             10% -> 40%      81 - 276      240%
    #
    # area_ratio is inert. Every rail, ceiling, fill factor and tightness
    # constant in this file operates on it, and on a plate photo none of that
    # reaches the answer, because the coverage conversion overwrites it first.
    # The two live channels are the box and plate_coverage, and until now the
    # bench printed neither -- it printed area_ratio, which is why four nights
    # of tuning changed nothing anybody could see.
    reported_area_ratio: float | None = None
    plate_coverage_used: float | None = None
    box_area_ratio: float | None = None
    # The footprint MEASURED off the pixels, when one was used. None means the
    # grams came from the model's own area claim, which is where every scan was
    # before the segmenter was connected. Carried out so the bench can score the
    # two apart rather than reporting one blended number nobody can act on.
    measured_area_used: float | None = None


def _word(text: str, word: str) -> bool:
    """Is `word` present as a whole word? "roll" must not match "rolled oats"."""
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _classify_shape(name: str, hint: str | None) -> str:
    """Which physical shape prior applies to this food.

    STRUCTURE beats the model's hint; everything else defers to it.

    The model picks from six words -- flat, mound, loose, cluster, liquid,
    wrapped -- and for some foods none of them is the answer. Asked about two
    open tacos it said "flat", which is not unreasonable and is still wrong:
    "flat" is the prior for a steak, a slab of near-uniform height. Measured,
    that put a 133 g taco meal at 233 g, and no constant in the flat row fixes
    it, because a taco is not a slab and the model has no word for what it is.
    So when the NAME identifies a structure -- an open flatbread, a wrap, a
    roll -- that beats whatever shape word came back, and the hint decides only
    the cases where the name says nothing structural.

    Below that, this keyword fallback runs and its coverage matters: anything
    it misses lands on "default", a middling prior that is wrong for most
    specific foods. Auditing the foods actually photographed in testing, five
    of eight fell through, so the lists were extended to cover ordinary plates.

    Order is deliberate, and getting it wrong is easy. A structural word beats
    an ingredient word: "cheesesteak sandwich" is a sandwich, not a steak, and
    "falafel wrap" is a wrap, not a scatter of falafel. Both were misclassified
    when the ingredient lists were tested first.
    """
    # The DISH, not everything trailing after it. Matching the whole label let
    # a side or a sauce capture the shape: "grilled chicken with mashed potato
    # and gravy" matched "gravy" and became liquid -- a 25 mm height, a 0.98
    # profile, and the spread correction disabled -- for a chicken breast.
    # density_for already reads dish_head for exactly this reason.
    n = dish_head(name if isinstance(name, str) else "")

    # 1a. Open flatbread with something on it. Checked BEFORE the closed
    # shapes, because "taco" used to match the wrapped list and a taco is
    # nothing like a burrito -- and before the model's hint, because the model
    # cannot report this shape at all.
    #
    # Measured: two breakfast tacos weighing 133 g on a kitchen scale, with the
    # footprint measured off the photograph at 22.7% of a frame whose scale
    # came from a credit card in the shot. That needs height x profile x
    # density near 6.9. As "wrapped" -- 42 mm tall, 0.85 profile, built for a
    # burrito -- the estimator was using 36.8, and the meal read +88%.
    #
    # The numbers below were not solved backwards from that. A corn tortilla is
    # ~2 mm and a filling mound stands ~14 mm above it, so 16 mm peak; and
    # since the filling covers part of the disc while the rest is bare bread,
    # mean-to-peak is well under half. Those give 7.2, which lands near the
    # 6.9 the scale asks for -- corroboration, not a fit.
    #
    # A closed sandwich stays "wrapped": two slices and a filling really is a
    # tall object. It is the OPEN ones that are flat.
    if any(k in n for k in ("taco", "tostada", "pizza slice", "slice of pizza",
                            "open-faced", "open faced", "bruschetta", "crostini",
                            "avocado toast", "nachos")):
        return "topped_flat"

    # 1a-ii. Whole solid pieces, not a spread or a pile of grains. A skewer of
    # beef cubes is a stack of 40 mm objects; every height prior below was
    # calibrated on food lying flat.
    # Bone-in poultry pieces belong here too, and this one was measured.
    #
    # A drumstick in mole, weighed at 146 g, photographed on a plate with a
    # credit card in frame: 130 x 56 mm, 4,600 mm2 of footprint, stable across
    # three darkness thresholds. That needs height x profile x density of 31.7.
    # It was classifying as "flat" -- the prior for a steak, an 18 mm slab --
    # and using 17.0. A drumstick is a 30 mm thick solid object, not a slab.
    #
    # A chicken BREAST is a slab and stays flat; it is the bone-in pieces that
    # are chunky, which is also why they are named individually rather than by
    # matching "chicken".
    if any(k in n for k in ("kebab", "kabob", "skewer", "brochette", "satay",
                            "meatball", "albondiga", "nugget", "chunk", "cube",
                            "drumstick", "drumette")) \
            or _word(n, "wing") or _word(n, "wings") \
            or any(k in n for k in ("chicken leg", "turkey leg", "bone-in",
                                    "bone in")) \
            or ("potato" in n and any(k in n for k in
                ("roast", "baby", "new", "boiled", "whole", "halved"))):
        return "chunky"

    # 1b. Structure. How a food is assembled beats what is inside it.
    if any(k in n for k in ("burrito", "sandwich", "wrap", "sushi",
                            "sub ", "hoagie", "panini", "gyro", "shawarma",
                            "empanada", "samosa", "spring roll", "burger",
                            "hot dog", "calzone", "quesadilla")) or _word(n, "roll"):
        return "wrapped"

    # Structure said nothing. Now the model's own reading decides, and the
    # keyword lists below are the fallback for when it gave none.
    #
    # Normalised like every other label read back from a model: "Mound" was
    # dropped where "mound" was honoured. And "default" is a fallback key in
    # the tables, not a shape the model may claim -- accepting it
    # short-circuited the whole keyword ladder and handed rice a generic prior.
    hint_key = _key(hint)
    if hint_key and hint_key != "default" and hint_key in SHAPE_FACTORS:
        return hint_key

    # 2. Anything served swimming in liquid, however solid its ingredients.
    if any(k in n for k in ("soup", "broth", "smoothie", "sauce", "stew", "curry",
                            "chili", "gravy", "porridge", "congee", "dal",
                            "gazpacho", "bisque", "chowder", "yogurt", "pudding",
                            "custard", "masala", "tikka", "korma", "pho",
                            # Soups this list did not know were soups. "beef
                            # posole" came back as shape "default" on the bench
                            # and was sized as food on a plate -- it is a bowl
                            # of broth. Ramen sat in rule 3 with the noodles,
                            # which is what it is made of, not what it is.
                            "posole", "pozole", "menudo", "minestrone", "ramen",
                            "miso", "bouillon", "consomme", "consommé",
                            "cioppino", "bouillabaisse", "avgolemono")):
        return "liquid"

    # 3. Long thin food that piles loosely and settles flat.
    if any(k in n for k in ("noodle", "pasta", "spaghetti", "linguine",
                            "fettuccine", "ramen", "udon", "vermicelli", "fideo",
                            "macaroni", "penne", "orzo", "chow mein", "lo mein",
                            "salad", "greens", "lettuce", "fries", "slaw",
                            "sprouts", "shred", "spinach", "kale", "arugula",
                            "cabbage", "herbs", "cereal", "granola", "chips",
                            "crisps", "popcorn")):
        return "loose"

    # 4. Slabs and single solid pieces of protein or bread.
    if any(k in n for k in ("steak", "fillet", "filet", "breast", "pizza",
                            "pancake", "tortilla", "toast", "waffle", "omelet",
                            "omelette", "chop", "cutlet", "crepe", "flatbread",
                            "naan", "pita", "bacon", "fish", "salmon", "tuna",
                            "tofu", "lasagna", "frittata", "cornbread",
                            "drumstick", "thigh", "wing", "sausage", "rib")):
        return "flat"

    # 5. Soft food that holds a heap.
    if any(k in n for k in ("rice", "mash", "puree", "oatmeal", "couscous",
                            "quinoa", "grits", "beans", "lentil", "hummus",
                            "polenta", "risotto", "scrambled", "egg", "stuffing",
                            "casserole", "curd", "cottage", "guacamole",
                            "refried", "grain", "barley", "buckwheat", "millet",
                            "farro")):
        return "mound"

    # 6. Many small separate pieces with air between them.
    if any(k in n for k in ("broccoli", "berries", "grapes", "nuts", "peas",
                            "corn", "olives", "cauliflower", "brussels", "carrot",
                            "tomato", "shrimp", "prawn", "meatball", "dumpling",
                            "gnocchi", "falafel", "nugget", "cherry", "edamame")):
        return "cluster"

    return "default"


def reference_width_mm(hint: GeometryHint) -> float | None:
    """Real-world width of the thing we are measuring against, in mm."""
    if hint.plate_diameter_mm:
        return hint.plate_diameter_mm
    key = _key(hint.vessel)
    return VESSEL_WIDTH_MM.get(key) if key else None


# Words that separate a dish from what is in it or on it.
#
# Deliberately NOT " and ": "steak and cheese sandwich" is a sandwich, and
# cutting at "and" would leave the head as "steak" and make it worse. These are
# the words that mean "what follows is an ingredient, a sauce or a topping".
DISH_SEPARATORS = (
    " with ", " in ", " on ", " over ", " topped ", " smothered ",
    " covered ", " served ", ",",
)


def dish_head(name: str) -> str:
    """The dish itself, without the ingredients trailing after it.

    "taco with scrambled eggs and vegetables" -> "taco"
    "chicken drumstick in mole"               -> "chicken drumstick"
    "refried beans"                           -> "refried beans"
    """
    # Guarded like every other label read back from a model: a number or a
    # null in a name field is not a crash worth a user's scan.
    n = name.lower() if isinstance(name, str) else ""
    cut = len(n)
    for sep in DISH_SEPARATORS:
        i = n.find(sep)
        if i != -1:
            cut = min(cut, i)
    return n[:cut].strip()


# Published densities are LOADED but NOT APPLIED to the weight path.
#
# Density sits in the weight formula -- grams = area x height x profile x
# density -- so swapping seven of the eleven bench foods' densities in one go
# would move the weight numbers on a pipeline that took days to stabilise, with
# no bench run to say which way. The reference folder's job is macros; the
# weight system has its own evidence and its own slow, measured changes.
#
# The values are good and they are cited. They are held here rather than
# deleted, behind a switch, so adopting them is a decision somebody makes on
# purpose with a bench result in hand -- not a side effect of adding a data
# file.
#
# ANSWERED, 2026-09-10, and the answer is no. Keeping it off.
#
# The decision this comment asked for has now been made against a kitchen
# scale rather than a bench run -- ten of the user's weighed plates, measured
# footprints, leave-one-out, offline. Applying the sourced densities:
#
#     density ignored (rho = 1)   mean absolute 14.4%   worst 34.1%
#     density applied             mean absolute 20.9%   worst 79.8%
#
# and, the sharper test, the spread of implied HEIGHTS went from 2.3x to 8.8x
# (coefficient of variation 25% -> 92%). A density term that is describing
# something real removes variation from the height; this one added it.
#
# The mechanism is visible in one row. Caesar salad is listed at 0.22 g/ml,
# which is the density of loose leaves in a bowl. Multiplied through a measured
# footprint it demands an 86 mm pile on a flat plate -- three and a half inches
# of salad -- because footprint x height is the ENVELOPE volume, and the air
# between the leaves is already inside it. A bulk density and an envelope
# volume count the same air twice.
#
# Six of the ten foods came back at exactly 0.85, which is a food-group
# default, not a measurement -- so most of what the table would change is not
# specific to the food anyway.
#
# The values stay loaded for macros, where they belong and where they are
# right. To revisit: the test is not "does the error drop" but "does the
# spread of implied heights narrow" -- that is what distinguishes a real
# density term from error moved around. /tmp scripts are gone; the method is
# ten weighed plates, footprint from food_on_plate, height = grams / (area x
# rho), leave-one-out on the median.
USE_SOURCED_DENSITIES = False

try:
    from ..nutrition.references import load_densities as _load_densities

    _SOURCED = _load_densities()
    if USE_SOURCED_DENSITIES:
        DENSITY_G_ML.update(_SOURCED)
except Exception:  # noqa: BLE001
    # A data file must never stop the app starting.
    _SOURCED = {}


def density_for(
    name: str,
    explicit: float | None = None,
    group: str | None = None,
) -> float:
    """Density in g/ml for a food name, most specific source first.

    Precedence, and it matters:

      1. ``explicit``  a real measured density from the nutrition database
      2. dish match    the dish itself, in the table below
      3. ``group``     the coarse food-group density
      4. full-name match, only when no group was reported at all
      5. default

    The name is matched against the DISH, not the whole label. A composite
    dish does not have one ingredient's density, and matching anywhere in the
    string meant it inherited one anyway:

        "taco with scrambled eggs"    matched "egg"     -> 1.03 g/ml
        "baked spaghetti with cheese" matched "cheese"  -> cheese
        "chicken drumstick in mole"   matched "mole"

    1.03 is denser than water, for a food that is corn tortilla and fluffy egg.
    Measured on a weighed taco meal, that alone was most of a 2x error in
    height x profile x density. Whatever follows "with" or "in" is a filling or
    a sauce; it is not what the dish weighs per millilitre. So when the dish
    itself is not in the table, the food GROUP answers -- "composite" is an
    honest mixture density -- rather than whichever ingredient happened to be
    named.

    This used to be 1 -> 4 with the group density passed in AS ``explicit``,
    which meant the name table was never consulted from a live scan at all.
    "refried beans" therefore kept the legume group's 0.78 -- the density of
    whole beans sitting in broth -- rather than the 1.05 of a smooth paste.

    The LONGEST matching key wins, not the first. Order-of-insertion matching
    let a generic key silently shadow a specific one: "refried beans" matched
    "beans" (0.72, whole beans in liquid) instead of "refried" (1.05, a paste),
    and the estimate came out about 20% light on every meal containing them.

    Longest-match also makes the table safe to extend -- adding a specific food
    can no longer be defeated by where it happens to sit in the dict.
    """
    if explicit and 0.05 < explicit < 3.0:
        return explicit
    def longest_match(text: str) -> float | None:
        # The LONGEST matching key wins, not the first. Order-of-insertion
        # matching let a generic key shadow a specific one.
        best_key, best_val = "", None
        for key, val in DENSITY_G_ML.items():
            if key != "default" and key in text and len(key) > len(best_key):
                best_key, best_val = key, val
        return best_val

    from_dish = longest_match(dish_head(name))
    if from_dish is not None:
        return from_dish
    if group:
        return group_density(group)
    # No group reported: a match anywhere in the name still beats the global
    # default, even though it may be an ingredient rather than the dish.
    from_anywhere = longest_match(name.lower() if isinstance(name, str) else "")
    if from_anywhere is not None:
        return from_anywhere
    return DENSITY_G_ML["default"]


def _visible_or_floor(value) -> float:
    """How much of this food can be seen, as a fraction, never below the floor.

    Unreported or unusable means fully visible -- no correction. A number below
    the floor means almost nothing is visible, which is a claim about the photo
    and must not be rounded up to "no occlusion".
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    if v != v or v <= 0.0 or v > 1.0:
        return 1.0
    return max(0.05, v)


# How far past the plate a measured footprint may reach before it is refused.
#
# Not 1.0. Food genuinely overhangs a plate -- a chop, a slice of toast, a taco
# shell resting on the rim -- and the plate ratio it is being compared against
# is itself the model's bounding-box ellipse, which is quantised to a 0.05 grid.
# Refusing at exactly the plate would throw away good measurements to defend a
# number that is not itself measured.
#
# Above this the mask has leaked onto the tablecloth, and a leaked mask is the
# one failure mode that produces a large, confident, wrong weight.
MEASURED_OVER_PLATE_LIMIT = 1.15

# AND HOW MUCH SMALLER THAN ITS OWN BOX A FOOTPRINT MAY BE.
#
# The guard above catches a mask that LEAKED -- one bigger than the plate. The
# opposite failure had nothing watching it: a mask that found a FRAGMENT. On
# the weighed caesar salad the segmenter returned 4.3% of the frame against a
# bounding box of 25% -- it had segmented croutons and parmesan, not the salad
# -- and that footprint was believed, published as 28 g against 123 g weighed,
# and was the single worst item in the bench.
#
# Scattered food is legitimately much smaller than its box: eight baby carrots
# measured 4.8% inside a 16% box, 3.3x, and that measurement is correct. So
# the limit has to sit above that. Measured on this bench:
#
#     zucchini      1.1x     chicken   box < mask     macaroni  box < mask
#     carrots       3.3x     correct, scattered pieces
#     caesar        5.8x     a crouton mistaken for a salad
#
# 5.0 sits in the gap. It rests on ONE correct case at 3.3 and ONE wrong case
# at 5.8, which is thin, and it is deliberately set nearer the wrong one: a
# refused measurement costs a rung on the ladder, and a believed fragment costs
# the whole weight. Worth refitting when more weighed photographs exist.
MEASURED_UNDER_BOX_LIMIT = 5.0

# A BOX-RATIO GUARD WAS TRIED HERE AND IS REFUTED. Do not re-add it.
#
# The bench published 58 g of scattered baby carrots as 686 g: the segmenter
# returned a mask covering 34.9% of the frame against the detector's box at
# 16.0% -- 2.2x its own box -- and the plate limit above let it through. The
# obvious guard is "refuse a mask far larger than its own box".
#
# It cannot work, and this file already contains the evidence. The boxes were
# measured against a ruler and found SMALL: a real 4.84% footprint boxed at
# 4.00%, a real 4.50% boxed at 3.00%, and test_the_bounding_box_rail_is_not_
# applied_to_a_measurement pins a legitimate 8.5% measurement against a 4.0%
# box -- 2.1x. A threshold that refuses the carrots at 2.2x throws away a
# ruler-verified measurement at 2.1x. There is no line between them here,
# because the ratio is not what distinguishes them.
#
# The real fix is upstream, where the information is: a mask is chosen by
# which masks lie INSIDE the item's box (segment_hosted.segment_boxes), so the
# plate is never a candidate in the first place. Fix the selection, not the
# symptom.
def measured_area_plausible(measured, hint) -> tuple[bool, str]:
    """May this measured footprint be used as an area? And if not, why not.

    Returns the reason as well as the verdict because a refused measurement is
    the thing an operator most needs to see in the notes: it is the difference
    between "the segmenter is off" and "the segmenter is on and being ignored",
    and those look identical from the outside.
    """
    try:
        area = float(measured)
    except (TypeError, ValueError):
        return False, "not a number"
    if not (0.0 < area <= 1.0):
        return False, "not a fraction of the frame"
    plate = getattr(hint, "plate_ellipse_area_ratio", None) or 0.0
    if plate > 0 and area > plate * MEASURED_OVER_PLATE_LIMIT:
        return False, (
            f"{area:.0%} of the frame against a plate covering {plate:.0%} — "
            f"the mask has leaked past the plate"
        )
    return True, "ok"


def railed_area(area_ratio: float, bbox) -> float:
    """The area we will actually use for this item, after the bounding-box rail.

    Extracted so a caller can total a plate's food the same way the estimator
    does. Summing the RAW reported areas instead would total a set of numbers
    the estimator has already rejected -- measurement put them 2.4x to 4.3x
    over their own boxes -- and a plate-level correction built on that would
    inherit the whole error.
    """
    try:
        area = float(area_ratio)
    except (TypeError, ValueError):
        return 0.0
    if area <= 0:
        return 0.0
    box_ratio = _bbox_area_ratio(bbox)
    if box_ratio <= 0:
        return area
    # One answer, whatever the model claimed. How far apart its two numbers were
    # still matters -- it costs confidence and widens the band, both handled by
    # the caller -- but it no longer moves the estimate, because it is a fact
    # about the model, not about the food.
    ceiling = box_ratio * BBOX_FILL_CEILING * BOX_TIGHTNESS
    if 0 < ceiling < area:
        return ceiling
    return area


def _plausible(value, lo: float, hi: float, default: float) -> float:
    """Accept a client-reported number only if it could be real.

    Out-of-range values are replaced by the default rather than clamped to the
    nearest bound. A clamp converts nonsense into a plausible-looking figure
    the rest of the pipeline then trusts; the default is at least honest about
    being a default.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi else default


def mm2_per_frame(hint: GeometryHint) -> tuple[float | None, str]:
    """How many square millimetres does the whole camera frame cover?

    Returns ``(mm2, method)`` or ``(None, method)`` when we have no scale at all.
    """
    # Rung 1: an explicitly calibrated reference object.
    if hint.reference_area_mm2 and hint.plate_ellipse_area_ratio:
        return hint.reference_area_mm2 / max(hint.plate_ellipse_area_ratio, 1e-4), "plate_reference"

    # Rung 1b: a known plate diameter plus the plate's share of the frame.
    if hint.plate_diameter_mm and hint.plate_ellipse_area_ratio:
        plate_area = math.pi * (hint.plate_diameter_mm / 2.0) ** 2
        return plate_area / max(hint.plate_ellipse_area_ratio, 1e-4), "plate_reference"

    # Rung 2a: an object of known real size, found in the pixels.
    #
    # Ahead of depth deliberately, and the ladder already said so: the ceiling
    # table puts reference_object at 0.88 and depth_model at 0.84. The order
    # here contradicted it, so any phone reporting an ARKit distance silently
    # beat a measured credit card -- a 3.5x difference in frame area on the one
    # photo where both were available.
    #
    # This is what sizes the photos nothing else can: food on paper, in a
    # takeout container, on a bare table.
    # MEASURED DEBT: A CARD ON THE TABLE UNDER-SCALES ANYTHING RAISED.
    #
    # The card lies on the table. The food does not -- it sits on a plate's
    # inner surface, or inside a bowl, 15 to 80 mm nearer the lens. Nearer means
    # bigger in the frame, so a card at the table plane makes every raised thing
    # measure LARGER than it is, by D / (D - h) for camera distance D and height
    # h, and AREA goes as the square of that.
    #
    # Measured on two of this user's own vessels, photographed top-down with a
    # card in frame, against a tape:
    #
    #     vessel        tape      read off the card    over-read
    #     dinner plate  228.6 mm      240.0 mm            +5%   -> +10% of area
    #     soup crock    114.3 mm      148.4 mm           +30%   -> +69% of area
    #
    # Both are explained by one camera distance: solving D/(D-h) for each gives
    # a consistent D of roughly 300-400 mm with rim heights of ~17 mm and
    # ~80 mm, which is what those two vessels actually are. The effect is real,
    # it is large, and on a deep bowl it is the single biggest error in this
    # file.
    #
    # NOT CORRECTED HERE, on purpose. The correction needs h (the food's height
    # above the reference plane) and D (the camera distance), and D is optional
    # on the scan request today. Applying it with a guessed D would trade a
    # measurable bias for an unmeasurable one -- and this rung is the scale, so
    # it changes only with a weighed bench behind it.
    #
    # What to do when it is done: take D from `camera_distance_mm`, take h from
    # the calibrated vessel's own depth, and divide the frame width by
    # D / (D - h). Refuse the correction when D is absent rather than assuming
    # one, exactly as the depth rung refuses a top-down plate.
    #
    # WHICH RUNG THIS ACTUALLY AFFECTS -- narrowed 2026-09-10, after this note
    # was briefly over-read into "every plate weight is 16% over". It is not.
    #
    #   Rung 1b, plate_reference: NOT AFFECTED. The scale is the plate's own
    #   declared diameter against the plate's own share of the frame, and the
    #   food sits ON that plate. Reference and subject are the same plane, so
    #   the parallax cancels exactly. Every weighed plate on the bench runs on
    #   this rung, so none of those numbers carry this error.
    #
    #   Rung 2a, reference_object: AFFECTED, as described above. A card on the
    #   table sizing food that sits ~28 mm higher on a plate over-reads the
    #   area by about 10%. This is the rung for food on paper, in a takeout
    #   box, on a bare table -- the photographs nothing else can size.
    #
    #   A BOWL IS THE OPPOSITE SIGN, AND LARGER. When the declared diameter is
    #   a crock's RIM, the scale is calibrated at the rim -- but the soup
    #   surface sits well below it, FARTHER from the lens, so its true mm per
    #   pixel is larger than the rim's. Using the rim's value under-measures
    #   the food. At D = 406 mm with a 94 mm rim and the surface 40 mm down,
    #   the area comes out at (312/352)^2 = 0.79 of true -- soup weights ~21%
    #   LOW, where a card on a table makes a plate ~10% high.
    #
    # So there are two corrections here, not one, with opposite signs, and
    # neither is applied. Both need a ruler on the vessel, which is cheap, and
    # a weighed bench to confirm, which is not.
    if hint.reference_frame_width_mm:
        width_mm = hint.reference_frame_width_mm
        if REFERENCE_MIN_FRAME_MM <= width_mm <= REFERENCE_MAX_FRAME_MM:
            aspect = _plausible(hint.aspect_ratio, 0.5, 2.5, DEFAULT_ASPECT_RATIO)
            return width_mm * (width_mm / aspect), "reference_object"

    # Rung 2: depth. Given the camera-to-subject distance and the lens's field
    # of view, the frame's real-world size is trigonometry, not a prior:
    #
    #     frame_width = 2 * distance * tan(fov / 2)
    #
    # The field of view has to come from the device. This used to be hardcoded
    # at 65 degrees, which is wrong for most modern phones -- an iPhone main
    # camera is nearer 70, and since area goes as the square of width, a 5
    # degree error is roughly 16% of the answer. Every phone can report its
    # own, so ask for it and only fall back to a default when it is missing.
    if hint.depth_mm:
        # Implausible optics are ignored, not clamped into range. Clamping a
        # 5 degree reading to 30 turns obvious junk into a confident wrong
        # answer; falling back to the default at least fails the way an
        # unreported value does.
        fov = _plausible(hint.camera_fov_deg, 40.0, 100.0, DEFAULT_CAMERA_FOV_DEG)
        aspect = _plausible(hint.aspect_ratio, 0.5, 2.5, DEFAULT_ASPECT_RATIO)

        # The quoted field of view spans the LONG axis of the sensor, not the
        # image width. Those are the same thing only in landscape.
        #
        # This was measured, not reasoned. One meal photographed from 12-14
        # inches, portrait, with a credit card in the frame: the card puts the
        # frame at 319 mm across. The old line -- width = 2 d tan(fov/2) with
        # fov 68 -- computes 445 mm. Frame AREA goes as the square of width, so
        # that is roughly 95% too much food, on every photo taken upright by a
        # phone that reports its distance.
        #
        # Deriving the long side first and the short side from the aspect ratio
        # gives 334 mm against the card's measured 319 -- inside 5%, which is
        # about as close as a nominal lens figure gets to a real one.
        long_side = 2.0 * hint.depth_mm * math.tan(math.radians(fov) / 2.0)
        if aspect >= 1.0:            # landscape: the width IS the long axis
            w = long_side
            h = w / aspect
        else:                        # portrait: the height is
            h = long_side
            w = h * aspect
        return w * h, "depth_model"

    # Rung 3b: the vision model told us what the food is served on. A named
    # vessel of known typical size is a real reference -- weaker than a
    # measured one, far better than assuming everything is a dinner plate.
    area, _certainty = vessel_area(hint.vessel, hint.vessel_shape)
    if area and hint.plate_ellipse_area_ratio:
        return area / max(hint.plate_ellipse_area_ratio, 1e-4), "vessel_reference"

    # If the model named a surface with no standard size -- paper, foil, a hand,
    # a bare table -- then there is no scale in this photo. Say so and fall
    # through to the serving prior rather than inventing a plate.
    if hint.vessel and vessel_area(hint.vessel, hint.vessel_shape)[0] is None:
        if _key(hint.vessel) in VESSEL_NOT_A_REFERENCE:
            return None, "ai_prior"

    # A named vessel we have no size for is not a reference either. The escape
    # below only caught the explicit not-a-reference words, so "ramekin" or
    # "wooden_board" fell through to the 270 mm dinner plate -- the exact
    # failure the vessel table was introduced to end.
    if hint.vessel and _key(hint.vessel) not in VESSEL_WIDTH_MM:
        return None, "ai_prior"

    # Rung 4: a dish was detected but not identified. Assume a standard plate.
    if hint.plate_ellipse_area_ratio:
        plate_area = math.pi * (DEFAULT_PLATE_DIAMETER_MM / 2.0) ** 2
        return plate_area / max(hint.plate_ellipse_area_ratio, 1e-4), "pixel_area"

    return None, "ai_prior"


# Confidence ceiling per rung — an estimate can never be more certain than the
# scale it was built on.
_METHOD_CEILING = {
    "plate_reference": 0.92,
    "depth_model": 0.84,
    "multi_image": 0.86,
    "vessel_reference": 0.80,
    # Above a vessel prior, below a plate the user measured: the card's size is
    # exact and it is found in the pixels rather than described, but it is a
    # small object and its scale carried ~7% on the one meal we have measured.
    "reference_object": 0.88,
    "pixel_area": 0.70,
    "ai_prior": 0.52,
}
# Half-width of the reported band, as a fraction of the estimate.
_METHOD_BAND = {
    "plate_reference": 0.14,
    "depth_model": 0.20,
    "multi_image": 0.18,
    "vessel_reference": 0.22,
    "reference_object": 0.16,
    "pixel_area": 0.30,
    "ai_prior": 0.45,
}


def normalize_bbox(bbox) -> dict | None:
    """Coerce whatever the model sent into {"x","y","w","h"}, or None.

    The prompt asks for an object. Models return a bare [x, y, w, h] list often
    enough, sometimes `width`/`height` keys, sometimes numeric strings. Every
    consumer downstream expects a dict -- including the pydantic DetectedItem,
    where a list raises ValidationError and surfaces to the user as a bare 500
    on an otherwise perfectly good photo.

    Normalising once at the boundary is the fix. A schema in a prompt is a
    request, not a guarantee, and every field read back from a model has to be
    treated that way.
    """
    if bbox is None:
        return None
    try:
        if isinstance(bbox, dict):
            x = bbox.get("x", 0)
            y = bbox.get("y", 0)
            w = bbox.get("w", bbox.get("width", 0))
            h = bbox.get("h", bbox.get("height", 0))
        elif isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            x, y, w, h = bbox[0], bbox[1], bbox[2], bbox[3]
        else:
            return None
        out = {"x": float(x or 0), "y": float(y or 0),
               "w": float(w or 0), "h": float(h or 0)}
    except (TypeError, ValueError):
        return None
    if out["w"] <= 0 or out["h"] <= 0:
        return None
    return out


def _bbox_area_ratio(bbox) -> float:
    """Fraction of the frame a detection's bounding box covers.

    Accepts the documented object form and the list form models drift into,
    and returns 0.0 for anything it cannot read. Returning 0 disables the
    ceiling for that item, which is the safe direction: the rail exists to
    catch impossible claims, so a box we cannot parse should simply not
    constrain anything rather than fail the scan.
    """
    box = normalize_bbox(bbox)
    if box is None:
        return 0.0
    area = box["w"] * box["h"]
    # Ratios are fractions of the frame. Anything outside that is not a ratio.
    return area if 0.0 < area <= 1.0 else 0.0


def estimate_grams(
    *,
    name: str,
    area_ratio: float,
    hint: GeometryHint,
    plate_coverage: float | None = None,
    shape_hint: str | None = None,
    height_ratio: float | None = None,
    bbox: dict | None = None,
    visible_fraction: float | None = None,
    # How much of the plate ALL the food on it covers, after the bounding-box
    # rail. Optional: a single-item photo can leave it out and gets the same
    # answer, because there the item's coverage IS the plate's.
    plate_food_coverage: float | None = None,
    density: float | None = None,
    food_group: str | None = None,
    ai_prior_grams: float | None = None,
    detection_confidence: float = 0.7,
    # Heights that corrections have earned, loaded once per scan by the caller.
    # Passed in rather than fetched here so this function stays pure and
    # testable, and so one photo costs one query instead of one per food.
    learned_heights: dict | None = None,
    # The food's MEAN height in mm, measured from a depth map. When present it
    # replaces the prior AND the profile factor -- see below for why both.
    measured_height_mm: float | None = None,
    # The food's FOOTPRINT as a fraction of the frame, measured off the pixels
    # by a point-prompted segmenter. When present and plausible it replaces both
    # the model's area claim and its plate-coverage claim, and the bounding-box
    # rail is skipped -- see below for why the rail must not apply to it.
    #
    # The caller must pass this ONLY for a source whose mask is independent of
    # the plate box; `food_seg.area_is_absolute` is where that is decided. A
    # colour-grown footprint is a share, not an area, and passing one here would
    # feed the plate box's error back in wearing a measurement's clothes.
    measured_area_ratio: float | None = None,
    # The largest connected piece as a fraction of that measured footprint.
    # 1.0 is one connected mass, about 0.5 is separate pieces. Only meaningful
    # alongside measured_area_ratio, and ignored without it.
    largest_piece_share: float | None = None,
) -> PortionEstimate:
    """Convert one detection's frame-area share into grams.

    ``area_ratio`` is the food's share of the whole image (0..1).
    ``plate_coverage`` is its share of the VESSEL'S surface, which is the same
    quantity asked a better way and is preferred whenever both are available.

    Measured on a weighed plate: the model put the plate itself at 50% of frame
    against a photographed 53.1% — within 6% — while calling a rice mound 15% of
    frame against a photographed 4.2%, 3.5x too large. Judging a small region
    against one big salient object is a far easier task than judging it against
    the whole frame, and the numbers say the model can do the first and not the
    second.
    """
    notes: list[str] = []
    occluded_by = 1.0
    shape = _classify_shape(name, shape_hint)
    frame_mm2, method = mm2_per_frame(hint)

    # Prefer coverage-of-the-plate over share-of-the-frame. Both describe the
    # same area; the plate-relative one is simply the question the model can
    # answer. Converting here means everything downstream — the bbox rail, the
    # spread correction, the blend — is unchanged and still sees one number.
    # Kept because area_ratio is about to be overwritten and the notes below
    # were printing the OVERWRITTEN value under the words "reported area".
    #
    # Four nights were spent tuning the rail on the strength of those notes.
    # They read like the model contradicting itself -- "reported area 18% of
    # frame and its own bounding box 9% disagree by 2.4x" -- when the model had
    # actually said 12%, and 18% was coverage x plate_ratio, a number it never
    # reported. The instrument was describing its own arithmetic.
    reported_area_ratio = area_ratio
    coverage_used = False

    # A MEASURED footprint outranks both of the model's answers.
    #
    # Both of those are claims about size, and this file exists because size is
    # the thing the model cannot do: measured against a ruler on one weighed
    # 146 g chicken leg, boxed at 4.00% of frame against a real 4.84%, and at
    # 3.00% against 4.50%. Not inaccurate -- impossible, since food cannot be
    # smaller than its own box. The area claim is worse: over its own box on 20
    # of 22 bench items, by 2x to 6x.
    #
    # WHY THE FORESHORTENING DOES NOT NEED CORRECTING. This is a projected area
    # in an angled photograph, so it understates the true horizontal footprint
    # by cos(tilt) -- and the frame scale it is about to be multiplied by was
    # derived from the plate's projected ellipse against the plate's known true
    # area, so it overstates by exactly 1/cos(tilt). Food and plate lie in the
    # same plane and share the angle, so the two cancel and the product is the
    # true horizontal area. That cancellation is why the footprint can be used
    # raw here while the depth HEIGHT needs the tilt derivation in depth_map.
    measured_used = False
    # Whether the model's bounding box may cap this area. A measurement and a
    # known vessel are both exempt, for the same reason: the box is a claim
    # about a thing we have already sized by other means.
    rail_exempt = False
    if measured_area_ratio is not None:
        ok, why = measured_area_plausible(measured_area_ratio, hint)
        # A footprint far smaller than the item's own box is a fragment of the
        # food, not the food. See MEASURED_UNDER_BOX_LIMIT.
        own_box = _bbox_area_ratio(bbox)
        if ok and own_box > 0:
            shrink = own_box / max(float(measured_area_ratio), 1e-9)
            if shrink > MEASURED_UNDER_BOX_LIMIT:
                ok = False
                why = (f"{float(measured_area_ratio):.1%} of the frame inside a "
                       f"box of {own_box:.0%} — {shrink:.1f}x smaller than "
                       f"the food it is meant to be, so the mask found a piece "
                       f"of it rather than the whole")
        if ok:
            measured_used = True
            rail_exempt = True
            area_ratio = float(measured_area_ratio)
            notes.append(
                f"{name}: footprint measured from the photo "
                f"({area_ratio:.1%} of the frame) rather than estimated."
            )
        else:
            notes.append(
                f"{name}: the measured footprint was refused — {why}. Sized "
                f"from the model's own estimate instead."
            )

    # LIQUID IN A WALLED VESSEL: THE AREA IS THE VESSEL, NOT THE FOOD.
    #
    # Everything else in this function asks "how much of the frame does this
    # food cover". For soup that question has no answer worth having -- the
    # surface is the vessel's mouth, and any attempt to find its edges in the
    # pixels finds noodles, or a reflection, or nothing. The vessel's size is
    # already known: the user calibrated it, or told us, and it arrives as
    # plate_ellipse_area_ratio.
    #
    # Refused when the vessel's share of the frame is unknown, rather than
    # guessed -- as everywhere else on this ladder.
    soup_in_a_bowl = (
        shape == "liquid"
        and (_key(hint.vessel) in WALLED_VESSELS or hint.vessel is None)
        and bool(hint.plate_ellipse_area_ratio)
    )
    if soup_in_a_bowl:
        area_ratio = float(hint.plate_ellipse_area_ratio)
        # NOT measured_used. That flag means "a segmenter measured this
        # footprint and it set the weight", and it is reported as such to the
        # caller and the bench. A bowl is not a measured footprint; it is a
        # known vessel. Overloading the flag crashed the reporting line
        # immediately, which is the correct response to giving one name two
        # meanings.
        rail_exempt = True
        notes.append(
            f"{name}: sized from the bowl rather than from the liquid's outline "
            f"— a surface has no edges to measure, and the vessel's do."
        )
        # ...but only if that ellipse is bowl-sized. See MAX_BOWL_MOUTH_MM.
        if frame_mm2:
            mouth_mm2 = frame_mm2 * area_ratio
            if mouth_mm2 > MAX_BOWL_MOUTH_MM2:
                area_ratio = MAX_BOWL_MOUTH_MM2 / frame_mm2
                detection_confidence = min(detection_confidence, 0.5)
                notes.append(
                    f"{name}: the round shape found here is "
                    f"{2.0 * math.sqrt(mouth_mm2 / math.pi):.0f} mm across, which "
                    f"is a plate rather than a bowl — so this is the largest bowl "
                    f"of soup that is real, not a measurement of yours. Worth "
                    f"correcting by hand."
                )

    if (
        not soup_in_a_bowl
        and not measured_used
        and plate_coverage
        and 0 < plate_coverage <= 1.0
        and hint.plate_ellipse_area_ratio
    ):
        coverage_used = True
        from_plate = plate_coverage * hint.plate_ellipse_area_ratio
        # Only speak up when the two disagree enough to matter, so the notes
        # stay about the food rather than about our own bookkeeping.
        if area_ratio > 0 and from_plate < area_ratio / 1.5:
            notes.append(
                f"Sized from how much of the plate it covers "
                f"({plate_coverage:.0%}) rather than its share of the photo — "
                f"the plate is the more reliable yardstick."
            )
        area_ratio = from_plate
    if method == "vessel_reference":
        _area, certainty = vessel_area(hint.vessel, hint.vessel_shape)
        detection_confidence *= certainty
        if certainty < 0.85:
            notes.append(
                f"Scale taken from a {hint.vessel.replace('_', ' ')}, whose size "
                f"varies more than a dinner plate — the range is wider to match."
            )
    if method == "reference_object":
        detection_confidence *= REFERENCE_CERTAINTY.get(
            _key(hint.reference_kind), DEFAULT_REFERENCE_CERTAINTY
        )
        notes.append(
            f"Scale measured from the {_key(hint.reference_kind).replace('_', ' ')} "
            f"found in the photo — it makes the frame about "
            f"{hint.reference_frame_width_mm:.0f} mm across."
        )
    elif hint.reference_frame_width_mm:
        kind = _key(hint.reference_kind).replace("_", " ") or "reference object"
        width = hint.reference_frame_width_mm
        if REFERENCE_MIN_FRAME_MM <= width <= REFERENCE_MAX_FRAME_MM:
            # Perfectly good, just outranked. This note used to claim the card
            # had been rejected for implying an impossible frame -- and printed
            # a figure that was plainly inside the range it named. Being told
            # your reference was thrown away when it was merely not needed is
            # worse than being told nothing.
            notes.append(
                f"A {kind} was found and agreed the frame is about "
                f"{width:.0f} mm across, but the measured plate was used "
                f"instead — it is the larger object, so its outline is measured "
                f"over more pixels."
            )
        else:
            notes.append(
                f"A {kind} was found, but it implies a photo {width:.0f} mm "
                f"across — outside the {REFERENCE_MIN_FRAME_MM:.0f}-"
                f"{REFERENCE_MAX_FRAME_MM:.0f} mm a phone can take of a meal, "
                f"so it was not used for scale."
            )

    rail_spread = 1.0
    dens = density_for(name, density, food_group)
    # A SOUP IS MOSTLY WATER, WHATEVER IS FLOATING IN IT.
    #
    # density_for matches the most specific token it finds, and in "broccoli
    # cheese soup" that is "broccoli" -- 0.35 g/ml, the density of dry florets
    # in a colander. Measured on the bench: the same bowl came out at 52.5 g
    # against 152 g weighed, a threefold error from one word. Chicken noodle,
    # whose name happens to contain no vegetable, was fine at 1.05.
    #
    # Broth is 1.00; a thick chowder with cream and potato reaches about 1.10;
    # nothing served as soup is under 0.90. Clamping, not replacing, so a
    # genuinely measured density from the nutrition database still moves inside
    # the range.
    if shape == "liquid":
        clamped = max(LIQUID_DENSITY_MIN, min(LIQUID_DENSITY_MAX, dens))
        if abs(clamped - dens) > 1e-6:
            notes.append(
                f"{name}: density {dens:.2f} came from an ingredient rather than "
                f"the dish — a bowl of soup is mostly water, so {clamped:.2f} "
                f"was used."
            )
        dens = clamped
    # The prior, unless corrections have earned a better number for this shape
    # or this specific food.
    #
    # This lookup did not exist until an audit went looking for a constant
    # nothing referenced. portion_learning wrote a height on every correction,
    # logged it, and no one read it back -- the estimator took HEIGHT_PRIORS_MM
    # unconditionally. A learning loop that reports progress and changes nothing
    # is worse than none, because the user is told their corrections matter.
    # WHICH HEIGHT TABLE, AND WHY IT DEPENDS ON WHERE THE AREA CAME FROM.
    #
    # HEIGHT_PRIORS_MM is calibrated against the RAILED area -- the model's
    # box, capped -- which is smaller than the real footprint. Multiplying a
    # true measured footprint by those heights double-counts and comes out
    # heavy. food_seg.MEASURED_HEIGHTS_MM is the pair for a measured area,
    # solved on weighed plates and validated leave-one-out at 10.3% mean
    # absolute against 22.4% for the priors on the same bench.
    #
    # That table, and the function beside it, were built, calibrated, tested
    # and never called from app/ -- the estimator took the priors whatever the
    # area was. Measured on the two live SAM2 footprints from tonight's bench:
    #
    #     baby carrots      136.0 g -> 63.3 g   against 58 g weighed
    #     sliced zucchini    96.4 g -> 84.7 g   against 74 g weighed
    #
    # i.e. +134.5% -> +9.1% and +30.3% -> +14.5%, by choosing the table that
    # matches the measurement. Swapping these into the railed path would make
    # every estimate lighter and the bench worse; the pair only works together,
    # which is why this is conditional and not a replacement.
    if soup_in_a_bowl:
        base_height = SOUP_DEPTH_MM
    elif measured_used and largest_piece_share is not None:
        from .food_seg import ONE_PIECE_SHARE
        one_mass = float(largest_piece_share) >= ONE_PIECE_SHARE
        base_height = (CONNECTED_PILE_HEIGHT_MM if one_mass
                       else SEPARATE_PIECES_HEIGHT_MM)
        if not one_mass:
            notes.append(
                f"{name}: measured as separate pieces rather than one mass, so "
                f"it is a single layer — pieces on a plate cannot stack."
            )
    elif measured_used:
        from .food_seg import MEASURED_HEIGHTS_MM
        base_height = MEASURED_HEIGHTS_MM.get(shape, MEASURED_HEIGHTS_MM["default"])
    else:
        base_height = HEIGHT_PRIORS_MM[shape]
    height_mm, height_source = portion_learning.height_for(
        shape, name, learned_heights, base_height
    )
    # A depth map beats both the prior and anything learned from corrections,
    # because it is a measurement of THIS plate rather than a statement about
    # food in general. Bounded here as well as at the source: a height that
    # reached this point implausible would be a wrong meal, and the check costs
    # nothing.
    depth_measured = False
    if measured_height_mm is not None:
        try:
            candidate = float(measured_height_mm)
        except (TypeError, ValueError):
            candidate = 0.0
        if HEIGHT_MM_MIN <= candidate <= HEIGHT_MM_MAX:
            height_mm, height_source, depth_measured = candidate, "depth_map", True
    if height_source == "depth_map":
        notes.append(
            f"{name}: height measured from the photo's depth — {height_mm:.0f} mm "
            f"rather than the usual {HEIGHT_PRIORS_MM[shape]:.0f} mm for "
            f"something {shape}."
        )
    elif height_source != "prior":
        notes.append(
            f"{name}: sized with a height learned from corrections "
            f"({height_mm:.0f} mm rather than the usual "
            f"{HEIGHT_PRIORS_MM[shape]:.0f} mm)."
        )
    depth_factor = 1.0

    if frame_mm2 is None:
        # No geometry at all: trust the model's serving-size prior, or a
        # conservative default so the meal is still loggable.
        grams = float(ai_prior_grams or 150.0)
        notes.append("No plate or depth reference found — used a typical-serving estimate.")
    else:
        # Physical sanity, part one: a shape cannot cover more area than its own
        # bounding box. This is geometry, not a prior -- if the model says an
        # item fills 30% of the frame while boxing it at 15%, one of the two is
        # wrong and only the box has a hard ceiling.
        #
        # It matters most on plates holding several foods, where measurement
        # showed area fractions over-reported by 2.4x to 4.3x: a drumstick
        # claimed at 120x120 mm, a smear of refried beans at 99x99. Bounding
        # boxes are linear judgements, which vision models make far better than
        # area fractions, so the box is the more trustworthy of the two.
        #
        # The fill factor is how much of its box a real food occupies: nothing
        # fills a rectangle, and a drumstick is closer to half of one.
        # Be liberal about the shape of this. The model is asked for
        # {"x":..,"y":..,"w":..,"h":..} but returns a bare [x,y,w,h] list often
        # enough, and null values sometimes. Anything unreadable simply means
        # no ceiling -- a malformed box must never cost the user their scan.
        box_ratio = _bbox_area_ratio(bbox)
        railed = railed_area(area_ratio, bbox)
        # THE RAIL DOES NOT APPLY TO A MEASUREMENT, and this is the single most
        # important line in the wiring.
        #
        # The rail caps the area at a fraction of the model's own box. That is
        # right for a claim, because a claim cannot beat geometry. It is exactly
        # wrong for a measurement, because the boxes were measured against a
        # ruler and found SMALL: a real 4.84% footprint boxed at 4.00%, a real
        # 4.50% boxed at 3.00%. Capping the measured footprint at
        # BBOX_FILL_CEILING of a box that is already undersized would put the
        # box's error straight back into the grams -- and it would do it
        # silently, with a note saying the number had been made more physical.
        #
        # This is the same lesson as GrabCut, which lost 50.9% to 38.3% for one
        # reason: every method that starts from the box inherits the box.
        if box_ratio > 0 and not rail_exempt:
            ceiling = box_ratio * BBOX_FILL_CEILING
            if 0 < ceiling < area_ratio:
                # WHERE the estimate lands and HOW SURE we are of it are two
                # different questions, and they need answering separately.
                #
                # It lands on the box, because two ruler measurements say the
                # box is the trustworthy number. But an item boxed at 4% of the
                # frame and simultaneously called 30% of it means one of the
                # model's own outputs is badly wrong, and a photo that produced
                # a self-contradiction that large deserves a wider band than one
                # where the two numbers nearly agreed -- even though we keep the
                # same number in both cases.
                #
                # This used to be two flat penalties chosen by which branch of
                # the old partway-cut fired. Once the rail always landed on the
                # box, only one branch could ever run, so every disagreement
                # cost exactly 0.85 and the size of it stopped mattering: a
                # 7.5x contradiction came back MORE confident than a 1.1x one,
                # because the only surviving penalty was the unrelated
                # disagreed-with-the-prior one. Continuous, so that confidence
                # falls monotonically with the disagreement and there is no
                # threshold to sit either side of.
                disagreement = area_ratio / ceiling
                # Kept for the band, and ONLY when the box took the whole answer.
                # An ordinary blend is a correction, not a coin toss, and
                # widening its range too would put a +/-100% band on a plate of
                # rice that the pipeline is actually fairly sure about.
                if disagreement >= BBOX_ABSURD_DISAGREEMENT:
                    rail_spread = area_ratio / max(railed, 1e-9)
                detection_confidence *= max(
                    BBOX_DISAGREEMENT_FLOOR,
                    1.0 - BBOX_DISAGREEMENT_PENALTY * (disagreement - 1.0),
                )
                # Say which number this is. When coverage was used, the figure
                # being compared is coverage x plate_ratio, not anything the
                # model said about the frame, and labelling it "reported" sent
                # four nights of work at the wrong input.
                source = (
                    f"{plate_coverage:.0%} of the plate, which is {area_ratio:.0%} "
                    f"of the frame"
                    if coverage_used else
                    f"{area_ratio:.0%} of the frame"
                )
                # Wording is deliberately stable -- "bounding box", "disagree
                # by", "capped to the box" are what the operator reads and what
                # the tests pin. Only the SOURCE label is new, and it is the
                # whole point: it names which number is being compared.
                if disagreement >= BBOX_SEVERE_DISAGREEMENT:
                    notes.append(
                        f"{name}: the model put this at {source} and its own "
                        f"bounding box at {box_ratio:.0%} — they disagree by "
                        f"{disagreement:.1f}x. The box is the number that has held "
                        f"up against a ruler, so the estimate uses it "
                        f"({railed:.0%}) — but a gap that wide means something was "
                        f"misread, so the range is wide."
                    )
                else:
                    notes.append(
                        f"{name}: at {source} this exceeds what fits inside its own "
                        f"bounding box ({box_ratio:.0%}); capped to the box."
                    )
                area_ratio = railed

        # Physical sanity, part two: food on a plate cannot occupy more of the
        # frame than the plate does. A vision model that reports otherwise has miscounted
        # the region, so we cap it and take the confidence hit rather than
        # producing a confident, impossible number.
        # Not applied to a measurement either, and for a narrower reason than
        # the rail above: the plate ratio being compared against is the model's
        # own bounding-box ellipse on a 0.05 grid, not a measured plate. Food
        # really does overhang a rim, and `measured_area_plausible` has already
        # refused anything past MEASURED_OVER_PLATE_LIMIT -- which is the same
        # check with a margin that admits the overhang. Applying both would
        # shave up to 15% off every measured footprint on the strength of an
        # unmeasured number.
        if (not rail_exempt and hint.plate_ellipse_area_ratio
                and area_ratio > hint.plate_ellipse_area_ratio):
            notes.append(
                f"Detected area ({area_ratio:.0%} of frame) exceeded the plate "
                f"({hint.plate_ellipse_area_ratio:.0%}); capped to the plate."
            )
            area_ratio = hint.plate_ellipse_area_ratio
            detection_confidence *= 0.6

        food_mm2 = frame_mm2 * max(area_ratio, 1e-5)

        # ------------------------------------------------------------------
        # Measured height, when the photo actually contains one.
        # ------------------------------------------------------------------
        tilt = hint.camera_tilt_deg
        ref_w = reference_width_mm(hint)
        height_trust = 0.0
        if (USE_MEASURED_HEIGHT and height_ratio and ref_w
                and tilt is not None and tilt >= MIN_TILT_DEG):
            measured = float(height_ratio) * ref_w
            if HEIGHT_MM_MIN <= measured <= HEIGHT_MM_MAX:
                # Trust rises with the angle: at 20 degrees almost none of the
                # food's side is visible, at 45 it plainly is.
                height_trust = max(0.0, min(1.0,
                    (tilt - MIN_TILT_DEG) / (FULL_TRUST_TILT_DEG - MIN_TILT_DEG)))
                blended = height_mm * (1 - height_trust) + measured * height_trust
                notes.append(
                    f"Photographed at about {tilt:.0f} degrees, so the height "
                    f"could be measured rather than assumed: {measured:.0f} mm "
                    f"against a typical {height_mm:.0f} mm."
                )
                height_mm = blended
                # A measurement beats a prior, so the estimate earns confidence
                # instead of losing it -- but only as much as the angle earns.
                detection_confidence = min(0.95, detection_confidence * (1 + 0.15 * height_trust))
            else:
                notes.append(
                    f"Ignored a reported height of {measured:.0f} mm — outside "
                    f"anything a plated food could be."
                )

        # How much of the plate does this food actually cover? Spread wide
        # means thin; a small footprint means piled. See REFERENCE_COVERAGE.
        #
        # This correction exists ONLY because the height is a guess: it infers
        # depth from how far the food is spread. When the height has actually
        # been measured, applying it too would correct a number that is already
        # right, so it is faded out exactly as the measurement is faded in.
        walled = _key(hint.vessel) in WALLED_VESSELS or shape == "liquid"
        if hint.plate_ellipse_area_ratio and not walled and height_trust < 1.0:
            coverage = area_ratio / max(hint.plate_ellipse_area_ratio, 1e-4)

            # REFERENCE_COVERAGE describes a PLATE, not an item on it: "about a
            # third of the surface is covered by food". Comparing one item
            # against it is only the same question when that item is alone.
            #
            # Measured, on the plate meals: with four foods sharing a plate
            # each covers 8-12%, so every one of them was read as piled and
            # inflated by the capped 1.10 -- on every item, on every plate
            # photo. The comment above records this compounding at +104% and
            # the response was to cap the inflation rather than fix the
            # premise. A food is not tall because three other foods are sharing
            # its plate.
            #
            # So the comparison is now plate-to-plate: how covered is THIS
            # plate against a normally-served one. A single item alone is
            # unaffected -- its coverage is the plate's coverage, which is what
            # the constant always meant.
            plate_total = plate_food_coverage if plate_food_coverage else coverage
            spread = (REFERENCE_COVERAGE / max(plate_total, 0.02)) ** SPREAD_EXPONENT
            spread = max(SPREAD_FACTOR_MIN, min(SPREAD_FACTOR_MAX, spread))
            # Fade toward 1.0 (no correction) as the measurement takes over.
            spread = 1.0 + (spread - 1.0) * (1.0 - height_trust)
            # TURNING THIS OFF FOR A MEASURED FOOTPRINT WAS TRIED AND IS WRONG.
            #
            # The reasoning was sound and the measurement disagreed:
            # MEASURED_HEIGHTS_MM was solved by arithmetic that applies no
            # spread factor, so applying one on top looked like a double-count.
            # Switched off for measured footprints, the two live SAM2 cases went
            # from -22.2%/-18.5% to -29.3%/-25.9% -- worse in both, mean
            # absolute 20.4% to 27.6%. The factor is carrying something the
            # height table does not. Left on. Do not re-derive this from first
            # principles without a weighed bench in hand.
            if abs(spread - 1.0) > 0.05:
                notes.append(
                    f"The food on this plate covers {plate_total:.0%} of it; "
                    f"{'more spread out' if spread < 1 else 'piled deeper'} "
                    f"than a normally-served plate, so height was scaled "
                    f"x{spread:.2f}."
                )
            height_mm *= spread

        elif walled and hint.plate_ellipse_area_ratio:
            coverage = area_ratio / max(hint.plate_ellipse_area_ratio, 1e-4)
            if coverage > 0.5:
                notes.append(
                    f"Fills {coverage:.0%} of a vessel with walls, so it is deep "
                    f"rather than spread thin -- the usual thinning correction "
                    f"does not apply here."
                )

        # Depth refines the height prior: food photographed from close up tends
        # to be shot at an angle, which foreshortens the pile. Nudge, never
        # override — this is a correction, not a measurement.
        # ...but not on the depth rung itself, where the same measurement has
        # already set the frame area (area goes as distance squared). Applying
        # it again made grams scale as distance^2.25 -- one measurement entering
        # the product twice. On the plate rungs it is an independent
        # foreshortening correction and stays.
        if hint.depth_mm and method != "depth_model":
            depth_factor = max(0.75, min(1.30, (hint.depth_mm / 350.0) ** 0.25))
            height_mm *= depth_factor

        # PROFILE_FACTORS, unconditionally.
        #
        # The comment here used to describe blending this with SHAPE_FACTORS by
        # height_trust, and the code never did -- `shape_factor` was computed
        # and never read. Since USE_MEASURED_HEIGHT is off, height_trust is
        # always 0, so the described blend would have used SHAPE_FACTORS for
        # every estimate: about 20% smaller on every mound and loose item.
        #
        # The shipped behaviour is the one the bench was measured on, so the
        # numbers stay and the comment is corrected instead. When the measured
        # height is re-enabled this is the first thing to revisit, because
        # PROFILE_FACTORS assumes the bbox rail has already cut the reported
        # area down to the food's true footprint -- which it cannot do when the
        # model returns no bbox.
        # PROFILE_FACTORS, not SHAPE_FACTORS.
        #
        # SHAPE_FACTORS bundles two corrections: the pile's profile (mean height
        # over peak height) AND how much of a bounding box the food fills. That
        # was correct when area_ratio came straight from the model. It is not
        # correct now: the bbox rail cuts the reported area down to the food's
        # true footprint -- measured against a photograph, it lands within 6% --
        # so applying a bbox-fill correction on top counts it twice.
        #
        # Measured on meal 08 over ten runs: every item came in 16-19% light
        # with the correct area and a sourced density. Rice needed x1.24 and
        # beans x1.23; the ratio between these two tables is x1.24 for a mound.
        # The mechanism and the arithmetic agree, which is the only reason this
        # is a fix rather than a fudge.
        # PROFILE_FACTORS converts a PEAK height into a MEAN one -- 0.68 for a
        # mound, on the reasoning that a dome tapers to nothing at its rim. A
        # depth map returns the mean directly, over whatever shape the food
        # actually has, so applying the factor as well would count the same
        # correction twice and read every measured portion about a third light.
        #
        # This is the same double-count that HEIGHT_PRIORS_MM and
        # MEASURED_HEIGHTS_MM exist as two separate tables to avoid.
        effective_factor = 1.0 if depth_measured else PROFILE_FACTORS[shape]
        volume_mm3 = food_mm2 * height_mm * effective_factor
        grams = volume_mm3 * dens / 1000.0  # mm^3 -> cm^3 -> g

        if hint.image_count > 1:
            method = ("multi_image"
                      if method in ("pixel_area", "depth_model", "vessel_reference")
                      else method)
            notes.append(f"Cross-checked across {hint.image_count} angles.")

        # If the model also offered a serving prior and we disagree, meet it
        # partway. Two weak signals that disagree should not produce a
        # confident wrong answer.
        #
        # The pull ramps in smoothly. It used to be a hard switch at 2.5x:
        # below the line you got raw geometry, above it the geometric mean.
        # That put a cliff in the middle of the operating range -- at 2.4x
        # disagreement a plate read 372 g and at 2.6x the same plate read
        # 241 g, so ordinary vision noise either side of the threshold changed
        # the answer by half. Worse, over that step MORE detected food
        # produced LESS mass, which is not a defensible thing for a food
        # scanner to do. A continuous ramp reaches exactly the old geometric
        # mean at the old threshold, and moves smoothly below it.
        if ai_prior_grams and ai_prior_grams > 0 and grams > 0:
            ratio = grams / ai_prior_grams
            disagreement = max(ratio, 1.0 / ratio)
            if disagreement > BLEND_START:
                max_w = BLEND_MAX_WEIGHT_BY_METHOD.get(method, BLEND_MAX_WEIGHT)
                w = max_w * min(
                    1.0,
                    math.log(disagreement / BLEND_START) / math.log(BLEND_FULL / BLEND_START),
                )
                blended = math.exp(
                    (1.0 - w) * math.log(grams) + w * math.log(ai_prior_grams)
                )
                notes.append(
                    f"Geometry ({grams:.0f} g) and typical serving "
                    f"({ai_prior_grams:.0f} g) disagreed by {disagreement:.1f}x; "
                    f"pulled {w:.0%} of the way toward the prior, "
                    f"to {blended:.0f} g."
                )
                grams = blended
                # Normalised by THIS method's cap, not the global fallback.
                # Dividing by 0.5 meant the intended 15% hit at a full pull was
                # only ever delivered to methods whose cap happened to be 0.5;
                # a fully saturated pull on plate_reference took 6.6%.
                detection_confidence *= 1.0 - 0.15 * (w / max(max_w, 1e-6))

    # Partly hidden food, applied last and deliberately so.
    #
    # This used to scale the visible area before the serving prior blended in,
    # and the blend then undid it: at 90% visible the answer was 164 g and at
    # 75% visible it fell to 159 g -- more hidden food producing a smaller
    # estimate, which is precisely backwards for a correction that exists to
    # stop under-counting.
    #
    # The order is the fix. The prior describes a typical serving of the food
    # you can SEE, so geometry and prior belong on the same footing and get
    # reconciled first. Only then does the physical fact that some of the item
    # is buried get applied to the result.
    # A reported 0.02 means "almost entirely hidden", and _plausible replaced
    # anything under 0.05 with the DEFAULT of 1.0 -- fully visible, no
    # correction at all. So an item reported as 6% visible was inflated 2.9x
    # while one reported as 2% visible was left alone: more hidden food
    # producing a smaller estimate, which is the exact defect the occlusion
    # logic below exists to prevent. Below the floor the honest reading is
    # "as hidden as we are willing to correct for", not "not hidden".
    vis = _visible_or_floor(visible_fraction)
    if vis < OCCLUSION_IGNORE_ABOVE:
        occluded_by = min(1.0 / max(vis, OCCLUSION_MIN_VISIBLE), OCCLUSION_MAX_INFLATE)
        grams *= occluded_by
        notes.append(
            f"About {1.0 - vis:.0%} of this item is hidden behind other food; "
            f"what is visible is a minimum, so the estimate was raised "
            f"x{occluded_by:.2f} and the range widened upward. Worth confirming."
        )
        # Less certain -- and not symmetrically so.
        detection_confidence *= max(0.55, vis)

    # A NON-FINITE GRAMS IS NOT A HEAVY MEAL. Refuse it before the clamp.
    #
    # min(MAX_GRAMS, nan) returns MAX_GRAMS -- every comparison with nan is
    # False, so min() keeps its first argument -- and then abs(clamped - grams)
    # is also False, so the clamp note and the confidence penalty never fire.
    # The result: 1500 g, the largest portion this file will admit, published
    # with no warning and a measured-rung method. Verified by execution: one
    # non-finite frame area is enough to produce it.
    #
    # nan reaches here from a single unguarded float() on model output, so it
    # is not hypothetical; and the honest answer to "we computed nothing" is
    # the smallest portion with the confidence floored, never the largest.
    if not math.isfinite(grams):
        notes.append(
            f"{name}: the arithmetic produced no usable number, so this is a "
            f"floor rather than a measurement. Worth correcting by hand."
        )
        grams = MIN_GRAMS
        detection_confidence = min(detection_confidence, 0.2)
    clamped = max(MIN_GRAMS, min(MAX_GRAMS, grams))
    if abs(clamped - grams) > 1e-6:
        notes.append("Estimate hit a plausibility limit and was clamped.")
        detection_confidence *= 0.7
    grams = clamped

    ceiling = _METHOD_CEILING[method]
    confidence = round(min(ceiling, ceiling * max(0.2, min(1.0, detection_confidence))), 3)
    band = _METHOD_BAND[method] * (1.0 + (1.0 - confidence))

    # An occluded item's uncertainty is one-sided: the hidden part can only add
    # food, never remove it. A symmetric band would understate the risk of
    # under-counting, which is the whole reason this matters.
    band_low, band_high = band, band

    # The old one-sided widening here was for a LENGTH read off a model, which
    # could only come in short and so could only push grams high. A rectangle
    # fitted to its four corners has no such direction, so the band is
    # symmetric again.

    if occluded_by > 1.01:
        band_high = min(0.60, band_high * (1.0 + (occluded_by - 1.0)))
        band_low = band_low * 0.8

    # The band is a PERCENTAGE of the estimate, which quietly means it can never
    # say "this might be several times too small". When the rail threw away an
    # area claim and took the box instead, that is exactly what it might be.
    #
    # Photo 12 is the case. Cherry tomatoes: the model claimed 5% of frame and
    # boxed them at 0%, a 25x disagreement, so the box won and the estimate came
    # out 7.7 g against a weighed 27 g. The band around it was 5-10 g -- and the
    # reasoning stage, which had correctly asked for 25 g, was clamped down to
    # 10 g by that band. A number the pipeline had every reason to distrust was
    # handed a range tight enough to block its own rescue.
    #
    # So when two candidates disagreed several-fold, the range spans both of
    # them: the estimate stands, but the discarded answer stays reachable.
    # Capped, because a 25x spread is not a range anyone can act on.
    if rail_spread > 1.01:
        band_high = min(BAND_MAX_SPREAD - 1.0, max(band_high, rail_spread - 1.0))

    # A garnish is below the resolution of the method that measured it. Widen
    # the range rather than pretending to a precision the box cannot support.
    if 0 < area_ratio < SMALL_ITEM_AREA:
        short_by = (SMALL_ITEM_AREA - area_ratio) / SMALL_ITEM_AREA
        widen = 1.0 + (SMALL_ITEM_BAND_FACTOR - 1.0) * short_by
        band_low = min(0.75, band_low * widen)
        band_high = min(BAND_MAX_SPREAD - 1.0, band_high * widen)
        notes.append(
            f"{name}: at {area_ratio:.1%} of the frame this is near the limit of "
            f"what a photo can size — the range is wide because the estimate is "
            f"genuinely uncertain, not because the food is."
        )

    return PortionEstimate(
        reported_area_ratio=round(reported_area_ratio, 5),
        plate_coverage_used=(round(plate_coverage, 4) if coverage_used else None),
        box_area_ratio=(round(_bbox_area_ratio(bbox), 5) or None),
        measured_area_used=(
            round(float(measured_area_ratio), 5) if measured_used else None
        ),
        grams=round(grams, 1),
        grams_low=round(max(MIN_GRAMS, grams * (1 - band_low)), 1),
        grams_high=round(min(MAX_GRAMS, grams * (1 + band_high)), 1),
        occluded=occluded_by > 1.01,
        method=method,
        confidence=confidence,
        pixel_area_ratio=round(area_ratio, 4),
        depth_factor=round(depth_factor, 4),
        notes=notes,
    )


def reconcile_multi_image(estimates: list[PortionEstimate]) -> PortionEstimate:
    """Combine per-image estimates of the same food into one.

    Weighted by confidence, and the spread between images feeds back into the
    band: if two angles disagree by 2x, we should not report +-14%.
    """
    if not estimates:
        raise ValueError("nothing to reconcile")
    if len(estimates) == 1:
        return estimates[0]

    weights = [max(e.confidence, 0.05) for e in estimates]
    total_w = sum(weights)
    grams = sum(e.grams * w for e, w in zip(estimates, weights)) / total_w

    spread = (max(e.grams for e in estimates) - min(e.grams for e in estimates)) / max(grams, 1e-6)
    agreement_penalty = min(0.35, spread / 2.0)
    # Several views agreeing is worth a little more than one view -- but not
    # more than the rung allows. The ceiling table is described as a hard rule
    # ("an estimate can never be more certain than the scale it was built on")
    # and estimate_grams enforces it; this path did not, so two confident plate
    # estimates reconciled to 0.975 against a multi_image ceiling of 0.86 and
    # were reported to the user as "high".
    confidence = round(
        min(
            _METHOD_CEILING["multi_image"],
            max(0.15, (sum(e.confidence * w for e, w in zip(estimates, weights)) / total_w)
                * (1 - agreement_penalty) * 1.06),
        ),
        3,
    )
    band = max(0.12, spread / 2 + 0.10)

    # If any single view saw this food partly buried, the combined figure
    # inherits that: the reconciled number is still a corrected minimum, and
    # its uncertainty is still one-sided, because hidden food can only add
    # weight. Averaging several views does not reveal what none of them saw.
    occluded = any(e.occluded for e in estimates)
    band_low = band * 0.8 if occluded else band
    band_high = min(0.60, band * 1.35) if occluded else band

    notes = [f"Reconciled {len(estimates)} views (spread {spread * 100:.0f}%)."]
    for e in estimates:
        notes.extend(e.notes)

    return PortionEstimate(
        grams=round(grams, 1),
        grams_low=round(max(MIN_GRAMS, grams * (1 - band_low)), 1),
        grams_high=round(min(MAX_GRAMS, grams * (1 + band_high)), 1),
        occluded=occluded,
        method="multi_image",
        confidence=confidence,
        pixel_area_ratio=round(sum(e.pixel_area_ratio for e in estimates) / len(estimates), 4),
        depth_factor=round(sum(e.depth_factor for e in estimates) / len(estimates), 4),
        notes=notes[:6],
    )


def band_label(confidence: float) -> str:
    if confidence >= 0.78:
        return "high"
    if confidence >= 0.55:
        return "medium"
    return "low"
