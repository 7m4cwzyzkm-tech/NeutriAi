-- ============================================================
-- NeutriAI :: 0013 make the rollup triggers survive a cascade delete
--
-- Deleting an account failed with:
--     500 unexpected_failure "Database error deleting user"
--
-- The cause is a trigger fighting the cascade that fired it. Deleting a user
-- cascades auth.users -> profiles -> meals, and the AFTER DELETE trigger on
-- meals calls recompute_daily_summary(), which INSERTs a daily_summaries row
-- for that user. Its foreign key points at profiles, which is already gone
-- inside the same transaction, so the insert violates the constraint and takes
-- the whole delete down with it.
--
-- The same path exists for water_logs and health_days, all three routing
-- through this one function -- so one guard fixes all of them.
--
-- Guarding here rather than dropping the triggers keeps the summaries live for
-- every normal edit, which is what makes the dashboard a single-row read.
-- ============================================================

create or replace function recompute_daily_summary(p_user uuid, p_day date)
returns void language plpgsql security definer as $$
declare
  v record;
  v_water int;
  v_health record;
  v_workouts int;
  v_fast int;
  v_targets record;
  v_met text[] := '{}';
begin
  -- The profile is gone (or going) -- this is a cascade delete, not an edit.
  -- Re-inserting a summary row here would violate its FK and abort the whole
  -- transaction, which is exactly the account-deletion failure this fixes.
  if not exists (select 1 from profiles where id = p_user) then
    return;
  end if;

  select coalesce(sum(kcal),0) kcal, coalesce(sum(protein_g),0) p,
         coalesce(sum(carbs_g),0) c, coalesce(sum(fat_g),0) f,
         coalesce(sum(fiber_g),0) fi, coalesce(sum(sugar_g),0) s
    into v from meals where user_id = p_user and day = p_day;

  select coalesce(sum(amount_ml),0) into v_water
    from water_logs where user_id = p_user and day = p_day;

  select * into v_health from health_days
   where user_id = p_user and day = p_day
   order by case provider
     when 'apple_health' then 1 when 'garmin' then 2 when 'fitbit' then 3
     when 'google_fit' then 4 when 'samsung_health' then 5 else 6 end
   limit 1;

  select count(*) into v_workouts from workouts
   where user_id = p_user and started_at::date = p_day;

  select coalesce(sum(actual_minutes),0) into v_fast from fasts
   where user_id = p_user and started_at::date = p_day and status = 'completed';

  select * into v_targets from nutrition_targets
   where user_id = p_user and effective_from <= p_day
   order by effective_from desc limit 1;

  if v_targets.id is not null then
    if v.kcal between v_targets.target_kcal * 0.85 and v_targets.target_kcal * 1.05
      then v_met := array_append(v_met, 'calories'); end if;
    if v.p >= v_targets.protein_g * 0.9 then v_met := array_append(v_met, 'protein'); end if;
    if v_water >= v_targets.water_ml then v_met := array_append(v_met, 'water'); end if;
  end if;
  if v_workouts > 0 then v_met := array_append(v_met, 'workout'); end if;
  if coalesce(v_health.steps,0) >= 10000 then v_met := array_append(v_met, 'steps'); end if;

  insert into daily_summaries as d
    (user_id, day, kcal_in, protein_g, carbs_g, fat_g, fiber_g, sugar_g,
     kcal_out, steps, water_ml, workouts, fast_minutes, goals_met, updated_at)
  values
    (p_user, p_day, round(v.kcal), v.p, v.c, v.f, v.fi, v.s,
     coalesce(v_health.active_kcal,0) + coalesce(v_health.resting_kcal,0),
     coalesce(v_health.steps,0), v_water, v_workouts, v_fast, v_met, now())
  on conflict (user_id, day) do update set
     kcal_in = excluded.kcal_in, protein_g = excluded.protein_g,
     carbs_g = excluded.carbs_g, fat_g = excluded.fat_g,
     fiber_g = excluded.fiber_g, sugar_g = excluded.sugar_g,
     kcal_out = excluded.kcal_out, steps = excluded.steps,
     water_ml = excluded.water_ml, workouts = excluded.workouts,
     fast_minutes = excluded.fast_minutes, goals_met = excluded.goals_met,
     updated_at = now();
end $$;

-- bump_streak has the same exposure: it is called from application code today,
-- but a future trigger would hit the identical wall.
create or replace function bump_streak(p_user uuid, p_kind text, p_day date)
returns integer language plpgsql security definer as $$
declare v_cur int; v_last date; v_new int;
begin
  if not exists (select 1 from profiles where id = p_user) then
    return 0;
  end if;

  select current_len, last_day into v_cur, v_last
    from streaks where user_id = p_user and kind = p_kind;

  if v_last = p_day then
    return v_cur;
  elsif v_last = p_day - 1 then
    v_new := coalesce(v_cur,0) + 1;
  else
    v_new := 1;
  end if;

  insert into streaks (user_id, kind, current_len, best_len, last_day, updated_at)
  values (p_user, p_kind, v_new, v_new, p_day, now())
  on conflict (user_id, kind) do update set
    current_len = v_new,
    best_len = greatest(streaks.best_len, v_new),
    last_day = p_day, updated_at = now();
  return v_new;
end $$;
