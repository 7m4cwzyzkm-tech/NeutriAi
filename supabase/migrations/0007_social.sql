-- ============================================================
-- NutriAI :: 0007 social graph + feed
-- ============================================================

create table follows (
  follower_id  uuid not null references profiles(id) on delete cascade,
  followee_id  uuid not null references profiles(id) on delete cascade,
  created_at   timestamptz not null default now(),
  primary key (follower_id, followee_id),
  check (follower_id <> followee_id)
);
create index on follows(followee_id);

create table posts (
  id           uuid primary key default gen_random_uuid(),
  author_id    uuid not null references profiles(id) on delete cascade,
  kind         post_kind_t not null default 'text',
  body         text not null default '',
  media_paths  text[] not null default '{}',
  meal_id      uuid references meals(id) on delete set null,
  workout_id   uuid references workouts(id) on delete set null,
  recipe_id    uuid references recipes(id) on delete set null,
  metrics      jsonb not null default '{}'::jsonb,  -- denormalized snapshot
  visibility   text not null default 'public' check (visibility in ('public','followers','private')),
  like_count   integer not null default 0,
  comment_count integer not null default 0,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index on posts(author_id, created_at desc);
create index on posts(created_at desc) where visibility = 'public';

create table post_likes (
  post_id    uuid not null references posts(id) on delete cascade,
  user_id    uuid not null references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (post_id, user_id)
);

create table comments (
  id         uuid primary key default gen_random_uuid(),
  post_id    uuid not null references posts(id) on delete cascade,
  author_id  uuid not null references profiles(id) on delete cascade,
  parent_id  uuid references comments(id) on delete cascade,
  body       text not null,
  like_count integer not null default 0,
  created_at timestamptz not null default now()
);
create index on comments(post_id, created_at);

-- Counter maintenance so the feed never has to COUNT(*).
create or replace function bump_post_likes() returns trigger language plpgsql as $$
begin
  if tg_op = 'INSERT' then
    update posts set like_count = like_count + 1 where id = new.post_id;
  else
    update posts set like_count = greatest(like_count - 1, 0) where id = old.post_id;
  end if;
  return null;
end $$;
create trigger t_post_likes after insert or delete on post_likes
  for each row execute function bump_post_likes();

create or replace function bump_post_comments() returns trigger language plpgsql as $$
begin
  if tg_op = 'INSERT' then
    update posts set comment_count = comment_count + 1 where id = new.post_id;
  else
    update posts set comment_count = greatest(comment_count - 1, 0) where id = old.post_id;
  end if;
  return null;
end $$;
create trigger t_comments after insert or delete on comments
  for each row execute function bump_post_comments();

create or replace function bump_recipe_saves() returns trigger language plpgsql as $$
begin
  if tg_op = 'INSERT' then
    update recipes set save_count = save_count + 1 where id = new.recipe_id;
  else
    update recipes set save_count = greatest(save_count - 1, 0) where id = old.recipe_id;
  end if;
  return null;
end $$;
create trigger t_recipe_saves after insert or delete on recipe_saves
  for each row execute function bump_recipe_saves();
