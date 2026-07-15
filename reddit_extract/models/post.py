"""Normalized Reddit post as harvested from a rendered listing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


@dataclass(frozen=True)
class Post:
    """
    One post card harvested from a listing page.

    Attributes:
        id: Reddit's post fullname/ID.
        type: Reddit's raw ``post-type`` attribute (``image``, ``gallery``, ``video``, ``gif``, ``link``, ``text``, ``crosspost``, ``poll``, ...).
        permalink: Path or URL of the post's comment page.
        content_href: The card's linked content (often the media URL itself).
        author: Post author's username.
        title: Post title.
        subreddit: Subreddit name, without the ``r/`` prefix.
        domain: Domain of the linked content.
        created_raw: Raw creation timestamp as harvested (epoch ms or ISO text).
        packaged_media_json: Raw ``packaged-media-json`` attribute for videos, when present.
        raw: The full harvested attribute dict, kept for custom handlers.
    """

    id: str
    type: str | None = None
    permalink: str | None = None
    content_href: str | None = None
    author: str | None = None
    title: str | None = None
    subreddit: str | None = None
    domain: str | None = None
    created_raw: str | None = None
    packaged_media_json: str | None = field(default=None, repr=False)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_harvest(cls, data: Mapping[str, Any]) -> "Post":
        """
        Build a Post from one harvested post's raw attribute dict.

        Args:
            data: Attribute mapping produced by the listing-harvest JS (modern or legacy). The full mapping is retained on ``raw``.

        Returns:
            A normalized Post. The ``r/`` prefix is stripped from the subreddit name.
        """
        sub = (data.get("subreddit") or "").strip()
        if sub.lower().startswith("r/"):
            sub = sub[2:]
        return cls(
            id=data.get("id") or "",
            type=data.get("type"),
            permalink=data.get("permalink"),
            content_href=data.get("content_href"),
            author=data.get("author"),
            title=data.get("title"),
            subreddit=sub or None,
            domain=data.get("domain"),
            created_raw=data.get("created"),
            packaged_media_json=data.get("packaged_media"),
            raw=dict(data),
        )

    @property
    def created(self) -> str | None:
        """Post creation time as an ISO-8601 string (when available)."""
        if not self.created_raw:
            return None
        try:
            ms = int(self.created_raw)
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
        except (ValueError, TypeError):
            return None

    @property
    def url(self) -> str | None:
        """Absolute permalink URL of the post."""
        if not self.permalink:
            return None
        if self.permalink.startswith("http"):
            return self.permalink
        return "https://www.reddit.com" + self.permalink
