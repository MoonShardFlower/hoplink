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
    AsyncRedditExtractor,
)
from reddit_extract.events import Events
from reddit_extract.exceptions import NoPostsFoundError
from reddit_extract.handlers import ImageHandler, TextHandler
from reddit_extract.handlers.base import MediaHandler
from reddit_extract.models.config import ExtractorConfig
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

    async def dismiss_gates(self, page: Any) -> None:
        pass

    async def wait_for_posts(self, page: Any, timeout_ms: int) -> bool:
        return self.posts_render

    async def fetch(self, url: str, timeout_ms: int | None = None) -> FetchResult:
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
    config = ExtractorConfig(img_delay=0.0, scroll_pause=0.0, **cfg_kwargs)
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
    rex = AsyncRedditExtractor(headless=False, img_delay=0.0)
    assert rex.config.headless is False
    assert rex.config.img_delay == 0.0


def test_keyword_overrides_layer_onto_a_given_config():
    rex = AsyncRedditExtractor(ExtractorConfig(output_dir="/out"), headless=False)
    assert rex.config.output_dir == "/out"
    assert rex.config.headless is False


def test_an_invalid_override_is_rejected_at_construction():
    with pytest.raises(ValueError, match="img_delay must be >= 0"):
        AsyncRedditExtractor(img_delay=-1.0)


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


# -- _filename --------------------------------------------------------------


def candidate(url="https://i.redd.it/a.jpg", media_type=MediaType.IMAGE, ext=None):
    return MediaCandidate(url=url, media_type=media_type, ext=ext)


def test_a_single_file_post_is_named_by_its_index():
    assert AsyncRedditExtractor._filename(1, None, candidate(ext="jpg")) == "0001.jpg"
    assert AsyncRedditExtractor._filename(42, None, candidate(ext="jpg")) == "0042.jpg"


def test_a_multi_file_post_numbers_its_parts():
    assert AsyncRedditExtractor._filename(1, 2, candidate(ext="jpg")) == "0001_02.jpg"


def test_an_index_beyond_four_digits_still_works():
    assert (
        AsyncRedditExtractor._filename(12345, None, candidate(ext="jpg")) == "12345.jpg"
    )


def test_a_candidate_without_an_extension_takes_it_from_the_url():
    assert (
        AsyncRedditExtractor._filename(1, None, candidate("https://x/a.PNG"))
        == "0001.png"
    )


def test_a_url_query_string_is_not_mistaken_for_an_extension():
    cand = candidate("https://i.redd.it/a.jpg?width=640&s=abc")
    assert AsyncRedditExtractor._filename(1, None, cand) == "0001.jpg"


def test_an_extension_less_video_falls_back_to_mp4():
    cand = candidate("https://v.redd.it/abc123", media_type=MediaType.VIDEO)
    assert AsyncRedditExtractor._filename(1, None, cand) == "0001.mp4"


def test_an_extension_less_image_falls_back_to_jpg():
    assert (
        AsyncRedditExtractor._filename(1, None, candidate("https://x/a")) == "0001.jpg"
    )


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
    assert browser.pages[0].mouse.wheels == []  # the first round already sufficed


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
    assert len(browser.pages[0].mouse.wheels) == 3


async def test_a_fresh_round_resets_the_stale_counter():
    rounds = [[harvested("a")], [harvested("a")], [harvested("a"), harvested("b")]]
    rex, _, _ = build(rounds, max_stale_scrolls=2)
    result = await rex.extract(Subreddit("pics", limit=100), media_types=MediaType.ALL)
    assert result.posts_scanned == 2  # the stale round did not end the harvest


async def test_scrolling_uses_the_configured_distance():
    rex, _, browser = build([[harvested("a")]], max_stale_scrolls=1, scroll_px=999)
    await rex.extract(Subreddit("pics", limit=100), media_types=MediaType.ALL)
    wheels = browser.pages[0].mouse.wheels
    assert wheels and set(wheels) == {
        (0, 999)
    }  # straight down, by the configured distance


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
    assert browser.pages[0].mouse.wheels == []


async def test_a_legacy_listing_stops_at_its_last_page():
    browser = FakeBrowser([[harvested("a")]], modern=False, next_urls=())
    result, _, _ = await run(browser, limit=100)
    assert result.posts_scanned == 1  # no next link, so the harvest ends


# -- posts nobody wants -----------------------------------------------------


async def test_a_post_no_handler_claims_is_scanned_but_not_matched():
    result, _, browser = await run([[harvested("p", type="poll")]], limit=1)
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


async def test_a_metadata_only_post_is_matched_without_any_media():
    # Text posts exist to be recorded, not downloaded.
    result, storage, browser = await run(
        [[harvested("t", type="text")]], media_types=MediaType.TEXT
    )
    assert result.posts_matched == 1
    assert [p.id for p in result.posts] == ["t"]
    assert result.media_found == 0
    assert browser.fetched == []
    assert storage.files["pics"] == {}


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
    assert list(storage.files["pics"]) == ["0002.jpg"]


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
    assert sorted(storage.files["pics"]) == ["0001.jpg", "0002.jpg"]


async def test_identical_bytes_at_a_new_url_are_skipped_when_dedupe_is_on():
    # A crosspost or repost is the same upload wearing a new URL.
    result, storage, _ = await run(
        [[harvested("a"), harvested("b")]], dedupe_by_hash=True
    )
    assert result.media_saved == 1
    assert result.skipped_duplicate == 1
    assert list(storage.files["pics"]) == ["0001.jpg"]


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
    assert storage.manifests["pics"]["files"]["0001.jpg"]["sha256"] == digest


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
    assert item.filename == "0001.jpg"
    assert item.path == "memory://pics/0001.jpg"
    assert item.downloaded is True
    assert item.size == len(b"imagebytes")
    assert item.post_id == "a"
    assert item.author == "alice"
    assert item.title == "photo a"
    assert item.source_key == "pics"


async def test_saving_is_announced():
    saved: List[str] = []
    rex, _, _ = build([[harvested("a")]])
    await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        events=Events(on_media_saved=lambda src, item: saved.append(item.filename)),
    )
    assert saved == ["0001.jpg"]


async def test_the_result_reports_where_everything_went():
    result, _, _ = await run([[harvested("a")]])
    assert result.output_dir == "memory://pics"
    assert result.manifest_path == "memory://pics/manifest.json"
    assert result.finished_at is not None
    assert result.duration is not None


# -- the manifest -----------------------------------------------------------


async def test_the_manifest_is_written_at_the_end_of_a_job():
    _, storage, _ = await run([[harvested("a")]])
    assert list(storage.manifests["pics"]["files"]) == ["0001.jpg"]


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


# -- dry runs ---------------------------------------------------------------


async def test_a_dry_run_plans_files_without_writing_them():
    rex, storage, browser = build([[harvested("a"), harvested("b")]])
    result = await rex.extract(
        Subreddit("pics", limit=2), media_types=MediaType.ALL, dry_run=True
    )
    assert result.media_found == 2
    assert result.media_saved == 0
    assert [i.filename for i in result.items] == ["0001.jpg", "0002.jpg"]
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
        ExtractorConfig(img_delay=0.0, scroll_pause=0.0),
        storage=MemoryStorage(),
        browser=browser,  # type: ignore[arg-type]
        events=Events(on_media_saved=lambda src, item: saved.append(item.filename)),
    )
    await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        events=Events(on_job_end=lambda res: ended.append(res)),
    )
    assert saved == ["0001.jpg"]  # the constructor's callback survived
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
            Post.from_harvest(harvested("p", type="poll")), MediaType.ALL
        )
        is None
    )
