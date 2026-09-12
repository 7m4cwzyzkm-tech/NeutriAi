-- ============================================================
-- NeutriAI :: 0019  allow the reference_object estimation method
--
-- The portion estimator gained another rung: an everyday object of known real
-- size lying in the photo -- a credit card, a fork, a soda can -- gives the
-- frame a scale even when the food is on butcher paper, in a takeout
-- container, or on a bare table. Those are the photos nothing else could size,
-- and they were the two worst measured errors on the bench (-31.7% and -35.4%).
--
-- This is the same schema change 0015 was, for the same reason, and it is
-- written in the same commit as the Python that produces the value -- because
-- last time the two were separated and every scan using the new rung died on
-- insert with 23514, reaching the user as a bare 500 on a photo that had been
-- analysed perfectly well.
--
-- `estimation_method` is a closed enum shared between the application and the
-- database. A new value is a schema change, not a code change.
-- ============================================================

alter table meal_items
  drop constraint if exists meal_items_estimation_method_check;

alter table meal_items
  add constraint meal_items_estimation_method_check
  check (estimation_method in (
    'pixel_area',
    'plate_reference',
    'vessel_reference',
    'reference_object',   -- new: scale from a known-size object in the frame
    'depth_model',
    'multi_image',
    'ai_prior',
    'user_entered',
    'barcode'
  ));
