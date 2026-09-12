from __future__ import annotations

from datetime import datetime, time

from pydantic import Field

from .common import InputBase, Base


class WaterIn(InputBase):
    amount_ml: int = Field(gt=0, le=4000)
    container: str | None = None
    logged_at: datetime | None = None


class WaterDayOut(Base):
    day: str
    goal_ml: int
    total_ml: int
    pct: float
    remaining_ml: int
    logs: list[dict] = []
    streak: int = 0
    on_pace: bool = True


class HydrationSettingsIn(InputBase):
    daily_goal_ml: int | None = Field(None, ge=500, le=8000)
    reminder_enabled: bool | None = None
    reminder_start: time | None = None
    reminder_end: time | None = None
    reminder_every_min: int | None = Field(None, ge=15, le=480)
    sync_apple_health: bool | None = None


class FastStartIn(InputBase):
    protocol: str = Field("16:8", pattern=r"^(16:8|18:6|20:4|omad|5:2|custom)$")
    custom_hours: float | None = Field(None, ge=1, le=72)
    started_at: datetime | None = None


class FastOut(Base):
    id: str
    protocol: str
    status: str
    started_at: datetime
    ends_at: datetime | None = None
    ended_at: datetime | None = None
    target_minutes: int
    elapsed_minutes: int
    remaining_minutes: int
    pct: float
    phase: str
    phase_note: str
    streak: int = 0


class FastingSettingsIn(InputBase):
    protocol: str | None = Field(None, pattern=r"^(16:8|18:6|20:4|omad|5:2|custom)$")
    custom_fast_hours: float | None = Field(None, ge=1, le=72)
    eating_window_start: time | None = None
    auto_start: bool | None = None
    notify_start: bool | None = None
    notify_end: bool | None = None
    notify_halfway: bool | None = None
