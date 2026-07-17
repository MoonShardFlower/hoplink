"""
Post-level predicates: *which* of the harvested posts are worth keeping.

`MediaType` decides which **kind** of post a job handles; a `PostFilter` narrows that down to a slice of it (posts
above a score, inside a date window, by particular authors,...).
The engine applies one between harvest and resolve, so a rejected post costs no page visits and no downloads.

Example::

    from datetime import datetime, timezone
    from reddit_extract import PostFilter, RedditExtractor

    keepers = PostFilter(
        min_score=500,
        title_exclude=r"\\[?(meta|mod ?post)\\]?",
        title_regex=True,
        after=datetime(2026, 1, 1, tzinfo=timezone.utc),
        skip_stickied=True,
    )
    with RedditExtractor() as rex:
        result = rex.extract("r/EarthPorn", post_filter=keepers)

Every predicate is optional; an empty filter keeps everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Pattern, Union

from .post import Post

#: A comma-separated string, an iterable of strings, or nothing.
StrListLike = Union[str, Iterable[str], None]

#: A datetime, an ISO-8601 string (``2026-01-01`` or ``2026-01-01T12:00:00Z``), or nothing.
DateLike = Union[datetime, str, None]


def coerce_str_list(value: StrListLike) -> tuple[str, ...]:
    """
    Normalize a name list into a lower-cased tuple.

    Args:
        value: A comma-separated string (``"alice,bob"``), an iterable of strings, or None.
            Entries are trimmed and lower-cased; blanks are dropped.

    Returns:
        The normalized names, empty when ``value`` is None or holds nothing usable.
    """
    if value is None:
        return ()
    parts: Iterable[str] = value.split(",") if isinstance(value, str) else value
    return tuple(p.strip().lower() for p in parts if p and p.strip())


def coerce_datetime(value: DateLike, *, field_name: str = "date") -> datetime | None:
    """
    Normalize a date bound into a timezone-aware UTC datetime.

    Args:
        value: A datetime, an ISO-8601 string such as ``"2026-01-01"`` or ``"2026-01-01T12:00:00+00:00"``, or None.
        field_name: Name used in the error message.

    Returns:
        An aware UTC datetime, or None when ``value`` is None. Naive inputs are read as UTC.

    Raises:
        ValueError: If a string is not valid ISO-8601.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(
                "{} must be an ISO-8601 date such as 2026-01-01 or "
                "2026-01-01T12:00:00+00:00, got {!r}".format(field_name, value)
            ) from None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PostFilter:
    """
    Optional predicates applied to each harvested post before its media is resolved.

    A post is kept only when it satisfies *every* predicate that is set. Unset predicates
    (the defaults) are ignored, so ``PostFilter()`` keeps everything.

    A predicate whose input the listing didn't report **rejects** the post rather than waving it
    through. Rejections carry a reason, which the engine reports through ``Events.on_skip``

    Attributes:
        min_score: Keep posts whose score is at least this.
        min_comments: Keep posts with at least this many comments.
        title_include: Keep only posts whose title matches this.
        title_exclude: Drop posts whose title matches this.
        title_regex: Treat ``title_include``/``title_exclude`` as regular expressions instead of plain substrings.
             Either way matching is case-insensitive and unanchored.
        authors: keep only posts by these authors (comma string or iterable).
        block_authors: drop posts by these authors. Takes precedence over ``authors``.
        flairs: Keep only posts whose link flair matches one of these (case-insensitive, exact).
        min_gallery: Keep gallery posts only when they carry at least this many images. Non-gallery
            posts are unaffected. Applied after resolve (see `rejection_after_resolve`).
        after: Keep posts created at or after this moment (datetime or ISO-8601 string).
        before: Keep posts created strictly before this moment.
        skip_stickied: Drop stickied/pinned posts.
    """

    min_score: int | None = None
    min_comments: int | None = None
    title_include: str | None = None
    title_exclude: str | None = None
    title_regex: bool = False
    authors: tuple[str, ...] = ()
    block_authors: tuple[str, ...] = ()
    flairs: tuple[str, ...] = ()
    min_gallery: int | None = None
    after: datetime | None = None
    before: datetime | None = None
    skip_stickied: bool = False

    # Compiled in __post_init__ from the title_* fields; not part of the public constructor.
    _include_re: Pattern[str] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _exclude_re: Pattern[str] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """
        Normalize the list and date fields and validate the numbers and patterns.

        Raises:
            ValueError: If a count is negative, a date string is not ISO-8601, a title pattern is
                an invalid regex, or ``after`` is not before ``before``.
        """
        object.__setattr__(self, "authors", coerce_str_list(self.authors))
        object.__setattr__(self, "block_authors", coerce_str_list(self.block_authors))
        object.__setattr__(self, "flairs", coerce_str_list(self.flairs))
        object.__setattr__(
            self, "after", coerce_datetime(self.after, field_name="after")
        )
        object.__setattr__(
            self, "before", coerce_datetime(self.before, field_name="before")
        )
        for name in ("min_score", "min_comments", "min_gallery"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError("{} must be >= 0, got {}".format(name, value))
        if self.after and self.before and self.after >= self.before:
            raise ValueError(
                "after ({}) must be earlier than before ({})".format(
                    self.after.isoformat(), self.before.isoformat()
                )
            )
        # Compile once per filter rather than once per post; also surfaces a bad pattern up front.
        object.__setattr__(self, "_include_re", self._compile(self.title_include))
        object.__setattr__(self, "_exclude_re", self._compile(self.title_exclude))

    def _compile(self, pattern: str | None) -> Pattern[str] | None:
        """
        Compile a title pattern, escaping it first unless ``title_regex`` is set.

        Args:
            pattern: The raw pattern, or None.

        Returns:
            The compiled case-insensitive pattern, or None.

        Raises:
            ValueError: If ``title_regex`` is set and the pattern is not a valid regex.
        """
        if not pattern:
            return None
        source = pattern if self.title_regex else re.escape(pattern)
        try:
            return re.compile(source, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                "invalid title regex {!r}: {}".format(pattern, exc)
            ) from None

    @property
    def active(self) -> bool:
        """Whether any predicate is actually set (False for a default-constructed filter)."""
        return any(
            (
                self.min_score is not None,
                self.min_comments is not None,
                self.title_include,
                self.title_exclude,
                self.authors,
                self.block_authors,
                self.flairs,
                self.min_gallery is not None,
                self.after,
                self.before,
                self.skip_stickied,
            )
        )

    def accepts(self, post: Post) -> bool:
        """
        Whether ``post`` satisfies every predicate set on this filter.

        Args:
            post: The harvested post to test.

        Returns:
            True if the post should be kept. This covers the listing-time predicates only:
            ``min_gallery`` is decided later by `rejection_after_resolve`.
        """
        return self.rejection(post) is None

    def rejection(self, post: Post) -> str | None:
        """
        Explain why ``post`` fails this filter.

        Args:
            post: The harvested post to test.

        Returns:
            A short human-readable reason for the first predicate the post fails
            (suitable for ``Events.on_skip``), or None if it passes.
        """
        author = (post.author or "").lower()
        if self.block_authors and author in self.block_authors:
            return "author {} is blocked".format(post.author)
        if self.authors and author not in self.authors:
            return "author {} not in allow list".format(post.author or "?")
        if self.skip_stickied and post.stickied:
            return "post is stickied"
        if self.min_score is not None:
            if post.score is None:
                return "score unknown (min {})".format(self.min_score)
            if post.score < self.min_score:
                return "score {} below min {}".format(post.score, self.min_score)
        if self.min_comments is not None:
            if post.comment_count is None:
                return "comment count unknown (min {})".format(self.min_comments)
            if post.comment_count < self.min_comments:
                return "comments {} below min {}".format(
                    post.comment_count, self.min_comments
                )
        if self.flairs:
            flair = (post.flair or "").lower()
            if flair not in self.flairs:
                return "flair {!r} not in {}".format(
                    post.flair or "", ", ".join(self.flairs)
                )
        title = post.title or ""
        if self._include_re is not None and not self._include_re.search(title):
            return "title does not match {!r}".format(self.title_include)
        if self._exclude_re is not None and self._exclude_re.search(title):
            return "title matches excluded {!r}".format(self.title_exclude)
        return self._date_rejection(post)

    def _date_rejection(self, post: Post) -> str | None:
        """Check ``post`` against the created-after/created-before window."""
        if self.after is None and self.before is None:
            return None
        created = post.created_at
        if created is None:
            return "creation time unknown"
        if self.after is not None and created < self.after:
            return "created {} before {}".format(
                created.isoformat(), self.after.isoformat()
            )
        if self.before is not None and created >= self.before:
            return "created {} at or after {}".format(
                created.isoformat(), self.before.isoformat()
            )
        return None

    def rejection_after_resolve(self, post: Post, media_count: int) -> str | None:
        """
        Explain why ``post`` fails the predicates that need its resolved media count.

        Only ``min_gallery`` lives here, because a listing doesn't contain the gallery's real size: Reddit virtualizes
        the carousel, rendering a couple of slides regardless of how many the gallery holds, and its ``gallery``
        attribute is just a boolean. The true count is known only once `GalleryHandler` has  visited the post page.

        Args:
            post: The post whose media was just resolved.
            media_count: How many candidates the handler produced.

        Returns:
            A short reason to drop the post, or None to keep it.
        """
        if (
            self.min_gallery is not None
            and post.type == "gallery"
            and media_count < self.min_gallery
        ):
            return "gallery has {} image(s), below min {}".format(
                media_count, self.min_gallery
            )
        return None
