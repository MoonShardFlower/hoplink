"""
Tests for the RedGIFs handler: resolve a post's RedGIFs id to the audio-bearing MP4.

Browser-free: the module's helpers are plain functions, and ``RedGifsHandler.resolve`` only ever uses ``ctx.fetch``
and ``ctx.skip``. A small routing fake context exercises the whole two-call API strategy with no Playwright.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Union

from reddit_extract.core.browser import FetchResult
from reddit_extract.handlers.redgifs import (
    AUTH_URL,
    RedGifsHandler,
    best_media_url,
    gif_api_url,
    parse_token,
    parse_user_gifs,
    parse_username,
    redgifs_id,
    user_search_url,
)
from reddit_extract.models.config import ExtractorConfig
from reddit_extract.models.media import MediaType
from reddit_extract.models.post import Post

GID = "gleamingwearygrouse"
WATCH = "https://www.redgifs.com/watch/" + GID
HD = "https://media.redgifs.com/GleamingWearyGrouse.mp4"
SD = "https://media.redgifs.com/GleamingWearyGrouse-mobile.mp4"


class FakeContext:
    """Routes each fetched URL to a canned result (or a queue of them), and records skips.

    A route value may be a single FetchResult (returned every time) or a list consumed one entry per call, the last
    entry repeating (enough to model a token that is rejected once and then accepted). ``scrape_all`` sets the matching
    config flag.
    """

    def __init__(
        self,
        routes: Dict[str, Union[FetchResult, List[FetchResult]]],
        *,
        scrape_all: bool = False,
    ) -> None:
        self.routes = routes
        self.config = ExtractorConfig(redgifs_scrape_all=scrape_all, scroll_pause=0.0)
        self.fetches: List[tuple[str, Any]] = []
        self.skips: List[tuple[str, str]] = []
        self.sleeps = 0
        self.slept: List[float | None] = []

    async def fetch(self, url: str, headers: Any = None) -> FetchResult:
        self.fetches.append((url, headers))
        value = self.routes[url]
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    async def skip(self, url: str, reason: str) -> None:
        self.skips.append((url, reason))

    async def sleep(self, seconds: float | None = None) -> None:
        self.sleeps += 1
        self.slept.append(seconds)


def rg_post(**overrides: Any) -> Post:
    """One harvested RedGIFs link post, shaped like the modern UI's JS_HARVEST output."""
    data: dict[str, Any] = {
        "id": "r1",
        "type": "link",
        "permalink": "/r/gifs/comments/r1/clip/",
        "content_href": WATCH,
        "domain": "redgifs.com",
        "player_src": None,
    }
    data.update(overrides)
    return Post.from_harvest(data)


def token_result(
    token: str | None = "tok123", *, ok: bool = True, status: int = 200
) -> FetchResult:
    body = json.dumps({"token": token}).encode() if token is not None else b"{}"
    return FetchResult(
        ok=ok, status=status, content_type="application/json", body=body if ok else None
    )


def gif_result(urls: dict, *, ok: bool = True, status: int = 200) -> FetchResult:
    body = json.dumps({"gif": {"id": GID, "urls": urls, "hasAudio": True}}).encode()
    return FetchResult(
        ok=ok, status=status, content_type="application/json", body=body if ok else None
    )


def gif_by(user: str, urls: dict | None = None) -> FetchResult:
    """A ``/v2/gifs/<id>`` response that names its uploader (for scrape-all)."""
    body = json.dumps(
        {"gif": {"id": GID, "userName": user, "urls": urls or {"hd": HD}}}
    )
    return FetchResult(
        ok=True, status=200, content_type="application/json", body=body.encode()
    )


def user_page(urls: list, *, pages: int = 1) -> FetchResult:
    """One page of a ``/v2/users/<name>/search`` response, one gif per URL."""
    gifs = [{"id": "g{}".format(i), "urls": {"hd": u}} for i, u in enumerate(urls)]
    body = json.dumps({"page": 1, "pages": pages, "total": len(urls), "gifs": gifs})
    return FetchResult(
        ok=True, status=200, content_type="application/json", body=body.encode()
    )


# -- redgifs_id: finding the gif id -----------------------------------------


def test_a_watch_url_yields_the_id():
    assert redgifs_id(WATCH) == GID


def test_the_id_is_lower_cased():
    assert redgifs_id("https://redgifs.com/watch/GleamingWearyGrouse") == GID


def test_an_ifr_embed_url_is_recognized():
    assert redgifs_id("https://www.redgifs.com/ifr/" + GID) == GID


def test_a_direct_media_url_yields_the_id_without_its_suffix():
    # The CamelCase media name stops at the '-mobile' suffix, then lower-cases to the canonical id.
    assert redgifs_id(SD) == GID
    assert (
        redgifs_id("https://thumbs4.redgifs.com/GleamingWearyGrouse-silent.mp4") == GID
    )


def test_a_lookalike_host_does_not_match():
    # The host boundary is pinned, so a domain that merely ends in redgifs.com is ignored.
    assert redgifs_id("https://notredgifs.com/watch/abc") is None


def test_a_non_redgifs_url_is_none():
    assert redgifs_id("https://i.redd.it/a.jpg") is None
    assert redgifs_id("redgifs.com") is None  # a bare domain carries no id


def test_the_first_value_carrying_an_id_wins():
    assert redgifs_id(None, "https://i.redd.it/a.jpg", WATCH) == GID


# -- parse_token ------------------------------------------------------------


def test_a_token_is_read_from_the_auth_body():
    assert parse_token(json.dumps({"token": "abc"}).encode()) == "abc"


def test_a_missing_or_empty_token_is_none():
    assert parse_token(json.dumps({"addr": "x"}).encode()) is None
    assert parse_token(json.dumps({"token": ""}).encode()) is None


def test_a_non_string_token_is_none():
    assert parse_token(json.dumps({"token": 42}).encode()) is None


def test_an_empty_or_invalid_body_is_none():
    assert parse_token(None) is None
    assert parse_token(b"") is None
    assert parse_token(b"{not json") is None


# -- best_media_url ---------------------------------------------------------


def test_hd_is_preferred_over_sd():
    assert best_media_url(gif_result({"hd": HD, "sd": SD}).body) == HD


def test_sd_is_used_when_there_is_no_hd():
    assert best_media_url(gif_result({"sd": SD}).body) == SD


def test_the_silent_rendition_is_never_chosen():
    # Only 'silent'/'poster' offered: nothing with audio, so nothing is returned.
    assert best_media_url(gif_result({"silent": HD, "poster": SD}).body) is None


def test_a_non_string_url_is_ignored():
    assert best_media_url(gif_result({"hd": 42, "sd": SD}).body) == SD


def test_a_body_with_no_usable_shape_is_none():
    assert best_media_url(None) is None
    assert best_media_url(b"{not json") is None
    assert best_media_url(json.dumps({"gif": {}}).encode()) is None
    assert best_media_url(json.dumps({"gif": {"urls": "nope"}}).encode()) is None


# -- can_handle -------------------------------------------------------------


def test_a_redgifs_link_post_is_claimed():
    assert RedGifsHandler().can_handle(rg_post()) is True


def test_a_redgifs_embed_is_claimed_via_its_player_src():
    post = rg_post(
        content_href=None, domain=None, player_src="https://www.redgifs.com/ifr/" + GID
    )
    assert RedGifsHandler().can_handle(post) is True


def test_a_non_redgifs_post_is_left_alone():
    assert (
        RedGifsHandler().can_handle(
            rg_post(content_href="https://i.redd.it/a.jpg", domain="i.redd.it")
        )
        is False
    )


# -- resolve: the happy path ------------------------------------------------


async def test_the_hd_mp4_is_resolved_via_the_api():
    ctx = FakeContext(
        {AUTH_URL: token_result(), gif_api_url(GID): gif_result({"hd": HD, "sd": SD})}
    )
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [(c.url, c.ext, c.media_type) for c in candidates] == [
        (HD, "mp4", MediaType.VIDEO)
    ]


async def test_the_candidate_accepts_video_content_types():
    ctx = FakeContext(
        {AUTH_URL: token_result(), gif_api_url(GID): gif_result({"hd": HD})}
    )
    cand = (await RedGifsHandler().resolve(rg_post(), ctx))[0]
    assert cand.content_prefixes == ("video/", "application/octet-stream")


async def test_the_bearer_token_is_sent_on_the_lookup_only():
    ctx = FakeContext(
        {AUTH_URL: token_result("tok123"), gif_api_url(GID): gif_result({"hd": HD})}
    )
    await RedGifsHandler().resolve(rg_post(), ctx)
    assert ctx.fetches == [
        (AUTH_URL, None),  # the token request itself carries no auth
        (gif_api_url(GID), {"Authorization": "Bearer tok123"}),
    ]


async def test_the_token_is_reused_across_posts():
    # One handler instance resolves a whole job; the token is fetched once, not per post.
    ctx = FakeContext(
        {AUTH_URL: token_result(), gif_api_url(GID): gif_result({"hd": HD})}
    )
    handler = RedGifsHandler()
    await handler.resolve(rg_post(), ctx)
    await handler.resolve(rg_post(), ctx)
    assert [url for url, _ in ctx.fetches].count(AUTH_URL) == 1


# -- resolve: a rejected token is refreshed once ----------------------------


async def test_a_rejected_token_is_refreshed_and_the_lookup_retried():
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): [
                gif_result({}, ok=False, status=401),
                gif_result({"hd": HD}),
            ],
        }
    )
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [c.url for c in candidates] == [HD]
    # a fresh token was fetched after the 401
    assert [url for url, _ in ctx.fetches].count(AUTH_URL) == 2


# -- resolve: everything that can go wrong ----------------------------------


async def test_a_failed_token_request_is_skipped_without_a_lookup():
    ctx = FakeContext({AUTH_URL: token_result(ok=False, status=500)})
    assert await RedGifsHandler().resolve(rg_post(), ctx) == []
    assert [url for url, _ in ctx.fetches] == [
        AUTH_URL
    ]  # the lookup is never attempted
    assert ctx.skips and GID in ctx.skips[0][1]


async def test_a_failed_lookup_is_skipped_with_a_reason():
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): gif_result({}, ok=False, status=500),
        }
    )
    assert await RedGifsHandler().resolve(rg_post(), ctx) == []
    assert ctx.skips == [(rg_post().url, "redgifs: could not resolve video for " + GID)]


async def test_a_lookup_with_no_audio_rendition_is_skipped():
    ctx = FakeContext(
        {AUTH_URL: token_result(), gif_api_url(GID): gif_result({"silent": HD})}
    )
    assert await RedGifsHandler().resolve(rg_post(), ctx) == []
    assert ctx.skips and "could not resolve" in ctx.skips[0][1]


async def test_a_token_rejected_twice_gives_up_and_skips():
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): gif_result({}, ok=False, status=401),
        }
    )
    assert await RedGifsHandler().resolve(rg_post(), ctx) == []
    # tried the initial token and one refresh, then stopped
    assert [url for url, _ in ctx.fetches].count(AUTH_URL) == 2
    assert ctx.skips and "could not resolve" in ctx.skips[0][1]


# -- parse_username ---------------------------------------------------------


def test_the_uploader_is_read_and_lower_cased():
    assert parse_username(gif_by("CoolCreator").body) == "coolcreator"


def test_an_anonymous_upload_has_no_username():
    assert parse_username(gif_result({"hd": HD}).body) is None  # no userName field


def test_a_non_string_or_empty_username_is_none():
    assert parse_username(json.dumps({"gif": {"userName": ""}}).encode()) is None
    assert parse_username(json.dumps({"gif": {"userName": 7}}).encode()) is None


def test_username_from_a_bad_body_is_none():
    assert parse_username(None) is None
    assert parse_username(b"{not json") is None
    assert parse_username(json.dumps({"gif": "nope"}).encode()) is None


# -- parse_user_gifs --------------------------------------------------------


def test_a_user_page_yields_each_clips_best_url_and_the_page_count():
    urls, pages = parse_user_gifs(user_page([HD, SD], pages=3).body)
    assert (urls, pages) == ([HD, SD], 3)


def test_user_page_clips_without_a_usable_rendition_are_dropped():
    body = json.dumps(
        {"pages": 1, "gifs": [{"urls": {"hd": HD}}, {"urls": {"silent": SD}}, {}]}
    ).encode()
    assert parse_user_gifs(body) == ([HD], 1)


def test_a_bad_user_page_body_is_empty_with_zero_pages():
    assert parse_user_gifs(None) == ([], 0)
    assert parse_user_gifs(b"{not json") == ([], 0)
    assert parse_user_gifs(json.dumps({"gifs": []}).encode()) == ([], 0)  # no pages
    assert parse_user_gifs(b"[]") == ([], 0)  # a JSON array, not the expected object


def test_user_search_url_pages_newest_first():
    assert user_search_url("creator", 2) == (
        "https://api.redgifs.com/v2/users/creator/search?order=new&count=80&page=2"
    )


# -- scrape-all: page the uploader's whole profile --------------------------


async def test_scrape_all_pages_the_whole_profile():
    a, b, c = "https://media.redgifs.com/A.mp4", HD, SD
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): gif_by("creator"),
            user_search_url("creator", 1): user_page([a, b], pages=2),
            user_search_url("creator", 2): user_page([c], pages=2),
        },
        scrape_all=True,
    )
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [x.url for x in candidates] == [a, b, c]
    assert all(x.media_type is MediaType.VIDEO and x.ext == "mp4" for x in candidates)


async def test_a_scraped_profile_is_tagged_as_its_uploaders_collection():
    # A profile is a collection: the pipeline stores it in a folder of its own, under its own manifest.
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): gif_by("creator"),
            user_search_url("creator", 1): user_page([HD, SD], pages=1),
        },
        scrape_all=True,
    )
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [x.collection for x in candidates] == ["creator", "creator"]


async def test_a_single_clip_belongs_to_no_collection():
    # Without scrape-all the post yields one file, which belongs beside the source's others.
    ctx = FakeContext({AUTH_URL: token_result(), gif_api_url(GID): gif_by("creator")})
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [x.collection for x in candidates] == [None]


async def test_paging_paces_with_api_pause_not_the_scroll_pause():
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): gif_by("creator"),
            user_search_url("creator", 1): user_page([HD], pages=2),
            user_search_url("creator", 2): user_page([SD], pages=2),
        },
        scrape_all=True,
    )
    ctx.config = ctx.config.replace(scroll_pause=9.0, api_pause=0.25)
    await RedGifsHandler().resolve(rg_post(), ctx)
    assert ctx.slept == [0.25]


async def test_paging_can_be_left_unpaced():
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): gif_by("creator"),
            user_search_url("creator", 1): user_page([HD], pages=2),
            user_search_url("creator", 2): user_page([SD], pages=2),
        },
        scrape_all=True,
    )
    ctx.config = ctx.config.replace(api_pause=0.0)
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [x.url for x in candidates] == [HD, SD]
    assert ctx.slept == [0.0]


async def test_a_repeat_uploader_is_scraped_only_once_per_run():
    # The same handler instance carries the per-run blacklist across posts.
    handler = RedGifsHandler()
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): gif_by("creator"),
            gif_api_url("second"): gif_by("creator"),
            user_search_url("creator", 1): user_page([HD, SD], pages=1),
        },
        scrape_all=True,
    )
    first = await handler.resolve(rg_post(), ctx)
    second = await handler.resolve(
        rg_post(content_href="https://redgifs.com/watch/second"), ctx
    )
    assert [x.url for x in first] == [HD, SD]
    assert second == []  # covered by the first scrape, not re-paged
    assert "already scraped" in ctx.skips[-1][1]
    # the expensive profile endpoint was hit exactly once
    assert [u for u, _ in ctx.fetches].count(user_search_url("creator", 1)) == 1


async def test_scrape_all_falls_back_to_the_single_clip_for_an_anonymous_upload():
    ctx = FakeContext(
        {AUTH_URL: token_result(), gif_api_url(GID): gif_result({"hd": HD})},
        scrape_all=True,
    )
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [x.url for x in candidates] == [HD]  # no userName, so just the linked clip
    assert all("/users/" not in u for u, _ in ctx.fetches)  # profile never paged


async def test_scrape_all_falls_back_when_the_profile_reads_empty():
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): gif_by("creator"),
            user_search_url("creator", 1): user_page([], pages=0),
        },
        scrape_all=True,
    )
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [x.url for x in candidates] == [HD]  # empty profile: keep the linked clip
    assert candidates[0].collection is None  # ...beside the source's other files


async def test_scrape_all_falls_back_when_a_profile_page_request_fails():
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): gif_by("creator"),
            user_search_url("creator", 1): FetchResult(ok=False, status=500),
        },
        scrape_all=True,
    )
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [x.url for x in candidates] == [HD]  # paging failed; keep the linked clip


async def test_scrape_all_stops_when_a_page_repeats_instead_of_looping_forever():
    # A server that ignores the page param (returns page 1 forever) must still terminate.
    ctx = FakeContext(
        {
            AUTH_URL: token_result(),
            gif_api_url(GID): gif_by("creator"),
            user_search_url("creator", 1): user_page([HD, SD], pages=9),
            user_search_url("creator", 2): user_page([HD, SD], pages=9),
        },
        scrape_all=True,
    )
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [x.url for x in candidates] == [
        HD,
        SD,
    ]  # de-duplicated; the repeat page halts


async def test_without_scrape_all_a_named_uploader_still_yields_only_the_one_clip():
    ctx = FakeContext(
        {AUTH_URL: token_result(), gif_api_url(GID): gif_by("creator")}
    )  # scrape_all defaults off
    candidates = await RedGifsHandler().resolve(rg_post(), ctx)
    assert [x.url for x in candidates] == [HD]
    assert all("/users/" not in u for u, _ in ctx.fetches)
