"""
Tests for downloading one post's media in parallel (``config.download_concurrency``).

The engine plans a post's files sequentially, fetches them with a worker pool, and commits the outcomes in candidate
order. Test that nothing observable (filenames, hash de-duplication, the manifest, the event stream) shifts.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from reddit_extract.core.browser import FetchResult
from reddit_extract.core.extractor import AsyncRedditExtractor
from reddit_extract.events import Events
from reddit_extract.handlers.base import MediaHandler
from reddit_extract.models.config import ExtractorConfig
from reddit_extract.models.media import MediaCandidate, MediaType
from reddit_extract.models.post import Post
from reddit_extract.models.source import Subreddit
from reddit_extract.storage import MemoryStorage
from tests.test_extractor import FakeBrowser, run
from tests.test_extractor_filtering import harvested

URLS = ["https://i.redd.it/c{}.jpg".format(i) for i in range(8)]


class MultiHandler(MediaHandler):
    """Resolves every post to a fixed list of files, like a gallery or a scraped RedGIFs profile."""

    media_type = MediaType.IMAGE

    def __init__(self, urls: List[str]) -> None:
        self.urls = urls

    def can_handle(self, post: Post) -> bool:
        return True

    async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
        return [
            MediaCandidate(
                url=url,
                media_type=MediaType.IMAGE,
                ext="jpg",
                content_prefixes=("image/",),
            )
            for url in self.urls
        ]


class TrackingBrowser(FakeBrowser):
    """A browser that records how many fetches overlap, and can finish them out of order."""

    def __init__(
        self,
        rounds: List[List[dict[str, Any]]],
        *,
        holds: dict[str, int] | None = None,
        fail: tuple[str, ...] = (),
        body: bytes | None = None,
    ) -> None:
        super().__init__(rounds)
        self.holds = holds or {}
        self.fail = fail
        self.body = body
        self.in_flight = 0
        self.peak = 0
        self.completed: List[str] = []

    async def fetch(
        self,
        url: str,
        timeout_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        self.fetched.append(url)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        for _ in range(self.holds.get(url, 1)):
            await asyncio.sleep(0)
        self.in_flight -= 1
        self.completed.append(url)
        if url in self.fail:
            return FetchResult(ok=False, status=404, error="HTTP 404")
        return FetchResult(
            ok=True,
            status=200,
            content_type="image/jpeg",
            body=self.body if self.body is not None else url.encode(),
        )


def browser(**kwargs: Any) -> TrackingBrowser:
    """A one-post listing whose single post resolves to every URL in `URLS`."""
    return TrackingBrowser([[harvested("a")]], **kwargs)


async def extract(bro: TrackingBrowser, urls: List[str] = URLS, **cfg: Any):
    """Run the one post through a MultiHandler with the given config overrides."""
    return await run(bro, limit=1, handlers=[MultiHandler(urls)], **cfg)


@pytest.mark.asyncio
async def test_downloads_are_sequential_by_default():
    bro = browser()
    result, _, _ = await extract(bro)
    assert bro.peak == 1
    assert result.media_saved == len(URLS)


@pytest.mark.asyncio
async def test_download_concurrency_overlaps_fetches():
    bro = browser()
    result, _, _ = await extract(bro, download_concurrency=4)
    assert bro.peak == 4
    assert result.media_saved == len(URLS)


@pytest.mark.asyncio
async def test_the_pool_keeps_working_while_a_slow_file_finishes():
    # The first candidate parks far longer than the rest of its window, which must not wait for it.
    bro = browser(holds={URLS[0]: 50})
    await extract(bro, download_concurrency=4)
    for url in URLS[1:4]:
        assert bro.completed.index(url) < bro.completed.index(URLS[0])


@pytest.mark.asyncio
async def test_a_slow_file_does_not_hold_back_the_slots_behind_it():
    window = 3
    bro = browser(holds={URLS[0]: 500})
    await extract(bro, download_concurrency=window)

    finished_first = bro.completed.index(URLS[0])
    assert finished_first >= window  # more than the old window-1 got through
    assert finished_first <= 2 * window - 1  # but the lookahead still caps it


@pytest.mark.asyncio
async def test_the_pool_runs_only_a_bounded_lookahead_ahead_of_the_writes():
    # A scraped RedGIFs profile arrives as one post's worth of candidates, and fetching them all before writing any
    # would hold a whole profile of MP4s in memory.
    window = 3
    bro = browser(holds={URLS[0]: 500})
    started_when_saved: List[int] = []

    events = Events(
        on_media_saved=lambda source, item: started_when_saved.append(len(bro.fetched))
    )
    rex = AsyncRedditExtractor(
        ExtractorConfig(delay=0.0, scroll_pause=0.0, download_concurrency=window),
        storage=MemoryStorage(),
        browser=bro,  # type: ignore[arg-type]
        handlers=[MultiHandler(URLS)],
        events=events,
    )
    await rex.extract(Subreddit("pics", limit=1), media_types=MediaType.ALL)

    assert len(started_when_saved) == len(URLS)
    for saved, started in enumerate(started_when_saved, 1):
        assert started <= saved + 2 * window
    # And the lookahead is genuinely used, so the bound above is not vacuous.
    assert max(started_when_saved) > window


@pytest.mark.asyncio
@pytest.mark.parametrize("workers", [1, 3, 8])
async def test_filenames_follow_candidate_order_whatever_the_completion_order(workers):
    holds = {url: (len(URLS) - i) * 4 for i, url in enumerate(URLS)}
    bro = browser(holds=holds)
    result, _, _ = await extract(bro, download_concurrency=workers)

    assert [item.url for item in result.items] == URLS
    assert [item.filename for item in result.items] == sorted(
        item.filename for item in result.items
    )


@pytest.mark.asyncio
async def test_hash_dedupe_keeps_the_first_candidate_not_the_first_to_finish():
    holds = {url: (len(URLS) - i) * 4 for i, url in enumerate(URLS)}
    bro = browser(holds=holds, body=b"same bytes")
    result, _, _ = await extract(bro, download_concurrency=4, dedupe_by_hash=True)

    assert result.media_saved == 1
    assert result.skipped_duplicate == len(URLS) - 1
    assert [item.url for item in result.items] == URLS[:1]


@pytest.mark.asyncio
async def test_a_repeated_url_within_one_post_is_fetched_once():
    bro = browser()
    result, _, _ = await extract(
        bro, [URLS[0], URLS[1], URLS[0]], download_concurrency=3
    )

    assert bro.fetched.count(URLS[0]) == 1
    assert result.media_saved == 2
    assert result.skipped_known == 1


@pytest.mark.asyncio
async def test_one_failure_is_recorded_without_losing_the_rest_of_the_batch():
    bro = browser(fail=(URLS[2],))
    result, _, _ = await extract(bro, download_concurrency=4)

    assert result.media_saved == len(URLS) - 1
    assert [url for url, _ in result.failures] == [URLS[2]]
    assert URLS[2] not in [item.url for item in result.items]
