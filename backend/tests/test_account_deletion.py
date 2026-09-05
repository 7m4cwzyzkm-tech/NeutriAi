"""Account deletion is required by both stores, and getting it wrong either
leaves someone paying for a deleted account or claims success after a partial
delete."""
from __future__ import annotations

from app.services.account import USER_BUCKETS


def test_every_user_bucket_is_purged():
    """Rows cascade from auth.users; object storage does not. A bucket missing
    from this list means meal photos outlive the account."""
    for bucket in ("meal-photos", "equipment-photos", "recipe-photos", "avatars", "post-media"):
        assert bucket in USER_BUCKETS, f"{bucket} would be orphaned on deletion"


def test_private_buckets_are_covered():
    """These two hold photos of people's food and homes — the ones that most
    need to actually disappear."""
    assert "meal-photos" in USER_BUCKETS
    assert "equipment-photos" in USER_BUCKETS
