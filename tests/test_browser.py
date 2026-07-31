"""Tests for the Playwright lifecycle wrapper.

No Chromium is ever launched. `BrowserManager` reaches Playwright through a small number of objects (driver, context,
request API, page): we use fakes standing in for those exercise every branch.

`start` is the one method that must import Playwright for real (it resolves the driver and the timeout type lazily),
so those tests patch the imported module and skip if it is absent. The rest inject a fake context.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from typing import Any, List

import pytest

from reddit_extract.core.browser import GATE_BUTTON_NAMES, BrowserManager, FetchResult
from reddit_extract.exceptions import BrowserError
from reddit_extract.models.config import ExtractorConfig


class FakeResponse:
    """A Playwright APIResponse: headers, a body that may fail, and disposal tracking."""

    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"imagebytes",
        body_error: Exception | None = None,
        dispose_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.ok = 200 <= status < 300
        self.headers = {"content-type": "image/jpeg"} if headers is None else headers
        self._body = body
        self._body_error = body_error
        self._dispose_error = dispose_error
        self.disposed = False

    async def body(self) -> bytes:
        if self._body_error is not None:
            raise self._body_error
        return self._body

    async def dispose(self) -> None:
        self.disposed = True
        if self._dispose_error is not None:
            raise self._dispose_error


class FakeRequestAPI:
    """The context's ``request`` namespace: one canned response, or a transport error."""

    def __init__(
        self, response: FakeResponse | None = None, error: Exception | None = None
    ) -> None:
        self.response = response if response is not None else FakeResponse()
        self.error = error
        self.calls: List[tuple[str, Any]] = []
        self.headers: List[dict[str, str] | None] = []

    async def get(
        self, url: str, timeout: Any = None, headers: dict[str, str] | None = None
    ) -> FakeResponse:
        self.calls.append((url, timeout))
        self.headers.append(headers)
        if self.error is not None:
            raise self.error
        return self.response


class FakeContext:
    """A Playwright BrowserContext: hands out pages, serves cookies, tracks closure."""

    def __init__(self, request: FakeRequestAPI | None = None) -> None:
        self.request = request if request is not None else FakeRequestAPI()
        self.pages: List[object] = []
        self.closed = False
        self.cookie_list: List[dict[str, Any]] = []
        self.cookie_error: Exception | None = None
        self.cookie_queries = 0

    async def new_page(self) -> object:
        page = object()
        self.pages.append(page)
        return page

    async def cookies(self, url: str) -> List[dict[str, Any]]:
        self.cookie_queries += 1
        if self.cookie_error is not None:
            raise self.cookie_error
        return self.cookie_list

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    """A Playwright Browser: makes incognito contexts."""

    def __init__(self) -> None:
        self.context = FakeContext()
        self.context_kwargs: dict[str, Any] | None = None
        self.closed = False

    async def new_context(self, **kwargs: Any) -> FakeContext:
        self.context_kwargs = kwargs
        return self.context

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    """The driver's chromium launcher, in both its incognito and persistent flavours."""

    def __init__(self, launch_error: Exception | None = None) -> None:
        self.launch_error = launch_error
        self.browser = FakeBrowser()
        self.persistent_context = FakeContext()
        self.launches = 0
        self.launch_kwargs: dict[str, Any] | None = None
        self.persistent_dir: str | None = None
        self.persistent_kwargs: dict[str, Any] | None = None

    async def launch(self, **kwargs: Any) -> FakeBrowser:
        self.launches += 1
        self.launch_kwargs = kwargs
        if self.launch_error is not None:
            raise self.launch_error
        return self.browser

    async def launch_persistent_context(
        self, profile_dir: str, **kwargs: Any
    ) -> FakeContext:
        self.launches += 1
        self.persistent_dir = profile_dir
        self.persistent_kwargs = kwargs
        if self.launch_error is not None:
            raise self.launch_error
        return self.persistent_context


class FakeDriver:
    """What ``async_playwright().start()`` returns."""

    def __init__(self, launch_error: Exception | None = None) -> None:
        self.chromium = FakeChromium(launch_error)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeStarter:
    """What ``async_playwright()`` itself returns: an object whose start() yields the driver."""

    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver

    async def start(self) -> FakeDriver:
        return self._driver


@pytest.fixture
def driver(monkeypatch):
    """Patch the lazily imported ``async_playwright`` so start() launches fakes, not Chromium."""
    pytest.importorskip("playwright.async_api")
    fake = FakeDriver()
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: FakeStarter(fake)
    )
    return fake


@pytest.fixture
def direct(monkeypatch):
    """
    Replace the direct route, so no socket is opened and its answer can be scripted.

    ``urls``/``headers`` record what it was asked for; setting ``result`` makes it answer with that
    instead of a plain success.
    """
    recorder = SimpleNamespace(urls=[], headers=[], result=None)

    async def fake_fetch_direct(url, *, headers=None, timeout_ms):
        recorder.urls.append(url)
        recorder.headers.append(dict(headers or {}))
        if recorder.result is not None:
            return recorder.result
        return FetchResult(
            ok=True, status=200, content_type="video/mp4", body=b"direct"
        )

    monkeypatch.setattr("reddit_extract.core.browser.fetch_direct", fake_fetch_direct)
    return recorder


def started(context: FakeContext, **cfg: Any) -> BrowserManager:
    """A manager wired to a fake context, as if start() had already launched it."""
    mgr = BrowserManager(ExtractorConfig(**cfg))
    mgr._context = context
    return mgr


# -- start: launching -------------------------------------------------------


async def test_a_fresh_manager_is_not_started():
    assert BrowserManager(ExtractorConfig()).started is False


async def test_start_launches_an_incognito_context_by_default(driver):
    mgr = BrowserManager(ExtractorConfig(headless=True, locale="de-DE"))
    await mgr.start()
    assert mgr.started is True
    assert driver.chromium.launch_kwargs == {"headless": True}
    assert driver.chromium.browser.context_kwargs == {
        "user_agent": ExtractorConfig().user_agent,
        "viewport": {"width": 1366, "height": 900},
        "locale": "de-DE",
    }


async def test_start_is_idempotent(driver):
    mgr = BrowserManager(ExtractorConfig())
    await mgr.start()
    await mgr.start()
    assert driver.chromium.launches == 1  # the second call is a no-op


async def test_a_profile_dir_switches_to_a_persistent_context(driver, tmp_path):
    profile = tmp_path / "rex-profile"
    mgr = BrowserManager(ExtractorConfig(profile_dir=str(profile), headless=False))
    await mgr.start()
    assert driver.chromium.persistent_dir == str(profile)
    assert profile.is_dir()  # created for you on first use
    assert driver.chromium.persistent_kwargs["headless"] is False
    assert driver.chromium.persistent_kwargs["locale"] == "en-US"


async def test_a_profile_dir_is_expanded_before_use(driver):
    mgr = BrowserManager(ExtractorConfig(profile_dir="~/rex-test-profile"))
    try:
        await mgr.start()
    finally:
        await mgr.close()
    # Playwright has no idea what ~ means, so the tilde must never reach it.
    assert not (driver.chromium.persistent_dir or "").startswith("~")
    os.rmdir(os.path.expanduser("~/rex-test-profile"))


async def test_start_records_playwrights_timeout_type(driver):
    # wait_for_posts catches this exact type; a stale one would let timeouts escape.
    pw = pytest.importorskip("playwright.async_api")
    mgr = BrowserManager(ExtractorConfig())
    await mgr.start()
    assert mgr._timeout_error is pw.TimeoutError


async def test_the_underlying_context_is_reachable(driver):
    mgr = BrowserManager(ExtractorConfig())
    await mgr.start()
    assert mgr.playwright_context is driver.chromium.browser.context


# -- start: failure ---------------------------------------------------------


async def test_a_launch_failure_becomes_a_browser_error(driver):
    driver.chromium.launch_error = RuntimeError("Executable doesn't exist")
    mgr = BrowserManager(ExtractorConfig())
    with pytest.raises(BrowserError, match="could not launch Chromium"):
        await mgr.start()


async def test_a_failed_start_releases_the_driver(driver):
    # Otherwise a doomed retry loop would leak a node process per attempt.
    driver.chromium.launch_error = RuntimeError("boom")
    mgr = BrowserManager(ExtractorConfig())
    with pytest.raises(BrowserError):
        await mgr.start()
    assert driver.stopped is True
    assert mgr.started is False


async def test_a_browser_error_from_below_is_not_double_wrapped(driver):
    driver.chromium.launch_error = BrowserError("already explained")
    mgr = BrowserManager(ExtractorConfig())
    with pytest.raises(BrowserError, match="^already explained$"):
        await mgr.start()


# -- close ------------------------------------------------------------------


async def test_close_releases_context_browser_and_driver(driver):
    mgr = BrowserManager(ExtractorConfig())
    await mgr.start()
    context = driver.chromium.browser.context
    await mgr.close()
    assert context.closed is True
    assert driver.chromium.browser.closed is True
    assert driver.stopped is True
    assert mgr.started is False


async def test_close_is_safe_when_never_started():
    await BrowserManager(ExtractorConfig()).close()  # must not raise


async def test_close_is_idempotent(driver):
    mgr = BrowserManager(ExtractorConfig())
    await mgr.start()
    await mgr.close()
    await mgr.close()


async def test_a_manager_can_be_restarted_after_close(driver):
    mgr = BrowserManager(ExtractorConfig())
    await mgr.start()
    await mgr.close()
    await mgr.start()
    assert mgr.started is True
    assert driver.chromium.launches == 2


# -- new_page ---------------------------------------------------------------


async def test_new_page_before_start_raises():
    with pytest.raises(BrowserError, match="browser is not started"):
        await BrowserManager(ExtractorConfig()).new_page()


async def test_new_page_comes_from_the_context():
    context = FakeContext()
    page = await started(context).new_page()
    assert context.pages == [page]


# -- wait_for_posts ---------------------------------------------------------


class FakeSelectorPage:
    """A page whose wait_for_selector either resolves or times out."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.selectors: List[tuple[str, int]] = []

    async def wait_for_selector(self, selector: str, timeout: int) -> None:
        self.selectors.append((selector, timeout))
        if self.error is not None:
            raise self.error


async def test_wait_for_posts_is_true_once_cards_render():
    page = FakeSelectorPage()
    assert await BrowserManager(ExtractorConfig()).wait_for_posts(page, 500) is True


async def test_wait_for_posts_waits_for_either_ui():
    # Reddit still serves some listings (multireddits) on the legacy layout.
    page = FakeSelectorPage()
    await BrowserManager(ExtractorConfig()).wait_for_posts(page, 500)
    assert page.selectors == [("shreddit-post, #siteTable .thing", 500)]


async def test_a_timeout_is_reported_as_false_not_raised():
    # The engine turns this into NoPostsFoundError with an actionable message.
    page = FakeSelectorPage(TimeoutError("Timeout 30000ms exceeded"))
    assert await BrowserManager(ExtractorConfig()).wait_for_posts(page, 500) is False


# -- dismiss_gates ----------------------------------------------------------


class FakeLocator:
    """A Playwright locator, which stops matching once its button is clicked away."""

    def __init__(self, count: int, click_error: Exception | None = None) -> None:
        self._count = count
        self._click_error = click_error
        self.clicks = 0

    async def count(self) -> int:
        return self._count

    @property
    def first(self) -> "FakeLocator":
        return self

    async def click(self, timeout: int | None = None) -> None:
        if self._click_error is not None:
            raise self._click_error
        self.clicks += 1
        self._count = 0  # dismissing the interstitial takes its button with it


class FakeGatePage:
    """A page where only the named buttons exist."""

    def __init__(self, present: dict[str, FakeLocator] | None = None) -> None:
        self.present = present or {}
        self.asked: List[str] = []
        self.waits = 0

    def get_by_role(self, role: str, name: Any) -> FakeLocator:
        # BrowserManager asks by case-insensitive regex; match it against what this page has.
        self.asked.append(name.pattern)
        for label, locator in self.present.items():
            if name.search(label):
                return locator
        return FakeLocator(0)

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits += 1


async def test_a_present_gate_is_clicked_away():
    button = FakeLocator(1)
    page = FakeGatePage({"Yes, I'm over 18": button})
    await BrowserManager(ExtractorConfig()).dismiss_gates(page)
    assert button.clicks == 1
    assert page.waits == 1  # a beat for the interstitial to disappear


async def test_a_dismissed_gate_is_not_clicked_again_by_an_overlapping_name():
    # The names are matched as case-insensitive substrings, so "I'm over 18" and "Yes" both
    # also match a "Yes, I'm over 18" button. Only the gate that is still there gets clicked.
    button = FakeLocator(1)
    page = FakeGatePage({"Yes, I'm over 18": button})
    await BrowserManager(ExtractorConfig()).dismiss_gates(page)
    assert button.clicks == 1


async def test_absent_gates_are_not_clicked():
    page = FakeGatePage()
    await BrowserManager(ExtractorConfig()).dismiss_gates(page)
    assert page.waits == 0


async def test_every_known_gate_name_is_tried():
    page = FakeGatePage()
    await BrowserManager(ExtractorConfig()).dismiss_gates(page)
    assert page.asked == list(GATE_BUTTON_NAMES)


async def test_a_click_failure_does_not_abort_the_sweep():
    # Gate dismissal is best-effort: a stale or covered button must not sink the job.
    doomed = FakeLocator(1, click_error=RuntimeError("element is not visible"))
    reachable = FakeLocator(1)
    page = FakeGatePage({"Accept all": doomed, "Continue": reachable})
    await BrowserManager(ExtractorConfig()).dismiss_gates(page)
    assert reachable.clicks == 1


# -- fetch: the happy path --------------------------------------------------


async def test_fetch_before_start_raises():
    with pytest.raises(BrowserError, match="browser is not started"):
        await BrowserManager(ExtractorConfig()).fetch("https://i.redd.it/a.jpg")


async def test_a_successful_fetch_carries_body_status_and_type():
    mgr = started(FakeContext(FakeRequestAPI(FakeResponse())))
    result = await mgr.fetch("https://i.redd.it/a.jpg")
    # The timings are wall-clock, so compare everything else.
    assert result._replace(wait=0.0, transfer=0.0) == FetchResult(
        ok=True, status=200, content_type="image/jpeg", body=b"imagebytes", error=None
    )


async def test_content_type_parameters_are_stripped_and_the_type_lowercased():
    response = FakeResponse(headers={"content-type": "IMAGE/JPEG; charset=utf-8"})
    mgr = started(FakeContext(FakeRequestAPI(response)))
    assert (await mgr.fetch("https://i.redd.it/a.jpg")).content_type == "image/jpeg"


async def test_a_missing_content_type_header_becomes_empty():
    mgr = started(FakeContext(FakeRequestAPI(FakeResponse(headers={}))))
    assert (await mgr.fetch("https://i.redd.it/a.jpg")).content_type == ""


# -- fetch: failures are returned, never raised -----------------------------


async def test_a_non_2xx_response_is_reported_not_raised():
    mgr = started(FakeContext(FakeRequestAPI(FakeResponse(status=404))))
    result = await mgr.fetch("https://i.redd.it/gone.jpg")
    assert result.ok is False
    assert result.status == 404
    assert result.error == "HTTP 404"
    assert result.body is None  # an error page's bytes are worthless


async def test_a_transport_error_becomes_status_zero():
    # _is_transient keys off status 0 to decide a timeout or reset is worth retrying.
    api = FakeRequestAPI(error=RuntimeError("Timeout 60000ms exceeded"))
    result = await started(FakeContext(api)).fetch("https://i.redd.it/a.jpg")
    assert result.ok is False
    assert result.status == 0
    assert result.error == "error: Timeout 60000ms exceeded"


async def test_a_body_read_failure_is_reported_as_an_error():
    response = FakeResponse(body_error=RuntimeError("connection reset"))
    mgr = started(FakeContext(FakeRequestAPI(response)))
    result = await mgr.fetch("https://i.redd.it/a.jpg")
    assert result.ok is False
    assert result.error == "error: connection reset"


# -- fetch: disposal --------------------------------------------------------


async def test_a_successful_response_is_disposed():
    # Playwright holds bodies in memory until the context closes; a long job would balloon.
    response = FakeResponse()
    await started(FakeContext(FakeRequestAPI(response))).fetch(
        "https://i.redd.it/a.jpg"
    )
    assert response.disposed is True


async def test_a_failed_response_is_disposed_too():
    response = FakeResponse(status=500)
    await started(FakeContext(FakeRequestAPI(response))).fetch(
        "https://i.redd.it/a.jpg"
    )
    assert response.disposed is True


async def test_a_dispose_failure_does_not_lose_the_result():
    response = FakeResponse(dispose_error=RuntimeError("already disposed"))
    mgr = started(FakeContext(FakeRequestAPI(response)))
    assert (await mgr.fetch("https://i.redd.it/a.jpg")).ok is True


# -- fetch: timeouts --------------------------------------------------------


async def test_fetch_uses_the_configured_timeout_by_default():
    api = FakeRequestAPI()
    await started(FakeContext(api), request_timeout_ms=12345).fetch("https://x/a.jpg")
    assert api.calls == [("https://x/a.jpg", 12345)]


async def test_an_explicit_timeout_overrides_the_configured_one():
    api = FakeRequestAPI()
    await started(FakeContext(api), request_timeout_ms=12345).fetch(
        "https://x/a.jpg", 50
    )
    assert api.calls == [("https://x/a.jpg", 50)]


# -- fetch: headers ---------------------------------------------------------


async def test_no_headers_are_passed_by_default():
    # The common call must stay get(url, timeout=...); nothing extra reaches Playwright.
    api = FakeRequestAPI()
    await started(FakeContext(api)).fetch("https://x/a.jpg")
    assert api.headers == [None]


async def test_request_headers_are_forwarded_when_given():
    api = FakeRequestAPI()
    await started(FakeContext(api)).fetch(
        "https://api.redgifs.com/v2/gifs/x", headers={"Authorization": "Bearer t"}
    )
    assert api.headers == [{"Authorization": "Bearer t"}]


# -- cookies ----------------------------------------------------------------


async def test_cookies_become_a_header_value():
    context = FakeContext()
    context.cookie_list = [
        {"name": "over18", "value": "1"},
        {"name": "session", "value": "abc"},
    ]
    assert await started(context).cookie_header("https://i.redd.it/a.jpg") == (
        "over18=1; session=abc"
    )


async def test_no_cookies_is_an_empty_header():
    assert await started(FakeContext()).cookie_header("https://i.redd.it/a.jpg") == ""


async def test_cookies_are_empty_before_the_browser_starts():
    mgr = BrowserManager(ExtractorConfig())
    assert await mgr.cookie_header("https://i.redd.it/a.jpg") == ""


async def test_a_playwright_failure_yields_no_cookies_rather_than_raising():
    context = FakeContext()
    context.cookie_error = RuntimeError("context closed")
    assert await started(context).cookie_header("https://i.redd.it/a.jpg") == ""


# -- download: picking a route ----------------------------------------------


async def test_media_goes_direct_by_default(direct):
    api = FakeRequestAPI()
    result = await started(FakeContext(api)).download("https://media.redgifs.com/a.mp4")
    assert result.body == b"direct"
    assert direct.urls == ["https://media.redgifs.com/a.mp4"]
    assert api.calls == []  # the browser was never asked


async def test_the_browser_is_used_when_direct_downloads_are_off(direct):
    api = FakeRequestAPI()
    await started(FakeContext(api), direct_download=False).download(
        "https://media.redgifs.com/a.mp4"
    )
    assert direct.urls == []
    assert api.calls == [("https://media.redgifs.com/a.mp4", 60000)]


async def test_a_direct_request_carries_the_user_agent_and_cookies(direct):
    context = FakeContext()
    context.cookie_list = [{"name": "over18", "value": "1"}]
    await started(context, user_agent="Mozilla/5.0 test").download(
        "https://i.redd.it/a.jpg"
    )
    assert direct.headers[0]["User-Agent"] == "Mozilla/5.0 test"
    assert direct.headers[0]["Cookie"] == "over18=1"


async def test_cookies_are_queried_once_per_host(direct):
    context = FakeContext()
    context.cookie_list = [{"name": "over18", "value": "1"}]
    mgr = started(context)
    for _ in range(3):
        await mgr.download("https://i.redd.it/a.jpg")
    assert context.cookie_queries == 1


async def test_no_cookie_header_is_sent_when_there_are_none(direct):
    await started(FakeContext()).download("https://i.redd.it/a.jpg")
    assert "Cookie" not in direct.headers[0]


# -- download: falling back -------------------------------------------------


async def test_a_host_that_refuses_directly_is_retried_through_the_browser(direct):
    direct.result = FetchResult(ok=False, status=403, error="HTTP 403")
    api = FakeRequestAPI()
    result = await started(FakeContext(api)).download("https://media.redgifs.com/a.mp4")
    assert result.body == b"imagebytes"  # the browser's answer won
    assert api.calls == [("https://media.redgifs.com/a.mp4", 60000)]


async def test_a_refusing_host_is_only_attempted_directly_once(direct):
    direct.result = FetchResult(ok=False, status=403, error="HTTP 403")
    mgr = started(FakeContext(FakeRequestAPI()))
    for _ in range(3):
        await mgr.download("https://media.redgifs.com/a.mp4")
    assert len(direct.urls) == 1


async def test_the_fallback_is_remembered_per_host_not_globally(direct):
    direct.result = FetchResult(ok=False, status=403, error="HTTP 403")
    mgr = started(FakeContext(FakeRequestAPI()))
    await mgr.download("https://media.redgifs.com/a.mp4")
    direct.result = None  # this host is fine
    await mgr.download("https://i.redd.it/a.jpg")
    assert direct.urls == ["https://media.redgifs.com/a.mp4", "https://i.redd.it/a.jpg"]


async def test_a_missing_file_is_reported_rather_than_retried(direct):
    # A 404 would be a 404 in the browser too; retrying it just doubles the cost.
    direct.result = FetchResult(ok=False, status=404, error="HTTP 404")
    api = FakeRequestAPI()
    result = await started(FakeContext(api)).download("https://media.redgifs.com/a.mp4")
    assert result.status == 404
    assert api.calls == []


async def test_the_fallback_is_logged(direct, caplog):
    direct.result = FetchResult(ok=False, status=403, error="HTTP 403")
    with caplog.at_level(logging.INFO, logger="reddit_extract.core.browser"):
        await started(FakeContext(FakeRequestAPI())).download("https://x/a.mp4")
    assert "refused a direct request" in caplog.text
