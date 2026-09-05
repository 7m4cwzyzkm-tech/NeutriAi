"""Push delivery rules.

The bugs this guards against are the embarrassing kind: waking someone at 3am,
sending one user's reminders to another's phone, or retrying a dead token
forever.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.push import (
    KIND_PREFERENCE, QUIET_HOURS_EXEMPT, _in_quiet_hours, _valid_token,
)


@pytest.mark.parametrize(
    "token,ok",
    [
        ("ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]", True),
        ("ExpoPushToken[yyyyyyyyyyyyyyyyyyyyyy]", True),
        ("fcm-raw-token-abc123", False),
        ("", False),
        (None, False),
    ],
)
def test_only_expo_tokens_accepted(token, ok):
    assert _valid_token(token) is ok


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 4, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "hour,quiet",
    [(23, True), (2, True), (6, True), (7, False), (12, False), (21, False), (22, True)],
)
def test_quiet_hours_wrap_midnight(hour, quiet):
    """22:00–07:00 spans midnight, which naive comparisons get wrong."""
    assert _in_quiet_hours(_at(hour), "UTC", "22:00", "07:00") is quiet


def test_quiet_hours_use_the_users_timezone():
    """18:00 UTC is 11:00 in Los Angeles — not quiet — but 03:00 in Tokyo."""
    utc_6pm = _at(18)
    assert _in_quiet_hours(utc_6pm, "America/Los_Angeles", "22:00", "07:00") is False
    assert _in_quiet_hours(utc_6pm, "Asia/Tokyo", "22:00", "07:00") is True


def test_unknown_timezone_falls_back_to_utc():
    assert _in_quiet_hours(_at(2), "Mars/Olympus_Mons", "22:00", "07:00") is True


def test_non_wrapping_quiet_hours():
    """A daytime quiet window (13:00–15:00) must also work."""
    assert _in_quiet_hours(_at(14), "UTC", "13:00", "15:00") is True
    assert _in_quiet_hours(_at(16), "UTC", "13:00", "15:00") is False


def test_celebrations_ignore_quiet_hours():
    """A confetti animation the user triggered themselves is not a 3am alarm —
    it fires when they hit the goal, whenever that is."""
    assert "celebration" in QUIET_HOURS_EXEMPT
    assert "social" not in QUIET_HOURS_EXEMPT
    assert "motivation" not in QUIET_HOURS_EXEMPT


def test_billing_and_system_messages_are_never_preference_gated():
    """A user who muted motivation must still hear that their card failed."""
    assert KIND_PREFERENCE["system"] is None
    assert KIND_PREFERENCE["alert"] is None
    assert KIND_PREFERENCE["motivation"] == "motivation"
    assert KIND_PREFERENCE["social"] == "social"
