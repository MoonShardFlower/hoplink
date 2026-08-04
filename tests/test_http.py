"""Tests for the direct (browser-free) HTTP route."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from hoplink.core.http import FetchResult, fetch_direct, looks_blocked

URL = "https://media.redgifs.com/Clip.mp4"


class FakeResponse:
    """The context-manager object ``urlopen`` hands back."""

    def __init__(
        self,
        status: int = 200,
        headers: dict | None = None,
        body: bytes = b"videobytes",
    ) -> None:
        self.status = status
        self.headers = headers if headers is not None else {"content-type": "video/mp4"}
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def urlopen(monkeypatch):
    """Capture the request ``fetch_direct`` builds, and script what ``urlopen`` does with it."""

    calls: list = []
    outcome: list = [FakeResponse()]

    def fake_urlopen(request, timeout=None):
        calls.append((request, timeout))
        result = outcome[0]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return type("Urlopen", (), {"calls": calls, "outcome": outcome})


async def direct(**kwargs) -> FetchResult:
    kwargs.setdefault("timeout_ms", 30000)
    return await fetch_direct(URL, **kwargs)


@pytest.mark.asyncio
async def test_a_response_becomes_a_successful_result(urlopen):
    result = await direct()
    assert result.ok is True
    assert result.status == 200
    assert result.content_type == "video/mp4"
    assert result.body == b"videobytes"


@pytest.mark.asyncio
async def test_content_type_parameters_are_stripped_and_lowercased(urlopen):
    urlopen.outcome[0] = FakeResponse(
        headers={"content-type": "VIDEO/MP4; codecs=avc1"}
    )
    assert (await direct()).content_type == "video/mp4"


@pytest.mark.asyncio
async def test_a_missing_content_type_becomes_empty(urlopen):
    urlopen.outcome[0] = FakeResponse(headers={})
    assert (await direct()).content_type == ""


@pytest.mark.asyncio
async def test_both_timings_are_recorded(urlopen):
    result = await direct()
    assert result.wait >= 0.0
    assert result.transfer >= 0.0


@pytest.mark.asyncio
async def test_the_given_headers_are_sent(urlopen):
    await direct(headers={"User-Agent": "Mozilla/5.0 test", "Cookie": "over18=1"})
    request = urlopen.calls[0][0]
    # urllib canonicalizes header names to Capitalized-Case.
    assert request.get_header("User-agent") == "Mozilla/5.0 test"
    assert request.get_header("Cookie") == "over18=1"


@pytest.mark.asyncio
async def test_the_timeout_is_passed_in_seconds(urlopen):
    await direct(timeout_ms=4500)
    assert urlopen.calls[0][1] == 4.5


@pytest.mark.asyncio
async def test_it_is_a_get(urlopen):
    await direct()
    assert urlopen.calls[0][0].get_method() == "GET"


@pytest.mark.asyncio
async def test_a_non_2xx_keeps_its_status(urlopen):
    urlopen.outcome[0] = urllib.error.HTTPError(URL, 403, "Forbidden", {}, None)
    result = await direct()
    assert result.ok is False
    assert result.status == 403
    assert result.error == "HTTP 403"


@pytest.mark.asyncio
async def test_a_transport_failure_reports_status_zero(urlopen):
    urlopen.outcome[0] = OSError("connection reset")
    result = await direct()
    assert result.ok is False
    assert result.status == 0
    assert "connection reset" in result.error


@pytest.mark.asyncio
async def test_a_failure_still_reports_the_time_it_burned(urlopen):
    urlopen.outcome[0] = OSError("timed out")
    assert (await direct()).wait >= 0.0


@pytest.mark.parametrize("status", [0, 401, 403, 405, 406, 451])
def test_a_refusal_is_worth_trying_through_the_browser(status):
    assert looks_blocked(FetchResult(ok=False, status=status, error="nope"))


@pytest.mark.parametrize("status", [404, 410, 429, 500, 503])
def test_a_real_answer_is_not_retried_through_the_browser(status):
    assert not looks_blocked(FetchResult(ok=False, status=status, error="nope"))


def test_a_success_is_never_treated_as_blocked():
    assert not looks_blocked(FetchResult(ok=True, status=200, body=b"x"))
