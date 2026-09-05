-- ============================================================
-- Why is deleting a user failing?
--
-- GoTrue wraps every database failure as "Database error deleting user",
-- which tells us nothing. Running the delete directly in the SQL editor
-- surfaces the real Postgres error: the constraint name, the trigger, the
-- table. Run these one block at a time.
-- ============================================================

-- ---------- 1. did migration 0013 actually apply? ----------
select
  proname,
  prosrc like '%not exists (select 1 from profiles%' as guard_present
from pg_proc
where proname in ('recompute_daily_summary', 'bump_streak');
-- Both rows must show guard_present = true.
-- If false, 0013 did not apply -- re-run it before anything else.


-- ---------- 2. what leftover test accounts exist? ----------
select id, email, created_at
from auth.users
where email like 'neutriai.smoke%' or email like 'smoke+%'
order by created_at desc;


-- ---------- 3. everything that points at auth.users ----------
-- A foreign key without ON DELETE CASCADE or SET NULL blocks the delete.
-- Look for confdeltype = 'a' (NO ACTION) or 'r' (RESTRICT) -- those are the
-- ones that would stop it. 'c' = cascade, 'n' = set null, both fine.
select
  n.nspname   as schema,
  c.relname   as table_name,
  con.conname as constraint_name,
  case con.confdeltype
    when 'a' then 'NO ACTION  <-- blocks'
    when 'r' then 'RESTRICT   <-- blocks'
    when 'c' then 'cascade'
    when 'n' then 'set null'
    when 'd' then 'set default'
  end as on_delete
from pg_constraint con
join pg_class c      on c.oid = con.conrelid
join pg_namespace n  on n.oid = c.relnamespace
join pg_class ref    on ref.oid = con.confrelid
join pg_namespace rn on rn.oid = ref.relnamespace
where con.contype = 'f'
  and rn.nspname = 'auth' and ref.relname = 'users'
order by on_delete desc, schema, table_name;


-- ---------- 4. THE ACTUAL ERROR ----------
-- Deletes one leftover test account. Postgres will report the real reason
-- instead of GoTrue's generic wrapper. Copy whatever error comes back.
delete from auth.users
where id = (
  select id from auth.users
  where email like 'neutriai.smoke%' or email like 'smoke+%'
  order by created_at desc
  limit 1
);


-- ---------- 5. if step 4 succeeded, clean up the rest ----------
-- delete from auth.users
--  where email like 'neutriai.smoke%' or email like 'smoke+%';
