-- ============================================================
-- NeutriAI :: 0022  measure the user's own crockery, from their own photos
--
-- THE TARGET
--
-- Scale is a multiplier on every gram the app ever reports. Get the vessel's
-- real size wrong by 10% in width and every portion off it is wrong by 21% in
-- area, before the food has even been identified. So scale is the one term
-- worth driving to 1%, and unlike the rest of the pipeline, 1% is reachable --
-- because a dinner plate has a fixed size and only has to be measured once.
--
-- WHY THIS TABLE EXISTS
--
-- Two things were already true and were never connected:
--
--   * A credit card is 85.60 mm on its long edge, the same for every bank on
--     earth, and reference_cv finds one in the pixels to about 1%.
--   * The vision model reports how wide the plate looks as a fraction of the
--     frame.
--
-- Multiply them and you have measured the user's plate. No tape, no typing, no
-- extra step -- from a photo they were taking anyway. Every photo with a card
-- and a plate in it is one free measurement of that plate.
--
-- WHY IT NEEDS MANY SAMPLES RATHER THAN ONE
--
-- The card side is precise. The plate side is not: the model returns its
-- geometry on a 0.05 grid -- measured, 100% of values on the weighed bench sat
-- exactly on it, where chance would put 20% -- so a plate that looks 0.60 of
-- the frame wide is reported as 0.60 whether it is 0.58 or 0.62. That is about
-- +/-4% from one photo, and no cleverness recovers it from that photo.
--
-- Across photos it does recover, because the rounding lands in different
-- directions as the camera moves. The error of the mean falls as 1/sqrt(n):
--
--     photos of the same plate      1      4      9     16     25
--     expected error on its width  4.2%   2.1%   1.4%   1.1%   0.8%
--
-- Roughly twenty photos of one plate gets under 1%. A person photographing
-- their meals for three weeks produces that without being asked for anything.
-- This is what the free trial is FOR: not a trial of the app, a period in which
-- the app measures the user's kitchen.
--
-- WHY OBSERVATIONS AND NOT A RUNNING AVERAGE
--
-- A running average cannot be audited, cannot be recomputed when the method
-- improves, and cannot tell an honest confidence from a lucky one. Rows can.
-- They also let a bad stretch be deleted without destroying the good history,
-- and they let the app show the user WHY it believes what it believes.
-- ============================================================

create table if not exists vessel_observations (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references profiles(id) on delete cascade,
  -- Which vessel this measures: dinner_plate, bowl, takeout_box... Matched
  -- against the container the vision model reports, the same key
  -- scan_calibrations.vessel uses.
  vessel      text not null,
  -- Its real width across, in millimetres. Width and not area: the model
  -- reports a linear fraction, the card gives a linear scale, and squaring
  -- early would square the error with it.
  width_mm    numeric(7,2) not null,
  -- How this observation was obtained. Precision differs by an order of
  -- magnitude between them, so the estimator weights by source rather than
  -- treating every row alike.
  --   tape             the user measured it. Best available, ~0.5%.
  --   reference_object a card found in the same photo. ~4% each, averages down.
  --   correction       inferred from the user changing a portion. Weakest;
  --                    kept because it is the only signal on photos with
  --                    neither a card nor a tape measurement.
  source      text not null check (source in ('tape','reference_object','correction')),
  -- What the app thought the width was when this observation was made, so a
  -- systematic drift can be seen rather than averaged away silently.
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

-- How good the current answer is, cached onto the calibration so the scan path
-- does not recompute it per photo. Written by scale_learning; never by hand.
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

-- ------------------------------------------------------------------
-- Row Level Security
--
-- Added after an audit found this table was the ONLY one carrying a user_id
-- without it -- 31 tables have one, 30 were covered, this was written later and
-- missed the list in 0011.
--
-- It matters more than the data suggests. With RLS off, Postgres does not deny
-- by default: Supabase grants the `anon` and `authenticated` roles privileges on
-- public tables, so any signed-in user could have read every other user's rows
-- with the public key. The contents are plate widths rather than anything
-- sensitive, but the posture 0011 set is deny-all-then-grant, and a table that
-- opts out of it silently is how the first real leak happens.
--
-- Written to be safe whether or not the rest of this file has already been
-- applied: enabling RLS twice is a no-op, and the policy is dropped before it is
-- created because Postgres has no CREATE POLICY IF NOT EXISTS.
-- ------------------------------------------------------------------

alter table vessel_observations enable row level security;
alter table vessel_observations force row level security;

drop policy if exists vessel_observations_own on vessel_observations;
create policy vessel_observations_own on vessel_observations
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());
