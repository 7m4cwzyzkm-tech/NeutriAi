-- ============================================================
-- NeutriAI :: 0023  one learned height per shape, actually enforced
--
-- 0021 declares `unique (shape, food_name)` and that constraint does NOT do
-- what it looks like it does.
--
-- In Postgres a NULL is never equal to another NULL, so a unique constraint
-- containing a nullable column does not restrict rows where that column is
-- null. The shape-level rows -- the ones every food falls back to -- are
-- exactly the rows with food_name null. Verified against a real Postgres 16:
--
--     insert (mound, null, 24.0, 12)   -> accepted
--     insert (mound, null, 40.0, 12)   -> ALSO accepted
--     select count(*) where shape='mound' and food_name is null  ->  2
--
--     insert (mound, 'mexican rice', ...) twice -> correctly rejected
--
-- So the named-food half of the constraint works and the shape half never did.
--
-- WHY IT MATTERS MORE THAN A DUPLICATE ROW USUALLY WOULD
--
-- `learned_heights()` reads every row into a dict keyed by (shape, food_name).
-- With two rows for one shape, the last one read wins -- and row order in
-- Postgres is not guaranteed. The height applied to every mound-shaped food on
-- the plate could differ between two scans of the same photo, with nothing in
-- the output saying why. That is a silent, non-deterministic change to the
-- WEIGHT of the food, which is the one thing this pipeline must not do.
--
-- Two fixes, because either alone is incomplete: the index stops new duplicates,
-- and the estimator now picks the best-evidenced row deterministically so
-- existing data cannot bite either.
-- ============================================================

-- Any duplicates that already exist: keep the row resting on the most
-- corrections, then the most recently updated. Deleting the others loses no
-- learning -- the survivor is the one the most evidence went into.
delete from portion_learning a
using portion_learning b
where a.food_name is null
  and b.food_name is null
  and a.shape = b.shape
  and (a.samples, a.updated_at, a.id) < (b.samples, b.updated_at, b.id);

-- The constraint 0021 meant to write. A partial index is how a uniqueness rule
-- over nulls is expressed in Postgres.
create unique index if not exists portion_learning_shape_only_idx
  on portion_learning (shape)
  where food_name is null;

comment on index portion_learning_shape_only_idx is
  'One shape-level height per shape. The unique constraint in 0021 cannot do '
  'this: NULL is not equal to NULL, so it never restricted the rows where '
  'food_name is null -- which is every shape-level row.';
