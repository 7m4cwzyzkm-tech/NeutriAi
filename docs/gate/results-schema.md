# Validation Gate results file — schema

What `backend/scripts/gate_export.py` (`dev gateexport`) writes, and what
Base44's Validation Gate app reads. Produced entirely offline from evidence
already committed to this repo: no model call, no paid API, no live
database, no network. See `docs/gate/staging-plan.md` for the separate
question of a staging backend to run contract checks against.

Sample output: `docs/gate/sample-results.json` (a real export, not
hand-written — `test_committed_sample_matches_the_schema_the_exporter_writes`
in `backend/tests/test_gate_export.py` keeps it from drifting).

## Top level

| field           | type   | meaning |
|-----------------|--------|---------|
| `schema_version`| string | semver of this document. Bump on any breaking field change. |
| `generated_at`  | string | UTC timestamp, `YYYY-MM-DDTHH:MM:SSZ`, when the export ran. |
| `generator`     | string | path to the script that wrote this file. |
| `repo.commit`   | string \| null | full git SHA of the checkout the export ran from (`git rev-parse HEAD`). `null` if git was unavailable. |
| `repo.branch`   | string \| null | branch name at export time (`git rev-parse --abbrev-ref HEAD`). |
| `gates`         | array  | one entry per acceptance gate, see below. |

## Each entry in `gates`

| field                   | type              | meaning |
|-------------------------|-------------------|---------|
| `id`                    | string            | stable identifier, snake_case, never reused for a different gate. |
| `bundle`                | `"A"` \| `"B"`    | which HANDOFF.md bundle the gate belongs to. |
| `description`           | string            | the gate's acceptance rule, quoted from HANDOFF.md. |
| `metric`                | string            | machine name of the underlying number. |
| `arm`                   | string \| null    | `"CAL"` / `"UNCAL"` where the metric is arm-specific, else `null`. |
| `unit`                  | string \| null    | `"percent"`, `"count"`, or `null`. |
| `comparison`            | string \| null    | how Base44 should compare `value` to `threshold`: `"lt"` (pass if value < threshold), `"eq"` (pass if value == threshold). `null` when not measurable. |
| `threshold`             | number \| null    | the fixed acceptance number from HANDOFF.md. |
| `measurable`            | boolean           | `true` if `value` was read from committed evidence; `false` if nothing in the repo produces this number yet. |
| `n`                     | integer \| null   | sample size behind `value`, when applicable. |
| `value`                 | number \| null    | **never fabricated.** `null` whenever `measurable` is `false`. |
| `reason_not_measurable` | string \| null    | required and non-empty whenever `measurable` is `false`; states exactly what is missing. `null` when measurable. |
| `evidence`              | array of `{path, sha256}` | which committed file(s) `value` was read from, and that file's hash at export time, so a stale export is detectable. Empty when `measurable` is `false`. |
| `notes`                 | string \| null    | caveats a reader needs before treating `value` as the answer (e.g. "this is the pre-fix baseline"). |

**Base44 applies the fixed acceptance rule** (`value <comparison> threshold`)
to gates where `measurable` is `true`. A gate with `measurable: false` cannot
be evaluated and should be surfaced as NOT_MEASURABLE, not as a failure — it
means the underlying fix or instrumentation does not exist yet, not that it
was tried and failed.

## The four gates, mapped from HANDOFF.md

Source of the acceptance rules: `docs/HANDOFF.md`, "BUNDLE B — KILL THE
TAIL" (§ ACCEPTANCE, adopted 13 Sep) and "BUNDLE A" piece 2's acceptance.

### 1. `bundle_b_energy_mean_below_55` — MEASURABLE

> Per-meal energy MEAN below 55% (CAL arm, the 25 scored meals).

Read from `docs/evidence/2026-09-13-meal-replay/score.txt`, the `ARM CAL`
section, the `energy, all detections (what the user sees)` line —
score.py's own name for "what the user sees," n=25, mean |e|. Current value:
**74.7%** (fails the 55% threshold).

**This is the pre-fix baseline, not a post-fix measurement.** Neither the
phantom-food classifier nor the wrong-rows fix is built yet (see gates 2 and
3), so this number cannot yet reflect either. It is reported because it is
the only value `score.py` produces for this metric today; re-run
`dev gateexport` after a fix lands and this line's provenance hash will
change, which is the signal the number is fresh.

### 2. `bundle_b_paired_invariant` — NOT MEASURABLE

> Paired invariant: every meal containing no item the fix touches is
> BIT-IDENTICAL in every item's grams and energy, before and after. Any
> difference fails.

**Missing:** a "before" and "after" pair of replay runs to diff. The current
`docs/evidence/2026-09-13-meal-replay/replay.json` is the only run in the
repo — there is no post-fix run to compare it against, because no fix
exists. HANDOFF.md records the phantom-food classifier as "SPECIFICATION,
not built" and the wrong-rows piece as "specification not yet written."

**To make this measurable:** once a fix (phantom classifier or wrong-row
correction) lands, re-run the replay rig with it applied, producing a second
`replay.json`. Diff it item-by-item (grams, kcal) against the current one for
every meal the fix does not touch (i.e. every meal with no item flagged or
re-matched by that fix). The gate passes if that diff is empty.

### 3. `bundle_b_phantom_zero_legitimate_exclusions` — NOT MEASURABLE

> Phantom-food piece only — it is a CLASSIFIER and errs both ways: every
> EXCLUDED item is listed and checked by eye on its photograph, and the
> count of LEGITIMATE items wrongly excluded is reported. Hard ceiling: zero
> legitimate exclusions on the 25 bench meals.

**Missing:** the phantom-food classifier itself. HANDOFF.md is explicit:
"PHANTOM FOOD — SPECIFICATION, not built." Two candidate signals are
specified (off-vessel bounding box, smear-shaped portion with no
`typical_serving_g`) but nothing runs them. There is no excluded-item list
to check by eye.

**To make this measurable:** once the classifier runs on the 25 bench meals,
it must emit the list of items it excluded per photo. A human reviews each
against its photograph and counts how many were legitimate (wrongly
excluded). That count is `value`; the gate passes at exactly 0.

### 4. `bundle_a_piece2_bench_equals_clean` — MEASURABLE

> Acceptance 1 (piece 2), free: the 16 cached Nutrition5k detections,
> bench-calibration arm vs clean arm ... the bench arm must equal the clean
> arm dish for dish.

Read from
`docs/evidence/2026-09-13-n5k-probe-and-bundle-a-scoring/n5k_rung_piece2.json`
— counts rows where `identical` is `true`. Current value: **16/16**
(passes).

**Caveat carried in `notes`:** this result is recorded at commit `a2fc9b7`
on branch `bundle-a-card-rung`, not at this export's own commit — Bundle A
piece 3 has not landed, and HANDOFF.md says this acceptance "needs
re-running after piece 3." A gate reader should treat a stale `evidence`
hash here as expected until piece 3 ships a new `n5k_rung_piece2.json`.

## What was *not* invented

The task brief for this export named a "UBL" and a "six-category TOML
contract schema." Neither term appears anywhere in this repository —
checked across `docs/`, `backend/`, and `mobile/` (`grep -ri` for `ubl` and
`toml` outside dependency lockfiles turns up nothing relevant). This schema
does not attempt to satisfy either; if Base44 or Gil have a definition for
them elsewhere, it needs to be supplied before this schema can be reconciled
with it.
