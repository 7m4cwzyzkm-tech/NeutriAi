# Measured height, the tilt gate, and the plate w/h convention

12 Sep 2026. Written here, not in HANDOFF.md, because another session is committing
to HANDOFF. To be folded in by that session. Nothing in the code was changed.

**Fold-in order: section A ranks as a LIVE BLOCKER, above the blend ceiling.**

---

## A. LIVE BLOCKER: scale learning teaches every portrait user a plate ~38% too small

### The defect

`scale_learning.observe_width_mm` (scale_learning.py:106-126) computes
`card frame WIDTH mm x plate_ellipse w`. The model reports w as a fraction of the
image's LONG side (section 4). On a portrait photo the frame width is the short
side, so every observation carries x0.75, plus the model's own ~0.10 under-read.

Measured bias, observed width / true width, from 25 bench photos:

    portrait    n=19   median 0.625   range 0.53-0.81
    landscape   n=6    median 0.841   range 0.77-0.97
    Gil's real readings, photo 14, portrait, taped 254 mm:  0.623 (x5), 0.499 (x1)

### Every write path

1. **Card observation, on every scan** (vision.py:1743-1752): a card detected, a vessel
   named in `MEASURABLE_VESSELS`, and a plate ellipse -> `record(source=
   "reference_object")` -> `vessel_observations` -> `refresh` -> once there are 4
   observations, a `scan_calibrations` row for that vessel name (`learned=True`, with
   `real_diameter_mm` AND `real_area_mm2`).
2. **Tape observation** via `POST /calibrations/measure` (routers/scans.py:156) -> the
   same `record`/`refresh`. No mobile caller exists.
3. **Correction learning** via `PATCH /meals/{id}` -> `calibration.learn_from_correction`
   (calibration.py:114). This one IS called by the app (MealDetailScreen.tsx:144). It
   writes `real_diameter_mm` directly and never files an observation, so
   `SOURCE_ERROR["correction"]` in scale_learning is never used.
4. **Direct write** via `POST /calibrations` (routers/scans.py:116), user-typed, not
   learned.

### What a user WITHOUT a tape ends up with

`scale_learning.estimate`, simulated with the measured per-photo bias, 2000 users each,
plate 254 mm. Median [5th-95th percentile]:

    user                  after 1 scan     after 5 scans               after 20 scans
    portrait only         nothing          158.5 mm [147-174]  -38%     158.5 mm [152-165]  -38%
    80% portrait          nothing          165.3 mm [151-188]  -35%     165.1 mm [158-174]  -35%
    landscape only        nothing          213.2 mm [203-226]  -16%     213.2 mm [208-219]  -16%
    claimed error, portrait                5.3%                          2.8%
    with the card's measured +10% table-plane excess: portrait -31%, landscape -10%

- **It converges on the wrong value; it does not drift.** Observations never read the
  learned calibration, so there is no feedback loop -- a fixed biased point.
- **It grows more confident as it stays wrong.** The claimed error falls 5.3% -> 2.8%
  while the bias stays at -38%. That number reaches only the progress card
  (`/calibrations/progress`), which has no mobile caller.
- **The simulation reproduces the live data.** Portrait-only publishes 158.5 mm; Gil's
  real card readings were 158.33 mm.
- **What Gil would have without the tape:** his six card readings publish
  **150.79 mm** (-41%), claimed error 3.5%, `usable=True`.

### Which rungs consume it, and at what ceiling

- vision.py:1677-1690 picks the calibration whose vessel matches the model's vessel
  name (no diameter or calibration_id sent). `_calibration_fits` accepts it.
- `GeometryHint.reference_area_mm2 = real_area_mm2` -> `mm2_per_frame` rung 1,
  **`plate_reference`, ceiling 0.92, band 0.14, serving-prior pull capped at 0.10** --
  the most confident rung there is, from the 4th card photo.
- **Grams go as width squared:** -38% width is x0.384 area, **~-62% grams** pre-blend;
  Gil's 150.79 mm is **-65%**; -31% (card excess) is -52%. The 0.10 blend cap cannot
  rescue that.
- **It overrides the card that taught it.** Rung 1 outranks rung 2a, so once a plate is
  learned, later photos WITH a correctly detected card are scaled by the wrong plate --
  and each of them files another biased observation.
- Also fed to `_measured_heights` (depth; off, NullDepth) and `reference_width_mm`
  (measured height; off).

### Would anything downstream notice? No.

- Bounds: 60-600 mm in `observe_width_mm` and `record`; 60-500 mm in `calibration.py`;
  60-600 in depth_map. A 158 mm "dinner_plate" passes all of them.
- `OUTLIER_RATIO = 1.8` is relative to the median of the user's own observations, so a
  consistently wrong set passes.
- Nothing compares a learned width with the vessel's prior (`VESSEL_WIDTH_MM`
  dinner_plate 270), with the pixel rim, or with the card's own frame. The
  frame-size rail (120-1000 mm) guards only the card rung.
- **User corrections cannot rescue it.** `learn_from_correction` updates
  `real_diameter_mm` but leaves `real_area_mm2`, and `mm2_per_frame` reads the AREA
  first (portion.py:1616) -- so once `refresh` has written an area, a correction moves
  no gram. The next card photo's `refresh` rewrites both anyway. (Another path built
  from both ends.)

### What protected Gil — the tape, ONLY, and conditionally

- **For his calibration row, the tape was the only protection.** Without it, his six
  card readings clear `MIN_OBSERVATIONS = 4` and publish 150.79 mm.
- **Separately, the bench scores were protected** because every bench row sends
  `plate_diameter_mm`, which skips the calibration lookup entirely. That protects the
  bench numbers, not Gil's stored row.
- **The tape protection itself is conditional.** `_weighted` drops any reading beyond
  1.8x the median -- the tape included -- while `estimate` sets `measured` from ALL
  observations. `refresh` skips the write only when `not measured`. Replayed on Gil's
  real observations:

      + 0 to 5 more readings of 126.66 mm   median >= 142.5   publishes 254, keeps the tape
      + 6 more                               median 126.66     tape dropped as an outlier;
                                                               refresh WRITES 136.6 mm, learned=False

  The failure point is a card median below 254 / 1.8 = 141 mm. After it, the stored row
  claims to be user-measured while holding inference, and stays that way as long as the
  median stays low. Realistic only for framings where the model reads w low (2 of 19
  portrait bench photos sit below 0.556); proven as a mechanism, not a likely event for
  Gil.

### Exposure today

`food_scans` has 2 distinct users; `vessel_observations` and `scan_calibrations` have 1
(Gil). No subscriber has been damaged yet. The path is the one every new user is sent
down: the trial-period calibration arc, with the scan screen's card prompt.

### Fix — specified, not built

1. The convention fix in section B. **It is not enough for scale learning:** after it,
   portrait matches landscape at ~0.84 -> still ~-16% width, ~-29% grams, from the
   model's under-read.
2. **Take the model out of scale learning.** Measure the plate's width in pixels (the
   Hough / "pixels" rim, `food_seg.plate_is_measured`) and multiply by the card's mm/px.
   Both are pixel measurements; no model fraction enters.
3. **Bound a learned width** against its vessel's plausible range, and require it to
   agree with the pixel rim before publishing.
4. **Tape safety:** a tape reading is never outlier-filtered, and `refresh` writes
   `learned=False` only when a tape reading is among the rows it kept.
5. **Correction learning** writes `real_area_mm2` with `real_diameter_mm`, or rung 1
   reads the diameter.

---

## B. The convention is the root: ONE normalisation at the parse boundary

**Where:** `vision._plate_ellipse` (vision.py:504) is the only place a model
`plate_ellipse` becomes numbers. Every consumer reads its output: `GeometryHint
.plate_ellipse_wh` (vision.py:1698), which feeds `tilt_deg` AND scale learning
(vision.py:1745), plus scripts/depth_lab.py:163, measure_lab.py:127 and
scan_debug.py:177.

**The change:** `_plate_ellipse(detection, aspect)` converts the model's long-side
fractions into per-axis fractions, which is what every consumer already assumes:

    L = max(W, H)
    w_axis = w x L / W        h_axis = h x L / H
    portrait (aspect = W/H < 1):   w_axis = w / aspect,  h_axis = h
    landscape (aspect > 1):        w_axis = w,           h_axis = h x aspect

With no aspect it returns None -- no reading, never a guess. The per-axis contract goes
in `GeometryHint.plate_ellipse_wh`'s docstring. `tilt_deg`'s conversion and
`observe_width_mm`'s product are then correct as written, and the three sites are not
patched.

**Deliberately not in this change:**

- **The prompt.** Any wording change re-rolls the model's convention and has to be
  re-measured first.
- **The ~0.10 under-read.** That is a measurement bias, not a convention; folding a bias
  into a unit conversion hides it.

**The test that would have caught it** -- replacing
`test_tilt_is_measured_in_one_set_of_units`, which fed invented per-axis numbers:

- **Fixtures from real recorded model responses:** portrait 23 and 35, landscape 22, 34
  and 44 (already cached from the uncalibrated sweep). Each stores the response JSON, the
  image W and H, and the pixel-measured rim extents -- **not the photo bytes, which show
  a live card with the name legible.**
- **Assert** that a round plate shot top-down (pixel rim roundness >= 0.97) reads as
  SQUARE after normalisation, |w_axis x W - h_axis x H| <= 0.05 x L (one grid step),
  and that `tilt_deg < MIN_TILT_DEG`.
- **Assert portrait/landscape parity:** normalised w_axis over the pixel width fraction
  shows the same distribution in both orientations -- no x0.75 split.
- Today this fails: photo 23's (0.4, 0.4) parses to 41.4 deg, and to 0.75 non-square in
  pixels.
- **A live contract check, paid and on demand:** re-detect the fixture photos and re-run
  the assertions. A fixture guards the parser; only a live call notices the MODEL
  changing its convention.

---

## C. Recorded: THE EVIDENCE THAT DISABLED USE_MEASURED_HEIGHT DOES NOT SURVIVE

The recorded regression (7.4% -> 11.8%, "lowered every item") was not a measurement
failing. It fits a broken gate admitting heights invented from overhead frames: the
parser reads every 3:4 photo as 41.4 deg, the "two angled meals" measure 13 and 18 deg
in pixels, and 08's own filename says topdown. On photo 35 the parser's tilt admits an
invented 23 mm burger height and takes the item from -10.6% to -45.0%; with the pixel
tilt of 3.2 deg the output is identical to switch-off. **One photo, two items --
evidence, not proof.** Whether the height path works is untested either way, and
tomorrow's shoot tests it.

## 0. A premise corrected first

On the current bench `height_ratio` is NULL on 39 of 41 items, not present on 39. The
two non-null values are both on photo 35 (burger 0.10, fries 0.05), shot top-down. The
gate is `USE_MEASURED_HEIGHT and height_ratio and ref_w and tilt >= MIN_TILT_DEG`
(portion.py:2296), so a null height never fires however wrong the tilt is.

## 1. Is there a ruler-measured height for meals 06 or 08? No.

Searched: bench_all.py, the bench scripts, app code, docs (HANDOFF, NEXT-SESSION),
scan_results.csv (no height column), git history, and the scan rows.

- `food_scans.raw_vision` is empty on all 74 scans of photos 06/07/08, so no
  `height_ratio`, plate ellipse or computed tilt was ever stored for them. There is no
  scandebug output on file.
- The 7.4% -> 11.8% comment arrived in the import commit 32f734c (11 Sep), so git
  cannot date that evaluation.
- The only tape in those photos is 08c's, laid flat across the plate for a footprint
  (portion.py:532), not a height. scripts/depth_lab.py already says so: "the ruler it
  wants does not exist".
- 06 and 08 ARE weighed (scan_results.csv, per item).

## 2. The contradiction: "every photo reads 41.4 deg" vs "fired on the two angled meals"

Both can be true. The tilt gate admits every 3:4 photo, but the switch also needs a
non-null `height_ratio`, and the model returns null from overhead almost always. So
"fired on two meals" means the two meals on which the model reported a height. The
tilt was never the gate that mattered; the model's own null discipline was.

"Angled" is most likely the parser's word, not the photographs':

- 08 is named `08-plate-mole-chicken-topdown.jpg`.
- Pixel-measured rim tilt (ray-traced rim ellipse, checked on a contact sheet):
  06 18 deg (from a 480x640 file), 07 14, 08 13, 08b 16, 08c 20, 13 22, 14 20.
  Neither 06 nor 08 reaches MIN_TILT_DEG = 20. The fits on 07 and 13 are loose.
- Either parser produced ~41 deg on overhead 3:4 photos. The docstring of
  `test_tilt_is_measured_in_one_set_of_units` records an earlier parser that read raw
  h/w as cos(tilt) and "claimed 41 degrees of tilt on an overhead photo"; the fix added
  a per-axis conversion, which the model does not follow (section 4). Which model
  behaviour held at evaluation time cannot be recovered.

"Fired" versus "changed materially" cannot be separated: the notes that would say were
never stored.

## 3. The free re-run — what it can and cannot show

**It cannot recover the 7.4%.** The evaluation's detections were never stored, and on
the current bench only photo 35 carries a height. The other 25 photos are provably
unchanged by the switch, because `height_ratio` is null and the condition
short-circuits.

**Photo 35, one cached detection, nutrition facts shared, one SAM2 call.** Pixel tilt
3.2 deg; parser tilt 41.4 deg.

    arm                              burger (145 g)       fries (65 g)
    switch OFF, as shipped           129.7 g  -10.6%       23.8 g  -63.4%
    switch OFF, pre-blend            124.6 g  -14.1%       19.4 g  -70.2%
    switch ON, parser tilt 41.4      79.7 g  -45.0%        14.2 g  -78.2%
    switch ON, parser, pre-blend     70.2 g  -51.6%        10.9 g  -83.2%
    switch ON, pixel tilt 3.2        129.7 g  -10.6%       23.8 g  -63.4%   (rejected: identical to OFF)

The note it emitted: "Photographed at about 41 degrees, so the height could be measured
rather than assumed: 23 mm against a typical 42 mm."

- **Candidate 3 -- the feature was never honestly evaluated -- is supported.** The broken
  gate admitted a height invented from overhead and lowered both items, the recorded
  signature. A correct tilt removes it entirely.
- n = 1 photo, 2 items: evidence, not proof.
- **Tomorrow's shoot is still diagnosis, not validation.** No angled photo with a real
  height exists, so under-reporting (k) against PROFILE_FACTORS (m) is untested. What
  changed: the evidence that disabled the feature no longer counts against it.
- A cheaper intermediate, NOT free: re-detect legacy 06 and 08 from the local files (2-6
  vision calls) and run the same three arms against their weighed rows. The model and
  prompt have changed since, so it would not reproduce the original run.

## 4. 41.4 is a fingerprint — every place a reported w,h meets a convention

**What the model actually does, measured on 26 bench photos:** it reports `plate_ellipse`
w and h BOTH as fractions of the image's LONG side, in portrait and landscape alike.

    model value vs pixel rim           mean |diff|   portrait   landscape
    w vs width fraction (per axis)       0.245        0.289       0.100
    w vs long-side fraction              0.099        0.099       0.100
    h vs height fraction (per axis)      0.147        0.100       0.302
    h vs long-side fraction              0.098        0.100       0.091

Long-side fits both orientations, and it is consistently ~0.10 LOW (e.g. 0.5 reported
where the rim is 0.63). This is what the prompt implies ("from directly above is a
circle and w == h"; prompts.py:75-80).

| Place | Convention assumed | Consequence |
|---|---|---|
| `GeometryHint.tilt_deg`, portion.py:968-1000 | per-axis fractions, converted by aspect | **41.4 deg on every 3:4 photo.** Latent: only consumer is the switched-off height path |
| `food_seg.plate_surface` box ellipse, food_seg.py:366-378; vision.py:905-912 | the model box's axis ratio | **Caught once**: 8 of 16 photos read 41.4. Guarded: depth accepts only "pixels" plates |
| the earlier tilt parser, test_portion.py:1672-1674 docstring | raw h/w as cos(tilt) | **Caught twice**: the fix moved the defect instead of removing it |
| **`scale_learning.observe_width_mm`, scale_learning.py:16, 106-124** | **w as a fraction of frame WIDTH** | **The third, and it is LIVE** (below) |
| scripts/depth_lab.py:163-184 | inherits `tilt_deg` | reports 41.4 |
| scripts/scan_debug.py:175-181 | `tilt_deg` without aspect | always None, printed as "not a round vessel" |
| scripts/mask_lab.py:137-146 | per-axis, via `GeometryHint` | lab only; depends on its own plate dict |
| depth_map.py:185-189, depth_probe.py:223, segment_lab.py:76 | pixel minor/major | fine |
| reference_cv.py:103-113 | card pixel aspect | right convention, noisy (a 2% aspect error reads 11 deg) |

**The live one, measured.** Every card-derived plate width ever recorded
(`vessel_observations`, source `reference_object`) comes from photo 14 -- portrait, plate
tape-measured at 254 mm:

    158.33 mm x5, 126.66 mm x1       -38% and -50%

The code multiplies the card's frame WIDTH (the short side on a portrait photo, ~317 mm)
by a w that is a fraction of the LONG side: x0.75. Add the model's ~0.1 under-read
(0.5 and 0.4 reported against a true long-side 0.60) and it lands on exactly those
values. Gil's calibration survived only because `refresh` never replaces a tape
measurement with an inferred one. **For any user without a tape, the trial-period
calibration arc would learn a plate about 40% too small -- about 60% too little area.**
Outside backend/app/services/nutrition/**, so reported, not touched.

## 5. The shoot spec — confirmed, with two notes

Gil's arithmetic holds. One 0.05 step of `height_ratio` is 0.05 x D:

    40 mm pile, 260 mm plate    13 mm step    +/-33%
    60 mm pile, 200 mm plate    10 mm step    +/-17%

Tallest stable pile of cooked rice on the smallest flat plate, not a bowl. Frames
top-down, ~30 deg and ~60 deg, card on the plate in all three. Weighed mass, ruler peak
height above the plate surface, tape plate diameter, loosely packed cup weight.

The minimum the discriminator needs:

- **One pile is enough.** Test 1 (k = r x D / H) needs one angled frame, H and D. Test 2
  (m = M / (rho x A x H)) needs M, the top-down footprint A from the card on the plate,
  H, and rho from the cup.
- **The second angle is needed only if k < 1**, to tell a constant under-report
  (k(30)/k(60) = 1) from the model reporting projected height
  (k(30)/k(60) = sin 30 / sin 60 = 0.58). Shooting it now avoids a second session.

Notes:

- **The harness must take tilt from the pixels** (card on the plate, or the rim
  ellipse), never from `GeometryHint.tilt_deg`. The parser will say ~41 deg on the
  top-down frame too.
- The model under-reads the plate's width by ~0.10 of the long side. If it under-reads
  heights the same way, k falls below 1 from reading, not physics; `height_ratio_self`
  (height over the food's own width) is a vessel-free cross-check.
