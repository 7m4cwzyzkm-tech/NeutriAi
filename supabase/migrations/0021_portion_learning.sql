-- NeutriAI :: 0021  learn how tall food actually is, from what users correct
--
-- WHY
-- ---
-- 0018 learns VESSEL SIZE from corrections, and only from photos where a vessel
-- set the scale. That leaves the most informative corrections on the floor:
-- the ones on photos where a credit card or a measured plate already fixed the
-- scale. There, the scale is right, so a correction means the food stood taller
-- or shorter than the estimator assumed -- and height is the largest remaining
-- guess in the whole pipeline.
--
-- The arithmetic is simpler than the vessel case. Grams are linear in height:
--
--     corrected / estimated = true_height / assumed_height
--
-- No square root, no area. One correction on a card-scaled photo is a direct
-- reading of how tall that food really was.
--
-- WHAT IT IS WORTH
-- ----------------
-- Done by hand on three weighed plates, this recovered heights that the shipped
-- priors had badly wrong, and the same food solved to the same height in every
-- photo (9-15% spread):
--
--     chicken drumstick   35, 38, 35 mm      prior said 40
--     mexican rice        22, 21, 24 mm      prior said 32
--     refried beans       29, 22, 23 mm      prior said 32
--
-- Rice is the headline: a mound of rice is nothing like 32 mm tall, and that one
-- number is most of why spread foods have read heavy. Validated leave-one-out
-- -- heights solved on two plates, used to predict the third -- it gave 10.3%
-- mean absolute against 22.4% for the shipped estimator on the same bench.
--
-- This table is that measurement, running continuously on real meals instead of
-- once by hand on three.
--
-- WHY IT IS GLOBAL, NOT PER USER
-- ------------------------------
-- How tall a mound of rice sits is a property of rice, not of the person eating
-- it. A vessel belongs to its owner; a food's shape does not. So this table has
-- no user_id -- which also means it is never personal data, and nothing here
-- can identify anybody.

create table if not exists portion_learning (
  id            uuid primary key default gen_random_uuid(),
  -- The shape class the estimator assigns (mound, chunky, flat, ...). Always
  -- present; this is the level that generalises.
  shape         text not null,
  -- A specific food, when there is enough evidence for one. Null means this row
  -- is the shape-level answer, which is the fallback for every food that has
  -- not earned its own.
  food_name     text,
  -- Effective height in mm, learned. Compared against HEIGHT_PRIORS_MM.
  height_mm     numeric(6,2) not null,
  -- How many corrections it rests on. The estimator ignores a row until this
  -- clears a floor, so one opinionated user cannot move anything.
  samples       integer not null default 0,
  -- Spread of the underlying readings, as a fraction. A row whose samples
  -- disagree wildly is describing a food nobody plates the same way twice, and
  -- the estimator should trust it less than one that is tight.
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

-- Read by the estimator on every scan; written only by the service role.
--
-- Every statement in this file is written to survive being run twice. Somebody
-- applying these by hand in the SQL editor WILL re-run one, and a migration
-- that fails halfway on the second attempt leaves a schema nobody planned --
-- which is worse than either outcome it was choosing between.
alter table portion_learning enable row level security;

drop policy if exists portion_learning_read on portion_learning;
create policy portion_learning_read on portion_learning
  for select using (true);
