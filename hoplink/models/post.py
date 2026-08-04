"""Normalized Reddit post as harvested from a rendered listing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def _to_int(value: Any) -> int | None:
    """Parse a harvested numeric attribute, returning None when it is absent or not a number."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


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
        created_raw: Raw creation timestamp as harvested (ISO text on the modern UI, epoch ms on the legacy one).
        score: Net upvotes, or None when the listing doesn't report a score.
        comment_count: Number of comments, or None when the listing doesn't report one.
        flair: Link-flair text, or None when the post is unflaired.
        stickied: Whether the post is stickied/pinned in its listing.
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
    score: int | None = None
    comment_count: int | None = None
    flair: str | None = None
    stickied: bool = False
    packaged_media_json: str | None = field(default=None, repr=False)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_harvest(cls, data: Mapping[str, Any]) -> "Post":
        """
        Build a Post from one harvested post's raw attribute dict.

        Args:
            data: Attribute mapping produced by the listing-harvest JS (modern or legacy). The full mapping is retained on ``raw``.

        Returns:
            A normalized Post. The ``r/`` prefix is stripped from the subreddit name, and numeric
            attributes that the listing omitted (a hidden score, say) stay None rather than becoming 0.
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
            score=_to_int(data.get("score")),
            comment_count=_to_int(data.get("comment_count")),
            flair=(data.get("flair") or "").strip() or None,
            stickied=bool(data.get("stickied")),
            packaged_media_json=data.get("packaged_media"),
            raw=dict(data),
        )

    @property
    def created_at(self) -> datetime | None:
        """
        Post creation time as a timezone-aware UTC datetime (None when unavailable or unparseable).

        The two listing UIs disagree on format: the legacy one stamps epoch milliseconds, the modern
        one ISO-8601 text. Both are accepted here.
        """
        if not self.created_raw:
            return None
        text = str(self.created_raw).strip()
        try:
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError):
            pass
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @property
    def created(self) -> str | None:
        """Post creation time as an ISO-8601 string (when available)."""
        moment = self.created_at
        return moment.isoformat() if moment else None

    @property
    def url(self) -> str | None:
        """Absolute permalink URL of the post."""
        if not self.permalink:
            return None
        if self.permalink.startswith("http"):
            return self.permalink
        return "https://www.reddit.com" + self.permalink
