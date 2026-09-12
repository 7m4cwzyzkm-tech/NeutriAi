-- ============================================================
-- NeutriAI :: 0017  match a saved calibration to the vessel in the photo
--
-- Measurement, not theory. Across six weighed meals:
--
--   measured plate      per-item error  7.1%
--   guessed bowl        meal error    -23.7%
--   guessed takeout box meal error    -53.8%
--
-- The vessel widths in the code are priors I reasoned out, and they are wrong
-- by the amount those errors describe -- the bowl is nearer 217 mm than the
-- 190 assumed, the container nearer 280 mm. A prior cannot be fixed centrally
-- either, because one person's soup bowl is not another's.
--
-- The app already lets a user measure a vessel, but the scan only ever read
-- ONE calibration -- the default. Someone who measured a plate AND a bowl got
-- the plate's diameter applied to their soup, which is worse than the generic
-- prior it replaced.
--
-- This lets a calibration say which vessel it describes, so a photographed
-- bowl uses the measured bowl and a photographed plate uses the measured
-- plate. Null keeps the old behaviour: a general default.
-- ============================================================

alter table scan_calibrations
  add column if not exists vessel text;

comment on column scan_calibrations.vessel is
  'Which vessel this measurement describes (dinner_plate, bowl, takeout_box, tray...). '
  'Matched against the container the vision model reports. Null = a general default.';

create index if not exists scan_calibrations_user_vessel_idx
  on scan_calibrations(user_id, vessel);
