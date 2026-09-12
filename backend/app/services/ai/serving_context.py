"""What the serving surface says about the food on it.

A photograph contains more than the food. It contains what the food is sitting
on, and that carries real information: nobody serves rice, beans and a
drumstick loose on butcher paper, but wrap the same three things in a tortilla
and paper is exactly where it turns up.

WHAT THIS MAY AND MAY NOT DO
----------------------------
It may break a TIE between two candidate identifications. It may not remove an
item, override a confident identification, or change a portion.

That boundary is not a style preference, it is a scar. This codebase has
already watched contextual reasoning delete a 133 g portion of fideo from a
weighed plate because pasta "did not fit cuisine coherence" with the Mexican
dishes beside it -- taking the meal from 426 g to 232 g, and a third of
someone's lunch out of their day. The rule written into the reasoning prompt
afterwards is the rule here:

    Context may help you NAME a food. It must never decide whether a food is
    there. The plate is the evidence.

So `typical_for` returns an opinion, never a verdict, and the caller may only
consult it when two candidates are already close. A surprising food on a
surprising surface stays exactly as detected, because people eat what they
like however they like, and the ones whose meals look least like a textbook
are precisely the ones an app like this must not quietly mis-measure.

WHERE THE ASSOCIATIONS COME FROM
--------------------------------
Food-service packaging guides on what each paper type is used for, and
regional barbecue references on how the meat is served. Sources are cited on
the entries they support. Where nothing is known, the answer is None -- no
opinion -- which is the common and correct case for a dinner plate, on which
literally anything can appear.
"""
from __future__ import annotations

# Surfaces, grouped by what they imply. The keys match the vessel vocabulary
# the vision model is given.
#
# TYPICAL: foods that genuinely arrive this way, often enough to be evidence.
# The lists are deliberately about FORM -- handheld, wrapped, sliced-to-order --
# rather than cuisine, because form is what the surface actually constrains.
SURFACE_TYPICAL: dict[str, tuple[str, ...]] = {
    # Wrapping and deli papers are for food you pick up, and for barbecue.
    # [src] webstaurantstore packaging guide: sandwich wrap is used for
    # "sandwiches, burgers, hot dogs, and bratwursts", sides, pretzels and
    # pastries; wax paper for sandwiches and pizza-box liners; butcher paper
    # for meat and fish.
    # [src] Central Texas barbecue is "served on butcher paper with minimal
    # accompaniments" -- brisket, beef ribs, sausage, pork ribs, turkey, with
    # white bread, pickles, onions and jalapenos.
    "paper": (
        "sandwich", "burger", "hamburger", "cheeseburger", "hot dog", "brat",
        "sub", "hoagie", "panini", "wrap", "burrito", "taco", "quesadilla",
        "gyro", "shawarma", "kebab", "empanada", "pastry", "pretzel", "donut",
        "cookie", "pizza", "fries", "chips", "crisps", "fried chicken",
        "chicken tender", "nugget", "wing", "fish and chips", "brisket",
        "ribs", "sausage", "pulled pork", "barbecue", "bbq", "bagel", "toast",
    ),
    "butcher_paper": (
        "brisket", "ribs", "sausage", "pulled pork", "barbecue", "bbq",
        "turkey", "steak", "sandwich", "burger",
    ),
    # A board is for things sliced or arranged, and for barbecue at home.
    "cutting_board": (
        "charcuterie", "cheese", "cured meat", "salami", "prosciutto", "bread",
        "baguette", "steak", "brisket", "ribs", "roast", "sandwich", "pizza",
        "fruit", "crackers",
    ),
    # A metal tray is cafeteria, diner and barbecue service.
    "tray": (
        "barbecue", "bbq", "brisket", "ribs", "sausage", "fried chicken",
        "burger", "fries", "thali", "banchan", "combo", "platter",
    ),
    # Takeout containers hold what a restaurant sends home.
    "takeout_box": (
        "pasta", "noodles", "rice", "fried rice", "stir fry", "curry", "salad",
        "chow mein", "lo mein", "pad thai", "biryani",
    ),
}
SURFACE_TYPICAL["wrapper"] = SURFACE_TYPICAL["paper"]
SURFACE_TYPICAL["foil"] = SURFACE_TYPICAL["paper"]
SURFACE_TYPICAL["napkin"] = SURFACE_TYPICAL["paper"]
SURFACE_TYPICAL["board"] = SURFACE_TYPICAL["cutting_board"]
SURFACE_TYPICAL["box"] = SURFACE_TYPICAL["takeout_box"]
SURFACE_TYPICAL["clamshell"] = SURFACE_TYPICAL["takeout_box"]

# Foods that are eaten with cutlery from a surface with a rim, and do not
# travel loose on paper. Rice IS on this list; a burrito is not, and that
# distinction is the whole point -- the same rice arrives on paper the moment
# it is wrapped in something.
NEEDS_A_RIM: tuple[str, ...] = (
    "soup", "stew", "broth", "chili", "curry", "porridge", "oatmeal", "cereal",
    "yogurt", "pudding", "gravy", "mashed potato", "refried beans", "risotto",
    "spaghetti", "noodle soup", "congee", "dal",
)

# A plate holds anything. This is not a gap in the table, it is the fact.
SURFACES_WITH_NO_OPINION = frozenset({
    "plate", "dinner_plate", "side_plate", "salad_plate", "bowl", "soup_bowl",
    "large_bowl", "pasta_bowl", "skillet", "pan", "cup", "mug", "glass",
    "table", "counter", "hand", "unknown", "none", "",
})


def typical_for(surface: str | None, food_name: str | None) -> bool | None:
    """Is this food usually served on this surface?

    True  -- yes, this is a normal pairing
    False -- this food is not normally served loose on this surface
    None  -- no opinion, which includes every plate and bowl

    None is the answer far more often than either of the others, and callers
    must treat it as "say nothing" rather than as a weak False.
    """
    key = (surface or "").strip().lower().replace(" ", "_").replace("-", "_")
    name = (food_name or "").strip().lower()
    if not key or not name or key in SURFACES_WITH_NO_OPINION:
        return None
    typical = SURFACE_TYPICAL.get(key)
    if typical is None:
        return None
    if any(word in name for word in typical):
        return True
    if any(word in name for word in NEEDS_A_RIM):
        return False
    return None
