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

---

## The one live hypothesis, and it is untested rather than refuted

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

