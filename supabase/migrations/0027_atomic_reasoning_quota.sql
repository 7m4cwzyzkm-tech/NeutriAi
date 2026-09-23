-- ============================================================
-- NeutriAI :: 0027  meter the reasoning calls that had no counter at all
--
-- POST /recipes/{id}/adapt and POST /plans call a paid reasoning model
-- (ask_reasoning) gated by ProDep alone -- require_pro only checks
-- is_active, and under free_launch_mode it skips that check entirely for
-- everyone. Unlike meal/equipment scans (AiScanDep / consume_ai_scan,
-- 0026_atomic_scan_quota.sql), nothing has ever counted how many times a
-- user calls either of these routes in a day. This is that counter, built
-- the same way and for the same reason 0026 was: a naive read-modify-write
-- counter lets N concurrent requests cost one quota unit, and every one of
-- these calls is a real paid model call.
--
-- Reusing entitlements.quota_reset_on for both counters is deliberate, not
-- an oversight: both reset on the same daily cadence (midnight-crossing,
-- checked at first touch of a new day in app/deps.py's get_entitlement),
-- there is no product reason for scans and reasoning calls to roll over on
-- different days, and a second reset-date column would just be two dates
-- that must always agree.
--
-- Re-runnable.
-- ============================================================

alter table entitlements
  add column if not exists ai_reasoning_used_today integer not null default 0,
  add column if not exists ai_reasoning_quota integer not null default 2; -- free tier daily cap

create or replace function public.consume_ai_reasoning(
    p_user_id uuid,
    p_is_active boolean,
    p_pro_ceiling integer
)
returns table (used integer, quota integer, allowed boolean)
language plpgsql
security definer
set search_path = public
as $$
declare
    v_quota integer;
    v_used  integer;
    v_limit integer;
begin
    -- Same one-statement, one-lock shape as consume_ai_scan: `+ 1` is
    -- evaluated by Postgres against the row it is holding, not against a
    -- number this process read a moment ago.
    update public.entitlements
       set ai_reasoning_used_today = ai_reasoning_used_today + 1
     where user_id = p_user_id
    returning ai_reasoning_used_today, ai_reasoning_quota
      into v_used, v_quota;

    if not found then
        return query select 0, 0, false;
        return;
    end if;

    -- Same shape as consume_ai_scan's Pro ceiling: Pro (or, today,
    -- free_launch_mode) is not unmetered, it gets a generous ceiling as a
    -- backstop against a runaway bug or a script, not a real per-person
    -- limit.
    v_limit := case when p_is_active then p_pro_ceiling else v_quota end;

    if v_used > v_limit then
        -- Give the unit back. The increment happened first on purpose:
        -- doing it the other way round is the same race in a different
        -- order.
        update public.entitlements
           set ai_reasoning_used_today = ai_reasoning_used_today - 1
         where user_id = p_user_id;
        return query select v_used - 1, v_limit, false;
        return;
    end if;

    return query select v_used, v_limit, true;
end;
$$;

revoke all on function public.consume_ai_reasoning(uuid, boolean, integer) from public;
revoke all on function public.consume_ai_reasoning(uuid, boolean, integer) from anon;
revoke all on function public.consume_ai_reasoning(uuid, boolean, integer) from authenticated;
