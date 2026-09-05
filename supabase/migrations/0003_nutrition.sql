-- ============================================================
-- NeutriAI :: 0003 food scans, meals, nutrition cache
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
