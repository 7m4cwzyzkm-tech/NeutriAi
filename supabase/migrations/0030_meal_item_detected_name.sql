-- ============================================================
-- NeutriAI :: 0030  what the camera called a food, kept beside what we logged
--
-- meal_items.name is the NUTRITION DATABASE's name for the row it matched --
-- "Scallops, grilled", "Mollusks, scallop, (bay and sea), cooked, steamed" --
-- not the word the vision model used. So when someone renamed a food, 0024's
-- food_aliases stored the database's wording as `described_as`, and the next
-- scan, which compares against the MODEL's wording, did not recognise it:
-- "scallops, grilled" and "scallops" share one identity word, and one is not
-- enough (food_identity.MIN_SHARED_WORDS). The correction was kept and never
-- found again.
--
-- detected_name is the model's own name for the item, after any rename the
-- person had already taught it -- the exact string food_identity.apply_to()
-- compares against on the next scan. food_identity.learn_from_correction()
-- prefers it and falls back to `name`.
--
-- Nullable, no backfill. Rows written before this migration have no recorded
-- raw name, and the fallback handles them exactly as before; there is no way
-- to recover what the model said for them.
--
-- Re-runnable.
-- ============================================================

alter table public.meal_items
    add column if not exists detected_name text;
