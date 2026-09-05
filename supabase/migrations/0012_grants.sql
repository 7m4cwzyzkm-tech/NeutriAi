-- ============================================================
-- NeutriAI :: 0012 role privileges
--
-- Supabase normally grants the API roles access to new tables in `public`
-- through ALTER DEFAULT PRIVILEGES. That did not happen for tables created by
-- pasting into the SQL editor, which surfaces as:
--
--     42501: permission denied for table exercises
--
-- Note this is a GRANT failure, not an RLS one. RLS returns zero rows; it
-- never says "permission denied". The two are easy to confuse and lead you to
-- debug policies that are working fine.
--
-- Privilege model here, deliberately narrower than the Supabase default:
--
--   service_role   full access. This is the backend's identity. It also holds
--                  BYPASSRLS, which is what lets trusted server code read
--                  across users after checking ownership itself.
--   authenticated  full DML, constrained by the RLS policies in 0011. The
--                  GRANT is what lets the policy be evaluated at all -- with no
--                  grant, the request dies before RLS is consulted.
--   anon           schema usage and function execute only. NO table grants.
--
-- That last one departs from the platform default, which grants anon SELECT on
-- public tables and relies on RLS alone. Every policy in 0011 targets
-- `authenticated`, so anon has no readable rows either way -- but this app
-- holds meal photos and health data, and a grant is the second lock. If
-- someone later disables RLS on a table while debugging, or writes a policy
-- `to public` by accident, the missing grant is what stops it leaking.
-- Sign-up and sign-in are unaffected: those run through GoTrue against the
-- `auth` schema, not `public`.
-- ============================================================

grant usage on schema public to anon, authenticated, service_role;

-- ---------- service_role: the backend ----------
grant all privileges on all tables    in schema public to service_role;
grant all privileges on all sequences in schema public to service_role;
grant all privileges on all functions in schema public to service_role;

-- ---------- authenticated: the mobile client, through RLS ----------
grant select, insert, update, delete on all tables    in schema public to authenticated;
grant usage, select                  on all sequences in schema public to authenticated;
grant execute                        on all functions in schema public to authenticated;

-- ---------- anon: no table access ----------
grant execute on all functions in schema public to anon;

-- ---------- future tables inherit the same shape ----------
alter default privileges in schema public
  grant all privileges on tables to service_role;
alter default privileges in schema public
  grant all privileges on sequences to service_role;
alter default privileges in schema public
  grant all privileges on functions to service_role;

alter default privileges in schema public
  grant select, insert, update, delete on tables to authenticated;
alter default privileges in schema public
  grant usage, select on sequences to authenticated;
alter default privileges in schema public
  grant execute on functions to authenticated;

alter default privileges in schema public
  grant execute on functions to anon;

-- ---------- verify ----------
-- Should list the same grants for service_role across all 42 tables.
-- select grantee, count(*) from information_schema.role_table_grants
--  where table_schema = 'public' and privilege_type = 'SELECT'
--  group by grantee order by grantee;
