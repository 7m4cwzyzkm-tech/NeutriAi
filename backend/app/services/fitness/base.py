"""Wearable integration contract.

Five providers, three integration shapes:

* **Apple HealthKit / Samsung Health** — no server API exists. The phone reads
  the store locally and *pushes* to us. Their adapter is push-only.
* **Fitbit / Google Fit** — OAuth 2.0 + REST pull. Full server-side sync.
* **Garmin** — OAuth 1.0a + a push "Health API" that POSTs to a webhook we
  register. Modelled as pull-capable with a webhook ingest path.

Every adapter normalizes into the same ``health_days`` row shape, so the rest of
the app never branches on provider — except in one place: ``PRECEDENCE`` below,
which decides who wins when two devices report the same day.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date

# Apple's on-wrist data is the most trustworthy for HR and calories, then
# Garmin, then Fitbit; Google Fit is usually an aggregator of the others.
PRECEDENCE = ["apple_health", "garmin", "fitbit", "google_fit", "samsung_health", "manual"]


@dataclass(slots=True)
class NormalizedDay:
    day: date
    provider: str
    steps: int | None = None
    distance_m: float | None = None
    floors: int | None = None
    active_kcal: int | None = None
    resting_kcal: int | None = None
    resting_hr: int | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    hrv_ms: float | None = None
    vo2max: float | None = None
    sleep_minutes: int | None = None
    sleep_deep_min: int | None = None
    sleep_rem_min: int | None = None
    sleep_score: int | None = None
    recovery_score: int | None = None
    raw: dict = field(default_factory=dict)

    def to_row(self, user_id: str) -> dict:
        d = {
            "user_id": user_id, "day": self.day.isoformat(), "provider": self.provider,
            "steps": self.steps, "distance_m": self.distance_m, "floors": self.floors,
            "active_kcal": self.active_kcal, "resting_kcal": self.resting_kcal,
            "resting_hr": self.resting_hr, "avg_hr": self.avg_hr, "max_hr": self.max_hr,
            "hrv_ms": self.hrv_ms, "vo2max": self.vo2max,
            "sleep_minutes": self.sleep_minutes, "sleep_deep_min": self.sleep_deep_min,
            "sleep_rem_min": self.sleep_rem_min, "sleep_score": self.sleep_score,
            "recovery_score": self.recovery_score, "raw": self.raw,
        }
        if self.active_kcal is not None or self.resting_kcal is not None:
            d["total_kcal"] = (self.active_kcal or 0) + (self.resting_kcal or 0)
        return d


@dataclass(slots=True)
class NormalizedWorkout:
    external_id: str
    provider: str
    title: str
    kind: str
    started_at: str
    ended_at: str | None = None
    duration_s: int | None = None
    kcal: int | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    hr_zones: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def to_row(self, user_id: str) -> dict:
        return {
            "user_id": user_id, "provider": self.provider, "external_id": self.external_id,
            "title": self.title, "kind": self.kind, "started_at": self.started_at,
            "ended_at": self.ended_at, "duration_s": self.duration_s, "kcal": self.kcal,
            "avg_hr": self.avg_hr, "max_hr": self.max_hr, "hr_zones": self.hr_zones,
        }


class Adapter(abc.ABC):
    """One provider. Subclasses implement whichever half they support."""

    provider: str = "manual"
    supports_pull: bool = False
    supports_push: bool = True
    oauth_version: str | None = None

    # ---- OAuth (pull providers only) ----
    def authorize_url(self, state: str) -> str:
        raise NotImplementedError(f"{self.provider} does not use a server OAuth flow.")

    async def exchange_code(self, code: str, state: str) -> dict:
        raise NotImplementedError

    async def refresh(self, refresh_token: str) -> dict:
        raise NotImplementedError

    # ---- Sync ----
    async def fetch_days(
        self, access_token: str, external_user_id: str | None, start: date, end: date
    ) -> list[NormalizedDay]:
        return []

    async def fetch_workouts(
        self, access_token: str, external_user_id: str | None, start: date, end: date
    ) -> list[NormalizedWorkout]:
        return []

    # ---- Push (phone-side providers) ----
    def normalize_push(self, payload: dict) -> NormalizedDay:
        raise NotImplementedError


def map_activity_kind(name: str) -> str:
    n = (name or "").lower()
    if any(k in n for k in ("run", "jog", "walk", "hike", "cycl", "bike", "swim", "row", "elliptical")):
        return "cardio"
    if any(k in n for k in ("hiit", "interval", "crossfit", "circuit", "bootcamp")):
        return "hiit"
    if any(k in n for k in ("yoga", "pilates", "stretch", "mobility", "foam")):
        return "mobility"
    if any(k in n for k in ("weight", "strength", "lift", "resistance", "gym")):
        return "strength"
    if any(k in n for k in ("calisthenic", "bodyweight")):
        return "calisthenics"
    return "strength"
