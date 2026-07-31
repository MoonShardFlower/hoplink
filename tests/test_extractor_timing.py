"""Tests that a job attributes its time to the right phase."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from reddit_extract.core.browser import FetchResult
from reddit_extract.models.media import MediaType
from tests.test_extractor import FakeBrowser, build, run
from tests.test_extractor_filtering import harvested


def timed(wait: float, transfer: float, body: bytes = b"imagebytes") -> FetchResult:
    """A successful image fetch that claims to have taken a fixed amount of time."""
    return FetchResult(
        ok=True,
        status=200,
        content_type="image/jpeg",
        body=body,
        wait=wait,
        transfer=transfer,
    )


class TimedBrowser(FakeBrowser):
    """Serves each URL a scripted FetchResult, so phase seconds come out deterministic."""

    async def fetch(
        self,
        url: str,
        timeout_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        self.fetched.append(url)
        route = self.results[url]
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        return route


def one_post(result: Any) -> TimedBrowser:
    """A one-post listing whose image resolves to ``result``."""
    return TimedBrowser([[harvested("a")]], results={"https://i.redd.it/a.jpg": result})


@pytest.mark.asyncio
async def test_a_run_attributes_the_network_split_it_was_given():
    result, _, _ = await run(one_post(timed(0.25, 1.75)), limit=1)
    assert result.timings.seconds["fetch_wait"] == 0.25
    assert result.timings.seconds["fetch_body"] == 1.75
    assert result.timings.downloaded_bytes == len(b"imagebytes")


@pytest.mark.asyncio
async def test_every_phase_of_a_normal_save_is_measured():
    result, _, _ = await run(one_post(timed(0.1, 0.2)), limit=1, dedupe_by_hash=True)
    for phase in ("harvest", "resolve", "fetch_wait", "fetch_body", "pace", "hash"):
        assert phase in result.timings.calls, phase
    assert result.media_saved == 1
    assert result.timings.calls["write"] == 1
    assert result.timings.calls["manifest"] >= 1


@pytest.mark.asyncio
async def test_hashing_is_not_measured_when_de_duplication_is_off():
    result, _, _ = await run(one_post(timed(0.1, 0.2)), limit=1)
    assert "hash" not in result.timings.calls


@pytest.mark.asyncio
async def test_a_dry_run_measures_resolution_but_never_the_download():
    rex, _, _ = build([[harvested("a")]])
    result = await rex.extract("r/pics", dry_run=True, media_types=MediaType.ALL)
    assert "resolve" in result.timings.calls
    assert "fetch_wait" not in result.timings.calls
    assert "write" not in result.timings.calls


@pytest.mark.asyncio
async def test_a_retried_download_reports_the_time_all_its_attempts_cost():
    flaky = [
        FetchResult(ok=False, status=500, error="HTTP 500", wait=2.0),
        timed(0.5, 1.0),
    ]
    result, _, _ = await run(one_post(flaky), limit=1, max_retries=1, retry_backoff=0.0)
    assert result.media_saved == 1
    assert result.timings.seconds["fetch_wait"] == 2.5
    assert result.timings.seconds["fetch_body"] == 1.0


@pytest.mark.asyncio
async def test_a_failed_download_still_reports_what_it_cost():
    dead = FetchResult(ok=False, status=404, error="HTTP 404", wait=1.5)
    result, _, _ = await run(one_post(dead), limit=1)
    assert result.media_saved == 0
    assert result.timings.seconds["fetch_wait"] == 1.5


@pytest.mark.asyncio
async def test_the_breakdown_is_logged_when_the_run_is_verbose(caplog):
    with caplog.at_level(logging.INFO, logger="reddit_extract.core.extractor"):
        await run(one_post(timed(0.25, 1.75)), limit=1)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "where the time went" in logged
    assert "fetch_body" in logged


@pytest.mark.asyncio
async def test_nothing_is_logged_below_info(caplog):
    with caplog.at_level(logging.WARNING, logger="reddit_extract.core.extractor"):
        await run(one_post(timed(0.25, 1.75)), limit=1)
    assert "where the time went" not in caplog.text


@pytest.mark.asyncio
async def test_concurrent_downloads_are_flagged_in_the_log(caplog):
    with caplog.at_level(logging.INFO, logger="reddit_extract.core.extractor"):
        await run(one_post(timed(0.25, 1.75)), limit=1, download_concurrency=4)
    assert "overlap" in caplog.text


@pytest.mark.asyncio
async def test_the_run_report_carries_the_breakdown():
    result, _, _ = await run(one_post(timed(0.25, 1.75)), limit=1)
    document = result.to_report_dict()
    assert document["timings"]["seconds"]["fetch_body"] == 1.75
    assert document["timings"]["downloaded_bytes"] == len(b"imagebytes")
