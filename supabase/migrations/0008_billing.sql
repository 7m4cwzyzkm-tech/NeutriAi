-- ============================================================
-- NeutriAI :: 0008 Stripe billing
-- ============================================================

create table billing_customers (
  user_id            uuid primary key references profiles(id) on delete cascade,
  stripe_customer_id text unique not null,
  default_pm_brand   text,
  default_pm_last4   text,
  created_at         timestamptz not null default now()
);

create table subscriptions (
  id                    uuid primary key default gen_random_uuid(),
  user_id               uuid not null references profiles(id) on delete cascade,
  stripe_subscription_id text unique not null,
  stripe_price_id       text not null,
  status                subscription_status_t not null,
  plan_interval         plan_interval_t not null,
  amount_cents          integer not null,
  currency              text not null default 'usd',
  trial_start           timestamptz,
  trial_end             timestamptz,
  current_period_start  timestamptz,
  current_period_end    timestamptz,
  cancel_at_period_end  boolean not null default false,
  canceled_at           timestamptz,
  promo_code            text,
  discount_pct          numeric(5,2),
  metadata              jsonb not null default '{}'::jsonb,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
create index on subscriptions(user_id, status);
create trigger t_subs_updated before update on subscriptions
  for each row execute function set_updated_at();

-- Idempotency ledger. Stripe retries; we must not double-apply.
create table stripe_events (
  id            text primary key,          -- Stripe event id (evt_...)
  type          text not null,
  received_at   timestamptz not null default now(),
  processed_at  timestamptz,
  status        text not null default 'received'
                check (status in ('received','processed','ignored','failed')),
  error         text,
  payload       jsonb not null
);
create index on stripe_events(type, received_at desc);

-- Single source of truth the API gates on. One row per user, cheap to read.
create table entitlements (
  user_id        uuid primary key references profiles(id) on delete cascade,
  tier           text not null default 'free' check (tier in ('free','trial','pro','pro_annual','comped')),
  is_active      boolean not null default false,
  ai_scans_used_today integer not null default 0,
  ai_scans_quota integer not null default 3,        -- free tier daily cap
  quota_reset_on date not null default current_date,
  expires_at     timestamptz,
  source         text not null default 'none' check (source in ('none','stripe','promo','admin','apple_iap','google_iap')),
  updated_at     timestamptz not null default now()
);

create or replace function entitlement_is_live(u uuid) returns boolean
language sql stable as $$
  select coalesce(
    (select is_active and (expires_at is null or expires_at > now())
       from entitlements where user_id = u), false)
$$;
