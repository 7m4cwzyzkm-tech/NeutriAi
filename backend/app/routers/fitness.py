"""Workouts, plans, equipment scanning, wearable integrations."""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request, Response, status

from ..db import maybe_one, one, rows, service
from ..deps import AiScanDep, CurrentUserDep, ProDep
from ..errors import AppError, NotFound
from ..models.common import Ok
from ..models.fitness import (
    EquipmentScanIn, EquipmentScanOut, HealthDayIn, PlanOut, PlanRequest,
    WorkoutIn, WorkoutOut,
)
from ..security import decrypt, encrypt
from ..services.ai import coach
from ..services.fitness.base import PRECEDENCE
from ..services.fitness.providers import ADAPTERS, get_adapter, sync_connection
from ..services.motivation.engine import celebrate, check_daily_goals, on_event
from ..services.nutrition.macros import age_from

router = APIRouter(tags=["fitness"])


# ===========================================================================
# Workouts
# ===========================================================================
@router.post("/workouts", response_model=WorkoutOut, status_code=201)
async def log_workout(body: WorkoutIn, user: CurrentUserDep):
    started = body.started_at or datetime.now(timezone.utc)
    ended = body.ended_at
    duration = body.duration_s or (int((ended - started).total_seconds()) if ended else None)

    profile = maybe_one(
        user.sb.table("profiles").select("birth_date,weight_kg").eq("id", user.id)
        .limit(1).execute()
    ) or {}

    workout = one(
        user.sb.table("workouts").insert({
            "user_id": user.id, "provider": "manual", "title": body.title,
            "kind": body.kind, "started_at": started.isoformat(),
            "ended_at": ended.isoformat() if ended else None,
            "duration_s": duration, "kcal": body.kcal, "avg_hr": body.avg_hr,
            "max_hr": body.max_hr,
            "hr_zones": coach.hr_zones(
                body.avg_hr, body.max_hr, age_from(profile.get("birth_date")), duration or 0
            ),
            "perceived_effort": body.perceived_effort, "notes": body.notes,
            "plan_day_id": body.plan_day_id,
        }).execute()
    )

    sets_payload = [
        {
            "workout_id": workout["id"], "exercise_name": s.exercise_name,
            "set_index": s.set_index, "reps": s.reps, "weight_kg": s.weight_kg,
            "duration_s": s.duration_s, "distance_m": s.distance_m, "rpe": s.rpe,
            "rest_s": s.rest_s, "is_warmup": s.is_warmup,
        }
        for s in body.sets
    ]
    if sets_payload:
        # Link to the library where the slug matches, so PR tracking is stable
        # even if the user renames the exercise in the UI.
        slugs = {s.exercise_slug for s in body.sets if s.exercise_slug}
        lib = {
            e["slug"]: e["id"] for e in rows(
                service().table("exercises").select("id,slug")
                .in_("slug", list(slugs) or ["__none__"]).execute()
            )
        }
        for payload, s in zip(sets_payload, body.sets):
            if s.exercise_slug and s.exercise_slug in lib:
                payload["exercise_id"] = lib[s.exercise_slug]
        user.sb.table("workout_sets").insert(sets_payload).execute()

    prs = coach.detect_prs(
        user.id, workout["id"],
        [{**p, "exercise_slug": s.exercise_slug} for p, s in zip(sets_payload, body.sets)],
    )

    if body.plan_day_id:
        service().table("plan_days").update(
            {"completed_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", body.plan_day_id).execute()

    service().rpc(
        "bump_streak",
        {"p_user": user.id, "p_kind": "workout", "p_day": started.date().isoformat()},
    ).execute()

    if prs:
        best = prs[0]
        await on_event(user.id, "pr_hit", {
            "exercise": best["exercise_slug"].replace("-", " ").title(),
            "value": best["value"], "unit": best["unit"],
        })
        await celebrate(user.id, "pr", "New personal record",
                        f"{best['exercise_slug'].replace('-', ' ').title()} "
                        f"{best['value']}{best['unit']}", {"prs": prs})
    else:
        await on_event(user.id, "workout_done",
                       {"minutes": int((duration or 0) / 60), "workouts": 1})
    await check_daily_goals(user.id, started.date().isoformat())

    fresh = one(
        user.sb.table("workouts").select("*, workout_sets(*)").eq("id", workout["id"])
        .limit(1).execute()
    )
    return WorkoutOut(**{**fresh, "sets": fresh.pop("workout_sets", []), "new_prs": prs})


@router.get("/workouts", response_model=list[WorkoutOut])
async def list_workouts(user: CurrentUserDep, limit: int = Query(30, le=200)):
    result = rows(
        user.sb.table("workouts").select("*, workout_sets(*)").eq("user_id", user.id)
        .order("started_at", desc=True).limit(limit).execute()
    )
    return [WorkoutOut(**{**w, "sets": w.pop("workout_sets", [])}) for w in result]


@router.delete("/workouts/{workout_id}", response_model=Ok)
async def delete_workout(workout_id: str, user: CurrentUserDep):
    user.sb.table("workouts").delete().eq("id", workout_id).eq("user_id", user.id).execute()
    return Ok(message="Workout deleted.")


@router.get("/personal-records")
async def personal_records(user: CurrentUserDep):
    """Best value per exercise/metric — the PR board."""
    all_prs = rows(
        user.sb.table("personal_records").select("*").eq("user_id", user.id)
        .order("achieved_at", desc=True).limit(500).execute()
    )
    best: dict[str, dict] = {}
    for pr in all_prs:
        key = f"{pr['exercise_slug']}:{pr['metric']}"
        if key not in best or float(pr["value"]) > float(best[key]["value"]):
            best[key] = pr
    return sorted(best.values(), key=lambda p: p["achieved_at"], reverse=True)


@router.get("/exercises")
async def exercise_library(
    equipment: str | None = Query(None, description="Comma-separated equipment filter"),
    kind: str | None = None,
):
    q = service().table("exercises").select("*")
    if kind:
        q = q.eq("kind", kind)
    result = rows(q.execute())
    if equipment:
        have = set(equipment.split(",")) | {"none"}
        result = [e for e in result if set(e.get("equipment") or ["none"]) & have]
    return sorted(result, key=lambda e: (e.get("difficulty", 3), e["name"]))


# ===========================================================================
# Equipment scanning + AI plans
# ===========================================================================
@router.post("/equipment/scan", response_model=EquipmentScanOut, status_code=201)
async def scan_equipment(body: EquipmentScanIn, user: CurrentUserDep, _q: AiScanDep):
    result = await coach.scan_equipment(user.id, body.image_paths, body.space_note)
    row = one(
        service().table("equipment_scans").insert({
            "user_id": user.id, "image_paths": body.image_paths,
            "detected": result["detected"], "equipment": result["equipment"],
            "space_note": result["space_note"], "confidence": result["confidence"],
        }).execute()
    )
    return EquipmentScanOut(
        id=row["id"], equipment=result["equipment"], detected=result["detected"],
        confidence=result["confidence"], fallback_to_calisthenics=result["fallback"],
    )


@router.post("/plans", response_model=PlanOut, status_code=201)
async def create_plan(body: PlanRequest, user: CurrentUserDep, _pro: ProDep):
    equipment = body.equipment
    if body.equipment_scan_id and not equipment:
        scan = maybe_one(
            user.sb.table("equipment_scans").select("equipment").eq("id", body.equipment_scan_id)
            .eq("user_id", user.id).limit(1).execute()
        )
        if not scan:
            raise NotFound("Equipment scan not found.")
        equipment = scan["equipment"]
    equipment = equipment or ["none"]

    profile = maybe_one(
        user.sb.table("profiles").select("*").eq("id", user.id).limit(1).execute()
    ) or {}
    history = rows(
        user.sb.table("workouts").select("id,kind,started_at").eq("user_id", user.id)
        .gte("started_at", (datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        .limit(60).execute()
    )

    plan = await coach.generate_plan(user.id, body, equipment, profile, history)

    # Deactivate the previous plan so the app always has exactly one current one.
    service().table("training_plans").update({"is_active": False}).eq(
        "user_id", user.id
    ).eq("is_active", True).execute()

    row = one(
        service().table("training_plans").insert({
            "user_id": user.id, "equipment_scan_id": body.equipment_scan_id,
            "name": plan.get("name", "Training plan")[:120], "goal": body.goal,
            "days_per_week": body.days_per_week, "weeks": body.weeks,
            "equipment": equipment,
            "is_calisthenics_fallback": plan.get("is_calisthenics_fallback", False),
            "safety_notes": plan.get("safety_notes", [])[:12],
            "progression": plan.get("progression", {}),
            "is_active": True,
        }).execute()
    )

    days = [
        {
            "plan_id": row["id"],
            "week_index": int(d.get("week_index", 1)),
            "day_index": int(d.get("day_index", i + 1)),
            "title": str(d.get("title", f"Day {i + 1}"))[:120],
            "kind": d.get("kind", "strength"),
            "est_minutes": int(d.get("est_minutes", body.session_minutes)),
            "blocks": d.get("blocks", []),
        }
        for i, d in enumerate(plan.get("days", []))
    ]
    if days:
        service().table("plan_days").upsert(
            days, on_conflict="plan_id,week_index,day_index"
        ).execute()

    return PlanOut(**{
        **row,
        "days": rows(
            service().table("plan_days").select("*").eq("plan_id", row["id"])
            .order("week_index").order("day_index").execute()
        ),
    })


@router.get("/plans/current", response_model=PlanOut | None)
async def current_plan(user: CurrentUserDep):
    plan = maybe_one(
        user.sb.table("training_plans").select("*").eq("user_id", user.id)
        .eq("is_active", True).order("created_at", desc=True).limit(1).execute()
    )
    if not plan:
        return None
    return PlanOut(**{
        **plan,
        "days": rows(
            user.sb.table("plan_days").select("*").eq("plan_id", plan["id"])
            .order("week_index").order("day_index").execute()
        ),
    })


# ===========================================================================
# Wearable integrations
# ===========================================================================
@router.get("/integrations")
async def list_integrations(user: CurrentUserDep):
    connected = {
        c["provider"]: c for c in rows(
            user.sb.table("device_connections")
            .select("provider,status,last_sync_at,scopes,error")
            .eq("user_id", user.id).execute()
        )
    }
    return [
        {
            "provider": name,
            "supports_pull": a.supports_pull,
            "supports_push": a.supports_push,
            "oauth_version": a.oauth_version,
            "connected": name in connected,
            **({k: v for k, v in connected[name].items() if k != "provider"} if name in connected else {}),
        }
        for name, a in ADAPTERS.items()
    ]


@router.get("/integrations/{provider}/connect")
async def connect(provider: str, user: CurrentUserDep):
    adapter = get_adapter(provider)
    if not adapter.supports_pull:
        return {
            "flow": "device_push",
            "message": (
                f"{provider} has no server API. Grant permission in the app and the "
                "phone will push data to POST /v1/health/push."
            ),
        }
    # State carries the user id, signed by unguessability rather than a secret;
    # it is single-use and checked against the row we write here.
    state = f"{user.id}:{secrets.token_urlsafe(24)}"
    service().table("device_connections").upsert({
        "user_id": user.id, "provider": provider, "status": "error",
        "error": "awaiting_oauth", "sync_cursor": state,
    }, on_conflict="user_id,provider").execute()
    return {"flow": "oauth", "authorize_url": adapter.authorize_url(state), "state": state}


@router.get("/integrations/callback/{provider}", include_in_schema=False)
async def oauth_callback(provider: str, code: str, state: str):
    adapter = get_adapter(provider)
    user_id = state.split(":", 1)[0]
    pending = maybe_one(
        service().table("device_connections").select("*").eq("user_id", user_id)
        .eq("provider", provider).eq("sync_cursor", state).limit(1).execute()
    )
    if not pending:
        raise AppError("This authorization link is no longer valid.", code="bad_oauth_state")

    tokens = await adapter.exchange_code(code, state)
    service().table("device_connections").update({
        "access_token": encrypt(tokens["access_token"]),
        "refresh_token": encrypt(tokens.get("refresh_token") or ""),
        "external_user_id": tokens.get("external_user_id"),
        "scopes": tokens.get("scopes", []),
        "expires_at": tokens.get("expires_at"),
        "status": "connected", "error": None, "sync_cursor": None,
    }).eq("id", pending["id"]).execute()

    # Deep-link back into the app rather than leaving a blank browser tab.
    return Response(
        content=f'<html><body><script>location.replace("neutriai://integrations/{provider}/connected")'
                f'</script><p>Connected. You can close this window.</p></body></html>',
        media_type="text/html",
    )


@router.post("/integrations/{provider}/sync")
async def sync_now(provider: str, user: CurrentUserDep, days_back: int = Query(7, le=90)):
    conn = maybe_one(
        service().table("device_connections").select("*").eq("user_id", user.id)
        .eq("provider", provider).limit(1).execute()
    )
    if not conn or not conn.get("access_token"):
        raise NotFound(f"{provider} is not connected.")
    return await sync_connection(conn, days_back)


@router.delete("/integrations/{provider}", response_model=Ok)
async def disconnect(provider: str, user: CurrentUserDep):
    service().table("device_connections").delete().eq("user_id", user.id).eq(
        "provider", provider
    ).execute()
    return Ok(message=f"{provider} disconnected.")


@router.post("/health/push", status_code=202)
async def push_health(days: list[HealthDayIn], user: CurrentUserDep):
    """Ingest point for HealthKit / Health Connect / Garmin webhooks.

    The phone sends whole days; we upsert on (user, day, provider) so re-sending
    a day is free and ordering does not matter.
    """
    if not days:
        return {"ingested": 0}
    payload = []
    for d in days:
        adapter = get_adapter(d.provider)
        try:
            nd = adapter.normalize_push({**d.model_dump(), **(d.raw or {})})
        except NotImplementedError:
            nd = None
        row = nd.to_row(user.id) if nd else {
            **d.model_dump(exclude={"raw"}), "user_id": user.id,
            "day": d.day.isoformat(), "raw": d.raw,
        }
        if d.active_kcal is not None or d.resting_kcal is not None:
            row["total_kcal"] = (d.active_kcal or 0) + (d.resting_kcal or 0)
        payload.append(row)

    service().table("health_days").upsert(payload, on_conflict="user_id,day,provider").execute()
    await check_daily_goals(user.id)
    return {"ingested": len(payload), "precedence": PRECEDENCE}


@router.get("/health")
async def health_days(user: CurrentUserDep, days: int = Query(14, le=180)):
    since = (date.today() - timedelta(days=days)).isoformat()
    result = rows(
        user.sb.table("health_days").select("*").eq("user_id", user.id)
        .gte("day", since).order("day", desc=True).execute()
    )
    # Collapse to one row per day using the provider precedence order.
    rank = {p: i for i, p in enumerate(PRECEDENCE)}
    best: dict[str, dict] = {}
    for r in result:
        d = str(r["day"])
        if d not in best or rank.get(r["provider"], 99) < rank.get(best[d]["provider"], 99):
            best[d] = r
    return sorted(best.values(), key=lambda r: str(r["day"]), reverse=True)
