"""
Tests that a PostFilter really narrows a job, driven through the engine itself.

`AsyncRedditExtractor` takes a custom browser and storage backend, so the whole pipeline runs here against canned
 harvest data with no Playwright and no network.
"""

from __future__ import annotations

from typing import Any, List

from reddit_extract.core.browser import FetchResult
from reddit_extract.core.extractor import JS_HARVEST, JS_IS_MODERN, AsyncRedditExtractor
from reddit_extract.events import Events
from reddit_extract.handlers.base import MediaHandler
from reddit_extract.models.config import ExtractorConfig
from reddit_extract.models.filters import PostFilter
from reddit_extract.models.media import MediaCandidate, MediaType
from reddit_extract.models.post import Post
from reddit_extract.models.source import Subreddit
from reddit_extract.storage import MemoryStorage

JPEG = FetchResult(ok=True, status=200, content_type="image/jpeg", body=b"imagebytes")


def harvested(pid: str, **overrides) -> dict[str, Any]:
    """One raw harvest record, shaped like the modern UI's JS_HARVEST output."""
    data = {
        "id": pid,
        "type": "image",
        "permalink": "/r/pics/comments/{}/t/".format(pid),
        "content_href": "https://i.redd.it/{}.jpg".format(pid),
        "author": "alice",
        "title": "photo {}".format(pid),
        "subreddit": "r/pics",
        "domain": "i.redd.it",
        "created": "2026-07-16T11:45:34.060000+0000",
        "score": "1000",
        "comment_count": "50",
        "flair": None,
        "stickied": False,
        "packaged_media": None,
        "player_src": None,
    }
    data.update(overrides)
    return data


class FakeMouse:
    async def wheel(self, dx: int, dy: int) -> None:
        pass


class FakePage:
    """Serves canned harvest data in place of a real listing page."""

    def __init__(self, records: List[dict[str, Any]]) -> None:
        self.records = records
        self.mouse = FakeMouse()
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> None:
        pass

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == JS_IS_MODERN:
            return True
        if js == JS_HARVEST:
            return self.records
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    """Stands in for BrowserManager: canned pages, canned fetches, no Chromium."""

    def __init__(self, records: List[dict[str, Any]]) -> None:
        self.records = records
        self.fetched: List[str] = []

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def new_page(self) -> FakePage:
        return FakePage(self.records)

    async def download(self, url: str, timeout_ms: int | None = None) -> FetchResult:
        # The real manager may route this straight out to the network; a fake has only the one route.
        return await self.fetch(url, timeout_ms)

    async def dismiss_gates(self, page: Any) -> None:
        pass

    async def wait_for_posts(self, page: Any, timeout_ms: int) -> bool:
        return True

    async def fetch(
        self,
        url: str,
        timeout_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        self.fetched.append(url)
        return JPEG


class CountingGalleryHandler(MediaHandler):
    """A gallery handler that yields a fixed number of slides without visiting any page."""

    media_type = MediaType.GALLERY

    def __init__(self, slides: int) -> None:
        self.slides = slides

    def can_handle(self, post: Post) -> bool:
        return post.type == "gallery"

    async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
        return [
            MediaCandidate(
                url="https://i.redd.it/{}_{}.jpg".format(post.id, n),
                media_type=MediaType.GALLERY,
                ext="jpg",
            )
            for n in range(1, self.slides + 1)
        ]


def build(records, post_filter=None, handlers=None, **cfg_kwargs):
    """An extractor wired to fake infrastructure, plus its storage and browser."""
    storage = MemoryStorage()
    browser = FakeBrowser(records)
    config = ExtractorConfig(delay=0.0, scroll_pause=0.0, **cfg_kwargs)
    rex = AsyncRedditExtractor(
        config,
        storage=storage,
        browser=browser,  # type: ignore[arg-type]
        handlers=handlers,
        post_filter=post_filter,
    )
    return rex, storage, browser


async def run(records, post_filter=None, handlers=None, **kwargs):
    """Extract a source whose limit matches the canned data, and return its result."""
    rex, storage, browser = build(records, post_filter, handlers, **kwargs)
    result = await rex.extract(
        Subreddit("pics", limit=len(records)), media_types=MediaType.ALL
    )
    return result, storage, browser


# -- no filter: the baseline ----------------------------------------------


async def test_without_a_filter_every_post_is_kept():
    result, storage, browser = await run([harvested("a"), harvested("b")])
    assert result.posts_scanned == 2
    assert result.posts_matched == 2
    assert result.posts_filtered == 0
    assert result.media_saved == 2
    assert len(browser.fetched) == 2


# -- filtering actually prevents work -------------------------------------


async def test_filtered_posts_are_never_downloaded():
    records = [harvested("a", score="1000"), harvested("b", score="10")]
    result, storage, browser = await run(records, PostFilter(min_score=500))
    assert result.posts_scanned == 2  # both were harvested
    assert result.posts_matched == 1  # only one survived the filter
    assert result.posts_filtered == 1
    assert result.media_saved == 1
    # the rejected post cost no fetch at all
    assert browser.fetched == ["https://i.redd.it/a.jpg"]
    assert list(storage.files["pics"]) == ["0001.jpg"]


async def test_filtered_post_is_absent_from_the_result():
    records = [harvested("a", author="alice"), harvested("b", author="spammer")]
    result, _, _ = await run(records, PostFilter(block_authors="spammer"))
    assert [p.id for p in result.posts] == ["a"]


async def test_filter_reports_each_rejection_through_on_skip():
    skips: List[tuple[str, str]] = []

    rex, _, _ = build([harvested("a", score="10")], PostFilter(min_score=500))
    await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        events=Events(on_skip=lambda src, url, reason: skips.append((url, reason))),
    )
    assert len(skips) == 1
    assert "score 10 below min 500" in skips[0][1]


async def test_filtering_everything_saves_nothing():
    records = [harvested("a", score="1"), harvested("b", score="2")]
    result, storage, browser = await run(records, PostFilter(min_score=500))
    assert result.posts_filtered == 2
    assert result.media_saved == 0
    assert browser.fetched == []


# -- the filter is applied after handler selection -------------------------


async def test_posts_no_handler_wants_are_not_counted_as_filtered():
    # A text post with media_types=IMAGE has no handler, so it is out of the filter's scope:
    # counting it as "filtered" would overstate what the filter did.
    records = [harvested("a", type="text", score="1"), harvested("b", score="1")]
    rex, _, _ = build(records, PostFilter(min_score=500))
    result = await rex.extract(Subreddit("pics", limit=2), media_types=MediaType.IMAGE)
    assert result.posts_scanned == 2
    assert result.posts_filtered == 1  # only the image post reached the filter


# -- min_gallery, decided after resolve ------------------------------------


async def test_min_gallery_drops_a_small_gallery_after_resolve():
    records = [harvested("g", type="gallery")]
    result, _, browser = await run(
        records,
        PostFilter(min_gallery=3),
        handlers=[CountingGalleryHandler(slides=2)],
    )
    assert result.posts_filtered == 1
    assert result.posts_matched == 0
    assert result.media_saved == 0
    assert browser.fetched == []  # rejected before any slide was downloaded


async def test_min_gallery_keeps_a_big_enough_gallery():
    records = [harvested("g", type="gallery")]
    result, _, browser = await run(
        records,
        PostFilter(min_gallery=3),
        handlers=[CountingGalleryHandler(slides=4)],
    )
    assert result.posts_filtered == 0
    assert result.posts_matched == 1
    assert result.media_saved == 4
    assert len(browser.fetched) == 4


# -- per-call override -----------------------------------------------------


async def test_per_call_filter_overrides_the_extractor_default():
    records = [harvested("a", score="10")]
    rex, _, _ = build(records, PostFilter(min_score=500))
    # the constructor's filter would reject this post; the per-call one accepts it
    result = await rex.extract(
        Subreddit("pics", limit=1),
        media_types=MediaType.ALL,
        post_filter=PostFilter(min_score=1),
    )
    assert result.posts_matched == 1
    assert result.posts_filtered == 0


# -- filtering is orthogonal to dry runs -----------------------------------


async def test_filter_applies_during_a_dry_run():
    records = [harvested("a", score="1000"), harvested("b", score="1")]
    rex, storage, browser = build(records, PostFilter(min_score=500))
    result = await rex.extract(
        Subreddit("pics", limit=2), media_types=MediaType.ALL, dry_run=True
    )
    assert result.posts_filtered == 1
    assert len(result.items) == 1
    assert browser.fetched == []  # a dry run downloads nothing either way


# -- the counter reaches the report ----------------------------------------


async def test_posts_filtered_reaches_the_report_and_summary():
    records = [harvested("a", score="1"), harvested("b", score="1000")]
    result, _, _ = await run(records, PostFilter(min_score=500))
    assert result.to_report_dict()["posts_filtered"] == 1
    assert "1 filtered out" in result.summary()


async def test_summary_omits_the_counter_when_nothing_was_filtered():
    result, _, _ = await run([harvested("a")])
    assert "filtered out" not in result.summary()
