-- ============================================================
-- NeutriAI :: 0024  what the user says a food actually is
--
-- The vision model now reports whether it RECOGNISED a dish or only described
-- what it could see. When it can only describe, the person is asked, and this
-- is where their answer goes.
--
-- WHY A NAME IS WORTH A TABLE
--
-- One plate of rajas, photographed twice a minute apart, came back as "creamy
-- chicken" and then "creamy mushroom sauce". Neither is the dish. That
-- difference alone moved the reported meal from 315 g to 186 g and its energy
-- by about 40%, on geometry that was within 12% both times. The name is not a
-- label -- it picks the density, the height prior and the nutrition lookup.
--
-- WHY PER USER AND NOT A SHARED CATALOGUE
--
-- A curated list of dishes works for the foods on the list and quietly makes
-- the app worse for everyone whose cooking is not on it. What is stored here
-- is what THIS person eats and has named. It is unbounded, it adapts to
-- anybody, and it steers nobody toward a cuisine.
--
-- Re-runnable, unlike 0011, which enables row-level security in a loop and
-- then fails on its policies -- leaving tables locked with no policy on them.
-- ============================================================

create table if not exists public.food_aliases (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references auth.users(id) on delete cascade,
    -- what the model called it, lowercased and trimmed
    described_as  text not null,
    -- what the person says it actually is
    actual_name   text not null,
    -- how many times they have said so. One answer is an offer; two is applied.
    samples       integer not null default 1,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint food_aliases_described_not_blank check (length(trim(described_as)) > 0),
    constraint food_aliases_actual_not_blank    check (length(trim(actual_name)) > 0)
);

-- One row per (person, description). Neither column is nullable, so unlike
-- 0021's `unique (shape, food_name)` this constraint actually constrains --
-- in Postgres a NULL never equals another NULL, and a unique constraint over a
-- nullable column silently does not restrict the null rows.
create unique index if not exists food_aliases_user_described_idx
    on public.food_aliases (user_id, described_as);

create index if not exists food_aliases_user_idx
    on public.food_aliases (user_id);

alter table public.food_aliases enable row level security;

drop policy if exists food_aliases_own on public.food_aliases;
create policy food_aliases_own on public.food_aliases
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
