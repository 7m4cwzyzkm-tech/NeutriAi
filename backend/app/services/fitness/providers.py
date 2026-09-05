"""The remaining four providers, plus the registry and sync loop."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import structlog

from ...config import settings
from ...errors import AppError, UpstreamError
from .base import Adapter, NormalizedDay, NormalizedWorkout, map_activity_kind
from .fitbit import FitbitAdapter

log = structlog.get_logger()


# ===========================================================================
# Apple HealthKit — push only. The phone reads HKHealthStore and POSTs here.
# ===========================================================================
class AppleHealthAdapter(Adapter):
    provider = "apple_health"
    supports_pull = False
    supports_push = True

    def normalize_push(self, payload: dict) -> NormalizedDay:
        """Payload keys mirror HealthKit identifiers so the Swift side is a
        straight mapping with no invented vocabulary."""
        return NormalizedDay(
            day=date.fromisoformat(str(payload["day"])[:10]),
            provider=self.provider,
            steps=payload.get("stepCount"),
            distance_m=payload.get("distanceWalkingRunning"),
            floors=payload.get("flightsClimbed"),
            active_kcal=payload.get("activeEnergyBurned"),
            resting_kcal=payload.get("basalEnergyBurned"),
            resting_hr=payload.get("restingHeartRate"),
            avg_hr=payload.get("heartRate"),
            max_hr=payload.get("heartRateMax"),
            hrv_ms=payload.get("heartRateVariabilitySDNN"),
            vo2max=payload.get("vo2Max"),
            sleep_minutes=payload.get("sleepAnalysisAsleep"),
            sleep_deep_min=payload.get("sleepAnalysisDeep"),
            sleep_rem_min=payload.get("sleepAnalysisREM"),
            raw={"source": "HealthKit", "device": payload.get("device")},
        )


# ===========================================================================
# Samsung Health — push only, via Health Connect on Android.
# ===========================================================================
class SamsungHealthAdapter(Adapter):
    provider = "samsung_health"
    supports_pull = False
    supports_push = True

    def normalize_push(self, payload: dict) -> NormalizedDay:
        return NormalizedDay(
            day=date.fromisoformat(str(payload["day"])[:10]),
            provider=self.provider,
            steps=payload.get("steps"),
            distance_m=payload.get("distance"),
            active_kcal=payload.get("activeCalories"),
            resting_kcal=payload.get("basalCalories"),
            resting_hr=payload.get("restingHeartRate"),
            avg_hr=payload.get("heartRate"),
            sleep_minutes=payload.get("sleepDuration"),
            raw={"source": "HealthConnect"},
        )


# ===========================================================================
# Google Fit — OAuth 2.0 + the aggregate REST endpoint.
# ===========================================================================
GF_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GF_TOKEN = "https://oauth2.googleapis.com/token"
GF_AGG = "https://www.googleapis.com/fitness/v1/users/me/dataset:aggregate"
GF_SCOPES = [
    "https://www.googleapis.com/auth/fitness.activity.read",
    "https://www.googleapis.com/auth/fitness.heart_rate.read",
    "https://www.googleapis.com/auth/fitness.sleep.read",
    "https://www.googleapis.com/auth/fitness.location.read",
]


class GoogleFitAdapter(Adapter):
    provider = "google_fit"
    supports_pull = True
    supports_push = False
    oauth_version = "2.0"

    def authorize_url(self, state: str) -> str:
        return f"{GF_AUTH}?" + urlencode({
            "client_id": settings.google_fit_client_id,
            "redirect_uri": f"{settings.oauth_redirect_base}/google_fit",
            "response_type": "code",
            "scope": " ".join(GF_SCOPES),
            "access_type": "offline",
            "prompt": "consent",          # required to actually get a refresh token
            "state": state,
        })

    async def exchange_code(self, code: str, state: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(GF_TOKEN, data={
                "code": code,
                "client_id": settings.google_fit_client_id,
                "client_secret": settings.google_fit_client_secret,
                "redirect_uri": f"{settings.oauth_redirect_base}/google_fit",
                "grant_type": "authorization_code",
            })
        if r.status_code >= 400:
            raise UpstreamError(f"Google Fit token exchange failed: {r.text[:200]}")
        b = r.json()
        return {
            "access_token": b["access_token"],
            "refresh_token": b.get("refresh_token"),
            "external_user_id": None,
            "scopes": (b.get("scope") or "").split(),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=int(b.get("expires_in", 3600)))
            ).isoformat(),
        }

    async def refresh(self, refresh_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(GF_TOKEN, data={
                "refresh_token": refresh_token,
                "client_id": settings.google_fit_client_id,
                "client_secret": settings.google_fit_client_secret,
                "grant_type": "refresh_token",
            })
        if r.status_code >= 400:
            raise UpstreamError(f"Google Fit refresh failed: {r.text[:200]}")
        b = r.json()
        return {
            "access_token": b["access_token"],
            "refresh_token": refresh_token,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=int(b.get("expires_in", 3600)))
            ).isoformat(),
        }

    async def fetch_days(
        self, access_token: str, external_user_id: str | None, start: date, end: date
    ) -> list[NormalizedDay]:
        start_ms = int(datetime.combine(start, datetime.min.time()).timestamp() * 1000)
        end_ms = int(datetime.combine(end + timedelta(days=1), datetime.min.time()).timestamp() * 1000)
        body = {
            "aggregateBy": [
                {"dataTypeName": "com.google.step_count.delta"},
                {"dataTypeName": "com.google.calories.expended"},
                {"dataTypeName": "com.google.distance.delta"},
                {"dataTypeName": "com.google.heart_rate.bpm"},
            ],
            "bucketByTime": {"durationMillis": 86_400_000},
            "startTimeMillis": start_ms,
            "endTimeMillis": end_ms,
        }
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(GF_AGG, headers={"Authorization": f"Bearer {access_token}"}, json=body)
        if r.status_code >= 400:
            log.warning("google_fit_aggregate_failed", status=r.status_code, body=r.text[:200])
            return []

        out: list[NormalizedDay] = []
        for bucket in r.json().get("bucket") or []:
            day = date.fromtimestamp(int(bucket["startTimeMillis"]) / 1000)
            nd = NormalizedDay(day=day, provider=self.provider)
            for ds in bucket.get("dataset") or []:
                dtype = ds.get("dataSourceId", "")
                vals = [
                    v for p in ds.get("point") or [] for v in p.get("value") or []
                ]
                if not vals:
                    continue
                if "step_count" in dtype:
                    nd.steps = sum(int(v.get("intVal") or 0) for v in vals)
                elif "calories" in dtype:
                    nd.active_kcal = int(sum(float(v.get("fpVal") or 0) for v in vals))
                elif "distance" in dtype:
                    nd.distance_m = sum(float(v.get("fpVal") or 0) for v in vals)
                elif "heart_rate" in dtype:
                    hrs = [float(v.get("fpVal") or 0) for v in vals if v.get("fpVal")]
                    if hrs:
                        nd.avg_hr = int(sum(hrs) / len(hrs))
                        nd.max_hr = int(max(hrs))
            out.append(nd)
        return out


# ===========================================================================
# Garmin — OAuth 1.0a + push webhooks (the Health API PINGs us, we pull detail).
# ===========================================================================
class GarminAdapter(Adapter):
    provider = "garmin"
    supports_pull = True
    supports_push = True
    oauth_version = "1.0a"

    def authorize_url(self, state: str) -> str:
        # OAuth 1.0a needs a request-token round trip first. Kept explicit so it
        # is obvious this is a two-call flow, not a URL you can just build.
        if not settings.garmin_consumer_key:
            raise AppError("Garmin is not configured.", code="provider_unconfigured")
        return (
            "https://connect.garmin.com/oauthConfirm?"
            + urlencode({"oauth_token": state})
        )

    def normalize_push(self, payload: dict) -> NormalizedDay:
        """Garmin's Health API pushes daily summaries directly."""
        cal = payload.get("calendarDate") or payload.get("day")
        return NormalizedDay(
            day=date.fromisoformat(str(cal)[:10]),
            provider=self.provider,
            steps=payload.get("steps"),
            distance_m=payload.get("distanceInMeters"),
            floors=payload.get("floorsClimbed"),
            active_kcal=payload.get("activeKilocalories"),
            resting_kcal=payload.get("bmrKilocalories"),
            resting_hr=payload.get("restingHeartRateInBeatsPerMinute"),
            avg_hr=payload.get("averageHeartRateInBeatsPerMinute"),
            max_hr=payload.get("maxHeartRateInBeatsPerMinute"),
            vo2max=payload.get("vo2Max"),
            sleep_minutes=(
                int(payload["sleepTimeInSeconds"] / 60) if payload.get("sleepTimeInSeconds") else None
            ),
            recovery_score=payload.get("bodyBatteryChargedValue"),
            raw={"summaryId": payload.get("summaryId")},
        )


# ===========================================================================
# Registry + sync
# ===========================================================================
ADAPTERS: dict[str, Adapter] = {
    "fitbit": FitbitAdapter(),
    "google_fit": GoogleFitAdapter(),
    "garmin": GarminAdapter(),
    "apple_health": AppleHealthAdapter(),
    "samsung_health": SamsungHealthAdapter(),
}


def get_adapter(provider: str) -> Adapter:
    a = ADAPTERS.get(provider)
    if not a:
        raise AppError(f"Unknown provider '{provider}'.", code="unknown_provider")
    return a


async def sync_connection(connection: dict, days_back: int = 7) -> dict:
    """Pull recent data for one connection, refreshing the token if needed."""
    from ...db import service
    from ...security import decrypt, encrypt

    sb = service()
    provider = connection["provider"]
    adapter = get_adapter(provider)
    if not adapter.supports_pull:
        return {"provider": provider, "skipped": "push-only provider"}

    token = decrypt(connection["access_token"])
    expires = connection.get("expires_at")
    if expires and datetime.fromisoformat(str(expires).replace("Z", "+00:00")) <= datetime.now(
        timezone.utc
    ) + timedelta(minutes=5):
        fresh = await adapter.refresh(decrypt(connection["refresh_token"]))
        token = fresh["access_token"]
        sb.table("device_connections").update({
            "access_token": encrypt(fresh["access_token"]),
            "refresh_token": encrypt(fresh.get("refresh_token") or ""),
            "expires_at": fresh.get("expires_at"),
            "status": "connected", "error": None,
        }).eq("id", connection["id"]).execute()

    end = date.today()
    start = end - timedelta(days=days_back)
    user_id = connection["user_id"]

    try:
        days = await adapter.fetch_days(token, connection.get("external_user_id"), start, end)
        workouts = await adapter.fetch_workouts(
            token, connection.get("external_user_id"), start, end
        )
    except Exception as exc:  # noqa: BLE001
        sb.table("device_connections").update(
            {"status": "error", "error": str(exc)[:400]}
        ).eq("id", connection["id"]).execute()
        raise

    if days:
        sb.table("health_days").upsert(
            [d.to_row(user_id) for d in days], on_conflict="user_id,day,provider"
        ).execute()
    if workouts:
        sb.table("workouts").upsert(
            [w.to_row(user_id) for w in workouts], on_conflict="user_id,provider,external_id"
        ).execute()

    sb.table("device_connections").update({
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "status": "connected", "error": None,
    }).eq("id", connection["id"]).execute()

    return {"provider": provider, "days": len(days), "workouts": len(workouts)}
