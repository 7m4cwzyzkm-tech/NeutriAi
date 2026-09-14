# Measured height, the tilt gate, and the plate w/h convention

> **STATE OF THE BRANCH, 13 Sep 2026 -- READ BEFORE ANY MERGE.**
> `bundle-a-card-rung` carries Bundle A pieces 1 and 2 and **MUST NOT BE MERGED TO MASTER.**
> Piece 1 (chroma card detector) scored alone is HARMFUL: subscriber meal energy 37.3% ->
> 43.4% MAE/mean. Piece 2 (rung ordering) moves nothing on the bench. Merging now makes
> master worse. It merges only after piece 3 (floor-height correction) is built and the
> whole bundle scores. Master already carries the bundle's documentation and the replay-rig
> fix (cherry-picked); the three code commits (7e2b559, ac99f3a, e4a34e8) exist only on the
> branch. The branch's HANDOFF is behind master's: merge master INTO the branch before
> building piece 3. Details: section H.

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

### A SECOND, INDEPENDENT CAUSE: the learned width is keyed on the model's vessel NAME (13 Sep 2026)

Found by HANDOFF's cache-key sweep. It compounds the axis convention; fixing the
axis does not touch it.

- **Write:** `scale_learning.record(user_id, vessel=_key(detected_vessel), ...)`
  (vision.py:1748-1752); `refresh` and `calibration.learn_from_correction`
  (calibration.py:132) look the row up by `(user_id, vessel)`. **Read:**
  vision.py:1676-1689 picks the calibration whose `vessel` equals the model's
  name for THIS photograph, and it becomes rung 1.
- **The label is not the plate.** A physical vessel is identified by nothing but
  the word the vision model chose for it, and the model does not choose
  consistently. In the 12 Sep uncalibrated sweep the SAME 229 mm plate was
  `side_plate` on 21-25, 28 and 34 and `dinner_plate` on 18-20, 29, 30, 35 and
  36; photo 18 alone was `side_plate` in 4 of 6 earlier scans. The 222 mm foam
  plate (40, 41) was also `side_plate`.
- **So even with the width computed correctly,** one plate's observations split
  across two keys, and different plates that draw the same word pool into one.
  A scan then reads back whichever pooled number its label points at.
- **What the live table does and does NOT show -- checked, not assumed.**
  `vessel_observations` holds 7 rows, one user, all `dinner_plate`: a tape at
  254 mm and card readings of 158.33 mm x5 and 126.66 mm x1. Joined through
  `scan_id` to `food_scans.image_paths`, **all six card readings are photo 14**
  -- the one taped 254 mm plate -- and they are the axis bias above (0.623 and
  0.499 of 254). **The live rows are ONE plate read wrongly, not three plates
  pooled.** The pooling is established by the model's labels on the bench and
  by the key's construction; it has not yet happened in stored data, because
  only one user and one plate have ever written to it.

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
        "also_on_plate": [{"what": "mayonnaise smear", "on_scale": "yes|no"}],
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

- **`also_on_plate`** is every other thing on the plate -- sauce, dressing, oil, butter,
  garnish, another food -- each with whether it was on the scale when `mass_g` was read.
  `[]` means nothing else was on the plate. Same field as `WEIGHED_WITH` in
  scripts/bench_all.py. Without it a sauce the model detects cannot be scored as part of
  the portion or as phantom food.
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

### TWO RULER MEASUREMENTS FOR THE SAME SESSION — plate floor height and field of view (13 Sep)

Both were being derived from photographs of cards; both are cheaper and exact with a
ruler. They go in the same `session.json`. Every number is still READ FROM A PHOTO where
one can be taken, and the typed value is the cross-check -- the rule above.

**A. Plate heights, per plate and bowl** (replaces the ~28 mm portion.py:1638-1678
assumes for food above the table; do the deep bowl of IMG_4192/4193/4196 too, and give it
a tape diameter).

1. Plate empty, on the table where meals are shot.
2. **Rim height.** Ruler standing vertical ON THE TABLE, zero end down, touching the
   plate's outer edge at its highest point. Camera at rim height, square to the ruler
   (`side_level`). Read the top of the rim. Shoot it; record `rim_height_mm_typed`.
3. **Depth, rim to floor.** Lay a straight edge (a second ruler) across the plate, resting
   on the rim at two opposite points. Stand the first ruler on the plate's inner surface at
   the CENTRE, zero end down, touching the underside of the straight edge. Camera level
   with the straight edge. Read where it crosses. Shoot it; record
   `rim_to_floor_depth_mm_typed`.
4. Floor height above the table = rim height - depth. For a bowl, depth is the fill
   depth available, and floor height is read the same way.
5. If the ruler's zero is not at its end, `zero_offset_mm` applies to both readings.

**B. Field of view -- one flat tape, read at three points.**

1. Main camera only: 1x, not 0.5x or 2x, and Macro Control OFF (at close range the phone
   can switch to the ultra-wide silently). Photo mode, 4:3, no crop, no Portrait or Live
   effects. Confirm afterwards: EXIF lens "6.765mm", digital zoom 1.0.
2. Settings > Camera > Level ON. Shoot straight down; the level crosshair must turn
   yellow (aligned) at the shutter.
3. A tape measure lying flat on the table, straight, running across the WHOLE frame and
   past both edges, through the frame centre. Weigh the ends if it curls.
4. **Lens height.** Stand the ruler on the table beside the phone and read the height of
   the main lens centre (the top-left lens of the three, not the phone's back or edge).
   Record `lens_height_mm_typed`. A second person reads it, or use a stack of books as a
   rest.
5. **Two orientations at the SAME height:** tape along the frame's long side (landscape),
   then along its short side (portrait).
6. **Two heights, both orientations:** about 250 mm and about 400 mm. The ratio of the
   two widths to the two heights removes the lens-position offset (~8 mm) instead of
   carrying it as uncertainty: W is proportional to (D + offset), so two D's solve it.
7. Nothing is read off the phone screen. From each ORIGINAL photo the harness reads the
   tape value at the frame's LEFT edge, CENTRE and RIGHT edge (three clicks, stored in the
   manifest), with the tape's FFT tick period as the pixel-per-mm check.
8. **Squareness check, per photo:** |(centre - left) - (right - centre)| must be within
   1% of the width read. Otherwise the camera was not square: reshoot, do not correct.
9. FOV = 2 x arctan(W / 2D), W = right - left.

**Manifest additions:**

    "plates": [{"plate_id": "P1", ...,
                "rim_height_mm_typed": 0, "rim_to_floor_depth_mm_typed": 0,
                "frames": [{"file": "IMG_0000.jpeg", "role": "rim_height_side_level"},
                           {"file": "IMG_0001.jpeg", "role": "floor_depth_side_level"}]}],
    "fov": [{"file": "IMG_0002.jpeg", "tape_axis": "long", "lens_height_mm_typed": 250,
             "height_to": "main lens centre", "level_crosshair_aligned": true},
            {"file": "IMG_0003.jpeg", "tape_axis": "short", "lens_height_mm_typed": 250, ...},
            {"file": "IMG_0004.jpeg", "tape_axis": "long", "lens_height_mm_typed": 400, ...},
            {"file": "IMG_0005.jpeg", "tape_axis": "short", "lens_height_mm_typed": 400, ...}]

Six photos, a few minutes, and two numbers that three rounds of card photogrammetry could
not settle.

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
  source_id, density_g_ml, raw, hits`. **Line endings normalised, content unchanged:**
  the scratch file was written on Windows with CRLF (sha256 `8a397d01...565a`, 30,633
  bytes); `.gitattributes` (`* text=auto eol=lf`) stores it as LF. The committed blob is
  sha256 `69b80ec17d9aacbe26a062ea1df0556ed8e05ab310b908042d004826f1a3ce8a`, 29,207
  bytes, which equals the scratch file with every CR removed -- checked. A Windows
  checkout may show CRLF again; hash `git show <commit>:<path>`, not the working file.
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

---

## H. 13 Sep 2026 session -- meal-level score, two fixes, Bundle A, and what nothing else records

### H.0 State at session end -- READ FIRST

- **Branch `bundle-a-card-rung` must not merge** (banner at the top of this file). Pieces 1
  (7e2b559) and 2 (ac99f3a, amended e4a34e8) are built and scored; piece 3 waits on Gil.
- **master is clean** (`git status` empty at 406904b). Checked out: master.
- **Bundle B:** phantom-food rule SPECIFIED, not built -- waits on the photo 25 mayonnaise
  answer. Wrong-rows fix not yet specified. Acceptance adopted (HANDOFF, pass/fail).
- **Rule in force (HANDOFF standing check):** an investigation that spawns another is
  parked, not followed. Nothing outside Bundles A and B until both score.

### H.1 Waiting on Gil -- nothing below proceeds without these

1. **The white plate of bench photos 17-36:** outer diameter, rim height, rim-to-floor
   depth (floor height = rim height - depth). Bundle A piece 3 needs the floor height. The
   bench declares 229 mm for these photos, and nothing has re-taped it since.
2. **The crock of 31-33:** inside diameter at the rim, inside depth. Crocks are outside
   Bundle A's correction; the card over-reads them 1.4x-5x in area.
3. **Photo 25: was the mayonnaise in the weighed 136 g?** Decides whether excluding it is a
   phantom fix or breaches the zero-legitimate-exclusion ceiling. The phantom rule is not
   built until this is answered.
4. **The eight-food calibration set with the ruler** (intake: section D; plate heights and
   flat-tape FOV: "TWO RULER MEASUREMENTS"; `also_on_plate` per food). It is also the holdout
   pre-registered for chroma x3 (HANDOFF).
5. **Confirmation that 17-36 really are one plate.** Every rim, over-read and floor-height
   figure pooled across them assumes it.
- Also open, PARKED: which photos carry the measured 12 in, confirmed at GROUP level against
  the surface x card grid (H.2); and the IMG_4197 conflict (EXIF 12 Sep 14:28:14, pasted as
  "measured at 12 in", inside the set stated as not measured).

### H.2 Numbers from today that are not in HANDOFF

- **Preparation cache-key fix (43c9b2d), measured before and after:** all 36 unique bench
  lookup names resolved live through the real resolver with writes blocked. 0 served rows
  changed; 364 replay arm-items identical. Meal score with LIVE served rows, identical before
  and after: CAL energy 45.58% MAE/mean, 72.90% per-meal mean, 35.91% median; UNCAL 41.45 /
  75.53 / 29.12. These differ from the 12 Sep figures (48.4 / 74.7 / 40.9) because
  `food_facts` changed between the runs, not because of code.
- **Three `food_facts` rows restamped resolver_version 3 with unchanged values:** mexican
  rice, refried beans, chicken drumstick, ~15:17-15:20 -0700. No script of this session wrote
  them. Most likely the `dev api` server on :8000, running with `--reload`, picked up the new
  resolver and served a request. No API log was found to confirm it.
- **A live-API hazard that happened:** during the axis fix, `--reload` loaded vision.py with
  `_plate_ellipse(detection, aspect)` before `_run_scan`'s call site was updated. Any scan in
  that window raised. The server reloads whatever branch is checked out.
- **Axis fix (303f6b5), scale learning:** on photo 14 (tape 254 mm) the card-derived width it
  would record goes 158.33 mm (-38%) -> 211.10 mm (-17%); 43 and 44 (landscape) unchanged at
  213 and 242 mm. The remaining -17% is the model's ~0.10 under-read, deliberately not folded in.
- **Grey-plus-chroma union on the bench:** 26/28, zero false positives -- identical to chroma
  alone; grey found no card that chroma missed. Where both fire, the quads agree at IoU 0.96-0.98.
- **HALF-CHECK THAT MATTERS FOR THE HOLDOUT:** on IMG_4190-4198 (green card on the
  blue-and-white tablecloth, daylight) chroma x3 ALONE missed the card on 6 of 7 frames
  (4190, 4192, 4193, 4196, 4197, 4198); grey found all 6 (classification.json, field
  `card_source`). The shipped detector (chroma, then grey) still finds them. The
  pre-registration scores the frozen chroma channel ALONE, so if the calibration session
  resembles these frames, reject criterion 1 can fire on a channel the product never uses
  alone. Why chroma misses there is NOT checked. **Before scoring the holdout, decide and
  write down whether it scores chroma alone (as registered) or the shipped detector.**
- **Nutrition5k probe selection:** `depth_test_ids` (507) chained into 198 built-up plates (a
  later scan within 900 s containing the earlier one's ingredients); one final dish per plate;
  mass >= 40 g and kcal >= 30; 16 at even kcal quantiles; 31 dishes carrying a "deprecated"
  ingredient label excluded. FOV 69.05 deg from the paper's 5.957e-3 cm^2/px at 640x480 and
  35.9 cm. Worst dishes: pasta with cream 78 g vs 545 g weighed; plain rice 222 vs 75; sausage
  136 vs 73.
- **Qualification pass (FOV parked):** on full-resolution originals, only IMG_4118 (23) and
  IMG_4136 (30) passed edges plus two-axis card tilt. The red-card originals IMG_4004, 4021,
  4028 and 4091 read 22, 15, 21 and 7 deg of card tilt. The 640x480 copies (40-42, 45, 46)
  returned 72-74 deg: their corners are too coarse to use, not evidence of a crop.
- **Mechanical surface x card grid (classify.py), card cells:** IMG_4190-4198 session 7;
  tablecloth / green 7 (40-46); tablecloth / red 4 (IMG_4004, IMG_4028 = legacy 14,
  IMG_4091 = 15); paper / red 2 (IMG_4021 = legacy 11); white plate / red 1 (legacy 10,
  MISCLASSIFIED -- its card lies on the cloth); wood / green 18 (17-32, 34, 36). Also
  misclassified, checked by eye: 03 and 05 are on granite, not the tablecloth. These are
  the classifier's errors, not the bench's labels.
- **NIH figure:** "250-345 kcal per meal of hidden fat" is Gil's; its source is not in the
  repo. bench_all.py's own citation is ~30 g fat per meal.

### H.3 Dead ends -- do not re-walk

**The FOV archaeology is PARKED, replaced by one flat-tape photo (section D, "TWO RULER
MEASUREMENTS").** What was tried, in order, and why each failed:

1. **Card + 304.8 mm on IMG_4190-4198.** Void at the root: that set never had a measured
   distance. It read 65.9-69.2 deg against EXIF's 71.6 because the premise was wrong.
2. **Card aspect as tilt.** The threshold edge biased every side by a constant ~9 px, with the
   sign flipping between cloth and white plate (aspect 1.600-1.629 and 1.562; true 1.586).
3. **Rim ellipse as tilt.** The rim's 150-270 deg arc has a 2.5-3.7 grey-level step on EVERY
   frame, the plate-only control included, so the ellipse fitted half a circle. The 5-13 deg
   readings were artefacts; and acos near 1 turns a 1% axis error into 8 deg regardless.
4. **Gradient-peak edges.** Zero points on the card's bottom and left sides on all six cloth
   frames: those sides are a 20-40 px shadow ramp (1.5-3 mm), not a step. Measured with
   averaged profiles and seen on crops.
5. **Vanishing points (route 1).** Never run: the shadow sides bias one line in each pair, and
   4198, the frame with clean edges, is near top-down, where vanishing points go to infinity.
6. **EXIF.** No FocalPlaneXResolution tag, so only the 35 mm equivalent: 71.6 deg if diagonal,
   73.7 deg if horizontal, 3.9% apart, convention unstated. It cannot arbitrate.
7. **Provenance.** Which photos carry the 12 in was stated three ways -- "tablecloth", "red
   card", "green card on tablecloth" -- true descriptions of OVERLAPPING groups, never file
   names. Group-level confirmation against the grid is parked.

- **Kept from it (Gil's correction):** off-centre is not an error. For a level camera over a
  flat plane mm/px is constant; offset matters only multiplied by tilt.
- **Other dead ends:**
  - `git worktree` runs need `backend/.env` copied into the worktree, because Settings reads
    .env relative to the cwd. The copies were removed with the worktrees.
  - The replay rig's control FAILS by design when code changes a recorded arm. Score such runs
    against a reference arm with `--pieces-from`, never against the rig's exit code.
  - **A test gate that greps pytest `--co` output for "error" stops on a healthy suite**:
    test names contain "error". Gate on the summary line only.
  - The chat copy of IMG_4197 was a 583x791 recompression. Identify a photo by its
    arrangement; never measure from a chat copy.

### H.4 Scratch files -- lost when this session ends

Session scratchpad A (`...scratch-2026-09-13-7961eb\112f021e...\scratchpad`):

| File | What | Recommendation |
|---|---|---|
| `n5k/detections/*.json` (16), `n5k/selection.json` | the PAID gpt-4o detections + second_look for the Nutrition5k probe; piece 2's acceptance replays them | **COMMIT** to `docs/evidence/2026-09-13-n5k-probe/` next session: re-buying costs money and draws different detections |
| `n5k_probe.py`, `n5k_probe.json` | the paid probe and its per-dish result | commit with the detections |
| `n5k/rgb/*.png` (16), `n5k/*.csv`, `depth_test_ids.txt` | Nutrition5k data, CC BY 4.0, re-downloadable | leave; the dish ids are in selection.json |
| `measure_resolve.py`, `resolve_*.json`, `score_live.py` | the cache-key before/after measurement | numbers are in H.2; leave |
| `replay_before/after1/after2.json`, `compare_replay.py` | the two fixes' zero-movement proof | numbers are in HANDOFF; leave |
| `usda_truth.py/json`, `served_facts.*`, `sb_macros.py`, `dump.py` | truth-row searches and served-row reads | their values are written into score.py; leave |

Session scratchpad B (`...C--Users-VetaM-Downloads-nutriai-nutriai\112f021e...\scratchpad`):

| File | What | Recommendation |
|---|---|---|
| `n5k_rung_acceptance.py`, `n5k_rung_piece2.json` | piece 2's acceptance harness and result | **COMMIT** with the n5k detections: it is Bundle A's first acceptance and must re-run after piece 3 |
| `score_piece.py`, `chroma_net.py`, `chroma_net.json` | per-piece bench scorer; the detector-swap net effect | **COMMIT** to the meal-replay evidence folder: the whole-bundle score needs score_piece.py |
| `replay_fixed_{base,piece1,head}.json/.log` | the fixed rig's control and piece scores | numbers are in HANDOFF; leave |
| `classify.py`, `classify/classification.json`, `measured-12in-candidates.*` | the surface x card grid for the parked provenance question | commit the JSON only when that item is unparked |
| `qualify.py/.json`, `routes.py`, `edges.py`, `tilt_check.py`, `fov_floor.py` | the FOV archaeology | leave: PARKED; H.3 records why each failed |
| `IMG_4197_for_Gil.png`, `identify_4190_4197.png`, `verify_03_05_10_46.png`, `shadow_crops.png`, `diag_*`, `card_colour_*.png`, `all7.png` | thumbnails and crops | **do not commit**: several show the card with the name legible |
| `card_colour.py/json` | already committed as docs/evidence/2026-09-13-card-chroma | leave |

**Repo:** `git status` is empty. One stale worktree, `.claude/worktrees/adoring-bun-cb0df3`
(branch `claude/adoring-bun-cb0df3` at 5942ed8, already contained in master): **recommend
removing it** (`git worktree remove`, then delete the branch). Not done here, because this
pass changes nothing but the notes.

### H.5 Commits of 13 Sep, in order (author time -0700; B = on bundle-a-card-rung only; CP = cherry-picked onto master)

    0d1ecc8  00:21      Notes: USDA POST session state, the food_facts mutation, dead ends, 12 Sep commit list
    66161ef  12:21      Evidence: food_facts as it was before the step-4 resolve run
    4182d9a  12:21      USDA search: POST the fields in a JSON body; pin the food each bench name selects
    ebd3f9e  12:23      HANDOFF: the USDA POST results, corrected to 13 rows; wrong-food matches head the follow-up
    5942ed8  12:23      Notes: the evidence blob is LF-normalised; record both hashes
    06d17e5  14:06      Score the shipped number: meal macros; commit the zero-cost replay rig
    592005b  14:36      Standing check: cache keys must derive from content; record the sweep
    1db24c6  14:44      Rank the sweep's product defects; calibrate the target to Nutrition5k
    43c9b2d  15:17      Nutrition cache key keeps raw/cooked/fresh
    303f6b5  15:23      Plate ellipse: convert the model's long-side fractions to per-axis once
    532a573  20:51      Record the depth probe, the ruler protocol, the photo-set limits, and the chroma detector experiment
    86ca158  21:19      HANDOFF: two bundles and a parked list; the parking rule; photo provenance corrections; chroma net effect
    7e2b559  21:24  B   Bundle A piece 1: card detector looks for edges in chroma first, grey second
    ac99f3a  21:30  B   Bundle A piece 2: an inferred saved calibration no longer outranks a scale measured in the photo
    a2fc9b7  21:30  B   Replay rig: --pieces-from, so a code change to one recorded arm cannot move the others
    6ae385a  21:33  B   HANDOFF: phantom-food specification and a proposed amendment to Bundle B's acceptance
    dfcddd9  21:36  B   HANDOFF: Bundle A pieces 1 and 2 scored
    e4a34e8  21:52  B   Bundle A piece 2 amended: rank follows provenance and applicability
    b941eef  21:52  B   HANDOFF: the ranking rule in provenance terms; Bundle B acceptance adopted with the exclusion ceiling
    13812eb  21:33  CP  of 6ae385a
    2502349  21:30  CP  of a2fc9b7
    bf8b600  21:36  CP  of dfcddd9
    eb87f92  21:52  CP  of b941eef
    bbdf820  21:59      Bench intake: every row records what its weight includes
    406904b  21:59      HANDOFF: the bench's weights do not say what they include; do not merge Bundle A before piece 3
    (next)              this notes commit -- its own hash cannot be written into itself

### H.6 If a fresh agent hears one thing

Do not merge `bundle-a-card-rung`: piece 1 alone makes subscriber scans worse (37.3% ->
43.4% MAE/mean), and only piece 3 -- the floor-height correction, blocked on one ruler reading
from Gil -- can recover it, so the branch waits and nothing else fills the gap. The product's
real number is meal energy, not item grams: 48.4% MAE/mean calibrated, against a 16.5%
published frontier measured in the same statistic. Its tail is phantom food and wrong
nutrition rows, not geometry, and both bundles have pass/fail acceptance written down before
anything is built. Score every change with the zero-cost replay rig in
`docs/evidence/2026-09-13-meal-replay/`, prediction stated first; when a run contradicts its
prediction, chase it before believing it -- today's two contradictions were both defects in
the instrument, not the product. And when a question starts spawning questions, park it: the
FOV hunt took an afternoon and produced no number, and one photo of a tape measure replaces it.
