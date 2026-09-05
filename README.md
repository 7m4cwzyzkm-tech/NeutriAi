# NutriAI

An AI nutrition, fitness and lifestyle platform: photograph a meal and get an
honest calorie and macro estimate; get a training programme built around the
equipment you actually own; track water, fasts, workouts and recipes; share it
with people.

```
mobile/     React Native (Expo 52) app
backend/    FastAPI service + background worker
supabase/   PostgreSQL schema, RLS policies, seed data
docs/       Architecture, API reference, deployment, optimization
```

---

## What is actually built

| Module | State |
|---|---|
| Food recognition (GPT-4o Vision) | Complete — multi-food, cuisine, mixed plates, confidence |
| Pixel-to-gram estimator | Complete — 5-rung geometry ladder, calibration, depth, multi-image reconciliation |
| Nutrition engine | Complete — USDA / Nutritionix / Edamam with cache-first resolution and AI fallback |
| Personalised macros | Complete — Mifflin-St Jeor + Katch-McArdle, goal and diet-mode aware |
| Overeating detection | Complete — deterministic verdict, AI wording, next-meal correction |
| Water tracking | Complete — goal, streaks, pace, reminders, Apple Health write-back |
| Intermittent fasting | Complete — 16:8/18:6/20:4/OMAD/custom, phases, streaks, notifications |
| Workout logger | Complete — sets, reps, load, RPE, PR detection, HR zones |
| AI workout coach | Complete — equipment photo → inventory → validated multi-week programme |
| Calisthenics fallback | Complete — real progression ladder, not a downgrade |
| Wearables | Fitbit fully implemented (OAuth 2 + pull). Google Fit implemented. Garmin + Apple + Samsung normalized through the same adapter contract — see note below |
| Recipes | Complete — posting, ingredient parsing, automatic macros, saves, shopping lists |
| AI recipe personalization | Complete — allergy-safe substitution with post-generation verification |
| Motivation | Complete — database-enforced no-repeat, tone filter, template floor |
| Daily celebration | Complete — animated overlay, idempotent per day |
| Social feed | Complete — follows, posts, likes, comments, Supabase realtime |
| Stripe subscriptions | Complete — 15-day trial, $6.99/mo, $50/yr, promos, portal, idempotent webhooks |

**The one honest caveat.** Apple HealthKit and Samsung Health have no server
API — data can only leave the device if the app reads it and uploads it. That
phone-side read is implemented in `mobile/src/native/health.ts` against
`react-native-health` and `react-native-health-connect`, but it cannot be
verified in CI; it needs a real device. Garmin's OAuth 1.0a request-token dance
also needs live credentials to finish. Fitbit is the fully-worked reference
implementation, and the other providers follow its shape.

---

## Quick start

```bash
# 1. Database
supabase link --project-ref YOUR_REF
supabase db push                 # runs supabase/migrations in order
psql "$DATABASE_URL" -f supabase/seed.sql

# 2. Backend
cp .env.example backend/.env     # then fill it in
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload    # http://localhost:8000/docs

# 3. Worker (separate process)
python -m app.workers.scheduler

# 4. Mobile
cd ../mobile
npm install
# set supabaseUrl / supabaseAnonKey / apiUrl in app.json -> expo.extra
npx expo start
```

Or `docker compose up --build` for the API plus worker plus Redis.

---

## The three ideas worth knowing

**1. Geometry, not vibes.** Asking a vision model "how many grams is this?"
produces confident nonsense. Asking it "what fraction of the frame does this
cover?" produces a number it can actually see. `backend/app/services/ai/portion.py`
turns that fraction into grams through a scale reference, a height prior and a
food density — and reports which rung of the ladder it used, so the confidence
you see is earned rather than decorative.

**2. The model never decides anything that matters.** Overeating severity,
subscription access, PR detection and plan validity are all computed
deterministically. The model writes the wording and the LLM output is checked
against the arithmetic before it reaches a user. If every AI provider went down,
the app would still log meals, still gate billing correctly, and still produce a
training plan.

**3. Uniqueness is enforced, not requested.** "Never repeat a motivational
message" is a `UNIQUE` index on a content hash, plus a retry loop, plus a
template floor — not a line in a prompt hoping for the best.

---

## Tests

```bash
cd backend && pytest          # 85 tests
```

They cover the parts where being wrong is expensive: macro arithmetic that must
sum to its own calorie target, portion estimates that must land inside real
serving sizes, severity bands, the shaming-language filter, ingredient parsing
against messy real-world strings, and the billing rules that decide who gets
access.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system diagram, data flow, why each choice
- [`docs/API.md`](docs/API.md) — every endpoint, with request and response shapes
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — Supabase, Stripe, OAuth, hosting, app stores
- [`docs/OPTIMIZATION.md`](docs/OPTIMIZATION.md) — cost, latency and scale, with numbers
