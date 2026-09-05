-- ============================================================
-- NutriAI :: account-deletion root cause
--
-- One paste. Returns a table of facts, including the REAL Postgres error
-- behind GoTrue's generic "Database error deleting user".
--
-- The delete is attempted inside a subtransaction that always rolls back,
-- so nothing is destroyed either way.
-- ============================================================

create or replace function _nutriai_diag()
returns table(step text, finding text)
language plpgsql
security definer
as $func$
declare
  uid uuid;
  n_blocking int;
  rec record;
begin
  -- 1. did migration 0013 apply?
  step := '1. rollup guard (0013)';
  select case when count(*) = 2 then 'applied to both functions'
              else format('MISSING -- found %s of 2 guarded functions; re-run 0013', count(*))
         end
    into finding
    from pg_proc
   where proname in ('recompute_daily_summary','bump_streak')
     and prosrc like '%not exists (select 1 from profiles%';
  return next;

  -- 2. foreign keys to auth.users that would block a delete
  step := '2. blocking FKs -> auth.users';
  select count(*) into n_blocking
    from pg_constraint con
    join pg_class ref     on ref.oid = con.confrelid
    join pg_namespace rn  on rn.oid = ref.relnamespace
   where con.contype = 'f' and rn.nspname = 'auth' and ref.relname = 'users'
     and con.confdeltype in ('a','r');
  finding := case when n_blocking = 0 then 'none'
                  else format('%s constraint(s) with NO ACTION/RESTRICT', n_blocking) end;
  return next;

  for rec in
    select n.nspname||'.'||c.relname as tbl, con.conname,
           case con.confdeltype when 'a' then 'NO ACTION' else 'RESTRICT' end as act
      from pg_constraint con
      join pg_class c       on c.oid = con.conrelid
      join pg_namespace n   on n.oid = c.relnamespace
      join pg_class ref     on ref.oid = con.confrelid
      join pg_namespace rn  on rn.oid = ref.relnamespace
     where con.contype = 'f' and rn.nspname = 'auth' and ref.relname = 'users'
       and con.confdeltype in ('a','r')
  loop
    step := '   blocking';
    finding := format('%s . %s (%s)', rec.tbl, rec.conname, rec.act);
    return next;
  end loop;

  -- 3. triggers that fire during the cascade
  step := '3. AFTER DELETE triggers on public';
  select coalesce(string_agg(distinct c.relname, ', '), 'none')
    into finding
    from pg_trigger t
    join pg_class c on c.oid = t.tgrelid
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public' and not t.tgisinternal
     and (t.tgtype & 8) > 0;   -- DELETE
  return next;

  -- 4. a test account to try
  select id into uid from auth.users
   where email like 'nutriai.smoke%' or email like 'smoke+%'
   order by created_at desc limit 1;

  step := '4. test account';
  if uid is null then
    finding := 'none found -- run the smoke test once, then re-run this';
    return next;
    return;
  end if;
  finding := uid::text;
  return next;

  -- 5. THE ACTUAL ERROR, captured and rolled back
  step := '5. real error';
  begin
    delete from auth.users where id = uid;
    -- Deliberate abort: we only wanted to know whether it works.
    raise exception 'NUTRIAI_ROLLBACK';
  exception
    when others then
      if SQLERRM = 'NUTRIAI_ROLLBACK' then
        finding := 'DELETE SUCCEEDS -- the database is fine; the failure is in GoTrue or the API layer';
      else
        finding := format('%s  [SQLSTATE %s]', SQLERRM, SQLSTATE);
      end if;
  end;
  return next;
end
$func$;

select * from _nutriai_diag();
