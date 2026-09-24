-- ============================================================
-- 0029 -- another user's profile is name, handle, avatar and bio. Nothing else.
--
-- profiles_read (0011) decides which ROWS a signed-in user may see: their own,
-- any public profile, and private ones they follow. It cannot restrict
-- COLUMNS, and 0012 granted `authenticated` SELECT on the whole table -- so
-- for every row that rule lets through, anyone with a login token could ask
-- Supabase's REST API directly for sex, birth_date, height_cm, weight_kg,
-- target_weight_kg, is_tester and push_token (the last one lets a stranger
-- send push notifications to that person's phone). Nothing in the app asked
-- for them; the database did not stop a request that did. Testers include
-- strangers, so it has to.
--
-- THE FIX IS COLUMN PRIVILEGES, NOT A VIEW
--
-- The first design was a `profiles_public` view (security_invoker) plus
-- REVOKE SELECT on the table. Tested on Postgres 16 before writing this: a
-- security_invoker view checks the BASE TABLE's privileges as the caller, so
-- once SELECT is revoked the view itself fails ("permission denied for table
-- profiles"). A definer view would work only by copying profiles_read's row
-- logic into the view, a second copy of an access rule to keep in step.
--
-- Column privileges do exactly the job with one rule: `authenticated` keeps
-- SELECT on the five public columns only, and profiles_read keeps deciding
-- the rows. Verified on a local Postgres 16 with stand-in roles:
--   * another user's row, select * / weight_kg / is_tester / push_token /
--     birth_date  -> permission denied        (before: all returned)
--   * id, handle, display_name, avatar_url, bio -> returned for the rows
--     profiles_read allows; a private, unfollowed account stays invisible;
--     a follower sees its public columns and still not its private ones
--   * the feed/comment/notification embeds and the follow/search lookups the
--     API makes (they already ask for these columns only) -> unchanged
--   * service_role -> unaffected, reads and writes every column
--
-- WHAT THIS MEANS FOR THE CLIENT ROLE ON ITS OWN ROW
--
-- Privileges are per role, not per row, so `authenticated` can no longer
-- read its OWN private columns directly either. The API reads the caller's
-- own profile with the service role, filtered to the verified user id (as it
-- already did in identity.ensure_profile). UPDATE/INSERT grants are
-- unchanged: an update filtered on id still works, it just cannot ask for the
-- row back (RETURNING needs SELECT), so the API re-reads it separately.
--
-- anon has no table grants at all (0012), so there is nothing to revoke.
-- is_tester's write protection (0028's trigger) is untouched.
-- ============================================================

revoke select on profiles from authenticated;

grant select (id, handle, display_name, avatar_url, bio) on profiles to authenticated;

comment on table profiles is
  'One row per user. Signed-in clients may SELECT only id, handle, '
  'display_name, avatar_url and bio (0029), on the rows profiles_read allows; '
  'everything else is read by the API with the service role.';
