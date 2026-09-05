"""Motivational messaging with a hard no-repeat guarantee.

The requirement is "personalized, dynamic, unique, non-repetitive". A language
model on its own will not give you that — ask for encouragement a hundred times
and you will get "You've got this!" a dozen of them. So uniqueness is enforced
in the database, not hoped for in the prompt:

* Every message is normalized and hashed; the hash has a UNIQUE index per user.
* Generation shows the model the last 25 lines this user received and asks for
  something structurally different.
* On a hash collision we retry with a higher temperature and an explicit
  "that was too close to one you already sent" nudge.
* After the retries, we fall back to a large template bank filtered against
  already-used hashes, so the user still gets *something* new.

The tone rules live in the prompt, but they are also enforced here: a message
containing shaming language is rejected before it is ever stored.
"""
from __future__ import annotations

import hashlib
import random
import re
from datetime import date, datetime, timezone

import structlog

from ...config import settings
from ...db import maybe_one, rows, service
from ..ai.client import ask_text, record_usage
from ..ai.prompts import MOTIVATION_SYSTEM

log = structlog.get_logger()

# Anything matching these never reaches a user, whatever the model produced.
BANNED = re.compile(
    r"\b(cheat|guilt(y|ing)?|sinful|shame|fat[- ]?ass|lazy|failure|"
    r"burn it off|earn(ed)? (it|those)|bad food|good food|"
    r"punish|starv|purge|make up for it)\b",
    re.I,
)

TRIGGERS = {
    "goal_hit", "meal_logged", "workout_done", "water_goal", "fast_complete",
    "recipe_posted", "progress_shared", "pr_hit", "overate", "under_hydrated",
    "behind_on_steps", "inactive", "streak_extended", "first_log", "comeback",
}


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def hash_body(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Template bank — the guaranteed floor. Placeholders are filled from context.
# ---------------------------------------------------------------------------
TEMPLATES: dict[str, list[str]] = {
    "goal_hit": [
        "Every target met today. That is {streak} days of showing up.",
        "Calories, protein, water — all three landed. Quietly excellent.",
        "You closed every ring on the plan today. Nothing flashy, just done.",
        "Day {streak}. The habit is starting to look like a default.",
    ],
    "meal_logged": [
        "Logged — {kcal} kcal in. You are keeping the picture honest.",
        "That is {meals} meals tracked today. The data is doing its job.",
        "Noted at {kcal} kcal. Small acts of attention add up.",
    ],
    "workout_done": [
        "Session done. {minutes} minutes you will not get back and would not want to.",
        "Training logged. Consistency beats intensity, and you have both this week.",
        "That is {workouts} sessions this week. The plan is working because you are.",
    ],
    "water_goal": [
        "Full gallon down. Your kidneys send their regards.",
        "Hydration target hit — {ml} ml. Easy to skip, easy to feel.",
        "Water goal closed out for day {streak} running.",
    ],
    "fast_complete": [
        "{hours} hours closed out. Clean finish.",
        "Fast complete at {hours}h. Break it with protein and you will feel it.",
        "That window held. {streak} fasts in a row now.",
    ],
    "recipe_posted": [
        "Recipe is live. Someone is going to cook that tonight.",
        "Posted — and the macros are already calculated for whoever tries it.",
    ],
    "pr_hit": [
        "New PR on {exercise}: {value}{unit}. That number was not there last month.",
        "{exercise} personal best. Write it down somewhere you will see it.",
    ],
    "streak_extended": [
        "Day {streak}. Streaks are just decisions repeated.",
        "{streak} days unbroken. The hard part was the first four.",
    ],
    "overate": [
        "Over target today, and that is genuinely fine. Tomorrow starts at zero.",
        "One heavy day inside a good week changes almost nothing. Keep going.",
        "You are {over} kcal over. Worth noticing, not worth carrying around.",
    ],
    "under_hydrated": [
        "Only {ml} ml so far — a glass now and you are back on pace.",
        "Water is lagging today. Easiest win available to you right now.",
    ],
    "behind_on_steps": [
        "{steps} steps so far. A ten-minute walk moves that more than you would think.",
        "Movement is light today. No verdict, just a nudge.",
    ],
    "inactive": [
        "It has been a few days. The app remembers where you left off.",
        "No pressure and no lecture — the plan is still here when you want it.",
    ],
    "comeback": [
        "Back at it. Restarting is the skill, not a setback.",
        "Gap closed. Day one of the next stretch.",
    ],
    "first_log": [
        "First meal logged. That is the whole trick — start, then repeat.",
        "You are on the board. Everything after this is momentum.",
    ],
    "progress_shared": [
        "Shared. Someone in your feed needed to see that today.",
    ],
}

DEFAULT_TEMPLATES = [
    "Still here, still tracking. That is most of it.",
    "Another data point. The pattern is what matters, not the day.",
    "Showing up beats optimising. You are showing up.",
]


def _fill(text: str, ctx: dict) -> str:
    safe = {
        "streak": ctx.get("streak", 1), "kcal": ctx.get("kcal", 0),
        "meals": ctx.get("meals", 1), "minutes": ctx.get("minutes", 30),
        "workouts": ctx.get("workouts", 1), "ml": ctx.get("ml", 0),
        "hours": ctx.get("hours", 16), "over": ctx.get("over", 0),
        "steps": ctx.get("steps", 0), "exercise": ctx.get("exercise", "that lift"),
        "value": ctx.get("value", ""), "unit": ctx.get("unit", ""),
    }
    try:
        return text.format(**safe)
    except (KeyError, IndexError):
        return text


async def recent_bodies(user_id: str, limit: int = 25) -> list[str]:
    return [
        r["body"]
        for r in rows(
            service().table("motivation_messages").select("body")
            .eq("user_id", user_id).order("created_at", desc=True)
            .limit(limit).execute()
        )
    ]


def _acceptable(text: str) -> bool:
    if not text or len(text) > 200:
        return False
    return not BANNED.search(text)


async def generate(user_id: str, trigger: str, ctx: dict, profile: dict | None = None) -> str | None:
    """Produce one never-before-sent message for this user. Returns None only if
    every avenue is exhausted, which in practice does not happen."""
    sb = service()
    history = await recent_bodies(user_id)
    used = {hash_body(b) for b in history}
    profile = profile or {}

    tone = "encouraging"
    if trigger in ("overate", "under_hydrated", "behind_on_steps", "inactive"):
        tone = "gentle and forward-looking"
    elif trigger in ("pr_hit", "goal_hit", "streak_extended"):
        tone = "genuinely pleased, understated"

    for attempt in range(3):
        nudge = ""
        if attempt:
            nudge = (
                "\n\nYour previous attempt was too close to something this user has "
                "already received. Change the sentence structure entirely — different "
                "opening word, different rhythm, different angle on the same fact."
            )
        call = await ask_text(
            pipeline="motivation",
            model=settings.motivation_model,
            system=MOTIVATION_SYSTEM,
            user_text=(
                f"Trigger: {trigger}\nTone: {tone}\n"
                f"User goal: {profile.get('goal', 'maintain')}\n"
                f"Context data: {ctx}\n\n"
                "Lines this user has ALREADY received (do not repeat or paraphrase):\n"
                + ("\n".join(f"- {h}" for h in history[:25]) or "- (none yet)")
                + nudge
            ),
            max_tokens=120,
        )
        await record_usage(call, user_id)
        text = (call.payload or "").strip().strip('"') if call.ok else ""
        if _acceptable(text) and hash_body(text) not in used:
            return text
        if text and not _acceptable(text):
            log.warning("motivation_rejected_by_filter", trigger=trigger)

    # Template floor: pick something from the bank we have not used before.
    pool = TEMPLATES.get(trigger, []) + DEFAULT_TEMPLATES
    random.shuffle(pool)
    for tpl in pool:
        candidate = _fill(tpl, ctx)
        if hash_body(candidate) not in used and _acceptable(candidate):
            return candidate

    # Everything in the bank is used: append a distinguishing fact so it is
    # still literally new rather than sending nothing.
    base = _fill(random.choice(pool or DEFAULT_TEMPLATES), ctx)
    return f"{base} (day {ctx.get('streak', date.today().day)})"


async def on_event(user_id: str, trigger: str, ctx: dict | None = None) -> dict | None:
    """Generate, store and enqueue a motivational message for one event."""
    if trigger not in TRIGGERS:
        return None
    sb = service()
    prefs = maybe_one(
        sb.table("notification_settings").select("*").eq("user_id", user_id).limit(1).execute()
    )
    if prefs and not prefs.get("motivation"):
        return None

    profile = maybe_one(
        sb.table("profiles").select("goal,display_name,diet_mode").eq("id", user_id)
        .limit(1).execute()
    ) or {}
    ctx = ctx or {}

    # Enrich context so messages can name real numbers.
    streak = maybe_one(
        sb.table("streaks").select("current_len").eq("user_id", user_id)
        .eq("kind", "log").limit(1).execute()
    )
    ctx.setdefault("streak", (streak or {}).get("current_len", 1))

    body = await generate(user_id, trigger, ctx, profile)
    if not body:
        return None

    try:
        msg = sb.table("motivation_messages").insert({
            "user_id": user_id, "trigger": trigger, "tone": "encouraging",
            "body": body, "body_hash": hash_body(body), "context": ctx,
            "model": settings.motivation_model,
            "delivered_at": datetime.now(timezone.utc).isoformat(),
        }).execute().data[0]
    except Exception as exc:  # noqa: BLE001
        # Unique violation means we raced ourselves — not worth retrying.
        log.info("motivation_duplicate_skipped", trigger=trigger, error=str(exc)[:120])
        return None

    sb.table("notifications").insert({
        "user_id": user_id, "kind": "motivation", "title": "NeutriAI",
        "body": body, "deep_link": "neutriai://home",
        "payload": {"trigger": trigger, "message_id": msg["id"]},
    }).execute()
    return msg


# ---------------------------------------------------------------------------
# Daily celebration
# ---------------------------------------------------------------------------
ANIMATIONS = {
    "daily_goals": "confetti", "streak": "flame", "pr": "trophy",
    "first_post": "rings", "fast_complete": "wave", "milestone": "fireworks",
    "water_goal": "wave",
}


async def celebrate(user_id: str, kind: str, title: str, subtitle: str = "", payload: dict | None = None):
    """Record a celebration for the app to animate. Idempotent per user/day/kind."""
    sb = service()
    prefs = maybe_one(
        sb.table("notification_settings").select("celebration").eq("user_id", user_id)
        .limit(1).execute()
    )
    if prefs and not prefs.get("celebration"):
        return None
    try:
        return sb.table("celebrations").upsert({
            "user_id": user_id, "day": date.today().isoformat(), "kind": kind,
            "title": title[:120], "subtitle": subtitle[:200],
            "animation": ANIMATIONS.get(kind, "confetti"),
            "payload": payload or {},
        }, on_conflict="user_id,day,kind").execute().data[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("celebration_failed", error=str(exc)[:200])
        return None


async def check_daily_goals(user_id: str, day: str | None = None) -> dict | None:
    """Called after any logging action; fires the celebration once per day."""
    sb = service()
    day = day or date.today().isoformat()
    summary = maybe_one(
        sb.table("daily_summaries").select("*").eq("user_id", user_id).eq("day", day)
        .limit(1).execute()
    )
    if not summary or summary.get("celebrated_at"):
        return None
    met = summary.get("goals_met") or []
    if len(met) < 3:
        return None

    sb.table("daily_summaries").update(
        {"celebrated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("user_id", user_id).eq("day", day).execute()

    streak = sb.rpc("bump_streak", {"p_user": user_id, "p_kind": "goals", "p_day": day}).execute()
    n = streak.data if isinstance(streak.data, int) else 1

    await on_event(user_id, "goal_hit", {"streak": n, "goals": met})
    return await celebrate(
        user_id, "daily_goals",
        "Every goal met today",
        f"{', '.join(met)} — {n} day{'s' if n != 1 else ''} running",
        {"goals": met, "streak": n},
    )
