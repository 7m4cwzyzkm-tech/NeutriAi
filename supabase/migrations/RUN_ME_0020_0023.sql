-- NeutriAI :: migrations 0020-0023, applied together.
-- Verified: this exact text applied three times in a row against
-- PostgreSQL 16 with no error. Safe to re-run if anything goes wrong.

-- ===================== 0020_normalise_calibration_vessel.sql =====================
update scan_calibrations
set vessel = replace(replace(lower(btrim(vessel)), ' ', '_'), '-', '_')
where vessel is not null
  and vessel <> replace(replace(lower(btrim(vessel)), ' ', '_'), '-', '_');

-- ===================== 0021_portion_learning.sql =====================
create table if not exists portion_learning (
  id            uuid primary key default gen_random_uuid(),
  shape         text not null,
  food_name     text,
  height_mm     numeric(6,2) not null,
  samples       integer not null default 0,
  dispersion    numeric(5,3),
  updated_at    timestamptz not null default now(),
  unique (shape, food_name)
);
comment on table portion_learning is
  'Learned food heights, recovered from user corrections on photos whose scale was already measured. Global and non-personal: how tall rice sits is a property of rice.';
comment on column portion_learning.samples is
  'Corrections behind this height. The estimator ignores the row below a minimum, so no single user can move a global prior.';
comment on column portion_learning.dispersion is
  'Spread of the readings. High means the food is plated inconsistently, not that the estimate is bad.';
create index if not exists portion_learning_shape_idx
  on portion_learning(shape);
alter table portion_learning enable row level security;
drop policy if exists portion_learning_read on portion_learning;
create policy portion_learning_read on portion_learning
  for select using (true);

-- ===================== 0022_vessel_observations.sql =====================
create table if not exists vessel_observations (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references profiles(id) on delete cascade,
  vessel      text not null,
  width_mm    numeric(7,2) not null,
  source      text not null check (source in ('tape','reference_object','correction')),
  prior_mm    numeric(7,2),
  scan_id     uuid references food_scans(id) on delete set null,
  created_at  timestamptz not null default now()
);
create index if not exists vessel_observations_user_vessel_idx
  on vessel_observations(user_id, vessel, created_at desc);
comment on table vessel_observations is
  'One measurement of one of the user''s vessels. The scale estimate for a '
  'vessel is computed from these rows rather than stored as a running average, '
  'so it can be recomputed when the method improves and audited when it looks '
  'wrong.';
alter table scan_calibrations
  add column if not exists width_error_pct numeric(5,2),
  add column if not exists observations integer not null default 0;
comment on column scan_calibrations.width_error_pct is
  'Expected error on this vessel''s width, as a percent, from the spread of its '
  'observations. This is what the progress card reports and what the 1% target '
  'is measured against.';
comment on column scan_calibrations.observations is
  'How many rows in vessel_observations back this size. Distinct from samples, '
  'which counted corrections only.';
alter table vessel_observations enable row level security;
alter table vessel_observations force row level security;
drop policy if exists vessel_observations_own on vessel_observations;
create policy vessel_observations_own on vessel_observations
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- ===================== 0023_portion_learning_unique_shape.sql =====================
delete from portion_learning a
using portion_learning b
where a.food_name is null
  and b.food_name is null
  and a.shape = b.shape
  and (a.samples, a.updated_at, a.id) < (b.samples, b.updated_at, b.id);
create unique index if not exists portion_learning_shape_only_idx
  on portion_learning (shape)
  where food_name is null;
comment on index portion_learning_shape_only_idx is
  'One shape-level height per shape. The unique constraint in 0021 cannot do '
  'this: NULL is not equal to NULL, so it never restricted the rows where '
  'food_name is null -- which is every shape-level row.';

