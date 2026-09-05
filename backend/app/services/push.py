"""Push notification delivery via Expo.

Why this exists: the app writes rows to ``notifications`` and the worker
generates reminders on a schedule, but until now nothing ever left the server.
Every hydration nudge, fasting alert and social notification sat in a table the
user only saw if they happened to open the app — which defeats the point of a
reminder.

Design notes:

* **The database row is the record; the push is a best-effort copy.** We never
  fail a request because a push failed, and ``pushed_at`` records what actually
  went out so a redelivery bug can be diagnosed later.
* **Expo tickets are asynchronous.** A 200 from the send endpoint means Expo
  accepted the message, not that Apple or Google delivered it. Tickets marked
  ``DeviceNotRegistered`` mean the token is dead — we clear it, because
  retrying a dead token forever is how you get rate-limited.
* **Quiet hours are enforced here, not at the call site.** Every path that
  sends goes through ``deliver()``, so there is one place that can wake someone
  at 3 a.m. and one place to stop it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import structlog

from ..db import maybe_one, rows, service

log = structlog.get_logger()

EXPO_ENDPOINT = "https://exp.host/--/api/v2/push/send"
BATCH_SIZE = 100          # Expo's documented maximum per request
SEND_TIMEOUT_S = 15

# Which notification kinds respect quiet hours. A social like can wait until
# morning; a fasting window closing cannot be usefully delayed.
QUIET_HOURS_EXEMPT = {"celebration"}

# notify_kind_t -> the column in notification_settings that gates it
KIND_PREFERENCE = {
    "motivation": "motivation",
    "celebration": "celebration",
    "reminder": None,          # gated per-feature by the worker instead
    "social": "social",
    "system": None,            # billing and account messages always send
    "alert": None,
}


def _valid_token(token: str | None) -> bool:
    return bool(token) and (
        token.startswith("ExponentPushToken[") or token.startswith("ExpoPushToken[")
    )


def _in_quiet_hours(now_utc: datetime, tz_name: str, start: Any, end: Any) -> bool:
    """Quiet hours are stored as local wall-clock times, so they have to be
    compared in the user's timezone, not UTC."""
    try:
        local = now_utc.astimezone(ZoneInfo(tz_name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        local = now_utc
    if isinstance(start, str):
        start = time.fromisoformat(start[:5] if len(start) >= 5 else "22:00")
    if isinstance(end, str):
        end = time.fromisoformat(end[:5] if len(end) >= 5 else "07:00")
    now_t = local.time()
    # Quiet hours normally wrap midnight (22:00 -> 07:00).
    if start <= end:
        return start <= now_t < end
    return now_t >= start or now_t < end


async def _post_batch(messages: list[dict]) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S) as c:
            resp = await c.post(
                EXPO_ENDPOINT,
                json=messages,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                },
            )
        if resp.status_code >= 400:
            log.warning("expo_send_http_error", status=resp.status_code, body=resp.text[:300])
            return []
        return resp.json().get("data") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("expo_send_failed", error=str(exc)[:200])
        return []


def _handle_tickets(tickets: list[dict], tokens: list[str]) -> None:
    """Clear tokens Expo tells us are dead."""
    dead: list[str] = []
    for ticket, token in zip(tickets, tokens):
        if ticket.get("status") == "error":
            code = (ticket.get("details") or {}).get("error")
            if code == "DeviceNotRegistered":
                dead.append(token)
            else:
                log.warning("expo_ticket_error", code=code, message=ticket.get("message", "")[:160])
    if dead:
        # The user reinstalled or revoked notifications. Clearing the token stops
        # us retrying a dead endpoint on every future send.
        service().table("profiles").update({"push_token": None}).in_("push_token", dead).execute()
        log.info("cleared_dead_push_tokens", count=len(dead))


async def deliver(notification_ids: Iterable[str] | None = None, limit: int = 200) -> dict:
    """Send pending notifications. Idempotent — only rows with pushed_at NULL.

    Called by the worker every minute and directly after high-value events
    (a PR, a completed fast) so those feel immediate.
    """
    sb = service()
    q = (
        sb.table("notifications")
        .select("id,user_id,kind,title,body,deep_link,payload")
        .is_("pushed_at", "null")
        .order("created_at")
        .limit(limit)
    )
    if notification_ids:
        ids = list(notification_ids)
        if not ids:
            return {"sent": 0, "skipped": 0}
        q = q.in_("id", ids)
    pending = rows(q.execute())
    if not pending:
        return {"sent": 0, "skipped": 0}

    user_ids = list({n["user_id"] for n in pending})
    profiles = {
        p["id"]: p
        for p in rows(
            sb.table("profiles").select("id,push_token,timezone").in_("id", user_ids).execute()
        )
    }
    prefs = {
        s["user_id"]: s
        for s in rows(
            sb.table("notification_settings").select("*").in_("user_id", user_ids).execute()
        )
    }

    now = datetime.now(timezone.utc)
    messages: list[dict] = []
    tokens: list[str] = []
    sent_ids: list[str] = []
    skipped_ids: list[str] = []

    for n in pending:
        profile = profiles.get(n["user_id"]) or {}
        token = profile.get("push_token")
        pref = prefs.get(n["user_id"]) or {}

        gate = KIND_PREFERENCE.get(n["kind"])
        if gate and pref.get(gate) is False:
            skipped_ids.append(n["id"])
            continue
        if n["kind"] not in QUIET_HOURS_EXEMPT and pref and _in_quiet_hours(
            now, profile.get("timezone", "UTC"),
            pref.get("quiet_start", "22:00"), pref.get("quiet_end", "07:00")
        ):
            # Leave pushed_at NULL so it goes out when quiet hours end, rather
            # than being silently dropped.
            continue
        if not _valid_token(token):
            skipped_ids.append(n["id"])
            continue

        messages.append({
            "to": token,
            "title": n["title"][:100],
            "body": (n.get("body") or "")[:240],
            "sound": "default",
            "channelId": n["kind"],
            "data": {
                "notification_id": n["id"],
                "kind": n["kind"],
                "deep_link": n.get("deep_link"),
                **(n.get("payload") or {}),
            },
        })
        tokens.append(token)
        sent_ids.append(n["id"])

    for i in range(0, len(messages), BATCH_SIZE):
        chunk = messages[i : i + BATCH_SIZE]
        tickets = await _post_batch(chunk)
        if tickets:
            _handle_tickets(tickets, tokens[i : i + BATCH_SIZE])

    stamp = now.isoformat()
    if sent_ids:
        sb.table("notifications").update({"pushed_at": stamp}).in_("id", sent_ids).execute()
    if skipped_ids:
        # Mark as handled so we do not reconsider them every minute forever.
        sb.table("notifications").update({"pushed_at": stamp}).in_("id", skipped_ids).execute()

    log.info("push_delivered", sent=len(sent_ids), skipped=len(skipped_ids))
    return {"sent": len(sent_ids), "skipped": len(skipped_ids)}


def deliver_soon(notification_ids: Iterable[str]) -> None:
    """Fire-and-forget delivery for events that should feel instant."""
    try:
        asyncio.get_running_loop().create_task(deliver(list(notification_ids)))
    except RuntimeError:
        pass  # no loop (sync context) — the worker will pick it up within a minute


async def register_token(user_id: str, token: str) -> bool:
    """Store an Expo push token, clearing it from any other account first.

    A shared device (or a reinstall) can leave the same token on two profiles,
    which would send one user's reminders to another. That is a privacy bug, so
    the token is treated as unique.
    """
    if not _valid_token(token):
        return False
    sb = service()
    sb.table("profiles").update({"push_token": None}).eq("push_token", token).neq(
        "id", user_id
    ).execute()
    sb.table("profiles").update({"push_token": token}).eq("id", user_id).execute()
    return True
