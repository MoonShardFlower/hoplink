"""RedGIFs-hosted videos, resolved back to the source for their audio.

Reddit routinely carries RedGIFs clips as a ``link`` card whose target is ``redgifs.com``. When Reddit rehosts one to
``v.redd.it`` the sound the clip had on RedGIFs is lost. To preserve the sound we go to the source by resolving the
post's RedGIFs id and ask RedGIFs' API for the pre-muxed MP4 (video and audio).

Resolution uses RedGIFs' documented v2 API (https://github.com/Redgifs/api/wiki):

1. **Token.** ``/v2/auth/temporary`` issues a short-lived bearer token. The token is bound to the requesting IP and
   User-Agent, so (like every download) we fetch through the browser context
2. **Lookup.** ``/v2/gifs/<id>`` (carrying that token) returns the clip's metadata, including a ``urls`` map of
   MP4s. We take ``hd`` when present, else ``sd``.

RedGIFs clips are videod, so this handler serves :attr:`MediaType.VIDEO` and sits before
:class:`~reddit_extract.handlers.video.VideoHandler` and :class:`~reddit_extract.handlers.link.LinkImageHandler` in the
default chain, so a RedGIFs post is claimed here instead of by them.

**Scrape-all.** With ``config.redgifs_scrape_all`` set the clip's ``userName`` is read, and the uploader's whole
RedGIFs profile is paged (``/v2/users/<name>/search``) so every one of their clips is downloaded, not just the one
Reddit linked. Each profile is scraped at most once per run: the handler keeps an in-memory set of profiles already
seen and skips any uploader it has handled. (The manifest still de-duplicates across runs by URL, so a later run only
fetches new clips.)

**Blacklist.** ``config.redgifs_blacklist`` If a clip's metadata names a ``userName`` on that list, the post is skipped
before anything is downloaded. Anonymous uploads carry no username and are not blocked.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler
from .video import VIDEO_CONTENT_PREFIXES

if TYPE_CHECKING:  # pragma: no cover
    from ..core.browser import FetchResult
    from ..core.context import ExtractionContext

log = logging.getLogger(__name__)

#: RedGIFs' anonymous, short-lived token endpoint.
AUTH_URL = "https://api.redgifs.com/v2/auth/temporary"

#: Page size and hard page cap for profile scraping. 80 is RedGIFs' accepted maximum.
USER_SEARCH_COUNT = 80
_MAX_PROFILE_PAGES = 500

# A watch/embed page (``/watch/<id>`` or ``/ifr/<id>``) or a direct media host
# (``media.redgifs.com/<Name>.mp4``, ``thumbs2.redgifs.com/<Name>-mobile.mp4``).
# The id is always alphanumeric; the media hosts spell it in CamelCase, so the match stops at the first ``-``/``.``
# and is lower-cased to the canonical id. The leading ``(?:^|[/.])`` pins the host boundary.
_WATCH_RE = re.compile(r"(?:^|[/.])redgifs\.com/(?:watch|ifr)/([A-Za-z0-9]+)", re.I)
_MEDIA_RE = re.compile(
    r"(?:^|[/.])(?:media|thumbs)\d*\.redgifs\.com/([A-Za-z0-9]+)", re.I
)

#: MP4 renditions in descending preference.
_QUALITY_ORDER = ("hd", "sd")


def redgifs_id(*values: Optional[str]) -> Optional[str]:
    """Return the canonical RedGIFs id found in any of ``values`` (None if none carry one).

    Args:
        *values: Candidate strings (a post's content href, domain, player src, ...).

    Returns:
        The lower-cased alphanumeric gif id, or None when no value names a RedGIFs clip.
    """
    for value in values:
        if not value:
            continue
        m = _WATCH_RE.search(value) or _MEDIA_RE.search(value)
        if m:
            return m.group(1).lower()
    return None


def gif_api_url(gif_id: str) -> str:
    """The ``/v2/gifs/<id>`` metadata endpoint for a gif id."""
    return "https://api.redgifs.com/v2/gifs/" + gif_id


def user_search_url(username: str, page: int) -> str:
    """The ``/v2/users/<name>/search`` endpoint for one page of a user's clips (newest first)."""
    return (
        "https://api.redgifs.com/v2/users/{}/search?order=new&count={}&page={}".format(
            username, USER_SEARCH_COUNT, page
        )
    )


def parse_token(body: Optional[bytes]) -> Optional[str]:
    """Pull the bearer token out of a ``/v2/auth/temporary`` response body.

    Args:
        body: The raw JSON response bytes, or None.

    Returns:
        The token string, or None if the body is missing, not JSON, or carries no non-empty token.
    """
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    token = data.get("token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def _best_from_urls(urls: object) -> Optional[str]:
    """Pick the best audio-bearing MP4 from a gif's ``urls`` map (``hd`` before ``sd``)."""
    if not isinstance(urls, dict):
        return None
    for key in _QUALITY_ORDER:
        url = urls.get(key)
        if isinstance(url, str) and url:
            return url
    return None


def best_media_url(body: Optional[bytes]) -> Optional[str]:
    """Pick the best audio-bearing MP4 URL from a ``/v2/gifs/<id>`` response body.

    Args:
        body: The raw JSON response bytes, or None.

    Returns:
        The ``hd`` URL when present, else ``sd``; None if the body is missing, not JSON,
        or exposes neither rendition. The silent rendition is never chosen.
    """
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    gif = data.get("gif") if isinstance(data, dict) else None
    return _best_from_urls(gif.get("urls")) if isinstance(gif, dict) else None


def parse_username(body: Optional[bytes]) -> Optional[str]:
    """Read the uploader's username from a ``/v2/gifs/<id>`` response body.

    Args:
        body: The raw JSON response bytes, or None.

    Returns:
        The lower-cased ``gif.userName``, or None when the body is missing, not JSON, or carries no username.
    """
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    gif = data.get("gif") if isinstance(data, dict) else None
    name = gif.get("userName") if isinstance(gif, dict) else None
    return name.lower() if isinstance(name, str) and name else None


def parse_user_gifs(body: Optional[bytes]) -> Tuple[List[str], int]:
    """Read one page of a ``/v2/users/<name>/search`` response.

    Args:
        body: The raw JSON response bytes, or None.

    Returns:
        A ``(urls, pages)`` pair: the best audio-bearing MP4 URL of each clip on this page and the profile's total page
        count (0 when the body is missing, not JSON, or reports no page count).
    """
    if not body:
        return [], 0
    try:
        data = json.loads(body)
    except ValueError:
        return [], 0
    if not isinstance(data, dict):
        return [], 0
    urls: List[str] = []
    gifs = data.get("gifs")
    if isinstance(gifs, list):
        for gif in gifs:
            url = _best_from_urls(gif.get("urls")) if isinstance(gif, dict) else None
            if url:
                urls.append(url)
    pages = data.get("pages")
    return urls, pages if isinstance(pages, int) and pages > 0 else 0


class RedGifsHandler(MediaHandler):
    """RedGIFs posts: resolve the muxed (audio-bearing) MP4 from RedGIFs' own API."""

    media_type = MediaType.VIDEO

    def __init__(self) -> None:
        """Start with no cached token and an empty per-run set of scraped profiles."""
        self._token: Optional[str] = None
        #: usernames whose whole profile has already been scraped this run (scrape-all mode)
        self._scraped_users: Set[str] = set()

    def can_handle(self, post: Post) -> bool:
        """Match any post whose linked content is a RedGIFs clip, whatever its post-type."""
        return (
            redgifs_id(post.content_href, post.domain, post.raw.get("player_src"))
            is not None
        )

    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """Resolve the post's RedGIFs clip or, with scrape-all, its uploader's whole profile.

        The clip's metadata is fetched once. If its uploader is on ``config.redgifs_blacklist`` the post is skipped.
        Otherwise, in scrape-all mode its uploader is paged into many candidates (unless that profile was already
        scraped this run), else just the one clip is returned. A clip whose token can't be issued, whose lookup
        fails, or that exposes no usable rendition is reported via ``ctx.skip`` and yields nothing.
        """
        gif_id = redgifs_id(post.content_href, post.domain, post.raw.get("player_src"))
        if gif_id is None:  # pragma: no cover (can_handle already required one)
            return []
        ref = post.url or post.content_href or post.id
        result = await self._authed_get(gif_api_url(gif_id), ctx)
        if result is None or not result.ok or result.body is None:
            await ctx.skip(
                ref, "redgifs: could not resolve video for {}".format(gif_id)
            )
            return []

        if ctx.config.redgifs_blacklist:
            user = parse_username(result.body)
            if user is not None and user in ctx.config.redgifs_blacklist:
                await ctx.skip(ref, "redgifs: @{} is blacklisted".format(user))
                return []

        if ctx.config.redgifs_scrape_all:
            profile = await self._scrape_profile(result.body, ref, ctx)
            if profile is not None:  # None => fall back to the single clip below
                return profile

        url = best_media_url(result.body)
        if url is None:
            await ctx.skip(
                ref, "redgifs: could not resolve video for {}".format(gif_id)
            )
            return []
        log.debug("redgifs %s: %s", gif_id, url)
        return [self._candidate(url)]

    async def _scrape_profile(
        self, gif_body: bytes, ref: str, ctx: "ExtractionContext"
    ) -> Optional[List[MediaCandidate]]:
        """Page an uploader's whole RedGIFs profile into candidates (once per run).

        Args:
            gif_body: The clip's ``/v2/gifs/<id>`` response body, read for its ``userName``.
            ref: A post reference for skip messages.
            ctx: The active extraction context.

        Returns:
            One candidate per clip in the uploader's profile; ``[]`` when that profile was already scraped this run; or
            None to signal "no profile to scrape" so the caller falls back to the single linked clip.
        """
        user = parse_username(gif_body)
        if user is None:
            return None  # anonymous upload: nothing to page, keep just the linked clip
        if user in self._scraped_users:
            await ctx.skip(ref, "redgifs: @{} already scraped this run".format(user))
            return []
        self._scraped_users.add(user)

        urls = await self._page_user(user, ctx)
        if not urls:
            return None  # profile unreadable/empty: fall back to the single linked clip
        log.debug("redgifs @%s: %d clips", user, len(urls))
        return [self._candidate(u) for u in urls]

    async def _page_user(self, user: str, ctx: "ExtractionContext") -> List[str]:
        """Walk every page of a user's clips, returning their MP4 URLs (de-duplicated, in order)."""
        urls: List[str] = []
        seen: Set[str] = set()
        page, pages = 1, 1
        while page <= pages and page <= _MAX_PROFILE_PAGES:
            result = await self._authed_get(user_search_url(user, page), ctx)
            if result is None or not result.ok or result.body is None:
                break
            page_urls, total_pages = parse_user_gifs(result.body)
            if total_pages:
                pages = total_pages
            added = False
            for u in page_urls:
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
                    added = True
            if not added:  # empty or all-duplicate page: stop rather than spin
                break
            page += 1
            if page <= pages:
                await ctx.sleep(ctx.config.api_pause)
        return urls

    def _candidate(self, url: str) -> MediaCandidate:
        """Wrap a RedGIFs MP4 URL as a video MediaCandidate."""
        return MediaCandidate(
            url=url,
            media_type=self.media_type,
            ext="mp4",
            content_prefixes=VIDEO_CONTENT_PREFIXES,
        )

    async def _authed_get(
        self, url: str, ctx: "ExtractionContext"
    ) -> Optional["FetchResult"]:
        """GET a RedGIFs API URL with the bearer token, refreshing a rejected token once.

        Args:
            url: The API URL to fetch.
            ctx: The active extraction context.

        Returns:
            The FetchResult (successful or not), or None if no token could be issued.
        """
        for attempt in (1, 2):
            token = await self._auth(ctx, force=attempt == 2)
            if token is None:
                return None
            result = await ctx.fetch(url, headers={"Authorization": "Bearer " + token})
            # A rejected token is worth one refresh (it may simply have expired mid-run).
            if result.status in (401, 403) and attempt == 1:
                self._token = None
                continue
            return result
        return None  # pragma: no cover

    async def _auth(
        self, ctx: "ExtractionContext", *, force: bool = False
    ) -> Optional[str]:
        """Return a bearer token, reusing the cached one unless ``force`` asks for a fresh fetch.

        Args:
            ctx: The active extraction context.
            force: Ignore any cached token and request a new one.

        Returns:
            A token string, or None if the token endpoint failed or returned no token.
        """
        if self._token is not None and not force:
            return self._token
        result = await ctx.fetch(AUTH_URL)
        self._token = parse_token(result.body) if result.ok else None
        return self._token
