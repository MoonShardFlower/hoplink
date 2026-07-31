"""
Tests for what the CLI prints, what it writes, and how it exits.

Where `test_cli_options` covers the flags and how they map onto the library, this file covers the console output,
the ``--report`` file, and the exit codes. The runs swap the browser out, so no Playwright is launched.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

from reddit_extract import cli
from reddit_extract.exceptions import BrowserError
from reddit_extract.models.media import MediaItem, MediaType
from reddit_extract.models.result import ExtractionResult
from reddit_extract.models.source import Subreddit
from tests.test_extractor import FakeBrowser as ScriptedBrowser
from tests.test_extractor_filtering import FakeBrowser, harvested

START = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def result(**overrides) -> ExtractionResult:
    """A finished result for r/pics."""
    fields = {
        "source": Subreddit("pics"),
        "started_at": START,
        "finished_at": START + timedelta(seconds=5),
    }
    fields.update(overrides)
    return ExtractionResult(**fields)


def item(**overrides) -> MediaItem:
    fields = {
        "url": "https://i.redd.it/a.jpg",
        "media_type": MediaType.IMAGE,
        "filename": "0001.jpg",
        "post_url": "https://www.reddit.com/r/pics/comments/a/t/",
    }
    fields.update(overrides)
    return MediaItem(**fields)


@pytest.fixture
def fake_reddit(monkeypatch):
    """Make every extractor built during a run drive a FakeBrowser over canned posts."""
    browser = FakeBrowser([harvested("a"), harvested("b")])
    monkeypatch.setattr(
        "reddit_extract.core.extractor.BrowserManager", lambda cfg: browser
    )
    return browser


def out(capsys) -> str:
    return capsys.readouterr().out


# -- the progress printers --------------------------------------------------


def test_the_full_printer_set_is_wired_by_default():
    events = cli.make_events(quiet=False)
    assert events.on_job_start is not None
    assert events.on_scroll is not None
    assert events.on_harvested is not None
    assert events.on_media_saved is not None
    assert events.on_media_found is not None
    assert events.on_skip is not None
    assert events.on_job_end is not None


def test_quiet_keeps_only_the_end_summary():
    # -q is for batch runs where the per-file chatter would bury the result.
    events = cli.make_events(quiet=True)
    assert events.on_job_end is not None
    assert events.on_job_start is None
    assert events.on_scroll is None
    assert events.on_media_saved is None
    assert events.on_media_found is None
    assert events.on_skip is None


def test_the_job_start_printer_names_the_url(capsys):
    cli.make_events(False).on_job_start(Subreddit("pics"))
    assert "https://www.reddit.com/r/pics/new/" in out(capsys)


def test_the_scroll_printer_reports_the_running_total(capsys):
    cli.make_events(False).on_scroll(Subreddit("pics"), 25, 5)
    printed = out(capsys)
    assert "25 posts collected" in printed
    assert "no new this scroll" not in printed


def test_the_scroll_printer_flags_a_stale_scroll(capsys):
    # It explains why a run seems to stall near the end of a short listing.
    cli.make_events(False).on_scroll(Subreddit("pics"), 25, 0)
    assert "no new this scroll" in out(capsys)


def test_the_harvest_printer_reports_the_count(capsys):
    cli.make_events(False).on_harvested(Subreddit("pics"), 25)
    assert "Collected 25 posts" in out(capsys)


def test_the_saved_printer_maps_the_file_to_its_post(capsys):
    cli.make_events(False).on_media_saved(Subreddit("pics"), item())
    printed = out(capsys)
    assert "0001.jpg" in printed
    assert "https://www.reddit.com/r/pics/comments/a/t/" in printed


def test_the_found_printer_reports_the_url_a_dry_run_would_fetch(capsys):
    cli.make_events(False).on_media_found(Subreddit("pics"), item())
    printed = out(capsys)
    assert "0001.jpg" in printed
    assert "https://i.redd.it/a.jpg" in printed


def test_the_skip_printer_gives_the_reason(capsys):
    cli.make_events(False).on_skip(
        Subreddit("pics"), "https://i.redd.it/a.gif", "is a gif"
    )
    printed = out(capsys)
    assert "https://i.redd.it/a.gif" in printed
    assert "is a gif" in printed


# -- the end-of-job summary -------------------------------------------------


def test_the_summary_reports_the_headline_counters(capsys):
    cli.make_events(False).on_job_end(
        result(posts_scanned=50, posts_matched=30, media_saved=42)
    )
    printed = out(capsys)
    assert "[pics] done." in printed
    assert "posts scanned       : 50" in printed
    assert "posts matched       : 30" in printed
    assert "media saved (new)   : 42" in printed


def test_a_failed_job_prints_its_error_and_nothing_else(capsys):
    # The counters are meaningless when the job never really ran.
    cli.make_events(False).on_job_end(result(error="NoPostsFoundError: private"))
    printed = out(capsys)
    assert "[pics] FAILED: NoPostsFoundError: private" in printed
    assert "posts scanned" not in printed


def test_a_dry_run_summary_reports_what_would_be_downloaded(capsys):
    cli.make_events(False).on_job_end(result(dry_run=True, items=[item(), item()]))
    printed = out(capsys)
    assert "would download      : 2" in printed
    assert "media saved" not in printed


def test_a_dry_run_summary_omits_the_output_locations(capsys):
    # Nothing was written, so there is nowhere to point the user.
    cli.make_events(False).on_job_end(result(dry_run=True, output_dir="/out/pics"))
    printed = out(capsys)
    assert "output folder" not in printed
    assert "manifest" not in printed


def test_a_real_run_summary_points_at_the_output(capsys):
    cli.make_events(False).on_job_end(
        result(output_dir="/out/pics", manifest_path="/out/pics/manifest.json")
    )
    printed = out(capsys)
    assert "output folder       : /out/pics" in printed
    assert "manifest            : /out/pics/manifest.json" in printed


@pytest.mark.parametrize(
    "counter, label",
    [
        ("posts_filtered", "filtered out        : 3"),
        ("skipped_known", "already in manifest : 3"),
        ("skipped_existing", "already on disk     : 3"),
        ("skipped_duplicate", "duplicate content   : 3"),
    ],
)
def test_an_optional_counter_appears_only_when_it_is_non_zero(capsys, counter, label):
    cli.make_events(False).on_job_end(result(**{counter: 3}))
    assert label in out(capsys)

    cli.make_events(False).on_job_end(result(**{counter: 0}))
    assert label.split(":")[0].strip() not in out(capsys)


def test_failures_are_counted_in_the_summary(capsys):
    cli.make_events(False).on_job_end(
        result(failures=[("u1", "HTTP 404"), ("u2", "is a gif")])
    )
    assert "failed              : 2" in out(capsys)


def test_no_failures_means_no_failure_line(capsys):
    cli.make_events(False).on_job_end(result())
    assert "failed" not in out(capsys)


# -- write_report: JSON -----------------------------------------------------


def test_a_json_report_carries_per_source_detail(tmp_path):
    path = tmp_path / "report.json"
    cli.write_report(str(path), [result(posts_scanned=10, media_saved=8)])
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["sources"][0]["source"] == "pics"
    assert document["sources"][0]["posts_scanned"] == 10
    assert document["generated_at"].endswith("+00:00")


def test_a_json_report_totals_every_counter_across_sources(tmp_path):
    path = tmp_path / "report.json"
    cli.write_report(
        str(path),
        [
            result(
                posts_scanned=10,
                posts_matched=8,
                posts_filtered=1,
                media_found=9,
                media_saved=8,
                skipped_existing=1,
                skipped_known=2,
                skipped_duplicate=3,
                failures=[("u", "e")],
            ),
            result(
                source=Subreddit("art"),
                posts_scanned=5,
                posts_matched=4,
                posts_filtered=1,
                media_found=4,
                media_saved=3,
                skipped_existing=1,
                skipped_known=1,
                skipped_duplicate=1,
                error="boom",
            ),
        ],
    )
    totals = json.loads(path.read_text(encoding="utf-8"))["totals"]
    assert totals == {
        "sources": 2,
        "sources_failed": 1,
        "posts_scanned": 15,
        "posts_matched": 12,
        "posts_filtered": 2,
        "media_found": 13,
        "media_saved": 11,
        "skipped_existing": 2,
        "skipped_known": 3,
        "skipped_duplicate": 4,
        "failures": 1,
    }


def test_an_extension_other_than_csv_writes_json(tmp_path):
    path = tmp_path / "report.txt"
    cli.write_report(str(path), [result()])
    assert json.loads(path.read_text(encoding="utf-8"))["totals"]["sources"] == 1


def test_a_json_report_is_readable_utf8(tmp_path):
    path = tmp_path / "report.json"
    cli.write_report(str(path), [result(items=[item(title="ünïcode")])])
    text = path.read_text(encoding="utf-8")
    assert "ünïcode" in text
    assert "\n  " in text  # indented


def test_an_empty_run_still_writes_a_report(tmp_path):
    path = tmp_path / "report.json"
    cli.write_report(str(path), [])
    assert json.loads(path.read_text(encoding="utf-8"))["totals"]["sources"] == 0


# -- write_report: CSV ------------------------------------------------------


def test_a_csv_report_writes_one_row_per_source(tmp_path):
    path = tmp_path / "report.csv"
    cli.write_report(
        str(path), [result(posts_scanned=10), result(source=Subreddit("art"))]
    )
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert [r["source"] for r in rows] == ["pics", "art"]
    assert rows[0]["posts_scanned"] == "10"


def test_a_csv_report_is_chosen_by_extension_case_insensitively(tmp_path):
    path = tmp_path / "report.CSV"
    cli.write_report(str(path), [result()])
    assert path.read_text(encoding="utf-8").startswith("source,ok,error")


def test_a_csv_report_headers_every_column(tmp_path):
    path = tmp_path / "report.csv"
    cli.write_report(str(path), [result()])
    header = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))[0]
    assert tuple(header) == cli._REPORT_CSV_COLUMNS


def test_a_csv_report_counts_failures_rather_than_listing_them(tmp_path):
    path = tmp_path / "report.csv"
    cli.write_report(str(path), [result(failures=[("u1", "a"), ("u2", "b")])])
    row = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))[0]
    assert row["failures"] == "2"


def test_a_csv_report_blanks_an_absent_error_and_output_dir(tmp_path):
    path = tmp_path / "report.csv"
    cli.write_report(str(path), [result()])
    row = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))[0]
    assert row["error"] == ""
    assert row["output_dir"] == ""
    assert row["ok"] == "True"


def test_a_csv_report_records_a_failure(tmp_path):
    path = tmp_path / "report.csv"
    cli.write_report(str(path), [result(error="BrowserError: boom")])
    row = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))[0]
    assert row["ok"] == "False"
    assert row["error"] == "BrowserError: boom"


def test_a_csv_report_has_no_blank_lines_between_rows(tmp_path):
    # csv.writer emits \r\n itself; without newline="" the file would double-space on Windows.
    path = tmp_path / "report.csv"
    cli.write_report(str(path), [result(), result()])
    assert "\n\n" not in path.read_text(encoding="utf-8", newline="")


# -- main(): usage errors exit 2 --------------------------------------------


def test_no_sources_exits_with_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "no sources given" in capsys.readouterr().err


def test_an_unknown_media_type_exits_with_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["r/pics", "--types", "audio"])
    assert exc.value.code == 2
    assert "unknown media type" in capsys.readouterr().err


def test_a_config_file_naming_no_media_type_exits_with_usage(tmp_path, capsys):
    conf = tmp_path / "job.toml"
    conf.write_text('sources = ["r/pics"]\ntypes = []\n', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(conf)])
    assert exc.value.code == 2
    assert "--types must name at least one media type" in capsys.readouterr().err


def test_an_invalid_source_exits_with_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["r/has space"])
    assert exc.value.code == 2
    assert "invalid subreddit name" in capsys.readouterr().err


def test_a_missing_config_file_exits_with_usage(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(tmp_path / "nope.toml")])
    assert exc.value.code == 2
    assert "config file not found" in capsys.readouterr().err


def test_a_config_file_with_an_unknown_key_exits_with_usage(tmp_path, capsys):
    conf = tmp_path / "job.toml"
    conf.write_text('sources = ["r/pics"]\nnonsense = 1\n', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(conf)])
    assert exc.value.code == 2
    assert "unknown" in capsys.readouterr().err


# -- main(): runtime failures ------------------------------------------------


def test_a_browser_that_will_not_start_exits_one_with_a_clean_message(
    monkeypatch, capsys, tmp_path
):
    # No traceback: "playwright install chromium" is the user's actual next step.
    class DoomedBrowser:
        def __init__(self, cfg):
            pass

        async def start(self):
            raise BrowserError("could not launch Chromium: Executable doesn't exist")

        async def close(self):
            pass

    monkeypatch.setattr("reddit_extract.core.extractor.BrowserManager", DoomedBrowser)
    assert cli.main(["r/pics", "--out", str(tmp_path)]) == 1
    assert "error: could not launch Chromium" in capsys.readouterr().err


def test_a_keyboard_interrupt_exits_130(monkeypatch, capsys, tmp_path):
    # 130 is the conventional shell code for "terminated by Ctrl-C".
    class InterruptedExtractor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def iter_batch(self, *args, **kwargs):
            raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "RedditExtractor", InterruptedExtractor)
    assert cli.main(["r/pics", "--out", str(tmp_path)]) == 130
    assert "Interrupted." in capsys.readouterr().err


def test_a_failed_source_exits_one(monkeypatch, tmp_path):
    browser = ScriptedBrowser([[harvested("a")]], fail_urls=("pics",))
    monkeypatch.setattr(
        "reddit_extract.core.extractor.BrowserManager", lambda cfg: browser
    )
    assert (
        cli.main(["r/pics", "--limit", "1", "--out", str(tmp_path), "--delay", "0"])
        == 1
    )


def test_a_successful_run_exits_zero(fake_reddit, tmp_path):
    assert (
        cli.main(["r/pics", "--limit", "2", "--out", str(tmp_path), "--delay", "0"])
        == 0
    )


# -- main(): an unwritable report --------------------------------------------


def test_an_unwritable_report_exits_one(fake_reddit, tmp_path, capsys):
    # A directory can never be opened for writing, on any platform.
    report = tmp_path / "report-dir"
    report.mkdir()
    code = cli.main(
        [
            "r/pics",
            "--limit",
            "2",
            "--out",
            str(tmp_path / "o"),
            "--delay",
            "0",
            "--report",
            str(report),
        ]
    )
    assert code == 1
    assert "could not write report" in capsys.readouterr().err


def test_a_written_report_is_announced(fake_reddit, tmp_path, capsys):
    report = tmp_path / "report.json"
    cli.main(
        [
            "r/pics",
            "--limit",
            "2",
            "--out",
            str(tmp_path / "o"),
            "--delay",
            "0",
            "--report",
            str(report),
        ]
    )
    assert "Report written to {}".format(report) in out(capsys)


# -- main(): the batch tally --------------------------------------------------


def test_a_single_source_gets_no_batch_tally(fake_reddit, tmp_path, capsys):
    cli.main(["r/pics", "--limit", "2", "--out", str(tmp_path), "--delay", "0"])
    assert "Batch finished" not in out(capsys)


def test_several_sources_are_tallied(fake_reddit, tmp_path, capsys):
    cli.main(
        ["r/pics", "r/art", "--limit", "2", "--out", str(tmp_path), "--delay", "0"]
    )
    printed = out(capsys)
    assert "Batch finished: 2 sources, 4 new files." in printed
    assert "pics: 2 posts, 2 matched, 2 saved" in printed
    assert "art: 2 posts, 2 matched, 2 saved" in printed


def test_a_dry_run_batch_tallies_what_it_would_download(fake_reddit, tmp_path, capsys):
    cli.main(
        [
            "r/pics",
            "r/art",
            "--limit",
            "2",
            "--out",
            str(tmp_path),
            "--delay",
            "0",
            "--dry-run",
        ]
    )
    assert "Batch finished: 2 sources, 4 files would be downloaded." in out(capsys)


def test_a_batch_tally_flags_failed_sources(monkeypatch, tmp_path, capsys):
    browser = ScriptedBrowser([[harvested("a")]], fail_urls=("art",))
    monkeypatch.setattr(
        "reddit_extract.core.extractor.BrowserManager", lambda cfg: browser
    )
    code = cli.main(
        ["r/pics", "r/art", "--limit", "1", "--out", str(tmp_path), "--delay", "0"]
    )
    printed = out(capsys)
    assert code == 1
    assert "Batch finished: 2 sources, 1 new files, 1 source(s) FAILED." in printed
    assert "art: ERROR - RuntimeError: navigation failed" in printed
