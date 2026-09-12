-- ============================================================
-- NeutriAI :: 0015  allow the vessel_reference estimation method
--
-- The portion estimator gained a rung: when the vision model names the vessel
-- (bowl, takeout box, tray, cutting board) we scale from that vessel's typical
-- size instead of assuming every surface is a 270 mm dinner plate. A takeout
-- clamshell read as a dinner plate inflates every gram by roughly double, and
-- most photographed food is not on a dinner plate.
--
-- The new method name was added in Python but not here, so every scan that
-- used it failed on insert:
--
--   23514  new row for relation "meal_items" violates check constraint
--          "meal_items_estimation_method_check"
--
-- which reached the user as a bare 500 on a photo that had actually been
-- analysed correctly. The lesson is narrow and worth stating: this column is a
-- closed enum shared between the application and the database, so a new value
-- is a schema change, not a code change.
-- ============================================================

alter table meal_items
  drop constraint if exists meal_items_estimation_method_check;

alter table meal_items
  add constraint meal_items_estimation_method_check
  check (estimation_method in (
    'pixel_area',
    'plate_reference',
    'vessel_reference',   -- new: scale from an identified vessel
    'depth_model',
    'multi_image',
    'ai_prior',
    'user_entered',
    'barcode'
  ));
