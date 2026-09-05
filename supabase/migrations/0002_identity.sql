-- ============================================================
-- NutriAI :: 0002 identity, profile, goals, targets
-- ============================================================

create table profiles (
  id                 uuid primary key references auth.users(id) on delete cascade,
  handle             citext unique not null,
  display_name       text not null default '',
  avatar_url         text,
  bio                text default '',
  sex                sex_t,
  birth_date         date,
  height_cm          numeric(5,1) check (height_cm between 60 and 260),
  weight_kg          numeric(5,2) check (weight_kg between 20 and 400),
  target_weight_kg   numeric(5,2),
  activity_level     activity_level_t not null default 'moderate',
  goal               goal_t not null default 'maintain',
  diet_mode          diet_mode_t not null default 'balanced',
  unit_system        unit_system_t not null default 'imperial',
  timezone           text not null default 'UTC',
  locale             text not null default 'en-US',
  onboarded_at       timestamptz,
  is_private         boolean not null default false,
  push_token         text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create index on profiles using gin (handle gin_trgm_ops);
create trigger t_profiles_updated before update on profiles
  for each row execute function set_updated_at();

-- Allergies / restrictions kept normalized so the recipe AI can query them.
create table dietary_restrictions (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references profiles(id) on delete cascade,
  kind        text not null check (kind in ('allergy','intolerance','avoid','religious','preference')),
  label       text not null,
  severity    text not null default 'moderate' check (severity in ('mild','moderate','severe','anaphylactic')),
  created_at  timestamptz not null default now(),
  unique (user_id, kind, label)
);
create index on dietary_restrictions(user_id);

-- Body metrics timeline (weight-ins, body fat, circumference).
create table body_metrics (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references profiles(id) on delete cascade,
  measured_at   timestamptz not null default now(),
  weight_kg     numeric(5,2),
  body_fat_pct  numeric(4,1),
  waist_cm      numeric(5,1),
  chest_cm      numeric(5,1),
  hip_cm        numeric(5,1),
  arm_cm        numeric(5,1),
  thigh_cm      numeric(5,1),
  note          text,
  source        provider_t not null default 'manual',
  created_at    timestamptz not null default now()
);
create index on body_metrics(user_id, measured_at desc);

-- Denormalized computed daily target. Recomputed whenever profile/goal changes.
create table nutrition_targets (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references profiles(id) on delete cascade,
  effective_from    date not null default current_date,
  bmr_kcal          integer not null,
  tdee_kcal         integer not null,
  target_kcal       integer not null,
  protein_g         integer not null,
  carbs_g           integer not null,
  fat_g             integer not null,
  fiber_g           integer not null default 30,
  sugar_g_max       integer not null default 50,
  water_ml          integer not null default 3785,   -- 1 US gallon
  rationale         jsonb not null default '{}'::jsonb,
  created_at        timestamptz not null default now(),
  unique (user_id, effective_from)
);
create index on nutrition_targets(user_id, effective_from desc);

-- Rolling per-day rollup so the dashboard is one row read, not a scan.
create table daily_summaries (
  user_id        uuid not null references profiles(id) on delete cascade,
  day            date not null,
  kcal_in        integer not null default 0,
  protein_g      numeric(7,2) not null default 0,
  carbs_g        numeric(7,2) not null default 0,
  fat_g          numeric(7,2) not null default 0,
  fiber_g        numeric(7,2) not null default 0,
  sugar_g        numeric(7,2) not null default 0,
  kcal_out       integer not null default 0,
  steps          integer not null default 0,
  water_ml       integer not null default 0,
  workouts       integer not null default 0,
  fast_minutes   integer not null default 0,
  goals_met      text[] not null default '{}',
  celebrated_at  timestamptz,
  updated_at     timestamptz not null default now(),
  primary key (user_id, day)
);
create index on daily_summaries(user_id, day desc);

-- Streaks are read on nearly every screen: keep them precomputed.
create table streaks (
  user_id      uuid not null references profiles(id) on delete cascade,
  kind         text not null,           -- 'log','water','fast','workout','post'
  current_len  integer not null default 0,
  best_len     integer not null default 0,
  last_day     date,
  updated_at   timestamptz not null default now(),
  primary key (user_id, kind)
);
