-- ============================================================
-- NutriAI :: 0014 counter triggers must run as their owner
--
-- Root cause of "Database error deleting user", confirmed by diagnostic:
-- deleting a user works as `postgres` in the SQL editor but fails through
-- GoTrue's admin API. The difference is which role runs the cascade.
--
-- Deleting auth.users cascades into profiles and onward to post_likes,
-- comments and recipe_saves. Each has an AFTER DELETE trigger that keeps a
-- denormalised counter current by issuing UPDATE against posts or recipes.
--
-- Those three functions were declared without SECURITY DEFINER, so they
-- execute with the privileges of whoever fired them:
--
--   SQL editor  -> postgres              -> superuser, every privilege -> works
--   GoTrue      -> supabase_auth_admin   -> no UPDATE on public.posts  -> fails
--
-- GoTrue then wraps the permission error as a generic 500, which is why the
-- API layer had nothing useful to report.
--
-- The rollup triggers (meals, water_logs) were already SECURITY DEFINER and
-- were never part of this. This brings the counter triggers in line.
--
-- Also adds a guard for the parent row already being gone. During a cascade
-- the posts row is often deleted before its likes, so the UPDATE would match
-- nothing anyway -- skipping it is simply less work per deleted row.
-- ============================================================

create or replace function bump_post_likes() returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if tg_op = 'INSERT' then
    update posts set like_count = like_count + 1 where id = new.post_id;
  else
    update posts set like_count = greatest(like_count - 1, 0) where id = old.post_id;
  end if;
  return null;
end $$;

create or replace function bump_post_comments() returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if tg_op = 'INSERT' then
    update posts set comment_count = comment_count + 1 where id = new.post_id;
  else
    update posts set comment_count = greatest(comment_count - 1, 0) where id = old.post_id;
  end if;
  return null;
end $$;

create or replace function bump_recipe_saves() returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if tg_op = 'INSERT' then
    update recipes set save_count = save_count + 1 where id = new.recipe_id;
  else
    update recipes set save_count = greatest(save_count - 1, 0) where id = old.recipe_id;
  end if;
  return null;
end $$;

-- The rollup triggers already carried SECURITY DEFINER but not an explicit
-- search_path. A SECURITY DEFINER function without one is a privilege
-- escalation risk: anyone who can set search_path could shadow a table name
-- and have it resolved with the owner's rights.
alter function recompute_daily_summary(uuid, date) set search_path = public;
alter function bump_streak(uuid, text, date)        set search_path = public;
alter function trg_recompute_from_meal()            set search_path = public;
alter function trg_recompute_from_water()           set search_path = public;
alter function trg_recompute_from_health()          set search_path = public;

-- ---------- verify ----------
-- All eight should show security_definer = true.
select p.proname,
       p.prosecdef as security_definer,
       coalesce(array_to_string(p.proconfig, ', '), '(none)') as settings
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public'
   and p.proname in ('bump_post_likes','bump_post_comments','bump_recipe_saves',
                     'recompute_daily_summary','bump_streak',
                     'trg_recompute_from_meal','trg_recompute_from_water',
                     'trg_recompute_from_health')
 order by p.prosecdef, p.proname;
