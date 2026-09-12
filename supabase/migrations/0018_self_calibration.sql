-- ============================================================
-- NeutriAI :: 0018  learn vessel sizes from user corrections
--
-- Measured across six weighed meals: a vessel the user had measured gave 7%
-- per-item error; vessels the app guessed at gave -24% to -54%. Calibration is
-- the single biggest accuracy lever left.
--
-- But asking people to measure their bowls does not scale. Most will not, and
-- an app that is only accurate for those who do is inaccurate for almost
-- everyone.
--
-- A correction is already a measurement. When someone changes 150 g to 200 g,
-- and we know which vessel was in the photo, the vessel size can be recovered:
-- grams scale with vessel area, so the width was wrong by sqrt(200/150). Store
-- that, and every later photo of that vessel is calibrated -- with no extra
-- work from the user, from data they were giving anyway.
--
-- Two columns are needed:
--   food_scans.vessel          what was in the photo, so a correction can be
--                              attributed to the right bowl or plate
--   scan_calibrations.samples  how many corrections a size rests on, so a
--                              measured value is not overwritten by one odd
--                              correction, and so the estimator can say how
--                              much to trust it
-- ============================================================

alter table food_scans
  add column if not exists vessel text;

alter table scan_calibrations
  add column if not exists samples integer not null default 0,
  add column if not exists learned boolean not null default false;

comment on column food_scans.vessel is
  'The container the vision model reported for this scan. Lets a later correction be attributed to the right vessel.';
comment on column scan_calibrations.samples is
  'How many user corrections this size is based on. 0 = the user measured it directly, which always wins.';
comment on column scan_calibrations.learned is
  'True when this size was inferred from corrections rather than measured with a tape.';
