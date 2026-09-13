# NeutriAI — handoff brief

## READ FIRST: THE MEASUREMENT STACK AGAINST A LOOKUP TABLE — 12 Sep 2026

The model's typical-serving guess ALONE -- name the food, return a serving --
against everything the pipeline measures. Clean paired re-run, 27 weighed items,
one detection per photo, nutrition facts shared across arms:

    stage                              mean |e|  median |e|  signed   mean |ln|  r on log grams
    serving guess alone (lookup)        56.9%     62.6%     +54.2%    0.425     +0.91
    calibrated, pre-blend geometry      39.2%     23.7%      -4.4%    0.465     +0.65
    calibrated, as shipped              35.9%     20.6%      -3.7%    0.413     +0.70
    subscriber, pre-blend geometry      49.9%     41.8%      +7.7%    0.537     +0.52
    subscriber, as shipped              36.8%     26.8%     +13.7%    0.334     +0.76

- **The lookup RANKS foods by size better than the measurement does** (r 0.91
  against 0.70). That is across-food signal only; it returns the same number
  for 45 g and 180 g of one food.
- **It does not SIZE portions.** It runs +54% heavy, and its median error is
  62.6% against 20.6% calibrated and 26.8% for a subscriber. Its low log error
  is almost all that one consistent bias.
- **For a subscriber, the geometry alone is WORSE than the lookup** on log error
  (0.537 against 0.425) and on correlation (0.52 against 0.91). Only the blend
  toward the lookup brings the shipped answer under it (0.334). Today the
  product's accuracy for a paying user is substantially the serving prior's.

"The whole measurement stack is worse than naming the food" is true of the
SUBSCRIBER'S pre-blend geometry on log error and ranking, and false of the
calibrated stack and of any per-portion error measure.

## LAUNCH BLOCKER: GRAMS DEPEND ON USDA UPTIME — 12 Sep 2026

Ranked on its own, independent of accuracy. Found while re-running the
uncalibrated sweep.

vision.py:1097-1102 says "ONLY a real measured density is passed as explicit."
The line passes `fact["density_g_ml"]` whatever its provenance, and
`density_for` puts an explicit density at precedence rank 1, above the dish
match.

### How an LLM density reaches the grams

- USDA is the only nutrition provider actually configured:
  `nutrition_provider_order` lists usda, nutritionix, edamam, and neither
  Nutritionix nor Edamam has keys.
- `resolver.resolve` returns a cached USDA row directly. A cached `ai_estimate`
  row does NOT short-circuit: the providers are retried on every scan. If USDA
  fails or finds nothing, the resolver returns that cached row -- carrying a
  reasoning-model `density_g_ml` -- or, with no row, falls to the reference
  table (no density) or a fresh `_ai_estimate` (a density).
- So a food's density source is decided at scan time, by whether USDA answered.

### (a) Same photograph, different grams

Photo 30's caesar salad came out 350 g in the first sweep, run during a USDA
outage, and 98 g in the clean re-run -- from the same cached vision response.
Zucchini 88 vs 52 g; rice 151 vs 107 g. That is nondeterminism in the product's
core output, driven by a third party's availability.

### (b) How often it bypasses the density table — measured

- 75 of the 116 `food_facts` rows are `ai_estimate` rows carrying an LLM
  density; the 33 USDA rows carry none. Those 75 are not used on every scan:
  they are the foods ONE USDA FAILURE away from bypassing the table.
- Bench, clean re-run, USDA answering: **2 of 27** scored items took an LLM
  density -- shredded beef with potatoes 0.85 (table 1.05) and pepperoni pizza
  0.85 (table 0.55). The other 25 used the table.
- Bench, first sweep, USDA failing: **at least 4 of 28** (zucchini, both rice
  items, caesar -- the items that moved), and up to 9 hit the failure path.
  Provenance was not recorded, so the exact count is unknown.
- Whenever it fires, everything done to the density table tonight -- the
  whole-word matcher, the valued keys, the bulk-vs-material finding, holding
  carrots and zucchini -- is outranked for that food by an LLM guess.

### The fix — stated, not built

- A provenance field on `food_facts`: measured, sourced table, provider, LLM
  estimate.
- A precedence rule: only a MEASURED density outranks the table. An LLM or
  provider density ranks below the dish match, or does not reach the weight at
  all.
- Each scan item records which density its grams used, so a bench can see it.

### The bleeding: USDA's intermittent 400 — diagnosed 12 Sep, not fixed

The USDA task was first triaged as macros-only because its card said "grams
unaffected". That was wrong (Gil's correction): this is a GRAM-DETERMINISM fix
and is now at the top of the buildable queue, re-scoped on the task itself.

- **Not the key.** Length 40, alphanumeric, no quotes, no padding, no CR, one
  line in backend/.env. No 403s appear in tonight's logs; every failure is an
  nginx HTML "400 Bad Request" page, not an API JSON error.
- **Not the rate limit.** ~3,500 requests remained, and a limit returns 429.
- **Intermittent, and query-dependent.** The same query flipped between 200 and
  400 on consecutive tries.
- **The cause: the `dataType` filter in the GET query string.** Controlled
  probe, 30 requests per shape, alternating:

      app's exact GET, dataType="Foundation,SR Legacy,Survey (FNDDS)"   12/30 failed
      GET, dataType as three repeated params                            18/30 failed
      GET without dataType                                               0/30 failed
      POST, same fields in a JSON body                                   0/30 failed

- **Fix for the task:** send the search as a POST with a JSON body, which keeps
  the filter and failed 0 of 30. Then measure the real steady-state bypass rate
  -- how many scored bench items still take an `ai_estimate` density once USDA
  answers reliably -- which nobody has seen.
- The provenance field and precedence rule above remain SPECIFIED, not built:
  the 400 is the bleeding, provenance is the cure.

## THE SERVING-PRIOR BLEND IS NOW THE ACCURACY CEILING

Found by the paired uncalibrated sweep (full record, and its clean re-run, near
the end of this file).

`estimate_grams` pulls its geometric answer toward the vision model's
typical-serving guess whenever the two disagree by more than 1.5x
(portion.py:2451-2469). How hard it may pull is capped per rung by
`BLEND_MAX_WEIGHT_BY_METHOD` (portion.py:922):

    plate_reference 0.10   reference_object 0.10   depth_model 0.12   multi_image 0.15
    vessel_reference 0.40  pixel_area 0.50          ai_prior 0.50

**Why it is the ceiling.** Clean re-run numbers. On the items the calibrated
pipeline already had within 25%, the uncalibrated path took error from 11.5% to
34.6% (+23.1 points, CI +5.7 to +40.4, n=15); on the rest it went 66.4% ->
39.5% (-26.9, n=12). As the geometry nears the 10% target, that accurate half
becomes the whole bench. Every improvement to footprint, density or height is
partly surrendered to the serving prior on any scan whose scale is ASSUMED --
and a subscriber's scale is always assumed today (31 of 41 items on
`vessel_reference`, 5 on `ai_prior`). The surrender grows as the geometry
improves. (The first sweep read +26.6 / -27.8; see the re-run for why it moved.)

**Mechanical check, with no stratification (Gil's).** Log-error SD fell 0.590
-> 0.416, a ratio of 0.705 (first sweep: 0.650 -> 0.457, 0.703). A full 0.40
pull toward a constant prior scales the SD by 0.60; an uncorrelated scale term
would push the ratio above 1. So 0.70 confirms compression of about the blend's
size.

The raw slope of uncalibrated on calibrated log error is 0.43, below that
floor. **Mostly explained by Gil's explanation 3: the geometry term itself
differs between the arms.** The vessel prior changes the area (200 / 229 / 270
mm), and on this draw the area factor is correlated with the calibrated error
(-0.34), so the raw slope is not a clean estimate of (1 - w). Controlling for
each item's area factor, the slope on the 20 vessel items is **0.566** -- close
to the 0.60 floor, still below it. The small remainder is consistent with a
prior that ranks foods by size (r 0.91), which a constant-prior floor does not
allow for. (First sweep: 0.597 controlled, correlation -0.16.)

**Is the serving guess real signal? Tested directly; the answer splits.**

    serving guess vs weighed mass, 28 items    Pearson r +0.91 on log grams, Spearman +0.88
    calibrated geometry vs weighed mass        Pearson r +0.69
    mean |ln error|   serving guess 0.423      calibrated geometry 0.462
    correlation of the two errors              -0.24: they err in opposite directions,
                                               which is why averaging them helps

- ACROSS foods the guess is strong: it knows chips are ~25 g and spaghetti ~250.
- WITHIN a food it carries no portion information. The same weight photographed
  twice: tortilla chips 28 g / 28 g (weighed 25); grapes 150 g / 150 g (weighed
  90); trail mix 50 g / 30 g (weighed 44), and that moved only because the NAME
  moved, "mixed nuts and dried fruits" vs "trail mix". It returns a serving
  size for the food, not a reading of the plate. Nine distinct values across
  28 items: 28, 30, 50, 60, 85, 150, 200, 250, 300.

So the blend is defensible as a food-type prior and indefensible as a portion
measurement. A portion that departs from typical is pulled back toward
typical in proportion to the weight -- the ceiling argument in its sharpest
form. n = 3 same-weight pairs; the pattern is structural (a serving table), not
a sampling accident.

**The design question -- stated, not answered. Do NOT change any blend weight.**
The weight already follows the rung, and the rung encodes whether the SCALE was
measured. But the pull is applied to the final GRAMS, so it discounts every
component of the estimate -- scale, footprint, height, density -- in proportion
to how uncertain the scale alone is. The question is whether the prior should
correct only the term it has information about:

- (a) the pull acts on the SCALE term -- a prior over the vessel's size or the
  frame area, weighted by the scale's uncertainty -- and footprint, height and
  density stand on their own evidence; or
- (b) it stays on grams, but its weight is set by the estimate's TOTAL
  uncertainty and falls as each non-scale term becomes measured, rather than
  being fixed by the rung.

"Turn it down" is not an answer: on today's geometry the pull is what rescues
the wild half (78.2% -> 50.4%).

---

> Imported into the repo on 12 Sep 2026 from `~/Downloads/HANDOFF-claude-code.md`,
> which sat outside the project and would not have survived the session.
> Verbatim below the line except for this header.
>
> **Checked against the repo on 12 Sep. Corrections, in the brief's own spirit:**
>
> - "Test suite green: 817 passed, 0 failed" -- TRUE WHEN WRITTEN, and stale by
>   one change rather than wrong. The suite was red on arrival (`test_wiring`,
>   on a dead `STEPS` constant in `scripts/box_replay.py`), but 817 + the 6
>   dump tests added afterwards = 823, exactly what was collected. The mask-dump
>   work landed between the brief and the next session: it added those 6 tests
>   and refactored the perturbation loop onto its `reach` parameter, orphaning
>   `STEPS` and tripping the dead-constant check. Nothing regressed in the work
>   the brief describes. Now 825/0.
> - "Then the structural fix -- have the pipeline emit its own masks" -- already
>   built when this session opened: `dump_masks` / `load_mask_dump`,
>   `dev boxreplay --dump`, and `dev api` setting NUTRIAI_MASK_DUMP.
> - The mask sets ARE different, now measured rather than deduced. Production
>   digest `2bce2dc323bf`; `mask_stability`'s cache `199239e25505b5e3`. Same
>   shape (1568x1176), so counts compare directly: 15 of 16 masks agree within
>   0.7%, production has a 22,678 px mask the cache lacks, and the cache has a
>   146,388 px one production lacks.
> - The box-translation table under "Established from production logs" is
>   REPRODUCED, in `evidence/2026-09-12-photo35-box-intermittency.txt`.
>
> See also: the topology inversion, which is a separate deterministic defect
> and is recorded at the end of this file.

You are picking up an AI food-scanning nutrition app mid-investigation. Repo:
`C:\Users\VetaM\Downloads\nutriai\nutriai` (Windows, portrait phone photos, React
Native/Expo + FastAPI + Supabase + Claude/GPT-4o vision + SAM2 via Replicate).

**Verify everything in this brief against the repo before acting on it.** Most of what
follows was established by measurement, but several confident-sounding claims in this
project have been refuted — eight of them in the last session alone — and the list of
what was refuted is as important as the list of what holds.

---

## How Gil wants to be worked with

- **No guessing.** Check, verify, fix. Do not assume a passing test isn't itself buggy.
- **Be assertive.** Make decisions and guide; don't hand back a menu.
- **Check for bugs after every build.**
- **Never print secret values back to him** — report length and prefix only. His USDA key
  has already leaked into chat logs twice and needs rotating.
- **Don't waste money.** Every bench photo is a paid model call.
- **Don't make him hunt.** Give exact commands; don't describe a thing he then has to
  translate.
- Prose instructions must never sit inside a block of runnable commands — he pastes
  blocks wholesale, and a stray line becomes a command.

---

## The goal

Weigh food from a photo accurately enough for weight control. Target **10% per-item
error or better**. Current: **~48% per item**. Scope is "subscriptions live and taking
money"; the stated trade-off is *"Accuracy — it is the product."*

The app must work out scale **itself** — it may not ask the user for plate size.

---

## Current state

- Test suite **green: 817 passed, 0 failed**. A `conftest.py` autouse fixture pins
  `food_seg._SEGMENTER`, `vision.DEPTH_PROVIDER` and the two settings they are built
  from, so no test reaches a live provider. Before this, `dev test` was making live
  paid Replicate calls and the suite was nondeterministic.
- One commit landed this session. **`mask_overlays/` was committed by accident** (64
  files, 23 `.npz`, 27 under `plate/`). Fix with `git reset --soft HEAD~1`,
  `git reset mask_overlays/`, recommit — *not* `rm --cached`, which leaves the blobs in
  history. No remote is configured, so this is safe.
- Full bench run: 26 of 27 photos scored, **48.2% on the 20 photos that overlap the old
  49.2% baseline**. Unchanged. The bench's own confidence interval is **23–80%**, so
  nothing smaller than about a 20-point move is detectable on it.

---

## The investigation, and exactly where it stopped

**The question:** photo `35-slider-fries-plate` produces wildly different grams on
repeated scans of the same photograph — fries 14–35 g, a 2.5× swing. Find out why.

### Established from production logs — not from any replay, so untainted

```
box translates by EXACTLY +1.00 and -2.00 grid steps; size never changes
  run A  (176, 784, 529, 1097)   353 x 313   union 0.0561   7 masks
  run B  (235, 784, 588, 1097)   353 x 313   union 0.0436   6 masks
  run C  (176, 627, 529,  940)   353 x 313   union 0.0458   6 masks
```

- The model quantises all geometry to a **0.05 grid** — the bench reports 82% of values
  landing there against 20% by chance, and says *"one step of that grid is 45% of the
  food's weight."*
- All three runs shared **one** SAM2 mask set (the memo, below), so the union moved from
  **box translation alone against a fixed input**. That is a clean natural experiment,
  not an inference.
- **Union moves 1.29×. Grams move 2.5×.** Box quantisation is real and accounts for about
  a fifth of the problem. **The amplification is downstream of the footprint and is not
  yet identified.** This is the open question.
- The fragment guard refused the burger's footprint **identically in all three runs** —
  so guard flipping is not what moved it here.
- SAM2 is **deterministic for identical bytes**: three cold instances, two photos,
  `IoU 1.0000` min and median on every pair.

### The scale finding, independent of the above

```
plate_box_vs_circle   box_area=0.362   circle_area=0.5396   iou=0.661
```

The Hough circle traces the plate rim exactly (confirmed on the overlay). **The model's
`plate_area_ratio` is 1.49× too small**, and that single error accounts for photo 35's
entire +45.8%: re-running the same scan with the circle's measured area gives **−2.2%**.

Verified: `prompts.py:130` asks for the fraction of the image covered by the vessel,
which is the same quantity `circle.mean()` measures; the consumer at
`portion.py:1504` divides a disc area by it, with no tilt term.

### And the reference card

```
35-slider-fries-plate  (no card)   +45.8%   spread 218 g
36-slider-fries-card   (card)      -18.1%   spread  27 g
```

Replicated across two independent bench runs. **Scale is where the error lives**;
everything downstream behaves once mm-per-pixel is real.

---

## DO NOT RETRY THESE — all refuted by measurement

1. **"The plate reordering caused the five `test_refine` failures."** Refuted by a
   pristine-vs-patched run: identical in all four combinations. The real cause was live
   Replicate calls from `.env`.
2. **"mm-per-pixel rests on the Hough circle."** False. `"circle"` is excluded from
   `MEASURED_PLATE_SOURCES` by design; it supplies location only.
3. **"The device bridge strips CRs from `dev.bat`."** False — that was a stale staging
   path serving cached bytes.
4. **"`test_portion` has a flake."** False — those were zombie entries in pytest's
   `lastfailed` cache from renamed tests. `lastfailed` is an accumulator, not a run record.
5. **"Sample SAM2 N times and take the median footprint."** Pointless — SAM2 is
   deterministic (IoU 1.0000). Nothing to average.
6. **"Drift scales with grid step over box size."** Held beautifully on synthetic boxes,
   **refuted on production boxes**: box ≤8% of frame is 33% unstable, >8% is 38%, and the
   worst case in the set (4.0×) has a 25% box.
7. **"The fragment guard flips between refused and accepted across runs."** Not operating
   on photo 35 — refused identically three times.
8. **"The Hough circle is oversized and needs a shrink guard."** Refuted by the overlay:
   the circle is correct and the model's ratio is wrong.
9. **"Use the UNION'S MASK COUNT for the height branch -- multi-mask means
   separate pieces, single-mask means one mass."** Refuted offline on 12 Sep,
   zero model calls, three independent ways.

   *Mechanically, and this alone is decisive:* on photo 35 the burger unions
   **3** masks and the fries **6**. Both are multi-mask, so the predictor does
   not separate them at all. The burger is one mass whose SAM2 mask arrived in
   three fragments -- which is the same fragmentation that makes its
   `piece_share` 0.52. Mask count inherits the identical defect and adds
   nothing; it is a coarser reading of the same broken correspondence.

   *Quantitatively:* it routes the fries from the branch scoring -56.9% to the
   one scoring -79.5% (-77.7% to -81.1% across prior-pull exponents 0.8-1.0).
   Per-item mean absolute error on photo 35 goes 76.8% -> 88.1%, +11.3 points.
   The burger is unaffected -- its footprint is refused, so no height branch
   runs for it whatever the predictor says.

   *Independently:* that predicted -79.5% lands within 3 points of the -76.8%
   `NEXT-SESSION.md` already records for fries on the separate-pieces branch,
   which both validates the arithmetic and IS the refutation.

   NOT TESTED, because the mechanical argument makes it unnecessary: chips,
   trail mix, grapes, pot roast and chicken have no production mask counts
   anywhere in the repo. `docs/evidence/` holds photo 35 only -- four request
   ids, two items. Getting the rest is a full bench run with
   `portion_height_branch` live, 27 calls, and it would be buying data to
   settle a question the burger already settles for nothing.

---

10. **"The plate ratio itself is moving run to run, and that is part of the
    gram swing."** Refuted for the pair compared: `box_area` was 0.362 in both
    invocations, so it cannot explain a difference between them.

    Two refinements, so this is not over-read. Across the FIVE requests in
    `evidence/2026-09-12-photo35-box-intermittency.txt` `box_area` takes two
    values, 0.415 once and 0.362 three times -- a 14.6% swing. So the
    box-derived plate ellipse is not stable in general; it simply did not move
    between the two runs in question. And `box_area` is a DIAGNOSTIC, not an
    input: the scale consumes `plate_area_ratio`. What is genuinely stable is
    the measurement -- `circle_area` is 0.5396 in all five, and the local
    decode's own Hough circle matches production's plate hint at IoU 1.000.
    That stability is the argument for ranked work #1, and it is a stronger
    one than the magnitude was.

## CONFIRMED 12 Sep 2026 -- the height flip is the amplifier

Both arms are now in the log, same photograph, same food:

    piece_share 0.8509  ->  one_mass True   ->  21.0 mm  ->  28.0 g
    piece_share 0.7052  ->  one_mass False  ->   9.2 mm  ->  11.8 g

The chain is complete and every link measured: box translates on the 0.05 grid
-> a different subset of one fixed mask set unions -> `piece_share` moves ->
it crosses `ONE_PIECE_SHARE` -> the height steps 2.28x -> grams move about
2.4x. Area alone gives 1.26x and never accounted for it.

**IT WAS REPORTED DEAD, AND THE REPORT WAS A SAMPLING ERROR. THE THIRD OF
THESE.** The three runs sampled all sat at 0.8509 and never crossed, so
"the branch does not flip" was a statement about three draws from one box
position, not about the branch. Same shape as `box_replay` measuring the cache
and calling it production, and same shape as the count-match that admitted
photo 35 to `UNSTABLE`. The pattern: A FIXED SAMPLE OBSERVED THREE TIMES IS
NOT A DISTRIBUTION, and with the memo warm `--runs N` holds the mask set fixed
BY CONSTRUCTION.

(Recorded as read from the API log by Gil; the 0.7052 line is not in any
artifact committed here, because that run's output was not redirected. The
0.8509 arm is in `evidence/2026-09-12-photo35-box-intermittency.txt`.)

## The hypothesis as it was written, before it was confirmed

**A binary height switch, flipped by the union's connected-component topology.**

```
CONNECTED_PILE_HEIGHT_MM  = 21.0
SEPARATE_PIECES_HEIGHT_MM =  9.2      ratio 2.28x
ONE_PIECE_SHARE           = 0.80      the threshold
```

Grams move as `geometry^0.9` (the bench pulls geometrically toward the prior — `9 g` and
`150 g` at 16.1× disagreement, pulled 10%, landing at `12 g = 9 × 16.1^0.1`).

```
area alone          1.29^0.9           = 1.26x      observed: grams moved 2.5x
area + height flip  (1.29 x 2.28)^0.9  = 2.63x
```

Area alone explains a fifth. Area times **one** height-branch flip lands on the observed
value. It was reported dead, then that report was withdrawn — see below.

---

## THE BLOCKER — read this before running anything

**`scripts/box_replay.py`'s numbers are all invalid**, and so is the report that the
height branch doesn't flip.

The replay ran against masks cached by `scripts/mask_stability.py`. Those are **not the
masks production used**. Proof needing no further call: the selection rule in
`segment_hosted.py:_union_in_box` is purely per-mask (every term depends on one mask and
the box; `union |= m` is order-independent), so the number taken is **monotone in the
candidate pool**. Production unioned over 14 masks and took 7; the replay unions over a
16-mask superset and takes 6. A superset cannot yield fewer takes — therefore the mask
sets differ.

A per-mask verdict table confirmed no rule divergence: every drop is emphatic (centroids
hundreds of pixels outside the box, or a mask 7.5× the box area), nothing within 0.15 of
a threshold.

**Likely cause:** production uploads the photo to Supabase, fetches it back, decodes and
re-encodes through the scan path; `mask_stability` encodes from the local file. Different
bytes, and SAM2 is only deterministic for *identical* bytes.

### The immediate next action — one paid call

The `sam2_auto_masks` log line has been extended with sorted mask areas and a digest
prefix. **It fires only on a memo miss**, so the API process must be restarted first or
the run logs nothing and looks like broken code.

```
window 1:  check for "Started server process" after the last segment_hosted.py commit
           if absent:  Ctrl-C, then  dev api > survey-api-log.txt 2>&1
window 2:  .\dev benchall --runs 1 --only 35
```

Compare against `mask_stability`'s encode of the local file: digest `199239e25505b5e3`,
and the cached 16 areas `1711, 3905, 5453, 9089, 11960, 11962, 12920, 12925, 14531,
19828, 20488, 21800, 23195, 36180, 146388, 828141` at shape `(1568, 1176)`.

**Compare area *fractions*, not raw pixel counts, and log the shape.** If the round-trip
decodes at a different size, every area scales with it and identical masks would read as
different ones.

- fractions match, digest differs → the round-trip is the cause; the masks are fine
- fractions differ → genuinely different masks; stop and investigate before writing code

### Then the structural fix

**Have the pipeline emit its own masks** — a dump of the post-`plate_hint_bound` set,
keyed by the digest actually used, written when the bench asks for it. Do **not** fix
this by teaching `mask_stability` to replicate the upload/fetch/re-encode route: that
fixes the instance and leaves the class intact, and the next route change breaks it again
silently.

With a faithful dump, the height hypothesis becomes testable: rebuild the three real
unions, compute `largest_piece_share` for each, and see whether it crosses `0.80`.

---

## Ranked work, once the instrument is trustworthy

1. **`vision.py:1573`** — feed the scale the measured circle area instead of the model's
   `plate_area_ratio`. One line. Accept on the bench's **0.05-grid resolution figure
   collapsing from 82% toward chance at 20%**, *not* on the headline, which the 23–80%
   interval cannot resolve. Note: `"circle"` must stay out of `MEASURED_PLATE_SOURCES` —
   a Hough circle has axis ratio 1.0 by construction and can never report tilt. It
   supplies **area**, never shape.
2. **Camera intrinsics and depth at capture.** ARKit / ARCore Depth give metric depth and
   focal length at the shutter; mm-per-pixel falls straight out, with no user input. The
   rungs already exist and none are connected: `mobile/src/native/depth.ts`,
   `depth_map.py` / `DEPTH_PROVIDER` (currently `available() is False`),
   `0016_camera_geometry.sql`, `portion.py:1590-1618`, and a camera-distance field in the
   bench's `CASES`.
3. **One-time vessel calibration** as the fallback for phones without depth.
   `0017_calibration_vessel.sql` and `0018_self_calibration.sql` are written and unrun.

**Do not tune constants.** Nothing is measurable until the instrument is fixed.

---

## Tooling facts that will save you hours

- **`_auto_masks` is memoised** on `sha256(image)`, single-slot, per `HostedSegmenter`
  instance (`segment_hosted.py:431`). `_SEGMENTER` is a process singleton, so all N runs
  of one photo in `--runs N` share **one** SAM2 call. `sam2_auto_masks` fires only on a
  miss. This means `--runs N` measures the **vision model**, not the segmenter — and any
  change to the memo's lifetime or key silently changes what the bench measures.
- **`bench_all.py` now refreshes its token** mid-run. Before the fix it fetched one token
  at startup and a ~70-minute run outlived it, losing the last four photos to
  `"exp" claim timestamp check failed`.
- **Output is line-buffered now.** Before, `dev benchall > file` left the file at 0 bytes
  for the entire run, so a healthy run and a hang looked identical. This cost two aborted
  runs and about an hour.
- **The API's own logs never reach the bench's stdout** — `plate_box_vs_circle`,
  `plate_hint_bound`, `sam2_box_union` all live in the API process. Start it as
  `dev api > survey-api-log.txt 2>&1` or the evidence is lost.
- Useful routes: `dev test`, `dev benchall --runs N [--only 35 36]`, `dev maskstability`,
  `dev boxreplay`, `dev platecheck`, `dev segcheck`, `dev api`.
- `test_wiring.py` requires **every** script in `scripts/` to be reachable from
  `dev.bat` — a new script with no route turns the suite red.
- Gil is usually in **PowerShell**, which needs `.\dev` and whose `>` writes UTF-16.
  For anything redirected to a file, use `cmd /c "dev ... > file.txt 2>&1"`.

---

## Launch blockers, unrelated to accuracy

- **Grams depend on USDA uptime** (added 12 Sep). An LLM-estimated density
  outranks the density table whenever USDA fails; same photo, 350 g vs 98 g.
  See "LAUNCH BLOCKER: GRAMS DEPEND ON USDA UPTIME" at the top of this file.
- ~~Supabase migrations **0017 and 0018** unrun.~~ STALE, checked 12 Sep: the
  live tables already carry 0017's and 0018's columns
  (`scan_calibrations.vessel/samples/learned`, `food_scans.vessel`) and
  0020-0023's (`width_error_pct`, `observations`, `vessel_observations`,
  `portion_learning`).
- Apple JWS signature verification, a live Stripe key, and privacy / support / terms URLs
  before store submission.
- `conftest.py`'s docstring claims "NO TEST TALKS TO A NETWORK PROVIDER" but pins only the
  segmenter and depth model — Supabase is not covered. The claim is wider than its
  mechanism.
- The USDA key has been exposed twice and should be rotated.

---

## THE TOPOLOGY INVERSION — found 12 Sep 2026, deterministic, not a variance bug

Separate from everything above. This one does not need a box to move and does
not need two runs to see. It is wrong the same way every time.

    food     piece_share   ONE_PIECE_SHARE=0.80   reality              applied?
    burger      0.5185     -> separate pieces     it is ONE MASS       no
    fries       0.8509     -> one mass  21.0 mm   they are SEPARATE    YES

Both foods are classified as their opposite, on the same photograph, in all
three runs of 12 Sep. `portion_height_branch` in
`evidence/2026-09-12-photo35-box-intermittency.txt` has the raw lines.

**BUT THE INVERSION IS NOT COSTING GRAMS ON THIS PHOTOGRAPH -- IT IS PAYING
THEM.** The fries score -56.9% on the connected-pile branch they were wrongly
given, and -79.5% on the separate-pieces branch their geometry says they
deserve. Their label is geometrically wrong and physically RIGHT, by accident:
fries ARE separate pieces and they DO heap, and `SEPARATE_PIECES_HEIGHT_MM`
asserts that pieces cannot stack. `NEXT-SESSION.md` says so outright -- "true
of carrot coins, FALSE of fries (-76.8%) and lettuce (+198%)".

So there are two defects here and they must not be conflated:

- **the measure** -- union connectivity is not food topology (below). A latent
  hazard: it decides nothing on the burger because the area guard refused that
  footprint first, and it happens to decide the fries the lucky way.
- **the mapping** -- two heights cannot describe food that is separate AND
  heaps. A real modelling error, and no improvement to the measure touches it.

  **This was written as "where the fries bucket actually lives". That is WRONG,
  refuted by the implied-height table below on the same day.** No height in the
  table reaches the fries, so the mapping is not their lever either. Photos
  40-45 (three weighed spread/heaped pairs) remain worth measuring for the
  heap-vs-layer question in its own right, but not as the fries' fix.

### Backing out the height the weighed grams require

Free, zero calls. `implied h = h_used x (weighed / geometry_grams)` -- exact,
because geometry grams are linear in height, so density and shape cancel.
Photo 35 fries: footprint 4.36% of frame, its own box 5.99%, mask filling 73%
of that box, 21.0 mm used, geometry 34 g, weighed 65 g.

    scenario                                    geom_g   h for 65 g
    as it ran (estimator scale, mask as is)       34.0      40.1 mm
    estimator scale, mask fills its whole box     46.7      29.2 mm
    MEASURED scale (circle), mask as is           22.8      59.8 mm
    MEASURED scale, mask fills its whole box      31.4      43.5 mm

    the table:  SEPARATE_PIECES 9.2   SOUP_DEPTH 15.0   CONNECTED_PILE 21.0
    tallest entry anywhere: "wrapped" 42.0, for burritos, not loose food

**Every scenario needs more than 21.0 mm -- the tallest loose-food height there
is.** The best case available, a correct scale and a perfect mask filling the
model's entire bounding box, still needs 43.5 mm (31.6 mm by an independent
bulk-density route at 0.45 g/cm3; both well over the ceiling). So the height
branch is not the fries' problem and cannot be made into their solution.

The deficit is in the FOOTPRINT or the SCALE. What is established: the mask
fills 73% of its box, and closing even that gap entirely is not enough. What is
NOT established, and should not be guessed at from one weighed item: whether
the model's box for the fries is itself undersized, whether the bulk density
for loose fries is wrong, or whether the plate scale is off in a way the circle
does not capture.

### And the scale fix does not rescue this item

**THE 1.49x FACTOR WAS WRONG AND THE 51.6% PROJECTION IS WITHDRAWN.** 1.49
compared the circle to `box_area` -- the box-derived ellipse `plate_box_vs_circle`
logs for diagnosis -- and NOT to `detection["plate_area_ratio"]`, which is the
quantity the scale actually consumes and which the fix replaces. They are
different numbers about the same plate and only one of them is an input.

`plate_area_ratio_measured` reports model=0.45 against measured=0.5396, so the
real factor is **1.199x** (read from the log by Gil; not in a committed
artifact here).

    f      source of f                 burger    fries     meal   per-item
    1.000  unchanged (today)           +96.8%   -56.9%   +49.2%     76.8%
    1.199  0.5396 / 0.45   the INPUT   +64.1%   -64.1%   +24.4%     64.1%
    1.491  0.5396 / 0.362  box_area    +32.0%   -71.1%    +0.1%     51.5%  <- WRONG

So the honest projection is **per-item 76.8% -> 64.1%, a 12.7-point
improvement**, and the meal lands at +24.4% rather than the +0.1% that made it
look like a cure. First-order: this ignores the blend against priors and the
band clamps.

The direction of the earlier finding survives. The fries still get WORSE,
-56.9% to -64.1%, because the two items are wrong in opposite directions and a
scale correction moves both the same way. Do the fix; it is worth 12.7 points
and it replaces a number the model guesses with one the rim measures. Do not
quote 25 points, and do not expect it to touch the fries bucket.

**Only the fries' inversion reaches the grams, and the brief's first telling of
this was wrong on that point.** `estimate_grams` reaches the topology branch
only at `elif measured_used and largest_piece_share is not None`, and the
burger's footprint was REFUSED before that -- 1.4% of the frame inside a box of
9%, 6.7x under `MEASURED_UNDER_BOX_LIMIT`. Its `measured_area_used` is None and
its 0.52 decided nothing. Reported next to the fries' 0.85 the two looked like
one finding; they are one cause with two different fates.

### Why it misfires

`food_seg.largest_piece_share` measures the connectivity of the UNION MASK, not
of the food. The two coincide only when the mask is both COMPLETE and CORRECTLY
SEPARATED, and photo 35 breaks each of those in a different direction:

- **burger** -- three disconnected fragments of one bun, so the largest is half
  the union and the share reads 0.52. The mask is incomplete, so its topology
  is noise about the food. The fragment guard catches the AREA; nothing checks
  the TOPOLOGY, and the two are gated separately.
- **fries** -- six masks of fries that touch, merging into one blob, so the
  share reads 0.85. The mask is complete and UNDER-separated. Nothing here is
  out of range, so no guard fires and the wrong height is used with confidence.

A single number cannot distinguish "one mass" from "a fragment of one mass"
from "several pieces that touch". It is being asked a question it cannot
answer.

### What it costs

`CONNECTED_PILE_HEIGHT_MM` 21.0 against `SEPARATE_PIECES_HEIGHT_MM` 9.2 is
2.28x on height and about 2.06x on grams (`^0.9`), on every scan where it
misfires. The fries scored -56.9% on the connected-pile branch here.

### Corroboration, and one thing NOT verified

`NEXT-SESSION.md` under *Next, in order* records fries at **-76.8%**, which is
the SEPARATE_PIECES branch -- the same food on the OTHER branch in an earlier
bench. So the classification does move between runs, from an independent
source, and this is not one photograph's accident.

VERIFIED, and free -- it is in the BENCH'S PRINTED OUTPUT, not in the API log,
which is why it could not be found there (all four request ids in that log are
photo 35). From one `--only 35 36` invocation:

    photo 36   note: "fried french fries: measured as separate pieces rather
                      than one mass, so it is a single layer -- pieces on a
                      plate cannot stack"
    photo 35   no such note; piece_share 0.8509 -> one_mass TRUE

Same fries, same meal, opposite classification, both in one run's output. The
note is emitted only on the `not one_mass` branch, so its presence and absence
are the classification. No model call needed to see this.

### Do not fix by moving ONE_PIECE_SHARE

0.5185 and 0.8509 sit either side of 0.80, so no threshold separates these two
cases -- they are inverted, not merely misplaced. Moving it trades one wrong
answer for the other, and that closes off the entire class of fix anyone would
reach for first. `largest_piece_share` is a FAITHFUL measure of what it
measures; photo 35 breaks its correspondence to the food in both directions at
once, and no constant can straddle that.

So a real fix has to be one of two things:

1. **The measure describes the wrong object.** It answers "did the segmenter
   return one blob or several", and the question asked of it is "is this food
   one mass or separate pieces". Those need not agree, and on this photograph
   neither of them does.
2. **The two gates stop being independent.** `MEASURED_UNDER_BOX_LIMIT` guards
   the AREA and the topology branch guards the HEIGHT, separately -- so a mask
   good enough to pass neither, one, or both can still reach a height decision
   on a topology nobody vouched for. A fragmented mask's connectivity is not
   evidence about the food, and the area guard already knows it is fragmented.


---

## THE HEIGHT CONSTANTS ARE FROZEN — and why, 12 Sep 2026

`CONNECTED_PILE_HEIGHT_MM = 21.0` and `SEPARATE_PIECES_HEIGHT_MM = 9.2` are not
to be re-fitted, and not to be replaced by a ramp, until the segmenter is
settled. Three reasons, in increasing order of force.

### 1. They were fitted on the mask cache

`height_fit.masks_for` reads `mask_overlays/masks-<stem>.npz` when it exists,
and `portion.py`'s own provenance note says these heights were "solved against
measured areas". Those areas came from that cache -- and the cache is provably
not production's mask set: different digest, 15 of 16 masks agreeing only to
within 0.7%, and one mask swapped outright (22,678 px against 146,388 px).

### 2. AND AT THE WRONG RESOLUTION, WHICH IS THE STRONGER FINDING

`height_fit` decodes at longest edge **1280**. The scan path decodes at
**1568**, in all three places it decodes. So even `height_fit`'s FRESH masks --
the ones it pays for when the cache misses -- were never production's, at a
size production never uses.

SAM2's automatic generator samples a `points_per_side` grid OVER THE IMAGE, so
resolution changes the mask set directly. Re-encoding at the same size already
swaps one mask of sixteen; 1280 against 1568 is a far larger perturbation.

The cache on disk carries FOUR resolutions under one naming scheme:

    17-34  the photographs the constants were fitted on   1280x960 / 960x1280
    41-45  the weighed spread/heaped pairs                  640x480 / 480x640
    35     slider-fries                                    1568x1176

### 3. A LIVE BUG: TWO WRITERS, ONE FILENAME

`height_fit` writes and reads `masks-<stem>.npz` at 1280. `mask_stability`
wrote `masks-<stem>.npz` at 1568 -- deliberately, commented "AND UNDER THE NAME
THE REPLAY READS". `box_replay` read whichever was there. Same name, different
segmentation, last writer wins, no error anywhere. That is why photo 35's entry
is the only 1568 file in the set: `mask_stability` overwrote it today, under the
name `height_fit` reads at 1280.

**Fixed 12 Sep.** `scripts/_mask_cache.py` gives each writer a namespace,
`<writer>-<stem>-le<long_edge>.npz`, and records `writer`, `long_edge` and
`shape` INSIDE each file. `load` refuses a file whose recorded long edge is not
the one asked for. Un-namespaced `masks-*.npz` files are no longer read at all:
they carry no writer and no resolution, so what they hold is unknowable, and
they are reported by name with `--dump` offered instead.

### What this means for the numbers

The heights were solved to make `measured_area x height ~ weighed grams`, so
**they absorb whatever bias the cache's areas carried**. The pair was
internally consistent for the cache and is being applied to production areas.
That is a live candidate for why photo 35's fries need 43.5 mm: if production's
footprints are systematically smaller than the 1280 footprints the heights were
fitted against, the fitted heights are short by the same factor.

### Why not re-fit now, even though dumps make it possible

The heights are a function of the SEGMENTER'S OUTPUT. If the segmenter changes
-- and the burger finding below says it should -- every footprint changes and
the re-fit is discarded. Settle the segmenter first, then re-fit once, from
pipeline-emitted dumps at production resolution.

---

## THE BINDING PROBLEM: composite foods are unmeasurable by this path

`dev dumpmask` on production's own dump for photo 35:

    the model's burger box          9.00% of frame
    largest mask anywhere in pool   36,174 px = 1.96% of frame
    the three masks taken            1.35% of frame after overlap

SAM2's automatic generator never produced a whole-burger mask. The shrink guard
refusing that footprint is catching a REAL SEGMENTATION FAILURE, not misfiring,
and this explains "9 live, 12 shadow" better than any tuning hypothesis.

`meta/sam-2`'s published inputs, read from the schema on 12 Sep:

    image, points_per_side, pred_iou_thresh, stability_score_thresh, use_m2m

**No prompt field of any kind.** It cannot be asked for the burger. A promptable
model has never been chosen; it has been an open item since the first brief.

### Candidates that take a BOX, from schema interrogation (free, no predictions)

    vufinder/sam3            prompts: array of per-concept JSON with text,
                             positive_boxes, negative_boxes, positive_points,
                             negative_points. All normalised [0,1]. Boxes are
                             center_x, center_y, width, height. 35,574 runs.
    casia-iva-lab/fastsam    box_prompt "[x,y,w,h]", also text_prompt and
                             point_prompt "[[x1,y1],[x2,y2]]"
    datong-new/sam-point     input_box (string, no published description),
                             input_points. 153,945 runs.

Text-only, no box: `schananas/grounded_sam` (`mask_prompt`,
`negative_mask_prompt`), `tmappdev/lang-segment-anything` (`text_prompt`).

**Two detector traps, both now tested against.** `crop_n_points_downscale_factor`
and `crop_n_layers` are the automatic generator's tiling parameters, and the
first survey reported three models as PROMPTED on that field alone.
`box_nms_thresh` and `min_mask_region_area` are NMS and post-processing, not box
prompts. A real prompt carries COORDINATES.

`dev sam3probe` hands sam3 photo 35 and the model's own burger box, outside the
pipeline. Dry by default. The test is one number: a mask near 9% of frame makes
composite foods measurable; 1-2% means the ceiling is real and the design has to
work around composites rather than through them.

---

## sam3, measured — 12 Sep 2026. Four predictions, all archived in evidence/

### A PREDICTION MISS, RECORDED AS ONE

The fries footprint was called at **10.2%** and came back at **13.09%** — right
direction, right order of magnitude, **wrong by 28%**. That is a miss, not a
confirmation, and it is written here as a miss because a prediction that is
"basically right" is exactly how the refuted claims in this file's own list got
their confidence.

### What sam3 returns, and what it does not

    photo  concept              detections   union      verdict
    35     cheeseburger                  1   10.08%     whole burger, clean
    35     french fries                 14   13.09%     per-fry instances
    23     brussels sprouts              5    1.99%     4-5 sprouts, correct
    30     croutons                     14    4.54%     every visible crouton
    30     romaine lettuce               0    EMPTY
    30     parmesan                      0    EMPTY
    30     caesar salad                  0    EMPTY

Empty means literally `{"boxes": [], "scores": [], "masks": []}` — 40 bytes, no
low-confidence entries filtered out. A clean null.

**THE INSTANCE-VS-DISH READ WAS WRONG.** It predicted that ingredient nouns
would work where the dish name failed. `croutons` did. `romaine lettuce` and
`parmesan` did not, and both are ingredient nouns. The line is not dish versus
ingredient, it is:

    DISCRETE SEPARABLE OBJECTS      croutons, fries, sprouts, a burger    found
    AMORPHOUS OR CONTINUOUS MATTER  leaves, shreds, sauce, a whole dish   empty

That is a worse result than the first read, because the foods it fails on are
the leafy and shredded ones — precisely the ones a footprint was always going
to be hardest for, and 123 g of caesar salad is mostly the lettuce it cannot
see.

Four concepts cost ONE prediction: sam3's `prompts` is an array and it returns
one result file per prompt, positionally. So a scan carrying one prompt per
detected food is the same call count as SAM2's one call per photograph —
**replacing is not more expensive than supplementing, and cost cannot be the
reason to choose.**

### THE FOOTPRINT IS THE DOMINANT ERROR, AND IT IS UPSTREAM OF EVERYTHING TUNED SO FAR

    item     production measured   sam3      ratio    the model's own box
    burger              1.35%     10.08%     7.5x     9.00%
    fries               4.36%     13.09%     3.0x     5.99%

Both footprints are several times too small, and sam3's burger mask (10.08%)
EXCEEDS the model's whole bounding box for it (9.00%). The vision model's box
is 53% of the burger's true extent at IoU 0.414 — it misses 159 px of the top
and 209 px of the right while overhanging 67 px onto the plate. **No
box-prompted segmenter constrained to that box can return the burger, however
good it is.** SAM2's 1.35% was never only SAM2's fault, and "composite foods are
structurally unmeasurable" was too pessimistic: they are measurable, the box is
wrong.

### AND THE THIRD "FITTED AS A PAIR" TRAP — DENSITY, SHAPE AND AREA COMPOSE

Backing the fries height out three ways gives three answers, and the spread is
NOT noise:

    footprint 13.09% of a 763.3 cm2 frame            = 99.9 cm2
    65 g at density 0.60, no shape factor            = 10.8 mm
    65 g at the pipeline's REAL 0.62 x ~0.65 shape    = 16.1 mm
    65 g at the potato row 0.59, no shape factor     = 11.0 mm

The 10.8 and the 16.0 quoted earlier differ by exactly the SHAPE FACTOR
(0.60 / 0.406 = 1.48 = 16.0 / 10.8), not by the density.

**Which density the fries actually used:** CORRECTED BELOW — see "THE DENSITY
LOOKUP". The claim first written here, `composite 0.85 x loose 0.50 = 0.425`,
is WRONG in both components. The real path is `dish_head` truncating at the
comma to `'potato'`, giving **0.62**, with a shape factor of about **0.65**.
The product was coincidentally close to the 0.406 backed out of the geometry,
which is why the wrong mechanism survived a commit.

**Bulk or material:** BULK. `potato 0.59` is FAO's "potato english boiled",
and solid potato flesh is about 1.05-1.10 g/cm3 — potatoes sink in water. A
figure of 0.59 is packed pieces with voids between them. The table's own header
confirms the intent: "grams = area x height x profile x DENSITY", i.e. a
density meant to pair with a BOUNDING volume.

And `SHAPE_FACTORS` is documented in `portion.py` as bundling two corrections:
the pile's profile AND how much of a bounding box the food fills. So with tight
per-instance masks:

  * the AREA no longer contains the gaps between pieces
  * the DENSITY still discounts for voids
  * the SHAPE FACTOR still discounts for box fill

Three terms fitted to compose against a loose blob, applied to a tight mask.
**A segmenter swap therefore needs a RE-DERIVATION, not a re-fit** — which is
the same trap as the height constants and the mask cache, now for the third
time in one day. The pattern: every constant in this pipeline was fitted
against a particular upstream, and none of them records which.

---

## THE DENSITY LOOKUP — audited 12 Sep 2026, three live bugs, zero calls

### First, a correction to this file

An earlier entry here said the fries were sized at `composite 0.85 x loose 0.50
= 0.425`. **That is wrong in both components.** The real path is
`dish_head` -> `'potato'` -> **0.62**, with a shape factor of about **0.65**.
The product was coincidentally close to the 0.406 backed out of the geometry,
which is exactly why the wrong claim survived a commit. A product matching is
not a mechanism matching.

### Bug 1 — `dish_head` truncates at the comma. Systematic.

`,` is in `DISH_SEPARATORS`, so:

    dish_head("potato, french fries, from fresh, fried")  ->  "potato"

`'fries'` never reaches the comparison at all. This is not the
"longest-key-wins" hazard that was first blamed — that rule never got a chance
to fire, because the candidate text had already been cut down to the commodity.

**USDA-style names put the COMMODITY FIRST and the PREPARATION AFTER THE
COMMA**, so every USDA-style label is truncated to its raw ingredient. The
commodity is precisely the wrong half: what a food weighs per millilitre is a
property of how it was prepared, not of what it was before. French fries get
potato's 0.62 instead of fries' 0.42, 1.48x too dense, on every scan.

The separator list is right about ` with `, ` in `, ` on ` — those cut off
sauces and fillings, and the docstring records a weighed meal where that fix
was worth most of a 2x error. The comma is the odd one out and should not be
in that list.

### Bug 2 — substring matching with no word boundary

    grilled cheeseburger  ->  'cheese'  ->  1.05

`'cheese'` matches inside `'cheeseBURGER'`. A cheeseburger is not cheese, and
the physical model below independently needs **0.52** for that burger — so this
is a 2x error, on the single heaviest item in the bench.

Same class, benign only by luck: `'smashed potatoes'` contains
`'s-MASHED POTATO-es'`, so it resolves to `mashed potato` 1.04 and happens to
be right.

### Bug 3 — longest key wins, so an ingredient beats the dish

    cheese and broccoli soup  ->  'broccoli'(8) beats 'soup'(4)  ->  0.35

A cheese and broccoli soup at 0.35 g/ml is about 3x too light. This IS the
longest-wins hazard, firing where there is no comma to truncate first. Length
is not specificity.

### The corrected coverage count

Counted by WHICH KEY MATCHED, not by value — several table entries equal the
0.85 default, which made the first count unreliable:

    matched on dish_head      20 / 33
    matched only on the full name   3 / 33
    NO key at all            10 / 33   -> group density, composite 0.85

The ten with nothing: brussels sprouts, dinner roll, grapes, pot roast,
spaghetti, steamed carrots, steamed zucchini, tortilla chips, trail mix, whole
plate. Note `macaroni salad` IS already a key at 0.85 — it appeared to be
missing only because its value equals the default. And `whole plate` is a bench
artifact, not a food; it should expect the default rather than a row.

Separately: `USE_SOURCED_DENSITIES = False`, so the 32-row FAO `densities.csv`
is loaded and discarded. The live table is a 63-row hardcoded dict. Leaving
that switch alone is deliberate — turning it on moves seven bench foods at once
and would make this change unattributable.

### THE PROPOSED RULE, to implement against the table below

1. **Word-boundary matching.** A key matches only on whole words. Kills
   `cheese`/`cheeseburger` and `mashed potato`/`smashed potatoes`.
2. **Drop `,` from `DISH_SEPARATORS`**; keep ` with `, ` in `, ` on ` and the
   rest, which correctly cut off sauces. Search each comma segment instead.
3. **Score by words matched, then by later segment.** English compounds are
   head-final, so `french fries` beats `potato` and `soup` beats `broccoli`.
4. **Length only as a final tie-break.**

SCOPE DISCIPLINE: this changes WHICH KEY MATCHES, not what the values mean. The
table's values are BULK figures meant to pair with blob areas and a shape
factor. Re-basing them to material densities is the separate change the
physical model implies, and bundling the two would make neither attributable.

### ACCEPTANCE — the 33 harvested names, as a test table

The new rule must get all 33; the current rule fails at least on fries,
cheeseburger and the soup. Ten keys need adding, in the table's existing BULK
semantics, not material ones.

    food                                   expect  via
    bbq chicken thigh                        1.05  chicken
    beef posole                              1.02  stew          ADD 'posole'
    brussels sprouts                         0.58  ADD
    caesar salad                             0.22  salad
    cheese and broccoli soup                 1.00  soup          (bug 3)
    cheeseburger slider                      0.55  ADD 'cheeseburger' (bug 2)
    chicken drumstick with mole sauce        1.05  chicken
    chicken drumstick, grilled with sauce    1.05  chicken
    chicken drumstick, rotisserie            1.05  chicken
    chicken noodle soup                      1.00  soup
    dinner roll                              0.28  ADD (bread-like)
    egg, whole, cooked, scrambled            1.03  egg
    fried french fries                       0.42  fries
    grapes                                   0.62  ADD (berries-like)
    grilled cheeseburger                     0.55  ADD 'cheeseburger' (bug 2)
    grilled chicken drumstick                1.05  chicken
    macaroni salad                           0.85  macaroni salad (already present)
    mexican rice                             0.67  rice
    pizza slice                              0.55  pizza
    pot roast                                1.05  ADD
    potato, french fries, from fresh, fried  0.42  fries         (bug 1)
    refried beans                            1.06  refried beans
    roast beef                               1.05  beef
    smashed potatoes                         1.04  mashed potato
    spaghetti                                0.65  ADD (pasta-like)
    spaghetti with chicken                   0.65  ADD
    spaghetti with sauce                     0.65  ADD
    squash, winter, spaghetti, ... with salt 0.65  ADD  -- NOTE: this is squash,
                                                   NOT pasta. A word-boundary
                                                   rule on comma segments will
                                                   match 'spaghetti' here and be
                                                   WRONG. Decide deliberately.
    steamed carrots                          0.60  ADD 'carrots'
    steamed zucchini                         0.60  ADD 'zucchini'
    tortilla chips                           0.18  ADD (very airy)
    trail mix                                0.50  ADD
    white rice                               0.67  rice
    whole plate                              0.85  default (not a food)

The `squash, winter, spaghetti` row is the trap in this table and is left in on
purpose: it is the case where the proposed rule's own logic produces a wrong
answer, and a rule chosen without it would look better than it is.

---

## THE PHYSICAL MODEL — consistent, NOT yet tested

Each photo's frame measured from its OWN plate rather than assumed shared —
photo 23's plate is 24% of its frame, so brussels is 33.5 cm2, not the 15.2 a
shared-frame assumption gives.

    food      area cm2   h mm  solidity  -> density   plausible?
    fries         99.9   10.0   0.90        0.72      fried potato, porous
    sprouts       33.5   33.0   0.67        0.88      a brassica that sinks
    burger        76.9   45.0   0.80        0.52      bun dominates the volume

One coherent set reproduces all three weighed values with physically sensible
densities. Against the live table: fries 0.62, sprouts 0.85 (no key), burger
1.05 — the burger is 2x out, in the direction bug 2 predicts.

**SOLIDITY IS NOT ~1.0.** A sphere is 2/3 of its bounding prism however tight
the mask is. That is the OBJECT'S OWN geometry, not box fill, so the third term
does not disappear when masks get tight — it changes meaning: prism ~0.9,
sphere 0.67, dome ~0.8. Forcing 1.0 on the sprouts gives 0.59, too light for a
vegetable that sinks.

**THIS IS NOT YET A TEST.** With n=3 and two chosen inputs per food it is
consistent, not falsifiable. The published-input version is **BLOCKED**: it
needs MATERIAL densities, and the repo's FAO file is BULK — `potato 0.59`
against solid potato's ~1.05-1.10, which sinks in water. Substituting
remembered numbers would manufacture a result, so the next session should
source material densities first and only then run the test.

---

## NOT DONE, AND WHY

- **The published-input physical test** — blocked on material densities, above.
- **The provenance test** — DESIGNED, NOT BUILT. Each constant table declares
  the world it was derived under (`segmenter`, `long_edge`, `mask_semantics`,
  `area_source`) and a test compares that to the live configuration and goes
  **RED, not warning**. It would fire today if the pipeline were pointed at
  sam3, and again the moment tight masks reach the grams. Build it BEFORE the
  segmenter swap, not after.
- **Integration** — held. The design shape, recorded and unbuilt: try sam3,
  fall back to today's path on an EMPTY result. sam3's emptiness is a reliable
  signal, so nothing has to classify foods as discrete or continuous in
  advance; caesar then scores what it scores today and every discrete food gets
  a real footprint.
- ~~**The density lookup fix itself**~~ — WRITTEN 12 Sep, see below.

---

## THE DENSITY LOOKUP FIX — landed 12 Sep 2026, and what checking the spec found

Suite 827 -> **871 passed, 0 failed** (44 new). Zero model calls. Committed
12 Sep in two parts, green at each: `dc6e63c` changes WHICH KEY MATCHES and
adds only aliases to existing rows; `2c6cd9c` adds the six valued keys (four
USDA cup weights, two [est]) as new data.

### The rule as built (`portion.density_for`)

Whole words, where the END of a word counts (`blueberries` -> berries,
`catfish` -> fish, but not `cheese`burger or `egg`plant); then more words
matched; then a preparation key (`refried`, `mashed`, `puree`) over a
commodity; then the later word; length last. The comma no longer cuts the name
FOR DENSITY, and a multi-word key may match across comma segments.
`dish_head` is UNCHANGED — `_classify_shape` and vision.py's rename guard read
it, and dropping the comma there would move heights, which this change must not.

### Corrections to the spec above, each checked

- **The table has 34 rows, not 33.** The old rule failed 18.
- **Bug 1 is NOT live on photo 35.** The API log shows the fries looked up as
  `fried french fries`, which already hit `fries` 0.42. `potato, french fries,
  from fresh, fried` is USDA's DISPLAY name in `food_facts`; `estimate_grams`
  receives the lookup name. The bug is real code, latent on this evidence.
- **Bug 2 IS live.** `grilled cheeseburger` -> 1.05, and `food_facts` carries
  `density_g_ml = None` for it, so nothing overrides the table. Now 0.55.
- **Bug 3 is mostly absorbed on the soups.** All three crock soups classify
  `liquid`, and the clamp lifts 0.35 to 0.90. Live cost ~10%, not 3x.
- **The spec contradicted itself on `smashed potatoes`.** Rule 1 kills
  `mashed potato` in it; the table expects 1.04 via that key. The word-end rule
  keeps 1.04 without a new key.
- **The ADD values were reasoned, against the table's own header rule** (g/cup
  / 236.588). USDA FDC cup weights replace them where published: brussels 0.659
  (169971), grapes 0.638 (174683), trail mix 0.634 (167561), spaghetti squash
  0.655 (170539). `cheeseburger` 0.55 and `tortilla chips` 0.18 have no USDA
  cup weight and stay, marked [est].
- **Carrots and zucchini were NOT added.** They are the only two foods
  `SEPARATE_PIECES_HEIGHT_MM` was solved on: 10.66 and 7.78 mm at 0.85, mean
  9.2. At the spec's 0.60, carrots go -14% -> -39% under the frozen height.
  `test_the_calibration_foods_keep_their_fitted_densities` now fails if any of
  the seven weighed foods' densities move without a refit.
- **The squash trap resolved by a key, not a rule.** `spaghetti squash` (two
  words) outranks `spaghetti` (one). The old code sized it at 0.92 — `oil`
  matching inside `b-OIL-ed`. `ice_cream` was also unreachable from "ice cream".

### What this does and does not tell you about grams

The before/after diff (`density_for` with NO group) moved 23 bench names. That
is not the production delta: vision.py passes `food_group`, so any name whose
head had no key before was on the GROUP density live, not 0.85. The group the
model reports per food is not in any committed artifact. Exact live moves are
known only where the head matched before: burger x0.52, chicken noodle x0.95,
posole x0.97, cheese-and-broccoli x1.11 after the clamp.

Also still open: `dish_head`'s comma is the same bug on the SHAPE path, and the
height fit used group=None while production passes a group — a pair mismatch of
its own.

---

## THE PHYSICAL-MODEL DENSITY TEST — verdict recorded 12 Sep 2026. STOPPED HERE.

Gil is measuring MATERIAL densities by water displacement: brussels sprouts,
carrot, cooked beef, potato. They are NOT to be patched into `DENSITY_G_ML`,
whose values are BULK and were fitted as a set with `SHAPE_FACTORS` and the
heights. They test the physical model only.

Pre-registered before any measurement arrived, and accepted:

- **Sprouts are a real test, and a loose one.** Photo 23 holds FOUR whole
  sprouts in one layer (sam3's fifth mask is a fragment of one). Plate
  measured by Hough at 25.0% of frame, not the "24%" above, which had no
  source: 0.4571 mm/px. Volume from each sprout's own mask: 64.9 cm3
  (spheroid) to 71.2 (sphere). 65 g falls out at 0.91-1.00 g/ml. A ±20%
  budget accepts 0.73-1.20, so only a gross failure can be caught.
- **Carrot is a test only with a MEASURED coin thickness.** Implied
  t = 58 g / (rho x 64.02 cm2), 9.1 mm at 1.00.
- **Beef and potato are FITS.** No published or visible height exists for
  folded roast beef or smashed potatoes; fries heap, and fried potato is not
  the material a boiled potato's density describes.

**Was the table a source of error? Not answerable, at any sample size, from
production residuals.** Footprint error of 3x to 7.5x on photo 35 dwarfs any
density difference. Separately, the items that CAN be tested never used a
table row: brussels had no key and carrots take the default.

**The re-derivation (density, height, solidity together) is NOT designed, by
decision.** Do not start it.

---

## THE CARD RUNG — `reference_cv`, audited 12 Sep 2026. REPORTED, NOT FIXED.

A credit card is ISO/IEC 7810 ID-1, 85.60 x 53.98 mm: the only object in these
photographs whose size is known rather than declared, estimated or quantised.
It is in 25 of the 27 photographs on disk. Zero model calls below.

### Where it is found

`find_reference` at 1280 (`MAX_IMAGE_EDGE`, what production passes) and at
full resolution, on every photograph on disk and every "card" photograph in
the storage bucket (1037 objects, fully paged):

    local 17-46, card visible in 25     found: 43, 44 only            2 / 25
    storage 10, 11, 14 (paper/cloth)    found: 252, 319, 317 mm       3 / 3
    storage 15-rajas-rice-card          not found
    36-slider-fries-CARD                not found
    no-card controls 09, 12, 16, 17-46  no false positives

### Two failure mechanisms, both measured

1. **The wood table is invisible in greyscale.** The detector runs Canny on
   grey only. Card against the wood beside it: grey 63 vs 73 on photo 23,
   100 vs 102 on 36 -- while the colour difference is large (Lab dE 28 and
   36). The edge maps show the outline in fragments (23) or fused into the
   wood grain and the card's printed ring (36). No setting closes it, and a
   broken edge chain has ~zero contour area, so it is dropped at the AREA gate
   before any geometric test runs. That is why it leaves no trace in any
   rejection count, and why 17-36 fail silently.
2. **Photo 15 is refused by consensus.** Red card on pale cloth: the card's
   outline IS found as a passing quad (aspect 1.691, 250 px) -- by ONE of the
   six edge settings. `MIN_SETTING_CONSENSUS = 2` refuses it. The other five
   lock onto the signature panel inside the card (aspect 5.0-5.8).

### Whether it has ever been used -- CORRECTED by the audit below

The first version of this section said the bench "cannot exercise" rung 2a,
that 43 and 44 "score plate_reference", and that the card/rim gap matched the
parallax debt. All three were wrong or unsupported; the audit replaces them.

---

## THE reference_cv AUDIT — every bench photo containing a card, 12 Sep 2026

Zero model calls. Every distinct bench photo in the storage bucket (43, from
1037 objects), run through `find_reference` on the bytes production fetched,
decoded exactly as `vision.downscale_jpeg` does (EXIF, RGB, thumbnail 1280).
Card presence judged on a contact sheet. The method column is what the
pipeline SELECTED, from `meal_items.estimation_method` joined to
`meals.photo_path` (250 bench meals, 521 items).

    photo                     analysed  card?  detected  long x short px  aspect  mm/px   frame mm  tilt  cons  method selected (items)
    10-tacos-paper-card       960x1280  yes    YES       326.6 x 212.7    1.535   0.2621  251.6     14.5  6     no saved scan
    11-kebab-paper-card       960x1280  yes    YES       257.7 x 166.6    1.547   0.3322  318.9     12.8  6     reference_object (12, 4 scans)
    14-plate-mole-chicken-card 960x1280 yes    YES       259.5 x 174.6    1.486   0.3298  316.7     20.4  2     plate_reference (12)
    15-rajas-rice-card        960x1280  yes    no        --                                                   plate_reference (12)
    17-32, 34, 36             960x1280  yes    no        --                                                   plate_reference (all)
    40, 41, 42, 45            480x640   yes    no        --                                                   45: plate_reference (3); 40-42 no saved scan
    43-chips-heaped           1280x960  yes    YES       257.1 x 157.3    1.634   0.3329  426.1     14.0  4     no saved scan
    44-grapes-spread          1280x960  yes    YES       271.6 x 163.8    1.658   0.3152  403.4     16.9  6     no saved scan
    no card: 01-09, 12, 13, 16, 33, 35, IMG_3938      no false positives

**Has method == "reference_object" ever been the selected rung on a bench
photo? Yes, on exactly one: 11-kebab-paper-card, all 12 items across its 4
scans, 7-8 Sep -- food on paper, no plate.** Detected on 5 of 28 card
photographs. Where the card was detected AND a plate was present (14), rung 1b
won, as `mm2_per_frame` orders it. Every current `CASES` row (17 onward)
passes a plate diameter, so the current bench cannot select rung 2a.

Why detection fails is recorded above: greyscale near-isoluminance on wood
(17-36), the consensus gate on 15. The 480x640 uploads (40-42, 45) were never
diagnosed.

---

## MATERIAL DENSITIES — measured 12 Sep 2026, with provenance

Method: fill-to-brim cup, V = F - (T - B), two runs on different fills. Run 2
read 1.19 / 1.22 raw; both foods reconcile at ONE baseline 7 ml above its
measured fill. That +7 ml was fitted to reconcile run 2, so only the second
food is an independent check (it passed, to 2%). Brim-fill repeatability is
~2% of cup volume; the uncertainties below follow from that, not from the
run-to-run agreement.

    carrots  1.04 g/ml +/- 0.07   63 g, coins cut from one ~4.5 in carrot, as eaten (Gil); variety not recorded
    grapes   1.11 g/ml +/- 0.06   90 g, 12 seedless grapes, mixed green and red (50% green by measured hue)

**Neither sample is the sample in any bench photo.** Carrots: 63 g of coins,
not photo 17's 58 g of whole glazed baby carrots. Grapes: photo 44 is 11
red/purple grapes (89% red, 2% green by measured hue; 45 has no green grape)
-- the cup grapes are not those, despite the same 90 g. Applying either
density to a bench photo is a stated varietal / preparation assumption.

## BULK vs MATERIAL — the durable finding, portion.py:282-288

The `[src]` rows added from USDA cup weights are BULK densities: a cup holds
pieces AND the air between them. Against measured material density:

    grapes   USDA 0.638 (FDC 174683)   material 1.11   ratio 0.575
    carrots  USDA 0.659 (FDC 170394, sliced; NOT a table row)  material 1.04  ratio 0.634

Ratios of 0.58-0.63 are what randomly packed rounded pieces fill. So those rows
carry ~40% void, and they pair ONLY with an envelope volume (footprint x pile
height x profile) -- which is what the table is fitted against. Paired with a
tight per-piece volume they under-read by ~1/0.6. The samples differ from
USDA's, so the ratio is a property of the FORM, not a precise constant.

## THE CARD / RIM GAP — AN OPEN INSTRUMENT QUESTION, NOT A FINDING

On the taped 260 mm plate (IMG_4190/4194/4197) the card's mm/px is 1.10x the
Hough rim's; on photo 44 (declared 222 mm) 1.09-1.15x.

What has been ruled out: Hough overshoot on the taped plate. Measured against
a ray-traced rim ellipse, verified on the overlay: -0.1%, -0.4%, +1.3%
(4197). On photo 44 that ray method stopped at the foam rim's inner ridge, so
it produced no valid overshoot number there; by eye the Hough circle sits on
the outer edge.

What has NOT been tested: card on the table vs rim plane; card position in a
tilted frame (tilt 11-17 deg); the detector's long side (quad and a colour
rectangle differ ~5% on 44). Note portion.py:1666-1678 already assigns a
table-card parallax of ~10% in AREA to rung 2a, not rung 1b, and :1925-1932
shows tilt cancelling for rung 1b's area. Neither cancellation carries to a
model that reads heights off pixels, which goes as scale^3 -- so this gap
limits the test rig, not production.

Pending: a photo of the same grapes with the card lying ON THE PLATE FLOOR,
top-down. Its purpose is this audit and this question, not the density.

**The volume model is paused until reference_cv is settled.** Three
consecutive runs ended "not a test"; the instrument changes next, not the
mask.

---

## reference_cv RECALL — 5 of 28 (18%), and where the 23 misses die. 12 Sep 2026

Replayed the detector's own loop (same constants, same order) on the exact
frames production analysed. The card in each miss was boxed by colour (Lab a*)
and checked on a contact sheet; photo 15's box is the detector's own passing
quad. Each photo is attributed to the FURTHEST gate any card-overlapping
contour reached. Nothing was changed.

    furthest gate reached         IoU>=0.3   IoU>=0.5
    not 4 corners (approxPolyDP)     13         10
    no card-sized contour             5          9
    consensus (<2 of 6 settings)      4          4
    size (open chain, area ~0)        1          0
    aspect ratio                      0          0

**Not one gate: two populations.**
- **Wood table, 18 photos (17-36).** Card-vs-surround grey contrast 1.6-16.6
  levels. Every one dies at the EDGE stage -- the outline never closes into a
  clean quad (7-13 corners when it closes at all). Near-isoluminance.
- **High contrast, 5 photos (15, 40, 41, 42, 45).** Contrast 67-97. Four pass
  EVERY geometric gate but in only ONE of six edge settings, and
  `MIN_SETTING_CONSENSUS = 2` refuses them. 45 dies on corners.

The aspect gate killed nothing. Any threshold change has to say which
population it is for.

## THE CALIBRATED/UNCALIBRATED RUN — three premises corrected before spending

1. **`food_scans.raw_vision` is empty on every bench scan (0 of 72).** No
   offline replay from stored detections exists; the request's
   `plate_diameter_mm` is not stored either.
2. **"Uncalibrated" on the bench account is NOT the subscriber path.** The
   account holds one `scan_calibrations` row: "my dinner plate", 254 mm,
   `vessel=dinner_plate`, not default. With no diameter sent, vision.py:1677
   looks up a calibration by the detected vessel, and `_calibration_fits`
   accepts an exact name match. Photo 29 was named `dinner_plate` in all 6
   saved scans, and all 30 of its items were saved `plate_reference` -- so the
   bench's one "uncalibrated" row has most likely been scoring a 254 mm
   calibration, not the 270 mm prior its comment describes. Also:
   `scale_learning.refresh` WRITES learned calibrations when a card is found,
   so a run can create calibrations mid-run and contaminate later photos.
3. **Subscribers do not fall uniformly to rung 4 at 270 mm.** A named vessel
   reaches rung 3b `vessel_reference` first, and the model's name is roulette:
       side_plate   -> 200 mm   area -24% on a 229 plate   (~half the plate scans)
       dinner_plate -> 270 mm   area +39%
       paper        -> ai_prior, no geometry              (17, 26, 27 every scan)
       cup          -> 80 mm    area -51% on a 114 crock  (31, 32, 33 every scan)
   Photo 18 was called side_plate 4 times and dinner_plate twice. Rows with a
   camera distance (40-45) reach rung 2 depth first; 43/44 reach rung 2a card.

## PRE-REGISTRATION — recorded before any run, 12 Sep 2026

Baseline: the calibrated errors of each photo's last saved bench day, BEFORE
today's density-lookup commits (so absolute numbers are stale; the paired ratio
is not, since both arms share the density). Each photo's uncalibrated grams =
calibrated x (subscriber-path frame area / calibrated)^0.9, over that photo's
historical vessel-name mix. 0.9 is this file's stated grams-vs-geometry
elasticity and is the main modelling assumption: a lower confidence ceiling on
rungs 3b/4 shifts the blend toward the model's prior, which this ignores.
Photo 45 takes the depth rung, its area factor 1.15 borrowed from photo 44's
geometry (rim frame 353 mm vs depth 378 mm).

    calibrated baseline     43.6% per item   CI 31.6-55.6   (59 items, 16 photos)
    PREDICTED uncalibrated  64.7% per item   CI 47.1-82.4
    PREDICTED paired gap    +20.0 points     CI +3.1 to +36.8   (photos as units)
    geometric-mean grams ratio uncal/cal  0.91 -- the SIGN varies by photo, 0.53 to 1.35

Not predictable, excluded: 17, 26, 27 (every scan named `paper`, so
`ai_prior`), 28 (no matched item), 40-44 (no saved scans).

**The prediction is that the gap is real but NOT uniform:** dinner-plate photos
get heavier, side-plate and crock photos lighter, and the aggregate moves about
20 points -- right at the readability line if the two arms are separate model
calls.

**Pairing, stated.** Two API calls per photo put the model's run-to-run spread
(9.3 points per photo on average, 37.3 worst) into the difference, so the
20-point rule applies. Paired at the DETECTION -- one vision response per
photo, the scale step run twice -- removes model and mask variance from the
difference entirely, and the per-photo ratio becomes deterministic. That needs
the run to force no calibration and block learned-calibration writes, on this
account or a fresh one. GO given 12 Sep; run as designed -- see the sweep
section below.

---

## THE CARD'S VIEWING ANGLE, THE ASPECT CLIFF, AND THE CORNERS — 12 Sep 2026

### Corrections accepted by Gil

- **`ReferenceFind` DOES carry the corners** (reference_cv.py:86, set at :253);
  there is no `short_px` field. They are dropped at vision.py:1727-1729, which
  copies only `kind`, `frame_width_mm` and `tilt_deg` into `GeometryHint`. The
  only reader of `.corners` is scripts/segment_lab.py:160.
- **The aspect gate caused NONE of the 23 recall misses.** Aspect-based tilt
  on the misses is 1-14 deg on 20 of them, 20-23 on two (15, 20) and 29 on one
  (25, uncertain colour box). Recall is a detection problem, not an angle one.
- **The edge-line corner fit is NOT a pending fix.** On IMG_4198, rim roundness
  after rectification: shipped detector corners 0.9935 (281 mm), edge-line fit
  0.986 (284 mm), approxPolyDP on a colour-mask hull 0.933 (311 mm). The 0.90
  failure came from the colour-mask input. That photo is near top-down (raw
  rim roundness 0.9988), so it cannot rank corner methods at all.

### The cliff, derived and rendered

    card short side foreshortened   aspect = 1.586 / cos t   rejected above 32.06 deg
    card long side foreshortened    aspect = 1.586 x cos t   rejected above 34.92 deg
    card diagonal to the tilt       aspect unchanged; corner gate fires above 50.4 deg

Rendered in perspective (D 300 mm, 68 deg field, 3.18 mm corner radius) and
run through the real `find_reference`: detected to 34 / 30 / 45 deg, first
miss at 36 / 32 / 50 deg.

### The gates see the CARD'S viewing angle, not the camera's tilt

Supersedes the uniform-tilt model. For a camera D mm from the scene point on
its axis, tilted theta, and a card offset Y mm along the tilt direction on the
table (positive = far side):

    cos(phi) = D cos(theta) / sqrt(D^2 + 2 D Y sin(theta) + Y^2)

D 300, Y +120, theta 30 -> phi ~46 deg. D 300, Y -120, theta 45 -> phi ~23.5.
Where the card lies in the frame moves the cliff by 15-20 deg of camera tilt.
Checked by Gil at both limits and at the D 300 / Y 120 / 30 deg case.

### tilt_deg

A 2% aspect error reads as 11.4 deg; near-top-down photos read 3.9-16.5. Blind
to a diagonal card (3-13 deg at every tilt). Its only consumer, the measured
height path at portion.py:2296, is off: `USE_MEASURED_HEIGHT = False`.

### Carrying the corners through — SPECIFIED, NOT BUILT

Held until the uncalibrated sweep reports.

- vision.py:1727 hand-off: pass `corners` and `image_size` into `GeometryHint`.
- Two frames: the card is found at 1280 (`downscale_jpeg`); crops and masks
  decode at 1568 (vision.py:801/841/894). Rescale, with identical EXIF handling.
- portion.py:2288 `food_mm2 = frame_mm2 x area_ratio` assumes one scale across
  the frame, false under tilt. Map the footprint through the homography
  instead -- touches `estimate_grams`, the measured footprint path in
  `food_seg`, and `scale_learning.observe_width_mm` (vision.py:1744).
- Rung order is a decision. The card-plane parallax at portion.py:1666-1678
  remains either way.
- Reuse `reference_object` rather than a new method name, to avoid a schema
  migration (0019 is the precedent for what a new name costs).

### Plate measurements on the card photos

- A V>=200 plate mask under-traces the rim: 852 px against 875 edge to edge on
  IMG_4198, 826 against 927 on IMG_4197. That missing arc produced the "22 deg"
  tilt; the camera was near top-down.
- The "four photos of one plate" were two objects: the glass bowl read 274-275
  mm, the plate 245-255.
- Hough rim x card scale puts the beaded plate at 269-288 mm across four
  photos, above the declared 10 1/4 in. Tape prediction, recorded before the
  tape: 255-280 mm, point 267, outer edge to outer edge.

### Tilt series — pre-registered, LOW priority, score on arrival

- Card near the frame centre, sides aligned with the frame: detected at 0, 15
  and 30 deg (30 is 2-5 deg from the across-tilt cliff and may miss); missed
  at 45 at the aspect gate. `tilt_deg` ~0-15 (noise), then ~15, ~30.
- Card toward the far side: misses from 30, possibly from 15.
- Card diagonal: all four detected; `tilt_deg` wrong throughout.
- Where the long side is foreshortened, `mm_per_px` reads ~15% high at 30.

---

## THE UNCALIBRATED SWEEP — run 12 Sep 2026, paired at the detection

### How it was run

A scratch harness (not in the repo) fetched each photo's stored bench copy,
bought ONE `gpt-4o` detection per photo (plus `second_look` when it fired) and
cached it, then called `vision.build_items` once per arm on a deep copy of that
same response. `estimate_grams` and `_measured_heights` -- the only stages that
read the scale -- run inside `build_items`, so both arms recompute them from
the same response. SAM2 is memoised per image, so both arms share masks. Depth
provider is `NullDepth`. Nothing persisted: no meals, items, assessments, scan
rows or learned calibrations.

    CAL      declared plate diameter (+ camera distance on rows 40-45)
    UNCAL    no diameter, no saved calibration, no camera distance -- a subscriber
    UNCAL_D  UNCAL keeping the 280 mm camera distance (rows 40-45)

NOT run: `refine`, the post-estimate reasoning call. It may move grams within
the estimator's band, and the uncalibrated rungs have WIDER bands (vessel 0.22,
pixel 0.30 vs plate 0.14), so refine has more room on the uncalibrated arm.
The comparison is at `build_items` output.

Pairing: items paired by index within one shared detection; photos are the
units for every interval (items within a photo averaged). Model and mask
variance are out of the difference -- EXCEPT the vessel name, which is part of
the one response: photo 18 was called side_plate in 4 of 6 earlier scans and
dinner_plate in this one. The result is conditional on this draw's names.

### Rung distribution a subscriber actually gets (question A)

    items    vessel_reference 31   ai_prior 5   reference_object 5   pixel_area 0
    photos   vessel_reference 19   ai_prior 5   reference_object 3

**Rung 4 `pixel_area` fired on nothing.** The model always returned a plate
ellipse; it named a vessel on every photo. Names this draw: side_plate (200 mm)
on 21-25, 28, 34, 40, 41; dinner_plate (270) on 18, 19, 20, 29, 30, 35, 36, 14;
cup (80) on the crocks 31-33; paper (no reference -> `ai_prior`, or the card
where one was found) on 17, 26, 27, 42, 43, 44, 45.

### Result (28 scored items, 25 photos)

    CAL     mean |e| 45.1%  CI 25.2-64.9   signed +7.6%   median signed -4.1%
    UNCAL   mean |e| 44.5%  CI 28.4-60.5   signed +21.4%  median signed +12.4%
    paired change in mean |e|        +1.9 points  CI -14.8 to +18.6
    paired change in signed error   +12.0 points  CI -7.5 to +31.6
    paired change in mean |ln err|  -0.04          CI -0.2 to +0.1
    grams ratio UNCAL/CAL           geometric mean 1.16, range 0.66-4.43
    photos worse 11, better 14; meal total on 19: CAL -36.4%, UNCAL -6.5%

**The headline does not move, and that is two errors cancelling, not a
product that tolerates a missing plate.**

    CAL within 25% of the scale   n=14   11.9% -> 38.5%   +26.6 pts  CI +8.7 to +44.6
    CAL off by 25% or more        n=14   78.2% -> 50.4%   -27.8 pts  CI -43.7 to -12.0

The mechanism is in the code, not inferred: `BLEND_MAX_WEIGHT_BY_METHOD`
(portion.py:922) lets the model's serving prior pull `plate_reference` at most
10% and `vessel_reference` up to 40%. Uncalibrated, nearly every item moves to a
40% pull, which compresses both tails toward a typical serving -- the log-error
SD FELL from 0.650 to 0.457. Where the calibrated geometry was right, that pull
plus a guessed vessel size makes it wrong; where the geometry was wild, it
rescues it. At today's accuracy the two balance. As geometry approaches the 10%
target the accurate half is the whole bench, and the penalty is the +27.

Caveat on that split: it selects on the CAL arm's error, which favours finding
exactly this pattern. The code mechanism is the independent evidence for it.

### Camera distance (UNCAL_D, rows 40-45)

Where the depth rung fired (40, 41, 42, 45) a 280 mm distance reproduced the
calibrated grams to within 0-11% (ratios 1.06, 1.00, 1.11, 1.05). n=4. If a
phone-reported distance reached the scan, the uncalibrated path would track
the calibrated one on these. 43/44 stay on the card rung (2a outranks depth).

### Card head-to-head (question B) — n=2 scored, 1 unscored: evidence, not proof

    photo  CAL (declared plate)   UNCAL card rung      UNCAL_D card rung
    43     -4.8%                   +23.2%               +16.4%
    44     +19.9%                  +64.9%               +55.9%
    14     122/108/71 g            129/114/73 g         (unscored; card ~5% heavier)

Withholding the plate DID let rung 2a fire, on all three. But on 43 and 44 the
model called the plate "paper", so the alternative was never the 270 mm default
-- it was `ai_prior` (photo 45, same 90 g grapes, no card found: +66.7%). The
card read 28-45 points heavier than the declared 222 mm plate on the same
detection, consistent with the card/rim gap above and with the declared plate
size itself being in question. 10 is not in the sweep set.

### Both pre-registrations scored

- **Mine: missed on all three numbers.** Uncalibrated 64.7% (CI 47.1-82.4)
  predicted, 44.5% observed. Paired gap +20.0 (CI +3.1 to +36.8) predicted,
  +1.9 observed. Grams ratio 0.91 predicted, 1.16 observed -- wrong sign. The
  area^0.9 rule held only where no blend or special path intervened (21, 23,
  25, 28 at 0.76; 18, 20 at 1.39). It ignored the per-rung prior pull, which
  dominated elsewhere (35 at 2.25, 36 at 2.94 against a rule of 1.35), and the
  soup path (crocks 0.79 against 0.53).
- **Gil's: 60-75% predicted, 44.5% observed, missed.** His caution was the
  right one: the signed error moved +12 while the absolute stayed flat. The
  mechanism was not "the bench runs light" (CAL median -4.1%) but the prior
  pull compressing both tails. His rung-4 assumption: zero items.

---

## CAMERA DISTANCE — LEAD WITHDRAWN, SCORED FAILED. 12 Sep 2026

Recommended as the priority lead and withdrawn by Gil the same night. The
premise -- that the phone already knows its distance -- was false: the native
sensor module does not exist. The 0-11% match on rows 40-45 was two declared
numbers agreeing, not a sensor validated. What follows is kept as the map of
the path, not as a recommendation.

### The path, end to end

- **Client.** `ScanScreen.capture()` runs `measureCameraGeometry()` alongside
  `takePictureAsync`, keeps the first shot's reading, and spreads it into the
  POST /scans payload (ScanScreen.tsx:47-54, 71-75; client.ts:129-131).
  Library photos send none, by design.
- `measureCameraGeometry()` (mobile/src/native/depth.ts:96) calls
  `NativeModules.NeutriDepth.measure()` with a 400 ms timeout and returns `{}`
  when the module is absent.
- **`NeutriDepth` does not exist.** depth.ts is the only file that names it.
  There is no ios/ or android/ directory, no config plugin, and no AR or depth
  library in package.json (the camera is `expo-camera`). Every real device
  sends nothing.
- **Server.** `ScanRequest.camera_distance_mm` (80-2000), `camera_fov_deg`
  (40-100), `camera_aspect_ratio` (models/nutrition.py:26-28) -> routers/scans.py
  stores distance and FOV on `food_scans` and passes both to `run_scan` ->
  `GeometryHint(depth_mm=..., camera_fov_deg=...)` (vision.py:1716-1718) ->
  `mm2_per_frame` rung 2 (portion.py:1707), below rung 1b plate and 2a card.
  FOV defaults to 68 deg when absent.

### What the data says

- 107 of 1025 `food_scans` carry a distance, ALL bench: kebab photos 11-13 at
  330 mm, rows 40-45 at 280 mm. None carries a FOV.
- The depth rung appears in saved results on one photo, 12 (12 items).
- In the sweep it fired on UNCAL_D for 40, 41, 42 and 45, matching the
  calibrated grams within 0-11%. CAUTION: 280 mm is hand-stated ("shot from
  10-12 inches, stated as 280 mm"), so that is two declared numbers agreeing,
  not a sensor validated.

### It fixes the scale AND the compression

`BLEND_MAX_WEIGHT_BY_METHOD`: `depth_model` 0.12 against `vessel_reference`
0.40. A real distance moves a subscriber's scan from a 0.40 prior pull to 0.12
-- the same relief from the blend ceiling that a declared plate gives, with
nothing asked. (Ceiling 0.84 vs 0.80; band 0.20 vs 0.22.)

### What it would take — not started

1. A development build (EAS), not Expo Go; native code is required.
2. iOS `NeutriDepth` on ARKit. An ARSession and expo-camera's capture session
   cannot both own the camera, so the photo comes from the AR frame
   (`ARFrame.capturedImage`, ~1920x1440, above the 1568 the server uses) or the
   session hands over at the shutter. A plane raycast needs a moment of
   tracking; LiDAR devices give `sceneDepth` at once. depth.ts's 400 ms timeout
   is too short for plane initialisation.
3. Android: ARCore Depth API where supported; Camera2 `LENS_FOCUS_DISTANCE` is a
   crude, often uncalibrated fallback.
4. **FOV ships with depth or not at all.** `camera_fov_deg` is read in exactly
   one place, portion.py:1712, inside the depth rung, and multiplied by
   `depth_mm` at :1728. With no distance it is never used. (An earlier version
   of this entry called it a cheap independent win; Gil verified it is not.)
   Once a distance exists, a 5 deg FOV error is ~16% of area
   (portion.py:1702-1706), so the device's real FOV has to come with it.
5. Tilt. Rung 2 assumes the camera looks straight down; a raycast distance on
   a tilted shot runs along the ray, not the height. ARKit's camera transform
   gives height and tilt directly -- the same table-plane homography the
   corners work specifies.
6. Validation against a bench with a TAPE-measured lens height per photo, so
   the rung is scored against a measurement rather than a stated number.

---

## THE CARD'S PARALLAX DEBT — DIRECTION CONFIRMED, MAGNITUDE NOT. n=2. 12 Sep 2026

On 43 and 44 both arms share one detection, the same masks and the same 0.10
prior cap, so the card rung's grams over the declared plate's grams track the
frame-area ratio. Linear scale = sqrt(area ratio) = D/(D-h).

    photo  declared plate  card rung  grams ratio  linear  implied h: D 280 | D from card frame + 68 deg
    43     -4.8%           +23.2%     1.294        1.138   33.9 mm | 38.2 mm (D 316)
    44     +19.9%          +64.9%     1.375        1.173   41.2 mm | 44.1 mm (D 299)

- **Direction: CONFIRMED on both.** Heavier, as portion.py:1636-1678 documents
  for a card at the table plane sizing food raised above it.
- **Magnitude: NOT parallax alone.** It needs the food's footprint 34-44 mm
  above the card. Chips and grapes on a 3 g foam plate sit perhaps 5-20 mm up,
  which at D 280-316 is 4-8% linear and 7-16% of area -- the ~10% the file
  predicts.
- **Revised by the yardstick check below.** The card over-reads the
  TAPE-MEASURED 229 mm plate by a median +10% linear (+21% area) across 16
  photos -- the same excess it shows on the 222 mm rows. So the rest of the gap
  on 43/44 is most simply the card instrument itself at the table plane, not
  evidence that 222 is wrong. The 222 figure is still unmeasured; the tape
  settles it.

**Product consequence.** A card lying ON the plate, in the food's plane, has no
table-to-plate parallax: it removes the debt instead of correcting for it, and
IMG_4198 shows the detector finds a card on a white plate easily (consensus
6/6). Two limits: food with height still stands above the card by about half
its height (~3% linear, ~7% area for a 20 mm heap at D 300), and "put the card
on the plate" is still an ask, as the card already is.

---

## THE YARDSTICK — where every declared bench diameter came from. 12 Sep 2026

    rows                     declared  source in the repo                                     measured?
    17-30, 34-36 (plate)     229 mm    "the 9-inch 320 g plate" (bench_all.py:179); "228.6
                                       mm plate ... confirmed against a tape measure"
                                       (NEXT-SESSION.md:40-41); tape 228.6 vs card 240.0
                                       (portion.py:1642-1647)                                 YES, tape
    31, 32, 33 (crock)       114 mm    "the 4.5 in crock" (bench_all.py:141); 114.3 "confirmed
                                       against a tape measure" (same two sources)             YES, tape
    40-45 (foam plate)       222 mm    "8.75 inches of Styrofoam weighing 3 g"
                                       (bench_all.py:178-180; height_fit.py:107).
                                       No measurement recorded anywhere.                     NO -- ASSUMED
    14 (legacy) and the      254 mm    "carried at 267-269 mm for weeks before a tape said
    saved calibration                  254" (bench_all.py:65); "my dinner plate", 254         YES, tape
    tonight's beaded plate   260 mm    Gil: "10 1/4 wide"                                     tape pending

Both tape figures are exact inch conversions (9.00 in, 4.50 in), so they were
read to about an eighth of an inch, +/-3 mm.

**The 222 mm rows (40-45) rest on an assumed ground truth.** Every
calibrated-arm error on those six rows, photo 44's +19.9% included, is measured
against a number nobody has recorded putting a tape on.

### What the card implies for each plate

The card lies on the table and the rim stands above it, so the card reads the
rim LARGE. Card long side from the detector's quad (43, 44) or a colour-box
minAreaRect checked on a contact sheet; Hough rim, visually verified only on
23, 4190, 4197 and 4198.

    229 mm plate, 16 photos      card implies 242-278 mm, median 252   +10% linear, +21% area
    222 mm foam plate, 6 photos  card implies 230-256 mm, median 244   +10% linear
    114 mm crock, photo 31       card implies 144 mm, +27% (the repo's own tape-vs-card note: +30%)
    photo 32                     Hough found the underliner plate (244 mm), not the crock: invalid

**This does not convict the 222.** The card over-reads the TAPE-MEASURED 229
plate by the same ~10% it over-reads the 222 plate, and photo 44's +15% sits
inside the 229 plate's own +6% to +21% spread. The simplest reading is one card
instrument with a ~10% table-plane excess, not a wrong declaration.

**CLOSED 12 Sep.** Gil withdrew the "the declaration is wrong" hypothesis; no
tape is needed for this question. The 222 figure remains an undocumented
nominal size and should be recorded as such, but the bench's ground truth is
not the explanation for the card's excess.

**Measured card correction candidate -- NOT APPLIED.** Against a tape-measured
plate, a card at the table plane reads the plate rim +10% linear (median over 16
photos, range +6% to +21%), i.e. about +21% in area. Two cautions before anyone
applies it:

- It is about TWICE, in area, what portion.py:1675-1677 predicts (~10% of area
  for food ~28 mm above a table card), and twice the +5% linear of the file's
  single tape-vs-card note. The prediction's direction held; its size did not.
- It is measured on the plate RIM, which stands higher than the food on the
  plate floor, so it overstates the correction the food needs. And the card
  instrument varies: colour boxes and detector quads differed by ~5% on 44.

---

## A CLASS OF DEFECT: A PATH BUILT FROM BOTH ENDS WITH NOBODY WALKING THE MIDDLE — 12 Sep 2026

Four instances tonight. Each passes its own tests, because the defect is the
connection, not the code.

1. **Sourced densities.** `densities.csv` is loaded, then discarded:
   `USE_SOURCED_DENSITIES = False` (portion.py:1305).
2. **Card corners.** Computed and validated in reference_cv, returned on
   `ReferenceFind`, dropped at vision.py:1727.
3. **The card rung.** `reference_object` sits on the ladder but is unreachable
   whenever a plate diameter is declared -- every current bench row.
4. **Camera distance.** Client capture, request schema, storage and rung are
   all wired; the `NeutriDepth` sensor behind them does not exist.

### The standing check

For every rung, every constant table or data file, and every client field: **is
there a test that fails when the path is broken END TO END, not per stage?**

- **Rungs.** For each key of `_METHOD_CEILING`, drive the scan pipeline from a
  request the client can actually send (stubbed model and providers, no
  network) to a returned item with that `estimation_method`. A rung no real
  request reaches fails.
- **Tables and data files.** For each one loaded, perturbing one entry must move
  a production output. Loaded-and-ignored fails.
- **Client fields.** For each `ScanRequest` field: (a) changing it must change
  the `ScanResult`, and (b) something on a real device must produce it. A field
  nothing produces, or nothing reads, fails.
- **Returned fields.** Every field of a result dataclass (`ReferenceFind`,
  `PortionEstimate`) must be read by app code outside its own module -- the
  static counterpart of tests/test_wiring.py's script-reachability check.
- **Deliberate switches.** Anything intentionally off is listed, with its
  reason, in one allowlist, so "off" is a decision on record rather than
  silence.

### What it would flag today, besides the four (each checked in code or data)

- **The `multi_image` rung.** `reconcile_multi_image` (portion.py:2599) is
  called only by tests and scripts/portion_lab.py. The client sends up to 3
  shots and `ScanRequest` accepts 4, but they go into one detection call. 0 of
  521 `meal_items` are `multi_image`.
- **The measured-height path.** `USE_MEASURED_HEIGHT = False` (portion.py:743):
  the model's `height_ratio`, the plate ellipse's tilt and `reference_tilt_deg`
  reach no gram.
- **The depth-map height path.** `depth_map.measure_heights` is wired at
  vision.py:928; `depth_provider` is unset, so `NullDepth`.
- **`camera_fov_deg`.** Read only inside the depth rung (portion.py:1712); part
  of instance 4.
- **`/calibrations/progress` and `/calibrations/measure`** (routers/scans.py:138,
  156) have no caller anywhere in mobile/src.
- **The correction-learning loops.** The client does call `PATCH /meals/{id}`
  (MealDetailScreen.tsx:144), which feeds portion_learning, food_identity and
  calibration learning, yet `portion_learning` and `food_aliases` both hold 0
  rows. Wired end to end in code; nothing has ever arrived. UNPROVEN as a defect
  -- there may simply have been no corrections -- and exactly what an end-to-end
  test would decide.
- **Scale learning from the card.** `scale_learning.record` fires only when a
  card is found AND a vessel is named AND a plate ellipse exists
  (vision.py:1743). With card recall at 5 of 28 it is starved: 7 observations, 1
  calibration row, none learned.
- **`grams_from_measured_area`** (food_seg.py:1237) is reachable only from tests,
  as its own docstring says. It belongs on the allowlist, not in a fix.
- **The macro reference-table fallback.** The live `food_facts` source check
  constraint rejected resolver writes tonight (23514). Suspected; queued as a
  separate task.
- **An LLM's density entering as a measured one** (vision.py:1097-1102). Added
  after the clean re-run; ranked as a launch blocker at the top of this file.

### A review failure mode, not a code one: linear against area

Twice tonight, by Gil's own count, a linear figure was compared with an area
figure and read as a match it was not. The recorded instance: the card's excess,
+10% linear, set against portion.py's ~10% of AREA -- in area it is +21%, about
double. A scale error doubles when squared into area and
triples when cubed into volume, so a comparison that does not say which power
it is in is not a comparison. The standing rule for review: every percentage
about scale names its dimension -- linear, area or volume -- before it is
compared with anything.

---

## HOW SOFT IS THE BENCH? Weighed mass against the serving guess, 28 items. 12 Sep 2026

    weighed / ai_prior_grams   median 0.65   IQR 0.58-0.86   range 0.43-1.47   geometric mean 0.68
    within 0.80-1.25x   8 of 28      within 0.67-1.5x   14 of 28      within 0.5-2x   23 of 28
    SD of ln(weighed / prior)   0.291      SD of ln(weighed) across items   0.586

- **The portions do NOT sit at a typical serving; they sit ~35% below one.**
  The serving guess runs about 47% heavy on almost every item (only 2 of 28
  above it).
- **But the spread around that bias is narrow**: 0.29 in log units against 0.59
  for the across-food spread. The guess explains about 83% of the log-weight
  variance on this bench (r = +0.91), because the bench is one ordinary portion
  per food. Nothing on it asks whether a scan can tell 45 g from 180 g of the
  same thing. In that sense the bench is a soft test of the product, and the
  portion-sensitivity arm below is the first hard one.

---

## PORTION SENSITIVITY — THE TOP ACCEPTANCE TEST. Pre-registered before the photos. 12 Sep 2026

One food at about 45, 90 and 180 g; the same 260 mm taped plate, card and
framing. **n = 3 on one food is thin**: one residual degree of freedom, and a
single misdetection, vessel-name change or height-branch flip between frames
can move the slope on its own. It is still the first test of the thing the
product is for.

### Scoring

- **Primary metric: sensitivity, the log-log slope** of estimated grams on
  weighed grams across the three (OLS on ln). A scanner scores ~1.0, a lookup
  table 0. Log-log so a consistent 30% bias does not read as extra sensitivity.
- Also reported: the linear slope, and every point.
- **Arms, all from one detection per frame** (the sweep harness: one model call,
  scale step run per arm):
  - CAL: declared 260 mm.
  - UNCAL: no diameter, no calibration, no distance, card as found.
  - UNCAL_NOCARD: UNCAL with the card reference withheld -- a subscriber with no
    card.
- **Each arm twice: post-blend (as shipped) and pre-blend** (blend weights
  zeroed for the call, no code change), so the sensitivity the blend removes is
  read directly.
- Reported per frame: rung, vessel name, the serving guess, whether the measured
  footprint was used, and the height branch (separate pieces 9.2 mm or one mass
  21.0 mm).
- Arm-to-arm slope differences are exact for this draw, because the arms share
  each frame's detection. The absolute slopes carry frame-to-frame detection
  noise.

### AMENDED before the photos: a step is a confound, not sensitivity

The first version of this pre-registration folded "+0.6 if the height class
switches 9.2 -> 21.0 mm" into the predicted slope. That was wrong (Gil's
correction). A threshold crossing between frames is a STEP, not a slope, and a
result near 1.0 obtained through one would be falsely reassuring -- the worst
outcome this test can produce.

- **Report per frame every discrete state that multiplies the grams:** the
  height branch (separate pieces 9.2 mm / one mass 21.0 mm / no measured
  footprint); whether the measured footprint was used or refused (the two paths
  use different height tables); the rung; the vessel name; the food name; the
  serving guess. READ FROM THE RUN, never assumed: the harness captures
  vision.py's `portion_height_branch` log event (piece_share, applied, one_mass,
  height_mm) and each item's `measured_area_used`.
- **Report two slopes:** the raw slope over all three frames, and the slope over
  the frames that share ONE state on every item above.
- **If any state changes between frames, the raw slope is NOT evidence of
  portion sensitivity and is not reported as such.** With two frames left,
  report their log ratio and say it is two points.
- Gil is shooting a single layer, grapes not touching, at all three weights,
  which should hold the branch at separate pieces. Verify from the logs.

### Predicted slopes, with reasoning (amended)

For frames sharing one state. With one layer of non-touching grapes that state
is expected to be separate pieces (9.2 mm), footprint measured.

- **Pre-blend geometry, CAL: ~0.95 (plausible 0.75-1.1).** At 45-180 g one item
  covers well under the 0.35 reference coverage, so the spread factor sits at
  its 1.10 clamp and is constant (portion.py:2348-2349). With the height fixed,
  grams follow the measured footprint, and one layer of separate grapes covers
  area in proportion to its count, ~mass^1. Mask edges and small-item
  resolution pull the small end, so slightly under 1.
- **Post-blend CAL: ~0.91 x pre-blend, so ~0.87.** With the serving guess
  constant across the frames and ~150 g, only the 45 g frame disagrees by more
  than 1.5x; the 0.10 cap moves ln(45 g) by 0.1 x ln(150/45), about 9% of the
  slope.
- **Post-blend UNCAL on `vessel_reference`: 0.50-0.67 x pre-blend, so ~0.55.**
  The vessel size factor is constant if the name is stable, so it leaves the
  slope alone. The 0.40 cap pulls the small frames toward the guess: 0.67x
  under a dinner_plate name, 0.50x under side_plate.
- **UNCAL named "paper" (`ai_prior`): 0.0.** No geometry; every frame returns the
  guess.
- **UNCAL with the card found (`reference_object`, cap 0.10): ~ CAL post-blend.**
  That is why UNCAL_NOCARD is its own arm.
- **Largest risks, every one a step:** the vessel name changing (a +/-39% area
  step), the food name changing (the trail-mix pair moved the guess 50 -> 30 g
  on a name alone), the footprint refused on one frame, grapes touching on the
  180 g frame.
- **Harness:** the corrected paired harness below, which shares nutrition facts
  across arms, extended to capture the per-frame states above. The first
  sweep's harness does neither.

---

## THE SWEEP, RE-RUN CLEAN — nutrition facts shared across arms. 12 Sep 2026

### What was wrong with the first sweep's pairing

- The harness cached the vision response but let `build_items` re-run
  `resolver.resolve_many` for every arm, while USDA was returning HTTP 400 on
  and off.
- **The weight path depends on whether USDA answers.** When it fails, the
  resolver falls back to `_ai_estimate`, a reasoning-model call that returns a
  `density_g_ml`. vision.py:1102 passes that as `density=`, and `density_for`
  puts an explicit density ahead of the table. 75 of the 116 `food_facts` rows
  are `ai_estimate` rows carrying such a density; the 33 USDA rows carry none.
- Photo 18 in the blend-off replay showed it: the one arm whose zucchini lookup
  failed got 88 g; the arms that then hit a cached USDA row (no density, so the
  vegetable group's 0.55) got a 51 g base.
- Six arm-items moved between the first sweep and the clean re-run, most of them
  in BOTH arms at once -- the fact changed between runs: zucchini 88 -> 52 g;
  rice 151 -> 107 g and 94 -> 71 g; caesar salad 350 -> 98 g, which was the
  first sweep's largest outlier (+185% -> -21%).
- The headline paired change survived: +1.9 -> +3.0 points (CI -14.4 to
  +20.5).

### A class member: an LLM's density entering as a measured one

vision.py:1097-1102 says "ONLY a real measured density is passed as explicit."
The resolver's fallback supplies a reasoning-model guess through that same
field, so a language model's density overrides the sourced table -- and whether
it does depends on USDA's availability at the moment of the scan. Wired end to
end, each stage correct on its own, broken in the connection.

Also, for macros rather than grams: USDA matched "white rice" -> "beans and
white rice", "grilled zucchini slices" -> "zucchini, pickled", "baked bread
roll" -> "roll, egg bread". Belongs with the queued USDA task.

### Photos 44 and 45: pre-blend geometry, w and post-blend grams

Weighed 90 g; serving guess 150 g on both.

    photo  arm                      pre-blend geometry   w      shipped   error
    44     CAL (plate 222 mm)       107.9 g              0.00   107.9 g   +19.9%
    44     UNCAL (card rung)        148.4 g              0.00   148.4 g   +64.9%
    45     CAL (plate 222 mm)       106.5 g              0.00   106.5 g   +18.3%
    45     UNCAL (ai_prior)         none                 --     150.0 g   +66.7%

- **45's +66.7% equals the guess's +66.7% because it IS the guess.** Named
  "paper" with no card found, the rung is `ai_prior` and no geometry exists to
  blend.
- **44's +64.9% is the card rung's own geometry**, not the blend. The blend did
  not fire on either photo: the guess sits within 1.5x of every geometry.

### Blend weights as observed

w reached exactly its cap wherever geometry and guess disagreed by 2.5x or more:
0.10 on `plate_reference` (17, 22, 35 and 36 calibrated) and 0.40 on
`vessel_reference` (22, 24, 31-33, 35, 36 and 40 uncalibrated).
