# Optimization

Notes on cost, latency and scale, with the numbers that motivated each choice.

## What a food scan costs

| Stage | Cost | Latency |
|---|---|---|
| Image downscale to 1280 px | 0 | ~120 ms |
| GPT-4o Vision (1 image, ~1.1k in / 700 out) | ~$0.010 | 2.0–3.5 s |
| Nutrition resolution (cache hit) | 0 | ~40 ms |
| Nutrition resolution (cache miss, per item) | 0 | 400–800 ms |
| Claude reasoning (~1.5k in / 600 out) | ~$0.014 | 1.5–3.0 s |
| Persist + assess | 0 | ~200 ms |
| **Total, warm cache** | **~$0.024** | **4–7 s** |

At $6.99/month, a user scanning three meals a day costs about $2.16 in models —
roughly 31% of revenue. That is workable but not comfortable, which is why the
levers below exist.

### Levers, in order of return

1. **Downscale before upload, not after.** Sending 1280 px instead of a 4032 px
   phone original cuts vision input tokens by roughly 85%. Doing the resize on
   the phone (`expo-image-manipulator`) also removes the upload wait.

2. **Route simple plates to `gpt-4o-mini`.** A single-item photo with a
   confident detection does not need the full model. Gate on the vision
   response: re-run on the large model only when `items.length > 2` or any
   confidence is below 0.6. On a typical distribution this cuts vision spend
   roughly in half.

3. **Skip the Claude pass when it cannot help.** If every item resolved from a
   real nutrition database and all confidences exceed 0.8, the reasoning pass
   almost never changes anything. Skipping it there saves ~$0.014 and 2 s on
   maybe 40% of scans.

4. **Cache aggressively across users.** `food_facts` is keyed on a normalized
   name, not per user. "Grilled chicken breast" is fetched once, ever. Expect
   above 90% hit rate within a few weeks of real traffic. Watch
   `select source, count(*) from food_facts group by 1` — a rising
   `ai_estimate` share means the providers are failing and quality is quietly
   degrading.

5. **Batch motivation generation.** Messages are short and not time-critical.
   Generating a day's worth per user in one call, in the worker, costs a
   fraction of one call per event.

## Database

**Already done.** Triggers keep `daily_summaries` current so the home screen
reads one row; `dashboard()` returns seven things in one round trip; feed
counters are maintained by trigger so nothing ever runs `COUNT(*)` on likes.

**Indexes that matter under load** (all present in the migrations):

```sql
meals (user_id, day desc, eaten_at desc)     -- the meal list
daily_summaries (user_id, day desc)          -- the history chart
posts (created_at desc) where visibility='public'  -- partial, for discover
notifications (user_id) where read_at is null      -- partial, for the badge
food_facts using gin (display_name gin_trgm_ops)   -- type-ahead
exercises using gin (equipment)              -- library filtering
```

**When you outgrow this.** At around 10k daily active users:

- Partition `health_days` and `meals` by month. Both are append-heavy and
  almost always queried over a recent window.
- Move the feed to a materialized fan-out table. The `IN (following ids)` query
  is fine to a few hundred follows and falls over past a few thousand.
- Add a read replica for the analytics/history endpoints.

## API

- **Photos bypass the API.** They go phone → Storage directly, so a request
  never buffers 4 MB. The API fetches from Storage on its own timeline.
- **The daily quota resets lazily**, on the user's first request of a new day,
  rather than in a nightly job over the whole table.
- **Side effects are detached.** Streaks, motivation and celebrations run as
  `asyncio.create_task` after the response is composed. A motivation failure can
  never fail a scan.
- **Two uvicorn workers per container.** Every route awaits network, not CPU.
  The usual `2×cores+1` rule is for CPU-bound apps and just wastes memory here.

**Next.** Move `/scans` to a job queue and stream progress over a websocket.
The user is currently blocked for 4–7 s on a request that could be three
observable stages. This also removes the timeout risk on a slow provider day.

## Mobile

- **React Query with a 30 s stale time** — the dashboard is cheap to refetch and
  wrong numbers are worse than an extra request.
- **Optimistic water logging.** Tapping a glass moves the bar immediately and
  rolls back on failure. It is the highest-frequency interaction in the app.
- **Rings are SVG, not an animation library.** The values change a few times a
  minute; a spring simulation would cost battery for nothing. The one exception
  is the fasting ring, which genuinely moves continuously, and it ticks locally
  every 30 s rather than polling the API.
- **40 confetti particles for 2.5 s.** Indistinguishable from a physics engine,
  and it does not drop frames on a three-year-old Android phone.

**Next.** Cache the dashboard to AsyncStorage and render it before the network
resolves. A cold open currently shows a spinner on a slow connection, and the
data is almost always still valid.

## Accuracy — the thing that actually retains users

The correction loop is the asset. Every `PATCH /meals/{id}` is a labelled
example: photo, our estimate, the user's correction.

```sql
-- systematic bias by food and estimation method
select mi.name, mi.estimation_method,
       count(*) as n,
       avg(mi.grams) as predicted,
       avg(corrected.grams) as actual,
       avg(corrected.grams - mi.grams) as bias_g
from meal_items mi
join meals m on m.id = mi.meal_id and m.is_verified
join meal_items corrected on corrected.meal_id = m.id and corrected.name = mi.name
group by 1, 2 having count(*) > 20
order by abs(avg(corrected.grams - mi.grams)) desc;
```

That query directly tunes the `SHAPE_FACTORS`, `HEIGHT_PRIORS_MM` and
`DENSITY_G_ML` tables in `portion.py` — those constants are priors, and priors
should be updated by evidence. A food with a consistent 30% under-estimate is a
one-line fix once you can see it.

The same data eventually justifies a small trained model for the area→grams
step per food class, which is where the accuracy ceiling really lifts. But not
before there is real correction data; guessing at the architecture first would
be building on nothing.
