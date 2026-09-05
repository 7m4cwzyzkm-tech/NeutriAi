# API reference

Base URL `https://api.neutriai.app/v1`. Every endpoint except `/healthz`,
`/v1/billing/plans` and `/v1/webhooks/stripe` requires:

```
Authorization: Bearer <supabase access token>
```

Interactive docs at `/docs` in non-production environments.

## Error shape

One shape everywhere, so the client parses one thing:

```json
{ "error": { "code": "quota_exceeded",
             "message": "You have used today's free AI scans.",
             "detail": { "used": 3, "quota": 3, "upgrade": true },
             "request_id": "a1b2c3d4e5f6" } }
```

| Status | Code | Meaning |
|---|---|---|
| 401 | `unauthorized` | Missing, malformed or expired token |
| 402 | `subscription_required` | Pro-only route without a live entitlement |
| 403 | `forbidden` | Authenticated but not permitted |
| 404 | `not_found` | No such record, or not yours |
| 422 | `validation_error` | Body failed validation; `detail` lists fields |
| 429 | `quota_exceeded` / `rate_limited` | Daily AI allowance, or 120 req/min |
| 502 | `upstream_error` | A third party failed |

Every response carries `x-request-id` and `server-timing`.

---

## Profile

| Method | Path | Notes |
|---|---|---|
| `GET` | `/me` | Creates the profile lazily on first call |
| `PATCH` | `/me` | Recomputes targets when the body fields are complete |
| `GET` | `/me/targets?recompute=false` | Includes `rationale` — the full working |
| `GET` | `/me/dashboard?day=` | Single round trip for the home screen |
| `GET` | `/me/streaks` | `{kind: {current, best, last_day}}` |
| `GET` | `/me/history?days=30` | Daily summaries, newest first |
| `GET`/`POST`/`DELETE` | `/me/restrictions` | Allergies and dietary rules |
| `GET`/`POST` | `/me/body-metrics` | Logging a weight recomputes every target |

```jsonc
// PATCH /me
{ "weight_kg": 82.5, "height_cm": 180, "sex": "male",
  "birth_date": "1990-04-12", "activity_level": "moderate",
  "goal": "lose", "diet_mode": "high_protein" }

// GET /me/targets
{ "bmr_kcal": 1805, "tdee_kcal": 2798, "target_kcal": 2238,
  "protein_g": 182, "carbs_g": 180, "fat_g": 75,
  "fiber_g": 31, "sugar_g_max": 56, "water_ml": 3400,
  "rationale": { "bmr_method": "mifflin_st_jeor", "activity_multiplier": 1.55,
                 "goal_delta_pct": -20.0, "protein_g_per_kg": 2.2,
                 "fat_pct_of_kcal": 30.2, "calorie_floor_applied": false } }
```

---

## Food scanning and meals

| Method | Path | Notes |
|---|---|---|
| `POST` | `/scans` | Metered. 4–8 s typical. Upload to Storage first |
| `GET` | `/scans/{id}` | Scan plus the meal it produced |
| `GET`/`POST` | `/calibrations` | Reference objects for accurate sizing |
| `GET` | `/meals?day=` | |
| `POST` | `/meals` | Manual entry, or log a recipe by `recipe_id` + `servings` |
| `PATCH` | `/meals/{id}` | User correction — also the accuracy feedback loop |
| `DELETE` | `/meals/{id}` | |
| `GET` | `/assessments?day=` | |
| `GET` | `/foods/search?q=` | Cache-first type-ahead |

```jsonc
// POST /scans
{ "image_paths": ["<uid>/1712345678-a1b2.jpg"],   // 1-4, multiple angles help
  "meal_slot": "dinner",
  "calibration_id": null,
  "plate_diameter_mm": 270 }                      // from ARKit, optional

// 201
{ "scan_id": "…", "meal_id": "…", "status": "complete",
  "items": [
    { "name": "grilled chicken breast", "cuisine": "american",
      "grams": 168.4, "grams_low": 143.1, "grams_high": 193.7,
      "estimation_method": "plate_reference", "confidence": 0.79,
      "bbox": { "x": 0.31, "y": 0.24, "w": 0.30, "h": 0.26 },
      "macros": { "kcal": 277.9, "protein_g": 52.1, "carbs_g": 0,
                  "fat_g": 6.1, "fiber_g": 0, "sugar_g": 0, "sodium_mg": 124 } }
  ],
  "totals": { "kcal": 612.4, "protein_g": 58.2, "carbs_g": 61.0, "fat_g": 14.8 },
  "overall_confidence": 0.76, "confidence_band": "medium",
  "needs_review": false,
  "notes": ["Cross-checked across 2 angles.",
            "Geometry (410 g) and typical serving (180 g) disagreed; blended to 272 g."],
  "latency_ms": 5240,
  "assessment": {
    "severity": "none",
    "headline": "Nicely on track — 890 kcal and 42 g protein to go.",
    "detail": "…",
    "portion_advice": [],
    "macro_corrections": { "protein_g": 42.1, "carbs_g": 88.0, "fat_g": 31.2 },
    "next_meal": { "target_kcal": 890, "emphasis": "protein",
                   "suggestions": ["Greek yoghurt with berries (~250 kcal, 20 g protein)"] } } }
```

`grams_low`/`grams_high` are not decoration — they widen as the geometry gets
worse, and the client shows them. A single fake-precise number would be a lie.

---

## Water and fasting

| Method | Path | Notes |
|---|---|---|
| `GET` | `/water?day=` | Includes `on_pace` against an eating-day curve |
| `POST` | `/water` | Fires the celebration on the log that crosses the goal |
| `DELETE` | `/water/{id}` | |
| `PATCH` | `/water/settings` | Goal, reminder window, cadence |
| `GET` | `/fasts/current` | `null` when none is running |
| `POST` | `/fasts` | `16:8` \| `18:6` \| `20:4` \| `omad` \| `5:2` \| `custom` |
| `POST` | `/fasts/{id}/end` | ≥95% of target counts as complete |
| `GET` | `/fasts` | History |
| `GET`/`PATCH` | `/fasts/settings` | Protocol, window, notifications |

```jsonc
// GET /fasts/current
{ "id": "…", "protocol": "16:8", "status": "active",
  "started_at": "2026-09-03T20:00:00Z", "ends_at": "2026-09-04T12:00:00Z",
  "target_minutes": 960, "elapsed_minutes": 743, "remaining_minutes": 217,
  "pct": 77.4, "phase": "Fat burning",
  "phase_note": "Glycogen is running low and fat oxidation is picking up.",
  "streak": 6 }
```

---

## Fitness

| Method | Path | Notes |
|---|---|---|
| `POST` | `/workouts` | Detects PRs, computes HR zones, completes the plan day |
| `GET` | `/workouts?limit=30` | |
| `DELETE` | `/workouts/{id}` | |
| `GET` | `/personal-records` | Best per exercise and metric |
| `GET` | `/exercises?equipment=&kind=` | Filtered library |
| `POST` | `/equipment/scan` | Metered. Photo → equipment inventory |
| `POST` | `/plans` | **Pro.** 20–40 s. Generates and validates all weeks |
| `GET` | `/plans/current` | |
| `GET` | `/integrations` | Every provider with connection state |
| `GET` | `/integrations/{provider}/connect` | Returns an OAuth URL, or a push instruction |
| `POST` | `/integrations/{provider}/sync` | Manual pull |
| `DELETE` | `/integrations/{provider}` | |
| `POST` | `/health/push` | HealthKit / Health Connect / Garmin ingest |
| `GET` | `/health?days=14` | Collapsed by provider precedence |

```jsonc
// POST /workouts
{ "title": "Push day", "kind": "strength",
  "started_at": "2026-09-04T07:00:00Z", "duration_s": 3300,
  "avg_hr": 128, "max_hr": 162, "perceived_effort": 7,
  "plan_day_id": "…",
  "sets": [
    { "exercise_name": "Bench Press", "exercise_slug": "bench-press",
      "set_index": 1, "reps": 5, "weight_kg": 102.5, "rpe": 8, "rest_s": 180 }
  ] }

// 201 — new_prs is what drives the trophy animation
{ "id": "…", "hr_zones": { "z1": 0, "z2": 825, "z3": 1980, "z4": 495, "z5": 0 },
  "new_prs": [ { "exercise_slug": "bench-press", "metric": "1rm",
                 "value": 119.6, "unit": "kg" } ] }

// POST /equipment/scan  → 201
{ "id": "…",
  "equipment": ["bench", "dumbbell", "pull_up_bar"],
  "detected": [ { "equipment": "dumbbell",
                  "detail": "pair of adjustable dumbbells, roughly 5–25 kg",
                  "quantity": 2, "load_range_kg": [5, 25], "confidence": 0.88 } ],
  "confidence": 0.85, "fallback_to_calisthenics": false }

// POST /health/push — an array of days
[ { "day": "2026-09-03", "provider": "apple_health", "steps": 11482,
    "active_kcal": 612, "resting_kcal": 1740, "resting_hr": 54,
    "sleep_minutes": 447, "vo2max": 47.2 } ]
```

Provider precedence when two devices report the same day:
`apple_health > garmin > fitbit > google_fit > samsung_health > manual`.

---

## Recipes

| Method | Path | Notes |
|---|---|---|
| `POST` | `/recipes` | Parses ingredients and computes macros automatically |
| `GET` | `/recipes?q=&tag=&cuisine=&max_kcal=&mine=&saved=` | |
| `GET`/`DELETE` | `/recipes/{id}` | |
| `POST`/`DELETE` | `/recipes/{id}/save` | |
| `POST` | `/recipes/{id}/adapt` | **Pro.** 15–30 s |
| `GET` | `/recipes/{id}/shopping-list?servings=` | Aisle-grouped |

```jsonc
// POST /recipes/{id}/adapt
{ "target_servings": 2, "max_kcal_per_serving": 600, "min_protein_g": 40,
  "honor_allergies": true, "honor_diet_mode": true,
  "consider_fasting_window": true, "consider_workout_load": true,
  "save_as_fork": true }

// 200
{ "recipe_id": "…",                      // the saved fork
  "title": "High-Protein Chicken Alfredo",
  "adaptation_note": "Swapped cream for blended cottage cheese …",
  "substitutions": [ { "from": "heavy cream", "to": "blended cottage cheese",
                       "reason": "cuts fat, adds 18 g protein per serving",
                       "flavour_impact": "medium" } ],
  "per_serving": { "kcal": 578, "protein_g": 46.2, "carbs_g": 51.0, "fat_g": 19.4 },
  "shopping_list": [ { "name": "cottage cheese", "qty": "300 g", "aisle": "dairy" } ],
  "warnings": [] }
```

Allergen removal is verified after generation: the adapted ingredient list is
scanned for every declared allergen, and any survivor is pushed to the top of
`warnings`. The model is not trusted on this by itself.

---

## Social

| Method | Path |
|---|---|
| `POST` `GET` `DELETE` | `/posts`, `/feed?scope=following\|discover\|mine`, `/posts/{id}` |
| `POST`/`DELETE` | `/posts/{id}/like` |
| `GET`/`POST` | `/posts/{id}/comments` |
| `POST`/`DELETE` | `/users/{handle}/follow` |
| `GET` | `/users/search?q=` |
| `GET`/`POST` | `/notifications`, `/notifications/read` |
| `GET` | `/celebrations`, `/motivation` |
| `POST` | `/celebrations/{id}/seen` |

Posts snapshot the metrics they reference, so editing a meal later never
rewrites what someone already saw in the feed.

---

## Billing

| Method | Path | Auth |
|---|---|---|
| `GET` | `/billing/plans` | Public |
| `GET` | `/billing/subscription` | Bearer |
| `POST` | `/billing/checkout` | Bearer |
| `POST` | `/billing/portal` | Bearer |
| `POST` | `/webhooks/stripe` | Stripe signature |

```jsonc
// POST /billing/checkout
{ "plan": "annual", "promo_code": "LAUNCH20" }
// 201
{ "url": "https://checkout.stripe.com/…", "session_id": "cs_…", "trial_days": 15 }

// GET /billing/subscription
{ "tier": "trial", "is_active": true, "status": "trialing",
  "plan_interval": "year", "amount_cents": 5000, "currency": "usd",
  "trial_end": "2026-09-19T00:00:00Z", "cancel_at_period_end": false,
  "ai_scans_used_today": 4, "ai_scans_quota": 500 }
```

Webhooks handled: `checkout.session.completed`,
`customer.subscription.created|updated|deleted|trial_will_end`,
`invoice.payment_succeeded|failed`. Each event id is claimed in
`stripe_events` before processing, so replays are free. `past_due` still grants
access — a failed renewal should not lock someone out mid-retry.

---

## Ops

| Method | Path |
|---|---|
| `GET` | `/healthz` — liveness |
| `GET` | `/readyz` — verifies Supabase is reachable; 503 if not |
