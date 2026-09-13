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

---

## D. Plan for 13 Sep, dependency-ordered. Nothing below is built tonight.

### 1. USDA POST — precondition, in flight in the worktree

Built and verified; UNCOMMITTED at session end. See section G.

### 2. Axis convention (section B), landed after USDA

**It does NOT move the bench.** The parsed ellipse reaches exactly two consumers:
`GeometryHint.tilt_deg`, read only by the height path (`USE_MEASURED_HEIGHT = False`),
and `scale_learning.observe_width_mm` (vision.py:1745), whose calibration the bench
never reads because every bench row sends `plate_diameter_mm`. The depth path takes
its ellipse from pixels. Proof when it lands: re-run the paired replay on the cached
detections before and after; grams must be identical. (Bench scans with a detected
card will file different `vessel_observations` rows -- a side effect, not a gram.)

**What it does:** removes the x0.75 portrait factor from scale learning, and unblocks
the tilt gate. **It closes part of the 62%-low error, not all of it:** the model's
~0.10 plate under-read remains, so learned widths stay ~-16%, about -29% grams, until
scale learning uses the pixel rim (section A, fix 2).

### 3. Measured height — ready when Gil's data lands

Harness per section 2 of the earlier notes. Tilt from the pixels, never the parser.
Discriminators k = r x D / H and m = M / (rho x A x H) as pre-registered, plus
`height_ratio_self` as the plate-independent check.

### 4. Monocular depth in the DEPTH_PROVIDER socket — ASSESSMENT, not built

**Verdict: the idea survives. The rung does not demand an absolute distance.** It has
two hard prerequisites.

**What the rung expects** (vision.py:866-931, depth_map.py):

- raw photo bytes, the model's `plate_bbox`, and **`plate_diameter_mm`**;
- a provider whose `units == "relative_inverse"`;
- a plate mask measured from pixels (`plate_is_measured`; box ellipses refused), not
  cropped by the frame, with at most 35% of the rim annulus under food, and a plane fit
  with rim roughness at most 0.35 of the food's relief;
- plate tilt **20-65 deg**, from the plate mask's own second moments.

It returns a MEAN height per item, which portion.py uses with the profile factor set to
1.0 (portion.py:2428), overriding the prior and any learned height (portion.py:2143-2149).

**A relative map is anchored without any distance.** The model returns D = s/Z + t.
Subtracting the plate plane kills t (a world plane is exactly affine in 1/Z across the
image). The plate's foreshortening pins s/Z^2, giving h = dD x k x sin(tilt) / |grad D|
with k = diameter / major axis px (depth_map.py:305-380). No focal length, no camera
distance. **Tonight's offline self-test** (`python -m scripts.depth_probe`, ray-traced,
no network): 20.50 mm recovered against 20.00 mm at 33.7 deg, and a colour-mapped map
refused.

**The two prerequisites:**

- **A diameter.** Height scales with k, linearly in the diameter. For a subscriber the
  only diameter is a calibration -- the section A blocker. A learned plate -38% in
  width would scale heights -38% on top of -62% area: **grams x0.24, -76%.** Section A
  must land before depth reaches a subscriber.
- **An angled photo.** Error amplification 1/tan^2(tilt) + 2: 9.5x at 20 deg, 5x at 30,
  3x at 45. Top-down photos are refused, not answered.

**How the plate anchors it, and what the card adds:**

- The plate supplies the plane (an annulus at 0.80-0.97 of its radius), the gradient
  and the tilt.
- The card is not used by the depth path today. **A card lying ON the plate floor** is
  a known-size patch of that same plane: it gives k at the plane (with tilt from its
  corners), which would remove the dependence on `plate_diameter_mm` -- and so on scale
  learning. The card is too small to carry the gradient itself; the plate annulus still
  does that.
- **A card on the table** sits on a different plane (the measured +10% linear excess)
  and should not anchor depth.

**Cost:** `chenxwh/depth-anything-v2` on Replicate: A100, ~2 s, ~$0.0013 per run
(model page, 12 Sep). SAM2 is ~2 cents per photo in this repo, so depth adds roughly
7% of the SAM2 cost, plus ~2 s and cold starts.

**LICENCE TRAP, verified from the model's API schema:** input `model_size` has enum
Small / Base / Large and **defaults to "Large"**. Small is Apache-2.0; Base and Large
are CC-BY-NC-4.0. A default configuration would ship non-commercial weights in a paid
app. The config must send `model_size: "Small"` (via `depth_model_input`) -- and Small
is the weaker model. The output schema was not expanded: whether a grey map comes back
(`depth_hosted` refuses colour maps) is unverified until one paid `dev depthcheck 13`.

**Which bench photos can test it from cached images tonight: none.**

- Every scored bench photo is top-down by pixel rim tilt (3.2-14.1 deg), below the
  20 deg floor, and no depth map has ever been cached.
- **`depthsurvey.txt` in the repo root is stale and wrong.** Its "9 of 16 measurable" and
  its seven readings of exactly 41.4 deg come from box ellipses, before
  `plate_is_measured` existed. Today's `depth_probe.plate_of` refuses them. Pixel tilts
  for those photos are 13-22 deg.
- Legacy photos at the floor with weighed grams -- 13 (22 deg), 08c and 14 (20 deg) --
  would be paid, at ~9.5x amplification, with no ruler heights: a weak test.
- The first valid test is tomorrow's 45 deg frames: about 8 foods x $0.0013.

**Honest failure modes:**

- **Top-down photos:** refused, and they are the bench's whole protocol.
- **Reflective food** (sauce, glaze, grapes): specular highlights make false bumps and
  dips; the mean moves with them.
- **Shadow:** the food's own shadow reads low. In the rim annulus it corrupts the plane
  (the roughness gate may catch it); inside the item mask it is clipped at zero but
  still lowers the mean.
- **A plate filling the frame:** refused as cropped (2% border tolerance), not wrong. A
  plate more than 35% covered is refused too.
- **Patterned or embossed rims:** texture copies into depth. On Gil's beaded plate the
  0.80-0.97 annulus sits on the bead ring.
- **Bowls and crocks:** the support surface is not the rim plane, and the path needs a
  plate.
- **Thin food** (tortilla, chips): below the resolution of an 8-bit map.
- **Edge smoothing** at pile boundaries bleeds plate into food, and a mask over separate
  pieces includes their gaps -- both read low. The item mask is the colour rule, not SAM2.
- **The model's output is only approximately affine in 1/Z.** The derivation is exact
  for an exact relative-inverse map, and verified only on a ray-traced one.

**Recommendation:** do not build before the ruler set exists. When it does, configure
Small, run `dev depthcheck 13` once to prove the output format, then score the depth
mean height against m x H from the ruler on each 45 deg frame.

### INTAKE FORMAT for the calibration set — specified before the photos

**Files.** Photos go under `photos/calib-2026-09-13/` (gitignored; the card must be
non-live). **Phone originals only**, with EXIF: tonight's chat-forwarded images were
recompressed to 1500x2000 against 4284x5712 originals. One manifest,
`photos/calib-2026-09-13/session.json`:

    {
      "session": "2026-09-13",
      "scale":  {"model": "...", "resolution_g": 1},
      "ruler":  {"graduation_mm": 1, "zero_offset_mm": 0},
      "card":   {"live": false, "placement": "on_plate_floor"},
      "cup":    {"cup_id": "C1", "empty_g": 0, "water_full_g": 0},
      "plates": [{"plate_id": "P1", "description": "white beaded ceramic",
                  "type": "flat|rimmed|foam|bowl",
                  "diameter_mm_tape": 260, "rim_height_mm": 0,
                  "measured": "outer edge to outer edge, widest"}],
      "foods": [{
        "food_id": "F01", "name_as_eaten": "cooked white rice", "prep": "boiled",
        "shape_word": "mound",
        "plate_id": "P1",
        "mass_g": 0, "mass_method": "plate tared, food added",
        "bulk": {"cup_id": "C1", "loose_food_g": 0, "packing": "spooned, not pressed, levelled"},
        "ruler_peak_mm_typed": 0,
        "pile_moved_between_frames": false,
        "frames": [
          {"file": "IMG_0000.jpeg", "role": "topdown"},
          {"file": "IMG_0001.jpeg", "role": "angled_45", "ruler": "standing, touching the peak"},
          {"file": "IMG_0002.jpeg", "role": "side_level", "ruler": "standing, touching the peak"},
          {"file": "IMG_0003.jpeg", "role": "scale_display"}
        ],
        "notes": ""
      }]
    }

- **`shape_word`** is the code's `_classify_shape` vocabulary (flat, mound, loose,
  cluster, liquid, wrapped, topped_flat, chunky), not the refuted five-class taxonomy.
  ~8 foods maps onto 8 words. For `liquid` in a bowl, the ruler reads fill depth.
- **Typed numbers are cross-checks.** Every quantity that can be read from a photo is
  read from the photo. `scale_display` is optional and cheap: mass read from the
  display, not transcribed.
- **Do not move the plate between the top-down and angled frames:** m needs the same
  pile's footprint A and height H.
- Cup volume is derived from `water_full_g - empty_g` (1 g = 1 ml). Loose-cup packing
  may differ from pile packing; record how it was filled.

**What this set measures, stated precisely:** the true PEAK height H of each pile, and
the true profile m = M / (rho x A x H). It does **not** measure `HEIGHT_PRIORS_MM`
directly. Those rows are effective heights fitted against the RAILED area -- and
`MEASURED_HEIGHTS_MM` and the 9.2/21.0 pair against measured footprints -- so each
absorbs its paired area and density bias. Comparing a ruler H to a table row is the pair
trap again. The set gives the physical truth each table should reproduce once its
paired terms are re-derived together.

### READING THE RULER FROM THE PHOTO, not trusting a typed number

**Placement,** asked of Gil: ruler vertical, zero end on the plate surface (record
`zero_offset_mm`, the end-to-zero distance), graduated face to the camera, touching the
pile at its highest point.

**Preferred frame: `side_level`** -- camera at pile height, square to the ruler. A peak
point x mm nearer the camera than the ruler shows up offset by ~x in a 45 deg frame; a
level frame removes that. The 45 deg frame's ruler is the cross-check.

**Extraction, per ruler frame:**

1. The harness writes an overlay. Annotated (clicked once, stored in the manifest as
   pixel coordinates): the ruler axis and three labelled graduations, e.g. 0, 50 and
   100 mm.
2. A 1-D projective map along the ruler from those three points (cross-ratio). It is
   exact under perspective; uniform tick spacing is not assumed.
3. **Automatic check:** the FFT period of intensity along the ruler axis gives the
   local px per mm. It must agree with the three-point map within 2%, or the frame is
   re-annotated.
4. **Pile top:** the highest food pixel (SAM2 mask, else the colour mask) within a narrow
   band beside the ruler, projected onto the axis -- horizontally, in the level frame.
5. H_photo = map(pile top) - map(plate contact) - `zero_offset_mm`.
6. **Compared with `ruler_peak_mm_typed`:** a gap larger than max(3 mm, 7%) is flagged.
   The photo value is used; the typed one is kept for audit.

**Footprint A** comes from the top-down frame: the card-on-plate homography (the
detector's corners) maps the food mask into mm^2 in the plate's own plane.

**Grape series (45 / 90 / 180 g):** scored against the existing portion-sensitivity
pre-registration, independently of all of the above.

---

## E. HOUSEKEEPING RULE — for HANDOFF's class list, 12 Sep 2026

Four stray root artifacts in one night: `mask_overlays/` committed by accident, the
scratch harnesses, `depthcheck.txt`, and `depthsurvey.txt` -- the last one reporting
"9 of 16 measurable" for four days after the code had already refused its method.

**The rule:**

- **Generated output -> `scratch/`**, gitignored, never the repo root. Transcripts,
  aborted runs, redirected logs, applied patches.
- **A result worth keeping ->** its value goes in a notes file, not a loose txt.
- **Superseded evidence -> `docs/evidence/<date>-<name>-INVALID.txt`,** with a header
  saying what produced it and why it is wrong. Not deleted: the record of what was
  believed is evidence too.

**Applied tonight:**

- `depthsurvey.txt` (untracked, gitignored -- so a plain move, no history to carry) ->
  `docs/evidence/2026-09-12-depthsurvey-INVALID.txt`, original bytes preserved under a
  header. It is the survey food_seg.py:377 already cites as wrong: box-ellipse tilts,
  the 41.4 deg fingerprint.
- `depthcheck.txt` deleted. Its one result (20.50 mm against 20.00 mm) is in section D.
  `scripts/depth_probe.py` now writes transcripts to `scratch/`.
- `.gitignore`: `scratch/` added, with the rule written above it; the old transcript
  names stay ignored.
- `dev.bat` `:api`: removed the stale advice to run `dev api > survey-api-log.txt`. The
  same block already tees every run into `docs/evidence/api-*.txt`.

**Root sweep, once:**

| Item | What it was | Done |
|---|---|---|
| `survey-api-log.txt` | raw `dev api` log, 12 Sep; its four request ids and content are the committed `docs/evidence/2026-09-12-photo35-box-intermittency.txt` | -> `scratch/` |
| `survey-bench-3run.txt` | aborted bench run, 11 Sep: connection refused, then interrupted after 4 photos; no result | -> `scratch/` |
| `survey-tail.txt` | `benchall --only` subset, 3 photos x 3 runs, 11 Sep, 61.2% per item; before the density-lookup and USDA fixes, superseded by the clean paired run | -> `scratch/` |
| `Claude outputs/task1-plate-priority.patch` | a session delivery, already applied (its text is food_seg.py:135) | -> `scratch/`; the empty folder removed |
| `mask_overlays/`, `mask_dumps/` | generated, gitignored, and WRITTEN TO BY CODE (`_mask_cache`, `NUTRIAI_MASK_DUMP`) | left; moving them is a code change |
| `scripts/rename_to_neutriai.py` | tracked one-shot nutriai -> neutriai rename, from the baseline commit, never applied; tied to the launch step that fixes bundle IDs | left: tool, not artifact. Unexplained in the docs |
| `.claude/` | session settings | left |

**Still pointing at the root, and not mine to edit:** HANDOFF.md:497 and :561 still tell
the reader to run `dev api > survey-api-log.txt 2>&1`. The other session should drop
them when it folds this in.

**Extends the class list to DOCUMENTS (Gil).** food_seg.py:377 had already cited
depthsurvey.txt as wrong, and the file sat in the root for four days still stating "9 of
16 measurable": a correct note in the code and a stale artifact on disk, never compared.
The standing check covers documents as well as code paths: **any file stating a measured
result carries what produced it and when, and anything the code contradicts is stamped
INVALID or moved.**

---

## F. Depth — licence rule, test scheduling, angles, format check. 12 Sep 2026

### Hard rule: every depth quality number is measured on Small

This product takes subscriptions, so Depth Anything V2 **Small** (Apache-2.0) is the only
usable variant. Base, Large and Giant are CC-BY-NC-4.0. A figure obtained on Large would be
unlicensable and would set an expectation the shipping model cannot meet: it is not
recorded as a result.

**Enforced in code:**

- `DEPTH_MODEL_SIZE` is a required setting; anything but "Small" (unset included) switches
  depth off (`depth_hosted.from_settings`).
- On the replicate dialect `model_size: "Small"` is sent on every prediction, because
  `chenxwh/depth-anything-v2` defaults to "Large".
- A different `model_size` in `DEPTH_MODEL_INPUT` switches depth off.
- Tests in test_depth_hosted.py pin each case.

### Output format, from one paid Small prediction (legacy photo 13) — FORMAT ONLY

- The prediction recorded `model_size: "Small"`. Outputs: `grey_depth` and `color_depth`.
- `grey_depth`: **8-bit greyscale PNG** (mode L), 768x1024 -- the 1024-edge upload size --
  256 levels. `decode_depth` accepts it and resamples to 1568x1176; the ~235k distinct
  values after resampling are interpolation, not information.
- `color_depth`: refused as colour-mapped (channel spread 0.47), as designed.
- **`DEPTH_OUTPUT_FIELD=grey_depth` is required.** Unset, the answer is dropped unread and
  billed; set to `color_depth`, it is refused.
- **Timing:** predict time 0.64 s, but total 29.2 s (queue and cold start) -- past the
  default `DEPTH_TIMEOUT_S=25`, which would have timed out this call and measured nothing.
  Recorded; the default was not changed.
- No quality claim is made from one photograph. The 8-bit depth resolution is a known limit
  whose effect on height is unmeasured.

### Tomorrow's depth test is NOT blocked on scale learning

Gil's calibration plates are tape-measured and the sheet records them, so the 60 deg
frames supply `plate_diameter_mm` directly. Nobody should defer the test waiting on the
scale-learning fix. The prerequisite in section D stands for anything that would SHIP.

### Angles: top-down, ~30, ~60, level with the tabletop

- **Height discriminator k:** 30 vs 60 separates a constant under-report from projected
  height by sin 30 / sin 60 = 0.58 -- wider than 30 vs 45 (0.71). Satisfied.
- **Profile test m:** uses the top-down footprint and the ruler height. Unaffected.
- **Depth at 60:** amplification 1/tan^2(60) + 2 = 2.3x. It is inside the refusal limit
  (`MAX_TILT_DEG = 65`) by only 5 deg, so a handheld "60" that measures 66 is refused. A
  tall pile at 60 also hides the far rim, and more than 35% of the rim annulus covered is
  refused.
- **The card at 30 and 60:** 60 is far past the card detector's aspect cliff (32-35 deg),
  and 30 sits at it. Depth does not use the card, but **the harness must take the 30 and 60
  frames' tilt from the rim ellipse, not the card.**
- **The level frame** is tilt 90 deg: outside depth, ruler only.
- **What I would rather have: ~55 instead of 60.** Amplification 2.5x, 10 deg of margin
  under the refusal limit, less far-rim occlusion, and k separation sin 30 / sin 55 = 0.61,
  still wide. Accept any frame whose rim ellipse measures 45-62 deg. Keep 30 for k; as a
  depth reading it carries 5x amplification.

### The fold-in protocol, and one correction to it

**Protocol (Gil):** when the USDA worktree lands, ONE session takes ownership of
HANDOFF.md, folds in both notes files, and DELETES each file as it is absorbed. A notes
file still in docs/ means a merge did not finish. The same session drops HANDOFF.md:497
and :561 and adds the documents extension above to the class list.

**Correction: fold the WHOLE of MEASURED-HEIGHT-NOTES.md, not only section E, before
deleting it.** It also holds:

- A: the scale-learning live blocker, ranked above the blend ceiling;
- B: the axis-convention root fix and its test;
- C: the verdict that the evidence disabling `USE_MEASURED_HEIGHT` does not survive;
- D: the dependency-ordered plan, the depth assessment, the intake format and the ruler
  read-out;
- F: this section.

Deleting the file after absorbing E alone would lose all five.

### Housekeeping item 4: already done

Commit bb6d39e. `depthcheck.txt` was deleted and the self-check writes to `scratch/`;
`depthsurvey.txt` is stamped INVALID at `docs/evidence/2026-09-12-depthsurvey-INVALID.txt`,
kept rather than deleted, per Gil's instruction that the record is the value.

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

---

## G. USDA POST session (worktree adoring-bun-cb0df3), 12-13 Sep — what nothing else records

Written at the end of the session that built section D.1. Fold into HANDOFF with the rest.

### G.0 State at session end — READ FIRST

- **The fix is BUILT, VERIFIED, and UNCOMMITTED** in worktree
  `.claude/worktrees/adoring-bun-cb0df3` (branch `claude/adoring-bun-cb0df3`,
  fast-forwarded to master 209a84e at session end so this notes commit sits on top of
  the existing file). Uncommitted there:
  - `backend/app/services/nutrition/providers.py`: `usda()` POSTs a JSON body.
  - `backend/tests/test_usda_post.py` + `backend/tests/fixtures/usda_bench_fdc.json`
    (290 KB; recorded foods only. Checked: no key; the one URL is the public endpoint
    named in its `_about` string).
  - `docs/HANDOFF.md`: a new section "The bleeding, stopped: USDA search is a POST",
    inserted after "the 400 is the bleeding, provenance is the cure." **If that worktree
    is discarded, that section is lost.** G.1 repeats its numbers so they survive.
- No commit was made by this session other than this notes commit.

### G.1 The numbers, duplicated from the uncommitted HANDOFF edit

- **fdcId pin:** 43 names (40 recorded bench lookup names + 3 parenthesised). GET retried
  until 200: 62 attempts, 19 were the nginx 400. POST selected a different fdcId for
  **0 of 43**. The top-10 candidate ids were identical, in order, for all 43.
- **Live rate, 101 searches** (43 names x2 + the 3 parenthesised x5; 21 parenthesised),
  GET control interleaved: **POST 0/101 non-200; GET 43/101, 9/21 on parenthesised.**
- **Steady-state bypass after the fix:** `resolver.resolve` on the 40 names -> **0 of 40**
  carry an `ai_estimate` density; all 40 `usda`, density null, table decides.
- Suite: 916 passed, 2 failed (`test_wiring` bench-photo tests, photos/ absent from the
  worktree); both pass with photos/ junctioned in (49/49 with test_usda_post).

### G.2 Shared state this session CHANGED — attribute it before comparing any bench

- **`food_facts` was mutated by the step-4 run.** `resolve` overwrote **13**
  `ai_estimate` rows with USDA rows (density -> null): sliced zucchini, grilled chicken
  with sauce, braised beef, brussels sprouts, roasted potatoes, boiled green beans, rice,
  shredded beef with potatoes, stew, roasted potato, baked pepperoni pizza slice, fried
  tortilla chips, fried mexican rice.
  - **The uncommitted HANDOFF section says "12 of the 40". It is 13** -- miscounted at
    the time; correct it when committing.
  - Before: 116 rows = 83 `ai_estimate` / 33 `usda`; 75 ai rows with a density (HANDOFF).
  - At session end: **70 `ai_estimate` / 46 `usda`; 62 ai rows with a density.** Both
    differences are exactly 13, so nothing else touched the table.
  - **So any bench run from 13 Sep differs from the 12 Sep clean re-run in density source
    for those foods** -- including both of the re-run's "2 of 27" bypass items. A gram
    change on them is the cache, not a code change.
  - HANDOFF's "75 of the 116" and "~71 other ai_estimate rows" are now 62.
- **USDA quota:** roughly 320 requests spent (probe 105, rate run 202, resolve ~12;
  estimated from the scripts, not read off the counter). ~3,500 remained before.
### G.2a EVIDENCE: `docs/evidence/2026-09-12-food_facts-before-resolve.json`

**The only record of `food_facts` before the step-4 resolve run, and the only artifact of
the night that cannot be regenerated.** Last night's clean paired numbers (calibrated
35.9% mean / 20.6% median, subscriber 36.8% / 26.8%) were measured against the table
BEFORE this run. Any later comparison must diff against this file, or it reads a data
change as a code change.

- **What produced it:** a read-only select of all 116 rows via `app.db.service()`
  (scratch script `ff_dump.py`), columns `canonical_key, display_name, source,
  source_id, density_g_ml, raw, hits`. Committed byte-identical to the scratch file,
  sha256 `8a397d01b7908864e21f1fd4033a1dfe810ba6c3a9bc262714dd7618a4ee565a`, 30,633 bytes.
- **When, relative to the resolve run:** file written **21:42:06 -0700, 12 Sep**. The
  resolve run (`bypass.py`) was written 21:48:28 and finished 21:48:48, so the snapshot
  is **~6.5 minutes before** it. In between ran only the GET/POST probe (21:42-21:44) and
  the rate run (21:47-21:48), which call USDA and write nothing. The resolve script's own
  pre-run read agreed with the snapshot on all 40 names.
- **When, relative to the clean paired run:** 8f049a5 recorded that run at 20:20, so the
  snapshot is ~82 minutes after it. Nothing is known to have written `food_facts` in
  between, but that is not proven.
- **What it does NOT contain:** no macro columns (kcal, protein, carbs, fat, fiber, sugar,
  sodium), no `id`, `cuisine` or `serving_hints`. It restores the gram inputs (source and
  explicit density), not the macros of the 13 rows.
- **Counts either side:**

      before (snapshot)   116 rows   83 ai_estimate / 33 usda   75 ai_estimate with a density
      after  (13 Sep)     116 rows   70 ai_estimate / 46 usda   62 ai_estimate with a density

  A diff of the snapshot against the live table after the run finds **exactly 13 rows**
  changed on (source, density), none added or removed. `hits` also rose on cached rows
  the run read, and was not compared.
- **The 13 rows** (canonical_key: before -> after; after is USDA fdcId and description):

      sliced zucchini             ai_estimate 0.95 -> usda null  2710104 zucchini, pickled
      grilled chicken sauce       ai_estimate 1.05 -> usda null  2705945 chicken, ns as to part, grilled with sauce, skin eaten
      braised beef                ai_estimate 1.05 -> usda null  168626  beef, variety meats and by-products, liver, cooked, braised
      brussels sprouts            ai_estimate 0.85 -> usda null  2709772 brussels sprouts, raw
      roasted potatoes            ai_estimate 0.75 -> usda null  170031  potatoes, roasted, salt added in processing, frozen, unprepared
      boiled green beans          ai_estimate 0.95 -> usda null  169321  beans, snap, green, cooked, boiled, drained, with salt
      rice                        ai_estimate 0.95 -> usda null  2709078 dirty rice
      shredded beef potatoes      ai_estimate 0.85 -> usda null  2706503 beef stew with potatoes, puerto rican style
      stew                        ai_estimate 1.02 -> usda null  2706678 stew, chicken
      roasted potato              ai_estimate 0.75 -> usda null  2709402 potato, roasted, nfs
      baked pepperoni pizza slice ai_estimate 0.85 -> usda null  2708642 pizza with pepperoni, stuffed crust
      fried tortilla chips        ai_estimate 0.25 -> usda null  2708204 tortilla chips, flavored
      fried mexican rice          ai_estimate 0.85 -> usda null  2708951 rice, fried, meatless

  Each lost its explicit LLM density, so on a scan each now takes the density table
  instead -- a gram change with no code change.

### G.3 Dead ends — do not re-walk

- **There is no vision-response cache in app code.** Grepping app/ for cache / sha256 /
  image_hash finds only the segmenter's mask cache. HANDOFF's "same cached vision
  response" for caesar 350 vs 98 g lives in whatever harness ran that sweep; not found.
- **The 12 Sep clean re-run and the outage sweep are not in Supabase.** `meals` since
  11 Sep: 73 meals / 127 items, bench scans only at 11 Sep 02:13-03:03 UTC and 12 Sep
  06:48-16:24 UTC. The re-run's 27 item names cannot be re-derived; "2 of 27" and
  "4 of 28" cannot be recomputed item by item. The 40 names used came from those
  persisted scans plus `food_facts` keys.
- `meal_items` stores `name`, `food_fact_id`, `estimation_method` -- **no preparation**;
  the name is already prep-folded ("baked bread roll"). `food_facts` has **no
  `updated_at`** (select fails 42703). `canonical()` drops stopwords, so "cooked greens"
  is keyed "greens".
- **Running scripts from the worktree:** no `backend/.env`, no `.venv` there, and
  `Settings(env_file=".env")` is cwd-relative. Recipe: cwd = main `backend/`, python =
  main `backend/.venv/Scripts/python.exe`, `sys.path.insert(0, <worktree>/backend)`.
  Pytest: from `<worktree>/backend`, `../../../../backend/.venv/Scripts/python.exe -m
  pytest -q`. The 2 photo failures are that environment, nothing else.
- **The main branch is `master`.** `git log main` fails.
- **A few GETs prove nothing about the 400.** All 3 parenthesised names got 200 on their
  first GET in the probe; the rate run then failed 9 of 21. Measure in 20+ alternating.
- The `table` / `used` densities printed in step 4 were computed with `group=None`. For
  names with no dish key (baby carrots, the zucchini names, steamed bun, cooked greens --
  0.85 default) the live scan's food group answers instead, so those columns are not
  the scan's density. The bypass count (explicit density present or not) is unaffected.

### G.4a USDA FOLLOW-UP LIST — top two are wrong-FOOD matches, flagged forward, not chased

1. **`rice` resolves to Dirty rice** (fdcId 2709078, live, and now cached as a `usda` row).
2. **`mexican rice` is cached as "mexican pizza"** (a `usda` row, so it short-circuits and
   is never re-fetched).

**Why these rank first (Gil):** they are the wrong FOOD, not a poor variant of the right
one, so they are not safely macros-only.

**What was checked before writing that down, 13 Sep:** on the live scan path, a wrong
USDA match does NOT reach the grams TODAY. `names = [_lookup_name(d) ...]`
(vision.py:977) are what `estimate_grams(name=name, density=fact["density_g_ml"])`
receives (vision.py:1082, :1102), and `density_for(name, density, food_group)`
(portion.py:2067) keys on the LOOKUP name. USDA rows carry `density_g_ml` null, so
"rice" gets the table's rice density and the words "dirty rice" never reach it.

**Where the wrong food DOES travel, and why it can still cost grams:**

- `DetectedItem.name = fact["display_name"]` (vision.py:1226). The wrong food's name is
  what the user sees and what `meal_items.name` stores (bench rows already show
  "grapes, red, seedless, raw" and "cheeseburger, nfs" as item names).
- **Unverified:** any later path that re-reads the stored item name and re-derives grams
  or learns from it -- `PATCH /meals/{id}` (routers/scans.py:277 selects `name, grams,
  estimation_method`), correction learning, portion learning keyed by name -- would feed
  "mexican pizza" to `density_for` (pizza 0.55 against rice 0.67) and to the shape and
  height lookups. Check those first.
- **The provenance fix changes this.** If a provider density is ever allowed to reach
  `density_for`, a wrong-food row carries the wrong food's density straight into the
  grams. Fix these matches before or with that work.
- Both are also wrong calories today (dirty rice has meat; pizza is not rice).

Then, lower: braised beef -> beef liver, steamed bun -> oysters, shredded beef -> canned
corned beef, zucchini names -> pickled, white rice -> beans and white rice.

### G.4 Half-checked — concluded partway, not verified

- **`rice` -> "Dirty rice" still, live** (now G.4a item 1), though `_match_score`'s
  docstring says that was fixed. By the formula a "Rice, white, ..." candidate scores 5.0 against Dirty rice's
  4.5, so the likely cause is that NO plain "Rice, ..." entry is in USDA's top 10 for the
  bare word with these dataTypes -- a ranking-depth problem, not a scoring one.
  **Inferred, not checked:** one POST with pageSize 50 settles it. Changing pageSize
  moves bench foods; pin it the same way as G.1.
- **"mexican rice" is cached as "mexican pizza"** (`usda` row, so it short-circuits and
  never re-fetches). Macros only; not in HANDOFF's poor-match list.
- **23514 `food_facts_source_check`:** cause identified (resolver stores
  `source='reference_table'`; migration 0003 allows usda/edamam/nutritionix/ai_estimate/
  user), recorded in the uncommitted HANDOFF edit, NOT reproduced live this session.
- The fix makes poor USDA matches serve MORE foods (the 13 flipped rows now take USDA
  macros: braised beef -> beef liver, steamed bun -> oysters, rice -> dirty rice).
  Grams unaffected (USDA rows carry no density); calories are.

### G.5 Scratch files — session temp, lost when the session ends

In `%TEMP%/claude/...adoring-bun-cb0df3/534853e3-.../scratchpad/`:

| File | What | Worth keeping? |
|---|---|---|
| `food_facts.json` | all 116 rows BEFORE the step-4 mutation | **COMMITTED** as `docs/evidence/2026-09-12-food_facts-before-resolve.json` (G.2a) |
| `meal_items.json` | 127 bench items since 11 Sep, joined to fact source/density | re-derivable from Supabase |
| `probe_fdc.json` / `probe_fdc.py` | GET vs POST per name, full candidate lists | superseded by the fixture |
| `rate.py` | 101-search POST/GET rate run | recipe in G.1 is enough |
| `bypass.py` / `bypass.json` | step-4 resolve per name | numbers in G.1/G.2 |
| `ff_dump.py`, `meals_dump.py` | read-only Supabase dumps | trivial |

### G.6 Commits of 12 Sep, in order (author time, -0700 unless noted)

    32f734c  (import)  Pin providers offline; fix structlog event= collision in iap; plate fence follows located plate
    1be5b28  02:12  Emit the masks and the height branch instead of reconstructing them
    e3a35b2  02:17  One dump per scan, not one per digest
    bbc8f8e  03:02  Say whether the mask dump is armed, before the call is paid for
    53fbe24  03:08  Bring the brief into the repo; record the topology inversion
    02edab3  08:42  The brief was stale by one change, not wrong; photo 36 verified
    477aba5  08:54  Mask-count hypothesis refuted; back out the heights the grams require
    eef3db3  09:29  Draw production's own masks; measure the plate instead of asking for it
    d402ae2  10:12  Always tee the API log; namespace the mask cache; freeze the heights
    78aac50  11:00  sam3 probe: use production's transport instead of a second one
    8902378  11:04  Archive sam3's three outputs for photo 35 before the URLs expire
    68e2a1b  11:15  Archive the sam3 text-only survey outputs
    6e1c038  11:46  Archive the four-concept caesar probe (one prediction)
    1ec4fb3  11:48  Record the sam3 measurements, the prediction miss, and the density trap
    cbe03da  12:16  Correct the density claim; record three lookup bugs, the rule and what is blocked
    dc6e63c  12:51  Density lookup: match whole words, head-final, not the longest substring
    2c6cd9c  12:54  Density table: six valued keys for bench foods that had none
    b5b3fc9  12:55  Record the lookup fix, the density-test verdict, and the card rung audit
    7c9891d  16:20  Record the reference_cv audit, material densities, and the card/rim question
    28a8352  16:29  Pre-register the uncalibrated run; record reference_cv recall by gate
    09e3f0e  17:44  Record the uncalibrated sweep, the card viewing-angle derivation, and both scored pre-registrations
    8cb7c55  19:27  Put the blend ceiling first; record the camera-distance lead and the card parallax result
    ef6e6a8  19:52  Record the both-ends defect class, the yardstick, and the serving-prior test
    8f049a5  20:20  Top entry: measurement stack vs lookup table; clean paired re-run; portion arm pre-registered
    5cd2064  20:51  Launch blocker: grams depend on USDA uptime; amend the portion-arm pre-registration
    755e2e1  20:57  Diagnose the USDA 400 and promote it as a gram-determinism fix
    5837444  21:07  Narrow the USDA 400 to parentheses in the query string; pin the fix on results
    20e1bd9  21:35  Prepare the measured-height experiment; record the tilt defect and a starved feature
    bb4bdf1  21:55  Measured-height notes: scale learning as a live blocker, the convention root, the disabling evidence
    d09c94f  22:11  Plan for 13 Sep: axis fix scope, depth-provider assessment, calibration intake and ruler read-out
    bb6d39e  22:22  Housekeeping: generated output to scratch/, stale depth survey kept as INVALID evidence
    209a84e  22:33  Depth: require the licensed Small model; record format check, angles and fold-in protocol
    (next)          this notes commit -- its own hash cannot be written into itself

The USDA POST fix has no hash yet: it is the uncommitted work in G.0.

### G.7 If a fresh agent hears one thing

Commit the USDA POST fix sitting uncommitted in worktree adoring-bun-cb0df3
(providers.py, test_usda_post.py and its fixture, the HANDOFF section) before touching
anything else -- it is verified, it is D.1, and everything after it assumes it -- and
then remember it stopped the bleeding, not the wound: any USDA miss still hands the
grams an LLM density (62 `ai_estimate` rows carry one), and `food_facts` was rewritten
for 13 foods tonight, so a bench gram that moved on those foods since 12 Sep is the
cache, not your change.
