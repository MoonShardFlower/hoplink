"""RedGIFs-hosted clips and stills, resolved back to the source (a clip for its audio, a still for its full album).

Reddit routinely carries RedGIFs media as a ``link`` card whose target is ``redgifs.com``. When Reddit rehosts a clip
to ``v.redd.it`` the sound it had on RedGIFs is lost. To preserve the sound we go to the source by resolving the
post's RedGIFs id and ask RedGIFs' API for the pre-muxed MP4 (video and audio).

Resolution uses RedGIFs' documented v2 API (https://github.com/Redgifs/api/wiki):

1. **Token.** ``/v2/auth/temporary`` issues a short-lived bearer token. The token is bound to the requesting IP and
   User-Agent, so (like every download) we fetch through the browser context
2. **Lookup.** ``/v2/gifs/<id>`` (carrying that token) returns the item's metadata, including a ``urls`` map of
   renditions. We take ``hd`` when present, else ``sd``.

**Clips and stills.** RedGIFs hosts both, and files them in one shape: ``gif.type`` tells them apart (see
`GIF_TYPE_IMAGE`) while ``urls`` carries the same ``hd``/``sd`` pair either way, MP4s for a clip and JPEGs for a
still. So this resolver declares both :attr:`MediaType.VIDEO` and :attr:`MediaType.IMAGE`, and each item becomes a
candidate of its own kind. A run keeps only what it asked for: stills are dropped on a video-only run and clips on an
images-only one, and a still whose extension is outside ``--formats`` is dropped like any other image.

**Albums.** A still is often one page of an album, which it names in ``gif.gallery``. Such a still is expanded into
the album's every image via ``/v2/gallery/<id>``, so an album yields all of its pages rather than just its cover.
That costs one extra API call per album, and only on a run that wants images.

This is a :class:`~hoplink.handlers.resolver.LinkResolver`, so it is reached through
:class:`~hoplink.handlers.external.ExternalLinkHandler` for any URL on the host, wherever that URL was found:
a link post's target, a crosspost's shared media, or a link in the body of a self post.

**Scrape-all.** With ``"redgifs"`` in ``config.scrape_all_hosts`` the item's ``userName`` is read, and the uploader's
whole RedGIFs profile is paged (``/v2/users/<name>/search``) so every one of their clips and stills is downloaded,
not just the one Reddit linked. Those candidates name the uploader as their *collection*, so all files of the profile
are stored in their own folder (``<source>/<uploader>/``). Each profile is scraped at most once per run: the claim is
recorded in shared state, so concurrent jobs cannot both page one profile, and it is given back if the paging comes
up empty.

**Blacklist.** ``host_options={"redgifs": {"blacklist": ...}}``: if an item's metadata names a ``userName`` on that
list, the post is skipped before anything is downloaded. Anonymous uploads carry no username and are not blocked.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, List, NamedTuple, Optional, Sequence, Set, Tuple

from ..models.media import MediaCandidate, MediaType
from ..storage import media_extension
from .resolver import LinkResolver
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

#: Renditions in descending preference. Both a clip and a still offer this pair.
_QUALITY_ORDER = ("hd", "sd")

#: ``gif.type`` in a v2 response: 1 is a video clip, 2 a still image.
GIF_TYPE_VIDEO = 1
GIF_TYPE_IMAGE = 2

#: Extensions that mark a rendition as a still, used when ``gif.type`` is missing or a value RedGIFs added later.
_IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "gif"})

#: Content types a RedGIFs still may be served as.
IMAGE_CONTENT_PREFIXES = ("image/",)

#: shared-state keys (see `hoplink.core.state`)
_TOKEN = "token"
_SCRAPED = "scraped_users"


class RedGifsMedia(NamedTuple):
    """One downloadable item named by a RedGIFs API response."""

    #: the best rendition's URL
    url: str
    #: :attr:`MediaType.VIDEO` for a clip, :attr:`MediaType.IMAGE` for a still
    media_type: MediaType
    #: the album this still is one page of, when it is one (clips are never in albums)
    gallery: Optional[str] = None


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


def gallery_api_url(gallery_id: str) -> str:
    """The ``/v2/gallery/<id>`` endpoint holding every page of an album."""
    return "https://api.redgifs.com/v2/gallery/" + gallery_id


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
    """Pick the best rendition from a gif's ``urls`` map (``hd`` before ``sd``)."""
    if not isinstance(urls, dict):
        return None
    for key in _QUALITY_ORDER:
        url = urls.get(key)
        if isinstance(url, str) and url:
            return url
    return None


def media_from_gif(gif: object) -> Optional[RedGifsMedia]:
    """Read one ``gif`` object -- from a lookup, a profile page, or an album -- as a downloadable item.

    Args:
        gif: One entry of a v2 response, or anything else (which yields None).

    Returns:
        The item's best rendition, typed by ``gif.type`` and carrying the album it belongs to; None when the entry
        exposes no usable rendition. For a clip that rendition is the audio-bearing MP4: the silent one is never
        chosen.
    """
    if not isinstance(gif, dict):
        return None
    url = _best_from_urls(gif.get("urls"))
    if url is None:
        return None
    kind = gif.get("type")
    # ``type`` is the documented discriminator; the extension decides when it is absent or a value RedGIFs added
    # after this was written, so an unknown kind of still is never downloaded as a video again.
    is_image = kind == GIF_TYPE_IMAGE or (
        kind != GIF_TYPE_VIDEO and media_extension(url, default="") in _IMAGE_EXTENSIONS
    )
    if not is_image:
        return RedGifsMedia(url, MediaType.VIDEO)
    gallery = gif.get("gallery")
    return RedGifsMedia(
        url,
        MediaType.IMAGE,
        gallery if isinstance(gallery, str) and gallery else None,
    )


def best_media(body: Optional[bytes]) -> Optional[RedGifsMedia]:
    """Read the item a ``/v2/gifs/<id>`` lookup describes.

    Args:
        body: The raw JSON response bytes, or None.

    Returns:
        The clip or still it names (see `media_from_gif`); None if the body is missing, not JSON, or exposes
        no usable rendition.
    """
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    gif = data.get("gif") if isinstance(data, dict) else None
    return media_from_gif(gif)


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


def _gifs_in(body: Optional[bytes]) -> Tuple[List[RedGifsMedia], object]:
    """The items of any response carrying a ``gifs`` list, plus that response's raw ``pages`` value."""
    if not body:
        return [], None
    try:
        data = json.loads(body)
    except ValueError:
        return [], None
    if not isinstance(data, dict):
        return [], None
    gifs = data.get("gifs")
    items = [] if not isinstance(gifs, list) else [media_from_gif(g) for g in gifs]
    return [item for item in items if item is not None], data.get("pages")


def parse_user_gifs(body: Optional[bytes]) -> Tuple[List[RedGifsMedia], int]:
    """Read one page of a ``/v2/users/<name>/search`` response.

    Args:
        body: The raw JSON response bytes, or None.

    Returns:
        An ``(items, pages)`` pair: this page's clips and stills, and the profile's total page count (0 when the
        body is missing, not JSON, or reports no page count).
    """
    items, pages = _gifs_in(body)
    return items, pages if isinstance(pages, int) and pages > 0 else 0


def parse_gallery(body: Optional[bytes]) -> List[RedGifsMedia]:
    """Read every page of an album from a ``/v2/gallery/<id>`` response.

    Args:
        body: The raw JSON response bytes, or None.

    Returns:
        The album's images in order, empty when the body is missing, not JSON, or names none.
    """
    return _gifs_in(body)[0]


class RedGifsResolver(LinkResolver):
    """RedGIFs URLs: resolve the muxed (audio-bearing) MP4 of a clip, or the images of a still's album."""

    host = "redgifs"
    domains = ("redgifs.com",)
    media_type = MediaType.VIDEO | MediaType.IMAGE

    def claims(self, url: str) -> bool:
        """Claim any URL carrying a RedGIFs id -- a watch page, an embed, or a direct media URL."""
        return redgifs_id(url) is not None

    async def resolve(
        self, url: str, ctx: "ExtractionContext", *, ref: str
    ) -> List[MediaCandidate]:
        """Resolve the item at ``url`` or, with scrape-all, its uploader's whole profile.

        The item's metadata is fetched once. If its uploader is blacklisted the post is skipped. Otherwise, in
        scrape-all mode its uploader is paged into many candidates (unless that profile was already scraped this
        run), else just the linked item is returned, expanded to its whole album when it is a still that names one.
        An item whose token can't be issued, whose lookup fails, that exposes no usable rendition, or that this run
        didn't ask for is reported via ``ctx.skip`` and yields nothing.
        """
        gif_id = redgifs_id(url)
        if gif_id is None:  # pragma: no cover (claims already required one)
            return []
        result = await self._authed_get(gif_api_url(gif_id), ctx)
        if result is None or not result.ok or result.body is None:
            await ctx.skip(
                ref, "redgifs: could not resolve media for {}".format(gif_id)
            )
            return []

        blacklist = ctx.host_list(self.host, "blacklist")
        if blacklist:
            user = parse_username(result.body)
            if user is not None and user in blacklist:
                await ctx.skip(ref, "redgifs: @{} is blacklisted".format(user))
                return []

        if ctx.scrape_all(self.host):
            profile = await self._scrape_profile(result.body, ref, ctx)
            if profile is not None:  # None => fall back to the single item below
                return profile

        best = best_media(result.body)
        if best is None:
            await ctx.skip(
                ref, "redgifs: could not resolve media for {}".format(gif_id)
            )
            return []
        unwanted = self._unwanted(best, ctx)
        if unwanted is not None:
            await ctx.skip(ref, "redgifs: {} {}".format(gif_id, unwanted))
            return []
        log.debug("redgifs %s: %s", gif_id, best.url)
        return await self._expand(best, ctx)

    async def _scrape_profile(
        self, gif_body: bytes, ref: str, ctx: "ExtractionContext"
    ) -> Optional[List[MediaCandidate]]:
        """Page an uploader's whole RedGIFs profile into candidates (once per run).

        Args:
            gif_body: The clip's ``/v2/gifs/<id>`` response body, read for its ``userName``.
            ref: A post reference for skip messages.
            ctx: The active extraction context.

        Returns:
            One candidate per item in the uploader's profile, each tagged with the uploader as its collection.
            ``[]`` when the profile was already scraped this run or None as "no profile to scrape found" signal.
        """
        user = parse_username(gif_body)
        if user is None:
            return None  # anonymous upload: nothing to page, keep just the linked item
        if not await self._claim_profile(user, ctx):
            await ctx.skip(ref, "redgifs: @{} already scraped this run".format(user))
            return []

        items = await self._page_user(user, ctx)
        if not items:
            # Nothing came back, so give the claim up: a later post may reach the profile when it is readable
            # again, and this one still falls back to the item Reddit linked.
            await self._release_profile(user, ctx)
            return None
        log.debug("redgifs @%s: %d files", user, len(items))
        return [self._candidate(item, collection=user) for item in items]

    async def _claim_profile(self, user: str, ctx: "ExtractionContext") -> bool:
        """Claim ``user`` for scraping, returning False when this run already claimed them.

        The check and the claim are one atomic step, so two jobs running concurrently cannot both conclude they
        are the one to page a profile.
        """
        store = ctx.state(self.host)
        async with store.lock:
            scraped: Set[str] = store.data.setdefault(_SCRAPED, set())
            if user in scraped:
                return False
            scraped.add(user)
            return True

    async def _release_profile(self, user: str, ctx: "ExtractionContext") -> None:
        """Give up a claim taken by `_claim_profile` (the profile turned out to be unreadable)."""
        store = ctx.state(self.host)
        async with store.lock:
            store.data.get(_SCRAPED, set()).discard(user)

    async def _page_user(
        self, user: str, ctx: "ExtractionContext"
    ) -> List[RedGifsMedia]:
        """Walk every page of a user's profile, returning the items this run wants (de-duplicated, in order).

        Items of a kind the run didn't ask for are dropped here rather than downstream, so a video-only run pays
        nothing for an uploader's stills. Album covers are expanded into their every page (see `_expand_all`); the
        cover is one of those pages, which the de-duplication folds back together.

        Pacing between pages is the fetch route's job: it holds successive requests to one host
        ``config.api_pause`` apart whoever makes them.
        """
        items: List[RedGifsMedia] = []
        seen: Set[str] = set()
        page, pages = 1, 1
        while page <= pages and page <= _MAX_PROFILE_PAGES:
            result = await self._authed_get(user_search_url(user, page), ctx)
            if result is None or not result.ok or result.body is None:
                # The profile is claimed for this run either way, so a page lost to a rate limit means files
                # silently missing from it. Say so rather than letting the count speak for a complete profile.
                log.warning(
                    "redgifs @%s: page %d failed (%s); keeping the %d files collected so far",
                    user,
                    page,
                    result.error if result is not None else "no token",
                    len(items),
                )
                break
            page_items, total_pages = parse_user_gifs(result.body)
            if total_pages:
                pages = total_pages
            if not page_items:  # an empty page: there is nothing further to walk
                break
            wanted = [i for i in page_items if self._unwanted(i, ctx) is None]
            new = [i for i in await self._expand_all(wanted, ctx) if i.url not in seen]
            # A page whose every item was already collected means the listing is repeating itself, so stop rather
            # than spin. A page filtered away entirely is not that: the next page may still hold what was asked
            # for, and an images-only run would otherwise give up on the first page of clips.
            if wanted and not new:
                break
            for item in new:
                seen.add(item.url)
                items.append(item)
            page += 1
        return items

    def _unwanted(self, media: RedGifsMedia, ctx: "ExtractionContext") -> Optional[str]:
        """Why this run doesn't want ``media``, phrased for a skip message (None when it does want it).

        Two things can rule an item out: its kind, when the job didn't ask for that media type, and -- for a still,
        as for every other image the library saves -- an extension outside ``config.formats``.
        """
        if not (media.media_type & ctx.wanted):
            kind = "a still" if media.media_type is MediaType.IMAGE else "a clip"
            return "is {}, which this run didn't ask for".format(kind)
        ext = media_extension(media.url, default="")
        if media.media_type is MediaType.IMAGE and ext not in ctx.formats:
            return "is a still ({}), which is not in formats".format(
                "." + ext if ext else "no extension"
            )
        return None

    async def _expand_all(
        self, items: Sequence[RedGifsMedia], ctx: "ExtractionContext"
    ) -> List[RedGifsMedia]:
        """Expand every album cover in ``items`` into its pages, leaving everything else as it is."""
        expanded: List[RedGifsMedia] = []
        for item in items:
            expanded.extend(await self._album_pages(item, ctx))
        return expanded

    async def _album_pages(
        self, media: RedGifsMedia, ctx: "ExtractionContext"
    ) -> List[RedGifsMedia]:
        """The album ``media`` is the cover of, or just ``media`` when it belongs to none.

        An album that cannot be read falls back to the cover alone, so an unreachable ``/v2/gallery/<id>`` costs the
        image Reddit actually linked rather than losing it.
        """
        if not media.gallery:
            return [media]
        result = await self._authed_get(gallery_api_url(media.gallery), ctx)
        if result is None or not result.ok or result.body is None:
            log.debug(
                "redgifs album %s is unreadable; keeping its cover", media.gallery
            )
            return [media]
        pages = [
            p for p in parse_gallery(result.body) if self._unwanted(p, ctx) is None
        ]
        log.debug("redgifs album %s: %d images", media.gallery, len(pages))
        return pages or [media]

    async def _expand(
        self, media: RedGifsMedia, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """Turn one resolved item into candidates, an album cover into one per page (see `_album_pages`)."""
        return [self._candidate(page) for page in await self._album_pages(media, ctx)]

    def _candidate(
        self, media: RedGifsMedia, *, collection: Optional[str] = None
    ) -> MediaCandidate:
        """Wrap a resolved item as a MediaCandidate, optionally belonging to an uploader's collection.

        A clip is saved as ``.mp4``; a still keeps the extension its URL names, which is also what the run's
        ``formats`` were checked against.
        """
        if media.media_type is MediaType.IMAGE:
            return MediaCandidate(
                url=media.url,
                media_type=MediaType.IMAGE,
                ext=media_extension(media.url, default="") or None,
                content_prefixes=IMAGE_CONTENT_PREFIXES,
                collection=collection,
            )
        return MediaCandidate(
            url=media.url,
            media_type=MediaType.VIDEO,
            ext="mp4",
            content_prefixes=VIDEO_CONTENT_PREFIXES,
            collection=collection,
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
        token: Optional[str] = None
        for attempt in (1, 2):
            # On the second pass the token just rejected is named as stale, so it cannot be handed back again.
            token = await self._auth(ctx, stale=token)
            if token is None:
                return None
            result = await ctx.fetch(url, headers={"Authorization": "Bearer " + token})
            # A rejected token is worth one refresh (it may simply have expired mid-run).
            if result.status in (401, 403) and attempt == 1:
                continue
            return result
        return None  # pragma: no cover

    async def _auth(
        self, ctx: "ExtractionContext", *, stale: Optional[str] = None
    ) -> Optional[str]:
        """Return a bearer token, reusing the one in shared state unless it is the one that just failed.

        The lock is held across the token request, so a burst of posts (or of concurrent jobs) costs one call to
        the token endpoint rather than one per caller. A caller whose token was rejected names it as ``stale``: if
        another caller has meanwhile replaced it, that replacement is handed back instead of fetching again.

        Args:
            ctx: The active extraction context.
            stale: A token known to be rejected, which must not be returned.

        Returns:
            A token string, or None if the token endpoint failed or returned no token.
        """
        store = ctx.state(self.host)
        async with store.lock:
            cached = store.data.get(_TOKEN)
            if isinstance(cached, str) and cached != stale:
                return cached
            result = await ctx.fetch(AUTH_URL)
            token = parse_token(result.body) if result.ok else None
            store.data[_TOKEN] = token
            return token
