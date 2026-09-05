"""Social feed, follows, comments, notifications."""
from __future__ import annotations

from fastapi import APIRouter, Query, status

from ..db import maybe_one, one, rows, service
from ..deps import CurrentUserDep
from ..errors import AppError, NotFound
from ..models.common import Ok
from ..models.social import CommentIn, NotificationOut, PostIn, PostOut
from ..services.motivation.engine import on_event

router = APIRouter(tags=["social"])


def _hydrate(posts: list[dict], liked_ids: set[str]) -> list[PostOut]:
    return [
        PostOut(
            id=p["id"],
            author=p.get("profiles") or {},
            kind=p["kind"], body=p["body"], media_paths=p.get("media_paths") or [],
            metrics=p.get("metrics") or {},
            like_count=p.get("like_count", 0), comment_count=p.get("comment_count", 0),
            liked_by_me=p["id"] in liked_ids,
            created_at=p["created_at"],
            attached=(p.get("meals") or p.get("workouts") or p.get("recipes")),
        )
        for p in posts
    ]


SELECT = (
    "*, profiles!posts_author_id_fkey(id,handle,display_name,avatar_url), "
    "meals(id,title,kcal,protein_g,photo_path), "
    "workouts(id,title,kind,duration_s,kcal), "
    "recipes(id,title,photo_paths,kcal_per_serving)"
)


@router.post("/posts", response_model=PostOut, status_code=status.HTTP_201_CREATED)
async def create_post(body: PostIn, user: CurrentUserDep):
    """Posting snapshots the referenced metrics, so a later edit to the meal
    doesn't silently rewrite what someone already saw in the feed."""
    metrics: dict = {}
    if body.meal_id:
        m = maybe_one(
            user.sb.table("meals").select("kcal,protein_g,carbs_g,fat_g,title")
            .eq("id", body.meal_id).eq("user_id", user.id).limit(1).execute()
        )
        if not m:
            raise NotFound("Meal not found.")
        metrics = m
    elif body.workout_id:
        w = maybe_one(
            user.sb.table("workouts").select("title,kind,duration_s,kcal")
            .eq("id", body.workout_id).eq("user_id", user.id).limit(1).execute()
        )
        if not w:
            raise NotFound("Workout not found.")
        metrics = w

    post = one(
        user.sb.table("posts").insert({
            "author_id": user.id, "kind": body.kind, "body": body.body,
            "media_paths": body.media_paths, "meal_id": body.meal_id,
            "workout_id": body.workout_id, "recipe_id": body.recipe_id,
            "visibility": body.visibility, "metrics": metrics,
        }).execute()
    )

    # Notify followers. Batched insert; push delivery is a worker's job.
    followers = rows(
        service().table("follows").select("follower_id").eq("followee_id", user.id)
        .limit(500).execute()
    )
    if followers:
        profile = maybe_one(
            service().table("profiles").select("display_name,handle").eq("id", user.id)
            .limit(1).execute()
        ) or {}
        name = profile.get("display_name") or profile.get("handle") or "Someone"
        service().table("notifications").insert([
            {
                "user_id": f["follower_id"], "kind": "social",
                "title": f"{name} posted", "body": body.body[:120],
                "actor_id": user.id, "deep_link": f"nutriai://post/{post['id']}",
            }
            for f in followers
        ]).execute()

    if body.kind == "progress":
        await on_event(user.id, "progress_shared", {})

    fresh = one(user.sb.table("posts").select(SELECT).eq("id", post["id"]).limit(1).execute())
    return _hydrate([fresh], set())[0]


@router.get("/feed", response_model=list[PostOut])
async def feed(
    user: CurrentUserDep,
    scope: str = Query("following", pattern="^(following|discover|mine)$"),
    limit: int = Query(20, le=50),
    offset: int = 0,
):
    sb = user.sb
    q = sb.table("posts").select(SELECT)

    if scope == "mine":
        q = q.eq("author_id", user.id)
    elif scope == "following":
        ids = [
            f["followee_id"] for f in rows(
                sb.table("follows").select("followee_id").eq("follower_id", user.id).execute()
            )
        ] + [user.id]
        q = q.in_("author_id", ids)
    else:
        # Discover: public posts, newest first. A real ranking model would live
        # here; recency is an honest v1 and does not pretend to be more.
        q = q.eq("visibility", "public")

    posts = rows(q.order("created_at", desc=True).range(offset, offset + limit - 1).execute())
    if not posts:
        return []
    liked = {
        l["post_id"] for l in rows(
            sb.table("post_likes").select("post_id").eq("user_id", user.id)
            .in_("post_id", [p["id"] for p in posts]).execute()
        )
    }
    return _hydrate(posts, liked)


@router.delete("/posts/{post_id}", response_model=Ok)
async def delete_post(post_id: str, user: CurrentUserDep):
    user.sb.table("posts").delete().eq("id", post_id).eq("author_id", user.id).execute()
    return Ok(message="Post deleted.")


@router.post("/posts/{post_id}/like", response_model=Ok)
async def like(post_id: str, user: CurrentUserDep):
    user.sb.table("post_likes").upsert(
        {"post_id": post_id, "user_id": user.id}, on_conflict="post_id,user_id"
    ).execute()
    author = maybe_one(
        service().table("posts").select("author_id").eq("id", post_id).limit(1).execute()
    )
    if author and author["author_id"] != user.id:
        service().table("notifications").insert({
            "user_id": author["author_id"], "kind": "social",
            "title": "Someone liked your post", "actor_id": user.id,
            "deep_link": f"nutriai://post/{post_id}",
        }).execute()
    return Ok(message="Liked.")


@router.delete("/posts/{post_id}/like", response_model=Ok)
async def unlike(post_id: str, user: CurrentUserDep):
    user.sb.table("post_likes").delete().eq("post_id", post_id).eq(
        "user_id", user.id
    ).execute()
    return Ok(message="Unliked.")


@router.get("/posts/{post_id}/comments")
async def list_comments(post_id: str, user: CurrentUserDep, limit: int = Query(50, le=200)):
    return rows(
        user.sb.table("comments")
        .select("*, profiles!comments_author_id_fkey(id,handle,display_name,avatar_url)")
        .eq("post_id", post_id).order("created_at").limit(limit).execute()
    )


@router.post("/posts/{post_id}/comments", status_code=201)
async def add_comment(post_id: str, body: CommentIn, user: CurrentUserDep):
    comment = one(
        user.sb.table("comments").insert({
            "post_id": post_id, "author_id": user.id,
            "body": body.body, "parent_id": body.parent_id,
        }).execute()
    )
    author = maybe_one(
        service().table("posts").select("author_id").eq("id", post_id).limit(1).execute()
    )
    if author and author["author_id"] != user.id:
        service().table("notifications").insert({
            "user_id": author["author_id"], "kind": "social",
            "title": "New comment on your post", "body": body.body[:120],
            "actor_id": user.id, "deep_link": f"nutriai://post/{post_id}",
        }).execute()
    return comment


@router.post("/users/{handle}/follow", response_model=Ok)
async def follow(handle: str, user: CurrentUserDep):
    target = maybe_one(
        user.sb.table("profiles").select("id").eq("handle", handle).limit(1).execute()
    )
    if not target:
        raise NotFound("User not found.")
    if target["id"] == user.id:
        raise AppError("You cannot follow yourself.", code="self_follow")
    user.sb.table("follows").upsert(
        {"follower_id": user.id, "followee_id": target["id"]},
        on_conflict="follower_id,followee_id",
    ).execute()
    service().table("notifications").insert({
        "user_id": target["id"], "kind": "social",
        "title": "You have a new follower", "actor_id": user.id,
        "deep_link": f"nutriai://profile/{user.id}",
    }).execute()
    return Ok(message=f"Following {handle}.")


@router.delete("/users/{handle}/follow", response_model=Ok)
async def unfollow(handle: str, user: CurrentUserDep):
    target = maybe_one(
        user.sb.table("profiles").select("id").eq("handle", handle).limit(1).execute()
    )
    if target:
        user.sb.table("follows").delete().eq("follower_id", user.id).eq(
            "followee_id", target["id"]
        ).execute()
    return Ok(message="Unfollowed.")


@router.get("/users/search")
async def search_users(q: str = Query(min_length=2), user: CurrentUserDep = None):
    return rows(
        user.sb.table("profiles").select("id,handle,display_name,avatar_url,bio")
        .ilike("handle", f"%{q}%").limit(20).execute()
    )


@router.get("/notifications", response_model=list[NotificationOut])
async def notifications(user: CurrentUserDep, unread_only: bool = False, limit: int = Query(50, le=200)):
    q = user.sb.table("notifications").select(
        "*, profiles!notifications_actor_id_fkey(id,handle,display_name,avatar_url)"
    ).eq("user_id", user.id)
    if unread_only:
        q = q.is_("read_at", "null")
    return [
        NotificationOut(**{**n, "actor": n.pop("profiles", None)})
        for n in rows(q.order("created_at", desc=True).limit(limit).execute())
    ]


@router.post("/notifications/read", response_model=Ok)
async def mark_read(user: CurrentUserDep, ids: list[str] | None = None):
    q = user.sb.table("notifications").update({"read_at": "now()"}).eq("user_id", user.id)
    if ids:
        q = q.in_("id", ids)
    else:
        q = q.is_("read_at", "null")
    q.execute()
    return Ok(message="Marked as read.")


@router.get("/celebrations")
async def celebrations(user: CurrentUserDep, unseen_only: bool = True):
    q = user.sb.table("celebrations").select("*").eq("user_id", user.id)
    if unseen_only:
        q = q.is_("seen_at", "null")
    return rows(q.order("created_at", desc=True).limit(10).execute())


@router.post("/celebrations/{celebration_id}/seen", response_model=Ok)
async def mark_celebration_seen(celebration_id: str, user: CurrentUserDep):
    user.sb.table("celebrations").update({"seen_at": "now()"}).eq(
        "id", celebration_id
    ).eq("user_id", user.id).execute()
    return Ok(message="Seen.")


@router.get("/motivation")
async def motivation_history(user: CurrentUserDep, limit: int = Query(20, le=100)):
    return rows(
        user.sb.table("motivation_messages").select("*").eq("user_id", user.id)
        .order("created_at", desc=True).limit(limit).execute()
    )
