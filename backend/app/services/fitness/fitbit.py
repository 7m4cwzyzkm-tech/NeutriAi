"""Fitbit Web API adapter — the reference full OAuth 2.0 + pull implementation.

The other pull providers (Google Fit, Garmin) follow the same shape; this one is
implemented end to end because it is the most commonly used and its API is the
most representative.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import structlog

from ...config import settings
from ...errors import UpstreamError
from .base import Adapter, NormalizedDay, NormalizedWorkout, map_activity_kind

log = structlog.get_logger()

AUTH = "https://www.fitbit.com/oauth2/authorize"
TOKEN = "https://api.fitbit.com/oauth2/token"
API = "https://api.fitbit.com/1"
SCOPES = ["activity", "heartrate", "profile", "sleep", "weight", "nutrition"]


class FitbitAdapter(Adapter):
    provider = "fitbit"
    supports_pull = True
    supports_push = False
    oauth_version = "2.0"

    # ---------------------------------------------------------------- OAuth
    def authorize_url(self, state: str) -> str:
        return f"{AUTH}?" + urlencode({
            "client_id": settings.fitbit_client_id,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "redirect_uri": f"{settings.oauth_redirect_base}/fitbit",
            "state": state,
            "prompt": "consent",
        })

    def _basic(self) -> str:
        raw = f"{settings.fitbit_client_id}:{settings.fitbit_client_secret}".encode()
        return base64.b64encode(raw).decode()

    async def exchange_code(self, code: str, state: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                TOKEN,
                headers={"Authorization": f"Basic {self._basic()}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": f"{settings.oauth_redirect_base}/fitbit",
                    "client_id": settings.fitbit_client_id,
                },
            )
        if r.status_code >= 400:
            raise UpstreamError(f"Fitbit token exchange failed: {r.text[:200]}")
        body = r.json()
        return {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token"),
            "external_user_id": body.get("user_id"),
            "scopes": (body.get("scope") or "").split(),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=int(body.get("expires_in", 28800)))
            ).isoformat(),
        }

    async def refresh(self, refresh_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                TOKEN,
                headers={"Authorization": f"Basic {self._basic()}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            )
        if r.status_code >= 400:
            raise UpstreamError(f"Fitbit token refresh failed: {r.text[:200]}")
        body = r.json()
        return {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token", refresh_token),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=int(body.get("expires_in", 28800)))
            ).isoformat(),
        }

    # ----------------------------------------------------------------- Pull
    async def fetch_days(
        self, access_token: str, external_user_id: str | None, start: date, end: date
    ) -> list[NormalizedDay]:
        h = {"Authorization": f"Bearer {access_token}", "Accept-Language": "en_US"}
        s, e = start.isoformat(), end.isoformat()
        out: dict[str, NormalizedDay] = {}

        async with httpx.AsyncClient(timeout=20, headers=h) as c:
            # Fitbit's time-series endpoints are one metric per call but cover a
            # whole range, which is far cheaper than one call per day.
            async def series(path: str) -> list[dict]:
                r = await c.get(f"{API}/user/-/{path}/date/{s}/{e}.json")
                if r.status_code >= 400:
                    log.warning("fitbit_series_failed", path=path, status=r.status_code)
                    return []
                body = r.json()
                return next((v for k, v in body.items() if isinstance(v, list)), [])

            for key, entries in {
                "steps": await series("activities/steps"),
                "calories": await series("activities/calories"),
                "distance": await series("activities/distance"),
                "floors": await series("activities/floors"),
                "resting_hr": await series("activities/heart"),
            }.items():
                for entry in entries:
                    d = entry.get("dateTime")
                    if not d:
                        continue
                    nd = out.setdefault(
                        d, NormalizedDay(day=date.fromisoformat(d), provider=self.provider)
                    )
                    try:
                        if key == "steps":
                            nd.steps = int(float(entry["value"]))
                        elif key == "calories":
                            nd.active_kcal = int(float(entry["value"]))
                        elif key == "distance":
                            nd.distance_m = float(entry["value"]) * 1000
                        elif key == "floors":
                            nd.floors = int(float(entry["value"]))
                        elif key == "resting_hr":
                            v = entry.get("value") or {}
                            if isinstance(v, dict) and v.get("restingHeartRate"):
                                nd.resting_hr = int(v["restingHeartRate"])
                    except (TypeError, ValueError):
                        continue

            # Sleep lives on a different API version.
            r = await c.get(f"https://api.fitbit.com/1.2/user/-/sleep/date/{s}/{e}.json")
            if r.status_code < 400:
                for rec in r.json().get("sleep") or []:
                    d = (rec.get("dateOfSleep") or "")[:10]
                    if not d:
                        continue
                    nd = out.setdefault(
                        d, NormalizedDay(day=date.fromisoformat(d), provider=self.provider)
                    )
                    nd.sleep_minutes = int(rec.get("minutesAsleep") or 0)
                    levels = ((rec.get("levels") or {}).get("summary") or {})
                    nd.sleep_deep_min = int((levels.get("deep") or {}).get("minutes") or 0)
                    nd.sleep_rem_min = int((levels.get("rem") or {}).get("minutes") or 0)
                    nd.sleep_score = int(rec.get("efficiency") or 0) or None

        return list(out.values())

    async def fetch_workouts(
        self, access_token: str, external_user_id: str | None, start: date, end: date
    ) -> list[NormalizedWorkout]:
        h = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=20, headers=h) as c:
            r = await c.get(
                f"{API}/user/-/activities/list.json",
                params={"afterDate": start.isoformat(), "sort": "asc",
                        "offset": 0, "limit": 100},
            )
        if r.status_code >= 400:
            return []
        out = []
        for a in r.json().get("activities") or []:
            hr_zones = {
                f"z{i + 1}": int(z.get("minutes", 0)) * 60
                for i, z in enumerate(a.get("heartRateZones") or [])
            }
            out.append(NormalizedWorkout(
                external_id=str(a.get("logId")),
                provider=self.provider,
                title=a.get("activityName") or "Workout",
                kind=map_activity_kind(a.get("activityName", "")),
                started_at=a.get("startTime"),
                duration_s=int((a.get("duration") or 0) / 1000),
                kcal=int(a.get("calories") or 0) or None,
                avg_hr=int(a.get("averageHeartRate") or 0) or None,
                hr_zones=hr_zones,
                raw={"logType": a.get("logType")},
            ))
        return out
