"""Profile bootstrap.

Every user-scoped table in the schema has a foreign key to ``profiles(id)``,
not to ``auth.users``. Supabase creates the auth user at signup; nothing
creates the profile. So the profile row is a precondition for essentially
every write the API makes, and it has to exist before the first one -- not
merely by the time someone happens to call ``GET /me``.

This used to live inline in the profiles router, which meant the row was
created only if the client called that endpoint first. Any other route hit
first -- a scan, a water log, an entitlement lookup -- failed on a foreign
key violation that surfaced to the phone as a generic 500 with nothing
actionable in it.

Two situations produce "no profile", and they need opposite responses:

  a) first call after signup -- create it.
  b) the account was deleted -- the JWT is still cryptographically valid
     (correct signature, not yet expired, up to an hour) but the auth user
     is gone. Creating a profile would violate profiles.id -> auth.users(id).
     The client needs a 401 so it signs out, not a 500 it will retry forever.

SQLSTATE 23503 (foreign key violation) is what separates them.
"""
from __future__ import annotations

import structlog

from ..db import maybe_one, service
from ..errors import Unauthorized

log = structlog.get_logger()

# Profiles are never deleted without the auth user going too, so "this id has
# a profile" is safe to remember for the life of the process. Bounded because
# an unbounded set keyed by user id is a slow memory leak on a busy instance.
_KNOWN: set[str] = set()
_KNOWN_MAX = 50_000


def handle_for(user_id: str, email: str | None) -> str:
    """A URL-safe starting handle. Uniqueness is enforced by the database."""
    base = (email or f"user{user_id[:8]}").split("@")[0][:20]
    cleaned = "".join(c for c in base if c.isalnum() or c in "_.")
    return cleaned or f"u{user_id[:8]}"


def _is_fk_violation(exc: Exception) -> bool:
    message = str(exc).lower()
    return "23503" in message or "foreign key" in message or "auth.users" in message


def ensure_profile(user_id: str, email: str | None = None) -> dict:
    """Return the caller's profile row, creating it on first sight.

    Raises Unauthorized if the auth user no longer exists.
    """
    sb = service()
    existing = maybe_one(
        sb.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    )
    if existing:
        _remember(user_id)
        return existing

    handle = handle_for(user_id, email)
    # Two attempts: the plain handle, then one disambiguated by user id.
    # A third collision is vanishingly unlikely and the error is honest.
    last: Exception | None = None
    for candidate in (handle, f"{handle}{user_id[:4]}"):
        try:
            created = sb.table("profiles").insert(
                {"id": user_id, "handle": candidate, "display_name": handle}
            ).execute()
            _remember(user_id)
            log.info("profile_created", user_id=user_id, handle=candidate)
            return created.data[0]
        except Exception as exc:  # noqa: BLE001
            if _is_fk_violation(exc):
                raise Unauthorized(
                    "This account no longer exists. Please sign in again."
                ) from exc

            # The insert can also lose a race. The app opens several requests
            # at once on launch, so two of them can both find no profile and
            # both insert; one wins and the other gets a duplicate key on
            # profiles.id. That is success, not an error -- and it must not be
            # mistaken for a handle collision, because retrying with a new
            # handle hits the same primary key and fails again.
            raced = maybe_one(
                sb.table("profiles").select("*").eq("id", user_id).limit(1).execute()
            )
            if raced:
                _remember(user_id)
                return raced

            last = exc  # genuinely a handle collision; try the next candidate

    raise last if last else RuntimeError("profile insert failed with no error")


def ensure_profile_cached(user_id: str, email: str | None = None) -> None:
    """Same guarantee, but skips the round trip for ids seen this process.

    Used on the hot path (every authenticated request). The cache can go stale
    in exactly one way: the account is deleted while this process keeps
    running. That user's remaining writes then fail on the foreign key until
    their token expires, which is the same behaviour as before this existed --
    and their token is rejected by the deleted-account path on any route that
    reads the profile.
    """
    if user_id in _KNOWN:
        return
    ensure_profile(user_id, email)


def _remember(user_id: str) -> None:
    if len(_KNOWN) >= _KNOWN_MAX:
        _KNOWN.clear()
    _KNOWN.add(user_id)


def forget(user_id: str) -> None:
    """Drop a cached id -- called on account deletion so a re-signup with the
    same id (or a stale token) is re-checked rather than assumed present."""
    _KNOWN.discard(user_id)
