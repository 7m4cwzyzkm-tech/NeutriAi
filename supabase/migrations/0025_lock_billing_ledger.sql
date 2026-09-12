-- ============================================================
-- NeutriAI :: 0025  the billing ledger is not public
--
-- `stripe_events` was created in 0008 and never appears in 0011, the migration
-- that turns row-level security on. 0012 then grants select, insert, update and
-- delete on every table in the schema to `authenticated`. With no RLS, those
-- grants are the whole story.
--
-- Reproduced against PostgreSQL 16 with all migrations applied, as an ordinary
-- signed-in user:
--
--     select payload -> 'data' -> 'object' ->> 'customer_email'
--       from stripe_events;              -> another customer's email address
--     delete from stripe_events;         -> DELETE 1
--
-- The payload column holds whole Stripe webhook bodies: emails, customer and
-- subscription ids, amounts. And deleting rows does a second kind of damage --
-- the table IS the idempotency ledger, so emptying it re-opens replay of every
-- event it was holding.
--
-- Nobody should reach this table but the service role, which bypasses RLS.
-- There is no user-facing read of it anywhere in the app.
--
-- WHY THE WIRING TEST DID NOT CATCH IT
--
-- `test_every_table_holding_a_users_data_has_row_level_security` only examines
-- tables whose CREATE TABLE body literally contains "user_id". stripe_events
-- keys on the Stripe event id, so it was never looked at. Twelve other tables
-- were skipped the same way. That test is fixed separately.
--
-- Re-runnable, unlike 0011.
-- ============================================================

alter table public.stripe_events enable row level security;

-- No policy is deliberate, and it is not an oversight to be corrected later.
-- RLS with no policy denies every row to every role except the service role and
-- the table owner. That is exactly right here: the app writes this table with
-- the service key and nothing else has any business reading it.
drop policy if exists stripe_events_own on public.stripe_events;

-- Belt and braces: take back the grants 0012 handed out wholesale, so the table
-- is closed even if RLS is ever disabled by a later migration doing the same
-- kind of sweep 0012 did.
revoke all on public.stripe_events from authenticated;
revoke all on public.stripe_events from anon;
