"""Supabase access.

Two clients, on purpose:

* ``service()``  — service-role key, bypasses RLS. Used by trusted server code
  that has already checked ownership itself (webhooks, rollups, workers).
* ``as_user(jwt)`` — anon key plus the caller's JWT. RLS applies, so a bug in
  our filter logic still cannot leak another user's rows.

Default to ``as_user``. Reach for ``service`` only when you can say why.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from supabase import Client, create_client

from .config import settings
from .errors import NotFound, UpstreamError


@lru_cache(maxsize=1)
def service() -> Client:
    if not settings.supabase_url or not settings.supabase_service_key:
        raise UpstreamError("Supabase service credentials are not configured.")
    return create_client(settings.supabase_url, settings.supabase_service_key)


def as_user(jwt: str) -> Client:
    client = create_client(settings.supabase_url, settings.supabase_anon_key)
    client.postgrest.auth(jwt)
    return client


# --------------------------------------------------------------------------
# Small helpers so routers do not repeat `.data or []` and None checks.
# --------------------------------------------------------------------------
def rows(resp: Any) -> list[dict]:
    return list(resp.data or [])


def one(resp: Any, what: str = "record") -> dict:
    data = resp.data or []
    if isinstance(data, dict):
        return data
    if not data:
        raise NotFound(f"{what.capitalize()} not found.")
    return data[0]


def maybe_one(resp: Any) -> dict | None:
    data = resp.data or []
    if isinstance(data, dict):
        return data or None
    return data[0] if data else None
