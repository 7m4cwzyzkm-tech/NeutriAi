from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .common import Base


class PostIn(Base):
    kind: str = Field("text", pattern="^(meal|workout|recipe|progress|text|milestone)$")
    body: str = Field("", max_length=2000)
    media_paths: list[str] = []
    meal_id: str | None = None
    workout_id: str | None = None
    recipe_id: str | None = None
    visibility: str = Field("public", pattern="^(public|followers|private)$")


class PostOut(Base):
    id: str
    author: dict
    kind: str
    body: str
    media_paths: list[str] = []
    metrics: dict = {}
    like_count: int = 0
    comment_count: int = 0
    liked_by_me: bool = False
    created_at: datetime
    attached: dict | None = None


class CommentIn(Base):
    body: str = Field(min_length=1, max_length=1000)
    parent_id: str | None = None


class NotificationOut(Base):
    id: str
    kind: str
    title: str
    body: str
    deep_link: str | None = None
    actor: dict | None = None
    read_at: datetime | None = None
    created_at: datetime
