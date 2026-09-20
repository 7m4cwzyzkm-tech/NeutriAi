# Staging plan for the Validation Gate's contract checks

Document only — nothing here was deployed. Read from `backend/Dockerfile`,
`docker-compose.yml`, `backend/docs/openapi.json` and `.env.example` (the
one at repo root; there is no `backend/.env.example` — see note at the
bottom). This is scoped to what a public HTTPS staging backend needs so
Base44 can run **read-only contract checks** against it — not a full
production deployment plan (see `docs/DEPLOYMENT.md` for that) and not a
plan to run the accuracy bench against it (that needs paid model calls and
is explicitly out of scope here).

## What a staging backend needs

From `backend/Dockerfile` and `docker-compose.yml`:

- The `api` service: built from `backend/Dockerfile` (`python:3.12-slim`,
  installs `backend/requirements.txt`, runs
  `uvicorn app.main:app --workers 2 --proxy-headers` on port 8000). Exposes
  `GET /healthz` as its container `HEALTHCHECK`.
- The `worker` service: same image, runs `python -m app.workers.scheduler`.
  Not needed for contract checks (it does not serve HTTP), but
  `docker-compose.yml` starts it alongside `api`.
- `redis`: both `api` and `worker` depend on it (`REDIS_URL`); the compose
  file runs `redis:7-alpine` with a 256 MB LRU cap. Needed for the process to
  start cleanly, not for any specific contract-check route.
- A Supabase project reachable from wherever staging runs — `readyz` (below)
  actively queries it, and most routes need it for auth and data.
- **A public HTTPS URL and a real TLS certificate.** The gate is described
  as validating "a public HTTPS staging backend," and `docker-compose.yml`
  as committed publishes plain HTTP on `:8000` with no reverse proxy or TLS
  termination in front of it. That piece (a proxy, a platform's HTTPS
  ingress, or similar) is not in this repo and needs adding before anything
  public-facing points at it — this plan does not choose a mechanism, since
  none is specified here.
- `CORS_ORIGINS` set to the gate's own calling origin (or left as `*` for a
  staging environment nothing else depends on) — see `.env.example`.
- `ENV=staging` (not `production`): this relaxes the `lifespan()` startup
  check in `backend/app/main.py` from "raise if Supabase config is missing"
  to "log a warning," and keeps `/docs` and `/redoc` enabled
  (`docs_url=None if settings.is_prod else "/docs"`) so the contract check
  has a live OpenAPI page to compare against, not just the committed
  `backend/docs/openapi.json`.

## Environment variables — names only, per `.env.example`

**Required for the API to boot at all**, from `app/main.py`'s own startup
check (`missing = [supabase_url, supabase_service_key]`; this raises only
when `ENV=production`, but staging needs both anyway since almost every
route reads the database):

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`

**Needed for auth to accept any real token** (`readyz`'s own `auth_mode()`
check wants at least one):

- `SUPABASE_JWT_SECRET` (legacy HS256 path), or a JWKS-based project (no
  separate variable — Supabase serves its own JWKS; `SUPABASE_JWT_AUDIENCE`
  matters either way)
- `SUPABASE_ANON_KEY`

**Needed for the process to start cleanly** (compose-file dependency):

- `REDIS_URL`

**App-level, low-risk to leave at `.env.example` defaults for a contract-check
staging box:**

- `ENV`, `LOG_LEVEL`, `API_PREFIX`, `CORS_ORIGINS`
- `RATE_LIMIT_PER_MINUTE`, `FREE_TIER_DAILY_SCANS`, `PRO_DAILY_SCAN_CEILING`
- `TRUST_PROXY_HEADER` — leave `false` unless a real reverse proxy sits in
  front and is trusted to overwrite `x-forwarded-for` (see the comment at
  `app/main.py:117-120`)

**Deliberately NOT needed for a contract-check staging box** (each names a
paid provider or a real-money path; leaving them unset makes the
corresponding feature no-op rather than live, per `.env.example`'s own
comments):

- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` — vision/coach/motivation calls cost
  money per request; the gate must spend nothing
- `USDA_API_KEY`, `NUTRITIONIX_APP_ID`/`APP_KEY`, `EDAMAM_APP_ID`/`APP_KEY`
- `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_MONTHLY`,
  `STRIPE_PRICE_ANNUAL` — real billing
- `APPLE_KEY_ID`, `APPLE_ISSUER_ID`, `APPLE_PRIVATE_KEY`,
  `GOOGLE_PLAY_SERVICE_ACCOUNT` — real in-app-purchase verification
- `DEPTH_PROVIDER` and the `DEPTH_*` family, `SEGMENTER_PROVIDER` and the
  `SEGMENTER_*` family — third-party model calls, each also billed
- `FITBIT_*`, `GARMIN_*`, `GOOGLE_FIT_*` — wearable OAuth, not exercised by a
  contract check
- `TOKEN_ENCRYPTION_KEY` — only needed once a wearable integration is
  actually connected

If a contract check happens to exercise a route that touches one of these
providers (see the forbidden list below), that is a sign the check is out of
scope for a no-spend gate, not a sign the variable should be set.

## Endpoints safe for read-only contract checks

From `backend/docs/openapi.json`. "Safe" here means: no state-changing side
effect, and no call to a paid third-party model or payment provider.

- `GET /healthz` — no auth required; confirms the process is up
- `GET /readyz` — no auth required; confirms Supabase is reachable and which
  auth mode is configured
- `GET /v1/billing/plans` — no auth required per `app/main.py`'s own
  docstring; lists prices, does not create a checkout session
- `GET /v1/foods/search` — read-only nutrition lookup
- `GET /v1/exercises` — read-only reference data
- `GET /v1/me`, `GET /v1/me/dashboard`, `GET /v1/me/targets`,
  `GET /v1/me/streaks`, `GET /v1/me/restrictions`,
  `GET /v1/me/body-metrics`, `GET /v1/me/history`,
  `GET /v1/me/deletion-preview` — all reads; need a real bearer token from a
  dedicated staging test account, but change nothing
- `GET /v1/meals`, `GET /v1/recipes`, `GET /v1/recipes/{id}`,
  `GET /v1/recipes/shopping-lists/saved`, `GET /v1/workouts`,
  `GET /v1/fasts`, `GET /v1/fasts/current`, `GET /v1/fasts/settings`,
  `GET /v1/water`, `GET /v1/water/settings`, `GET /v1/plans/current`,
  `GET /v1/assessments`, `GET /v1/calibrations`,
  `GET /v1/calibrations/progress`, `GET /v1/celebrations`,
  `GET /v1/personal-records`, `GET /v1/notifications`,
  `GET /v1/motivation`, `GET /v1/feed`, `GET /v1/integrations`,
  `GET /v1/users/search`, `GET /v1/billing/subscription`, `GET /v1/health`,
  `GET /v1/scans/{scan_id}` (read an existing scan, not create one) — all
  reads under the same caveat: authenticated, side-effect-free
- `GET /docs`, `GET /redoc`, `GET /openapi.json` (framework-provided; only
  live when `ENV != production`) — useful for the contract check to diff its
  expectations against the server's own live schema

A **dedicated staging test account**, not a real user's, should hold the
bearer token these checks use — its email is not one to reuse from any other
environment.

## Endpoints the gate must never call

**Hard rule from the task brief — billing and account deletion:**

- `DELETE /v1/me/account` — irreversible account deletion
- `POST /v1/billing/checkout` — creates a real Stripe checkout session
- `POST /v1/billing/portal` — opens a real Stripe billing portal session
- `POST /v1/billing/iap/apple`, `POST /v1/billing/iap/google` — verifies a
  real in-app-purchase receipt

**Extending the same principle — anything else that mutates state or spends
money, none of which a read-only contract check needs:**

- `POST /v1/scans`, `POST /v1/equipment/scan` — invoke the paid vision model
  (`gpt-4o` per `.env.example`) plus, depending on config, a paid depth or
  segmentation provider; this is the exact spend the gate-export task was
  told to avoid ("NO paid calls")
- Every other `POST`/`PATCH`/`DELETE` route in `backend/docs/openapi.json`
  (creating or deleting meals, workouts, recipes, posts, comments, likes,
  follows, restrictions, push tokens, water logs, fasts, integrations,
  celebrations-seen, notifications-read, recipe adapt/save) — each changes
  real data belonging to whatever account calls it. A contract check can
  confirm these routes *exist* and *reject* a malformed or unauthenticated
  request (a 4xx contract check), but should not exercise them to success
  against a real record.
- `POST /v1/integrations/{provider}/sync`,
  `GET /v1/integrations/{provider}/connect` — start real OAuth flows against
  Fitbit/Garmin/Google Fit
- `POST /v1/health/push` — writes wearable data as if from a real device

## What this plan does not decide

- **Hosting mechanism for the public HTTPS endpoint and TLS termination** —
  not specified anywhere in this repo; `docker-compose.yml` alone serves
  plain HTTP.
- **How the staging test account's bearer token is minted and rotated** —
  not specified; needs a decision before the "safe" GET list above can
  actually be exercised with auth.
- **Whether Base44's contract check hits staging directly or through some
  proxy/allowlist** — outside this repo's configuration.

Any of these left as open questions for Gil / Base44, not guessed at here.
