"""
Tests for the engine: harvesting a listing, batching jobs, and the download bookkeeping.

Browser-free: `AsyncRedditExtractor` takes a custom browser and storage backend, so the whole pipeline runs here against
canned harvest data with no Playwright. Where `test_extractor_filtering` drives the pipeline to check what a PostFilter
narrows, this file covers the machinery around it: the scroll/pagination loop, concurrency and error capture, and the
rules deciding a file is already present, a duplicate, or a failure.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from reddit_extract.core.browser import FetchResult
from reddit_extract.core.extractor import (
    JS_HARVEST,
    JS_HARVEST_LEGACY,
    JS_IS_MODERN,
    JS_NEXT_PAGE,
    JS_SCROLL,
    AsyncRedditExtractor,
)
from reddit_extract.events import Events
from reddit_extract.exceptions import NoPostsFoundError
from reddit_extract.handlers import ImageHandler, TextHandler
from reddit_extract.handlers.base import MediaHandler
from reddit_extract.models.config import ExtractorConfig
from reddit_extract.models.filters import PostFilter
from reddit_extract.models.media import MediaCandidate, MediaType
from reddit_extract.models.post import Post
from reddit_extract.models.source import Subreddit
from reddit_extract.storage import FilesystemStorage, MemoryStorage
from tests.test_extractor_filtering import harvested

JPEG = FetchResult(ok=True, status=200, content_type="image/jpeg", body=b"imagebytes")


class FakeMouse:
    def __init__(self) -> None:
        self.wheels: List[tuple[int, int]] = []

    async def wheel(self, dx: int, dy: int) -> None:
        self.wheels.append((dx, dy))


class FakePage:
    """
    A listing page serving a scripted sequence of harvest rounds.

    Each evaluate of the harvest JS returns the next round, then keeps returning the final one (which is what a listing
    that has stopped producing new posts looks like).
    """

    def __init__(
        self,
        rounds: List[List[dict[str, Any]]],
        *,
        modern: bool = True,
        next_urls: tuple[str, ...] = (),
        fail_urls: tuple[str, ...] = (),
    ) -> None:
        self.rounds = [list(r) for r in rounds] or [[]]
        self.modern = modern
        self.next_urls = list(next_urls)
        self.fail_urls = fail_urls
        self.mouse = FakeMouse()
        self.scrolls: List[int] = []  # px passed to each JS_SCROLL of the modern feed
        self.gotos: List[str] = []
        self.waits: List[int] = []
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.gotos.append(url)
        if any(bad in url for bad in self.fail_urls):
            raise RuntimeError("navigation failed")

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == JS_IS_MODERN:
            return self.modern
        if js in (JS_HARVEST, JS_HARVEST_LEGACY):
            return self.rounds.pop(0) if len(self.rounds) > 1 else self.rounds[0]
        if js == JS_SCROLL:
            self.scrolls.append(arg)
            return None
        if js == JS_NEXT_PAGE:
            return self.next_urls.pop(0) if self.next_urls else None
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    """Stands in for BrowserManager: a fresh canned page per job, canned fetches, no Chromium."""

    def __init__(
        self,
        rounds: List[List[dict[str, Any]]],
        *,
        modern: bool = True,
        next_urls: tuple[str, ...] = (),
        fail_urls: tuple[str, ...] = (),
        results: dict[str, FetchResult] | None = None,
        posts_render: bool = True,
    ) -> None:
        self.rounds = rounds
        self.modern = modern
        self.next_urls = next_urls
        self.fail_urls = fail_urls
        self.results = dict(results or {})
        self.posts_render = posts_render
        self.fetched: List[str] = []
        self.pages: List[FakePage] = []
        self.started = 0
        #: the headers each download was asked to send, so a candidate's own can be observed
        self.download_headers: List[dict[str, str] | None] = []

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        pass

    async def new_page(self) -> FakePage:
        page = FakePage(
            self.rounds,
            modern=self.modern,
            next_urls=self.next_urls,
            fail_urls=self.fail_urls,
        )
        self.pages.append(page)
        return page

    async def download(
        self,
        url: str,
        timeout_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        # The real manager may route this straight out to the network; a fake has only the one route.
        self.download_headers.append(headers)
        return await self.fetch(url, timeout_ms, headers)

    async def dismiss_gates(self, page: Any) -> None:
        pass

    async def wait_for_posts(self, page: Any, timeout_ms: int) -> bool:
        return self.posts_render

    async def fetch(
        self,
        url: str,
        timeout_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        self.fetched.append(url)
        return self.results.get(url, JPEG)


class CountingStorage(MemoryStorage):
    """MemoryStorage that counts manifest writes, so the flush cadence is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.manifest_writes = 0

    def write_manifest(self, key: str, data: Any) -> str:
        self.manifest_writes += 1
        return super().write_manifest(key, data)


class AlreadyThereStorage(MemoryStorage):
    """A backend that reports every file as already stored."""

    def exists(self, key: str, filename: str) -> bool:
        return True


class BoomHandler(MediaHandler):
    """A handler that always fails, standing in for a buggy custom one."""

    media_type = MediaType.IMAGE

    def can_handle(self, post: Post) -> bool:
        return True

    async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
        raise RuntimeError("handler exploded")


def build(rounds, *, storage=None, handlers=None, **cfg_kwargs):
    """An extractor wired to fake infrastructure, plus its storage and browser."""
    if isinstance(rounds, FakeBrowser):
        browser = rounds
    else:
        browser = FakeBrowser(rounds)
    storage = storage if storage is not None else MemoryStorage()
    config = ExtractorConfig(delay=0.0, scroll_pause=0.0, **cfg_kwargs)
    rex = AsyncRedditExtractor(
        config,
        storage=storage,
        browser=browser,  # type: ignore[arg-type]
        handlers=handlers,
    )
    return rex, storage, browser


async def run(rounds, *, limit=None, media_types=MediaType.ALL, **kwargs):
    """Extract one source and return its result, storage, and browser."""
    rex, storage, browser = build(rounds, **kwargs)
    if limit is None:
        limit = sum(
            len(r)
            for r in (rounds.rounds if isinstance(rounds, FakeBrowser) else rounds)
        )
    result = await rex.extract(Subreddit("pics", limit=limit), media_types=media_types)
    return result, storage, browser


# -- construction -----------------------------------------------------------


def test_keyword_overrides_replace_config_fields():
    rex = AsyncRedditExtractor(headless=False, delay=0.0)
    assert rex.config.headless is False
    assert rex.config.delay == 0.0


def test_keyword_overrides_layer_onto_a_given_config():
    rex = AsyncRedditExtractor(ExtractorConfig(output_dir="/out"), headless=False)
    assert rex.config.output_dir == "/out"
    assert rex.config.headless is False


def test_an_invalid_override_is_rejected_at_construction():
    with pytest.raises(ValueError, match="delay must be >= 0"):
        AsyncRedditExtractor(delay=-1.0)


def test_the_default_chain_is_used_when_no_handlers_are_given():
    assert [h.name for h in AsyncRedditExtractor().handlers][0] == "ImageHandler"


def test_given_handlers_replace_the_default_chain_entirely():
    rex = AsyncRedditExtractor(handlers=[TextHandler()])
    assert [h.name for h in rex.handlers] == ["TextHandler"]


# -- the handler chain ------------------------------------------------------


def test_a_registered_handler_takes_precedence_by_default():
    # A custom handler is only useful if it can outrank the built-in it is replacing.
    rex = AsyncRedditExtractor()
    custom = BoomHandler()
    rex.register_handler(custom)
    assert rex.handlers[0] is custom


def test_a_handler_can_be_appended_as_a_fallback():
    rex = AsyncRedditExtractor()
    custom = BoomHandler()
    rex.register_handler(custom, prepend=False)
    assert rex.handlers[-1] is custom


def test_the_handlers_property_hands_out_a_copy():
    # Mutating the returned list must not quietly reconfigure the extractor.
    rex = AsyncRedditExtractor()
    rex.handlers.clear()
    assert rex.handlers != []


# -- lifecycle --------------------------------------------------------------


async def test_the_context_manager_starts_and_closes_the_browser():
    browser = FakeBrowser([[]])
    async with AsyncRedditExtractor(browser=browser) as rex:  # type: ignore[arg-type]
        assert rex.config is not None
        assert browser.started == 1


async def test_extract_starts_the_browser_on_its_own():
    # Skipping the context manager is allowed; the call auto-starts.
    result, _, browser = await run([[harvested("a")]])
    assert browser.started == 1
    assert result.posts_scanned == 1


# -- storage selection ------------------------------------------------------


def test_output_dir_cannot_be_combined_with_a_custom_backend():
    # The backend already knows where it writes; honoring output_dir too would be ambiguous.
    rex = AsyncRedditExtractor(storage=MemoryStorage())
    with pytest.raises(ValueError, match="output_dir cannot be combined"):
        rex._resolve_storage("/tmp/out")


def test_a_per_call_output_dir_builds_a_filesystem_backend(tmp_path):
    storage = AsyncRedditExtractor()._resolve_storage(str(tmp_path))
    assert isinstance(storage, FilesystemStorage)
    assert storage.root == str(tmp_path)


def test_the_default_backend_follows_the_configured_output_dir():
    storage = AsyncRedditExtractor(output_dir="downloads")._resolve_storage(None)
    assert isinstance(storage, FilesystemStorage)
    assert storage.root == "downloads"


def test_the_default_backend_is_built_once_and_reused():
    rex = AsyncRedditExtractor()
    assert rex._resolve_storage(None) is rex._resolve_storage(None)


# -- extension and grouping -------------------------------------------------
# (the naming rules themselves live in test_naming.py)


def candidate(
    url="https://i.redd.it/a.jpg", media_type=MediaType.IMAGE, ext=None, collection=None
):
    return MediaCandidate(
        url=url, media_type=media_type, ext=ext, collection=collection
    )


def test_a_candidate_without_an_extension_takes_it_from_the_url():
    assert AsyncRedditExtractor._extension(candidate("https://x/a.PNG")) == "png"


def test_an_extension_less_video_falls_back_to_mp4():
    cand = candidate("https://v.redd.it/abc123", media_type=MediaType.VIDEO)
    assert AsyncRedditExtractor._extension(cand) == "mp4"


def test_an_extension_less_image_falls_back_to_jpg():
    assert AsyncRedditExtractor._extension(candidate("https://x/a")) == "jpg"


def test_candidates_with_no_collection_form_one_unnamed_group():
    cands = [candidate(), candidate("https://i.redd.it/b.jpg")]
    assert AsyncRedditExtractor._grouped(cands) == [("", cands)]


def test_each_collection_gets_its_own_group_in_resolution_order():
    first = candidate(collection="alice")
    own = candidate("https://i.redd.it/b.jpg")
    second = candidate("https://i.redd.it/c.jpg", collection="bob")
    third = candidate("https://i.redd.it/d.jpg", collection="alice")
    assert AsyncRedditExtractor._grouped([first, own, second, third]) == [
        ("alice", [first, third]),
        ("", [own]),
        ("bob", [second]),
    ]


def test_a_collection_name_is_slugged_once_for_the_whole_group():
    # The folder, the names inside it, and the manifest record must all agree on one spelling.
    cand = candidate(collection="Someone's Profile")
    assert AsyncRedditExtractor._grouped([cand])[0][0] == "Someone_s_Profile"


def test_a_collection_name_with_nothing_sluggable_is_no_collection_at_all():
    # There is no folder name to be had, so the files belong beside the source's own.
    cand = candidate(collection="!!!")
    assert AsyncRedditExtractor._grouped([cand]) == [("", [cand])]


# -- harvesting: the modern infinite scroll ---------------------------------


async def test_posts_are_accumulated_across_scrolls():
    # The modern listing recycles cards out of the DOM, so each round must be banked.
    rounds = [[harvested("a")], [harvested("b")], [harvested("c")]]
    result, _, _ = await run(rounds, limit=3)
    assert [p.id for p in result.posts] == ["a", "b", "c"]


async def test_a_post_seen_twice_is_only_counted_once():
    rounds = [[harvested("a")], [harvested("a"), harvested("b")]]
    result, _, _ = await run(rounds, limit=2)
    assert result.posts_scanned == 2


async def test_scrolling_stops_once_the_limit_is_reached():
    rounds = [[harvested("a")], [harvested("b")]]
    rex, _, browser = build(rounds)
    await rex.extract(Subreddit("pics", limit=1), media_types=MediaType.ALL)
    assert browser.pages[0].scrolls == []  # the first round already sufficed


async def test_a_round_that_overshoots_the_limit_is_truncated():
    result, _, _ = await run(
        [[harvested("a"), harvested("b"), harvested("c")]], limit=2
    )
    assert [p.id for p in result.posts] == ["a", "b"]


async def test_scrolling_gives_up_after_enough_stale_rounds():
    # Without this the harvester would scroll a listing shorter than the limit forever.
    rex, _, browser = build([[harvested("a")]], max_stale_scrolls=2)
    result = await rex.extract(Subreddit("pics", limit=100), media_types=MediaType.ALL)
    assert result.posts_scanned == 1
    # one scroll for the productive round, then one per stale round before giving up
    assert len(browser.pages[0].scrolls) == 3


async def test_a_fresh_round_resets_the_stale_counter():
    rounds = [[harvested("a")], [harvested("a")], [harvested("a"), harvested("b")]]
    rex, _, _ = build(rounds, max_stale_scrolls=2)
    result = await rex.extract(Subreddit("pics", limit=100), media_types=MediaType.ALL)
    assert result.posts_scanned == 2  # the stale round did not end the harvest


async def test_scrolling_uses_the_configured_distance():
    rex, _, browser = build([[harvested("a")]], max_stale_scrolls=1, scroll_px=999)
    await rex.extract(Subreddit("pics", limit=100), media_types=MediaType.ALL)
    scrolls = browser.pages[0].scrolls
    assert scrolls and set(scrolls) == {999}  # by the configured distance


async def test_each_scroll_is_reported():
    rounds = [[harvested("a")], [harvested("b"), harvested("c")]]
    rex, _, _ = build(rounds)
    scrolls: List[tuple[int, int]] = []
    await rex.extract(
        Subreddit("pics", limit=3),
        media_types=MediaType.ALL,
        events=Events(on_scroll=lambda src, total, new: scrolls.append((total, new))),
    )
    assert scrolls == [(1, 1), (3, 2)]  # (running total, new this round)


async def test_the_harvest_is_reported_once_it_finishes():
    counts: List[int] = []
    rex, _, _ = build([[harvested("a"), harvested("b")]])
    await rex.extract(
        Subreddit("pics", limit=2),
        media_types=MediaType.ALL,
        events=Events(on_harvested=lambda src, count: counts.append(count)),
    )
    assert counts == [2]


async def test_a_listing_that_renders_no_posts_raises():
    browser = FakeBrowser([[]], posts_render=False)
    rex, _, _ = build(browser)
    with pytest.raises(NoPostsFoundError, match="No posts rendered"):
        await rex.extract(Subreddit("pics"), media_types=MediaType.ALL)


async def test_the_no_posts_error_names_the_url_it_tried():
    browser = FakeBrowser([[]], posts_render=False)
    rex, _, _ = build(browser)
    with pytest.raises(NoPostsFoundError) as exc:
        await rex.extract(Subreddit("pics"), media_types=MediaType.ALL)
    assert exc.value.url == "https://www.reddit.com/r/pics/new/"
    assert "profile_dir" in str(exc.value)  # tells the user how to fix a login wall


# -- harvesting: the flair filter Reddit applies for us ---------------------


async def test_a_single_flair_is_pushed_into_the_listing_url():
    # Filtering upstream means the scroll loop never has to page past the posts it would discard.
    rex, _, browser = build([[harvested("a", flair="Art")]])
    await rex.extract(
        Subreddit("pics"),
        media_types=MediaType.ALL,
        post_filter=PostFilter(flairs="Art"),
    )
    assert (
        browser.pages[0].gotos[0]
        == "https://www.reddit.com/r/pics/new/?f=flair_name%3A%22art%22"
    )


async def test_several_flairs_leave_the_listing_url_alone():
    rex, _, browser = build([[harvested("a", flair="Art")]])
    await rex.extract(
        Subreddit("pics"),
        media_types=MediaType.ALL,
        post_filter=PostFilter(flairs="art,photos"),
    )
    assert browser.pages[0].gotos[0] == "https://www.reddit.com/r/pics/new/"


async def test_the_client_side_flair_check_still_runs_on_a_filtered_listing():
    # Belt and braces: if a listing ever ignores ?f=, the wrong-flair posts must still be dropped.
    rex, _, _ = build([[harvested("a", flair="Art"), harvested("b", flair="Other")]])
    result = await rex.extract(
        Subreddit("pics", limit=2),
        media_types=MediaType.ALL,
        post_filter=PostFilter(flairs="art"),
    )
    assert [p.id for p in result.posts] == ["a"]
    assert result.posts_filtered == 1


async def test_a_flair_filtered_listing_with_no_matches_is_empty_not_an_error():
    # An unknown or simply unused flair renders an empty listing. That is a real answer, not a broken source.
    browser = FakeBrowser([[]], posts_render=False)
    rex, _, _ = build(browser)
    result = await rex.extract(
        Subreddit("pics"),
        media_types=MediaType.ALL,
        post_filter=PostFilter(flairs="nosuchflair"),
    )
    assert result.posts_scanned == 0
    assert result.error is None


async def test_an_unfiltered_listing_that_renders_nothing_still_raises():
    # Only the flair-filtered case is exempt; a plain empty listing is still a fault worth reporting.
    browser = FakeBrowser([[]], posts_render=False)
    rex, _, _ = build(browser)
    with pytest.raises(NoPostsFoundError):
        await rex.extract(
            Subreddit("pics"),
            media_types=MediaType.ALL,
            post_filter=PostFilter(flairs="art,photos"),
        )


# -- harvesting: the legacy-paginated UI ------------------------------------


async def test_a_legacy_listing_follows_its_next_link():
    # Reddit still serves some listings (multireddits) on the old UI, which paginates.
    browser = FakeBrowser(
        [[harvested("a")], [harvested("b")]],
        modern=False,
        next_urls=("https://old.reddit.com/r/pics/?count=25&after=t3_a",),
    )
    result, _, _ = await run(browser, limit=2)
    assert [p.id for p in result.posts] == ["a", "b"]
    assert (
        browser.pages[0].gotos[-1]
        == "https://old.reddit.com/r/pics/?count=25&after=t3_a"
    )


async def test_a_legacy_listing_never_scrolls():
    browser = FakeBrowser([[harvested("a")]], modern=False)
    await run(browser, limit=2)
    assert browser.pages[0].scrolls == []


async def test_a_legacy_listing_stops_at_its_last_page():
    browser = FakeBrowser([[harvested("a")]], modern=False, next_urls=())
    result, _, _ = await run(browser, limit=100)
    assert result.posts_scanned == 1  # no next link, so the harvest ends


# -- posts nobody wants -----------------------------------------------------


async def test_a_post_no_handler_claims_is_scanned_but_not_matched():
    result, _, browser = await run([[harvested("p", type="unsupported")]], limit=1)
    assert result.posts_scanned == 1
    assert result.posts_matched == 0
    assert browser.fetched == []


async def test_a_media_type_the_job_did_not_ask_for_is_left_alone():
    rounds = [[harvested("a"), harvested("t", type="text")]]
    result, _, _ = await run(rounds, limit=2, media_types=MediaType.IMAGE)
    assert [p.id for p in result.posts] == ["a"]


async def test_a_post_whose_handler_yields_nothing_is_not_matched():
    # A .gif image post with GIFs disabled: claimed by the handler, then dropped by it.
    result, _, browser = await run(
        [[harvested("a", content_href="https://i.redd.it/a.gif")]]
    )
    assert result.posts_matched == 0
    assert browser.fetched == []


async def test_a_text_post_is_saved_as_a_markdown_document():
    # Text posts are archived as .md (metadata header + body), not fetched from a URL.
    result, storage, browser = await run(
        [[harvested("t", type="text")]], media_types=MediaType.TEXT
    )
    assert result.posts_matched == 1
    assert [p.id for p in result.posts] == ["t"]
    assert result.media_found == 1
    assert result.media_saved == 1
    assert browser.fetched == []  # the bytes are composed, not downloaded
    assert list(storage.files["pics"]) == ["0001_photo_t.md"]
    assert b"title:" in storage.files["pics"]["0001_photo_t.md"]


# -- a handler that misbehaves ----------------------------------------------


async def test_a_failing_handler_is_recorded_and_the_job_carries_on():
    # One bad post (or a buggy custom handler) must not abort a long job.
    rex, _, browser = build(
        [[harvested("a"), harvested("b")]], handlers=[BoomHandler()]
    )
    result = await rex.extract(Subreddit("pics", limit=2), media_types=MediaType.ALL)
    assert len(result.failures) == 2
    assert result.failures[0][1] == "handler BoomHandler error: handler exploded"
    assert result.posts_matched == 0


async def test_a_handler_failure_is_reported_through_on_skip():
    skips: List[tuple[str, str]] = []
    rex, _, _ = build([[harvested("a")]], handlers=[BoomHandler()])
    await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        events=Events(on_skip=lambda src, url, reason: skips.append((url, reason))),
    )
    assert "handler exploded" in skips[0][1]


async def test_a_cancelled_handler_does_not_become_a_failure():
    # Cancellation is the caller shutting the job down, not a post going wrong.
    class CancelHandler(MediaHandler):
        media_type = MediaType.IMAGE

        def can_handle(self, post: Post) -> bool:
            return True

        async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
            raise asyncio.CancelledError()

    rex, _, _ = build([[harvested("a")]], handlers=[CancelHandler()])
    with pytest.raises(asyncio.CancelledError):
        await rex.extract(Subreddit("pics", limit=1), media_types=MediaType.ALL)


# -- media already accounted for --------------------------------------------


async def test_a_url_already_in_the_manifest_is_not_downloaded_again():
    storage = MemoryStorage()
    storage.manifests["pics"] = {
        "source": "pics",
        "files": {"0001.jpg": {"media_url": "https://i.redd.it/a.jpg"}},
    }
    result, _, browser = await run([[harvested("a"), harvested("b")]], storage=storage)
    assert result.skipped_known == 1
    assert browser.fetched == ["https://i.redd.it/b.jpg"]


async def test_numbering_continues_past_what_the_manifest_already_holds():
    storage = MemoryStorage()
    storage.manifests["pics"] = {
        "source": "pics",
        "files": {"0001.jpg": {"media_url": "https://i.redd.it/a.jpg"}},
    }
    await run([[harvested("b")]], storage=storage)
    assert list(storage.files["pics"]) == ["0002_photo_b.jpg"]


async def test_a_file_already_in_storage_is_not_downloaded_again():
    result, _, browser = await run([[harvested("a")]], storage=AlreadyThereStorage())
    assert result.skipped_existing == 1
    assert result.media_saved == 0
    assert browser.fetched == []


# -- content de-duplication -------------------------------------------------


async def test_identical_bytes_at_a_new_url_are_saved_twice_by_default():
    # Hashing costs time, so it stays opt-in.
    result, storage, _ = await run([[harvested("a"), harvested("b")]])
    assert result.media_saved == 2
    assert result.skipped_duplicate == 0
    assert sorted(storage.files["pics"]) == ["0001_photo_a.jpg", "0002_photo_b.jpg"]


async def test_identical_bytes_at_a_new_url_are_skipped_when_dedupe_is_on():
    # A crosspost or repost is the same upload wearing a new URL.
    result, storage, _ = await run(
        [[harvested("a"), harvested("b")]], dedupe_by_hash=True
    )
    assert result.media_saved == 1
    assert result.skipped_duplicate == 1
    assert list(storage.files["pics"]) == ["0001_photo_a.jpg"]


async def test_a_duplicate_is_reported_through_on_skip():
    skips: List[tuple[str, str]] = []
    rex, _, _ = build([[harvested("a"), harvested("b")]], dedupe_by_hash=True)
    await rex.extract(
        Subreddit("pics", limit=2),
        media_types=MediaType.ALL,
        events=Events(on_skip=lambda src, url, reason: skips.append((url, reason))),
    )
    assert skips == [("https://i.redd.it/b.jpg", "duplicate content")]


async def test_dedupe_records_the_hash_it_saved():
    result, storage, _ = await run([[harvested("a")]], dedupe_by_hash=True)
    # sha256 of b"imagebytes"
    digest = result.items[0].sha256
    assert digest is not None and len(digest) == 64
    assert storage.manifests["pics"]["files"]["0001_photo_a.jpg"]["sha256"] == digest


async def test_different_bytes_are_both_kept_under_dedupe():
    other = FetchResult(
        ok=True, status=200, content_type="image/jpeg", body=b"different"
    )
    browser = FakeBrowser(
        [[harvested("a"), harvested("b")]], results={"https://i.redd.it/b.jpg": other}
    )
    result, _, _ = await run(browser, dedupe_by_hash=True)
    assert result.media_saved == 2
    assert result.skipped_duplicate == 0


async def test_a_hash_already_in_the_manifest_blocks_a_redownload():
    # Resuming a run must not re-save what a previous one already deduplicated.
    result, storage, _ = await run([[harvested("a")]], dedupe_by_hash=True)
    digest = result.items[0].sha256

    second_storage = MemoryStorage()
    second_storage.manifests["pics"] = {
        "source": "pics",
        "files": {
            "0001.jpg": {"media_url": "https://i.redd.it/other.jpg", "sha256": digest}
        },
    }
    again, _, _ = await run(
        [[harvested("a")]], storage=second_storage, dedupe_by_hash=True
    )
    assert again.skipped_duplicate == 1
    assert again.media_saved == 0


# -- downloads that fail ----------------------------------------------------


async def test_a_failed_download_is_recorded_with_its_reason():
    gone = FetchResult(ok=False, status=404, error="HTTP 404")
    browser = FakeBrowser([[harvested("a")]], results={"https://i.redd.it/a.jpg": gone})
    result, storage, _ = await run(browser)
    assert result.failures == [("https://i.redd.it/a.jpg", "HTTP 404")]
    assert result.media_saved == 0
    assert storage.files["pics"] == {}


async def test_a_failure_with_no_error_text_still_gets_a_reason():
    browser = FakeBrowser(
        [[harvested("a")]], results={"https://i.redd.it/a.jpg": FetchResult(ok=False)}
    )
    result, _, _ = await run(browser)
    assert result.failures == [("https://i.redd.it/a.jpg", "download failed")]


async def test_a_failed_download_is_reported_through_on_skip():
    gone = FetchResult(ok=False, status=404, error="HTTP 404")
    browser = FakeBrowser([[harvested("a")]], results={"https://i.redd.it/a.jpg": gone})
    skips: List[tuple[str, str]] = []
    rex, _, _ = build(browser)
    await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        events=Events(on_skip=lambda src, url, reason: skips.append((url, reason))),
    )
    assert skips == [("https://i.redd.it/a.jpg", "HTTP 404")]


async def test_a_post_still_counts_as_matched_when_its_download_fails():
    # The filter did want it; the CDN is what let the job down.
    gone = FetchResult(ok=False, status=404, error="HTTP 404")
    browser = FakeBrowser([[harvested("a")]], results={"https://i.redd.it/a.jpg": gone})
    result, _, _ = await run(browser)
    assert result.posts_matched == 1


# -- what a saved item records ----------------------------------------------


async def test_a_saved_item_carries_its_posts_provenance():
    result, _, _ = await run([[harvested("a")]])
    item = result.items[0]
    assert item.filename == "0001_photo_a.jpg"
    assert item.path == "memory://pics/0001_photo_a.jpg"
    assert item.downloaded is True
    assert item.size == len(b"imagebytes")
    assert item.post_id == "a"
    assert item.author == "alice"
    assert item.title == "photo a"
    assert item.source_key == "pics"
    assert item.collection is None


async def test_saving_is_announced():
    saved: List[str] = []
    rex, _, _ = build([[harvested("a")]])
    await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        events=Events(on_media_saved=lambda src, item: saved.append(item.filename)),
    )
    assert saved == ["0001_photo_a.jpg"]


async def test_the_result_reports_where_everything_went():
    result, _, _ = await run([[harvested("a")]])
    assert result.output_dir == "memory://pics"
    assert result.manifest_path == "memory://pics/manifest.json"
    assert result.finished_at is not None
    assert result.duration is not None


# -- the manifest -----------------------------------------------------------


async def test_the_manifest_is_written_at_the_end_of_a_job():
    _, storage, _ = await run([[harvested("a")]])
    assert list(storage.manifests["pics"]["files"]) == ["0001_photo_a.jpg"]


async def test_the_manifest_is_flushed_on_the_configured_cadence():
    # A long job that dies should not lose its whole index.
    storage = CountingStorage()
    await run(
        [[harvested("a"), harvested("b"), harvested("c")]],
        storage=storage,
        manifest_flush_every=2,
    )
    assert storage.manifest_writes == 2  # one mid-run flush, plus the final write


async def test_flushing_after_every_file_is_the_default():
    storage = CountingStorage()
    await run([[harvested("a"), harvested("b")]], storage=storage)
    assert storage.manifest_writes == 3  # one per save, plus the final write


async def test_a_dry_run_writes_no_manifest():
    storage = CountingStorage()
    rex, _, _ = build([[harvested("a")]], storage=storage)
    result = await rex.extract(
        Subreddit("pics", limit=1), media_types=MediaType.ALL, dry_run=True
    )
    assert storage.manifest_writes == 0
    assert result.manifest_path is None


# -- file names -------------------------------------------------------------


async def test_a_file_is_named_after_the_post_it_came_from():
    _, storage, _ = await run([[harvested("a", title="Hello, World!")]])
    assert list(storage.files["pics"]) == ["0001_Hello_World.jpg"]


async def test_a_post_with_no_usable_title_falls_back_to_its_index():
    _, storage, _ = await run([[harvested("a", title="***")]])
    assert list(storage.files["pics"]) == ["0001.jpg"]


async def test_a_multi_file_post_repeats_the_title_across_its_parts():
    class TwoFileHandler(MediaHandler):
        media_type = MediaType.IMAGE

        def can_handle(self, post: Post) -> bool:
            return True

        async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
            return [
                candidate("https://i.redd.it/one.jpg"),
                candidate("https://i.redd.it/two.jpg"),
            ]

    _, storage, _ = await run(
        [[harvested("a", title="Sunset Ridge")]], handlers=[TwoFileHandler()]
    )
    assert sorted(storage.files["pics"]) == [
        "0001_01_Sunset_Ridge.jpg",
        "0001_02_Sunset_Ridge.jpg",
    ]


# -- collections ------------------------------------------------------------


class CollectionHandler(MediaHandler):
    """A scrape-all style handler: one post, a whole named collection of files."""

    media_type = MediaType.IMAGE

    def __init__(self, collection: str = "creator", count: int = 2) -> None:
        self.collection = collection
        self.count = count

    def can_handle(self, post: Post) -> bool:
        return True

    async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
        return [
            candidate(
                "https://media.example/{}/{}.jpg".format(self.collection, n),
                collection=self.collection,
            )
            for n in range(1, self.count + 1)
        ]


async def test_a_collection_lands_in_its_own_folder():
    _, storage, _ = await run([[harvested("a")]], handlers=[CollectionHandler()])
    assert "pics" not in storage.files or storage.files["pics"] == {}
    assert sorted(storage.files["pics/creator"]) == [
        "0001_creator.jpg",
        "0002_creator.jpg",
    ]


async def test_a_collections_files_are_named_after_the_collection_not_the_post():
    # The post's title says nothing about the other 200 files the profile happens to hold.
    _, storage, _ = await run(
        [[harvested("a", title="Check out this clip")]], handlers=[CollectionHandler()]
    )
    assert all("Check" not in name for name in storage.files["pics/creator"])


async def test_a_collection_numbers_every_file_of_its_own():
    # Unlike a gallery, these are separate uploads, so each takes an index rather than a part number.
    _, storage, _ = await run([[harvested("a")]], handlers=[CollectionHandler(count=3)])
    assert sorted(storage.files["pics/creator"]) == [
        "0001_creator.jpg",
        "0002_creator.jpg",
        "0003_creator.jpg",
    ]


async def test_a_collection_gets_a_manifest_of_its_own():
    _, storage, _ = await run([[harvested("a")]], handlers=[CollectionHandler()])
    manifest = storage.manifests["pics/creator"]
    assert (manifest["source"], manifest["collection"]) == ("pics", "creator")
    assert sorted(manifest["files"]) == ["0001_creator.jpg", "0002_creator.jpg"]


async def test_the_sources_manifest_stays_at_post_level():
    # The point of the arrangement: a post that resolved to a whole profile costs one entry, not N.
    _, storage, _ = await run([[harvested("a")]], handlers=[CollectionHandler(count=5)])
    assert storage.manifests["pics"]["files"] == {}
    record = storage.manifests["pics"]["collections"]["creator"]
    assert record["post_id"] == "a"
    assert record["name"] == "creator"
    assert record["files"] == 5


async def test_a_collection_name_is_slugged_into_its_folder():
    _, storage, _ = await run(
        [[harvested("a")]], handlers=[CollectionHandler(collection="Some Creator")]
    )
    assert sorted(storage.files["pics/Some_Creator"]) == [
        "0001_Some_Creator.jpg",
        "0002_Some_Creator.jpg",
    ]


async def test_a_collection_resumes_its_own_numbering_across_runs():
    storage = MemoryStorage()
    storage.manifests["pics/creator"] = {
        "source": "pics",
        "collection": "creator",
        "files": {"0004_creator.jpg": {"media_url": "https://media.example/old.jpg"}},
    }
    await run([[harvested("a")]], storage=storage, handlers=[CollectionHandler()])
    assert sorted(storage.files["pics/creator"]) == [
        "0005_creator.jpg",
        "0006_creator.jpg",
    ]


async def test_a_collections_own_manifest_is_what_skips_a_known_url():
    storage = MemoryStorage()
    storage.manifests["pics/creator"] = {
        "source": "pics",
        "collection": "creator",
        "files": {
            "0001_creator.jpg": {"media_url": "https://media.example/creator/1.jpg"}
        },
    }
    result, _, browser = await run(
        [[harvested("a")]], storage=storage, handlers=[CollectionHandler()]
    )
    assert result.skipped_known == 1
    assert browser.fetched == ["https://media.example/creator/2.jpg"]


async def test_two_collections_from_one_listing_stay_apart():
    class TwoProfileHandler(MediaHandler):
        media_type = MediaType.IMAGE

        def can_handle(self, post: Post) -> bool:
            return True

        async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
            name = "creator_" + post.id
            return [
                candidate("https://media.example/{}.jpg".format(name), collection=name)
            ]

    _, storage, _ = await run(
        [[harvested("a"), harvested("b")]], handlers=[TwoProfileHandler()]
    )
    assert list(storage.files["pics/creator_a"]) == ["0001_creator_a.jpg"]
    assert list(storage.files["pics/creator_b"]) == ["0001_creator_b.jpg"]
    assert sorted(storage.manifests["pics"]["collections"]) == [
        "creator_a",
        "creator_b",
    ]


async def test_a_saved_collection_item_records_where_it_went():
    result, _, _ = await run([[harvested("a")]], handlers=[CollectionHandler(count=1)])
    item = result.items[0]
    assert item.collection == "creator"
    assert item.source_key == "pics/creator"
    assert item.path == "memory://pics/creator/0001_creator.jpg"


async def test_a_dry_run_plans_a_collection_without_creating_its_folder():
    rex, storage, browser = build([[harvested("a")]], handlers=[CollectionHandler()])
    result = await rex.extract(
        Subreddit("pics", limit=1), media_types=MediaType.ALL, dry_run=True
    )
    assert [i.filename for i in result.items] == [
        "0001_creator.jpg",
        "0002_creator.jpg",
    ]
    assert storage.files == {}
    assert browser.fetched == []


# -- dry runs ---------------------------------------------------------------


async def test_a_dry_run_plans_files_without_writing_them():
    rex, storage, browser = build([[harvested("a"), harvested("b")]])
    result = await rex.extract(
        Subreddit("pics", limit=2), media_types=MediaType.ALL, dry_run=True
    )
    assert result.media_found == 2
    assert result.media_saved == 0
    assert [i.filename for i in result.items] == [
        "0001_photo_a.jpg",
        "0002_photo_b.jpg",
    ]
    assert all(i.downloaded is False for i in result.items)
    assert browser.fetched == []
    assert storage.files == {}  # prepare() never even ran


async def test_a_dry_run_announces_what_it_found():
    found: List[str] = []
    rex, _, _ = build([[harvested("a")]])
    await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        dry_run=True,
        events=Events(on_media_found=lambda src, item: found.append(item.url)),
    )
    assert found == ["https://i.redd.it/a.jpg"]


# -- pages are cleaned up ---------------------------------------------------


async def test_the_listing_page_is_closed_when_the_job_ends():
    _, _, browser = await run([[harvested("a")]])
    assert browser.pages[0].closed is True


async def test_the_listing_page_is_closed_even_when_the_job_raises():
    # Otherwise a batch would leak a tab per failed source.
    browser = FakeBrowser([[]], posts_render=False)
    rex, _, _ = build(browser)
    with pytest.raises(NoPostsFoundError):
        await rex.extract(Subreddit("pics"), media_types=MediaType.ALL)
    assert browser.pages[0].closed is True


# -- batch ------------------------------------------------------------------


async def test_batch_returns_results_in_input_order():
    rex, _, _ = build([[harvested("a")]])
    results = await rex.batch(["r/one", "r/two", "r/three"], media_types=MediaType.ALL)
    assert [r.key for r in results] == ["one", "two", "three"]


async def test_batch_accepts_source_objects_as_well_as_strings():
    rex, _, _ = build([[harvested("a")]])
    results = await rex.batch([Subreddit("one"), "r/two"], media_types=MediaType.ALL)
    assert [r.key for r in results] == ["one", "two"]


async def test_batch_gives_each_job_its_own_page():
    rex, _, browser = build([[harvested("a")]])
    await rex.batch(["r/one", "r/two"], media_types=MediaType.ALL)
    assert len(browser.pages) == 2


async def test_a_failing_source_does_not_abort_the_batch():
    browser = FakeBrowser([[harvested("a")]], fail_urls=("badsub",))
    rex, _, _ = build(browser)
    results = await rex.batch(["r/badsub", "r/goodsub"], media_types=MediaType.ALL)
    assert results[0].error == "RuntimeError: navigation failed"
    assert results[0].ok is False
    assert results[1].ok is True  # its sibling still ran


async def test_a_failed_job_is_stamped_and_announced():
    browser = FakeBrowser([[harvested("a")]], fail_urls=("badsub",))
    rex, _, _ = build(browser)
    ended: List[Any] = []
    results = await rex.batch(
        ["r/badsub"],
        media_types=MediaType.ALL,
        events=Events(on_job_end=lambda result: ended.append(result)),
    )
    assert results[0].finished_at is not None
    assert ended == results  # on_job_end fires for failures too


async def test_raise_on_error_surfaces_the_first_failure():
    browser = FakeBrowser([[harvested("a")]], fail_urls=("badsub",))
    rex, _, _ = build(browser)
    with pytest.raises(RuntimeError, match="navigation failed"):
        await rex.batch(
            ["r/badsub", "r/goodsub"], media_types=MediaType.ALL, raise_on_error=True
        )


async def test_a_raising_batch_leaves_no_job_running_detached():
    # A sibling still driving a page after the error would write files nobody is waiting for.
    browser = FakeBrowser([[harvested("a")]], fail_urls=("badsub",))
    rex, _, _ = build(browser)
    with pytest.raises(RuntimeError):
        await rex.batch(
            ["r/badsub"] + ["r/other{}".format(n) for n in range(5)],
            media_types=MediaType.ALL,
            raise_on_error=True,
        )
    assert all(page.closed for page in browser.pages)


async def test_batch_bounds_how_many_jobs_run_at_once():
    rex, _, browser = build([[harvested("a")]])

    live, peak = 0, 0
    original = browser.new_page

    async def counting_new_page():
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)  # let a sibling run if the semaphore allows it
        live -= 1
        return await original()

    browser.new_page = counting_new_page  # type: ignore[method-assign]
    await rex.batch(
        ["r/a", "r/b", "r/c", "r/d"], concurrency=2, media_types=MediaType.ALL
    )
    assert peak <= 2


async def test_concurrency_below_one_still_runs_the_jobs():
    rex, _, _ = build([[harvested("a")]])
    results = await rex.batch(
        ["r/one", "r/two"], concurrency=0, media_types=MediaType.ALL
    )
    assert [r.key for r in results] == ["one", "two"]


async def test_an_empty_batch_returns_nothing():
    rex, _, _ = build([[harvested("a")]])
    assert await rex.batch([], media_types=MediaType.ALL) == []


async def test_cancelling_a_batch_in_flight_is_not_recorded_as_a_failure():
    # Cancellation is the caller shutting the run down, not the source going wrong, so it
    # must propagate rather than land in result.error as if r/pics had misbehaved.
    resolving = asyncio.Event()

    class HangingHandler(MediaHandler):
        media_type = MediaType.IMAGE

        def can_handle(self, post: Post) -> bool:
            return True

        async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
            resolving.set()
            await asyncio.sleep(3600)
            return []

    rex, _, browser = build([[harvested("a")]], handlers=[HangingHandler()])
    task = asyncio.ensure_future(rex.batch(["r/pics"], media_types=MediaType.ALL))
    await resolving.wait()  # the job is now inside extract(), not merely queued
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert browser.pages[0].closed is True


# -- iter_batch -------------------------------------------------------------


async def test_iter_batch_yields_every_result():
    rex, _, _ = build([[harvested("a")]])
    results = [
        r async for r in rex.iter_batch(["r/one", "r/two"], media_types=MediaType.ALL)
    ]
    assert sorted(r.key for r in results) == ["one", "two"]


async def test_iter_batch_captures_a_failure_like_batch_does():
    browser = FakeBrowser([[harvested("a")]], fail_urls=("badsub",))
    rex, _, _ = build(browser)
    results = [r async for r in rex.iter_batch(["r/badsub"], media_types=MediaType.ALL)]
    assert results[0].error == "RuntimeError: navigation failed"


async def test_abandoning_iter_batch_early_stops_the_rest():
    # The consumer walking away must not leave pages open behind it.
    rex, _, browser = build([[harvested("a")]])
    sources = ["r/s{}".format(n) for n in range(6)]
    agen = rex.iter_batch(sources, concurrency=1, media_types=MediaType.ALL)
    async for _ in agen:
        break
    await agen.aclose()
    assert all(page.closed for page in browser.pages)
    assert len(browser.pages) < len(sources)  # the rest never started


async def test_iter_batch_with_raise_on_error_propagates():
    browser = FakeBrowser([[harvested("a")]], fail_urls=("badsub",))
    rex, _, _ = build(browser)
    with pytest.raises(RuntimeError, match="navigation failed"):
        async for _ in rex.iter_batch(
            ["r/badsub"], media_types=MediaType.ALL, raise_on_error=True
        ):
            pass


# -- media_types defaults ---------------------------------------------------


async def test_a_call_without_media_types_uses_the_configured_default():
    rounds = [[harvested("a"), harvested("t", type="text")]]
    rex, _, _ = build(rounds, default_media_types=MediaType.IMAGE)
    result = await rex.extract(Subreddit("pics", limit=2))
    assert [p.id for p in result.posts] == ["a"]


async def test_media_types_accept_a_comma_string():
    rounds = [[harvested("a"), harvested("t", type="text")]]
    rex, _, _ = build(rounds)
    result = await rex.extract(Subreddit("pics", limit=2), media_types="image,text")
    assert sorted(p.id for p in result.posts) == ["a", "t"]


async def test_a_string_source_is_parsed():
    rex, _, browser = build([[harvested("a")]])
    result = await rex.extract("r/pics", media_types=MediaType.ALL)
    assert result.key == "pics"
    assert browser.pages[0].gotos[0] == "https://www.reddit.com/r/pics/new/"


# -- the job's own events ---------------------------------------------------


async def test_a_job_announces_its_start_and_end():
    started: List[Any] = []
    ended: List[Any] = []
    rex, _, _ = build([[harvested("a")]])
    result = await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        events=Events(
            on_job_start=lambda src: started.append(src),
            on_job_end=lambda res: ended.append(res),
        ),
    )
    assert [s.key for s in started] == ["pics"]
    assert ended == [result]


async def test_a_handler_match_is_announced():
    matched: List[tuple[str, str]] = []
    rex, _, _ = build([[harvested("a")]])
    await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        events=Events(
            on_post=lambda src, post, handler: matched.append((post.id, handler.name))
        ),
    )
    assert matched == [("a", "ImageHandler")]


async def test_per_call_events_layer_over_the_extractors_own():
    saved: List[str] = []
    ended: List[Any] = []
    browser = FakeBrowser([[harvested("a")]])
    rex = AsyncRedditExtractor(
        ExtractorConfig(delay=0.0, scroll_pause=0.0),
        storage=MemoryStorage(),
        browser=browser,  # type: ignore[arg-type]
        events=Events(on_media_saved=lambda src, item: saved.append(item.filename)),
    )
    await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        events=Events(on_job_end=lambda res: ended.append(res)),
    )
    assert saved == ["0001_photo_a.jpg"]  # the constructor's callback survived
    assert len(ended) == 1


# -- handler selection ------------------------------------------------------


def test_the_first_handler_that_wants_the_post_wins():
    rex = AsyncRedditExtractor()
    post = Post.from_harvest(harvested("a"))
    assert isinstance(rex._select_handler(post, MediaType.ALL), ImageHandler)


def test_a_handler_whose_type_was_not_asked_for_is_passed_over():
    rex = AsyncRedditExtractor()
    post = Post.from_harvest(harvested("a"))
    assert rex._select_handler(post, MediaType.VIDEO) is None


def test_no_handler_means_none():
    rex = AsyncRedditExtractor()
    assert (
        rex._select_handler(
            Post.from_harvest(harvested("p", type="unsupported")), MediaType.ALL
        )
        is None
    )


# -- handler selection: fallbacks come last ---------------------------------


class ClaimsEverything(MediaHandler):
    """A specific handler that claims any post and resolves one file."""

    media_type = MediaType.IMAGE

    def can_handle(self, post: Post) -> bool:
        return True

    async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
        return [MediaCandidate("https://i.redd.it/specific.jpg", MediaType.IMAGE)]


class CatchAll(ClaimsEverything):
    """The same, but marked as the chain's catch-all."""

    fallback = True

    async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
        return [MediaCandidate("https://i.redd.it/catchall.jpg", MediaType.IMAGE)]


async def test_a_specific_handler_beats_a_catch_all_registered_before_it():
    result, _, _ = await run(
        [[harvested("a")]], handlers=[CatchAll(), ClaimsEverything()]
    )
    assert [item.url for item in result.items] == ["https://i.redd.it/specific.jpg"]


async def test_the_catch_all_still_takes_what_nothing_else_claims():
    class Fussy(ClaimsEverything):
        def can_handle(self, post: Post) -> bool:
            return False

    result, _, _ = await run([[harvested("a")]], handlers=[CatchAll(), Fussy()])
    assert [item.url for item in result.items] == ["https://i.redd.it/catchall.jpg"]


def test_the_built_in_catch_all_is_reached_for_a_plain_image_link():
    # A direct image on an unknown host has no specific handler, so LinkImageHandler must still see it.
    rex, _, _ = build([[]])
    post = Post.from_harvest(
        {"id": "p1", "type": "link", "content_href": "https://unknown.test/a.jpg"}
    )
    assert rex._select_handler(post, MediaType.ALL).name == "LinkImageHandler"


class RefererHandler(MediaHandler):
    """A host whose CDN answers 403 without a Referer."""

    media_type = MediaType.IMAGE

    def can_handle(self, post: Post) -> bool:
        return True

    async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
        return [
            MediaCandidate(
                "https://cdn.example/a.jpg",
                MediaType.IMAGE,
                headers={"Referer": "https://example.com/"},
            )
        ]


async def test_a_candidates_headers_reach_the_download():
    _, _, browser = await run([[harvested("a")]], handlers=[RefererHandler()])
    assert browser.download_headers == [{"Referer": "https://example.com/"}]


async def test_an_ordinary_candidate_asks_for_no_extra_headers():
    _, _, browser = await run([[harvested("a")]])
    assert browser.download_headers == [None]


class TypelessHandler(MediaHandler):
    """A host whose URL names no format, so only the response can settle one."""

    media_type = MediaType.IMAGE

    def __init__(self, prefixes: tuple[str, ...] = ("image/",)) -> None:
        self._prefixes = prefixes

    def can_handle(self, post: Post) -> bool:
        return True

    async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
        return [
            MediaCandidate(
                "https://cdn.example/download?id=7",
                MediaType.IMAGE,
                content_prefixes=self._prefixes,
            )
        ]


def served(content_type: str) -> FakeBrowser:
    return FakeBrowser(
        [[harvested("a")]],
        results={
            "https://cdn.example/download?id=7": FetchResult(
                ok=True, status=200, content_type=content_type, body=b"bytes"
            )
        },
    )


async def test_a_guessed_extension_is_corrected_by_the_response():
    result, storage, _ = await run(
        served("image/png"), limit=1, handlers=[TypelessHandler()]
    )
    assert list(storage.files["pics"]) == ["0001_photo_a.png"]
    assert result.items[0].filename == "0001_photo_a.png"


async def test_a_response_naming_no_format_leaves_the_guess_alone():
    result, storage, _ = await run(
        served("application/octet-stream"),
        limit=1,
        handlers=[TypelessHandler(("image/", "application/octet-stream"))],
    )
    assert list(storage.files["pics"]) == ["0001_photo_a.jpg"]


async def test_an_extension_the_handler_asked_for_is_not_second_guessed():
    class NamedHandler(TypelessHandler):
        async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
            return [
                MediaCandidate(
                    "https://cdn.example/download?id=7", MediaType.IMAGE, ext="jpg"
                )
            ]

    _, storage, _ = await run(served("image/png"), limit=1, handlers=[NamedHandler()])
    assert list(storage.files["pics"]) == ["0001_photo_a.jpg"]


async def test_an_extension_in_the_url_is_not_second_guessed():
    _, storage, _ = await run([[harvested("a")]], limit=1)
    assert list(storage.files["pics"]) == ["0001_photo_a.jpg"]


# -- remembering a file behind an unstable URL ------------------------------


class SignedUrlHandler(MediaHandler):
    """A host whose URLs are signed, so the same file arrives under a new URL every resolve."""

    media_type = MediaType.IMAGE

    def __init__(self, *, keyed: bool) -> None:
        self.keyed = keyed
        self.resolves = 0

    def can_handle(self, post: Post) -> bool:
        return True

    async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
        self.resolves += 1
        return [
            MediaCandidate(
                "https://cdn.example/f.jpg?Expires=1&Signature=s{}".format(
                    self.resolves
                ),
                MediaType.IMAGE,
                key="cdn:file-1" if self.keyed else None,
            )
        ]


async def rerun_signed(*, keyed: bool):
    """Extract the same post twice through one handler, so its URL is freshly signed the second time."""
    storage = MemoryStorage()
    handler = SignedUrlHandler(keyed=keyed)
    for _ in range(2):
        result, _, _ = await run(
            [[harvested("a")]], limit=1, storage=storage, handlers=[handler]
        )
    return result, storage


async def test_a_keyed_candidate_is_recognized_under_a_fresh_signed_url():
    result, storage = await rerun_signed(keyed=True)
    assert result.media_saved == 0 and result.skipped_known == 1
    assert len(storage.files["pics"]) == 1


async def test_without_a_key_a_signed_url_is_downloaded_again():
    result, storage = await rerun_signed(keyed=False)
    assert result.media_saved == 1
    assert len(storage.files["pics"]) == 2


async def test_a_keyed_files_manifest_records_both_its_key_and_its_url():
    storage = MemoryStorage()
    await run(
        [[harvested("a")]],
        limit=1,
        storage=storage,
        handlers=[SignedUrlHandler(keyed=True)],
    )
    entry = next(iter(storage.manifests["pics"]["files"].values()))
    assert entry["media_key"] == "cdn:file-1"
    assert entry["media_url"].startswith("https://cdn.example/f.jpg?Expires=")
