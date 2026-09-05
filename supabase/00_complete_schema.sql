-- NutriAI complete schema: 11 migrations in order, then the seed.
-- Run once. 'create type' has no IF NOT EXISTS, so a second run will
-- fail on the first enum. If it stops partway, note the last
-- migration that succeeded and resume from the next file.

-- ---------- 0001_extensions_and_enums.sql ----------
-- ============================================================
-- NutriAI :: 0001 extensions, enums, shared helpers
-- ============================================================
create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";
create extension if not exists "citext";
create extension if not exists "pg_trgm";

-- ---------- enums ----------
create type sex_t              as enum ('male','female','other');
create type activity_level_t   as enum ('sedentary','light','moderate','active','very_active','athlete');
create type goal_t             as enum ('lose','maintain','gain','recomp');
create type diet_mode_t        as enum ('balanced','low_carb','keto','high_protein','athlete','vegetarian','vegan','pescatarian','paleo','mediterranean');
create type meal_slot_t        as enum ('breakfast','lunch','dinner','snack','pre_workout','post_workout');
create type unit_system_t      as enum ('metric','imperial');
create type confidence_t       as enum ('low','medium','high');
create type subscription_status_t as enum ('trialing','active','past_due','canceled','incomplete','incomplete_expired','unpaid','paused');
create type plan_interval_t    as enum ('month','year');
create type provider_t         as enum ('apple_health','fitbit','garmin','google_fit','samsung_health','manual');
create type fast_protocol_t    as enum ('16:8','18:6','20:4','omad','5:2','custom');
create type fast_status_t      as enum ('active','completed','broken','abandoned');
create type equipment_t        as enum (
  'none','dumbbell','kettlebell','barbell','resistance_band','bench','squat_rack',
  'pull_up_bar','cable_machine','smith_machine','treadmill','bike','rower',
  'medicine_ball','trx','plate','jump_rope','box','machine_generic'
);
create type exercise_kind_t    as enum ('strength','cardio','mobility','hiit','calisthenics','core','rest');
create type post_kind_t        as enum ('meal','workout','recipe','progress','text','milestone');
create type notify_kind_t      as enum ('motivation','celebration','reminder','social','system','alert');
create type overeat_severity_t as enum ('none','mild','moderate','severe');

-- ---------- shared trigger ----------
create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

-- ---------- helper: current user id from Supabase JWT ----------
create or replace function nutriai_uid() returns uuid
language sql stable as $$ select auth.uid() $$;

-- ---------- 0002_identity.sql ----------
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

-- ---------- 0003_nutrition.sql ----------
-- ============================================================
-- NutriAI :: 0003 food scans, meals, nutrition cache
-- ============================================================

-- Provider-agnostic food fact cache. Keyed by a normalized name so
-- three different vendors collapse into one row and we stop paying
-- per-lookup after the first hit.
create table food_facts (
  id             uuid primary key default gen_random_uuid(),
  canonical_key  text not null unique,      -- lower(trim(name)) + '|' + source_id
  display_name   text not null,
  source         text not null check (source in ('usda','edamam','nutritionix','ai_estimate','user')),
  source_id      text,
  cuisine        text,
  density_g_ml   numeric(6,3),              -- used by the pixel->gram estimator
  kcal_per_100g  numeric(7,2) not null,
  protein_per_100g numeric(7,2) not null default 0,
  carbs_per_100g   numeric(7,2) not null default 0,
  fat_per_100g     numeric(7,2) not null default 0,
  fiber_per_100g   numeric(7,2) not null default 0,
  sugar_per_100g   numeric(7,2) not null default 0,
  sodium_mg_per_100g numeric(8,2) not null default 0,
  serving_hints  jsonb not null default '[]'::jsonb,  -- [{label,grams}]
  raw            jsonb not null default '{}'::jsonb,
  fetched_at     timestamptz not null default now(),
  hits           integer not null default 0
);
create index on food_facts using gin (display_name gin_trgm_ops);
create index on food_facts(source, source_id);

-- One camera capture. Multiple images allowed (multi-angle -> better depth).
create table food_scans (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references profiles(id) on delete cascade,
  image_paths       text[] not null,            -- Supabase Storage keys
  captured_at       timestamptz not null default now(),
  meal_slot         meal_slot_t,
  status            text not null default 'pending'
                    check (status in ('pending','processing','complete','failed','needs_review')),
  vision_model      text,
  reasoning_model   text,
  overall_confidence numeric(4,3),
  confidence_band   confidence_t,
  calibration_id    uuid,
  latency_ms        integer,
  cost_usd          numeric(8,5),
  error             text,
  raw_vision        jsonb,
  raw_reasoning     jsonb,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index on food_scans(user_id, captured_at desc);
create trigger t_food_scans_updated before update on food_scans
  for each row execute function set_updated_at();

-- Per-plate reference object used to convert pixels to real-world area.
create table scan_calibrations (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references profiles(id) on delete cascade,
  label          text not null,               -- "my dinner plate"
  reference_kind text not null check (reference_kind in ('plate','card','coin','hand','utensil','custom')),
  real_diameter_mm numeric(7,2),
  real_area_mm2  numeric(10,2),
  is_default     boolean not null default false,
  created_at     timestamptz not null default now(),
  unique (user_id, label)
);
create index on scan_calibrations(user_id);

-- A meal is the user-facing, editable record. A scan produces one, but a
-- meal can also be created manually or from a recipe.
create table meals (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references profiles(id) on delete cascade,
  scan_id       uuid references food_scans(id) on delete set null,
  recipe_id     uuid,
  eaten_at      timestamptz not null default now(),
  day           date not null default current_date,
  meal_slot     meal_slot_t not null default 'snack',
  title         text not null default '',
  notes         text,
  kcal          numeric(8,2) not null default 0,
  protein_g     numeric(7,2) not null default 0,
  carbs_g       numeric(7,2) not null default 0,
  fat_g         numeric(7,2) not null default 0,
  fiber_g       numeric(7,2) not null default 0,
  sugar_g       numeric(7,2) not null default 0,
  sodium_mg     numeric(8,2) not null default 0,
  confidence    numeric(4,3),
  is_verified   boolean not null default false,   -- user confirmed / edited
  photo_path    text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index on meals(user_id, day desc, eaten_at desc);
create index on meals(scan_id);
create trigger t_meals_updated before update on meals
  for each row execute function set_updated_at();

-- Each detected food on the plate.
create table meal_items (
  id                uuid primary key default gen_random_uuid(),
  meal_id           uuid not null references meals(id) on delete cascade,
  food_fact_id      uuid references food_facts(id) on delete set null,
  name              text not null,
  cuisine           text,
  grams             numeric(8,2) not null,
  grams_low         numeric(8,2),
  grams_high        numeric(8,2),
  estimation_method text not null default 'pixel_area'
                    check (estimation_method in ('pixel_area','plate_reference','depth_model','multi_image','ai_prior','user_entered','barcode')),
  pixel_area_ratio  numeric(6,4),
  depth_factor      numeric(6,4),
  confidence        numeric(4,3),
  kcal              numeric(8,2) not null default 0,
  protein_g         numeric(7,2) not null default 0,
  carbs_g           numeric(7,2) not null default 0,
  fat_g             numeric(7,2) not null default 0,
  fiber_g           numeric(7,2) not null default 0,
  sugar_g           numeric(7,2) not null default 0,
  sodium_mg         numeric(8,2) not null default 0,
  bbox              jsonb,   -- {x,y,w,h} normalized 0..1
  created_at        timestamptz not null default now()
);
create index on meal_items(meal_id);

-- Overeating / macro-drift assessments produced after each meal.
create table intake_assessments (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references profiles(id) on delete cascade,
  meal_id          uuid references meals(id) on delete cascade,
  day              date not null default current_date,
  severity         overeat_severity_t not null default 'none',
  kcal_over        numeric(8,2) not null default 0,
  pct_of_target    numeric(6,2) not null default 0,
  carb_load_flag   boolean not null default false,
  meal_frequency   integer not null default 0,
  headline         text not null,
  detail           text not null default '',
  portion_advice   jsonb not null default '[]'::jsonb,
  macro_corrections jsonb not null default '{}'::jsonb,
  next_meal        jsonb not null default '{}'::jsonb,
  created_at       timestamptz not null default now()
);
create index on intake_assessments(user_id, day desc);

-- ---------- 0004_fitness.sql ----------
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

-- ---------- 0005_lifestyle.sql ----------
-- ============================================================
-- NutriAI :: 0005 hydration + intermittent fasting
-- ============================================================

create table water_logs (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references profiles(id) on delete cascade,
  day         date not null default current_date,
  amount_ml   integer not null check (amount_ml between 1 and 4000),
  container   text,                       -- 'glass','bottle','gallon_jug','custom'
  source      provider_t not null default 'manual',
  logged_at   timestamptz not null default now()
);
create index on water_logs(user_id, day desc);

create table hydration_settings (
  user_id          uuid primary key references profiles(id) on delete cascade,
  daily_goal_ml    integer not null default 3785,   -- 1 US gallon
  reminder_enabled boolean not null default true,
  reminder_start   time not null default '08:00',
  reminder_end     time not null default '21:00',
  reminder_every_min integer not null default 90,
  sync_apple_health boolean not null default false,
  updated_at       timestamptz not null default now()
);

create table fasting_settings (
  user_id           uuid primary key references profiles(id) on delete cascade,
  protocol          fast_protocol_t not null default '16:8',
  custom_fast_hours numeric(4,1),
  eating_window_start time not null default '12:00',
  auto_start        boolean not null default true,
  notify_start      boolean not null default true,
  notify_end        boolean not null default true,
  notify_halfway    boolean not null default false,
  updated_at        timestamptz not null default now()
);

create table fasts (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references profiles(id) on delete cascade,
  protocol       fast_protocol_t not null default '16:8',
  target_minutes integer not null,
  started_at     timestamptz not null default now(),
  ended_at       timestamptz,
  actual_minutes integer,
  status         fast_status_t not null default 'active',
  break_meal_id  uuid references meals(id) on delete set null,
  note           text,
  created_at     timestamptz not null default now()
);
create index on fasts(user_id, started_at desc);
-- At most one active fast per user.
create unique index one_active_fast on fasts(user_id) where status = 'active';

-- ---------- 0006_recipes.sql ----------
-- ============================================================
-- NutriAI :: 0006 recipes + AI personalization
-- ============================================================

create table recipes (
  id             uuid primary key default gen_random_uuid(),
  author_id      uuid not null references profiles(id) on delete cascade,
  parent_id      uuid references recipes(id) on delete set null, -- AI-adapted fork
  title          text not null,
  summary        text default '',
  photo_paths    text[] not null default '{}',
  categories     text[] not null default '{}',
  cuisine        text,
  servings       integer not null default 1 check (servings > 0),
  prep_minutes   integer not null default 0,
  cook_minutes   integer not null default 0,
  difficulty     integer not null default 2 check (difficulty between 1 and 5),
  steps          jsonb not null default '[]'::jsonb,  -- [{n,text,minutes,tip}]
  tags           text[] not null default '{}',
  kcal_per_serving   numeric(8,2) not null default 0,
  protein_g_per_serving numeric(7,2) not null default 0,
  carbs_g_per_serving   numeric(7,2) not null default 0,
  fat_g_per_serving     numeric(7,2) not null default 0,
  fiber_g_per_serving   numeric(7,2) not null default 0,
  sugar_g_per_serving   numeric(7,2) not null default 0,
  macros_computed_at timestamptz,
  is_public      boolean not null default true,
  is_ai_generated boolean not null default false,
  adaptation_note text,
  like_count     integer not null default 0,
  save_count     integer not null default 0,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index on recipes(author_id, created_at desc);
create index on recipes using gin (title gin_trgm_ops);
create index on recipes using gin (tags);
create trigger t_recipes_updated before update on recipes
  for each row execute function set_updated_at();

create table recipe_ingredients (
  id           uuid primary key default gen_random_uuid(),
  recipe_id    uuid not null references recipes(id) on delete cascade,
  position     integer not null default 0,
  raw_text     text not null,             -- "2 cups jasmine rice"
  name         text not null,
  quantity     numeric(9,3),
  unit         text,
  grams        numeric(9,2),
  food_fact_id uuid references food_facts(id) on delete set null,
  is_optional  boolean not null default false,
  substituted_from text,                  -- set on AI adaptations
  kcal         numeric(8,2) not null default 0,
  protein_g    numeric(7,2) not null default 0,
  carbs_g      numeric(7,2) not null default 0,
  fat_g        numeric(7,2) not null default 0,
  fiber_g      numeric(7,2) not null default 0,
  sugar_g      numeric(7,2) not null default 0
);
create index on recipe_ingredients(recipe_id, position);

create table recipe_saves (
  user_id    uuid not null references profiles(id) on delete cascade,
  recipe_id  uuid not null references recipes(id) on delete cascade,
  folder     text not null default 'default',
  created_at timestamptz not null default now(),
  primary key (user_id, recipe_id)
);

create table shopping_lists (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references profiles(id) on delete cascade,
  name       text not null default 'Shopping list',
  recipe_ids uuid[] not null default '{}',
  items      jsonb not null default '[]'::jsonb, -- [{name,qty,unit,aisle,checked}]
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index on shopping_lists(user_id, created_at desc);

-- ---------- 0007_social.sql ----------
-- ============================================================
-- NutriAI :: 0007 social graph + feed
-- ============================================================

create table follows (
  follower_id  uuid not null references profiles(id) on delete cascade,
  followee_id  uuid not null references profiles(id) on delete cascade,
  created_at   timestamptz not null default now(),
  primary key (follower_id, followee_id),
  check (follower_id <> followee_id)
);
create index on follows(followee_id);

create table posts (
  id           uuid primary key default gen_random_uuid(),
  author_id    uuid not null references profiles(id) on delete cascade,
  kind         post_kind_t not null default 'text',
  body         text not null default '',
  media_paths  text[] not null default '{}',
  meal_id      uuid references meals(id) on delete set null,
  workout_id   uuid references workouts(id) on delete set null,
  recipe_id    uuid references recipes(id) on delete set null,
  metrics      jsonb not null default '{}'::jsonb,  -- denormalized snapshot
  visibility   text not null default 'public' check (visibility in ('public','followers','private')),
  like_count   integer not null default 0,
  comment_count integer not null default 0,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index on posts(author_id, created_at desc);
create index on posts(created_at desc) where visibility = 'public';

create table post_likes (
  post_id    uuid not null references posts(id) on delete cascade,
  user_id    uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (post_id, user_id)
);

create table comments (
  id         uuid primary key default gen_random_uuid(),
  post_id    uuid not null references posts(id) on delete cascade,
  author_id  uuid not null references profiles(id) on delete cascade,
  parent_id  uuid references comments(id) on delete cascade,
  body       text not null,
  like_count integer not null default 0,
  created_at timestamptz not null default now()
);
create index on comments(post_id, created_at);

-- Counter maintenance so the feed never has to COUNT(*).
create or replace function bump_post_likes() returns trigger language plpgsql as $$
begin
  if tg_op = 'INSERT' then
    update posts set like_count = like_count + 1 where id = new.post_id;
  else
    update posts set like_count = greatest(like_count - 1, 0) where id = old.post_id;
  end if;
  return null;
end $$;
create trigger t_post_likes after insert or delete on post_likes
  for each row execute function bump_post_likes();

create or replace function bump_post_comments() returns trigger language plpgsql as $$
begin
  if tg_op = 'INSERT' then
    update posts set comment_count = comment_count + 1 where id = new.post_id;
  else
    update posts set comment_count = greatest(comment_count - 1, 0) where id = old.post_id;
  end if;
  return null;
end $$;
create trigger t_comments after insert or delete on comments
  for each row execute function bump_post_comments();

create or replace function bump_recipe_saves() returns trigger language plpgsql as $$
begin
  if tg_op = 'INSERT' then
    update recipes set save_count = save_count + 1 where id = new.recipe_id;
  else
    update recipes set save_count = greatest(save_count - 1, 0) where id = old.recipe_id;
  end if;
  return null;
end $$;
create trigger t_recipe_saves after insert or delete on recipe_saves
  for each row execute function bump_recipe_saves();

-- ---------- 0008_billing.sql ----------
-- ============================================================
-- NutriAI :: 0008 Stripe billing
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

-- ---------- 0009_motivation.sql ----------
-- ============================================================
-- NutriAI :: 0009 motivation, celebration, notifications
-- ============================================================

create table motivation_messages (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references profiles(id) on delete cascade,
  trigger      text not null,         -- goal_hit, meal_logged, overeat, ...
  tone         text not null default 'encouraging',
  body         text not null,
  emoji        text,
  context      jsonb not null default '{}'::jsonb,
  -- Hash of the normalized body. Enforces "never repeat the same line twice".
  body_hash    text not null,
  model        text,
  delivered_at timestamptz,
  reacted      text check (reacted in ('love','meh','mute')),
  created_at   timestamptz not null default now()
);
create unique index motivation_no_repeat on motivation_messages(user_id, body_hash);
create index on motivation_messages(user_id, created_at desc);

create table celebrations (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references profiles(id) on delete cascade,
  day        date not null default current_date,
  kind       text not null,           -- daily_goals, streak, pr, first_post
  title      text not null,
  subtitle   text not null default '',
  animation  text not null default 'confetti'
             check (animation in ('confetti','fireworks','rings','flame','trophy','wave')),
  payload    jsonb not null default '{}'::jsonb,
  seen_at    timestamptz,
  created_at timestamptz not null default now(),
  unique (user_id, day, kind)
);
create index on celebrations(user_id, created_at desc);

create table notifications (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references profiles(id) on delete cascade,
  kind       notify_kind_t not null default 'system',
  title      text not null,
  body       text not null default '',
  deep_link  text,
  actor_id   uuid references profiles(id) on delete set null,
  payload    jsonb not null default '{}'::jsonb,
  read_at    timestamptz,
  pushed_at  timestamptz,
  created_at timestamptz not null default now()
);
create index on notifications(user_id, created_at desc);
create index on notifications(user_id) where read_at is null;

create table notification_settings (
  user_id            uuid primary key references profiles(id) on delete cascade,
  motivation         boolean not null default true,
  celebration        boolean not null default true,
  water_reminders    boolean not null default true,
  fasting_reminders  boolean not null default true,
  workout_reminders  boolean not null default true,
  social             boolean not null default true,
  quiet_start        time not null default '22:00',
  quiet_end          time not null default '07:00',
  updated_at         timestamptz not null default now()
);

-- Rolling AI spend guard so a runaway loop can't burn the budget.
create table ai_usage (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid references profiles(id) on delete set null,
  pipeline     text not null,
  model        text not null,
  input_tokens integer not null default 0,
  output_tokens integer not null default 0,
  cost_usd     numeric(10,6) not null default 0,
  latency_ms   integer,
  ok           boolean not null default true,
  created_at   timestamptz not null default now()
);
create index on ai_usage(user_id, created_at desc);
create index on ai_usage(pipeline, created_at desc);

-- ---------- 0010_rollups.sql ----------
-- ============================================================
-- NutriAI :: 0010 rollup + streak automation
-- ============================================================

-- Recompute one user/day summary from source tables. Called by triggers and
-- by the nightly worker (idempotent, so replaying is safe).
create or replace function recompute_daily_summary(p_user uuid, p_day date)
returns void language plpgsql security definer as $$
declare
  v record;
  v_water int;
  v_health record;
  v_workouts int;
  v_fast int;
  v_targets record;
  v_met text[] := '{}';
begin
  select coalesce(sum(kcal),0) kcal, coalesce(sum(protein_g),0) p,
         coalesce(sum(carbs_g),0) c, coalesce(sum(fat_g),0) f,
         coalesce(sum(fiber_g),0) fi, coalesce(sum(sugar_g),0) s
    into v from meals where user_id = p_user and day = p_day;

  select coalesce(sum(amount_ml),0) into v_water
    from water_logs where user_id = p_user and day = p_day;

  -- Provider precedence: apple > garmin > fitbit > google > samsung > manual
  select * into v_health from health_days
   where user_id = p_user and day = p_day
   order by case provider
     when 'apple_health' then 1 when 'garmin' then 2 when 'fitbit' then 3
     when 'google_fit' then 4 when 'samsung_health' then 5 else 6 end
   limit 1;

  select count(*) into v_workouts from workouts
   where user_id = p_user and started_at::date = p_day;

  select coalesce(sum(actual_minutes),0) into v_fast from fasts
   where user_id = p_user and started_at::date = p_day and status = 'completed';

  select * into v_targets from nutrition_targets
   where user_id = p_user and effective_from <= p_day
   order by effective_from desc limit 1;

  if v_targets.id is not null then
    if v.kcal between v_targets.target_kcal * 0.85 and v_targets.target_kcal * 1.05
      then v_met := array_append(v_met, 'calories'); end if;
    if v.p >= v_targets.protein_g * 0.9 then v_met := array_append(v_met, 'protein'); end if;
    if v_water >= v_targets.water_ml then v_met := array_append(v_met, 'water'); end if;
  end if;
  if v_workouts > 0 then v_met := array_append(v_met, 'workout'); end if;
  if coalesce(v_health.steps,0) >= 10000 then v_met := array_append(v_met, 'steps'); end if;

  insert into daily_summaries as d
    (user_id, day, kcal_in, protein_g, carbs_g, fat_g, fiber_g, sugar_g,
     kcal_out, steps, water_ml, workouts, fast_minutes, goals_met, updated_at)
  values
    (p_user, p_day, round(v.kcal), v.p, v.c, v.f, v.fi, v.s,
     coalesce(v_health.active_kcal,0) + coalesce(v_health.resting_kcal,0),
     coalesce(v_health.steps,0), v_water, v_workouts, v_fast, v_met, now())
  on conflict (user_id, day) do update set
     kcal_in = excluded.kcal_in, protein_g = excluded.protein_g,
     carbs_g = excluded.carbs_g, fat_g = excluded.fat_g,
     fiber_g = excluded.fiber_g, sugar_g = excluded.sugar_g,
     kcal_out = excluded.kcal_out, steps = excluded.steps,
     water_ml = excluded.water_ml, workouts = excluded.workouts,
     fast_minutes = excluded.fast_minutes, goals_met = excluded.goals_met,
     updated_at = now();
end $$;

create or replace function trg_recompute_from_meal() returns trigger
language plpgsql security definer as $$
begin
  perform recompute_daily_summary(coalesce(new.user_id, old.user_id),
                                  coalesce(new.day, old.day));
  return null;
end $$;
create trigger t_meals_rollup after insert or update or delete on meals
  for each row execute function trg_recompute_from_meal();

create or replace function trg_recompute_from_water() returns trigger
language plpgsql security definer as $$
begin
  perform recompute_daily_summary(coalesce(new.user_id, old.user_id),
                                  coalesce(new.day, old.day));
  return null;
end $$;
create trigger t_water_rollup after insert or update or delete on water_logs
  for each row execute function trg_recompute_from_water();

create or replace function trg_recompute_from_health() returns trigger
language plpgsql security definer as $$
begin
  perform recompute_daily_summary(coalesce(new.user_id, old.user_id),
                                  coalesce(new.day, old.day));
  return null;
end $$;
create trigger t_health_rollup after insert or update on health_days
  for each row execute function trg_recompute_from_health();

-- Streak bump: extend if yesterday, reset if a day was skipped.
create or replace function bump_streak(p_user uuid, p_kind text, p_day date)
returns integer language plpgsql security definer as $$
declare v_cur int; v_last date; v_new int;
begin
  select current_len, last_day into v_cur, v_last
    from streaks where user_id = p_user and kind = p_kind;

  if v_last = p_day then
    return v_cur;                               -- already counted today
  elsif v_last = p_day - 1 then
    v_new := coalesce(v_cur,0) + 1;
  else
    v_new := 1;
  end if;

  insert into streaks (user_id, kind, current_len, best_len, last_day, updated_at)
  values (p_user, p_kind, v_new, v_new, p_day, now())
  on conflict (user_id, kind) do update set
    current_len = v_new,
    best_len = greatest(streaks.best_len, v_new),
    last_day = p_day, updated_at = now();
  return v_new;
end $$;

-- Dashboard read: one round trip for everything the home screen needs.
create or replace function dashboard(p_day date default current_date)
returns jsonb language sql stable security definer as $$
  select jsonb_build_object(
    'day', p_day,
    'summary', (select to_jsonb(d) from daily_summaries d
                 where d.user_id = auth.uid() and d.day = p_day),
    'targets', (select to_jsonb(t) from nutrition_targets t
                 where t.user_id = auth.uid() and t.effective_from <= p_day
                 order by t.effective_from desc limit 1),
    'streaks', (select coalesce(jsonb_object_agg(kind, current_len), '{}'::jsonb)
                 from streaks where user_id = auth.uid()),
    'active_fast', (select to_jsonb(f) from fasts f
                 where f.user_id = auth.uid() and f.status = 'active' limit 1),
    'meals', (select coalesce(jsonb_agg(to_jsonb(m) order by m.eaten_at), '[]'::jsonb)
                 from meals m where m.user_id = auth.uid() and m.day = p_day),
    'celebration', (select to_jsonb(c) from celebrations c
                 where c.user_id = auth.uid() and c.day = p_day and c.seen_at is null
                 order by c.created_at desc limit 1),
    'unread_notifications', (select count(*) from notifications
                 where user_id = auth.uid() and read_at is null)
  )
$$;

-- ---------- 0011_rls.sql ----------
-- ============================================================
-- NutriAI :: 0011 Row Level Security
-- Default posture: deny-all, then grant the narrowest thing that works.
-- The FastAPI service role bypasses RLS; the mobile client does not.
-- ============================================================

do $$
declare t text;
begin
  foreach t in array array[
    'profiles','dietary_restrictions','body_metrics','nutrition_targets',
    'daily_summaries','streaks','food_scans','scan_calibrations','meals',
    'meal_items','intake_assessments','device_connections','health_days',
    'workouts','workout_sets','personal_records','equipment_scans',
    'training_plans','plan_days','water_logs','hydration_settings',
    'fasting_settings','fasts','recipes','recipe_ingredients','recipe_saves',
    'shopping_lists','follows','posts','post_likes','comments',
    'billing_customers','subscriptions','entitlements','motivation_messages',
    'celebrations','notifications','notification_settings','ai_usage'
  ] loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
  end loop;
end $$;

-- food_facts and exercises are shared reference data: readable by all,
-- writable only by the service role.
alter table food_facts enable row level security;
alter table exercises  enable row level security;
create policy read_food_facts on food_facts for select to authenticated using (true);
create policy read_exercises  on exercises  for select to authenticated using (true);

-- ---------- generic owner policies ----------
do $$
declare t text;
begin
  foreach t in array array[
    'dietary_restrictions','body_metrics','nutrition_targets','daily_summaries',
    'streaks','food_scans','scan_calibrations','meals','intake_assessments',
    'device_connections','health_days','workouts','personal_records',
    'equipment_scans','training_plans','water_logs','hydration_settings',
    'fasting_settings','fasts','shopping_lists','recipe_saves',
    'motivation_messages','celebrations','notifications','notification_settings'
  ] loop
    execute format($f$
      create policy %1$s_own_select on %1$I for select to authenticated
        using (user_id = auth.uid());
      create policy %1$s_own_insert on %1$I for insert to authenticated
        with check (user_id = auth.uid());
      create policy %1$s_own_update on %1$I for update to authenticated
        using (user_id = auth.uid()) with check (user_id = auth.uid());
      create policy %1$s_own_delete on %1$I for delete to authenticated
        using (user_id = auth.uid());
    $f$, t);
  end loop;
end $$;

-- Read-only-to-client tables (server writes them).
create policy entitlements_own on entitlements for select to authenticated
  using (user_id = auth.uid());
create policy subscriptions_own on subscriptions for select to authenticated
  using (user_id = auth.uid());
create policy billing_own on billing_customers for select to authenticated
  using (user_id = auth.uid());
create policy ai_usage_own on ai_usage for select to authenticated
  using (user_id = auth.uid());

-- ---------- profiles ----------
create policy profiles_self_write on profiles for update to authenticated
  using (id = auth.uid()) with check (id = auth.uid());
create policy profiles_self_insert on profiles for insert to authenticated
  with check (id = auth.uid());
-- Public profiles are visible; private ones only to the owner and followers.
create policy profiles_read on profiles for select to authenticated using (
  id = auth.uid()
  or not is_private
  or exists (select 1 from follows f
              where f.followee_id = profiles.id and f.follower_id = auth.uid())
);

-- ---------- child tables: inherit the parent's owner ----------
create policy meal_items_own on meal_items for all to authenticated
  using (exists (select 1 from meals m where m.id = meal_id and m.user_id = auth.uid()))
  with check (exists (select 1 from meals m where m.id = meal_id and m.user_id = auth.uid()));

create policy workout_sets_own on workout_sets for all to authenticated
  using (exists (select 1 from workouts w where w.id = workout_id and w.user_id = auth.uid()))
  with check (exists (select 1 from workouts w where w.id = workout_id and w.user_id = auth.uid()));

create policy plan_days_own on plan_days for all to authenticated
  using (exists (select 1 from training_plans p where p.id = plan_id and p.user_id = auth.uid()))
  with check (exists (select 1 from training_plans p where p.id = plan_id and p.user_id = auth.uid()));

create policy recipe_ing_read on recipe_ingredients for select to authenticated
  using (exists (select 1 from recipes r where r.id = recipe_id
                  and (r.is_public or r.author_id = auth.uid())));
create policy recipe_ing_write on recipe_ingredients for all to authenticated
  using (exists (select 1 from recipes r where r.id = recipe_id and r.author_id = auth.uid()))
  with check (exists (select 1 from recipes r where r.id = recipe_id and r.author_id = auth.uid()));

-- ---------- recipes ----------
create policy recipes_read on recipes for select to authenticated
  using (is_public or author_id = auth.uid());
create policy recipes_write on recipes for insert to authenticated
  with check (author_id = auth.uid());
create policy recipes_update on recipes for update to authenticated
  using (author_id = auth.uid()) with check (author_id = auth.uid());
create policy recipes_delete on recipes for delete to authenticated
  using (author_id = auth.uid());

-- ---------- social ----------
create policy follows_read on follows for select to authenticated
  using (follower_id = auth.uid() or followee_id = auth.uid());
create policy follows_write on follows for insert to authenticated
  with check (follower_id = auth.uid());
create policy follows_delete on follows for delete to authenticated
  using (follower_id = auth.uid());

create policy posts_read on posts for select to authenticated using (
  author_id = auth.uid()
  or visibility = 'public'
  or (visibility = 'followers' and exists (
        select 1 from follows f
         where f.followee_id = posts.author_id and f.follower_id = auth.uid()))
);
create policy posts_write on posts for insert to authenticated
  with check (author_id = auth.uid());
create policy posts_update on posts for update to authenticated
  using (author_id = auth.uid()) with check (author_id = auth.uid());
create policy posts_delete on posts for delete to authenticated
  using (author_id = auth.uid());

create policy likes_read on post_likes for select to authenticated using (true);
create policy likes_write on post_likes for insert to authenticated
  with check (user_id = auth.uid());
create policy likes_delete on post_likes for delete to authenticated
  using (user_id = auth.uid());

create policy comments_read on comments for select to authenticated
  using (exists (select 1 from posts p where p.id = post_id));
create policy comments_write on comments for insert to authenticated
  with check (author_id = auth.uid());
create policy comments_delete on comments for delete to authenticated
  using (author_id = auth.uid()
         or exists (select 1 from posts p where p.id = post_id and p.author_id = auth.uid()));

-- ---------- storage buckets ----------
insert into storage.buckets (id, name, public)
values ('meal-photos', 'meal-photos', false),
       ('equipment-photos', 'equipment-photos', false),
       ('recipe-photos', 'recipe-photos', true),
       ('avatars', 'avatars', true),
       ('post-media', 'post-media', true)
on conflict (id) do nothing;

-- Users may only touch objects under a folder named with their own uid.
create policy own_folder_rw on storage.objects for all to authenticated
  using (bucket_id in ('meal-photos','equipment-photos','recipe-photos','avatars','post-media')
         and (storage.foldername(name))[1] = auth.uid()::text)
  with check (bucket_id in ('meal-photos','equipment-photos','recipe-photos','avatars','post-media')
         and (storage.foldername(name))[1] = auth.uid()::text);
create policy public_bucket_read on storage.objects for select to authenticated
  using (bucket_id in ('recipe-photos','avatars','post-media'));

-- ---------- seed.sql ----------
-- ============================================================
-- NutriAI :: exercise library seed (bodyweight-first so the
-- calisthenics fallback always has something to prescribe)
-- ============================================================
insert into exercises (slug,name,kind,primary_muscle,secondary_muscles,equipment,difficulty,cues,regression_slug,progression_slug,met) values
('push-up','Push-Up','calisthenics','chest','{triceps,front_delt,core}','{none}',2,'{"Ribs down, glutes tight","Elbows ~45 degrees from torso","Full lockout at top"}','incline-push-up','archer-push-up',8.0),
('incline-push-up','Incline Push-Up','calisthenics','chest','{triceps,front_delt}','{none,bench}',1,'{"Higher surface = easier","Body in one line"}','wall-push-up','push-up',6.0),
('wall-push-up','Wall Push-Up','calisthenics','chest','{triceps}','{none}',1,'{"Stand arm''s length from wall"}',null,'incline-push-up',3.5),
('archer-push-up','Archer Push-Up','calisthenics','chest','{triceps,core}','{none}',4,'{"Shift weight over working arm"}','push-up','one-arm-push-up',9.0),
('one-arm-push-up','One-Arm Push-Up','calisthenics','chest','{core,triceps}','{none}',5,'{"Widen feet for stability"}','archer-push-up',null,10.0),
('bodyweight-squat','Bodyweight Squat','calisthenics','quads','{glutes,core}','{none}',1,'{"Knees track over toes","Chest proud"}','box-squat','bulgarian-split-squat',5.0),
('box-squat','Box Squat','calisthenics','quads','{glutes}','{none,box,bench}',1,'{"Sit back, tap, stand"}',null,'bodyweight-squat',4.5),
('bulgarian-split-squat','Bulgarian Split Squat','strength','quads','{glutes,adductors}','{none,bench,dumbbell}',3,'{"Rear foot elevated","Front shin near vertical"}','reverse-lunge','pistol-squat',6.0),
('pistol-squat','Pistol Squat','calisthenics','quads','{glutes,core}','{none}',5,'{"Counterbalance with arms"}','bulgarian-split-squat',null,8.0),
('reverse-lunge','Reverse Lunge','calisthenics','quads','{glutes,hamstrings}','{none,dumbbell}',2,'{"Step back, drop straight down"}','box-squat','bulgarian-split-squat',6.0),
('goblet-squat','Goblet Squat','strength','quads','{glutes,core}','{dumbbell,kettlebell}',2,'{"Elbows inside knees at bottom"}','bodyweight-squat','front-squat',6.0),
('front-squat','Front Squat','strength','quads','{glutes,core}','{barbell,squat_rack}',4,'{"Elbows high","Brace before descent"}','goblet-squat','back-squat',7.0),
('back-squat','Back Squat','strength','quads','{glutes,hamstrings,core}','{barbell,squat_rack}',4,'{"Bar on upper traps","Break at hips and knees together"}','front-squat',null,7.5),
('deadlift','Conventional Deadlift','strength','hamstrings','{glutes,back,grip}','{barbell,plate}',4,'{"Bar over mid-foot","Lats engaged, push floor away"}','romanian-deadlift',null,7.5),
('romanian-deadlift','Romanian Deadlift','strength','hamstrings','{glutes,back}','{barbell,dumbbell,kettlebell}',3,'{"Hinge, soft knees","Bar stays close"}','hip-hinge','deadlift',6.0),
('hip-hinge','Bodyweight Hip Hinge','calisthenics','hamstrings','{glutes}','{none}',1,'{"Push hips back to a wall"}',null,'romanian-deadlift',3.5),
('kettlebell-swing','Kettlebell Swing','hiit','glutes','{hamstrings,core,shoulders}','{kettlebell}',3,'{"Hips snap, arms are ropes","Float, do not lift"}','romanian-deadlift','kettlebell-snatch',9.8),
('kettlebell-snatch','Kettlebell Snatch','hiit','glutes','{shoulders,core}','{kettlebell}',5,'{"Punch through at the top"}','kettlebell-swing',null,12.0),
('pull-up','Pull-Up','calisthenics','lats','{biceps,core}','{pull_up_bar}',4,'{"Chest to bar","Full hang at bottom"}','band-assisted-pull-up','weighted-pull-up',8.0),
('band-assisted-pull-up','Band-Assisted Pull-Up','calisthenics','lats','{biceps}','{pull_up_bar,resistance_band}',2,'{"Band under knee or foot"}','inverted-row','pull-up',6.0),
('weighted-pull-up','Weighted Pull-Up','strength','lats','{biceps,core}','{pull_up_bar,plate}',5,'{"Control the descent"}','pull-up',null,9.0),
('inverted-row','Inverted Row','calisthenics','back','{biceps,rear_delt}','{none,barbell,trx,squat_rack}',2,'{"Body rigid, pull chest to bar"}',null,'pull-up',5.5),
('bent-over-row','Bent-Over Row','strength','back','{biceps,rear_delt}','{barbell,dumbbell}',3,'{"Hinge to ~45 degrees","Drive elbows back"}','inverted-row',null,6.0),
('dumbbell-row','Single-Arm Dumbbell Row','strength','back','{biceps,core}','{dumbbell,bench}',2,'{"No torso rotation"}','inverted-row','bent-over-row',5.5),
('bench-press','Barbell Bench Press','strength','chest','{triceps,front_delt}','{barbell,bench,squat_rack}',3,'{"Shoulder blades pinched","Bar to lower chest"}','dumbbell-press','weighted-dip',6.0),
('dumbbell-press','Dumbbell Bench Press','strength','chest','{triceps,front_delt}','{dumbbell,bench}',2,'{"Wrists stacked over elbows"}','push-up','bench-press',5.5),
('overhead-press','Overhead Press','strength','shoulders','{triceps,core}','{barbell,dumbbell}',3,'{"Squeeze glutes, no lean back"}','pike-push-up',null,6.0),
('pike-push-up','Pike Push-Up','calisthenics','shoulders','{triceps}','{none}',3,'{"Hips high, crown to floor"}','incline-push-up','handstand-push-up',7.0),
('handstand-push-up','Handstand Push-Up','calisthenics','shoulders','{triceps,core}','{none}',5,'{"Wall for balance"}','pike-push-up',null,9.0),
('weighted-dip','Weighted Dip','strength','chest','{triceps}','{pull_up_bar,plate}',4,'{"Slight forward lean"}','dip',null,8.0),
('dip','Parallel Bar Dip','calisthenics','triceps','{chest,front_delt}','{pull_up_bar,none}',3,'{"Shoulders stay above elbows"}','bench-dip','weighted-dip',7.0),
('bench-dip','Bench Dip','calisthenics','triceps','{front_delt}','{bench,none}',1,'{"Keep hips close to bench"}',null,'dip',4.5),
('plank','Plank','core','core','{shoulders,glutes}','{none}',1,'{"Posterior tilt, breathe"}',null,'rollout',3.0),
('hollow-hold','Hollow Body Hold','core','core','{hip_flexors}','{none}',3,'{"Low back glued to floor"}','plank','rollout',4.0),
('rollout','Ab Wheel / Barbell Rollout','core','core','{lats}','{barbell,medicine_ball}',4,'{"No hip sag"}','plank',null,5.0),
('hanging-leg-raise','Hanging Leg Raise','core','core','{hip_flexors,grip}','{pull_up_bar}',4,'{"Posterior tilt at the top"}','hollow-hold',null,5.0),
('band-pull-apart','Band Pull-Apart','mobility','rear_delt','{traps}','{resistance_band}',1,'{"Straight arms, squeeze"}',null,null,3.0),
('face-pull','Face Pull','strength','rear_delt','{traps,rotator_cuff}','{cable_machine,resistance_band}',2,'{"Pull to forehead, thumbs back"}','band-pull-apart',null,4.0),
('burpee','Burpee','hiit','full_body','{quads,chest,core}','{none}',3,'{"Chest to floor, jump at top"}','squat-thrust',null,10.0),
('squat-thrust','Squat Thrust','hiit','full_body','{quads,core}','{none}',2,'{"Step back instead of jumping"}',null,'burpee',7.0),
('mountain-climber','Mountain Climber','hiit','core','{quads,shoulders}','{none}',2,'{"Hips low, quick feet"}',null,'burpee',8.0),
('jump-rope','Jump Rope','cardio','calves','{core,shoulders}','{jump_rope,none}',2,'{"Small hops, wrists turn the rope"}',null,null,11.0),
('rowing','Rowing Machine','cardio','back','{legs,core}','{rower}',2,'{"Legs, hips, arms - then reverse"}',null,null,8.5),
('treadmill-run','Treadmill Run','cardio','legs','{core}','{treadmill}',2,'{"Relaxed shoulders, cadence ~175"}',null,null,9.8),
('stationary-bike','Stationary Bike','cardio','quads','{glutes,calves}','{bike}',1,'{"Saddle height: slight knee bend"}',null,null,7.0),
('walking','Brisk Walk','cardio','legs','{core}','{none}',1,'{"Conversational pace"}',null,'treadmill-run',3.8),
('cat-cow','Cat-Cow','mobility','spine','{core}','{none}',1,'{"Move with the breath"}',null,null,2.3),
('worlds-greatest-stretch','World''s Greatest Stretch','mobility','hips','{thoracic,hamstrings}','{none}',2,'{"Elbow to instep, then rotate"}',null,null,3.0)
on conflict (slug) do nothing;
