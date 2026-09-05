-- ============================================================
-- NeutriAI :: 0005 hydration + intermittent fasting
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
