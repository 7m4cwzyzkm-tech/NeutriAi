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
