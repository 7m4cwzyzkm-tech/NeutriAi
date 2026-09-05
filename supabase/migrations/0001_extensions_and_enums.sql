-- ============================================================
-- NeutriAI :: 0001 extensions, enums, shared helpers
-- ============================================================
create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";
create extension if not exists "citext";
create extension if not exists "pg_trgm";

-- ---------- enums ----------
create type sex_t              as enum ('male','female','other');
create type activity_level_t   as enum ('sedentary','light','moderate','active','very_active','athlete');
create type goal_t             as enum ('lose','maintain','gain','recomp');
create type diet_mode_t        as enum ('balanced','low_carb','keto','high_protein','athlete','vegetarian','vegan','pescatarian','paleo','mediterranean');
create type meal_slot_t        as enum ('breakfast','lunch','dinner','snack','pre_workout','post_workout');
create type unit_system_t      as enum ('metric','imperial');
create type confidence_t       as enum ('low','medium','high');
create type subscription_status_t as enum ('trialing','active','past_due','canceled','incomplete','incomplete_expired','unpaid','paused');
create type plan_interval_t    as enum ('month','year');
create type provider_t         as enum ('apple_health','fitbit','garmin','google_fit','samsung_health','manual');
create type fast_protocol_t    as enum ('16:8','18:6','20:4','omad','5:2','custom');
create type fast_status_t      as enum ('active','completed','broken','abandoned');
create type equipment_t        as enum (
  'none','dumbbell','kettlebell','barbell','resistance_band','bench','squat_rack',
  'pull_up_bar','cable_machine','smith_machine','treadmill','bike','rower',
  'medicine_ball','trx','plate','jump_rope','box','machine_generic'
);
create type exercise_kind_t    as enum ('strength','cardio','mobility','hiit','calisthenics','core','rest');
create type post_kind_t        as enum ('meal','workout','recipe','progress','text','milestone');
create type notify_kind_t      as enum ('motivation','celebration','reminder','social','system','alert');
create type overeat_severity_t as enum ('none','mild','moderate','severe');

-- ---------- shared trigger ----------
create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

-- ---------- helper: current user id from Supabase JWT ----------
create or replace function neutriai_uid() returns uuid
language sql stable as $$ select auth.uid() $$;
