# Architecture

## System

```
┌──────────────────────────────────────────────────────────────────────┐
│  React Native (Expo 52)                                              │
│  Camera · Dashboard · Fasting · Train · Recipes · Feed · Paywall      │
│  React Query (server state) · Zustand (session, paywall, celebration) │
└───────┬──────────────────────────────────┬───────────────────────────┘
        │ 1. photo upload (direct)          │ 2. all other calls
        │    RLS: path must start {user_id} │    Bearer <supabase jwt>
        ▼                                   ▼
┌──────────────────────┐          ┌─────────────────────────────────────┐
│ Supabase Storage     │          │ FastAPI                             │
│ meal-photos          │◄─────────│ • verifies the JWT locally (no hop) │
│ equipment-photos     │  fetch   │ • RLS-scoped client per request     │
│ recipe-photos        │          │ • entitlement gate on paid routes   │
│ avatars, post-media  │          │ • rate limit, request id, timing    │
└──────────────────────┘          └───┬─────────────────────────────┬───┘
                                      │                             │
        ┌─────────────────────────────┘                             │
        ▼                                                            ▼
┌────────────────────────────────────────┐        ┌──────────────────────────┐
│ AI pipeline                            │        │ Supabase PostgreSQL      │
│                                        │        │ 39 tables, RLS forced    │
│  GPT-4o Vision   → what + where + area │        │ triggers keep daily      │
│         ↓                              │        │ summaries + streaks live │
│  portion.py      → grams + band         │        │ dashboard() = 1 round    │
│         ↓                              │        │ trip for the home screen │
│  resolver.py     → macros (cache first) │        └──────────────────────────┘
│         ↓                              │                     ▲
│  Claude          → sanity, merge, copy  │                     │
└────────────────────────────────────────┘                     │
        │                                                       │
        ▼                                                       │
┌──────────────────┐  ┌──────────────┐  ┌──────────────┐       │
│ USDA             │  │ Stripe       │  │ Wearables    │───────┘
│ Nutritionix      │  │ webhooks →   │  │ Fitbit pull  │
│ Edamam           │  │ entitlements │  │ Google Fit   │
└──────────────────┘  └──────────────┘  │ Garmin push  │
                                        │ Apple/Samsung│
┌─────────────────────────────────────┐ │ phone push   │
│ Worker (APScheduler)                │ └──────────────┘
│ wearable sync 4h · hydration nudges │
│ fasting alerts 15m · rollups · budget│
└─────────────────────────────────────┘
```

## The food scan, step by step

1. **Upload.** The phone writes to `meal-photos/{user_id}/...`. Storage RLS
   requires the first path segment to equal the caller's uid, so no user can
   read another's photos. The API never buffers the image.

2. **Gate.** `consume_ai_scan` checks the entitlement row. Free users get three
   scans a day; the counter resets lazily on first touch of a new day, which
   avoids a nightly job over the whole user table.

3. **Recognition.** GPT-4o Vision receives downscaled images (1280 px max —
   quality plateaus well below phone resolution and tokens scale with pixels)
   and returns detections with **frame-area fractions**, not grams. This is the
   single most important design decision in the app: a vision model can see what
   fraction of a frame something covers; it cannot see mass.

4. **Geometry.** `portion.py` converts area to grams:

   ```
   grams = frame_mm² × area_ratio × height_mm × shape_factor × density / 1000
   ```

   The scale (`frame_mm²`) comes from the best available source, in order:

   | Rung | Source | Typical error | Confidence ceiling |
   |---|---|---|---|
   | 1 | Calibrated reference object or known plate diameter | 10–15% | 0.92 |
   | 2 | ARKit / depth API distance | 15–20% | 0.84 |
   | 3 | Multiple angles reconciled | 15–20% | 0.86 |
   | 4 | Assume a 27 cm plate fills the detected ellipse | 25–35% | 0.70 |
   | 5 | No geometry — the model's serving prior | 40%+ | 0.52 |

   Height and shape come from a food-class prior (a steak is a slab, rice is a
   mound, soup is a shallow disc), density from a lookup table. Where geometry
   and the model's serving prior disagree by more than 2.5×, they are blended
   with a geometric mean and confidence is cut — two weak signals that disagree
   must not produce a confident answer.

5. **Nutrition.** `resolver.py` is cache-first: an exact key hit in
   `food_facts`, then a fuzzy match, then a provider call, then an AI estimate
   marked `source='ai_estimate'`. Everything fetched is written back, so a
   popular food is paid for once across the whole user base.

6. **Reasoning.** Claude gets the detections, the computed grams, the resolved
   macros and the user's context. It merges duplicates, drops non-food, and
   corrects implausible portions using real-world knowledge (a chicken breast is
   120–250 g; 900 g is a bug). It may re-rate confidence downward. It never
   invents nutrition numbers.

7. **Persist and assess.** The meal and its items are written; a database
   trigger recomputes the day's summary; the intake assessment runs; streaks and
   motivation fire as detached tasks so a failure there can never fail the scan.

## Why the split between Claude and GPT-4o

They are used for what each is structurally better at, not as redundancy.
GPT-4o Vision does the cheap, fast, spatial job: enumerate objects and report
their extent. Claude does the expensive, careful job: cross-check against
world knowledge, resolve ambiguity from context, and write text a human will
read. Running both is roughly $0.012 a scan; running Claude alone on the image
would cost more and still need the geometry layer.

## Deterministic core, AI surface

Everything that has a consequence is computed in Python or SQL:

| Decision | Where | Why not the model |
|---|---|---|
| Calorie and macro targets | `macros.py` | Must be reproducible and defensible; a user can see the working |
| Overeating severity | `assessment.py` | An alert is a product behaviour, not a generation |
| Subscription access | `entitlements` table | Money |
| PR detection | `coach.detect_prs` | It's a comparison |
| Plan validity | `coach._validate` | Prescribing equipment someone lacks is a safety issue |
| Message uniqueness | `UNIQUE` index on a body hash | Prompts do not guarantee anything |

The model writes wording, generates programmes and rewrites recipes — and each
of those outputs is validated against the deterministic layer before it ships.

## Database

39 tables in nine migrations. Three things carry most of the weight:

**RLS is forced on every user table.** The mobile client talks to PostgREST
directly for realtime and reads; without forced RLS a single missing `.eq()`
would leak another user's meals. The service role is used only where the server
has already checked ownership itself.

**Summaries are triggers, not queries.** `daily_summaries` is recomputed by
`recompute_daily_summary()` whenever a meal, water log or health day changes.
The home screen reads one row instead of scanning three tables.

**`dashboard()` is one RPC.** Summary, targets, streaks, active fast, today's
meals, pending celebration and the unread count come back in a single round
trip — which is what makes the app feel instant on a cellular connection.

## Failure behaviour

| What fails | What the user sees |
|---|---|
| GPT-4o Vision | "No food recognised" and a manual-entry path. Scan marked `needs_review`, not `failed`. |
| Claude | The geometric estimate, with confidence multiplied by 0.9 and no AI commentary. |
| A nutrition provider | Next provider in the chain; then an AI estimate; then a labelled default. Never a 500. |
| Claude, during plan generation | A deterministic full-body template built from the same validated exercise library. |
| Claude, during motivation | A template from a 40-line bank, filtered against hashes already sent. |
| Stripe webhook handler | 200 to Stripe (so it stops retrying), event marked `failed`, replayable from the dashboard. |
| A wearable token expiring | Automatic refresh; on failure the connection is marked `error` with the reason, and the app shows it. |
