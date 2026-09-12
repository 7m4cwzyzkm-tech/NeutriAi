# Where this was left, 11 Sep 2026

> **SUPERSEDED WHERE IT DISAGREES WITH [HANDOFF.md](HANDOFF.md) (12 Sep 2026).**
> Read that first; this remains the better map of WHY each decision was made.
>
> Known stale here, all re-measured on 12 Sep:
>
> - "test suite 801 passing" -- now 825, and the seven failures called
>   environmental are gone locally.
> - "last full bench 49.2% per item" -- the 12 Sep brief reports 48.2%, and
>   photo 35 alone scores 77.1% per item (CI 54.5-99.7%).
> - "86% of the model's geometry lands on a 0.05 grid" -- 100% of n=3 on
>   photo 35.
>
> Confirmed and sharpened rather than superseded: "Heap vs layer ... FALSE of
> fries (-76.8%)" under *Next, in order*. That is the same defect now recorded
> as the topology inversion in HANDOFF.md -- and -76.8% is the
> SEPARATE_PIECES branch, where the 12 Sep run put the same fries on the
> CONNECTED_PILE branch at -56.9%. The classification moves between runs.
>
> The "device bridge strips CRs from dev.bat" claim is NOT in this file. It is
> in HANDOFF.md's refuted list, item 3, already marked false.

Read this first, then the code. Every decision below is in the source with its
evidence; this is the map, not the argument.

## State

    dev replay        ALL THREE CHECKS PASS   (free, run before any paid bench)
    dev dead          clean
    test suite        801 passing
    last full bench   49.2% per item, from 62.5%

Seven suite failures are environmental only where the dev container lacks
`jose` and `fastapi`. They fail identically with every change reverted.

## What is solid

- **Soups** +0.4%, -15.2%, +1.5%, 0 g spread across runs. Bowl mouth x depth,
  solved on three weighed crocks. The 114.3 mm crock and 228.6 mm plate are
  both confirmed against a tape measure.
- **Plate finder** (`food_seg._plate_by_hough`). 30/30 across plain wood,
  patterned cloth and the bench set, every circle drawn over the photograph and
  looked at. Replaced a brightness threshold that scored 0/7 on a patterned
  tablecloth -- including a photo of the empty plate.
- **Topology heights** (`portion.CONNECTED_PILE_HEIGHT_MM` / `SEPARATE_PIECES`).
  Beat the eight-class shape table 58.4% -> 15.0% leave-one-out. Known WRONG for
  anything that heaps -- see below.

## Next, in order

1. **Point-prompted SAM2.** The automatic generator will not segment food on a
   patterned background: on the weighed grapes it returned 15 masks, none of
   them a grape, all tablecloth. Cropping to the plate was tried and REFUTED --
   mask counts fell (14->14, 23->22, 26->17, 31->29, 15->10), so tighter framing
   does not buy attention. The crop lives behind `dev heights --crop`, unused.
   We now have a reliable plate to prompt inside, which we did not before.

2. **Heap vs layer.** `SEPARATE_PIECES_HEIGHT_MM` says pieces cannot stack --
   true of carrot coins, FALSE of fries (-76.8%) and lettuce (+198%). Photos
   40-45 are three weighed spread/heaped pairs, already in both tables, and have
   never been measured because the pipeline fails one rung earlier. Fix 1 first.

3. **Recognition.** Nine detections in the last bench matched no weighed item --
   an invented cauliflower, an invented macaroni salad. The bench excludes them
   from its own average and says so, which means the real figure is worse
   than 49.2%.

## Traps, all of which bit once

- **Look at the masks.** Two footprints agreed 97% and 94% and were both the
  credit card. `dev heights --overlay` paints plate blue and food red.
  The agreement number cannot see this. Only looking can.
- **The blue plate does not exist.** There is one white 9-inch plate. An earlier
  session read its own debug overlay's tint back as a fact and wrote it into
  three files. If a theory explains the data suspiciously well, check whether it
  came from a rendering.
- **Built and never connected.** This defect appeared three times in one day.
  Every new wire needs a test that goes in the FRONT DOOR and fails when the
  wire is cut -- not three unit tests that pass with it severed.
- **Mutate before believing a test.** Several tests passed for the wrong reason:
  a guard fired that was not the one under test, a plate's centroid fell in a
  food hole, a `max()` over a one-element list.
- **A killed mutation run leaves the tree mutated.** One left
  `MAX_ITEM_OVER_PLATE = 5.0` in the source and the next run blamed a good test.
  The harness now restores on signal; check constants if a run is interrupted.

## Open, not forgotten

- `PLATE_MAX_FRAME = 0.60` is calibrated on uncropped frames. A cropped plate is
  67% and trips it. Revisit if the crop path ever returns.
- Photo 40 has timed out at the segmenter twice and has never returned masks.
- The card detector reports 245-260 mm for a 229 mm plate and misses cards that
  are plainly in frame.
- 86% of the model's geometry lands on a 0.05 grid where chance gives 20%. One
  step of that grid is 45% of the food's weight. Everything without a measured
  footprint is sized from it.
