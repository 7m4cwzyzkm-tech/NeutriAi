# NeutriAI — handoff brief

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

- Supabase migrations **0017 and 0018** unrun.
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

### Whether it has ever been used

- **Saved logs: never.** `scan_results.csv` and the survey files record 39
  `plate_reference` and 7 `pixel_area`, zero `reference_object`.
- **Production: yes, once.** 12 of 521 `meal_items` rows are
  `reference_object` -- ONE meal, `11-kebab-paper-card` (skewer, cherry
  tomatoes, potatoes), scanned four times on 7-8 Sep. Food on paper, no plate.
  So the rung is CONNECTED; it is not the built-and-never-wired defect.
- **The bench cannot exercise it at all.** Every `CASES` row passes a plate
  diameter, and `mm2_per_frame` puts rung 1b `plate_reference` above rung 2a
  `reference_object`. Photo 36 would score `plate_reference` even with the
  card found; 43 and 44 DO find it and still score `plate_reference`. The
  bench's own comment on 36 says the decision between the two "has never been
  measured" -- and as built, it cannot be.

### Where both exist, they disagree

On 43 and 44 the card's mm/px is **1.117x and 1.154x** the Hough rim's (222 mm
plate). The sign matches the parallax debt `mm2_per_frame` documents -- card
on the table, rim nearer the lens -- but at the 280 mm camera distance `CASES`
records for those rows it would need a rim 29-37 mm above the table, and that
plate's rim height is not recorded. Unresolved; not assumed either way.
