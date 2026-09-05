-- ============================================================
-- NutriAI :: 0004 wearables, workouts, coaching
-- ============================================================

create table device_connections (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references profiles(id) on delete cascade,
  provider       provider_t not null,
  external_user_id text,
  access_token   text,                -- encrypted at rest by the API layer
  refresh_token  text,
  scopes         text[] not null default '{}',
  expires_at     timestamptz,
  last_sync_at   timestamptz,
  sync_cursor    text,
  status         text not null default 'connected'
                 check (status in ('connected','expired','revoked','error')),
  error          text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (user_id, provider)
);
create trigger t_device_conn_updated before update on device_connections
  for each row execute function set_updated_at();

-- Normalized daily health rollup from any provider. One row per user/day/provider
-- so we can prefer a source (Apple > Garmin > Fitbit > Google) deterministically.
create table health_days (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references profiles(id) on delete cascade,
  day               date not null,
  provider          provider_t not null,
  steps             integer,
  distance_m        numeric(10,2),
  floors            integer,
  active_kcal       integer,
  resting_kcal      integer,
  total_kcal        integer,
  resting_hr        integer,
  avg_hr            integer,
  max_hr            integer,
  hrv_ms            numeric(6,2),
  vo2max            numeric(5,2),
  sleep_minutes     integer,
  sleep_deep_min    integer,
  sleep_rem_min     integer,
  sleep_score       integer,
  recovery_score    integer,
  raw               jsonb not null default '{}'::jsonb,
  synced_at         timestamptz not null default now(),
  unique (user_id, day, provider)
);
create index on health_days(user_id, day desc);

create table workouts (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references profiles(id) on delete cascade,
  plan_day_id      uuid,
  provider         provider_t not null default 'manual',
  external_id      text,
  title            text not null default 'Workout',
  kind             exercise_kind_t not null default 'strength',
  started_at       timestamptz not null,
  ended_at         timestamptz,
  duration_s       integer,
  kcal             integer,
  avg_hr           integer,
  max_hr           integer,
  hr_zones         jsonb not null default '{}'::jsonb, -- {z1..z5: seconds}
  perceived_effort integer check (perceived_effort between 1 and 10),
  notes            text,
  created_at       timestamptz not null default now(),
  unique (user_id, provider, external_id)
);
create index on workouts(user_id, started_at desc);

create table exercises (
  id            uuid primary key default gen_random_uuid(),
  slug          text unique not null,
  name          text not null,
  kind          exercise_kind_t not null default 'strength',
  primary_muscle text,
  secondary_muscles text[] not null default '{}',
  equipment     equipment_t[] not null default '{none}',
  is_unilateral boolean not null default false,
  difficulty    integer not null default 2 check (difficulty between 1 and 5),
  cues          text[] not null default '{}',
  regression_slug text,
  progression_slug text,
  met           numeric(4,2) not null default 5.0
);
create index on exercises using gin (equipment);

create table workout_sets (
  id           uuid primary key default gen_random_uuid(),
  workout_id   uuid not null references workouts(id) on delete cascade,
  exercise_id  uuid references exercises(id) on delete set null,
  exercise_name text not null,
  set_index    integer not null,
  reps         integer,
  weight_kg    numeric(7,2),
  duration_s   integer,
  distance_m   numeric(10,2),
  rpe          numeric(3,1),
  rest_s       integer,
  is_warmup    boolean not null default false,
  is_pr        boolean not null default false,
  created_at   timestamptz not null default now()
);
create index on workout_sets(workout_id, set_index);

create table personal_records (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references profiles(id) on delete cascade,
  exercise_slug text not null,
  metric        text not null check (metric in ('1rm','volume','reps','duration','distance','pace')),
  value         numeric(10,2) not null,
  unit          text not null,
  workout_id    uuid references workouts(id) on delete set null,
  achieved_at   timestamptz not null default now(),
  unique (user_id, exercise_slug, metric, achieved_at)
);
create index on personal_records(user_id, exercise_slug, metric, value desc);

-- Equipment the AI detected from the user's gym photo.
create table equipment_scans (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references profiles(id) on delete cascade,
  image_paths   text[] not null,
  detected      jsonb not null default '[]'::jsonb, -- [{equipment,confidence,detail}]
  equipment     equipment_t[] not null default '{}',
  space_note    text,
  confidence    numeric(4,3),
  created_at    timestamptz not null default now()
);
create index on equipment_scans(user_id, created_at desc);

create table training_plans (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references profiles(id) on delete cascade,
  equipment_scan_id uuid references equipment_scans(id) on delete set null,
  name           text not null,
  goal           text not null,
  days_per_week  integer not null check (days_per_week between 1 and 7),
  weeks          integer not null default 4,
  starts_on      date not null default current_date,
  equipment      equipment_t[] not null default '{}',
  is_calisthenics_fallback boolean not null default false,
  safety_notes   text[] not null default '{}',
  progression    jsonb not null default '{}'::jsonb,
  is_active      boolean not null default true,
  created_at     timestamptz not null default now()
);
create index on training_plans(user_id, is_active);

create table plan_days (
  id           uuid primary key default gen_random_uuid(),
  plan_id      uuid not null references training_plans(id) on delete cascade,
  week_index   integer not null,
  day_index    integer not null,
  title        text not null,
  kind         exercise_kind_t not null default 'strength',
  est_minutes  integer not null default 45,
  blocks       jsonb not null default '[]'::jsonb, -- [{exercise,sets,reps,rest_s,tempo,notes}]
  completed_at timestamptz,
  unique (plan_id, week_index, day_index)
);
create index on plan_days(plan_id, week_index, day_index);
