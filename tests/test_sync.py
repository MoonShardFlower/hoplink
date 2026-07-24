"""
Tests for the synchronous facade over the async engine.

Browser-free, but the loop thread is real: `RedditExtractor` runs a private event loop on a daemon thread and proxies
every call to it, these tests exercise that machinery against a FakeBrowser. We test the facade's behavior:
delegation, the loop's lifecycle, and cleanup (the extraction semantics are already covered against the async engine).
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import List

import pytest

from reddit_extract import RedditExtractor
from reddit_extract.core import sync as sync_module
from reddit_extract.events import Events
from reddit_extract.exceptions import NoPostsFoundError
from reddit_extract.handlers import TextHandler
from reddit_extract.models.config import ExtractorConfig
from reddit_extract.models.filters import PostFilter
from reddit_extract.models.media import MediaType
from reddit_extract.models.source import Subreddit
from reddit_extract.storage import MemoryStorage
from tests.test_extractor import BoomHandler, FakeBrowser
from tests.test_extractor_filtering import harvested


@pytest.fixture
def rex():
    """A synchronous extractor over canned posts, closed when the test ends."""
    extractor = RedditExtractor(
        ExtractorConfig(img_delay=0.0, scroll_pause=0.0),
        storage=MemoryStorage(),
        browser=FakeBrowser([[harvested("a"), harvested("b")]]),  # type: ignore[arg-type]
    )
    try:
        yield extractor
    finally:
        extractor.close()


# -- delegation to the engine -----------------------------------------------


def test_extract_runs_a_job_and_returns_its_result(rex):
    result = rex.extract(Subreddit("pics", limit=2), media_types=MediaType.ALL)
    assert result.key == "pics"
    assert result.media_saved == 2


def test_extract_accepts_a_string_source(rex):
    assert rex.extract("r/pics", media_types=MediaType.ALL).key == "pics"


def test_extract_forwards_a_dry_run(rex):
    result = rex.extract("r/pics", media_types=MediaType.ALL, dry_run=True)
    assert result.dry_run is True
    assert result.media_saved == 0


def test_extract_forwards_a_post_filter(rex):
    result = rex.extract(
        Subreddit("pics", limit=2),
        media_types=MediaType.ALL,
        post_filter=PostFilter(block_authors="alice"),
    )
    assert result.posts_filtered == 2


def test_extract_forwards_events(rex):
    saved: List[str] = []
    rex.extract(
        Subreddit("pics", limit=2),
        media_types=MediaType.ALL,
        events=Events(on_media_saved=lambda src, item: saved.append(item.filename)),
    )
    assert saved == ["0001.jpg", "0002.jpg"]


def test_extract_forwards_an_output_dir(tmp_path):
    extractor = RedditExtractor(
        ExtractorConfig(img_delay=0.0, scroll_pause=0.0),
        browser=FakeBrowser([[harvested("a")]]),  # type: ignore[arg-type]
    )
    try:
        extractor.extract("r/pics", media_types=MediaType.ALL, output_dir=str(tmp_path))
    finally:
        extractor.close()
    assert (tmp_path / "pics" / "0001.jpg").read_bytes() == b"imagebytes"


def test_the_config_is_the_engines(rex):
    assert rex.config.img_delay == 0.0


def test_constructor_overrides_reach_the_engine():
    extractor = RedditExtractor(headless=False, browser=FakeBrowser([[]]))  # type: ignore[arg-type]
    assert extractor.config.headless is False


def test_handlers_are_the_engines():
    extractor = RedditExtractor(handlers=[TextHandler()], browser=FakeBrowser([[]]))  # type: ignore[arg-type]
    assert [h.name for h in extractor.handlers] == ["TextHandler"]


def test_a_registered_handler_reaches_the_engine(rex):
    custom = BoomHandler()
    rex.register_handler(custom)
    assert rex.handlers[0] is custom


def test_an_error_from_a_job_surfaces_to_the_caller():
    extractor = RedditExtractor(
        ExtractorConfig(img_delay=0.0, scroll_pause=0.0),
        browser=FakeBrowser([[]], posts_render=False),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(NoPostsFoundError):
            extractor.extract("r/pics", media_types=MediaType.ALL)
    finally:
        extractor.close()


# -- batch ------------------------------------------------------------------


def test_batch_returns_results_in_input_order(rex):
    results = rex.batch(["r/one", "r/two", "r/three"], media_types=MediaType.ALL)
    assert [r.key for r in results] == ["one", "two", "three"]


def test_batch_captures_a_failing_source(rex):
    rex._async._browser.fail_urls = ("badsub",)
    results = rex.batch(["r/badsub", "r/goodsub"], media_types=MediaType.ALL)
    assert results[0].error == "RuntimeError: navigation failed"
    assert results[1].ok is True


def test_batch_can_raise_instead(rex):
    rex._async._browser.fail_urls = ("badsub",)
    with pytest.raises(RuntimeError, match="navigation failed"):
        rex.batch(["r/badsub"], media_types=MediaType.ALL, raise_on_error=True)


def test_batch_forwards_concurrency(rex):
    results = rex.batch(["r/one", "r/two"], concurrency=2, media_types=MediaType.ALL)
    assert len(results) == 2


# -- iter_batch -------------------------------------------------------------


def test_iter_batch_yields_every_result(rex):
    results = list(rex.iter_batch(["r/one", "r/two"], media_types=MediaType.ALL))
    assert sorted(r.key for r in results) == ["one", "two"]


def test_iter_batch_is_lazy_until_iterated(rex):
    generator = rex.iter_batch(["r/one"], media_types=MediaType.ALL)
    assert rex._async._browser.pages == []  # nothing ran yet
    list(generator)
    assert rex._async._browser.pages != []


def test_abandoning_iter_batch_early_closes_the_rest(rex):
    sources = ["r/s{}".format(n) for n in range(6)]
    for _ in rex.iter_batch(sources, concurrency=1, media_types=MediaType.ALL):
        break  # the generator is closed by the for-loop's cleanup
    assert all(page.closed for page in rex._async._browser.pages)


def test_iter_batch_captures_a_failing_source(rex):
    rex._async._browser.fail_urls = ("badsub",)
    results = list(rex.iter_batch(["r/badsub"], media_types=MediaType.ALL))
    assert results[0].error == "RuntimeError: navigation failed"


# -- the loop's lifecycle ---------------------------------------------------


def test_the_context_manager_starts_and_closes_the_loop():
    with RedditExtractor(browser=FakeBrowser([[harvested("a")]])) as extractor:  # type: ignore[arg-type]
        assert extractor._loop is not None
        assert extractor._thread is not None and extractor._thread.is_alive()
        thread = extractor._thread
    assert extractor._loop is None
    thread.join(timeout=5)
    assert not thread.is_alive()  # the daemon thread really stopped


def test_a_call_without_a_with_block_starts_the_loop_itself(rex):
    # start() is optional; the first call brings the loop up.
    assert rex._loop is None
    rex.extract("r/pics", media_types=MediaType.ALL)
    assert rex._loop is not None


def test_start_is_idempotent(rex):
    rex.start()
    loop, thread = rex._loop, rex._thread
    rex.start()
    assert rex._loop is loop  # the second call reuses the loop rather than stranding it
    assert rex._thread is thread


def test_start_returns_the_extractor(rex):
    assert rex.start() is rex


def test_close_is_safe_when_never_started(rex):
    rex.close()  # must not raise


def test_close_is_idempotent(rex):
    rex.start()
    rex.close()
    rex.close()


def test_an_extractor_can_be_restarted_after_close(rex):
    # A fresh loop must not inherit the closed one's primitives.
    rex.extract("r/pics", media_types=MediaType.ALL)
    rex.close()
    result = rex.extract("r/pics", media_types=MediaType.ALL)
    assert result.posts_scanned == 2


def test_the_loop_runs_on_its_own_daemon_thread(rex):
    rex.start()
    assert rex._thread is not None
    assert rex._thread.daemon is True
    assert rex._thread.name == "reddit-extract-loop"


def test_the_browser_is_closed_when_the_extractor_is():
    browser = FakeBrowser([[harvested("a")]])
    closed: List[bool] = []

    async def track_close() -> None:
        closed.append(True)

    browser.close = track_close  # type: ignore[method-assign]
    with RedditExtractor(browser=browser):  # type: ignore[arg-type]
        pass
    assert closed == [True]


def test_finished_calls_are_not_retained(rex):
    # A pending set that only grows would leak a future per download.
    rex.extract("r/pics", media_types=MediaType.ALL)
    assert rex._pending == set()


def test_a_posix_platform_uses_the_default_event_loop(monkeypatch, rex):
    # On Windows the loop must be a Proactor one, because the selector loop cannot launch Chromium as a subprocess.
    # Everywhere else the default loop is the right one. (Asserting on the type would prove nothing here: on Windows
    # the default *is* Proactor.)
    built: List[str] = []
    real_new_event_loop = asyncio.new_event_loop

    def spy() -> asyncio.AbstractEventLoop:
        built.append("default")
        return real_new_event_loop()

    monkeypatch.setattr(sync_module.sys, "platform", "linux")
    monkeypatch.setattr(sync_module.asyncio, "new_event_loop", spy)
    rex.start()
    assert built == ["default"]


@pytest.mark.skipif(sys.platform != "win32", reason="ProactorEventLoop is Windows-only")
def test_windows_gets_a_proactor_loop(rex):
    rex.start()
    assert isinstance(rex._loop, asyncio.ProactorEventLoop)


# -- interruption and cleanup -----------------------------------------------


def test_a_keyboard_interrupt_cancels_the_call_it_interrupted(rex, monkeypatch):
    # Ctrl-C lands in the main thread while it blocks on the result. The job carries on
    # running on the loop thread unless the interrupted call cancels it.
    class InterruptedFuture:
        """A pending call whose result() is cut short by Ctrl-C."""

        def __init__(self) -> None:
            self.cancelled = False

        def add_done_callback(self, callback: object) -> None:
            pass

        def cancel(self) -> None:
            self.cancelled = True

        def result(self, timeout: float | None = None) -> object:
            raise KeyboardInterrupt()

    rex.start()  # start before patching, so the real loop is up
    future = InterruptedFuture()
    monkeypatch.setattr(
        sync_module.asyncio, "run_coroutine_threadsafe", lambda coro, loop: future
    )

    async def job() -> None:
        pass

    coro = job()
    try:
        with pytest.raises(KeyboardInterrupt):
            rex._run(coro)
    finally:
        coro.close()
    assert future.cancelled is True


def test_close_cancels_outstanding_work(rex):
    rex.start()

    async def forever() -> None:
        await asyncio.sleep(3600)

    future = asyncio.run_coroutine_threadsafe(forever(), rex._loop)
    rex._pending.add(future)
    rex.close()
    assert future.cancelled()


def test_a_hanging_browser_close_does_not_wedge_the_extractor(monkeypatch):
    # close() must return even if the browser does not go quietly.
    monkeypatch.setattr(sync_module, "_CLOSE_TIMEOUT", 0.05)
    browser = FakeBrowser([[harvested("a")]])

    async def slow_close() -> None:
        await asyncio.sleep(30)

    browser.close = slow_close  # type: ignore[method-assign]
    extractor = RedditExtractor(browser=browser)  # type: ignore[arg-type]
    extractor.start()
    extractor.close()
    assert extractor._loop is None


def test_a_hanging_browser_close_is_cancelled_not_abandoned(monkeypatch):
    # Giving up on the close is not enough: the coroutine is still live on the loop, and
    # stopping the loop with it pending makes asyncio log "Task was destroyed but it is
    # pending!" on the user's stderr. close() must cancel it and let it unwind.
    monkeypatch.setattr(sync_module, "_CLOSE_TIMEOUT", 0.05)
    cancelled = threading.Event()
    browser = FakeBrowser([[harvested("a")]])

    async def slow_close() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    browser.close = slow_close  # type: ignore[method-assign]
    extractor = RedditExtractor(browser=browser)  # type: ignore[arg-type]
    extractor.start()
    extractor.close()
    assert cancelled.is_set(), "the abandoned close was never cancelled"


def test_a_close_that_ignores_cancellation_is_still_abandoned(monkeypatch):
    # The escape hatch: a close that swallows CancelledError cannot be unwound, so the outer
    # bound gives up and abandons it rather than wedging close(). Abandoning is precisely the
    # case asyncio logs about, and this test provokes it on purpose -- mute the loop's handler
    # so the warning doesn't land in the suite's output as if something had gone wrong.
    monkeypatch.setattr(sync_module, "_CLOSE_TIMEOUT", 0.05)
    monkeypatch.setattr(sync_module, "_CANCEL_TIMEOUT", 0.05)
    browser = FakeBrowser([[harvested("a")]])

    async def stubborn_close() -> None:
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                pass  # refuses to unwind

    browser.close = stubborn_close  # type: ignore[method-assign]
    extractor = RedditExtractor(browser=browser)  # type: ignore[arg-type]
    extractor.start()
    assert extractor._loop is not None
    extractor._loop.set_exception_handler(lambda loop, context: None)
    extractor.close()
    assert extractor._loop is None


def test_a_cancelled_browser_close_does_not_wedge_the_extractor():
    browser = FakeBrowser([[harvested("a")]])

    async def cancelled_close() -> None:
        raise asyncio.CancelledError()

    browser.close = cancelled_close  # type: ignore[method-assign]
    extractor = RedditExtractor(browser=browser)  # type: ignore[arg-type]
    extractor.start()
    extractor.close()
    assert extractor._loop is None
