"""Sources: *what* to scrape (subreddits, user pages, multireddits)."""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass
from typing import Any

SUBREDDIT_SORTS = ("hot", "new", "top", "rising")
USER_SORTS = ("hot", "new", "top", "controversial")
TIME_FILTERS = ("hour", "day", "week", "month", "year", "all")


class Source(abc.ABC):
    """A listing page to harvest posts from."""

    limit: int

    @property
    @abc.abstractmethod
    def url(self) -> str:
        """Absolute URL of the listing."""

    @property
    @abc.abstractmethod
    def key(self) -> str:
        """Stable, filesystem-safe identifier (used as an output subdirectory)."""

    def __str__(self) -> str:
        return self.key


def _validate(sort: str, time_filter: str, limit: int, sorts: tuple[str, ...]) -> None:
    """Validate shared source fields.

    Args:
        sort: The requested sort.
        time_filter: The requested time window.
        limit: The requested post limit.
        sorts: The sorts allowed for this source type.

    Raises:
        ValueError: If ``sort``, ``time_filter``, or ``limit`` is invalid.
    """
    if sort not in sorts:
        raise ValueError(
            "sort must be one of {}, got {!r}".format("/".join(sorts), sort)
        )
    if time_filter not in TIME_FILTERS:
        raise ValueError(
            "time_filter must be one of {}, got {!r}".format(
                "/".join(TIME_FILTERS), time_filter
            )
        )
    if limit < 1:
        raise ValueError("limit must be >= 1, got {}".format(limit))


@dataclass(frozen=True)
class Subreddit(Source):
    """
    A subreddit listing.

    Attributes:
        name: Subreddit name; accepts a URL, ``r/name``, or a bare name (all normalized to the bare name).
        sort: One of ``hot``, ``new``, ``top``, ``rising``.
        time_filter: Time window for ``top`` sorting (``hour``..``all``).
        limit: Maximum number of posts to harvest.
    """

    name: str
    sort: str = "new"
    time_filter: str = "all"
    limit: int = 100

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", parse_subreddit(self.name))
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.name):
            raise ValueError("invalid subreddit name {!r}".format(self.name))
        _validate(self.sort, self.time_filter, self.limit, SUBREDDIT_SORTS)

    @property
    def url(self) -> str:
        base = "https://www.reddit.com/r/{}/".format(self.name)
        if self.sort == "hot":
            return base
        if self.sort == "top":
            return base + "top/?t=" + self.time_filter
        return base + self.sort + "/"

    @property
    def key(self) -> str:
        return self.name


@dataclass(frozen=True)
class MultiReddit(Source):
    """
    Several subreddits browsed as one combined listing (``r/a+b+c``).

    Attributes:
        names: The subreddit names to combine (each normalized to its bare name).
        sort: One of ``hot``, ``new``, ``top``, ``rising``.
        time_filter: Time window for ``top`` sorting (``hour``..``all``).
        limit: Maximum number of posts to harvest.
    """

    names: tuple[str, ...]
    sort: str = "new"
    time_filter: str = "all"
    limit: int = 100

    def __post_init__(self) -> None:
        names = tuple(parse_subreddit(n) for n in self.names)
        if not names:
            raise ValueError("MultiReddit needs at least one subreddit name")
        for n in names:
            if not re.fullmatch(r"[A-Za-z0-9_]+", n):
                raise ValueError("invalid subreddit name {!r}".format(n))
        object.__setattr__(self, "names", names)
        _validate(self.sort, self.time_filter, self.limit, SUBREDDIT_SORTS)

    @property
    def joined(self) -> str:
        return "+".join(self.names)

    @property
    def url(self) -> str:
        base = "https://www.reddit.com/r/{}/".format(self.joined)
        if self.sort == "hot":
            return base
        if self.sort == "top":
            return base + "top/?t=" + self.time_filter
        return base + self.sort + "/"

    @property
    def key(self) -> str:
        return self.joined


@dataclass(frozen=True)
class UserProfile(Source):
    """
    A user's submitted posts.

    Attributes:
        name: Username; accepts a URL, ``u/name``, or a bare name (all normalized to the bare name).
        sort: One of ``hot``, ``new``, ``top``, ``controversial``.
        time_filter: Time window for ``top``/``controversial`` (``hour``..``all``).
        limit: Maximum number of posts to harvest.
    """

    name: str
    sort: str = "new"
    time_filter: str = "all"
    limit: int = 100

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", parse_username(self.name))
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", self.name):
            raise ValueError("invalid username {!r}".format(self.name))
        _validate(self.sort, self.time_filter, self.limit, USER_SORTS)

    @property
    def url(self) -> str:
        return "https://www.reddit.com/user/{}/submitted/?sort={}&t={}".format(
            self.name, self.sort, self.time_filter
        )

    @property
    def key(self) -> str:
        return "u_" + self.name


def parse_source(text: str, **overrides: Any) -> Source:
    """
    Build the appropriate Source from a free-form string.

    Args:
        text: A source reference. ``u/name`` and ``/user/name`` references (or reddit.com user URLs) become a UserProfile.
            Names containing ``+`` become a MultiReddit; everything else becomes a Subreddit.
        **overrides: Optional ``sort``, ``time_filter``, and ``limit`` passed through to the constructed source.
            Keys whose value is None are dropped so the source's own defaults apply.

    Returns:
        The constructed Subreddit, MultiReddit, or UserProfile.
    """
    kwargs = {k: v for k, v in overrides.items() if v is not None}
    stripped = text.strip()
    # A user reference is either a reddit.com/u|user/ URL or a string that *starts* with u/ or user/.
    is_user = re.search(r"reddit\.com/u(?:ser)?/", stripped, re.IGNORECASE) or re.match(
        r"/?u(?:ser)?/", stripped, re.IGNORECASE
    )
    if is_user:
        return UserProfile(stripped, **kwargs)
    name = parse_subreddit(stripped)
    if "+" in name:
        return MultiReddit(tuple(name.split("+")), **kwargs)
    return Subreddit(name, **kwargs)


def parse_subreddit(value: str) -> str:
    """
    Normalize a subreddit reference to its bare name.

    Args:
        value: A full reddit.com URL, ``r/name``, ``/r/name/``, or a bare name.

    Returns:
        The bare subreddit name (which may still contain ``+`` for a multireddit).
    """
    value = value.strip()
    m = re.search(r"reddit\.com/r/([^/?#]+)", value, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"^/?r/([^/?#]+)", value, re.IGNORECASE)
    if m:
        return m.group(1)
    return value.strip("/")


def parse_username(value: str) -> str:
    """
    Normalize a user reference to its bare name.

    Args:
        value: A full reddit.com URL, ``u/name``, ``/u/name/``, ``user/name``, or a bare name.

    Returns:
        The bare username.
    """
    value = value.strip()
    m = re.search(r"reddit\.com/(?:user|u)/([^/?#]+)", value, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"^/?u(?:ser)?/([^/?#]+)", value, re.IGNORECASE)
    if m:
        return m.group(1)
    return value.strip("/")
