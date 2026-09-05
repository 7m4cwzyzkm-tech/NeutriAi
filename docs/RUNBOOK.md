# NutriAI founder runbook

Ten sections, in the order you asked for them. Each is a checklist you can work
through; nothing here is aspirational — every command was run against this repo.

**Two corrections up front.** Section 7 asked for
`backend/app/services/ai/workout.py` and section 8 for
`backend/app/services/ai/recipes.py`. Neither exists. The real files are
**`coach.py`** and **`recipe_ai.py`**. Everything below uses the real names.

---

# 1. Get NutriAI running locally

`docs/ARCHITECTURE.md` describes a three-tier system: an Expo app, a FastAPI
service plus a separate worker process, and Supabase holding the data. Photos go
phone → Storage directly; the API fetches them itself. That shapes the order
below — nothing works until Supabase exists, and the API is useless until it has
model keys.

### The critical path

- [ ] **1.1** Install: Python 3.11+, Node 20+, `supabase` CLI, Docker (optional)
- [ ] **1.2** Create a Supabase project → note the project ref
- [ ] **1.3** `supabase db push` → 11 migrations, 39 tables
- [ ] **1.4** `psql "$DATABASE_URL" -f supabase/seed.sql` → 48 exercises
- [ ] **1.5** Get an Anthropic key and an OpenAI key (both paid, both instant)
- [ ] **1.6** Get a USDA key — free, instant, at `fdc.nal.usda.gov/api-key-signup.html`
- [ ] **1.7** Generate `TOKEN_ENCRYPTION_KEY` (command in §3)
- [ ] **1.8** Write `backend/.env` (§3 has the annotated file)
- [ ] **1.9** `python -m scripts.verify_supabase` → must exit 0
- [ ] **1.10** `uvicorn app.main:app --reload` → `/docs` lists 60+ endpoints
- [ ] **1.11** `python -m app.workers.scheduler` in a second terminal
- [ ] **1.12** Set `expo.extra` in `mobile/app.json` (§5)
- [ ] **1.13** `npx expo start` → sign up → onboard → scan a meal

### What you can do *right now*, before any credentials

The portion estimator is pure geometry with no dependencies:

```bash
cd backend
python -m scripts.portion_lab            # reference set
python -m scripts.portion_lab --ladder   # confidence degrading across rungs
python -m scripts.portion_lab --all
```

### Deliberately deferred

Stripe, wearable OAuth and IAP are **not** on the critical path. The app runs
without them: free tier gives 3 AI scans a day, and every wearable route
degrades to "not connected". Do §1.1–1.13 first, then come back.

**Start Google Fit early anyway** — the fitness scopes need a verified consent
screen with a recorded demo, and approval takes weeks. It is the only item here
with a multi-week lead time.

---

# 2. Verify your Supabase setup

I wrote you a script for this. It checks everything in this section and exits
non-zero with a list of what to fix:

```bash
cd backend
python -m scripts.verify_supabase
```

### The four keys, and exactly where each goes

| Key | Dashboard location | Goes in | Never |
|---|---|---|---|
| Project URL | Settings → API → Project URL | `backend/.env` **and** `mobile/app.json` | — |
| `anon` public | Settings → API → Project API keys | `backend/.env` **and** `mobile/app.json` | — |
| `service_role` | Settings → API → Project API keys | `backend/.env` **only** | **Never** in the mobile bundle |
| JWT Secret | Settings → API → JWT Settings | `backend/.env` **only** | Never client-side |

The anon key is safe to ship — RLS protects the data, not key secrecy. The
service key bypasses RLS entirely; in the app bundle it is a full database
breach. The verify script decodes your service key and checks
`role == "service_role"`, because pasting the anon key into both slots is the
single most common setup error.

### Confirming migrations ran

`supabase/migrations/` holds 11 files that must apply in filename order.

```bash
supabase migration list          # all 11 marked applied
```

```sql
-- 39 tables expected
select count(*) from information_schema.tables
where table_schema = 'public' and table_type = 'BASE TABLE';

-- the rollup + streak functions from 0010
select proname from pg_proc
where proname in ('recompute_daily_summary','bump_streak','dashboard');

-- seed loaded (0011 does not seed; seed.sql does)
select count(*) from exercises;                              -- 48
select count(*) from exercises where 'none' = any(equipment); -- 20+
```

That last one matters more than it looks: it is the calisthenics fallback. If
it returns 0, every equipment-free user gets an empty training plan.

### Confirming RLS

Migration `0011_rls.sql` enables **and forces** RLS on 39 tables. Enabled alone
is not enough — without `FORCE`, the table owner bypasses it.

```sql
select c.relname, c.relrowsecurity as enabled, c.relforcerowsecurity as forced
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
order by 2, 1;
```

Every user table must show `enabled = t`. `food_facts` and `exercises` are
shared reference data — readable by all authenticated users, writable only by
the service role — so they are intentionally enabled but not forced.

**The test that actually proves it.** Enabled policies that are wrong still
leak. Impersonate two users and confirm isolation:

```sql
-- as user A
set request.jwt.claims = '{"sub":"<UUID-A>","role":"authenticated"}';
set role authenticated;
select count(*) from meals;      -- only A's meals
select count(*) from entitlements; -- 1 row, A's own

-- as user B — must not see A's data
set request.jwt.claims = '{"sub":"<UUID-B>","role":"authenticated"}';
select count(*) from meals;      -- only B's

reset role;
```

The verify script also hits `meals` with the bare anon key over PostgREST. If
that returns rows, stop and fix RLS before writing any other code.

### Confirming storage

Migration `0011` creates five buckets. Visibility is not cosmetic:

| Bucket | Public | Why |
|---|---|---|
| `meal-photos` | **No** | Photos of what someone eats. Private, always |
| `equipment-photos` | **No** | Photos of the inside of someone's home |
| `recipe-photos` | Yes | Meant to be shared |
| `avatars` | Yes | |
| `post-media` | Yes | |

```sql
select id, public from storage.buckets order by id;
```

The `own_folder_rw` policy requires the first path segment of every object to
equal the uploader's uid — which is why `mobile/src/api/supabase.ts` builds
paths as `${uid}/${timestamp}-${random}.jpg`. Change that convention and
uploads start failing with a policy violation.

Round-trip test: sign in on the app, take a scan, then confirm the object
appears under `meal-photos/<your-uid>/` and that a signed URL is required to
fetch it.

### Auth settings people forget

- **Authentication → URL Configuration** → add `nutriai://` to redirects, or
  magic links dead-end in a browser.
- **Authentication → Providers → Email** → turn Confirm Email *off* in dev so
  test accounts are one step; *on* for production.

---

# 3. The `backend/.env` file

Copy `.env.example` — it is already annotated. Here it is with the reasoning.

```bash
cp .env.example backend/.env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste that into TOKEN_ENCRYPTION_KEY
```

```ini
# ---- app ----------------------------------------------------------------
ENV=local                       # local | staging | production
                                # production hides /docs and hard-fails on
                                # missing Supabase config at startup
LOG_LEVEL=INFO
API_PREFIX=/v1                  # every router mounts under this
CORS_ORIGINS=*                  # lock to real origins in production

# ---- Supabase (REQUIRED — nothing runs without these) --------------------
SUPABASE_URL=https://abcdefgh.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOi...        # RLS applies. Safe in the app bundle
SUPABASE_SERVICE_KEY=eyJhbGciOi...     # bypasses RLS. SERVER ONLY
SUPABASE_JWT_SECRET=super-secret-value # verifies tokens locally, no network hop
SUPABASE_JWT_AUDIENCE=authenticated

# ---- AI (REQUIRED for scanning, coaching, recipes) ----------------------
ANTHROPIC_API_KEY=sk-ant-api03-...
OPENAI_API_KEY=sk-proj-...
VISION_MODEL=gpt-4o                  # recognition + frame geometry
REASONING_MODEL=claude-sonnet-4-5    # sanity checks, advice
COACH_MODEL=claude-sonnet-4-5        # programme generation
MOTIVATION_MODEL=claude-haiku-4-5    # short, high-volume, cheap
AI_DAILY_COST_CEILING_USD=50         # worker warns past 80%
AI_TIMEOUT_S=45                      # a phone will not wait longer

# ---- nutrition (at least ONE strongly recommended) ----------------------
# Without any of these every food falls to an AI estimate: it works, but
# accuracy drops and cost rises. USDA is free and takes two minutes.
USDA_API_KEY=abc123...
NUTRITIONIX_APP_ID=                  # best for restaurant + branded items
NUTRITIONIX_APP_KEY=
EDAMAM_APP_ID=                       # general fallback
EDAMAM_APP_KEY=
NUTRITION_PROVIDER_ORDER=usda,nutritionix,edamam   # tried in this order

# ---- Stripe (optional locally; web billing only) ------------------------
STRIPE_SECRET_KEY=sk_test_...        # use test keys locally
STRIPE_WEBHOOK_SECRET=whsec_...      # from `stripe listen`
STRIPE_PRICE_MONTHLY=price_...       # $6.99/month recurring
STRIPE_PRICE_ANNUAL=price_...        # $50.00/year recurring
STRIPE_TRIAL_DAYS=15                 # set here, NOT on the price — see §10
STRIPE_SUCCESS_URL=nutriai://billing/success
STRIPE_CANCEL_URL=nutriai://billing/cancel
STRIPE_PORTAL_RETURN_URL=nutriai://billing/return

# ---- in-app purchase (mobile billing; see §9) ---------------------------
APPLE_BUNDLE_ID=app.nutriai.mobile
APPLE_KEY_ID=                        # App Store Connect → Integrations → IAP
APPLE_ISSUER_ID=
APPLE_PRIVATE_KEY=                   # .p8 contents, one line, \n-escaped
ANDROID_PACKAGE_NAME=app.nutriai.mobile
GOOGLE_PLAY_SERVICE_ACCOUNT=         # service-account JSON, one line

# ---- wearables (all optional) -------------------------------------------
# Apple Health and Samsung Health need NO server credentials — the phone
# reads them locally and POSTs to /v1/health/push.
FITBIT_CLIENT_ID=                    # dev.fitbit.com/apps, type "Server"
FITBIT_CLIENT_SECRET=
GARMIN_CONSUMER_KEY=                 # approval-gated, OAuth 1.0a
GARMIN_CONSUMER_SECRET=
GOOGLE_FIT_CLIENT_ID=                # needs a verified consent screen
GOOGLE_FIT_CLIENT_SECRET=
OAUTH_REDIRECT_BASE=http://localhost:8000/v1/integrations/callback

# ---- infra ---------------------------------------------------------------
REDIS_URL=redis://localhost:6379/0
TOKEN_ENCRYPTION_KEY=<Fernet key>    # encrypts wearable OAuth tokens at rest
FREE_TIER_DAILY_SCANS=3
RATE_LIMIT_PER_MINUTE=120
```

**Minimum viable set**: the four Supabase values, two AI keys, `USDA_API_KEY`,
`TOKEN_ENCRYPTION_KEY`. Everything else can stay blank while you develop.

`TOKEN_ENCRYPTION_KEY` has a dev fallback in `app/security.py` so local runs
work without it — but changing it later makes every stored wearable token
undecryptable, forcing every user to reconnect. Set it once, keep it.

---

# 4. Running the FastAPI backend

### 4.1 Install

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"        # pytest, ruff, mypy
```

### 4.2 Tests before the server

Run these first — they need no credentials and prove the logic is intact:

```bash
pytest                      # 88 tests, all offline
pytest -v tests/test_portion.py
```

If these fail, do not debug the server. Something is wrong with the install.

### 4.3 Start

```bash
uvicorn app.main:app --reload --port 8000
```

`app/main.py` on startup: configures structured logging, checks that
`supabase_url`, `supabase_service_key` and `supabase_jwt_secret` are present —
**warns** in local, **raises** in production — then mounts every router under
`/v1`.

Healthy startup looks like:

```
nutriai_starting   env=local prefix=/v1
Uvicorn running on http://127.0.0.1:8000
```

A `config_incomplete missing=[...]` warning tells you exactly which key is
blank.

### 4.4 Verify the routers

`app/routers/__init__.py` exports `ALL_ROUTERS`, mounted in `main.py`:

| Router | Prefix | Tag |
|---|---|---|
| `profiles` | `/v1/me` | profile |
| `scans` | `/v1` | nutrition |
| `lifestyle` | `/v1` | lifestyle |
| `fitness` | `/v1` | fitness |
| `recipes` | `/v1/recipes` | recipes |
| `social` | `/v1` | social |
| `billing` | `/v1` | billing |

```bash
curl -s localhost:8000/healthz | jq
# {"ok":true,"service":"nutriai-api","version":"1.0.0","env":"local"}

curl -s localhost:8000/readyz | jq
# {"ok":true,"database":"reachable"}   ← proves Supabase connectivity

# every route the app exposes
curl -s localhost:8000/openapi.json | jq -r '.paths | keys[]' | sort

# count them — expect 60+
curl -s localhost:8000/openapi.json | jq '.paths | length'
```

Open `http://localhost:8000/docs` for the interactive UI (hidden when
`ENV=production`).

### 4.5 Confirm Supabase connectivity

`/readyz` is the real check — it runs an actual query against `exercises`:

```bash
curl -s localhost:8000/readyz | jq
```

`{"ok":false,"database":"unreachable", "detail": "..."}` means bad credentials
or the project is paused. A 503 here is your deploy health check failing too.

### 4.6 Authenticated request

Get a token from a signed-in app session, or:

```bash
curl -s -X POST "$SUPABASE_URL/auth/v1/token?grant_type=password" \
  -H "apikey: $SUPABASE_ANON_KEY" -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"yourpassword"}' | jq -r .access_token
```

```bash
TOKEN=<paste>
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/v1/me | jq
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/v1/me/dashboard | jq
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/v1/billing/subscription | jq
```

`GET /v1/me` creates the profile row lazily on first call — so a 200 with a
generated handle is correct on a brand-new account, not a bug.

### 4.7 The background worker

`app/workers/scheduler.py` is a **separate process**. The API does not start it.

```bash
python -m app.workers.scheduler
```

```
worker_started jobs=['wearables','hydration','fasting','inactivity','rollup','budget']
```

| Job | Schedule | Does |
|---|---|---|
| `wearables` | every 4h | Pulls Fitbit / Google Fit / Garmin |
| `hydration` | 12:00, 16:00, 19:00 | Nudges users behind hydration pace |
| `fasting` | every 15 min | Halfway and completion notifications |
| `inactivity` | 17:00 | One nudge to users 3–7 days quiet |
| `rollup` | 01:20 | Recomputes yesterday's summaries |
| `budget` | hourly | Logs AI spend, errors past 80% of ceiling |

Force one immediately rather than waiting:

```bash
python -c "
import asyncio
from app.workers.scheduler import ai_budget_check, rollup_yesterday
asyncio.run(ai_budget_check())
"
```

**Run exactly one worker replica.** APScheduler holds its schedule in memory,
so two replicas send every hydration reminder twice. Scaling it needs a shared
job store.

---

# 5. Running the Expo app

### 5.1 Configuration — exact location

Open `mobile/app.json`. The block is at the **bottom**, `expo.extra`:

```jsonc
{
  "expo": {
    "name": "NutriAI",
    "slug": "nutriai",
    "scheme": "nutriai",
    // ... plugins, ios, android ...
    "extra": {
      "apiUrl": "http://192.168.1.42:8000/v1",        // ← 1
      "supabaseUrl": "https://abcdefgh.supabase.co",  // ← 2
      "supabaseAnonKey": "eyJhbGciOi...",             // ← 3
      "stripePublishableKey": "pk_test_..."           // ← 4
    }
  }
}
```

**`apiUrl` is the one that trips everyone.** `localhost` means *the phone* on a
physical device and *the simulator itself* on iOS. Use your machine's LAN IP:

```bash
ipconfig getifaddr en0        # macOS
# → 192.168.1.42, so apiUrl = http://192.168.1.42:8000/v1
```

The iOS simulator alone can use `localhost`. A physical device never can.

`app/config.py` reads these via `Constants.expoConfig.extra` in
`mobile/src/api/client.ts` and `mobile/src/api/supabase.ts`. Only the **anon**
key belongs here.

### 5.2 Install and start

```bash
cd mobile
npm install
npx expo start
```

Press `i` for the iOS simulator, `a` for Android, or scan the QR code.

For HealthKit you need a native build, not Expo Go:

```bash
npx expo prebuild --clean
npx expo run:ios
```

Expo Go cannot load `react-native-health`. Everything *except* Apple Health
works in Expo Go, so start there and prebuild when you get to wearables.

### 5.3 Verifying onboarding

The flow lives in `src/navigation/index.tsx` — `RootNavigator` gates on the
Supabase session and shows `AuthScreen` when there isn't one.

- [ ] **Sign up** — email + 6-char password. `AuthScreen` calls
      `supabase.auth.signUp`. If Confirm Email is on, check your inbox.
- [ ] **Session persists** — the tab bar replaces the auth screen. Force-quit and
      reopen; you should stay signed in (AsyncStorage).
- [ ] **Home shows the setup prompt** — with no targets yet, `HomeScreen`
      renders "Let's set your targets" rather than an empty dashboard.
- [ ] **Complete the profile** — `PATCH /v1/me` with weight, height, sex and
      birth date. Targets compute the moment all four are present.
- [ ] **Targets appear with their reasoning** — Profile tab shows BMR, the
      activity multiplier, the goal delta and the protein-per-kg figure. This is
      the "show the working" panel; if it renders, the whole macro chain works.
- [ ] **Scan a meal** — Scan tab, allow camera, fit the plate in the circle,
      capture, Analyse. Expect 4–8 s and per-item gram ranges with confidence
      dots.
- [ ] **Log water past the goal** — tap the +250/+500 chips repeatedly. The
      confetti overlay should fire **once**, on the log that crosses the line.
- [ ] **Free-tier gate** — a fourth scan on a free account returns 402 and the
      paywall opens by itself.

If the app hangs at "Loading your day", `apiUrl` is wrong. Check the Metro logs
for the failing request.

---

# 6. Testing the portion estimator

`backend/app/services/ai/portion.py`. This is the core claim of the product, so
it gets the most direct tooling. **No credentials needed** — pure geometry.

### 6.1 Automated

```bash
cd backend
pytest -v tests/test_portion.py     # 17 tests
```

They assert plausible outputs against real serving sizes, that bands contain
their estimate, that confidence degrades without a reference, and that the
physical guards hold.

### 6.2 The bench

```bash
python -m scripts.portion_lab                    # 10-food reference set
python -m scripts.portion_lab --ladder           # all five rungs
python -m scripts.portion_lab --sweep rice       # area sweep
python -m scripts.portion_lab --multi            # angle reconciliation
python -m scripts.portion_lab --constraints      # physical guards
python -m scripts.portion_lab --all
python -m scripts.portion_lab --food "pad thai" --area 0.22 --plate 270
```

### 6.3 Reading rung selection

`--ladder` is the clearest single view. Real output:

```
1. calibrated reference      214.4 g   [176–252]  ±35%   plate_reference  conf 0.74
1b. known plate diameter     214.4 g   [176–252]  ±35%   plate_reference  conf 0.74
2. depth sensor (350 mm)     307.1 g   [226–389]  ±53%   depth_model      conf 0.67
3. multi-angle               214.4 g   [164–265]  ±47%   multi_image      conf 0.69
4. assumed 27 cm plate       214.4 g   [122–307]  ±86%   pixel_area       conf 0.56
5. no geometry at all        180.0 g   [ 52–308]  ±143%  ai_prior         conf 0.42
```

Two things to confirm every time you touch this file:

1. **Confidence falls monotonically** down the ladder: 0.74 → 0.42.
2. **The band widens monotonically**: ±35% → ±143%.

If rung 5 ever reports a band as tight as rung 1, the honesty of the whole
feature is gone. That is the regression to watch for.

`mm2_per_frame()` picks the rung. It returns `(mm², method)` and the method
string is what surfaces in the API and the UI.

### 6.4 Verifying the low/high bands

The band is `_METHOD_BAND[method] × (1 + (1 − confidence))` — so it widens for
*both* a worse rung and a shakier detection. Check:

```bash
python -m scripts.portion_lab --multi
```

```
angles AGREE (0.14 / 0.145 / 0.138)
  view 1: 200.1 g  conf 0.74 ...
  → reconciled  204.2 g  [176–232]  ±27%  multi_image  conf 0.78
angles DISAGREE (0.08 / 0.15 / 0.30)
  → reconciled  208.8 g  [111–307]  ±94%  multi_image  conf 0.53
```

Agreement *raises* confidence and tightens the band; disagreement does the
opposite. Two views that disagree by 4× must never produce a confident answer.

### 6.5 Geometry constraints

```bash
python -m scripts.portion_lab --constraints
```

| Guard | Trigger | Effect |
|---|---|---|
| Plate cap | food area > plate area | Cap to plate ratio, confidence × 0.6 |
| Prior blend | geometry vs prior differ > 2.5× | Geometric mean, confidence × 0.85 |
| Absolute rails | outside 3 g – 1500 g | Clamp, confidence × 0.7 |

The plate cap was a bug the tests found: a food covering 99% of a frame where
the plate covers 55% is physically impossible, and the estimator used to accept
it and return a confident 1414 g.

### 6.6 Real-world calibration

Weigh five meals on a kitchen scale, photograph each, run the numbers:

```bash
python -m scripts.portion_lab --food "jasmine rice" --area 0.14 --plate 270
```

Compare to the scale. A consistent one-directional error on a food class means
its prior is wrong — tune `SHAPE_FACTORS`, `HEIGHT_PRIORS_MM` or `DENSITY_G_ML`
at the top of `portion.py`. Those constants are priors and should be updated by
evidence.

Two are already visibly off in the reference set: **scrambled eggs** reads high
(198 g vs a 100–180 g serving) and **broccoli** low (51 g vs 70–150 g). Those
are exactly the kind of drift the correction loop in `docs/OPTIMIZATION.md`
exists to find and fix.

---

# 7. Testing the AI workout coach

The file is **`backend/app/services/ai/coach.py`** (not `workout.py`).

### 7.1 Offline

```bash
cd backend
pytest -v tests/test_coach.py
python -m scripts.coach_lab            # validation, fallback, PRs, HR zones
python -m scripts.coach_lab --prs
python -m scripts.coach_lab --library dumbbell,bench,pull_up_bar
```

### 7.2 Feeding equipment photos

Two paths.

**Through the API** (what the app does):

```bash
# 1. upload to Storage under your own uid
curl -X POST "$SUPABASE_URL/storage/v1/object/equipment-photos/$UID/gym.jpg" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: image/jpeg" \
  --data-binary @gym.jpg

# 2. scan it
curl -s -X POST localhost:8000/v1/equipment/scan \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"image_paths":["'$UID'/gym.jpg"],"space_note":"garage, low ceiling"}' | jq
```

**Through the bench** (uploads for you):

```bash
python -m scripts.coach_lab --scan gym.jpg --user <your-uuid>
```

### 7.3 Verifying inventory detection

```jsonc
{ "id": "…",
  "equipment": ["bench", "dumbbell", "pull_up_bar"],
  "detected": [
    { "equipment": "dumbbell",
      "detail": "pair of adjustable dumbbells, roughly 5–25 kg",
      "quantity": 2, "load_range_kg": [5, 25], "confidence": 0.88 }
  ],
  "confidence": 0.85,
  "fallback_to_calisthenics": false }
```

Check three things:

- Every `equipment` value is in `VALID_EQUIPMENT` (19 values). The router filters
  unknown strings out, so a hallucinated `"hoverboard"` is silently dropped
  rather than poisoning the plan.
- **Photograph an empty room.** You should get `equipment: ["none"]` and
  `fallback_to_calisthenics: true`. This is a success path, not an error.
- `detail` should describe what is actually visible. Vague details across the
  board mean the photo is too dark or too wide.

### 7.4 Inspecting multi-week plans

```bash
curl -s -X POST localhost:8000/v1/plans \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"goal":"build_muscle","days_per_week":4,"weeks":4,
       "session_minutes":45,"equipment":["dumbbell","bench"],
       "experience":"intermediate","limitations":["bad left knee"]}' | jq
```

This route is **Pro-gated** — a free account gets 402. Grant yourself access:

```sql
update entitlements set tier='comped', is_active=true, ai_scans_quota=500
where user_id = '<your-uuid>';
```

Then verify:

```bash
python -m scripts.coach_lab --plan --equipment dumbbell,bench --user <uuid>
```

- [ ] `days` has `weeks × days_per_week` entries (16 for 4×4)
- [ ] Every `week_index` from 1 to `weeks` is present
- [ ] Every `blocks[].slug` exists in the exercise library — the bench asserts
      `used ⊆ library`
- [ ] No equipment appears that you did not supply
- [ ] `safety_notes` reflects your stated limitations

### 7.5 Progression logic

Compare week 1 to week 4 for the same session:

```bash
curl -s localhost:8000/v1/plans/current -H "Authorization: Bearer $TOKEN" \
  | jq '[.days[] | select(.day_index==1) | {week:.week_index, blocks:[.blocks[]|{name,sets,reps}]}]'
```

Week 4 must differ from week 1 — more sets, more reps, or a harder variation.
Identical weeks means the model copy-pasted, which the prompt forbids and which
you should treat as a generation failure.

`progression.model` should be one of `double_progression`, `linear`,
`rpe_autoregulated` or `density`, and `progression.rule` should be a single
sentence the app can display every week.

### 7.6 Validation — the safety net

`_validate()` runs *after* generation and drops or substitutes anything
unsatisfiable. The bench proves it:

```
submitted: bench-press, back-squat, push-up, plank, invented-exercise
survived:  push-up, plank
  · Removed 'Barbell Bench Press' — no usable substitute.
  · Removed 'Back Squat' — no usable substitute.
  · Removed 'Nonsense Machine Fly' — no usable substitute.
```

Every removal is explained and surfaces in `safety_notes`. This is what makes
"never prescribe equipment you don't have" a guarantee rather than a hope.

### 7.7 PR detection

```bash
curl -s -X POST localhost:8000/v1/workouts \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title":"Push day","kind":"strength","started_at":"2026-09-04T07:00:00Z",
       "duration_s":3300,"avg_hr":128,"max_hr":162,
       "sets":[{"exercise_name":"Bench Press","exercise_slug":"bench-press",
                "set_index":1,"reps":5,"weight_kg":102.5,"rpe":8}]}' | jq '.new_prs'
```

First submission returns a PR. Submit the **same** numbers again — `new_prs`
must be empty. Submit 105 kg × 5 and it should return one again.

Epley, with two corrections you can verify:

```bash
python -m scripts.coach_lab --prs
#  100 kg ×  1 →  100.0 kg   a true single is the weight itself
#  100 kg ×  5 →  116.7 kg
#   60 kg × 30 →   84.0 kg   clamped at 12 reps
```

The one-rep case was a bug: raw Epley inflates a genuine measured single by 3%.

---

# 8. Testing AI recipe personalization

The file is **`backend/app/services/ai/recipe_ai.py`** (not `recipes.py`).

### 8.1 Offline

```bash
cd backend
pytest -v tests/test_recipe_parsing.py    # 25 tests
python -m scripts.recipe_lab              # parsing, density, allergen drill
python -m scripts.recipe_lab --parse "2 1/2 cups jasmine rice"
```

### 8.2 Submitting a recipe

```bash
curl -s -X POST localhost:8000/v1/recipes \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title":"Chicken Alfredo","servings":4,"prep_minutes":10,"cook_minutes":20,
       "difficulty":2,"cuisine":"italian","is_public":true,
       "ingredients":[
         {"raw_text":"400 g chicken breast"},
         {"raw_text":"300 g fettuccine"},
         {"raw_text":"1 cup heavy cream"},
         {"raw_text":"50 g parmesan"},
         {"raw_text":"2 cloves garlic"},
         {"raw_text":"salt to taste"}],
       "steps":[{"n":1,"text":"Boil the pasta."},
                {"n":2,"text":"Sear the chicken."},
                {"n":3,"text":"Combine with cream and parmesan."}]}' | jq
```

Macros are computed at creation — no separate call.

### 8.3 Verifying ingredient parsing

```bash
python -m scripts.recipe_lab
```

```
raw                                  qty  unit       name                grams
2 1/2 cups jasmine rice             2.50  cups       jasmine rice        461.4g
½ onion, diced                      0.50  —          onion, diced         75.0g
3 large eggs                        3.00  —          large eggs          150.0g
2 cloves garlic                     2.00  cloves     garlic                6.0g
salt to taste                          —  —          salt to taste         2.0g
a handful of spinach                1.00  handful    spinach              30.0g
```

Two fixes worth knowing, both found by this bench:

- **`salt to taste` → 2 g, not 100 g.** The old default made it 100 g of salt,
  about 39,000 mg of sodium, which wrecked every recipe carrying the phrase.
- **`a handful of spinach` → 30 g.** The article now carries a quantity of one.

Density matters more than it looks — `1 cup` is 336 g of honey and 59 g of
puffed rice. A converter ignoring density is off by 5× on the calorie-dense
items:

```bash
python -m scripts.recipe_lab | grep -A5 "density"
```

### 8.4 Checking macro calculation

```bash
python -m scripts.recipe_lab --macros     # needs USDA key or a warm cache
```

The bench runs an Atwater cross-check: `P×4 + C×4 + F×9` must land within 15%
of the stated kcal. A large gap means an ingredient resolved to the wrong food.

```bash
curl -s localhost:8000/v1/recipes/<id> -H "Authorization: Bearer $TOKEN" \
  | jq '{per_serving, ingredients: [.ingredients[] | {name, grams, kcal}]}'
```

### 8.5 Allergy-safe substitution

Declare an allergy, then adapt a recipe that contains it:

```bash
curl -s -X POST localhost:8000/v1/me/restrictions \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"kind":"allergy","label":"dairy","severity":"severe"}'

curl -s -X POST localhost:8000/v1/recipes/<id>/adapt \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"target_servings":2,"max_kcal_per_serving":600,"min_protein_g":40,
       "honor_allergies":true,"honor_diet_mode":true,
       "consider_fasting_window":true,"consider_workout_load":true,
       "save_as_fork":true}' | jq
```

Check that cream and parmesan are gone, `substitutions` explains each swap with
a `flavour_impact`, the steps were rewritten to match, and the dish is still
recognisably alfredo — the prompt forbids turning a pasta bake into a salad.

### 8.6 Post-generation validation

This is the most important code path in the repo, and it exists because the
model cannot be trusted on it:

```bash
python -m scripts.recipe_lab --allergy-drill
```

After the model returns, `adapt()` re-scans the ingredients it produced for
every declared allergen. A survivor is pushed to the **top** of `warnings`:

```
⚠ 2 tbsp peanut butter
→ WARNING: 'peanut' still appears — do not cook this without checking.
```

Verify in place: `app/services/ai/recipe_ai.py`, in `adapt()`, the block
starting `if req.honor_allergies:`. Deliberately test it by declaring an
allergy to a *core* ingredient — allergic to chicken, adapt a chicken recipe —
and confirm either a genuine substitution or an explicit warning. Silence there
would be the one failure mode that could actually hurt somebody.

---

# 9. StoreKit and Play Billing

`docs/DEPLOYMENT.md` §6 flags this: Stripe Checkout for a digital subscription
inside a native app violates App Store rule 3.1.1 and Google Play policy. It is
a guaranteed rejection.

**I wrote the module.** `backend/app/services/billing/iap.py`, wired into
`app/routers/billing.py`.

### 9.1 The architecture that keeps this sane

Both providers terminate in one function:

```
StoreKit 2 receipt ──► verify_apple() ──┐
Apple S2S notification ─────────────────┤
                                        ├──► apply_entitlement() ──► entitlements
Play purchase token ──► verify_google()─┤                              (one row)
Play RTDN via Pub/Sub ──────────────────┘
                                        
Stripe webhook ──► apply_subscription() ──────► entitlements
```

`apply_entitlement()` writes the **same row** `stripe_service.apply_subscription()`
writes. Access has one definition regardless of who took the money;
`entitlements.source` records which — the schema already allowed `apple_iap`
and `google_iap`.

### 9.2 New endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/billing/iap/apple` | Verify a StoreKit 2 transaction |
| `POST` | `/v1/billing/iap/google` | Verify a Play purchase token |
| `POST` | `/v1/webhooks/apple` | App Store Server Notifications V2 |
| `POST` | `/v1/webhooks/google` | Play RTDN via Pub/Sub push |

All four are exempt from rate limiting — Apple and Google burst notifications
after an outage.

### 9.3 Apple setup

1. **App Store Connect → your app → Subscriptions.** Create a group and two
   products. The ids must match `PRODUCT_TIERS` in `iap.py`:
   `app.nutriai.pro.monthly` ($6.99) and `app.nutriai.pro.annual` ($50).
2. Add a **15-day free trial** as an Introductory Offer on each.
3. **Users and Access → Integrations → In-App Purchase** → generate a key.
   Save the `.p8` (downloadable once), the Key ID and the Issuer ID.
4. **App Information → App Store Server Notifications** → set the production
   *and* sandbox URLs to `https://api.nutriai.app/v1/webhooks/apple`.

```ini
APPLE_BUNDLE_ID=app.nutriai.mobile
APPLE_KEY_ID=ABC123XYZ
APPLE_ISSUER_ID=57246542-96fe-1a63-e053-0824d011072a
APPLE_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\nMIGT...\n-----END PRIVATE KEY-----
```

### 9.4 Google setup

1. **Play Console → Monetize → Subscriptions.** Same two product ids.
2. **Setup → API access** → link a Google Cloud project, create a service
   account with *View financial data* and *Manage orders and subscriptions*.
   Download the JSON.
3. **Monetization setup → Real-time developer notifications** → create a
   Pub/Sub topic, then a **push** subscription to
   `https://api.nutriai.app/v1/webhooks/google`.

```ini
ANDROID_PACKAGE_NAME=app.nutriai.mobile
GOOGLE_PLAY_SERVICE_ACCOUNT={"type":"service_account","project_id":"..."}
pip install google-auth      # already added to requirements.txt
```

### 9.5 Client side

```ts
// mobile/src/screens/PaywallScreen.tsx — replace startCheckout()
import { Platform } from 'react-native';
import * as IAP from 'expo-in-app-purchases';   // or react-native-iap
import { request } from '../api/client';

const PRODUCTS = ['app.nutriai.pro.monthly', 'app.nutriai.pro.annual'];

async function startPurchase(plan: 'monthly' | 'annual') {
  // Web keeps Stripe. Native must use the platform's billing.
  if (Platform.OS === 'web') return startStripeCheckout(plan);

  await IAP.connectAsync();
  const { results } = await IAP.getProductsAsync(PRODUCTS);
  const product = results.find(p => p.productId.includes(plan))!;

  IAP.setPurchaseListener(async ({ responseCode, results }) => {
    if (responseCode !== IAP.IAPResponseCode.OK) return;
    for (const purchase of results ?? []) {
      if (purchase.acknowledged) continue;

      if (Platform.OS === 'ios') {
        await request('/billing/iap/apple', {
          method: 'POST',
          query: { transaction_id: purchase.orderId, sandbox: __DEV__ },
        });
      } else {
        await request('/billing/iap/google', {
          method: 'POST',
          query: { product_id: purchase.productId,
                   purchase_token: purchase.purchaseToken },
        });
      }
      // Only finish AFTER the server confirms — a crash between the two
      // otherwise leaves a paid user with no entitlement.
      await IAP.finishTransactionAsync(purchase, false);
      qc.invalidateQueries({ queryKey: keys.subscription });
    }
  });

  await IAP.purchaseItemAsync(product.productId);
}
```

Add a **Restore Purchases** button. Apple rejects subscription apps without one.

### 9.6 Details that cause rejections or lost money

- **Acknowledge Play purchases within 3 days** or Google auto-refunds.
  `verify_google()` does this for you.
- **Sandbox vs production.** Apple returns 404 when you query the wrong
  environment. `verify_apple()` retries the other one automatically.
- **Grace and retry keep access.** Apple status 3 (billing retry) and 4 (grace),
  Play `ON_HOLD` and `IN_GRACE_PERIOD` all stay live — matching how Stripe's
  `past_due` is handled. A failed renewal is not a cancellation.
- **Verify Apple's JWS signature in production.** `_decode_jws()` decodes
  without verifying. That is acceptable on the receipt path (we immediately
  re-fetch from Apple), but a *notification* acted on unverified is forgeable.
  Add `app-store-server-library` before launch — this is marked with a `NOTE`
  in the source.
- **Notifications are the source of truth**, exactly like Stripe webhooks. The
  receipt call makes the purchase feel instant; the notification makes it
  correct.

### 9.7 Testing

```bash
# Apple: App Store Connect → Users and Access → Sandbox → add a tester.
# Sign out of the App Store on device, build via TestFlight, buy.
# Sandbox renews fast: 1 month = 5 minutes.

# Google: Play Console → Setup → License testing → add your account.
# Upload to internal testing; test purchases do not charge.

curl -s -X POST "localhost:8000/v1/billing/iap/apple?transaction_id=2000000&sandbox=true" \
  -H "Authorization: Bearer $TOKEN" | jq
```

```sql
select tier, is_active, source, expires_at from entitlements where user_id = '<uuid>';
-- source must read apple_iap or google_iap
```

---

# 10. Deploying end-to-end

### 10.1 Supabase (production project)

Use a **separate project** from dev. Then:

```bash
supabase link --project-ref PROD_REF
supabase db push
psql "$PROD_DATABASE_URL" -f supabase/seed.sql
```

- [ ] Confirm Email **on**
- [ ] Redirect allow-list includes `nutriai://` and your web origin
- [ ] Point-in-time recovery on (Pro plan)
- [ ] Run §2's RLS impersonation test against production
- [ ] `python -m scripts.verify_supabase` against production env → exit 0

### 10.2 Backend hosting

**Railway** — easiest for two processes:

```bash
railway init && railway up
railway add --service worker    # start command: python -m app.workers.scheduler
railway add redis
```

**Render** — a Web Service (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`)
plus a Background Worker from the same repo.

Either way:

- [ ] Every var from §3, with **production** values
- [ ] `ENV=production` — hides `/docs`, hard-fails on missing Supabase config
- [ ] `CORS_ORIGINS` set to real origins, not `*`
- [ ] Health check → `/readyz`
- [ ] API: 2+ replicas. **Worker: exactly 1** (APScheduler is in-memory)
- [ ] Custom domain + TLS
- [ ] Replace the in-process rate limiter with Redis — code in DEPLOYMENT.md §5

### 10.3 Stripe (web billing)

- [ ] Live mode, two prices: $6.99/month, $50/year
- [ ] Trial set **per checkout session**, not on the price — otherwise a user
      who cancels and returns gets a second free trial
- [ ] Webhook → `https://api.nutriai.app/v1/webhooks/stripe`, the 7 events from
      `HANDLED` in `stripe_service.py`
- [ ] Customer Portal enabled, cancellation allowed
- [ ] `stripe trigger checkout.session.completed` → confirm the entitlement flips

### 10.4 OAuth credentials

| Provider | Redirect URI | Lead time |
|---|---|---|
| Fitbit | `https://api.nutriai.app/v1/integrations/callback/fitbit` | Same day |
| Google Fit | `.../callback/google_fit` | **Weeks** — verified consent screen |
| Garmin | Health API + push to `/v1/health/push` | Weeks — approval-gated |
| Apple / Samsung | none | — |

Set `OAUTH_REDIRECT_BASE=https://api.nutriai.app/v1/integrations/callback` and
make sure every provider's registered URI matches **exactly**, trailing slash
included.

### 10.5 EAS builds

```bash
cd mobile
eas login && eas build:configure
# app.json extra → production apiUrl, prod Supabase, live Stripe pk
eas build --platform ios --profile production
eas build --platform android --profile production
```

- [ ] `apiUrl` is the production API, not a LAN IP
- [ ] Supabase URL/anon key are the production project
- [ ] Service key is **not** in the bundle — `grep -r "service_role" mobile/` is empty
- [ ] Bundle id matches `APPLE_BUNDLE_ID` and `ANDROID_PACKAGE_NAME`

### 10.6 Store submission

**iOS**

- [ ] HealthKit entitlement on the provisioning profile
- [ ] Review notes: which health types you read, why, and where the user sees
      the benefit. Reviewers reject HealthKit apps whose data has no visible use
- [ ] StoreKit products **Ready to Submit** (§9)
- [ ] Restore Purchases button present
- [ ] Privacy nutrition labels: Health, Photos, Identifiers
- [ ] Sandbox purchase tested end to end
- [ ] Demo account in review notes, pre-loaded with data

**Android**

- [ ] Health Connect permissions declared with rationale
- [ ] Data safety form filled
- [ ] Play Billing products active
- [ ] Internal testing track before production

### 10.7 Final verification

```bash
curl -s https://api.nutriai.app/healthz | jq
curl -s https://api.nutriai.app/readyz | jq            # database: reachable
curl -s https://api.nutriai.app/v1/billing/plans | jq '.[].amount_cents'  # 699, 5000
curl -s https://api.nutriai.app/docs                    # 404 — correct in prod
```

Then, on a real device from a store build:

- [ ] Sign up → onboard → targets appear with their rationale
- [ ] Scan a meal → gram ranges + confidence render
- [ ] Free tier caps at 3 scans → paywall opens by itself
- [ ] Purchase → entitlement flips within seconds → `source` is `apple_iap`
- [ ] Water past goal → celebration fires **once**
- [ ] Start and end a fast → streak increments
- [ ] Log a workout → PR fires → trophy animation
- [ ] Connect Apple Health → steps appear on the dashboard
- [ ] Post to the feed → visible from a second account

### 10.8 Week one

| Query | Watch for |
|---|---|
| `select sum(cost_usd) from ai_usage where created_at::date = current_date` | Approaching `AI_DAILY_COST_CEILING_USD` |
| `select status, count(*) from food_scans group by 1` | `needs_review` above ~25% means framing guidance isn't landing |
| `select * from stripe_events where status = 'failed'` | Each one is a user whose access is wrong |
| `select * from device_connections where status = 'error'` | Expired tokens, silently stopping sync |
| `select source, count(*) from food_facts group by 1` | Rising `ai_estimate` share = providers failing |

The correction-magnitude query in `docs/OPTIMIZATION.md` is the one that
matters most long-term. It is your real accuracy metric and the training data
for every prior in `portion.py`.
