-- ============================================================
-- NutriAI :: 0011 Row Level Security
-- Default posture: deny-all, then grant the narrowest thing that works.
-- The FastAPI service role bypasses RLS; the mobile client does not.
-- ============================================================

do $$
declare t text;
begin
  foreach t in array array[
    'profiles','dietary_restrictions','body_metrics','nutrition_targets',
    'daily_summaries','streaks','food_scans','scan_calibrations','meals',
    'meal_items','intake_assessments','device_connections','health_days',
    'workouts','workout_sets','personal_records','equipment_scans',
    'training_plans','plan_days','water_logs','hydration_settings',
    'fasting_settings','fasts','recipes','recipe_ingredients','recipe_saves',
    'shopping_lists','follows','posts','post_likes','comments',
    'billing_customers','subscriptions','entitlements','motivation_messages',
    'celebrations','notifications','notification_settings','ai_usage'
  ] loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
  end loop;
end $$;

-- food_facts and exercises are shared reference data: readable by all,
-- writable only by the service role.
alter table food_facts enable row level security;
alter table exercises  enable row level security;
create policy read_food_facts on food_facts for select to authenticated using (true);
create policy read_exercises  on exercises  for select to authenticated using (true);

-- ---------- generic owner policies ----------
do $$
declare t text;
begin
  foreach t in array array[
    'dietary_restrictions','body_metrics','nutrition_targets','daily_summaries',
    'streaks','food_scans','scan_calibrations','meals','intake_assessments',
    'device_connections','health_days','workouts','personal_records',
    'equipment_scans','training_plans','water_logs','hydration_settings',
    'fasting_settings','fasts','shopping_lists','recipe_saves',
    'motivation_messages','celebrations','notifications','notification_settings'
  ] loop
    execute format($f$
      create policy %1$s_own_select on %1$I for select to authenticated
        using (user_id = auth.uid());
      create policy %1$s_own_insert on %1$I for insert to authenticated
        with check (user_id = auth.uid());
      create policy %1$s_own_update on %1$I for update to authenticated
        using (user_id = auth.uid()) with check (user_id = auth.uid());
      create policy %1$s_own_delete on %1$I for delete to authenticated
        using (user_id = auth.uid());
    $f$, t);
  end loop;
end $$;

-- Read-only-to-client tables (server writes them).
create policy entitlements_own on entitlements for select to authenticated
  using (user_id = auth.uid());
create policy subscriptions_own on subscriptions for select to authenticated
  using (user_id = auth.uid());
create policy billing_own on billing_customers for select to authenticated
  using (user_id = auth.uid());
create policy ai_usage_own on ai_usage for select to authenticated
  using (user_id = auth.uid());

-- ---------- profiles ----------
create policy profiles_self_write on profiles for update to authenticated
  using (id = auth.uid()) with check (id = auth.uid());
create policy profiles_self_insert on profiles for insert to authenticated
  with check (id = auth.uid());
-- Public profiles are visible; private ones only to the owner and followers.
create policy profiles_read on profiles for select to authenticated using (
  id = auth.uid()
  or not is_private
  or exists (select 1 from follows f
              where f.followee_id = profiles.id and f.follower_id = auth.uid())
);

-- ---------- child tables: inherit the parent's owner ----------
create policy meal_items_own on meal_items for all to authenticated
  using (exists (select 1 from meals m where m.id = meal_id and m.user_id = auth.uid()))
  with check (exists (select 1 from meals m where m.id = meal_id and m.user_id = auth.uid()));

create policy workout_sets_own on workout_sets for all to authenticated
  using (exists (select 1 from workouts w where w.id = workout_id and w.user_id = auth.uid()))
  with check (exists (select 1 from workouts w where w.id = workout_id and w.user_id = auth.uid()));

create policy plan_days_own on plan_days for all to authenticated
  using (exists (select 1 from training_plans p where p.id = plan_id and p.user_id = auth.uid()))
  with check (exists (select 1 from training_plans p where p.id = plan_id and p.user_id = auth.uid()));

create policy recipe_ing_read on recipe_ingredients for select to authenticated
  using (exists (select 1 from recipes r where r.id = recipe_id
                  and (r.is_public or r.author_id = auth.uid())));
create policy recipe_ing_write on recipe_ingredients for all to authenticated
  using (exists (select 1 from recipes r where r.id = recipe_id and r.author_id = auth.uid()))
  with check (exists (select 1 from recipes r where r.id = recipe_id and r.author_id = auth.uid()));

-- ---------- recipes ----------
create policy recipes_read on recipes for select to authenticated
  using (is_public or author_id = auth.uid());
create policy recipes_write on recipes for insert to authenticated
  with check (author_id = auth.uid());
create policy recipes_update on recipes for update to authenticated
  using (author_id = auth.uid()) with check (author_id = auth.uid());
create policy recipes_delete on recipes for delete to authenticated
  using (author_id = auth.uid());

-- ---------- social ----------
create policy follows_read on follows for select to authenticated
  using (follower_id = auth.uid() or followee_id = auth.uid());
create policy follows_write on follows for insert to authenticated
  with check (follower_id = auth.uid());
create policy follows_delete on follows for delete to authenticated
  using (follower_id = auth.uid());

create policy posts_read on posts for select to authenticated using (
  author_id = auth.uid()
  or visibility = 'public'
  or (visibility = 'followers' and exists (
        select 1 from follows f
         where f.followee_id = posts.author_id and f.follower_id = auth.uid()))
);
create policy posts_write on posts for insert to authenticated
  with check (author_id = auth.uid());
create policy posts_update on posts for update to authenticated
  using (author_id = auth.uid()) with check (author_id = auth.uid());
create policy posts_delete on posts for delete to authenticated
  using (author_id = auth.uid());

create policy likes_read on post_likes for select to authenticated using (true);
create policy likes_write on post_likes for insert to authenticated
  with check (user_id = auth.uid());
create policy likes_delete on post_likes for delete to authenticated
  using (user_id = auth.uid());

create policy comments_read on comments for select to authenticated
  using (exists (select 1 from posts p where p.id = post_id));
create policy comments_write on comments for insert to authenticated
  with check (author_id = auth.uid());
create policy comments_delete on comments for delete to authenticated
  using (author_id = auth.uid()
         or exists (select 1 from posts p where p.id = post_id and p.author_id = auth.uid()));

-- ---------- storage buckets ----------
insert into storage.buckets (id, name, public)
values ('meal-photos', 'meal-photos', false),
       ('equipment-photos', 'equipment-photos', false),
       ('recipe-photos', 'recipe-photos', true),
       ('avatars', 'avatars', true),
       ('post-media', 'post-media', true)
on conflict (id) do nothing;

-- Users may only touch objects under a folder named with their own uid.
create policy own_folder_rw on storage.objects for all to authenticated
  using (bucket_id in ('meal-photos','equipment-photos','recipe-photos','avatars','post-media')
         and (storage.foldername(name))[1] = auth.uid()::text)
  with check (bucket_id in ('meal-photos','equipment-photos','recipe-photos','avatars','post-media')
         and (storage.foldername(name))[1] = auth.uid()::text);
create policy public_bucket_read on storage.objects for select to authenticated
  using (bucket_id in ('recipe-photos','avatars','post-media'));
