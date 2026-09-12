# Macro references

Sourced constants for the portion pipeline. Everything in this folder is
**looked up, not reasoned** — every value carries a source tag and a citation,
and a value without one does not belong here.

## The rule this folder obeys

**Weight is measured. Macros are looked up. Nothing here touches weight.**

```
grams   <-  geometry, the calibrated vessel, and what the app learns
            from this user's own corrections
macros  <-  grams x per-100g values from this folder, from USDA,
            and from what we collect ourselves
```

That separation is deliberate and it is enforced by a test. A lookup table that
quietly moved a weight would undo days of measurement, and the weight path has
its own evidence -- a kitchen scale -- which a published average cannot
overrule.

## What it is for

`macros_per_100g.csv` sits in the lookup order between a live USDA call and the
model:

1. the shared `food_facts` cache — what a real lookup already returned
2. a live USDA lookup — the authority, when reachable
3. **this folder** — published values, offline, cited
4. an AI estimate — a model asked to recall a number
5. a flat default — 150 kcal, 6 g protein

It exists to push (4) down. Asking a model for "standard reference values"
returns a figure nobody can check, in a field where **coming out a third light
is the documented failure of every competing app**. A cited row beats a recalled
one even when the recalled one happens to be right, because only one of them can
be audited. Every row carries its USDA food code, so anyone can re-check it at
`fdc.nal.usda.gov/food-details/<code>`, and every row's energy is cross-checked
against the macros that carry it (Atwater 4/4/9) before it loads.

## The densities: loaded, cited, and not applied

`densities.csv` is the same quality of data — FAO/INFOODS and USDA cup weights —
but density sits in the **weight** formula, so it is held behind
`USE_SOURCED_DENSITIES`, which is off.

It would change seven of the eleven weighed bench foods:

| food | in force | sourced | source |
|---|---|---|---|
| cherry tomatoes | 0.55 | 0.63 | USDA, 149 g/cup whole |
| mexican rice | 0.75 | 0.67 | USDA, 158 g/cup cooked |
| baked spaghetti casserole | 0.85 | 0.933 | FAO1, tuna noodle casserole |
| roasted potatoes | 0.55 | 0.59 | FAO2, potato boiled |
| bean soup | 1.00 | 1.054 | FAO2, bean soup |
| refried beans | 1.06 | 1.02 | USDA, 242 g/cup |
| fried potato chips | 0.55 | 0.59 | FAO2, potato boiled |

Seven changes at once, with no bench run to say which way, is exactly how a
stable pipeline gets destabilised. To adopt: flip the switch, run
`.\dev benchall`, keep it only if the weighed meals improve.

A correction to an earlier draft, kept because the mistake is instructive: it
claimed a grilled meat skewer was getting a generic 0.85 and losing 19%. It is
not — the pipeline passes the food GROUP, so a skewer gets the protein group's
1.03. The 0.85 appears only if `density_for` is called with no group, which the
app never does.

## Sources

| tag | what it is |
|---|---|
| `FAO2` | [FAO/INFOODS Density Database v2.0 (2012)](https://www.fao.org/fileadmin/templates/food_composition/documents/density_DB_v2_0_01.pdf) — 638 entries across 20 food groups. The standard reference for food density. |
| `FAO1` | [FAO/INFOODS Density Database v1.0 (2011)](https://www.fao.org/fileadmin/templates/food_composition/documents/upload/Density_databse_v1_final.pdf) — kept because it carries composite dishes v2.0 dropped, and composites are exactly what the matcher used to miss. |
| `USDAcup` | A USDA FoodData Central cup weight, converted with `density = g_per_cup / 236.588`. USDA does not publish density; it publishes gram weights per household measure, which is the same information one step back. |
| `BENCH` | Measured here against a kitchen scale. Used only where it disagrees with a published value, and the disagreement is written down rather than averaged away. |

## The one live disagreement

**Cooked rice.** FAO2 gives 0.73 for rice boiled. USDA's 158 g/cup gives 0.67.
The weighed bench supports 0.67 — moving to it fixed a +19% and a +22% over-read
on two separate meals. So 0.67 stands, the disagreement is recorded in the
`note` column, and nobody has to rediscover it.

This is the pattern for any future conflict: keep both numbers visible, let the
scale decide, and never split the difference — a value halfway between two
sources is supported by neither.

## Gaps

Foods with no published density found yet. They fall back to the group density,
which is honest, rather than to a wrong specific number:

- **fettuccine alfredo** — FAO2 has plain boiled pasta at 0.55–0.59, but a cream
  sauce changes it substantially and no source gives the sauced dish. Guessing a
  number here would be exactly what this folder exists to stop.
- **mole and other thick sauces on meat** — the sauce is a large fraction of the
  weight and no published figure covers it.
- **grilled meat on a skewer** — meat itself is well established near 1.05 and
  the protein group already supplies 1.03, which is close. What is unmeasured is
  the packing fraction: a skewer is pieces with gaps between them, so the true
  figure is somewhat below solid meat and nobody has published how far.

Adding one of these means finding a cup weight or a published density, not
reasoning toward a plausible figure.

## How to add a row

1. Find a published cup weight or density.
2. Add the row to `densities.csv` with its `source` tag and a citation precise
   enough to re-find (a USDA food code, or the FAO entry's own wording).
3. Run `.\dev test`. `test_macro_references.py` will reject a row with no
   citation, an implausible density, or a duplicate key.
4. Run `.\dev benchall`. If the new value moves a weighed meal the wrong way,
   say so in the `note` — do not adjust the number.
