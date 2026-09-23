# AI nutritionist / AI chef / workout-aware meal timing — survey — 23 Sep 2026

Read-only survey, no code changes. Every claim below names the file and line
(or table/migration) it came from. Where something could not be determined by
static reading alone, it is stated as "not found" or "needs a product
decision," not guessed.

Scope: Gil's three asks — (1) an AI nutritionist tracking a real micronutrient
progression log and advising on it, (2) an AI chef that scales recipe
portions to a headcount, (3) workout-day-aware meal timing — surveyed against
the real backend schema, the real nutrition/recipe/fitness code, and the real
mobile screens.

---

## 1. Current nutrient data coverage — the real gap

**Stored today, confirmed from the schema** — `supabase/migrations/0003_nutrition.sql:8-27`,
`food_facts`:

```
kcal_per_100g, protein_per_100g, carbs_per_100g, fat_per_100g,
fiber_per_100g, sugar_per_100g, sodium_mg_per_100g
```

Seven fields. Nothing else — no potassium, calcium, iron, magnesium, zinc, no
vitamin A/C/D/E/K, no B-vitamins. `meal_items` (migration `0003`, lines
102-123) and `meals` (lines 73-95) both snapshot the *same seven* fields per
item and per meal respectively, and `recipes`/`recipe_ingredients` (migration
`0006`) mirror the identical seven-field set again as `*_per_serving`. The gap
is structurally identical at every layer, because every layer copies the same
shape from `food_facts`.

**The USDA mapping — confirmed by name and by exact IDs** —
`backend/app/services/nutrition/providers.py:55-64`, the dict is called
`_USDA_NUTRIENTS`:

```python
_USDA_NUTRIENTS = {
    1008: "kcal_per_100g",      # Energy (kcal)
    2047: "kcal_per_100g",      # Energy (Atwater general)
    1003: "protein_per_100g",
    1005: "carbs_per_100g",
    1004: "fat_per_100g",
    1079: "fiber_per_100g",
    2000: "sugar_per_100g",
    1093: "sodium_mg_per_100g",
}
```

Eight nutrient IDs (two of them both map to kcal). The parsing loop at
`providers.py:155-158`:

```python
for n in food.get("foodNutrients") or []:
    key = _USDA_NUTRIENTS.get(n.get("nutrientId"))
    if key and out.get(key, 0) == 0:
        out[key] = float(n.get("value") or 0)
```

Any `nutrientId` not in the dict is silently dropped — `key` is `None`, the
`if key` guard skips it. **This is the concrete, confirmed finding the task
asked for: USDA's real search response for a typical food (Foundation/SR
Legacy items routinely carry 30-80 `foodNutrients` entries, including
potassium `1092`, calcium `1087`, iron `1089`, magnesium `1090`, zinc `1095`,
vitamin A `1106`, vitamin C `1162`, vitamin D `1114`, vitamin E `1109`,
vitamin K `1185`, and the B-vitamins `1165`/`1166`/`1167`/`1175`/`1178`) is
already being paid for over the network and then discarded in memory before
the function returns.** Nothing caches it for later backfill either —
`out["raw"] = {"fdcId": food.get("fdcId"), "dataType": food.get("dataType")}`
(line 165) stores only two identifiers, not the nutrient array. A later
"add the missing columns" task cannot backfill existing `food_facts` rows
from data already fetched; it would need to re-query USDA by the stored
`fdcId` for every row, or start capturing the full array going forward and
accept that older rows stay incomplete until re-resolved.

The other two providers have the same or a worse gap, checked directly:
- **Nutritionix** (`providers.py:196-244`) maps six named fields
  (`nf_calories`, `nf_protein`, `nf_total_carbohydrate`, `nf_total_fat`,
  `nf_dietary_fiber`, `nf_sugars`, `nf_sodium`) off its `/v2/natural/nutrients`
  response. That endpoint's real payload also carries a `full_nutrients` array
  keyed by the same USDA attribute IDs — unused here, same discard pattern as
  USDA.
- **Edamam** (`providers.py:250-282`) calls `/api/food-database/v2/parser`
  with `nutrition-type=logging`, whose `nutrients` object only ever contains
  `ENERC_KCAL, PROCNT, CHOCDF, FAT, FIBTG, SUGAR, NA` — this specific endpoint
  does not return vitamins/minerals at all. Edamam's richer nutrient set lives
  behind a *different* endpoint (`/api/food-database/v2/nutrients`, a POST),
  not called anywhere in this codebase.

**Bottom line:** USDA is the richest source already wired in, and is the one
worth extending first — the data is already being fetched, just discarded.
Nutritionix is a smaller second win. Edamam's current endpoint cannot answer
this at all without switching endpoints.

---

## 2. What a progression log would need

**There is already a per-user, per-day rollup table, written at log time —
not computed on read.** `supabase/migrations/0010_rollups.sql:1-66`:
`daily_summaries(user_id, day, kcal_in, protein_g, carbs_g, fat_g, fiber_g,
sugar_g, kcal_out, steps, water_ml, workouts, fast_minutes, goals_met)`,
maintained by `recompute_daily_summary(p_user, p_day)` — a `plpgsql` function
that re-sums straight from `meals`, `water_logs`, `health_days`, fired by
`AFTER INSERT OR UPDATE OR DELETE` triggers on those three tables
(`t_meals_rollup`, `t_water_rollup`, `t_health_rollup`, lines 68-91). This is
also idempotent and re-runnable by a nightly worker (referenced in
`app/workers/scheduler.py`'s rollup job). `GET /me/dashboard`
(`backend/app/routers/profiles.py:90-95`) reads this rollup via a single
`dashboard` RPC call — it does not recompute from raw logs on every read.

The chain that would need extending for micronutrients is four layers deep,
confirmed by reading each table:
`food_facts` (per-100g reference) → `meal_items` (per-item snapshot: grams ×
per-100g, `0003_nutrition.sql:102-123`) → `meals` (per-meal sum,
`0003_nutrition.sql:73-95`) → `daily_summaries` (per-day rollup,
`0010_rollups.sql`). Today all four layers carry exactly the same seven
fields; extending to "dozens of micronutrients" by adding dozens of new
columns to all four existing tables would mean altering three
already-populated, actively-written tables (`meals`, `meal_items`,
`daily_summaries`) with mostly-null historical data for every existing row,
and a fourth ALTER every time the tracked-nutrient list changes later.

**Recommendation: do not widen the existing tables. Add a separate, narrow
(tall, not wide) nutrient-log structure, rolled up the same way
`daily_summaries` already is.** Concretely, something like:

```
meal_item_nutrients(meal_item_id, nutrient_key, amount)      -- per-item, per-nutrient
nutrient_log_daily(user_id, day, nutrient_key, total_amount) -- per-day rollup
```

keyed by a `nutrient_key` (e.g. `'potassium_mg'`, `'vitamin_c_mg'`) rather
than one column per nutrient, populated by a trigger on `meal_item_nutrients`
the same way `t_meals_rollup` already populates `daily_summaries`. Reasoning:
(a) it matches this codebase's own established rollup pattern exactly — a
write-time trigger, not a read-time aggregation, which is the pattern already
proven correct for `daily_summaries`; (b) most foods report most
micronutrients as zero or absent, so a wide table would be extremely sparse
— a narrow table only stores what's actually present; (c) the tracked-nutrient
list is explicitly not finalized yet (see §6/product decisions below) — adding
a new nutrient to a narrow table is a data change, not a schema migration,
which matters a lot if the "dozens" list gets revised after launch. The one
real cost is a slightly less trivial read query (group by nutrient_key
instead of naming columns) — a non-issue at this data volume and exactly the
kind of query Postgres is good at.

---

## 3. What "advise accordingly" would run on — and its real cost

**A directly reusable pattern exists**, read in full at
`backend/app/services/ai/recipe_ai.py:200-286` (`adapt()`). It already:
- builds a personalization prompt from the user's `dietary_restrictions`,
  `nutrition_targets`, `fasting_settings`, and a workout-frequency signal
  (see §5 — this is the "carb-guidance adjustment" the task pointed at:
  `if workload >= 8: constraints.append("Training frequently — do not cut
  carbohydrate aggressively.")`, `recipe_ai.py:216`);
- calls `ask_reasoning(pipeline="recipe_adaptation", system=RECIPE_ADAPT_SYSTEM,
  user_text=..., max_tokens=4000)` (`recipe_ai.py:230`) — the same client
  wrapper (`backend/app/services/ai/client.py`) every other reasoning call in
  this app uses;
- calls `await record_usage(call, user_id)` immediately after (`recipe_ai.py:249`)
  — the same cost/usage telemetry every other call uses, writing to the
  `ai_usage` table (`client.py:268-279`: `pipeline, model, input_tokens,
  output_tokens, cost_usd, latency_ms, ok`).

**A new "AI nutritionist" reasoning call can reuse this exact pattern with a
new `pipeline=` tag and its own system prompt** — no new client, no new usage
tracking. `backend/app/services/ai/prompts.py`'s `RECIPE_ADAPT_SYSTEM` (the
recipe-personalization prompt referenced by name in `recipe_ai.py`) and
`INTAKE_SYSTEM` (used by `assessment.py` for the per-meal
overeating/macro-drift assessment) are both real prompts already following
this same shape — a new coaching prompt is additive, not a new mechanism.

**The gating gap, confirmed precisely — this is the part to flag plainly.**
Every `ask_reasoning`/`ask_vision`-backed route in this app uses exactly one
of two dependencies, confirmed by grepping every router:
- `AiScanDep` → `consume_ai_scan` (`backend/app/deps.py`) — **counted**,
  atomically, against a real daily ceiling (free tier 3/day, Pro/free-launch
  200/day). Used by `POST /equipment/scan` and `POST /scans` (meal scans).
- `ProDep` → `require_pro` (`backend/app/deps.py`) — **gate only, no
  counting at all**. Used by `POST /recipes/{id}/adapt` and
  `POST /fitness/plans` (plan generation). An active/trialing user — or, as
  of this repo's current state, *anyone at all* during `free_launch_mode`
  (`require_pro` returns early unconditionally when
  `settings.free_launch_mode` is true) — can call either of these routes an
  unbounded number of times in a day. Nothing counts or throttles it.

The only budget mechanism that exists at all is global, not per-user, and
advisory only: `app/workers/scheduler.py:152-164`'s `ai_budget_check()` sums
today's `ai_usage.cost_usd` across every user, compares it to
`settings.ai_daily_cost_ceiling_usd` (default $50/day), and **only logs a
warning past 80%** (`log.error("ai_budget_warning", ...)`) — it does not
block or slow down a single call.

**The product-decision-not-engineering-decision point, stated plainly, as
asked:** a new AI-nutritionist reasoning call that runs "on some cadence"
over a user's log has a real, ongoing, per-call cost (`claude-sonnet-4-5` at
$3/$15 per million input/output tokens, per `client.py:41`, or whichever
`settings.reasoning_model` is configured). If it is built the same way
`adapt()` is — `ProDep`-gated, callable on demand from the client — it
inherits the exact same "unlimited calls, no counter" gap the rest of the
`ProDep` routes already have, and during the *current* free-launch period
(no billing, `free_launch_mode=True` by default) that means literally
unlimited free reasoning calls per user with nothing today that would even
notice, let alone stop it, until the global $50/day alert fires for
everyone at once. This needs one of: (a) a fixed server-side cadence (e.g.
once per user per day, computed by a worker, not requested on demand — this
also happens to be the natural reading of "advises on some cadence" from
Gil's own ask, and bounds the cost to at most N users × 1 call/day
regardless of gating), or (b) a real per-user counter reusing
`consume_ai_scan`'s atomic-RPC pattern instead of `require_pro`'s
pass/fail-only pattern. Either way, this is Gil's call to make before any
code gets written, not something to default silently.

---

## 4. Servings-scaled recipe portions

**Much closer to already-supported than expected — the data model already
stores per-serving nutrition, and the scaling math already exists in one
endpoint.**

`recipes` (`supabase/migrations/0006_recipes.sql:5-35`) stores a base
`servings` integer and `kcal_per_serving`/`protein_g_per_serving`/
`carbs_g_per_serving`/`fat_g_per_serving`/`fiber_g_per_serving`/
`sugar_g_per_serving` — i.e. **per-person nutrition is already the native
unit**, not a total that needs dividing. "Per-person nutrition for N people"
for the recipe's *own* serving count is already exactly what's stored; no
scaling is needed for that case at all.

`recipe_ingredients` (same migration, lines 41-59) stores `quantity`
(numeric) and `grams` (numeric) as real structured numbers, not just
`raw_text` — so linearly rescaling an ingredient list to a different
headcount is pure arithmetic on data that already exists in the right shape.

**This arithmetic is already implemented, once, today** —
`backend/app/routers/recipes.py:251-290`, `GET
/recipes/{recipe_id}/shopping-list?servings=N`:

```python
factor = (servings or recipe["servings"]) / max(recipe["servings"], 1)
...
"qty": round(float(i["quantity"] or 0) * factor, 2) or None,
"grams": round(float(i["grams"] or 0) * factor, 1),
```

This is a free, deterministic, zero-AI-cost operation, confirmed working for
the shopping list. Per-serving macros need **no recomputation at all** under
a linear scale — a per-serving number is invariant by definition; only the
ingredient list and the total scale.

**The actual gap is narrow and mobile-only.** The only place a user can see a
recipe adapted to a different headcount today is the paid, `ProDep`-gated
`POST /recipes/{id}/adapt` flow (`useAdaptRecipe()` in
`mobile/src/hooks/useApi.ts`, wired into `mobile/src/screens/RecipesScreen.tsx`
lines 22-48, 183-243) — a full LLM rewrite meant for allergies/diet/macro
constraints, which also happens to accept `target_servings` and re-scale as
a side effect of rewriting the whole recipe. There is no cheap "just show me
this recipe for 4 people" path in the UI, even though the backend already
has the exact math for it (proven by the shopping-list endpoint) sitting
unused for the ingredient list itself.

**Recommendation:** build "cooking for N" as its own small, deterministic
feature — extend `GET /recipes/{recipe_id}` (or add a sibling endpoint
mirroring the shopping-list one) to accept `?servings=N` and return the same
`factor`-scaled ingredient list, with per-serving macros passed through
unchanged. This should **not** ride on `POST /adapt` — that call is
`ProDep`-gated, costs a real model call, and answers a different question
(rewrite for constraints) that a family cooking a recipe as-written for more
people does not need answered. Reusing the exact `factor` math from
`recipes.py:260` (already correct, already tested in production) is smaller
and safer than writing new scaling logic.

---

## 5. Workout-day awareness for meal timing

Two different signals exist, and they answer two different questions — this
distinction is the main finding here.

**"Did a workout happen today" — cheap, exists, unambiguous.**
`workouts` (`supabase/migrations/0004_fitness.sql:55-73`) has `started_at
timestamptz not null`, indexed as `(user_id, started_at desc)`. A single
query, `... where user_id = :u and started_at::date = current_date`, answers
this today with no new bookkeeping — this is the exact query
`recompute_daily_summary` already runs a version of (`count(*) ... where
started_at::date = p_day`, `0010_rollups.sql:34-35`) for the dashboard
rollup, so the precedent for a cheap per-day workout check already exists in
this codebase, just not exposed as its own reusable helper yet.
`plan_days.completed_at` (`0004_fitness.sql:156-167`) is a second,
independent "did something happen today" signal — set when a user marks a
scheduled plan day done — equally cheap to query by date.

**"Is today scheduled to be a training day" — does NOT exist today, and
needs new logic, confirmed by reading every place `plan_days` is touched.**
`training_plans` stores `starts_on` (a real date) and `days_per_week`
(`0004_fitness.sql:138-152`), but `plan_days` itself
(`0004_fitness.sql:156-167`) is indexed purely by `week_index`/`day_index` —
a position *within* the plan template, with **no calendar date column at
all**. Grepping every place `week_index`/`day_index`/`starts_on` is read
(`app/routers/fitness.py:213-260`, `app/services/ai/coach.py:121-212`) found
no code anywhere that maps "today's calendar date" to a specific
`(week_index, day_index)` — plan generation writes the full week×day grid as
a template (`coach.py:121-149`) and the mobile `TrainScreen.tsx` lets the
user manually pick a week via `Chip` buttons; nothing derives "this week,
this day" from `starts_on` + today's date automatically. Building this needs
a real design decision this survey cannot make for Gil: what day-of-week
does `day_index` 1 mean — the day the plan started, or a fixed weekday
(Mon/Wed/Fri)? What happens when a day is skipped — does the whole sequence
shift, leaving `plan_days` permanently a template rather than a calendar? This
is a small but real piece of logic that does not exist yet, not a query away.

**What pre/post/rest-day meal timing would hook into:** for a v1, "did a
workout happen today" (the cheap, existing, unambiguous signal) is
sufficient to distinguish a rest day from a training day *after the fact*
(post-workout meal timing, "how was training today"). A *prospective*
"is today going to be a training day" signal, needed for genuine
pre-workout meal timing decided in the morning, needs the calendar-mapping
logic above built first — it is not close to free.

---

## 6. Where this fits in the app

`mobile/src/screens/ProfileScreen.tsx`, current structure on `main` at this
survey's branch point (the BMI-line change from earlier tonight had not yet
merged when this was read, so the "Your daily targets" card still shows the
BMR/TDEE breakdown — the card's existence and position are unaffected either
way): header → **Plan** card (subscription) → **Your daily targets** card
(kcal/protein/carbs/fat, `ProfileScreen.tsx:83-129`) → **Connected devices**
card (wearables, lines 143-171) → Sign out → Delete account.

**Recommendation: a small text link inside (or immediately below) the "Your
daily targets" card, pushing to its own dedicated screen — not a new card,
not a new tab.** This mirrors the pattern already established earlier
tonight for the Water screen (a small "Reminders & more →" link on
HomeScreen's existing Water card, pushing to a dedicated modal stack screen,
rather than cramming reminder settings into the card itself) — the same
shape of problem (a dense screen, a feature that deserves more room than a
card can give it) got the same answer once already in this codebase. "Your
daily targets" is also the most topically adjacent existing card — a
progression log is fundamentally the history of the numbers that card
already shows today, so linking from there reads naturally rather than
requiring a new, separate section on an already-dense screen.

---

## Recommended phased breakdown

Not one implementation task. Six, in roughly this order, each sized like the
other small Claude Code Agent tasks tonight — and two explicit gates where
Gil has to decide something before code gets written.

**Gate 0 — product decisions needed before any schema or code (no engineering
work possible here yet):**
1. **The exact micronutrient list.** "Dozens" is not a schema. Someone needs
   to pick the real, named list (e.g. potassium, calcium, iron, magnesium,
   zinc, vitamin A/C/D/E/K, B1/B2/B3/B6/B9/B12 — a reasonable FDA-label-style
   set, but Gil's call) before §1/§2's table can be built for real.
2. **The AI-nutritionist cost model.** Fixed daily cadence (server-triggered,
   cost bounded by user count) vs. on-demand (`ProDep`-only, currently
   unbounded per user) vs. a new counted quota. This determines the shape of
   the code in task 4 below and has a direct, real dollar cost during the
   free-launch period specifically — see §3.

**Task 1 — USDA micronutrient capture (small, backend-only, unblocked once
Gate 0.1 is answered).** Extend `_USDA_NUTRIENTS` (`providers.py:55-64`) with
the named list's real USDA nutrient IDs, add the matching columns to
`food_facts` via migration, and add a `nutrient_log_daily`-style narrow
rollup table (§2) with its own write-time trigger, following
`0010_rollups.sql`'s exact existing pattern. No AI cost — this is pure data
plumbing. Ship without the coaching feature; it's useful (or at least visible
in a progression log) on its own.

**Task 2 — progression log screen and entry point (small, mobile +
lightweight read endpoint).** A new screen reading the new rollup table,
reached via the small link described in §6. No new AI calls, no new cost.
Depends on Task 1's data existing.

**Task 3 — "cooking for N" (small, mostly backend, some mobile).** Extend
`GET /recipes/{recipe_id}` (or a sibling endpoint) to accept `?servings=N`
and return the `factor`-scaled ingredient list already proven correct in
`recipes.py:260`'s shopping-list endpoint; add a servings stepper to the
recipe detail view in `RecipesScreen.tsx`. Zero AI cost, no schema change,
independent of every other task here — could ship first if a quick win is
wanted.

**Task 4 — the AI-nutritionist reasoning call itself (small-to-medium,
backend, blocked on Gate 0.2).** A new `pipeline="nutrition_coaching"` call
reusing `ask_reasoning`/`record_usage` exactly as `recipe_ai.adapt()` does,
reading the Task 1 rollup and `nutrition_targets`. Its gating mechanism is
decided by Gate 0.2, not invented fresh here.

**Task 5 — surfacing coaching output in Profile/dashboard (small, mobile).**
Wherever Task 4's output is meant to be read day to day — depends on Task 4
existing and on a decision (not surveyed here) about where advice text is
shown vs. the progression numbers from Task 2.

**Task 6 — workout-day-aware meal timing (medium, needs its own small design
pass first).** Start with the cheap "workout logged today" signal (§5) for
post-workout/rest-day framing — buildable now, no new bookkeeping. The
prospective "is today scheduled" signal for true pre-workout timing needs the
calendar-mapping design question in §5 answered first; treat that as its own
follow-on once Task 6's simpler half has shipped and the mapping question has
an owner.

---

## Not verified (needs Gil, or a runtime/product decision)

- The exact micronutrient list to track (Gate 0.1) — this survey lists a
  plausible label-style set as an example, not a recommendation of which
  dozens to pick.
- Which cost model Gate 0.2 should choose — this survey lays out the options
  and the real dollar exposure, not a chosen answer.
- Whether re-fetching USDA by stored `fdcId` to backfill existing
  `food_facts` rows (once micronutrient columns exist) is worth the API
  calls, versus only capturing them going forward. Not costed here.
- The day-of-week convention question in §5 (does `day_index` mean a fixed
  weekday or "N days after the plan started") — needs a decision before the
  prospective training-day signal can be built at all.
- Whether Nutritionix's `full_nutrients` array (mentioned in §1) is present
  on this app's actual paid tier — not confirmed against a live response,
  per the no-paid-API-calls rule for this task.
