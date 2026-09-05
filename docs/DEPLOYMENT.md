# Deployment

Order matters — each step needs credentials from the one before it.

## 1. Supabase

```bash
supabase login
supabase link --project-ref YOUR_REF
supabase db push                          # migrations 0001 → 0011, in order
psql "$DATABASE_URL" -f supabase/seed.sql # exercise library
```

Then in the dashboard:

- **Authentication → Providers** — enable Email. Turn on "Confirm email" for
  production; leave it off in staging so test accounts are one step.
- **Authentication → URL Configuration** — add `neutriai://` to the redirect
  allow-list, or magic links dead-end in a browser.
- **Storage** — the five buckets are created by migration `0011`. Confirm
  `meal-photos` and `equipment-photos` are **private**.
- **Settings → API** — copy the URL, anon key, service key and JWT secret.

Verify RLS actually bites before you ship anything:

```sql
-- as an authenticated user, must return only your own rows
set request.jwt.claims = '{"sub":"<some-user-uuid>","role":"authenticated"}';
set role authenticated;
select count(*) from meals;               -- only yours
select count(*) from profiles where is_private;  -- only ones you follow
reset role;
```

## 2. Stripe

Create two recurring prices on one product:

| Price | Amount | Interval | Env var |
|---|---|---|---|
| NeutriAI Pro Monthly | $6.99 | month | `STRIPE_PRICE_MONTHLY` |
| NeutriAI Pro Annual | $50.00 | year | `STRIPE_PRICE_ANNUAL` |

Do **not** set the trial on the price — the API sets `trial_period_days` per
checkout session so a returning user cannot re-trial (`has_used_trial()` checks
our own subscription history).

Webhook endpoint → `https://api.neutriai.app/v1/webhooks/stripe`, events:

```
checkout.session.completed
customer.subscription.created
customer.subscription.updated
customer.subscription.deleted
customer.subscription.trial_will_end
invoice.payment_succeeded
invoice.payment_failed
```

Copy the signing secret to `STRIPE_WEBHOOK_SECRET`. Enable the **Customer
Portal** in Settings → Billing, and allow plan switching and cancellation.

Test locally:

```bash
stripe listen --forward-to localhost:8000/v1/webhooks/stripe
stripe trigger checkout.session.completed
# then confirm the entitlement flipped:
#   select tier, is_active from entitlements where user_id = '…';
```

## 3. Nutrition providers

| Provider | Signup | Free tier | Use it for |
|---|---|---|---|
| USDA FoodData Central | `fdc.nal.usda.gov/api-key-signup.html` | 1000 req/hour, free | Whole foods. First in the chain |
| Nutritionix | `developer.nutritionix.com` | ~200/day | Restaurant and branded items |
| Edamam | `developer.edamam.com` | ~100/day | Composite dish names |

The cache makes these limits far less binding than they look — a food is
fetched once for the whole user base. Expect >90% cache hit rate within weeks.

## 4. Wearable OAuth

**Fitbit** (`dev.fitbit.com/apps`): type Server, callback
`https://api.neutriai.app/v1/integrations/callback/fitbit`, scopes
`activity heartrate profile sleep weight nutrition`.

**Google Fit** (Cloud Console): enable the Fitness API, create an OAuth client,
same callback path with `/google_fit`. Google requires a verified consent
screen with a recorded demo before the fitness scopes leave testing mode —
start this early, it takes weeks.

**Garmin** (`developerportal.garmin.com`): request Health API access — it is
approval-gated and OAuth 1.0a. Register the push endpoint at
`/v1/health/push`.

**Apple Health / Samsung Health** need no server credentials. Apple requires
the HealthKit entitlement on the provisioning profile and a review note
explaining the read/write scopes; App Review rejects HealthKit apps that read
data without a visible user benefit, so point them at the dashboard.

## 5. Backend

```bash
cp .env.example backend/.env    # fill it in
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# → TOKEN_ENCRYPTION_KEY (provider tokens are encrypted at rest with this)
docker compose up --build
```

Any container host works — Fly.io, Railway, Render, ECS, Cloud Run. Two
processes: `api` and `worker`. Do not run the worker with more than one replica
unless you move APScheduler to a shared job store; two schedulers means two
hydration reminders.

- Health checks: `/healthz` liveness, `/readyz` readiness.
- Scale on request volume; every route is IO-bound, so 2 uvicorn workers per
  container and more containers beats more workers per container.
- Put a real reverse proxy in front for TLS and pass `--proxy-headers`
  (already set in the Dockerfile).

**Before production**, replace the in-process rate limiter in `main.py` with a
Redis-backed one. As written it is per-container, so N containers means N×120
requests per minute per user:

```python
# app/main.py — swap the _hits dict for:
import redis.asyncio as aioredis
r = aioredis.from_url(settings.redis_url)
count = await r.incr(f"rl:{key}:{int(time.time() // 60)}")
if count == 1:
    await r.expire(f"rl:{key}:{int(time.time() // 60)}", 90)
if count > settings.rate_limit_per_minute:
    return JSONResponse(status_code=429, content={...})
```

## 6. Mobile

Set `expo.extra` in `app.json`:

```json
{ "apiUrl": "https://api.neutriai.app/v1",
  "supabaseUrl": "https://YOUR-PROJECT.supabase.co",
  "supabaseAnonKey": "eyJ...",
  "stripePublishableKey": "pk_live_..." }
```

The anon key is safe to ship — RLS is what protects the data, not key secrecy.
The **service key must never** appear in the mobile bundle.

```bash
cd mobile
npm install
npx expo prebuild                 # native projects, needed for HealthKit
eas build --platform ios --profile production
eas build --platform android --profile production
eas submit -p ios
```

Store-review notes that save a rejection round:

- **HealthKit.** Explain in the review notes exactly which types you read and
  why, and make sure the reviewer can see the data used on the dashboard.
- **External payment.** Stripe Checkout for a digital subscription violates
  App Store rules. Ship StoreKit / Google Play Billing for the mobile purchase
  path and keep Stripe for web. `entitlements.source` already has `apple_iap`
  and `google_iap` values for exactly this; wire the receipt verification to
  the same `apply_subscription`-shaped sync.
- **Camera and photo permission strings** are already in `app.json` and must
  describe the benefit, not the mechanism.

## 7. Smoke test after deploy

```bash
curl -s https://api.neutriai.app/healthz
curl -s https://api.neutriai.app/readyz          # must say database: reachable
curl -s https://api.neutriai.app/v1/billing/plans | jq '.[].amount_cents'  # 699, 5000

TOKEN=<a real supabase access token>
curl -s -H "Authorization: Bearer $TOKEN" https://api.neutriai.app/v1/me
curl -s -H "Authorization: Bearer $TOKEN" https://api.neutriai.app/v1/me/dashboard
```

Then, in the app: sign up → complete onboarding → confirm targets appear with a
rationale → scan a meal → confirm the item bands and confidence render → log
water past the goal and confirm the celebration fires exactly once.

## What to watch in week one

| Signal | Where | Why |
|---|---|---|
| `ai_usage` daily spend | `select sum(cost_usd) from ai_usage where created_at::date = current_date` | The worker warns above 80% of the ceiling |
| Scan `needs_review` rate | `food_scans` | Above ~25% means the framing guidance is not landing |
| Correction magnitude | diff `meals.kcal` before/after a PATCH | This is your real accuracy metric, and your tuning data |
| `stripe_events` where `status='failed'` | table | Each one is a user whose access is wrong |
| `device_connections` where `status='error'` | table | Expired tokens, silently stopping sync |
| `food_facts.hits` distribution | table | Confirms the cache is doing its job |
