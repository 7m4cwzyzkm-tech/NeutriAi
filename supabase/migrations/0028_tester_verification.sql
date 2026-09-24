-- ============================================================
-- 0028 -- tester weighed-verification
--
-- Testers weigh each food on a kitchen scale before plating it, scan it, and
-- then either confirm the scan matched their scale ("matched") or type the
-- real weight ("corrected"). This records, for the first time, which checks
-- were made against a scale and what the app predicted versus what the scale
-- said, so the real bias across testers can be computed later.
--
-- It changes nothing about how the app learns. Weighting a weighed correction
-- above a typed one is a separate, later decision.
-- ============================================================

-- ------------------------------------------------------------------
-- profiles.is_tester
--
-- Set by hand in the Supabase table editor for a handful of invited people.
-- There is no endpoint, screen or ProfileIn field that sets it, and there must
-- not be.
-- ------------------------------------------------------------------
alter table profiles
  add column if not exists is_tester boolean not null default false;

comment on column profiles.is_tester is
  'Invited weighed-verification tester. Set manually in Supabase; never by the '
  'user. Guarded by profiles_protect_is_tester.';

-- profiles_self_write (0011) lets a signed-in user update ANY column of their
-- own row straight through the REST API with their own JWT, and
-- profiles_self_insert lets them insert it -- so without this, anyone could
-- make themselves a tester without ever touching our API. A column-level
-- REVOKE does not help while the table-level UPDATE grant stands, so the guard
-- is a trigger: for the client roles the value is pinned (false on insert,
-- unchanged on update). The service role and the table editor are unaffected.
create or replace function profiles_protect_is_tester() returns trigger
language plpgsql as $$
begin
  if current_user in ('authenticated', 'anon') then
    if tg_op = 'INSERT' then
      new.is_tester := false;
    else
      new.is_tester := old.is_tester;
    end if;
  end if;
  return new;
end $$;

drop trigger if exists t_profiles_protect_is_tester on profiles;
create trigger t_profiles_protect_is_tester
  before insert or update on profiles
  for each row execute function profiles_protect_is_tester();

-- ------------------------------------------------------------------
-- scan_accuracy_checks
--
-- One row per food item per tester check. "matched": the tester's scale agreed
-- with the scan, so predicted_grams = actual_grams. "corrected": it did not,
-- and actual_grams is what the scale said. The learning-inventory "prediction
-- check within bias target" (docs/design/learning-db-inventory-2026-09-21.md,
-- row 5c) is computed from these rows; nothing recorded it before.
--
-- meal_id and scan_id are SET NULL on delete rather than cascading: a tester
-- deleting a meal later must not delete the measurement the bias figure rests
-- on.
-- ------------------------------------------------------------------
create table if not exists scan_accuracy_checks (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references profiles(id) on delete cascade,
  scan_id          uuid references food_scans(id) on delete set null,
  meal_id          uuid references meals(id) on delete set null,
  checked_at       timestamptz not null default now(),
  outcome          text not null check (outcome in ('matched', 'corrected')),
  item_name        text not null,
  predicted_grams  numeric(8,2) not null check (predicted_grams >= 0),
  actual_grams     numeric(8,2) not null check (actual_grams >= 0)
);

create index if not exists scan_accuracy_checks_user_idx
  on scan_accuracy_checks(user_id, checked_at desc);
create index if not exists scan_accuracy_checks_meal_idx
  on scan_accuracy_checks(meal_id);

comment on table scan_accuracy_checks is
  'Tester weighed-verification: what the scan predicted per item versus what '
  'the tester''s kitchen scale read. Written only by the API (service role), '
  'which checks profiles.is_tester first.';

-- ------------------------------------------------------------------
-- Row Level Security -- the house deny-all-then-grant posture (0011, 0022).
--
-- Owners may READ their own rows. They may not write them: the API writes
-- with the service role after checking is_tester, and an owner-insert policy
-- would let any signed-in user post "checks" straight to the REST API,
-- skipping that gate and polluting the bias figure. (vessel_observations uses
-- `for all`; this table deliberately narrows it to select.)
-- ------------------------------------------------------------------
alter table scan_accuracy_checks enable row level security;
alter table scan_accuracy_checks force row level security;

drop policy if exists scan_accuracy_checks_own_read on scan_accuracy_checks;
create policy scan_accuracy_checks_own_read on scan_accuracy_checks
  for select to authenticated
  using (user_id = auth.uid());
