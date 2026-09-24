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

import httpx
from postgrest.constants import DEFAULT_POSTGREST_CLIENT_TIMEOUT
from supabase import Client, ClientOptions, create_client

from .config import settings
from .errors import NotFound, UpstreamError


def _options() -> ClientOptions:
    """Client options carrying our own httpx client.

    Without one, supabase-py builds its own httpx clients and passes
    `timeout=`/`verify=` into postgrest and storage, which now warn that
    those kwargs are deprecated in favour of configuring the http client.
    This is that: the same settings the library used itself -- postgrest's
    own default timeout, TLS verification on, redirects followed, HTTP/2 --
    set on a client we hand in.

    A NEW httpx client per Supabase client, never a shared one, so each
    client stays exactly as isolated as before. Nothing is lost by it:
    postgrest, storage, auth and functions send their URL and headers
    (including the per-user JWT from `.auth()`) with each request, never on
    the session. One behavioural note: storage and functions now share this
    client's timeout (postgrest's default) instead of their own shorter
    defaults.
    """
    return ClientOptions(
        httpx_client=httpx.Client(
            timeout=DEFAULT_POSTGREST_CLIENT_TIMEOUT,
            verify=True,
            follow_redirects=True,
            http2=True,
        ),
    )


@lru_cache(maxsize=1)
def service() -> Client:
    if not settings.supabase_url or not settings.supabase_service_key:
        raise UpstreamError("Supabase service credentials are not configured.")
    return create_client(
        settings.supabase_url, settings.supabase_service_key, options=_options(),
    )


def as_user(jwt: str) -> Client:
    client = create_client(
        settings.supabase_url, settings.supabase_anon_key, options=_options(),
    )
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
