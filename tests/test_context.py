"""
Tests for the per-job context handed to media handlers.

Browser-free: ExtractionContext is a facade over a BrowserManager and a page, so fakes standing in for those cover
every method a handler is allowed to touch.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from hoplink.core import context as context_module
from hoplink.core.browser import FetchResult
from hoplink.core.context import ExtractionContext
from hoplink.core.state import SharedState
from hoplink.events import Events
from hoplink.models.config import ExtractorConfig
from hoplink.models.media import MediaType
from hoplink.models.source import Subreddit

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
    """Stands in for BrowserManager: fresh fake pages and canned fetches.

    ``results`` scripts a sequence of outcomes, one per call, the last repeating once the script runs out --
    enough to model a host that fails a few times and then answers.
    """

    def __init__(
        self,
        result: FetchResult = JPEG,
        page_result: Any = "<html>",
        results: List[FetchResult] | None = None,
    ) -> None:
        self.result = result
        self.page_result = page_result
        self._results = list(results) if results else None
        self.pages: List[FakePage] = []
        self.fetches: List[tuple[str, int | None]] = []
        self.fetch_headers: List[dict[str, str] | None] = []
        self.downloads: List[tuple[str, int | None]] = []
        self.download_headers: List[dict[str, str] | None] = []
        self.playwright_context = object()

    def _next(self) -> FetchResult:
        if self._results is None:
            return self.result
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]

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
        return self._next()

    async def download(
        self,
        url: str,
        timeout_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        self.downloads.append((url, timeout_ms))
        self.download_headers.append(headers)
        return self._next()


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


async def test_an_explicit_pause_overrides_the_scroll_pause(no_sleep):
    # How a handler paces something that isn't scrolling, e.g. paging a third-party API.
    ctx, _, _ = build(scroll_pause=1.5)
    await ctx.sleep(0.25)
    assert no_sleep == [0.25]


async def test_an_explicit_zero_pause_does_not_sleep(no_sleep):
    ctx, _, _ = build(scroll_pause=1.5)
    await ctx.sleep(0.0)
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


def failed(status: int) -> FetchResult:
    return FetchResult(ok=False, status=status, error="HTTP {}".format(status))


async def test_a_successful_fetch_is_not_retried(no_sleep):
    ctx, browser, _ = build()
    result = await ctx.fetch("https://api.example/v1/thing")
    assert result.ok is True
    assert len(browser.fetches) == 1
    assert no_sleep == []


@pytest.mark.parametrize("status", [0, 408, 425, 429, 500, 502, 503])
async def test_a_transient_failure_is_retried_then_succeeds(no_sleep, status):
    browser = FakeBrowser(results=[failed(status), JPEG])
    ctx, _, _ = build(browser)
    result = await ctx.fetch("https://api.example/v1/thing")
    assert result.ok is True
    assert len(browser.fetches) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
async def test_a_settled_answer_is_not_retried(no_sleep, status):
    browser = FakeBrowser(results=[failed(status), JPEG])
    ctx, _, _ = build(browser)
    result = await ctx.fetch("https://api.example/v1/thing")
    assert result.ok is False
    assert len(browser.fetches) == 1


async def test_fetch_retries_are_bounded_by_max_retries(no_sleep):
    browser = FakeBrowser(results=[failed(429)])
    ctx, _, _ = build(browser, max_retries=2)
    result = await ctx.fetch("https://api.example/v1/thing")
    assert result.ok is False
    assert len(browser.fetches) == 3  # the first attempt plus two retries


async def test_fetch_retries_can_be_waived_for_one_call(no_sleep):
    browser = FakeBrowser(results=[failed(429)])
    ctx, _, _ = build(browser, max_retries=2)
    await ctx.fetch("https://api.example/v1/thing", retries=0)
    assert len(browser.fetches) == 1


async def test_fetch_backoff_grows_and_never_dips_below_the_api_pause(no_sleep):
    browser = FakeBrowser(results=[failed(500)])
    ctx, _, _ = build(browser, max_retries=3, retry_backoff=1.0, api_pause=2.5)
    await ctx.fetch("https://api.example/v1/thing")
    assert no_sleep == [2.5, 2.5, 4.0]  # 1, 2, 4


async def test_fetch_forwards_its_headers_on_every_attempt(no_sleep):
    browser = FakeBrowser(results=[failed(503), JPEG])
    ctx, _, _ = build(browser)
    await ctx.fetch("https://api.example/v1/thing", {"Authorization": "Bearer t"})
    assert browser.fetch_headers == [
        {"Authorization": "Bearer t"},
        {"Authorization": "Bearer t"},
    ]


async def test_download_forwards_a_candidates_headers():
    # Some CDNs answer 403 without a Referer, so the candidate has to be able to ask for one.
    ctx, browser, _ = build()
    await ctx.download("https://cdn.example/v.mp4", {"Referer": "https://example.com/"})
    assert browser.download_headers == [{"Referer": "https://example.com/"}]


async def test_download_sends_no_extra_headers_by_default():
    ctx, browser, _ = build()
    await ctx.download("https://cdn.example/v.mp4")
    assert browser.download_headers == [None]


def test_each_namespace_gets_its_own_store():
    ctx, _, _ = build()
    ctx.state("redgifs").data["token"] = "abc"
    assert ctx.state("imgur").data == {}
    assert ctx.state("redgifs").data == {"token": "abc"}


def test_one_namespace_is_the_same_store_every_time():
    ctx, _, _ = build()
    assert ctx.state("redgifs") is ctx.state("redgifs")


def test_two_contexts_over_one_store_share_it():
    shared = SharedState()
    browser = FakeBrowser()
    contexts = [
        ExtractionContext(
            source=Subreddit("pics"),
            config=ExtractorConfig(),
            events=Events(),
            browser=browser,  # type: ignore[arg-type]
            page=FakePage(),
            state=shared,
        )
        for _ in range(2)
    ]
    contexts[0].state("redgifs").data["seen"] = {"creator"}
    assert contexts[1].state("redgifs").data["seen"] == {"creator"}


def test_a_context_without_a_store_gets_a_private_one():
    first, _, _ = build()
    second, _, _ = build()
    first.state("redgifs").data["token"] = "abc"
    assert second.state("redgifs").data == {}


def test_a_host_option_is_read_from_the_config():
    ctx, _, _ = build(host_options={"imgur": {"client_id": "abc123"}})
    assert ctx.host_option("imgur", "client_id") == "abc123"


def test_an_unset_host_option_falls_back_to_the_default():
    ctx, _, _ = build(host_options={"imgur": {"client_id": "abc123"}})
    assert ctx.host_option("imgur", "secret", "none") == "none"
    assert ctx.host_option("nobody", "client_id") is None


@pytest.mark.parametrize(
    "configured", ["Alice, BOB", ["Alice", "BOB"], ("Alice", "BOB")]
)
def test_a_host_list_option_normalizes_however_it_was_written(configured):
    ctx, _, _ = build(host_options={"redgifs": {"blacklist": configured}})
    assert ctx.host_list("redgifs", "blacklist") == ("alice", "bob")


def test_an_unset_host_list_option_is_empty():
    ctx, _, _ = build()
    assert ctx.host_list("redgifs", "blacklist") == ()


def test_scrape_all_reads_the_configured_hosts():
    ctx, _, _ = build(scrape_all_hosts=("redgifs",))
    assert ctx.scrape_all("redgifs") is True
    assert ctx.scrape_all("soundgasm") is False


def test_wanted_defaults_to_everything_when_a_job_does_not_narrow_it():
    ctx, _, _ = build()
    assert ctx.wanted is MediaType.ALL
