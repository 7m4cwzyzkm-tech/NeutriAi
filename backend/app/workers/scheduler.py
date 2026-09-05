"""Background jobs.

Run as a separate process:  ``python -m app.workers.scheduler``

Deliberately not in the API process — a long sync should never compete with a
user waiting on a food scan, and the API scales on request volume while these
jobs scale on user count.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import settings
from ..db import maybe_one, rows, service
from ..logging_conf import configure_logging
from ..services.fitness.providers import sync_connection
from ..services.push import deliver as deliver_pushes
from ..services.motivation.engine import on_event

configure_logging()
log = structlog.get_logger()


# ---------------------------------------------------------------------------
async def sync_all_wearables() -> None:
    """Pull the last 3 days for every connected pull-provider account."""
    conns = rows(
        service().table("device_connections").select("*")
        .in_("provider", ["fitbit", "google_fit", "garmin"])
        .eq("status", "connected").execute()
    )
    log.info("wearable_sync_start", connections=len(conns))
    # Bounded concurrency: providers rate-limit per app, not per user.
    sem = asyncio.Semaphore(8)

    async def one(c):
        async with sem:
            try:
                return await sync_connection(c, days_back=3)
            except Exception as exc:  # noqa: BLE001
                log.warning("wearable_sync_failed", provider=c["provider"], error=str(exc)[:200])
                return None

    results = await asyncio.gather(*(one(c) for c in conns))
    log.info("wearable_sync_done", ok=sum(1 for r in results if r))


# ---------------------------------------------------------------------------
async def hydration_reminders() -> None:
    """Nudge users who are behind their hydration pace, inside their window."""
    now = datetime.now(timezone.utc)
    today = date.today().isoformat()
    settings_rows = rows(
        service().table("hydration_settings").select("*").eq("reminder_enabled", True).execute()
    )
    for s in settings_rows:
        uid = s["user_id"]
        logged = sum(
            int(r["amount_ml"]) for r in rows(
                service().table("water_logs").select("amount_ml")
                .eq("user_id", uid).eq("day", today).execute()
            )
        )
        goal = int(s.get("daily_goal_ml") or 3785)
        # Linear pace across the reminder window.
        start_h = int(str(s.get("reminder_start", "08:00"))[:2])
        end_h = int(str(s.get("reminder_end", "21:00"))[:2])
        span = max(1, end_h - start_h)
        progress = max(0.0, min(1.0, (now.hour - start_h) / span))
        expected = goal * progress
        if logged < expected * 0.7 and progress > 0.25:
            await on_event(uid, "under_hydrated", {"ml": logged, "goal": goal})


# ---------------------------------------------------------------------------
async def fasting_notifications() -> None:
    """Fire start/halfway/end notifications for active fasts."""
    now = datetime.now(timezone.utc)
    active = rows(
        service().table("fasts").select("*").eq("status", "active").execute()
    )
    for f in active:
        started = datetime.fromisoformat(str(f["started_at"]).replace("Z", "+00:00"))
        elapsed = (now - started).total_seconds() / 60
        target = int(f["target_minutes"])
        prefs = maybe_one(
            service().table("fasting_settings").select("*").eq("user_id", f["user_id"])
            .limit(1).execute()
        ) or {}

        # Fire once per milestone by recording it in the fast's note field.
        marks = set((f.get("note") or "").split(","))
        fire = None
        if elapsed >= target and "end" not in marks and prefs.get("notify_end", True):
            fire = ("end", "Your eating window is open",
                    f"{round(elapsed / 60)}h fast complete. Break it with protein.")
        elif elapsed >= target / 2 and "half" not in marks and prefs.get("notify_halfway"):
            fire = ("half", "Halfway through your fast",
                    f"{round(elapsed / 60, 1)}h down, {round((target - elapsed) / 60, 1)}h to go.")

        if fire:
            mark, title, body = fire
            service().table("notifications").insert({
                "user_id": f["user_id"], "kind": "reminder", "title": title,
                "body": body, "deep_link": "neutriai://fasting",
            }).execute()
            service().table("fasts").update(
                {"note": ",".join(sorted(marks | {mark}) - {""})}
            ).eq("id", f["id"]).execute()


# ---------------------------------------------------------------------------
async def inactivity_nudges() -> None:
    """One gentle message to users who have not logged in 3-7 days.

    Bounded on both ends on purpose: nagging someone who left three weeks ago is
    not a nudge, it is spam.
    """
    cutoff_recent = (date.today() - timedelta(days=3)).isoformat()
    cutoff_old = (date.today() - timedelta(days=7)).isoformat()
    stale = rows(
        service().table("streaks").select("user_id,last_day,current_len")
        .eq("kind", "log").lt("last_day", cutoff_recent).gte("last_day", cutoff_old)
        .limit(500).execute()
    )
    for s in stale:
        await on_event(s["user_id"], "inactive", {"days_away": 3})


# ---------------------------------------------------------------------------
async def rollup_yesterday() -> None:
    """Belt-and-braces recompute. Triggers keep summaries live; this catches any
    day where a trigger was bypassed by a bulk import or a failed transaction."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    users = rows(
        service().table("daily_summaries").select("user_id").eq("day", yesterday).execute()
    )
    for u in users:
        try:
            service().rpc(
                "recompute_daily_summary", {"p_user": u["user_id"], "p_day": yesterday}
            ).execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("rollup_failed", user=u["user_id"], error=str(exc)[:200])


# ---------------------------------------------------------------------------
async def ai_budget_check() -> None:
    """Alert if today's model spend is approaching the ceiling."""
    today = date.today().isoformat()
    usage = rows(
        service().table("ai_usage").select("cost_usd")
        .gte("created_at", today).limit(50000).execute()
    )
    total = sum(float(u["cost_usd"] or 0) for u in usage)
    pct = total / max(settings.ai_daily_cost_ceiling_usd, 0.01) * 100
    log.info("ai_spend_today", usd=round(total, 3), pct_of_ceiling=round(pct, 1))
    if pct > 80:
        log.error("ai_budget_warning", usd=round(total, 2),
                  ceiling=settings.ai_daily_cost_ceiling_usd)


# ---------------------------------------------------------------------------
async def flush_push_queue() -> None:
    """Send any notification row that has not been pushed yet.

    Runs every minute rather than on write, so a notification created during a
    user's quiet hours goes out when those hours end instead of being dropped.
    """
    try:
        result = await deliver_pushes()
        if result["sent"]:
            log.info("push_queue_flushed", **result)
    except Exception as exc:  # noqa: BLE001
        log.warning("push_flush_failed", error=str(exc)[:200])


# ---------------------------------------------------------------------------
def build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(sync_all_wearables, CronTrigger(minute=0, hour="*/4"), id="wearables")
    sched.add_job(hydration_reminders, CronTrigger(minute=0, hour="12,16,19"), id="hydration")
    sched.add_job(fasting_notifications, CronTrigger(minute="*/15"), id="fasting")
    sched.add_job(inactivity_nudges, CronTrigger(minute=0, hour=17), id="inactivity")
    sched.add_job(rollup_yesterday, CronTrigger(minute=20, hour=1), id="rollup")
    sched.add_job(ai_budget_check, CronTrigger(minute=30), id="budget")
    sched.add_job(flush_push_queue, CronTrigger(minute="*"), id="push")
    return sched


async def main() -> None:
    sched = build_scheduler()
    sched.start()
    log.info("worker_started", jobs=[j.id for j in sched.get_jobs()])
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
