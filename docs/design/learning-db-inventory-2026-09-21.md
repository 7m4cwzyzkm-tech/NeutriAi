# Learning-database inventory — 21 Sep 2026

Documentation only. No code, schema, or data changed. Every claim below names a
file and line; where nothing was found, that is stated explicitly along with
what was searched.

Scope: this note inventories the repo against Gil's target design (mother /
user / pooled layers, weighed-vs-typed, graduation) and the eight additional
product decisions from the same day, both quoted in the task that produced
this note.

---

## 1. What exists today, with evidence

### `portion_learning` (learned food heights)

- Table: `portion_learning` — `supabase/migrations/0021_portion_learning.sql:44-64`.
  Columns: `id, shape, food_name (nullable), height_mm, samples, dispersion, updated_at`,
  `unique (shape, food_name)`. **No `user_id` column.**
- **Global by explicit design**, not an oversight. The migration's own header:
  "WHY IT IS GLOBAL, NOT PER USER ... this table has no user_id — which also
  means it is never personal data" (`0021_portion_learning.sql:37-42`); table
  comment: "Global and non-personal: how tall rice sits is a property of rice"
  (`0021_portion_learning.sql:66-67`).
- `0023_portion_learning_unique_shape.sql:34-48` fixes a real bug in 0021's
  constraint (`NULL != NULL` in Postgres means `unique(shape, food_name)` never
  actually enforced one row per shape) with a proper partial unique index.
- RLS: enabled, `for select using (true)` — publicly readable, service-role
  write only (`0021_portion_learning.sql:82-86`).
- Write path: `backend/app/services/portion_learning.py:231-258` (`_apply`,
  update-or-insert), triggered by `learn_from_correction()` (`:261-286`) off a
  user's correction on a scan whose **scale** was already measured
  (`MEASURED_SCALE_METHODS`, `:57`, mirrors `vision.MEASURED_SCALES`).
- Read path: `learned_heights()` (`:306-368`) is called once per photo —
  `backend/app/services/ai/vision.py:1060`, passed into estimation at
  `vision.py:1141` → `portion.py:1868` (param) → `portion.py:2139` (lookup).
  Applies floors before publishing a row: shape-level `MIN_SAMPLES`, food-level
  `MIN_FOOD_SAMPLES` (`portion_learning.py:78,80`) — this is the mechanism that
  makes the table pooled-evidence-only: "the estimator ignores a row until it
  has MIN_SAMPLES behind it, so no single user can move a global prior at all"
  (`portion_learning.py:40-41`).
- **Verdict: EXISTS, global.** This is the nearest existing scaffold to the
  MOTHER layer's "only changes from pooled evidence" behaviour — but it has no
  Nutrition5k seed, no Gil-review gate, and no version number (see §2.1).

### `food_identity` / `aliases_for` / `food_aliases`

- Purpose: when the vision model only *describes* a dish rather than *names*
  it, the user's correction is remembered and reapplied next time that same
  person gets the same description (`backend/app/services/food_identity.py:1-27`).
  Never touches a dish the model confidently named (guard at `:137-138`).
- `aliases_for(user_id)` — the DB read — `food_identity.py:181-193`:
  `service().table("food_aliases").select(...).eq("user_id", user_id)`.
  Explicitly per-user in the query itself.
- Table `food_aliases` — `supabase/migrations/0024_food_identity.sql:27-40`:
  `id, user_id (FK auth.users, cascade), described_as, actual_name, samples,
  created_at, updated_at`. Unique index on `(user_id, described_as)`
  (`:46-47`). RLS: `using (auth.uid() = user_id)` (`:52-58`).
- Migration's own rationale: "WHY PER USER AND NOT A SHARED CATALOGUE ...
  What is stored here is what THIS person eats and has named"
  (`0024_food_identity.sql:16-21`).
- `apply_to()` (`food_identity.py:126-159`) only renames once
  `samples >= MIN_SAMPLES_TO_APPLY` (2, `:58`) — "one answer is an offer; two
  is applied," matching the migration comment (`0024:34`).
- **Verdict: EXISTS, strictly per-user**, DB-enforced (RLS) as well as
  code-enforced.

### The resolver and density lookup

- File: `backend/app/services/nutrition/resolver.py` (HANDOFF.md's shorthand
  `resolver.py:24-31` etc. lines up with this file).
- `food_facts` table — `supabase/migrations/0003_nutrition.sql:8-27`:
  `canonical_key text not null unique`, no `user_id`. **Global — one row per
  canonical key across every user.** Confirmed in the module's own docstring:
  "`food_facts` is ONE table shared by every user" (`resolver.py:26-27`); "a
  popular food is paid for exactly once across the entire user base"
  (`resolver.py:9-10`).
- `canonical()` (`resolver.py:35-43`) collapses spelling noise but
  deliberately keeps preparation words (`raw`/`cooked`/`fresh`) after a real
  incident where collapsing them let one name's cached row silently answer for
  all three (`resolver.py:24-32`, and `docs/HANDOFF.md:337-346`).
- `RESOLVER_VERSION = 3` (`resolver.py:65`) is a cache-invalidation stamp
  written into each row (`:135`) and checked on every read (`_is_current`,
  `:68-71`) — versioning exists for the **matching logic**, not for any
  mother-layer concept.
- `density_for()` — `backend/app/services/ai/portion.py:1322-1397`. Precedence,
  quoted directly (`:1329-1335`):
  ```
  1. explicit   a real measured density from the nutrition database
  2. dish match the dish itself, in the table below
  3. group      the coarse food-group density
  4. full-name match, only when no group was reported at all
  5. default
  ```
  Rank 1 (`explicit`) comes from `food_facts.density_g_ml` — the same global
  table above. Ranks 2-5 are hardcoded Python dictionaries in `portion.py`.
  **No `user_id` appears anywhere in `density_for` or its helpers.**
- **Verdict: EXISTS, entirely global.** Nutrition facts and density are both
  one-table-for-everyone; there is no per-user density learning at all.

### Per-user plate-width learning: `vessel_observations` / `scale_learning.py`

- Table `vessel_observations` — `supabase/migrations/0022_vessel_observations.sql:52-77`:
  `id, user_id, vessel (text), width_mm, source (tape|reference_object|correction),
  prior_mm, scan_id, created_at`. RLS forced to `user_id = auth.uid()`
  (`:121-128`).
- `scale_learning.py` writes it: `record()` (`:246-275`) inserts one
  observation, then `refresh()` (`:289-332`) recomputes the vessel's best
  width (inverse-variance weighted, `_weighted()` `:129-200`) and writes it to
  `scan_calibrations` (extended by the same migration with `width_error_pct`,
  `observations` — `0022:90-92`). A tape reading is never overwritten by
  inference (`:308-310`).
- Fires on **every scan** with a card, a named vessel, and a plate ellipse —
  not only on corrections (`vision.py:1767-1787`, comment "THE FREE
  MEASUREMENT"). `source="reference_object"` is the value vision.py passes;
  `"tape"` and `"correction"` sources are defined in the schema/weights table
  but no write site for them was found in this pass.
- `TARGET_WIDTH_ERROR = 0.01` (1%, `scale_learning.py:65`); `MIN_OBSERVATIONS
  = 4` (`:86`); `progress()` (`:344-392`) computes per-vessel `at_target` and
  exposes it at `GET /calibrations/progress` (`backend/app/routers/scans.py:138-171`).
- The docstring frames this explicitly as trial-period design: "Three weeks
  of someone photographing their meals produces that without asking them for
  anything. That is what the trial period is: not a trial of the app, a
  period in which the app measures the user's kitchen" (`scale_learning.py:34-37`).
- **Verdict: EXISTS, per-user, and closely matches the target design's "their
  plates (outer diameter... averaged across photos)."** This is the most
  complete piece of the whole target design already built.

### "Weighed" vs "typed" — searched, not found

- Grepped `weighed`, `food_scale`, `scale_reading`, `verification_source` across
  `backend/`: the only hits are in bench/test scripts (`test_portion.py`,
  `scripts/mask_lab.py`, `scripts/measure_lab.py`, `scripts/segment_lab.py`),
  which refer to the *bench's own* ground-truth weighing, not a user-facing tag.
- The nearest same-named concept, `MEASURED_SCALES` / `MEASURED_SCALE_METHODS`
  (`vision.py:778-800`, `portion_learning.py:57`), is about **camera scale
  provenance** (was the frame's mm-per-pixel measured via a card/tape/depth
  model), not about whether the **food's mass** came from a kitchen scale or a
  typed correction. Do not conflate the two when building this — they answer
  different questions.
- **Verdict: MISSING.** No mechanism anywhere tags a correction as coming from
  a food scale versus a retyped number.

### How Nutrition5k is used today

- **Not wired into any production code path.** `backend/scripts/bench_all.py:56`'s
  `CASES` is a hand-written list of the team's own weighed photos; no loader
  from `dish_metadata_*.csv` or `depth_test_ids.txt` exists. This gap is
  itself recorded as future work: `docs/HANDOFF.md:829-830`.
  `backend/scripts/bench_all.py:381,583` and
  `backend/app/services/ai/depth_map.py:12-15` only cite N5K's published
  numbers in comments.
- All real use is **documentation and one-off evidence scripts**:
  `docs/HANDOFF.md:788-870` ("Nutrition5k as a bench — assessed 13 Sep,
  nothing downloaded" through the depth-rung probe); `docs/evidence/2026-09-13-n5k-probe-and-bundle-a-scoring/`
  (a standalone probe script + 16 cached GPT-4o detections, run once, not
  imported anywhere); `docs/evidence/2026-09-13-meal-replay/score_n5k_metric.py`
  (re-expresses the bench's own numbers in N5K's metric for comparison).
- **Verdict: MISSING as a data source.** Nutrition5k informs target-setting
  and one-off validation only; nothing in the running app reads its dishes.

### License / provenance note

Quoted exactly, both instances found (grepped `creative commons|CC BY|license`
case-insensitively across `docs/`; the only other hit, `docs/RUNBOOK.md:1030`,
is Google Play Console license testing and unrelated):

> `docs/HANDOFF.md:790-792`
> **Licence:** Creative Commons Attribution 4.0 (README links
> creativecommons.org/licenses/by/4.0) -- commercial use permitted, with
> attribution.

> `docs/MEASURED-HEIGHT-NOTES.md:1170`
> `n5k/rgb/*.png` (16), `n5k/*.csv`, `depth_test_ids.txt` | Nutrition5k data,
> CC BY 4.0, re-downloadable | leave; the dish ids are in selection.json

No top-level `LICENSE` or `README.md` mention of Nutrition5k was checked in
this pass (search was scoped to `docs/` per the task); flagged as unverified
below.

---

## 2. Target design, item by item

| # | Item | Verdict | Evidence |
|---|------|---------|----------|
| 1 | **MOTHER layer** — N5K seed, fixed pseudo-examples (10), only moves from pooled evidence, Gil-reviewed, versioned | **MISSING** | N5K not wired in (§1, "How N5K is used"). No pseudo-example weighting concept anywhere. `portion_learning` is structurally the closest thing (global, evidence-gated by `MIN_SAMPLES`) but is not seeded from N5K, has no review gate, and its `updated_at` is a timestamp, not a version — nothing like `RESOLVER_VERSION` exists for it. |
| 2a | USER layer — **their plates** (diameter from card photos, averaged) | **EXISTS** | `vessel_observations` + `scale_learning.py` (§1). Per-user, inverse-variance averaged across photos, essentially as specified. |
| 2b | USER layer — **their common foods** (height, density) | **PARTIAL** | Height: `portion_learning` learns height from corrections but is explicitly **global**, not per-user (§1) — the *mechanism* exists, the *layer* (per-user) does not. Density: `density_for` (`portion.py:1322-1397`) is 100% global/hardcoded; no per-user density anywhere. |
| 2c | USER layer — **their corrections** | **EXISTS as a feed**, not as storage of the correction itself for later mother-layer review | `PATCH /meals/{id}` (`mobile/src/screens/MealDetailScreen.tsx:144` per `docs/HANDOFF.md:2781-2786`) feeds `food_identity.learn_from_correction`, `portion_learning.learn_from_correction`, and `scale_learning` (`source="correction"`). Corrections are consumed and folded into priors immediately; there is no separate "corrections log" a pooled-review step could later inspect. |
| 2d | USER layer — **their habits** (shooting distance, tilt) | **MISSING** | No per-user table for camera distance or tilt was found. `camera_fov_deg` is read only transiently, inside one scan's depth-rung math (`docs/HANDOFF.md:2777`, `portion.py:1712`); nothing persists it per user across scans. |
| 2e | **3-week trial** framing | **PARTIAL** | `scale_learning.py:34-37`'s own docstring already frames the trial exactly this way ("not a trial of the app, a period in which the app measures the user's kitchen"), and `subscriptions.trial_start/trial_end` exist (`0008_billing.sql:22-23`). But the actual trial length shown to users is **15 days**, not 3 weeks/21 days (`mobile/src/screens/PaywallScreen.tsx:184-223`: "Start {trial_days ?? 15}-day free trial"; `README.md:37`), and nothing programmatically ties trial length to calibration completeness. |
| 3 | **POOLED layer** — opted-in users' weighed records, reviewed before reaching mother | **MISSING** | No opt-in flag, no review-status column, no "weighed" tag (§1) exists anywhere. |
| 4a | Card returns for **unknown/unreadable plate** | **MISSING** (as a targeted re-prompt) | `scale_learning` has no "this looks like a new vessel, measure it again" path — it just accumulates more observations under whatever vessel name the model reports. |
| 4b | Card returns for **unknown food**, prompts for name/description | **EXISTS** | `food_identity.py` does exactly this when the model can only describe (not name) a dish (§1). |
| 5a | Graduation — **plate diameter agrees within ~2%** | **PARTIAL** | `scale_learning.TARGET_WIDTH_ERROR = 0.01` (1%, stricter than Gil's ~2%) and `progress()`/`at_target` compute this per vessel today (`scale_learning.py:216-224, 344-392`), exposed at `GET /calibrations/progress` (`routers/scans.py:138-171`) — **but the mobile app never calls this endpoint** (confirmed: no reference to `calibrations/progress`, `complete_pct`, `at_target`, or `photos_to_target` anywhere in `mobile/src`). The number is computed and stranded. |
| 5b | Graduation — **common foods have ≥3 weighed examples** | **MISSING** | `portion_learning`'s `MIN_FOOD_SAMPLES` floor is a **global pooled** threshold (reported at 25 by the research pass), not a per-user "this user has weighed their own top foods 3 times" check — and there is no "weighed" tag to count against in the first place (§1). |
| 5c | Graduation — **prediction check within bias target** | **MISSING** | No in-trial backtest/bias-check mechanism was found anywhere. |
| 5d | **Graduation state itself** (a flag, a decision) | **MISSING** | Nothing persists a graduated/not-graduated flag; §5a's `at_target` is recomputed on every read and consumed by nothing. |

---

## 3. Additional product decisions (21 Sep) — reported, not built

| Decision | Verdict | Evidence |
|---|---|---|
| **Geometry-class catalog** (diameter steps, rim width/height, depth class) | **MISSING** | Everything keys on a product/vessel **name** string: `portion.py:377-401` `VESSEL_WIDTH_MM` (`"dinner_plate"`, `"bowl"`, ...), `:405-410` `VESSEL_DEFAULT_SHAPE`, `:431-438` `VESSEL_CERTAINTY`; `vessel_observations.vessel` and `scan_calibrations.vessel` are the same free-text name (`0017_calibration_vessel.sql:22-29`, `0022_vessel_observations.sql:55`). No diameter-step/rim/depth-class column or table exists. `docs/MEASURED-HEIGHT-NOTES.md:372-505` has a **per-plate manual measurement manifest** (rim height, floor depth, by hand, one plate at a time) — useful as a measurement *protocol* but not a reusable class catalog. |
| **Plausibility cap** (food ≤ plate floor, cannot heap without limit) | **PARTIAL** | Total detected food area across all items is capped at the measured plate's own area: `plate_food_coverage = min(1.0, total_area / plate_ratio)` (`vision.py:1046-1053`). A single item's area is also railed to ~84% of its own detection box (`portion.py:1571-1596`, `railed_area`). An absolute weight clamp exists: `MIN_GRAMS, MAX_GRAMS = 3.0, 1500.0` (`portion.py:944-946`, applied `:2530`). **Heaping is a soft multiplier, not a hard cap**: `SPREAD_FACTOR_MIN, SPREAD_FACTOR_MAX = 0.45, 1.10` bounds a correction applied to *height* (`portion.py:873, 2324-2373`), not a volume ceiling derived from the plate's own depth. So: area-vs-plate exists; volume/heap does not. |
| **Messaging wording** ("never say over"; approved style "larger than your usual plate") | **EXISTS AS A LIVE VIOLATION** | The product currently *does* say "over": `mobile/src/components/Rings.tsx:64` — `{remaining >= 0 ? 'kcal left' : 'kcal over'}`, and comment at `:55` "Over target turns the ring amber, well over turns it red." The backend's `overeat_severity_t` enum and `assessment.py`'s severity bands are also framed in "over the target" language. There **is** a good place to enforce the new rule: `backend/app/services/ai/prompts.py:362-391` (`INTAKE_SYSTEM`) already carries hard tone rules (no "bad", "cheat", "guilty", "earn", "damage" — `:382-383`) and is LLM-generated free text constrained by these rules, not a fixed template — but it does not yet forbid "over," and in fact uses "over the daily target" itself (`:387`). The literal `Rings.tsx` string needs a direct fix, independent of any LLM prompt change. |
| **Data-quality score / eligibility field** | **MISSING** | Zero matches for `quality_score`, `data_quality`, `eligib`, `participation`, `beneficial` anywhere in the repo. The nearest analog, `is_verified` (`backend/app/models/nutrition.py:189`; set at `routers/scans.py:237,323`), means "user manually confirmed this meal," not a data-quality score. |
| **Offer / entitlement / free-period mechanism** | **PARTIAL — mechanism exists, automation does not** | `entitlements` table (`0008_billing.sql:52-62`) is "the single source of truth the API gates on": `tier` includes `'comped'`, `source` includes `'promo'`/`'admin'`. This is a workable substrate for a manually- or programmatically-granted free period. **Nothing computes or triggers a grant from data quality** — no code path sets `tier='comped'` based on anything but manual/Stripe/IAP action. No RevenueCat integration exists (`mobile/src/native/purchases.ts:29` names it only as a future option). |
| **Meal-source / eating-context habit field** | **MISSING** | `"takeout_box"` etc. exist only as a **vessel/container** type for portion-size math (`portion.py:391`, `prompts.py:19`), not as a why/where-eaten tag. No `meal_source`, `eating_context`, `is_home_cooked`, or similar field exists on `meals`, `meal_items`, `recipes`, or `profiles` in any migration. |
| **Graduation state / per-user flag / card opt-out prompt** | **PARTIAL** | `scale_learning.progress()` (`:344-392`) computes readiness-to-stop-asking **for scale only**, exposed at `GET /calibrations/progress` — but it is not persisted as a flag (recomputed every call) and, per §2 row 5a, **is not consumed by the mobile client at all**. No card-opt-out prompt exists, and the other two graduation criteria (food samples, bias check) have no equivalent. |
| **Recipes** (opt-in, private by default, alias-matched, ownership/moderation) | **PARTIAL** | Full schema exists: `recipes`, `recipe_ingredients`, `recipe_saves`, `shopping_lists` (`0006_recipes.sql`), with `author_id` ownership and a full router (`backend/app/routers/recipes.py`). But: **`is_public` defaults to `true`** (`0006_recipes.sql:27`) — the opposite of Gil's "private by default, opt-in per recipe." **No moderation flag, no license/terms field** beyond `author_id` (checked `is_public|license|moderat|flag|report|ownership|copyright` against the migration — no hits). **Ingredient-to-nutrition matching uses the global resolver, not the per-user alias table**: `recipe_ai.py:10,139` calls `resolver.resolve_many(...)` — the same global `food_facts` path density lookup uses — never `food_identity.aliases_for`. So the specific mechanism Gil asked for (match recipes to foods via the alias table) does not exist, even though a general macro lookup does. |

---

## 4. Smallest safe first build per MISSING piece, in dependency order

Nothing below was built. Two sentences each, ordered so each item's stated
dependency comes first.

1. **Fix the live messaging violation** (no dependency; do this regardless of
   anything else). Replace `Rings.tsx:64`'s `'kcal over'` with wording in
   Gil's approved style, and add "never use the word 'over'; prefer 'larger
   than your usual plate'" as a hard rule in `prompts.py`'s `INTAKE_SYSTEM`
   (`:381-388`), next to its existing banned-word list.

2. **Add a weighed-vs-typed tag on corrections.** Everything else about
   "weighed" (graduation rule 5b, the pooled layer, the data-quality score)
   depends on this existing first. Add a `source` value (`"weighed"` /
   `"typed"`) alongside the existing `MEASURED_SCALE_METHODS` concept,
   captured at the same point `PATCH /meals/{id}` already records a
   correction, mirroring the enum pattern `vessel_observations.source`
   already uses.

3. **Add per-user food height/density tables.** Depends on #2 to weight a
   weighed correction more than a typed one. Create a `user_food_learning`
   table shaped like `vessel_observations` (`user_id, food_name, height_mm,
   density_g_ml, source, samples`), fed by the same correction path
   `portion_learning.learn_from_correction` already listens to, kept
   separate from the global `portion_learning` table so no single user's
   plating habit leaks into the shared prior.

4. **Persist per-user shooting habits (distance, tilt).** No new dependency;
   the values are already computed per scan. Add a small per-user rolling
   table populated the same way `scale_learning.record` populates
   `vessel_observations`, storing the camera distance and tilt already
   read at `portion.py:1712` and the plate-ellipse tilt, instead of
   discarding them after the scan.

5. **Build the plate geometry-class catalog.** Needed before an unknown-plate
   prompt (#6) can mean "new shape" rather than "new name." Add a lookup
   keyed on `(diameter_cm_step, rim_width_class, rim_height_class,
   depth_class)`, computed from the rim/ellipse measurements `food_seg.py`
   already extracts per scan, and match `vessel_observations` rows against
   it alongside the existing name-string key rather than replacing it
   outright.

6. **Add the unknown-plate prompt.** Depends on #5. Mirror
   `food_identity`'s ask-and-remember pattern (`apply_to`, `remember`,
   `MIN_SAMPLES_TO_APPLY`) but key it on the geometry-class fingerprint
   instead of a described-name string.

7. **Wire `scale_learning.progress()` into the client and persist graduation
   state.** Depends on #3 (food-sample counts) and #2 (weighed tag for rule
   5c's bias check) to cover all three graduation criteria, not just scale.
   Add a per-user `graduated_at` / `card_needed` column set by a job that
   evaluates all three rules together, and have the scan screen read it
   instead of always showing the card — today's `GET /calibrations/progress`
   already computes the scale third of this and is simply never called
   (§2 row 5a).

8. **Add the pooled layer with a review gate.** Depends on #2 (weighed tag)
   to know which rows are even eligible. Add an opt-in flag on `profiles`
   and a `review_status` column (`pending`/`approved`/`rejected`) on a new
   pooled-candidate table, and change `portion_learning`'s write path so
   nothing reaches it until a row is `approved` — today it writes on every
   qualifying correction with no gate at all.

9. **Seed and version the mother layer from Nutrition5k.** Depends on #8
   existing so "pooled evidence" has somewhere to flow from. Add the
   `dish_metadata_*` loader `docs/HANDOFF.md:829-830` already scopes as
   future work, store its 10 pseudo-examples with a version number the way
   `RESOLVER_VERSION` versions the nutrition cache, and gate any change to
   them on the same review step as #8.

10. **Compute and log the data-quality score.** Depends on #2 and #8 — it is
    computed from exactly the "weighed, complete, consistent" signal those
    two introduce. Add one per-user column populated by a scheduled job
    (never at request time, per Gil's "automatic, logged, never on personal
    attributes" requirement), logging its inputs alongside the score itself.

11. **Automate the free-months grant.** Depends on #10. Add a
    `"data_quality_reward"` value to `entitlements.source` and a scheduled
    job that sets `tier='comped'` after the extra month of record-keeping
    completes, reusing the entitlements table's existing comped/promo path
    (`0008_billing.sql:52-62`) rather than inventing a new billing mechanism.

12. **Turn the heap cap from soft to hard.** Depends on #5 (depth class) to
    have a real volume ceiling to check against. Once a plate's depth class
    exists, replace `SPREAD_FACTOR_MIN/MAX`'s fixed multiplier
    (`portion.py:873`) with a computed ceiling — footprint × the plate's own
    depth — so a heap is refused rather than merely discounted.

13. **Add the meal-source/context field.** No dependency on anything above;
    lowest priority because nothing else needs it. Add a nullable enum
    column on `meals` (`home_cooked` / `meal_prep` / `restaurant` /
    `takeout`), filled by an optional prompt at logging time, mirroring the
    free-text `container` field already asked of the vision model.

14. **Fix recipe privacy default and route matching through the alias
    table.** No dependency on the rest of this list. Flip
    `recipes.is_public`'s default to `false` and add a `moderation_status`
    column in one small migration; separately, change `recipe_ai.py`'s
    ingredient resolution to consult `food_identity.aliases_for` before
    falling back to `resolver.resolve_many`, so a recipe's ingredient names
    benefit from what the user has already taught the app about their own
    food descriptions.

---

## 5. Tests

Ran `pytest -q` from `backend/` (this cloud environment has no Windows
`dev.bat`/PowerShell; `.\dev test` is a thin wrapper around the same pytest
invocation per `docs/HANDOFF.md`'s own tooling notes, so this is the
equivalent check, not a substitute for a different one):

```
942 passed, 2 failed in 55.02s
FAILED tests/test_wiring.py::test_every_bench_case_has_a_photo_and_a_weight
FAILED tests/test_wiring.py::test_the_bench_checks_what_is_free_before_spending_anything
```

This matches the task's own stated expectation ("baseline on main is 944
passed, 0 failed on Gil's machine; in the cloud two photo-dependent failures
are expected") — 942 + 2 = 944, and both failures are the named
photo-dependent ones (the bench photographs referenced by these two tests are
not present in this container).

---

## 6. Not verified / could not check

- Whether a top-level `LICENSE` or `README.md` file (outside `docs/`) carries
  its own Nutrition5k attribution note — this pass searched `docs/` only, per
  the license grep scope actually run.
- The `"tape"` and `"correction"` values of `vessel_observations.source` are
  defined in the schema and used in `scale_learning.py`'s weighting table, but
  no call site that writes `source="tape"` or `source="correction"` was found
  in this pass (only `source="reference_object"`, from `vision.py:1767-1787`).
  Worth a targeted follow-up grep before assuming that path is dead code
  versus simply not exercised by the paths this note walked.
- Whether `RUN_ME_0020_0023.sql`'s duplicate `portion_learning` /
  `vessel_observations` definitions are still applied anywhere, or are a
  stale manual-run artifact now superseded by 0021-0023 — flagged by the
  research pass, not resolved.
- Live Supabase row counts (e.g., whether `portion_learning` or `food_aliases`
  hold any rows today) were not queried — this was a static-code inventory
  only, per the task's read-only/no-network constraint. `docs/HANDOFF.md:2782-2784`
  records both as 0 rows as of 13 Sep; that may have changed since.

---

## For Gil

The single most load-bearing finding: **the target design is not a green
field.** `vessel_observations` / `scale_learning.py` already implements the
USER-layer plate-diameter piece almost exactly as specified, down to framing
the trial period the same way ("not a trial of the app, a period in which the
app measures the user's kitchen," `scale_learning.py:36-37`) — but its output
(`GET /calibrations/progress`) is computed and then never read by the mobile
app. Wiring that one existing endpoint into the scan screen is close to free
and would deliver a working graduation signal for the scale third of rule 5a
before any new table is built. Separately, `mobile/src/components/Rings.tsx:64`
says "kcal over" today, which is a live violation of the new wording rule and
worth fixing independent of everything else in this note.
