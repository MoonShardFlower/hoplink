"""Tests for the extraction result: its summary line and its machine-readable report."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from reddit_extract.models.media import MediaItem, MediaType
from reddit_extract.models.post import Post
from reddit_extract.models.result import ExtractionResult
from reddit_extract.models.source import Subreddit

START = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def result(**overrides) -> ExtractionResult:
    """A finished result for r/pics."""
    fields = {"source": Subreddit("pics"), "started_at": START}
    fields.update(overrides)
    return ExtractionResult(**fields)


# -- identity ---------------------------------------------------------------


def test_the_key_comes_from_the_source():
    assert result().key == "pics"
    assert result(source=Subreddit("r/EarthPorn")).key == "EarthPorn"


def test_a_result_without_an_error_is_ok():
    assert result().ok is True


def test_a_result_with_an_error_is_not_ok():
    assert result(error="BrowserError: could not launch").ok is False


# -- duration ---------------------------------------------------------------


def test_an_unfinished_job_has_no_duration():
    assert result().duration is None


def test_duration_is_measured_in_seconds():
    assert result(finished_at=START + timedelta(seconds=90)).duration == 90.0


def test_duration_keeps_sub_second_precision():
    assert result(finished_at=START + timedelta(milliseconds=1500)).duration == 1.5


# -- summary ----------------------------------------------------------------


def test_a_plain_summary_reports_posts_and_saves():
    summary = result(posts_scanned=50, posts_matched=30, media_saved=42).summary()
    assert summary == "pics: 50 posts, 30 matched, 42 saved"


def test_a_failed_job_summarizes_as_its_error():
    # The counters are meaningless when the job never really ran.
    assert result(error="NoPostsFoundError: private").summary() == (
        "pics: ERROR - NoPostsFoundError: private"
    )


def test_a_dry_run_reports_what_it_found_rather_than_what_it_saved():
    summary = result(
        dry_run=True, posts_scanned=10, posts_matched=8, media_found=25
    ).summary()
    assert "25 media found (dry run)" in summary
    assert "saved" not in summary


def test_filtered_posts_appear_only_when_some_were_filtered():
    assert "2 filtered out" in result(posts_filtered=2).summary()
    assert "filtered out" not in result(posts_filtered=0).summary()


def test_already_present_pools_the_two_ways_a_file_can_be_known():
    # From the user's side "on disk" and "in the manifest" are the same non-event.
    assert "5 already present" in result(skipped_existing=2, skipped_known=3).summary()


def test_already_present_appears_when_either_counter_is_set():
    assert "2 already present" in result(skipped_existing=2).summary()
    assert "3 already present" in result(skipped_known=3).summary()
    assert "already present" not in result().summary()


def test_duplicates_appear_only_when_some_were_found():
    assert "4 duplicate" in result(skipped_duplicate=4).summary()
    assert "duplicate" not in result().summary()


def test_failures_are_counted_not_listed():
    failures = [
        ("https://i.redd.it/a.jpg", "HTTP 404"),
        ("https://i.redd.it/b.jpg", "is a gif"),
    ]
    assert "2 failed" in result(failures=failures).summary()
    assert "failed" not in result().summary()


def test_a_busy_summary_keeps_every_counter_in_order():
    summary = result(
        posts_scanned=100,
        posts_matched=60,
        posts_filtered=40,
        media_saved=50,
        skipped_existing=1,
        skipped_known=2,
        skipped_duplicate=3,
        failures=[("u", "HTTP 500")],
    ).summary()
    assert summary == (
        "pics: 100 posts, 60 matched, 40 filtered out, 50 saved, "
        "3 already present, 3 duplicate, 1 failed"
    )


# -- to_report_dict ---------------------------------------------------------


def test_a_report_carries_the_source_and_its_url():
    report = result().to_report_dict()
    assert report["source"] == "pics"
    assert report["url"] == "https://www.reddit.com/r/pics/new/"


def test_a_report_carries_every_counter():
    report = result(
        posts_scanned=10,
        posts_matched=8,
        posts_filtered=2,
        media_found=20,
        media_saved=18,
        skipped_existing=1,
        skipped_known=2,
        skipped_duplicate=3,
    ).to_report_dict()
    assert report["posts_scanned"] == 10
    assert report["posts_matched"] == 8
    assert report["posts_filtered"] == 2
    assert report["media_found"] == 20
    assert report["media_saved"] == 18
    assert report["skipped_existing"] == 1
    assert report["skipped_known"] == 2
    assert report["skipped_duplicate"] == 3


def test_failures_are_reported_as_url_and_reason_objects():
    report = result(failures=[("https://i.redd.it/a.jpg", "HTTP 404")]).to_report_dict()
    assert report["failures"] == [
        {"url": "https://i.redd.it/a.jpg", "reason": "HTTP 404"}
    ]


def test_items_are_reported_in_full():
    item = MediaItem(
        url="https://i.redd.it/a.jpg", media_type=MediaType.IMAGE, filename="0001.jpg"
    )
    report = result(items=[item]).to_report_dict()
    assert report["items"] == [item.to_report_dict()]


def test_a_report_carries_the_output_locations():
    report = result(
        output_dir="/out/pics", manifest_path="/out/pics/manifest.json"
    ).to_report_dict()
    assert report["output_dir"] == "/out/pics"
    assert report["manifest_path"] == "/out/pics/manifest.json"


def test_a_report_carries_the_error_and_the_ok_flag():
    report = result(error="BrowserError: boom").to_report_dict()
    assert report["ok"] is False
    assert report["error"] == "BrowserError: boom"


def test_a_report_times_the_run():
    report = result(finished_at=START + timedelta(seconds=30)).to_report_dict()
    assert report["started_at"] == "2026-07-16T12:00:00+00:00"
    assert report["finished_at"] == "2026-07-16T12:00:30+00:00"
    assert report["duration_seconds"] == 30.0


def test_an_unfinished_run_reports_no_finish_time_or_duration():
    report = result().to_report_dict()
    assert report["finished_at"] is None
    assert report["duration_seconds"] is None


def test_a_report_is_json_serializable():
    item = MediaItem(url="https://i.redd.it/a.jpg", media_type=MediaType.IMAGE)
    report = result(
        items=[item],
        failures=[("u", "HTTP 404")],
        finished_at=START + timedelta(seconds=1),
    ).to_report_dict()
    assert json.loads(json.dumps(report))["source"] == "pics"


def test_the_matched_posts_are_kept_but_stay_out_of_the_report():
    # They are for programmatic callers; the report is about files.
    matched = Post.from_harvest({"id": "a", "type": "image"})
    assert result(posts=[matched]).posts == [matched]
    assert "posts" not in result(posts=[matched]).to_report_dict()


# -- defaults ---------------------------------------------------------------


def test_a_fresh_result_counts_nothing_and_is_stamped():
    fresh = ExtractionResult(source=Subreddit("pics"))
    assert (fresh.posts_scanned, fresh.media_saved, fresh.posts_filtered) == (0, 0, 0)
    assert (fresh.items, fresh.posts, fresh.failures) == ([], [], [])
    assert fresh.dry_run is False
    assert fresh.started_at.tzinfo is timezone.utc


def test_results_do_not_share_their_mutable_defaults():
    first, second = ExtractionResult(source=Subreddit("a")), ExtractionResult(
        source=Subreddit("b")
    )
    first.items.append(MediaItem(url="u", media_type=MediaType.IMAGE))
    first.failures.append(("u", "boom"))
    assert second.items == [] and second.failures == []
