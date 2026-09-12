-- ============================================================
-- NeutriAI :: 0026  the scan counter cannot be raced
--
-- `consume_ai_scan` read the count, added one, and wrote it back. Two scans
-- arriving together both read the same number and both write one more than it,
-- so N concurrent requests cost one quota unit. A free user gets unlimited
-- scans by sending them at once, and every one of those is a paid GPT-4o call.
--
-- Postgres can do this correctly in a single statement. The function runs with
-- the definer's rights so the check cannot be skipped by a client that has
-- learned to update the row directly, and it returns the state the caller needs
-- rather than requiring a second read.
--
-- Re-runnable.
-- ============================================================

create or replace function public.consume_ai_scan(
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
    -- One statement, one lock. `used + 1` is evaluated by Postgres against the
    -- row it is holding, not against a number this process read a moment ago.
    update public.entitlements
       set ai_scans_used_today = ai_scans_used_today + 1
     where user_id = p_user_id
    returning ai_scans_used_today, ai_scans_quota
      into v_used, v_quota;

    if not found then
        return query select 0, 0, false;
        return;
    end if;

    -- Pro is not unmetered. It was, and that is a subscription-priced hole:
    -- one account, unlimited vision calls, at a per-scan cost the subscription
    -- does not cover. The ceiling is high enough that no real person meets it
    -- and low enough that a script does.
    v_limit := case when p_is_active then p_pro_ceiling else v_quota end;

    if v_used > v_limit then
        -- Give the unit back. The increment happened first on purpose: doing
        -- it the other way round is the same race in a different order.
        update public.entitlements
           set ai_scans_used_today = ai_scans_used_today - 1
         where user_id = p_user_id;
        return query select v_used - 1, v_limit, false;
        return;
    end if;

    return query select v_used, v_limit, true;
end;
$$;

revoke all on function public.consume_ai_scan(uuid, boolean, integer) from public;
revoke all on function public.consume_ai_scan(uuid, boolean, integer) from anon;
revoke all on function public.consume_ai_scan(uuid, boolean, integer) from authenticated;
