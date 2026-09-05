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
