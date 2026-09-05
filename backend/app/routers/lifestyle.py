"""Water intake and intermittent fasting."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Query

from ..db import maybe_one, one, rows, service
from ..deps import CurrentUserDep
from ..errors import AppError, NotFound
from ..models.common import Ok
from ..models.lifestyle import (
    FastingSettingsIn, FastOut, FastStartIn, HydrationSettingsIn, WaterDayOut, WaterIn,
)
from ..services.motivation.engine import celebrate, check_daily_goals, on_event

router = APIRouter(tags=["lifestyle"])

GALLON_ML = 3785


def streak_note(n) -> str:
    n = n if isinstance(n, int) else 1
    return "first day" if n <= 1 else f"{n} days running"

PROTOCOL_MINUTES = {
    "16:8": 16 * 60, "18:6": 18 * 60, "20:4": 20 * 60,
    "omad": 23 * 60, "5:2": 24 * 60,
}

# What the body is roughly doing at each point in a fast. Shown in the ring.
FAST_PHASES = [
    (0, "Fed", "Digesting and absorbing. Insulin is elevated."),
    (4 * 60, "Early fasting", "Blood sugar settling, glycogen becoming the main fuel."),
    (12 * 60, "Fat burning", "Glycogen is running low and fat oxidation is picking up."),
    (16 * 60, "Ketosis onset", "Ketone production ramps up; many people notice clearer focus."),
    (18 * 60, "Deep fasting", "Autophagy signalling increases. Keep hydration and electrolytes up."),
    (24 * 60, "Extended", "Past 24 hours — go gently and break the fast with something light."),
]


def _phase(minutes: int) -> tuple[str, str]:
    name, note = FAST_PHASES[0][1], FAST_PHASES[0][2]
    for edge, n, d in FAST_PHASES:
        if minutes >= edge:
            name, note = n, d
    return name, note


# ===========================================================================
# Water
# ===========================================================================
@router.get("/water", response_model=WaterDayOut)
async def water_day(user: CurrentUserDep, day: date | None = None):
    d = (day or date.today()).isoformat()
    sb = user.sb
    settings_row = maybe_one(
        sb.table("hydration_settings").select("*").eq("user_id", user.id).limit(1).execute()
    ) or {"daily_goal_ml": GALLON_ML}
    logs = rows(
        sb.table("water_logs").select("*").eq("user_id", user.id).eq("day", d)
        .order("logged_at").execute()
    )
    total = sum(int(l["amount_ml"]) for l in logs)
    goal = int(settings_row.get("daily_goal_ml") or GALLON_ML)
    streak = maybe_one(
        sb.table("streaks").select("current_len").eq("user_id", user.id)
        .eq("kind", "water").limit(1).execute()
    )

    # "On pace" = ahead of a linear curve across the reminder window.
    now = datetime.now(timezone.utc)
    hours_in = max(0.0, min(1.0, (now.hour + now.minute / 60 - 8) / 13))
    on_pace = total >= goal * hours_in * 0.85

    return WaterDayOut(
        day=d, goal_ml=goal, total_ml=total,
        pct=round(min(100.0, total / max(goal, 1) * 100), 1),
        remaining_ml=max(0, goal - total), logs=logs,
        streak=(streak or {}).get("current_len", 0), on_pace=on_pace,
    )


@router.post("/water", status_code=201)
async def log_water(body: WaterIn, user: CurrentUserDep):
    logged = body.logged_at or datetime.now(timezone.utc)
    row = one(
        user.sb.table("water_logs").insert({
            "user_id": user.id, "amount_ml": body.amount_ml,
            "container": body.container, "day": logged.date().isoformat(),
            "logged_at": logged.isoformat(),
        }).execute()
    )

    day = logged.date().isoformat()
    total = sum(
        int(l["amount_ml"]) for l in rows(
            user.sb.table("water_logs").select("amount_ml").eq("user_id", user.id)
            .eq("day", day).execute()
        )
    )
    hydration = maybe_one(
        user.sb.table("hydration_settings").select("daily_goal_ml").eq("user_id", user.id)
        .limit(1).execute()
    ) or {}
    goal = int(hydration.get("daily_goal_ml") or GALLON_ML)

    # Fire the goal celebration exactly once, on the log that crosses the line.
    if total >= goal > total - body.amount_ml:
        n = service().rpc(
            "bump_streak", {"p_user": user.id, "p_kind": "water", "p_day": day}
        ).execute().data
        await on_event(user.id, "water_goal", {"ml": total, "streak": n if isinstance(n, int) else 1})
        await celebrate(user.id, "water_goal", "Hydration goal hit",
                        f"{total} ml — {streak_note(n)}", {"ml": total})
        await check_daily_goals(user.id, day)

    return {**row, "day_total_ml": total, "goal_ml": goal}


@router.delete("/water/{log_id}", response_model=Ok)
async def delete_water(log_id: str, user: CurrentUserDep):
    user.sb.table("water_logs").delete().eq("id", log_id).eq("user_id", user.id).execute()
    return Ok(message="Removed.")


@router.patch("/water/settings")
async def update_hydration(body: HydrationSettingsIn, user: CurrentUserDep):
    patch = body.model_dump(exclude_none=True)
    for k in ("reminder_start", "reminder_end"):
        if patch.get(k):
            patch[k] = patch[k].isoformat()
    return one(
        user.sb.table("hydration_settings")
        .upsert({**patch, "user_id": user.id}, on_conflict="user_id").execute()
    )


# ===========================================================================
# Fasting
# ===========================================================================
def _to_out(fast: dict, streak: int = 0) -> FastOut:
    started = datetime.fromisoformat(str(fast["started_at"]).replace("Z", "+00:00"))
    target = int(fast["target_minutes"])
    if fast["status"] == "active":
        elapsed = int((datetime.now(timezone.utc) - started).total_seconds() // 60)
    else:
        elapsed = int(fast.get("actual_minutes") or 0)
    name, note = _phase(elapsed)
    return FastOut(
        id=fast["id"], protocol=fast["protocol"], status=fast["status"],
        started_at=started, ends_at=started + timedelta(minutes=target),
        ended_at=fast.get("ended_at"), target_minutes=target,
        elapsed_minutes=max(0, elapsed),
        remaining_minutes=max(0, target - elapsed),
        pct=round(min(100.0, elapsed / max(target, 1) * 100), 1),
        phase=name, phase_note=note, streak=streak,
    )


@router.get("/fasts/current", response_model=FastOut | None)
async def current_fast(user: CurrentUserDep):
    fast = maybe_one(
        user.sb.table("fasts").select("*").eq("user_id", user.id).eq("status", "active")
        .limit(1).execute()
    )
    if not fast:
        return None
    streak = maybe_one(
        user.sb.table("streaks").select("current_len").eq("user_id", user.id)
        .eq("kind", "fast").limit(1).execute()
    )
    return _to_out(fast, (streak or {}).get("current_len", 0))


@router.post("/fasts", response_model=FastOut, status_code=201)
async def start_fast(body: FastStartIn, user: CurrentUserDep):
    existing = maybe_one(
        user.sb.table("fasts").select("id").eq("user_id", user.id).eq("status", "active")
        .limit(1).execute()
    )
    if existing:
        raise AppError("A fast is already running. End it before starting another.",
                       code="fast_in_progress")

    if body.protocol == "custom":
        if not body.custom_hours:
            raise AppError("Custom protocol needs custom_hours.")
        target = int(body.custom_hours * 60)
    else:
        target = PROTOCOL_MINUTES[body.protocol]

    fast = one(
        user.sb.table("fasts").insert({
            "user_id": user.id, "protocol": body.protocol, "target_minutes": target,
            "started_at": (body.started_at or datetime.now(timezone.utc)).isoformat(),
            "status": "active",
        }).execute()
    )
    return _to_out(fast)


@router.post("/fasts/{fast_id}/end", response_model=FastOut)
async def end_fast(fast_id: str, user: CurrentUserDep, break_meal_id: str | None = None):
    fast = maybe_one(
        user.sb.table("fasts").select("*").eq("id", fast_id).eq("user_id", user.id)
        .limit(1).execute()
    )
    if not fast:
        raise NotFound("Fast not found.")
    if fast["status"] != "active":
        return _to_out(fast)

    started = datetime.fromisoformat(str(fast["started_at"]).replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    actual = int((now - started).total_seconds() // 60)
    completed = actual >= int(fast["target_minutes"]) * 0.95   # 5% grace

    updated = one(
        user.sb.table("fasts").update({
            "ended_at": now.isoformat(), "actual_minutes": actual,
            "status": "completed" if completed else "broken",
            "break_meal_id": break_meal_id,
        }).eq("id", fast_id).execute()
    )

    streak = 0
    if completed:
        n = service().rpc(
            "bump_streak",
            {"p_user": user.id, "p_kind": "fast", "p_day": date.today().isoformat()},
        ).execute().data
        streak = n if isinstance(n, int) else 1
        await on_event(user.id, "fast_complete",
                       {"hours": round(actual / 60, 1), "streak": streak})
        await celebrate(user.id, "fast_complete", f"{round(actual / 60)}h fast complete",
                        f"{streak} in a row", {"minutes": actual})
    return _to_out(updated, streak)


@router.get("/fasts", response_model=list[FastOut])
async def list_fasts(user: CurrentUserDep, limit: int = Query(30, le=200)):
    return [
        _to_out(f) for f in rows(
            user.sb.table("fasts").select("*").eq("user_id", user.id)
            .order("started_at", desc=True).limit(limit).execute()
        )
    ]


@router.get("/fasts/settings")
async def fasting_settings(user: CurrentUserDep):
    return maybe_one(
        user.sb.table("fasting_settings").select("*").eq("user_id", user.id).limit(1).execute()
    ) or {"user_id": user.id, "protocol": "16:8", "eating_window_start": "12:00"}


@router.patch("/fasts/settings")
async def update_fasting(body: FastingSettingsIn, user: CurrentUserDep):
    patch = body.model_dump(exclude_none=True)
    if patch.get("eating_window_start"):
        patch["eating_window_start"] = patch["eating_window_start"].isoformat()
    return one(
        user.sb.table("fasting_settings")
        .upsert({**patch, "user_id": user.id}, on_conflict="user_id").execute()
    )
