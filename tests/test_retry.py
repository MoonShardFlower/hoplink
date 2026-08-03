"""
Tests for the bounded download retry.

Browser-free: `_download` only touches ``ctx.download``, ``ctx.config`` and ``ctx.formats``. A small fake
context exercises the whole policy.
"""

from __future__ import annotations

import pytest

from reddit_extract.core import extractor as extractor_module
from reddit_extract.core.browser import FetchResult
from reddit_extract.core.extractor import AsyncRedditExtractor
from reddit_extract.models.config import ExtractorConfig
from reddit_extract.models.media import MediaCandidate, MediaType

JPEG = FetchResult(ok=True, status=200, content_type="image/jpeg", body=b"bytes")


class FakeContext:
    """Stands in for ExtractionContext: hands out canned fetch results and counts the calls."""

    def __init__(self, results, config=None, formats=("jpg", "jpeg")):
        self._results = list(results)
        self.config = config or ExtractorConfig()
        self.formats = frozenset(formats)
        self.fetches: list[str] = []
        self.headers: list[dict[str, str] | None] = []

    async def download(self, url: str, headers=None) -> FetchResult:
        self.fetches.append(url)
        self.headers.append(headers)
        # Once the script runs out, keep returning its final outcome.
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff waits instead of actually sleeping."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(extractor_module.asyncio, "sleep", fake_sleep)
    return waits


def candidate(**kw) -> MediaCandidate:
    kw.setdefault("url", "https://i.redd.it/x.jpg")
    kw.setdefault("media_type", MediaType.IMAGE)
    return MediaCandidate(**kw)


async def download(ctx, cand=None) -> FetchResult:
    return await AsyncRedditExtractor._download(ctx, cand or candidate())


# -- the happy path --------------------------------------------------------


async def test_success_on_first_attempt_does_not_retry(no_sleep):
    ctx = FakeContext([JPEG])
    result = await download(ctx)
    assert result.ok is True
    assert len(ctx.fetches) == 1
    assert no_sleep == []


# -- transient failures are retried ----------------------------------------


@pytest.mark.parametrize("status", [0, 408, 425, 429, 500, 502, 503, 504])
async def test_transient_failures_are_retried_then_succeed(no_sleep, status):
    ctx = FakeContext(
        [FetchResult(ok=False, status=status, error="HTTP {}".format(status)), JPEG],
        config=ExtractorConfig(max_retries=2),
    )
    result = await download(ctx)
    assert result.ok is True
    assert len(ctx.fetches) == 2


async def test_transport_error_is_retried():
    # BrowserManager reports a timeout/reset as status 0 with an error string.
    assert AsyncRedditExtractor._is_transient(
        FetchResult(ok=False, status=0, error="error: Timeout 60000ms exceeded")
    )


async def test_retries_are_bounded_by_max_retries(no_sleep):
    ctx = FakeContext(
        [FetchResult(ok=False, status=503, error="HTTP 503")],
        config=ExtractorConfig(max_retries=2),
    )
    result = await download(ctx)
    assert result.ok is False
    assert result.error == "HTTP 503"  # the last failure is what gets reported
    assert len(ctx.fetches) == 3  # 1 initial attempt + 2 retries


async def test_max_retries_zero_makes_a_single_attempt(no_sleep):
    ctx = FakeContext(
        [FetchResult(ok=False, status=503, error="HTTP 503")],
        config=ExtractorConfig(max_retries=0),
    )
    assert (await download(ctx)).ok is False
    assert len(ctx.fetches) == 1
    assert no_sleep == []


# -- permanent failures are not retried ------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451])
async def test_client_errors_are_not_retried(no_sleep, status):
    # The CDN has given its settled answer; asking again just wastes the run's time.
    ctx = FakeContext(
        [FetchResult(ok=False, status=status, error="HTTP {}".format(status))],
        config=ExtractorConfig(max_retries=5),
    )
    result = await download(ctx)
    assert result.ok is False
    assert len(ctx.fetches) == 1
    assert no_sleep == []


async def test_content_type_mismatch_is_not_retried(no_sleep):
    # Deterministic: the same bytes would come back with the same wrong type every time.
    ctx = FakeContext(
        [FetchResult(ok=True, status=200, content_type="text/html", body=b"<html>")],
        config=ExtractorConfig(max_retries=5),
    )
    result = await download(ctx)
    assert result.ok is False
    assert "unexpected content-type (text/html)" in (result.error or "")
    assert len(ctx.fetches) == 1


async def test_gif_rejection_is_not_retried(no_sleep):
    ctx = FakeContext(
        [FetchResult(ok=True, status=200, content_type="image/gif", body=b"GIF89a")],
        config=ExtractorConfig(max_retries=5),
        formats=("jpg",),  # gif not among the accepted formats
    )
    result = await download(ctx)
    assert result.ok is False
    assert result.error == "is a gif"
    assert len(ctx.fetches) == 1


async def test_gif_kept_when_the_format_is_enabled(no_sleep):
    ctx = FakeContext(
        [FetchResult(ok=True, status=200, content_type="image/gif", body=b"GIF89a")],
        formats=("jpg", "gif"),
    )
    assert (await download(ctx)).ok is True


async def test_non_image_candidate_accepts_its_own_content_type(no_sleep):
    ctx = FakeContext(
        [FetchResult(ok=True, status=200, content_type="video/mp4", body=b"\x00")]
    )
    cand = candidate(media_type=MediaType.VIDEO, content_prefixes=("video/",))
    assert (await download(ctx, cand)).ok is True


# -- backoff ---------------------------------------------------------------


async def test_backoff_grows_exponentially(no_sleep):
    ctx = FakeContext(
        [FetchResult(ok=False, status=503, error="HTTP 503")],
        config=ExtractorConfig(max_retries=3, retry_backoff=1.0, delay=0.0),
    )
    await download(ctx)
    assert no_sleep == [1.0, 2.0, 4.0]


async def test_backoff_never_dips_below_delay(no_sleep):
    # delay is the job's politeness floor; a small retry_backoff must not undercut it.
    ctx = FakeContext(
        [FetchResult(ok=False, status=503, error="HTTP 503")],
        config=ExtractorConfig(max_retries=2, retry_backoff=0.01, delay=2.0),
    )
    await download(ctx)
    assert no_sleep == [2.0, 2.0]


async def test_no_sleep_after_the_final_attempt(no_sleep):
    ctx = FakeContext(
        [FetchResult(ok=False, status=503, error="HTTP 503")],
        config=ExtractorConfig(max_retries=1, retry_backoff=1.0, delay=0.0),
    )
    await download(ctx)
    assert len(no_sleep) == 1  # waits between attempts only, not after the last one


# -- config validation -----------------------------------------------------


def test_negative_max_retries_rejected():
    with pytest.raises(ValueError, match="max_retries must be >= 0"):
        ExtractorConfig(max_retries=-1)


def test_negative_retry_backoff_rejected():
    with pytest.raises(ValueError, match="retry_backoff must be >= 0"):
        ExtractorConfig(retry_backoff=-1.0)


def test_retry_defaults():
    cfg = ExtractorConfig()
    assert cfg.max_retries == 2 and cfg.retry_backoff == 1.0


# -- retries are visible under --verbose -----------------------------------


async def test_retry_is_logged_at_info(no_sleep, caplog):
    ctx = FakeContext(
        [FetchResult(ok=False, status=503, error="HTTP 503"), JPEG],
        config=ExtractorConfig(max_retries=1),
    )
    with caplog.at_level("INFO", logger="reddit_extract.core.extractor"):
        await download(ctx)
    messages = [r.getMessage() for r in caplog.records]
    assert any("retrying in 1.0s" in m and "HTTP 503" in m for m in messages)
