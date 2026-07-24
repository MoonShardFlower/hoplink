"""
Tests for the per-job context handed to media handlers.

Browser-free: ExtractionContext is a facade over a BrowserManager and a page, so fakes standing in for those cover
every method a handler is allowed to touch.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from reddit_extract.core import context as context_module
from reddit_extract.core.browser import FetchResult
from reddit_extract.core.context import ExtractionContext
from reddit_extract.events import Events
from reddit_extract.models.config import ExtractorConfig
from reddit_extract.models.source import Subreddit

JPEG = FetchResult(ok=True, status=200, content_type="image/jpeg", body=b"bytes")


class FakePage:
    """A page that records navigation, waits, and evaluated scripts."""

    def __init__(self, result: Any = "<html>") -> None:
        self.result = result
        self.gotos: List[tuple[str, dict[str, Any]]] = []
        self.evaluated: List[tuple[str, Any]] = []
        self.waits: List[int] = []
        self.closed = False
        self.close_error: Exception | None = None

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.gotos.append((url, kwargs))

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        self.evaluated.append((js, arg))
        return self.result

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeBrowser:
    """Stands in for BrowserManager: fresh fake pages and canned fetches."""

    def __init__(self, result: FetchResult = JPEG, page_result: Any = "<html>") -> None:
        self.result = result
        self.page_result = page_result
        self.pages: List[FakePage] = []
        self.fetches: List[tuple[str, int | None]] = []
        self.fetch_headers: List[dict[str, str] | None] = []
        self.playwright_context = object()

    async def new_page(self) -> FakePage:
        page = FakePage(self.page_result)
        self.pages.append(page)
        return page

    async def fetch(
        self,
        url: str,
        timeout_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        self.fetches.append((url, timeout_ms))
        self.fetch_headers.append(headers)
        return self.result


def build(browser: FakeBrowser | None = None, events: Events | None = None, **cfg: Any):
    """A context over fake infrastructure, plus the browser and listing page behind it."""
    browser = browser or FakeBrowser()
    listing = FakePage()
    ctx = ExtractionContext(
        source=Subreddit("pics"),
        config=ExtractorConfig(**cfg),
        events=events or Events(),
        browser=browser,  # type: ignore[arg-type]
        page=listing,
    )
    return ctx, browser, listing


@pytest.fixture
def no_sleep(monkeypatch):
    """Record politeness pauses instead of actually sleeping."""
    waits: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(context_module.asyncio, "sleep", fake_sleep)
    return waits


# -- extension_of -----------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://i.redd.it/a.jpg", "jpg"),
        ("https://i.redd.it/a.JPG", "jpg"),
        (
            "https://i.redd.it/a.jpg?width=640&s=abc",
            "jpg",
        ),  # the query is not the extension
        ("https://i.redd.it/a", ""),
        ("https://i.redd.it/archive.tar.gz", "gz"),
        ("https://v.redd.it/abc123/DASH_480.mp4", "mp4"),
        ("", ""),
    ],
)
def test_extension_of(url, expected):
    assert ExtractionContext.extension_of(url) == expected


# -- evaluate_on ------------------------------------------------------------


async def test_evaluate_on_navigates_then_runs_the_script():
    ctx, browser, _ = build(FakeBrowser(page_result="carousel"))
    assert (
        await ctx.evaluate_on("https://reddit.com/p/1", "() => 1", arg="g1")
        == "carousel"
    )
    util = browser.pages[0]
    assert util.gotos[0][0] == "https://reddit.com/p/1"
    assert util.evaluated == [("() => 1", "g1")]


async def test_evaluate_on_waits_only_for_dom_content_and_the_configured_timeout():
    # Reddit's listings never go network-idle, so waiting for load would always time out.
    ctx, browser, _ = build(nav_timeout_ms=1234)
    await ctx.evaluate_on("https://reddit.com/p/1", "() => 1")
    assert browser.pages[0].gotos[0][1] == {
        "wait_until": "domcontentloaded",
        "timeout": 1234,
    }


async def test_evaluate_on_pauses_for_the_requested_settle_time():
    ctx, browser, _ = build()
    await ctx.evaluate_on("https://reddit.com/p/1", "() => 1", wait_ms=750)
    assert browser.pages[0].waits == [750]


async def test_no_settle_wait_is_requested_when_none_is_asked_for():
    ctx, browser, _ = build()
    await ctx.evaluate_on("https://reddit.com/p/1", "() => 1", wait_ms=0)
    assert browser.pages[0].waits == []


async def test_the_utility_page_is_reused_across_posts():
    # A fresh page per post would cost a tab launch each time.
    ctx, browser, _ = build()
    await ctx.evaluate_on("https://reddit.com/p/1", "() => 1")
    await ctx.evaluate_on("https://reddit.com/p/2", "() => 1")
    assert len(browser.pages) == 1
    assert [url for url, _ in browser.pages[0].gotos] == [
        "https://reddit.com/p/1",
        "https://reddit.com/p/2",
    ]


async def test_the_utility_page_is_not_the_listing_page():
    # Navigating the listing away would lose the scroll position mid-harvest.
    ctx, browser, listing = build()
    await ctx.evaluate_on("https://reddit.com/p/1", "() => 1")
    assert browser.pages[0] is not listing
    assert listing.gotos == []


# -- fetch ------------------------------------------------------------------


async def test_fetch_goes_through_the_browser_with_the_configured_timeout():
    ctx, browser, _ = build(request_timeout_ms=9999)
    assert await ctx.fetch("https://i.redd.it/a.jpg") == JPEG
    assert browser.fetches == [("https://i.redd.it/a.jpg", 9999)]
    assert browser.fetch_headers == [None]  # no headers unless a handler asks


async def test_fetch_forwards_request_headers():
    # A handler resolving a third-party API (e.g. RedGIFs) passes a bearer token through.
    ctx, browser, _ = build()
    await ctx.fetch(
        "https://api.redgifs.com/v2/gifs/x", headers={"Authorization": "Bearer t"}
    )
    assert browser.fetch_headers == [{"Authorization": "Bearer t"}]


# -- skip -------------------------------------------------------------------


async def test_skip_reports_the_source_url_and_reason():
    skips: List[tuple[Any, str, str]] = []
    ctx, _, _ = build(events=Events(on_skip=lambda s, u, r: skips.append((s, u, r))))
    await ctx.skip("https://i.redd.it/a.jpg", "is a gif")
    assert skips == [(ctx.source, "https://i.redd.it/a.jpg", "is a gif")]


async def test_skip_without_a_callback_is_a_no_op():
    ctx, _, _ = build()
    await ctx.skip("https://i.redd.it/a.jpg", "is a gif")  # must not raise


# -- sleep ------------------------------------------------------------------


async def test_sleep_waits_for_the_configured_pause(no_sleep):
    ctx, _, _ = build(scroll_pause=1.5)
    await ctx.sleep()
    assert no_sleep == [1.5]


async def test_sleep_does_nothing_when_pacing_is_disabled(no_sleep):
    ctx, _, _ = build(scroll_pause=0.0)
    await ctx.sleep()
    assert no_sleep == []


# -- escape hatches ---------------------------------------------------------


async def test_the_playwright_context_is_exposed_for_advanced_handlers():
    ctx, browser, _ = build()
    assert ctx.browser_context is browser.playwright_context


async def test_new_page_hands_the_caller_its_own_page():
    ctx, browser, _ = build()
    page = await ctx.new_page()
    assert page in browser.pages


async def test_the_listing_page_is_exposed():
    ctx, _, listing = build()
    assert ctx.page is listing


async def test_the_configured_formats_are_exposed_as_a_frozen_set():
    ctx, _, _ = build(formats="jpg,png")
    assert ctx.formats == frozenset({"jpg", "png"})


# -- aclose -----------------------------------------------------------------


async def test_aclose_closes_the_utility_page():
    ctx, browser, _ = build()
    await ctx.evaluate_on("https://reddit.com/p/1", "() => 1")
    await ctx.aclose()
    assert browser.pages[0].closed is True


async def test_aclose_leaves_the_listing_page_to_its_owner():
    # The engine closes the listing page itself; closing it twice would raise.
    ctx, _, listing = build()
    await ctx.evaluate_on("https://reddit.com/p/1", "() => 1")
    await ctx.aclose()
    assert listing.closed is False


async def test_aclose_without_a_utility_page_is_a_no_op():
    ctx, browser, _ = build()
    await ctx.aclose()
    assert browser.pages == []


async def test_aclose_is_idempotent():
    ctx, browser, _ = build()
    await ctx.evaluate_on("https://reddit.com/p/1", "() => 1")
    await ctx.aclose()
    await ctx.aclose()
    assert len(browser.pages) == 1


async def test_a_later_evaluate_opens_a_fresh_utility_page_after_aclose():
    ctx, browser, _ = build()
    await ctx.evaluate_on("https://reddit.com/p/1", "() => 1")
    await ctx.aclose()
    await ctx.evaluate_on("https://reddit.com/p/2", "() => 1")
    assert len(browser.pages) == 2
