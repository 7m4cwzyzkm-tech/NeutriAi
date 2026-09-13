"""MEAL-LEVEL MACRO SCORE of the 12 Sep clean paired run, plus the scale replay.
Written 13 Sep 2026. Produced score.txt in this directory.

    backend\\.venv\\Scripts\\python.exe docs\\evidence\\2026-09-13-meal-replay\\score.py

Reads replay.json (from replay.py) and inputs/paired_v2.json. Offline: no network,
no model, no database. Needs only backend/scripts (bench_all, scan_bench) on the path.

Truth  = weighed grams x the RIGHT food's per-100 g row (TRUTH below, each with an
         alternative, all USDA FoodData Central rows searched 13 Sep).
Served = estimated grams x the row the product actually served that item (SERVED
         below: live food_facts rows untouched since the run, read 13 Sep; the two
         ai_estimate rows the 13-row resolve overwrote -- pepperoni pizza and
         shredded beef with potatoes -- recovered from 11 Sep meal_items kcal/grams).
HAND   = which weighed food each detection is, read off the photographs.
Photos are the units. All detections count toward a meal, because the user sees
all of them.
"""
from __future__ import annotations

import json
import math
import random
import statistics as st
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "backend"))
from scripts.bench_all import confidence_interval  # noqa: E402
from scripts.scan_bench import match, parse_actuals  # noqa: E402

R = json.loads((HERE / "replay.json").read_text(encoding="utf-8"))
R.pop("_provenance", None)
# The replay injected facts keyed by lookup name, so its items carry the LOOKUP
# name. Restore the name the run SERVED (same detection, same order) from paired_v2.
_V2 = json.loads((HERE / "inputs" / "paired_v2.json").read_text(encoding="utf-8"))
for _n, _r in R.items():
    _served = [it["name"] for it in _V2[_n]["arms"]["CAL"]]
    for _arm in _r["arms"].values():
        assert len(_arm) == len(_served), _n
        for _it, _s in zip(_arm, _served):
            _it["name"] = _s

# ---- what each item was served (live food_facts, untouched rows; two LLM rows
# recovered from the 11 Sep meal_items the same food_facts rows produced) ----
SERVED = {
    "baby toddler carrots, stage 1": (26.0, 6.0), "zucchini, pickled": (35.0, 7.44),
    "spaghetti sauce with meat": (90.0, 6.54), "barbecue chicken": (167.0, 12.23),
    "brussels sprouts, cooked, boiled, drained, with salt": (36.0, 7.1),
    "cauliflower, cooked, boiled, drained, with salt": (23.0, 4.11),
    "macaroni or pasta salad, made with mayonnaise": (221.0, 24.65),
    "potatoes, mashed, ready-to-eat": (106.0, 13.3), "mayonnaise": (680.0, 0.6),
    "beans and white rice": (164.0, 25.19),
    "shredded beef with potatoes": (436.15 / 335.5 * 100, 27.51 / 335.5 * 100),   # ai_estimate, recovered
    "oysters, steamed": (65.0, 3.44), "greens, canned, cooked": (41.0, 2.71),
    "roll, egg bread": (287.0, 47.8), "beef, cured, corned beef, canned": (250.0, 0.0),
    "potato, nfs": (126.0, 20.45), "caesar salad, with romaine, no dressing": (77.0, 7.49),
    "soup, chicken noodle": (53.0, 6.21), "soup, bean, with meat": (84.0, 10.0),
    "soup, broccoli cheese": (82.0, 6.08),
    "baked pepperoni pizza slice": (280.1 / 105.3 * 100, 30.96 / 105.3 * 100),     # ai_estimate, recovered
    "trail mix with nuts and fruit": (454.0, 51.06), "trail mix, nfs": (454.0, 51.06),
    "tortilla chips, flavored": (519.0, 60.81), "grapes, red, seedless, raw": (85.9, 20.2),
    "cheeseburger, nfs": (296.0, 18.71), "potato, french fries, from fresh, fried": (198.0, 18.5),
}

# ---- the right row for each WEIGHED food, and a plausible alternative ------
TRUTH = {  # label: (kcal, carb, source)            alt: (kcal, carb, source)
    "steamed carrots": ((35.0, 8.22, "170394 Carrots, cooked, boiled"), None),
    "steamed zucchini": ((15.0, 2.69, "169292 Zucchini, cooked, boiled"), None),
    "spaghetti with sauce": ((102.0, 19.44, "2708829 Pasta with sauce, meatless"),
                             (100.0, 11.4, "172099 Spaghetti w/ meatballs in tomato sauce")),
    "bbq chicken thigh": ((233.0, 7.28, "2706041 Chicken thigh, grilled w/ sauce, skin eaten"),
                          (191.0, 7.28, "2706042 ... skin not eaten")),
    "roast beef": ((206.0, 0.0, "2705847 Beef, roast"), (183.0, 0.0, "168674 Chuck eye roast, lean")),
    "brussels sprouts": ((36.0, 7.1, "168513 Brussels sprouts, boiled"), None),
    "macaroni salad": ((221.0, 24.65, "2708932 Macaroni salad, with mayonnaise"), None),
    "smashed potatoes": ((114.0, 17.6, "2709499 Potato, mashed, from fresh"),
                         (138.0, 16.77, "2709500 Potato, mashed, restaurant")),
    "white rice": ((129.0, 27.99, "2708408 Rice, white, cooked, no added fat"),
                   (151.0, 27.19, "2708404 Rice, white, cooked, made with oil")),
    "pot roast": ((251.0, 0.0, "2705848 Beef, pot roast"),
                  (200.0, 3.0, "171225 shoulder pot roast braised, ~15% potato/veg by eye")),
    "steamed spinach": ((23.0, 3.75, "168463 Spinach, boiled"), (33.0, 5.65, "170407 Collards, boiled -- what the photo shows")),
    "dinner roll": ((279.0, 50.12, "2707654 Roll, white, soft"), (307.0, 52.0, "175027 Rolls, dinner, egg")),
    "caesar salad": ((77.0, 7.49, "2709591 Caesar salad, no dressing"),
                     (150.0, 7.0, "no dressed row; +~15 g caesar dressing per 123 g, arithmetic")),
    "chicken noodle soup": ((53.0, 6.21, "2709149 Soup, chicken noodle"), None),
    "beef posole": ((43.0, 4.59, "2707129 Soup, pozole"), (48.0, 4.04, "2707131 Soup, pork or ham")),
    "cheese and broccoli soup": ((82.0, 6.08, "2709659 Soup, broccoli cheese"), None),
    "pizza slice": ((275.0, 27.8, "2708636 Pizza w/ pepperoni, medium crust"),
                    (274.0, 24.7, "172096 Pizza, pepperoni, regular crust")),
    "trail mix": ((454.0, 51.06, "2707574 Trail mix, NFS"), (462.0, 44.9, "167561 Trail mix, regular")),
    "tortilla chips": ((472.0, 67.78, "2708202 Tortilla chips, plain"), None),
    "grapes": ((85.9, 20.2, "2346412 Grapes, red, seedless, raw"), (69.0, 18.1, "174683 Grapes, SR")),
    "cheeseburger slider": ((297.0, 17.9, "2706894 Cheeseburger slider"), None),
    "french fries": ((198.0, 18.5, "2709458 Potato, french fries, from fresh, fried"), None),
}

# ---- which weighed food each detection IS, read off the photographs --------
# None = food in the frame that was not weighed and is not on the scored plate.
HAND = {
    ("23", 1): None,   # cauliflower -- on the NEIGHBOURING plate at the frame's top edge
    ("23", 2): None,   # macaroni salad -- same neighbouring plate
    ("25", 1): None,   # mayonnaise -- a real smear on the plate, sized at 40 g
    ("29", 0): "steamed spinach",   # "cooked greens"
    ("29", 1): "dinner roll",       # "roll, egg bread" -- 'roll,' keeps its comma, so the matcher misses it
    ("29", 3): "pot roast",         # "shredded beef"
    ("29", 4): "pot roast",         # "potato" -- a potato piece in the pot roast
}

MEALS_SAME = {"26": "potroast", "27": "potroast", "28": "potroast", "29": "potroast",
              "35": "slider", "36": "slider", "40": "trail", "41": "trail",
              "42": "chips", "43": "chips", "44": "grapes", "45": "grapes"}


def bench_pair(items, actual):
    rem = dict(parse_actuals(actual)); out, drop = {}, []
    for i, it in enumerate(items):
        m = match(str(it["name"]), rem)
        if m:
            rem.pop(m[0], None); out[i] = m[0]
        else:
            drop.append(i)
    if len(drop) == 1 and len(rem) == 1:
        out[drop[0]] = next(iter(rem)); rem.clear(); drop = []
    return out, drop, list(rem)


def hand_pair(photo, items, actual):
    bench, _, _ = bench_pair(items, actual)
    out = {}
    for i in range(len(items)):
        out[i] = HAND[(photo, i)] if (photo, i) in HAND else bench.get(i)
    return out


def ci_mean(v):
    c = confidence_interval(v)
    return f"CI {c[0]:.1f}-{c[1]:.1f}" if c else "n<3"


def boot_median(v, n=10000, seed=7):
    rnd = random.Random(seed)
    meds = sorted(st.median(rnd.choices(v, k=len(v))) for _ in range(n))
    return meds[int(0.025 * n)], meds[int(0.975 * n)]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n; d = 1 + z * z / n
    c = p + z * z / (2 * n); h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (100 * (c - h) / d, 100 * (c + h) / d)


def summarise(label, errs):
    dropped = sum(1 for e in errs if e != e)
    errs = [e for e in errs if e == e]
    if dropped:
        label = f"{label} [{dropped} undefined]"
    a = [abs(e) for e in errs]
    lo, hi = boot_median(a)
    print(f"  {label:44s} n={len(a):2d}  mean |e| {st.mean(a):5.1f}% ({ci_mean(a)})  "
          f"median {st.median(a):5.1f}% (boot {lo:.1f}-{hi:.1f})  signed {st.mean(errs):+6.1f}%")
    return a


def truth_of(label, alt=False):
    main, other = TRUTH[label]
    row = other if (alt and other) else main
    return row[0], row[1]


# ============================================================================
photos = [(n, r) for n, r in R.items() if r["entry"]["kind"] == "per-item" and r["entry"]["actual"]]
ARMS = ["CAL", "UNCAL"]

print("=" * 100)
print("CONTROL: replay reproduces the 12 Sep grams?")
worst = max(photos, key=lambda p: p[1]["control_max_abs_g"])
print(f"  worst photo max |dgrams| over 4 recorded arms: {worst[1]['control_max_abs_g']:.2f} g ({worst[0]})")
bad = [n for n, r in photos if r["control_max_abs_g"] > 0.5]
print(f"  photos off by more than 0.5 g: {bad or 'none'}")

# ---- item roster --------------------------------------------------------------
print("\nUNSCORED DETECTIONS under the bench's name matcher")
n_det = n_scored = 0
for name, r in photos:
    items = r["arms"]["CAL"]
    bench, drop, missing = bench_pair(items, r["entry"]["actual"])
    n_det += len(items); n_scored += len(bench)
    for i in drop:
        print(f"  {name[:2]} [{i}] {items[i]['name'][:40]:40s} CAL {items[i]['grams']:6.1f} g  "
              f"-> photo says: {HAND.get((name[:2], i), 'n/a')}")
    if missing:
        print(f"  {name[:2]}      weighed but unpaired: {missing}")
print(f"  {n_det} detections on {len(photos)} scored photos; {n_scored} bench-scored; {n_det - n_scored} unscored")


def meal_numbers(name, r, arm, *, alt=False, all_items=True, scheme="hand"):
    """(served kcal, served carb, true kcal, true carb, right-row kcal/carb at est grams, per-item rows)"""
    photo = name[:2]
    items = r["arms"][arm]
    actual = parse_actuals(r["entry"]["actual"])
    pair = hand_pair(photo, items, r["entry"]["actual"]) if scheme == "hand" else bench_pair(items, r["entry"]["actual"])[0]
    use = range(len(items)) if all_items else [i for i in range(len(items)) if pair.get(i)]
    s_k = s_c = rr_k = rr_c = 0.0
    for i in use:
        it = items[i]; sk, sc = SERVED[it["name"]]
        s_k += it["grams"] * sk / 100; s_c += it["grams"] * sc / 100
        lab = pair.get(i)
        rk, rc = truth_of(lab, alt) if lab else (sk, sc)      # a phantom is the right FOOD, wrong to count
        rr_k += it["grams"] * rk / 100; rr_c += it["grams"] * rc / 100
    labels = set(actual) if all_items else {pair[i] for i in use}
    t_k = sum(actual[l] * truth_of(l, alt)[0] / 100 for l in labels)
    t_c = sum(actual[l] * truth_of(l, alt)[1] / 100 for l in labels)
    return s_k, s_c, t_k, t_c, rr_k, rr_c


def pct(a, b):
    return 100 * (a / b - 1) if b else float("nan")


CARB_FLOOR_G = 2.0   # below this much true carbohydrate a percentage is not a meaningful number


def pct_carb(a, b):
    return pct(a, b) if b >= CARB_FLOOR_G else float("nan")


result = {}
for arm in ARMS:
    print("\n" + "=" * 100)
    print(f"ARM {arm}")
    rows = []
    for name, r in photos:
        sk, sc, tk, tc, rk, rc = meal_numbers(name, r, arm)
        xk, xc, xtk, xtc, _, _ = meal_numbers(name, r, arm, all_items=False, scheme="bench")
        ak, ac, atk, atc, _, _ = meal_numbers(name, r, arm, alt=True)
        items = r["arms"][arm]
        g_est = sum(i["grams"] for i in items); g_true = sum(parse_actuals(r["entry"]["actual"]).values())
        rows.append(dict(photo=name[:2], n=len(items), kcal=pct(sk, tk), carb=pct_carb(sc, tc),
                         kcal_excl=pct(xk, xtk), carb_excl=pct_carb(xc, xtc),
                         kcal_alt=pct(ak, atk), carb_alt=pct_carb(ac, atc), carb_abs_g=sc - tc,
                         grams=pct(g_est, g_true), match_k=pct(sk, rk), match_c=pct(sc, rc),
                         weight_k=pct(rk, tk), weight_c=pct(rc, tc), sk=sk, tk=tk, sc=sc, tc=tc))
    print(f"  {'ph':3s}{'n':>2s} {'true kcal':>9s} {'served':>7s} {'kcal e':>7s} {'carb e':>7s} | "
          f"{'wt-only':>7s} {'match':>7s} | {'excl kcal':>9s} {'alt-truth':>9s} {'grams':>7s}")
    for x in rows:
        print(f"  {x['photo']:3s}{x['n']:2d} {x['tk']:9.0f} {x['sk']:7.0f} {x['kcal']:+7.1f} {x['carb']:+7.1f} | "
              f"{x['weight_k']:+7.1f} {x['match_k']:+7.1f} | {x['kcal_excl']:+9.1f} {x['kcal_alt']:+9.1f} {x['grams']:+7.1f}")

    print("\n MEAL TOTALS (photos as units)")
    summarise("energy, all detections (what the user sees)", [x["kcal"] for x in rows])
    summarise("carbs,  all detections", [x["carb"] for x in rows])
    summarise("energy, bench-scored items only (old rule)", [x["kcal_excl"] for x in rows])
    summarise("carbs,  bench-scored items only", [x["carb_excl"] for x in rows])
    summarise("energy, ALT truth rows", [x["kcal_alt"] for x in rows])
    summarise("grams,  all detections", [x["grams"] for x in rows])
    for key, lab in (("kcal", "energy"), ("carb", "carbs")):
        defined = [x for x in rows if x[key] == x[key]]
        k = sum(abs(x[key]) <= 10 for x in defined); n = len(defined); lo, hi = wilson(k, n)
        print(f"  within 10% on {lab:6s}: {k}/{n} = {100*k/n:.0f}%  (Wilson {lo:.0f}-{hi:.0f}%)"
              + (f"  [{len(rows)-n} meal(s) under {CARB_FLOOR_G:.0f} g true carbs left out: "
                 + ", ".join(f"{x['photo']} {x['carb_abs_g']:+.1f} g" for x in rows if x[key] != x[key]) + "]"
                 if n < len(rows) else ""))
    both = sum(abs(x["kcal"]) <= 10 and x["carb"] == x["carb"] and abs(x["carb"]) <= 10 for x in rows)
    print(f"  within 10% on BOTH (carb-defined meals): {both}/{sum(1 for x in rows if x['carb'] == x['carb'])}")
    k = sum(abs(x["kcal_alt"]) <= 10 for x in rows)
    print(f"  within 10% on energy under ALT truth rows: {k}/{len(rows)}")
    by_meal = {}
    for x in rows:
        by_meal.setdefault(MEALS_SAME.get(x["photo"], x["photo"]), []).append(abs(x["kcal"]))
    cl = [st.mean(v) for v in by_meal.values()]
    print(f"  clustered by distinct weighed meal: n={len(cl)}  mean |e| {st.mean(cl):.1f}% ({ci_mean(cl)})")

    # ---- per item, both ways -------------------------------------------------
    print("\n PER ITEM, energy")
    it_bench, it_all, it_hand, it_grams = [], [], [], []
    ratio_rows = []
    for name, r in photos:
        photo = name[:2]; items = r["arms"][arm]; actual = parse_actuals(r["entry"]["actual"])
        bench, drop, missing = bench_pair(items, r["entry"]["actual"])
        for i, lab in bench.items():
            sk = SERVED[items[i]["name"]][0]
            it_bench.append(pct(items[i]["grams"] * sk, actual[lab] * truth_of(lab)[0]))
            it_grams.append(pct(items[i]["grams"], actual[lab]))
        it_all.extend(it_bench[-len(bench):] if bench else [])
        it_all.extend([100.0] * len(drop))            # unscored detection: wrong, floor 100%
        it_all.extend([-100.0] * len(missing))        # weighed food no detection was paired to
        # hand pairing: sum detections per weighed food, phantoms at 100%
        hand = hand_pair(photo, items, r["entry"]["actual"])
        per_lab, abs_k, tk_meal = {}, 0.0, 0.0
        for i, lab in hand.items():
            sk = SERVED[items[i]["name"]][0] * items[i]["grams"] / 100
            if lab is None:
                it_hand.append(100.0); abs_k += sk
            else:
                per_lab[lab] = per_lab.get(lab, 0.0) + sk
        for lab, w in actual.items():
            tk = w * truth_of(lab)[0] / 100; tk_meal += tk
            e = pct(per_lab.get(lab, 0.0), tk); it_hand.append(e); abs_k += abs(e) / 100 * tk
        sk_all = sum(SERVED[i["name"]][0] * i["grams"] / 100 for i in items)
        if len(items) > 1:
            ratio_rows.append((photo, len(actual), 100 * abs_k / tk_meal, abs(pct(sk_all, tk_meal))))
    print(f"  grams check (bench-scored, should be the recorded run): n={len(it_grams)} mean |e| "
          f"{st.mean(map(abs, it_grams)):.1f}%  median {st.median(list(map(abs, it_grams))):.1f}%")
    item_bench_abs = summarise("bench-scored items only (old rule)", it_bench)
    summarise("all detections, unscored = wrong (+-100%)", it_all)
    summarise("photo-verified pairing, phantoms = 100%", it_hand)
    meal_abs = [abs(x["kcal"]) for x in rows]
    print(f"  item-level mean |e| / meal-level mean |e|: {st.mean(item_bench_abs):.1f} / {st.mean(meal_abs):.1f}")

    print("\n AVERAGING ON THE MULTI-DETECTION PLATES: kcal-weighted item |e| vs meal |e|")
    for photo, nw, item_e, meal_e in ratio_rows:
        print(f"  {photo}  weighed foods {nw}  item |e| {item_e:6.1f}%  meal |e| {meal_e:6.1f}%  ratio {meal_e / item_e if item_e else float('nan'):.2f}")
    single = sum(1 for name, r in photos if len(parse_actuals(r['entry']['actual'])) == 1)
    print(f"  {single} of {len(photos)} scored photos hold ONE weighed food: meal error = item error there by construction")
    result[arm] = rows

# ============================================================================
print("\n" + "=" * 100)
print("SCALE: replay with only mm^2-per-frame changed (energy, served rows, all detections)")


def meal_kcal(r, arm_key):
    return sum(SERVED[i["name"]][0] * i["grams"] / 100 for i in r["arms"][arm_key])


def truek(r):
    return sum(w * truth_of(l)[0] / 100 for l, w in parse_actuals(r["entry"]["actual"]).items())


def elastic(r, base, pert, k):
    a, b = meal_kcal(r, base), meal_kcal(r, pert)
    return math.log(b / a) / math.log(k) if a and b else float("nan")


for base, pert in (("CAL", "CAL_k1.2"), ("UNCAL", "UNCAL_k1.2")):
    el = [elastic(r, base, pert, 1.2) for _, r in photos]
    print(f"  {base}: meal-energy elasticity to frame area, median {st.median(el):.2f}, "
          f"range {min(el):.2f}-{max(el):.2f}  (1.0 = moves fully with the scale; +20% area -> "
          f"+{100*(1.2**st.median(el)-1):.1f}% energy at the median)")
    zero = [n[:2] for n, r in photos if abs(elastic(r, base, pert, 1.2)) < 0.05]
    print(f"     insensitive (<0.05): {zero}")

print("\n  CAL arm: the scale is already MEASURED (tape diameter + Hough rim area).")
e_cal = [pct(meal_kcal(r, "CAL"), truek(r)) for _, r in photos]
e_mod = [pct(meal_kcal(r, "CAL_MODELRATIO"), truek(r)) for _, r in photos]
e_up = [pct(meal_kcal(r, "CAL_k1.2"), truek(r)) for _, r in photos]
e_dn = [pct(meal_kcal(r, "CAL_k0.833"), truek(r)) for _, r in photos]
summarise("CAL as shipped (Hough area)", e_cal)
summarise("CAL with the model's own plate_area_ratio", e_mod)
summarise("CAL, frame area x1.2", e_up)
summarise("CAL, frame area /1.2", e_dn)
hr = [r["hough_ratio"] / r["model_ratio"] for _, r in photos if r["hough_ratio"] and r["model_ratio"]]
print(f"  Hough/model plate area: median {st.median(hr):.3f}, SD of ln {st.stdev([math.log(x) for x in hr]):.3f}")

print("\n  CARD CHECK on the calibrated scale (independent ruler, n small):")
for name, r in photos:
    fw = r.get("card_frame_width_mm"); asp = r.get("aspect"); mm2 = r["frame_mm2"]["CAL"]
    if fw and asp and mm2:
        card_mm2 = fw * fw / asp
        print(f"  {name[:2]} frame area plate/Hough {mm2:,.0f} mm2  card {card_mm2:,.0f} mm2  "
              f"plate/card {mm2 / card_mm2:.3f}  (card rung over-reads raised food ~10%, portion.py:1675)")

print("\n  UNCAL arm: replace only the subscriber's scale with the calibrated one (same rung, same blend cap)")
e_u = [pct(meal_kcal(r, "UNCAL"), truek(r)) for _, r in photos]
e_ut = [pct(meal_kcal(r, "UNCAL_TRUESCALE" if "UNCAL_TRUESCALE" in r["arms"] else "UNCAL"), truek(r)) for _, r in photos]
summarise("UNCAL as shipped", e_u)
summarise("UNCAL with calibrated scale substituted", e_ut)
for (name, r), a, b in zip(photos, e_u, e_ut):
    k = r.get("k_true_uncal")
    print(f"   {name[:2]} {r['frame_mm2']['UNCAL_method'] or 'ai_prior':17s} k={('%.3f' % k) if k else '  -  '}  "
          f"meal kcal {a:+7.1f}% -> {b:+7.1f}%   scale term {100*(math.exp(math.log((a/100+1)/(b/100+1)))-1):+6.1f}%")

# ---- decomposition, log terms, CAL and UNCAL ------------------------------------
print("\n" + "=" * 100)
print("DECOMPOSITION of ln(meal served kcal / true kcal)  =  match + weight   (weight = est grams at right rows vs truth)")
for arm in ARMS:
    rows = result[arm]
    parts = []
    for (name, r), x in zip(photos, rows):
        tot = math.log(x["sk"] / x["tk"])
        m = math.log(1 + x["match_k"] / 100)
        w = math.log(1 + x["weight_k"] / 100)
        sc = None
        if arm == "UNCAL" and "UNCAL_TRUESCALE" in r["arms"]:
            sc = math.log(meal_kcal(r, "UNCAL") / meal_kcal(r, "UNCAL_TRUESCALE"))
        elif arm == "UNCAL":
            sc = 0.0
        parts.append((name[:2], tot, m, w, sc))
    mt = st.mean(abs(p[1]) for p in parts)
    print(f"\n {arm}: mean |ln total| {mt:.3f}   mean |ln match| {st.mean(abs(p[2]) for p in parts):.3f}   "
          f"mean |ln weight| {st.mean(abs(p[3]) for p in parts):.3f}"
          + (f"   of which scale {st.mean(abs(p[4]) for p in parts):.3f}, "
             f"non-scale weight {st.mean(abs(p[3]-p[4]) for p in parts):.3f}" if arm == "UNCAL" else ""))
    vt = st.pvariance([p[1] for p in parts])
    print(f"   variance of ln total {vt:.4f}: match {st.pvariance([p[2] for p in parts]):.4f}, "
          f"weight {st.pvariance([p[3] for p in parts]):.4f}"
          + (f", scale {st.pvariance([p[4] for p in parts]):.4f}, non-scale weight {st.pvariance([p[3]-p[4] for p in parts]):.4f}"
             if arm == "UNCAL" else ""))
    tail = sorted(parts, key=lambda p: -abs(p[1]))[:6]
    print("   worst 6 meals (photo: total | match | weight" + (" | scale" if arm == "UNCAL" else "") + "), % terms:")
    for p in tail:
        f = lambda v: f"{100*(math.exp(v)-1):+6.1f}%"
        dom = "MATCH" if abs(p[2]) > abs(p[3]) else "weight"
        print(f"     {p[0]}: {f(p[1])} | {f(p[2])} | {f(p[3])}" + (f" | {f(p[4])}" if p[4] is not None else "") + f"   larger: {dom}")
    nm = sum(1 for p in tail if abs(p[2]) > abs(p[3]))
    print(f"   match term larger than weight term in {nm} of the worst 6; in {sum(1 for p in parts if abs(p[2]) > abs(p[3]))} of all {len(parts)}")

# ---- wrong-match cost table -------------------------------------------------------
print("\n" + "=" * 100)
print("WRONG-MATCH COST per 100 g: served row vs right row")
MIS = [
    ("rice (lookup key; served on other scans)", "dirty rice", (112.0, 17.25), "white rice", (129.0, 27.99)),
    ("mexican rice (photo 14, unweighed)", "mexican pizza", (249.0, 15.3), "spanish rice, NS fat 2709087", (115.0, 19.64)),
    ("white rice (26, 29)", "beans and white rice", (164.0, 25.19), "white rice", (129.0, 27.99)),
    ("grilled zucchini slices (18)", "zucchini, pickled", (35.0, 7.44), "zucchini, boiled", (15.0, 2.69)),
    ("steamed bun (28 = dinner roll)", "oysters, steamed", (65.0, 3.44), "roll, white, soft", (279.0, 50.12)),
    ("piece of meat with sauce (22 = roast beef)", "spaghetti sauce with meat", (90.0, 6.54), "beef, roast", (206.0, 0.0)),
    ("soup with meat (32 = beef posole)", "soup, bean, with meat", (84.0, 10.0), "soup, pozole", (43.0, 4.59)),
    ("cooked greens (29 = spinach)", "greens, canned, cooked", (41.0, 2.71), "spinach, boiled", (23.0, 3.75)),
    ("boiled spaghetti w/ tomato sauce (20)", "spaghetti sauce with meat", (90.0, 6.54), "pasta with sauce, meatless", (102.0, 19.44)),
    ("barbecue chicken (21 = thigh)", "barbecue chicken", (167.0, 12.23), "thigh, grilled w/ sauce", (233.0, 7.28)),
    ("baby carrots (17 = steamed carrots)", "baby toddler carrots, stage 1", (26.0, 6.0), "carrots, boiled", (35.0, 8.22)),
    ("shredded beef (29 = pot roast)", "corned beef, canned", (250.0, 0.0), "beef, pot roast", (251.0, 0.0)),
    ("shredded beef with potatoes (27 = pot roast, LLM row)", "LLM estimate", SERVED["shredded beef with potatoes"], "beef, pot roast", (251.0, 0.0)),
    ("baked bread roll (29)", "roll, egg bread", (287.0, 47.8), "roll, white, soft", (279.0, 50.12)),
    ("mashed potatoes (25 = smashed)", "potatoes, mashed, ready-to-eat", (106.0, 13.3), "potato, mashed, from fresh", (114.0, 17.6)),
    ("tortilla chips (42, 43)", "tortilla chips, flavored", (519.0, 60.81), "tortilla chips, plain", (472.0, 67.78)),
    ("cheeseburger (35, 36 = slider)", "cheeseburger, nfs", (296.0, 18.71), "cheeseburger slider", (297.0, 17.9)),
]
print(f"  {'bench food':52s} {'served':30s} {'kcal':>11s} {'carbs':>12s}   right row")
for food, srow, (sk, sc), rrow, (rk, rc) in MIS:
    kd = f"{sk:.0f}/{rk:.0f} {pct(sk, rk):+5.0f}%"
    cd = f"{sc:.1f}/{rc:.1f} " + (f"{pct(sc, rc):+5.0f}%" if rc else "  n/a")
    print(f"  {food:52s} {srow[:30]:30s} {kd:>16s} {cd:>17s}   {rrow}")

# ============================================================================
print("\n" + "=" * 100)
print("DECOMPOSITION WITH PHANTOMS SEPARATED: ln total = match + phantom + real-food weight (UNCAL: real weight = scale + rest)")
for arm in ARMS:
    terms = []
    for name, r in photos:
        photo = name[:2]; items = r["arms"][arm]; actual = parse_actuals(r["entry"]["actual"])
        hand = hand_pair(photo, items, r["entry"]["actual"])
        served = right_all = right_real = 0.0
        for i, it in enumerate(items):
            sk = SERVED[it["name"]][0]
            lab = hand[i]
            rk = truth_of(lab)[0] if lab else sk
            served += it["grams"] * sk / 100
            right_all += it["grams"] * rk / 100
            if lab:
                right_real += it["grams"] * rk / 100
        truth = sum(w * truth_of(l)[0] / 100 for l, w in actual.items())
        t = dict(photo=photo, total=math.log(served / truth), match=math.log(served / right_all),
                 phantom=math.log(right_all / right_real), weight=math.log(right_real / truth))
        if arm == "UNCAL":
            t["scale"] = (math.log(meal_kcal(r, "UNCAL") / meal_kcal(r, "UNCAL_TRUESCALE"))
                          if "UNCAL_TRUESCALE" in r["arms"] else 0.0)
            t["rest"] = t["weight"] - t["scale"]
        terms.append(t)
    keys = ["match", "phantom", "weight"] + (["scale", "rest"] if arm == "UNCAL" else [])
    print(f"\n {arm}  (n={len(terms)})   term: mean |ln|  variance  meals where it is the largest term")
    print(f"   {'total':8s} {st.mean(abs(t['total']) for t in terms):.3f}  {st.pvariance([t['total'] for t in terms]):.4f}")
    lead_keys = ["match", "phantom", "scale", "rest"] if arm == "UNCAL" else ["match", "phantom", "weight"]
    lead = [max(lead_keys, key=lambda k: abs(t[k])) for t in terms]
    for k in keys:
        print(f"   {k:8s} {st.mean(abs(t[k]) for t in terms):.3f}  {st.pvariance([t[k] for t in terms]):.4f}  "
              f"{lead.count(k) if k in lead_keys else '-'}")
    tail = sorted(terms, key=lambda t: -abs(t["total"]))
    big = [t for t in tail if abs(math.exp(t["total"]) - 1) > 0.5]
    f = lambda v: f"{100*(math.exp(v)-1):+6.1f}%"
    print(f"   meals off by more than 50% on energy: {len(big)}")
    for t in big:
        lk = max(lead_keys, key=lambda k: abs(t[k]))
        print(f"     {t['photo']}: total {f(t['total'])}  match {f(t['match'])}  phantom {f(t['phantom'])}  "
              f"real weight {f(t['weight'])}" + (f"  [scale {f(t['scale'])}, rest {f(t['rest'])}]" if arm == "UNCAL" else "")
              + f"   largest: {lk}")
    no_ph = [t for t in terms if abs(t["phantom"]) < 1e-9]
    e = [100 * (math.exp(t["total"]) - 1) for t in no_ph]
    summarise(f"{arm} energy, meals with no phantom detection", e)

print("\nMULTI-FOOD PLATES: are item errors on one plate independent? (log grams error per weighed food)")
for arm in ARMS:
    for name, r in photos:
        actual = parse_actuals(r["entry"]["actual"])
        if len(actual) < 2:
            continue
        items = r["arms"][arm]; hand = hand_pair(name[:2], items, r["entry"]["actual"])
        g = {}
        for i, it in enumerate(items):
            if hand[i]:
                g[hand[i]] = g.get(hand[i], 0.0) + it["grams"]
        print(f"  {arm:5s} {name[:2]}: " + "  ".join(f"{l} {100*(g.get(l,0)/w-1):+.0f}%" for l, w in actual.items()))
