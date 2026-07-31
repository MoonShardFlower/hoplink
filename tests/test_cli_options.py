"""Tests for the CLI's filter flags, retry flag, and logging setup."""

from __future__ import annotations

import json
import logging

import pytest

from reddit_extract import cli
from reddit_extract.config_file import EXTRACTOR_ONLY_KEYS
from tests.test_extractor_filtering import FakeBrowser, harvested


def parse(argv):
    """Parse ``argv`` with the real parser."""
    return cli.build_parser().parse_args(argv)


def filter_for(argv):
    """Build the PostFilter the CLI would hand the extractor for ``argv``."""
    return cli.build_post_filter(parse(["r/pics"] + argv))


def write_toml(tmp_path, text: str):
    path = tmp_path / "job.toml"
    path.write_text(text, encoding="utf-8")
    return path


# -- filter flags map onto PostFilter --------------------------------------


def test_no_filter_flags_gives_an_inactive_filter():
    assert filter_for([]).active is False


def test_min_score_and_min_comments():
    filt = filter_for(["--min-score", "500", "--min-comments", "10"])
    assert (filt.min_score, filt.min_comments) == (500, 10)


def test_title_flags():
    filt = filter_for(["--title-include", "cat", "--title-exclude", "meta"])
    assert filt.title_include == "cat"
    assert filt.title_exclude == "meta"
    assert filt.title_regex is False


def test_title_regex_flag():
    assert filter_for(["--title-include", "^x", "--title-regex"]).title_regex is True


def test_author_lists_split_on_commas():
    filt = filter_for(["--author", "Alice,Bob", "--block-author", "Spammer"])
    assert filt.authors == ("alice", "bob")
    assert filt.block_authors == ("spammer",)


def test_flair_list():
    assert filter_for(["--flair", "Politics,Software"]).flairs == (
        "politics",
        "software",
    )


def test_date_flags_parse_to_utc():
    filt = filter_for(["--after", "2026-01-01", "--before", "2027-01-01"])
    assert filt.after is not None and filt.after.year == 2026
    assert filt.before is not None and filt.before.year == 2027


def test_skip_stickied_and_min_gallery():
    filt = filter_for(["--skip-stickied", "--min-gallery", "3"])
    assert filt.skip_stickied is True
    assert filt.min_gallery == 3


# -- bad filter input exits with usage, not a traceback --------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--after", "last tuesday"],
        ["--title-include", "[unclosed", "--title-regex"],
        ["--min-score", "-5"],
        ["--after", "2027-01-01", "--before", "2026-01-01"],
    ],
)
def test_invalid_filter_arguments_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["r/pics"] + argv)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


# -- retries ---------------------------------------------------------------


def test_max_retries_defaults_to_two():
    assert parse(["r/pics"]).max_retries == 2


def test_max_retries_flag():
    assert parse(["r/pics", "--max-retries", "5"]).max_retries == 5


# -- redgifs scrape-all ----------------------------------------------------


def test_redgifs_scrape_all_defaults_off_and_flips_on():
    assert parse(["r/pics"]).redgifs_scrape_all is False
    assert parse(["r/pics", "--redgifs-scrape-all"]).redgifs_scrape_all is True


def test_redgifs_scrape_all_is_accepted_in_a_config_file(tmp_path):
    conf = write_toml(tmp_path, 'sources = ["r/gifs"]\nredgifs_scrape_all = true\n')
    parser = cli.build_parser()
    cli._load_cli_config(parser, ["--config", str(conf)])
    assert parser.parse_args(["--config", str(conf)]).redgifs_scrape_all is True


def test_redgifs_blacklist_defaults_none_and_takes_a_comma_list():
    assert parse(["r/pics"]).redgifs_blacklist is None
    assert parse(["r/pics", "--redgifs-blacklist", "Alice,Bob"]).redgifs_blacklist == (
        "Alice,Bob"  # raw string; ExtractorConfig splits and lower-cases it
    )


def test_redgifs_blacklist_is_accepted_as_a_list_in_a_config_file(tmp_path):
    conf = write_toml(
        tmp_path, 'sources = ["r/gifs"]\nredgifs_blacklist = ["Spammer", "AdBot"]\n'
    )
    parser = cli.build_parser()
    cli._load_cli_config(parser, ["--config", str(conf)])
    assert parser.parse_args(["--config", str(conf)]).redgifs_blacklist == [
        "Spammer",
        "AdBot",
    ]


def test_negative_max_retries_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["r/pics", "--max-retries", "-1"])
    assert exc.value.code == 2
    assert "max_retries must be >= 0" in capsys.readouterr().err


# -- log level resolution --------------------------------------------------


def test_logging_is_off_by_default():
    # Silence unless asked: the Events callbacks are the normal progress channel.
    assert cli.resolve_log_level(None, 0) is None


def test_verbose_maps_to_info_and_debug():
    assert cli.resolve_log_level(None, 1) == logging.INFO
    assert cli.resolve_log_level(None, 2) == logging.DEBUG
    assert cli.resolve_log_level(None, 5) == logging.DEBUG


def test_log_level_overrides_verbose():
    assert cli.resolve_log_level("warning", 2) == logging.WARNING


@pytest.mark.parametrize("name", list(cli.LOG_LEVELS))
def test_every_advertised_log_level_resolves(name):
    assert isinstance(cli.resolve_log_level(name, 0), int)


def test_verbose_counts():
    assert parse(["r/pics", "-vv"]).verbose == 2
    assert parse(["r/pics", "--verbose", "--verbose", "-v"]).verbose == 3


def test_bad_log_level_exits_2():
    with pytest.raises(SystemExit):
        parse(["r/pics", "--log-level", "chatty"])


# -- setup_logging ---------------------------------------------------------


@pytest.fixture
def pkg_logger():
    """The package logger, with its handlers/level restored afterwards."""
    logger = logging.getLogger(cli.PKG_LOGGER)
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    yield logger
    logger.handlers, logger.level, logger.propagate = handlers, level, propagate


def test_setup_logging_none_changes_nothing(pkg_logger):
    before = list(pkg_logger.handlers)
    cli.setup_logging(None)
    assert pkg_logger.handlers == before


def test_setup_logging_attaches_a_stderr_handler(pkg_logger):
    cli.setup_logging(logging.DEBUG)
    streams = [h for h in pkg_logger.handlers if isinstance(h, logging.StreamHandler)]
    assert len(streams) == 1
    assert pkg_logger.level == logging.DEBUG


def test_setup_logging_is_idempotent(pkg_logger):
    # main() may run more than once in a process; handlers must not stack up.
    cli.setup_logging(logging.INFO)
    cli.setup_logging(logging.INFO)
    streams = [h for h in pkg_logger.handlers if isinstance(h, logging.StreamHandler)]
    assert len(streams) == 1


def test_setup_logging_leaves_the_root_logger_alone(pkg_logger):
    root_before = list(logging.getLogger().handlers)
    cli.setup_logging(logging.DEBUG)
    assert logging.getLogger().handlers == root_before


def test_library_logger_has_a_null_handler():
    # Keeps the stdlib's "no handlers could be found" warning away from library users.
    import reddit_extract

    logger = logging.getLogger("reddit_extract")
    assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)
    assert reddit_extract.__version__


# -- filters and retries come free from the config file --------------------


def test_filter_keys_are_accepted_in_a_config_file(tmp_path):
    conf = write_toml(
        tmp_path,
        'sources = ["r/pics"]\n'
        "min_score = 500\n"
        'title-exclude = "meta"\n'
        'author = "alice,bob"\n'
        'after = "2026-01-01"\n'
        "skip_stickied = true\n"
        "max_retries = 4\n"
        "retry_backoff = 2.5\n",
    )
    parser = cli.build_parser()
    overrides = cli._load_cli_config(parser, ["--config", str(conf)])
    args = parser.parse_args(["--config", str(conf)])
    assert args.min_score == 500
    assert args.title_exclude == "meta"
    assert args.skip_stickied is True
    assert args.max_retries == 4
    # retry_backoff has no flag, so it rides along as an ExtractorConfig override
    assert overrides == {"retry_backoff": 2.5}

    filt = cli.build_post_filter(args)
    assert filt.min_score == 500
    assert filt.authors == ("alice", "bob")
    assert filt.after is not None


def test_cli_filter_flag_overrides_the_config_file(tmp_path):
    conf = write_toml(tmp_path, 'sources = ["r/pics"]\nmin_score = 500\n')
    parser = cli.build_parser()
    cli._load_cli_config(parser, ["--config", str(conf), "--min-score", "10"])
    args = parser.parse_args(["--config", str(conf), "--min-score", "10"])
    assert args.min_score == 10


def test_retry_backoff_has_no_cli_flag_so_it_stays_config_only():
    # EXTRACTOR_ONLY_KEYS are forwarded as **kwargs to ExtractorConfig.
    # An overlap with a CLI dest would raise TypeError for duplicate keyword arguments.
    dests = set(vars(cli.build_parser().parse_args([])))
    assert "retry_backoff" in EXTRACTOR_ONLY_KEYS
    assert not (dests & EXTRACTOR_ONLY_KEYS)


# -- a whole run through main(), with the browser swapped out ---------------


@pytest.fixture
def fake_reddit(monkeypatch):
    """Make every extractor built during a run drive a FakeBrowser over canned posts."""
    records = [
        harvested("a", score="1000", title="a cat"),
        harvested("b", score="10", title="a cat"),
        harvested("c", score="5000", title="[meta] rules"),
    ]
    browser = FakeBrowser(records)
    monkeypatch.setattr(
        "reddit_extract.core.extractor.BrowserManager", lambda cfg: browser
    )
    return browser


def test_main_applies_filters_to_a_real_run(fake_reddit, tmp_path, capsys):
    code = cli.main(
        [
            "r/pics",
            "--limit",
            "3",
            "--out",
            str(tmp_path / "out"),
            "--min-score",
            "500",
            "--title-exclude",
            "meta",
            "--delay",
            "0",
            "--report",
            str(tmp_path / "report.json"),
        ]
    )
    assert code == 0
    # only post "a" clears both the score floor and the title exclusion
    assert fake_reddit.fetched == ["https://i.redd.it/a.jpg"]
    assert "filtered out        : 2" in capsys.readouterr().out


def test_main_reports_filtered_posts(fake_reddit, tmp_path):
    report = tmp_path / "report.json"
    cli.main(
        [
            "r/pics",
            "--limit",
            "3",
            "--out",
            str(tmp_path / "out"),
            "--min-score",
            "500",
            "--delay",
            "0",
            "--report",
            str(report),
        ]
    )
    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["totals"]["posts_filtered"] == 1
    assert document["sources"][0]["posts_filtered"] == 1


def test_main_without_filters_keeps_everything(fake_reddit, tmp_path):
    code = cli.main(
        ["r/pics", "--limit", "3", "--out", str(tmp_path / "out"), "--delay", "0"]
    )
    assert code == 0
    assert len(fake_reddit.fetched) == 3


def test_main_accepts_redgifs_scrape_all(fake_reddit, tmp_path):
    # The canned posts are images, so the flag just opts video in and the run still succeeds.
    code = cli.main(
        [
            "r/pics",
            "--limit",
            "3",
            "--out",
            str(tmp_path / "out"),
            "--delay",
            "0",
            "--redgifs-scrape-all",
        ]
    )
    assert code == 0
    assert len(fake_reddit.fetched) == 3


def test_main_accepts_redgifs_blacklist(fake_reddit, tmp_path):
    # The canned posts are images, so the blacklist matches nothing; this just proves the
    # flag is wired through to ExtractorConfig (which normalizes it) without error.
    code = cli.main(
        [
            "r/pics",
            "--limit",
            "3",
            "--out",
            str(tmp_path / "out"),
            "--delay",
            "0",
            "--redgifs-blacklist",
            "Spammer,AdBot",
        ]
    )
    assert code == 0
    assert len(fake_reddit.fetched) == 3


def test_main_accepts_download_concurrency(fake_reddit, tmp_path):
    # The canned posts hold one image each, so the pool has nothing to overlap.
    code = cli.main(
        [
            "r/pics",
            "--limit",
            "3",
            "--out",
            str(tmp_path / "out"),
            "--delay",
            "0",
            "--download-concurrency",
            "4",
        ]
    )
    assert code == 0
    assert len(fake_reddit.fetched) == 3


def test_media_leaves_the_browser_by_default():
    assert parse(["r/pics"]).direct_download is True


def test_no_direct_download_forces_the_browser_route():
    assert parse(["r/pics", "--no-direct-download"]).direct_download is False


def test_direct_download_can_be_turned_off_from_a_config_file(
    fake_reddit, tmp_path, monkeypatch
):
    # It is on by default, so unlike the other booleans this one is the config file's to *disable*.
    seen = []

    def capture(cfg):
        seen.append(cfg)
        return fake_reddit

    monkeypatch.setattr("reddit_extract.core.extractor.BrowserManager", capture)
    conf = write_toml(
        tmp_path,
        'sources = ["r/pics"]\n'
        "limit = 3\n"
        "delay = 0.0\n"
        "direct_download = false\n"
        'out = "{}"\n'.format((tmp_path / "out").as_posix()),
    )
    assert cli.main(["--config", str(conf)]) == 0
    assert seen[0].direct_download is False


def test_api_pause_reaches_the_config_from_a_config_file(
    fake_reddit, tmp_path, monkeypatch
):
    # It has no CLI flag, so the config file is the only way to tune it.
    seen = []

    def capture(cfg):
        seen.append(cfg)
        return fake_reddit

    monkeypatch.setattr("reddit_extract.core.extractor.BrowserManager", capture)
    conf = write_toml(
        tmp_path,
        'sources = ["r/pics"]\n'
        "limit = 3\n"
        "delay = 0.0\n"
        "api_pause = 0.25\n"
        'out = "{}"\n'.format((tmp_path / "out").as_posix()),
    )
    assert cli.main(["--config", str(conf)]) == 0
    assert seen[0].api_pause == 0.25


def test_main_rejects_a_download_concurrency_below_one(fake_reddit, tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["r/pics", "--out", str(tmp_path), "--download-concurrency", "0"])
    assert "download_concurrency must be >= 1" in capsys.readouterr().err


def test_main_filters_from_a_config_file(fake_reddit, tmp_path):
    conf = write_toml(
        tmp_path,
        'sources = ["r/pics"]\n'
        "limit = 3\n"
        "min_score = 500\n"
        'title-exclude = "meta"\n'
        "delay = 0.0\n"
        'out = "{}"\n'.format((tmp_path / "out").as_posix()),
    )
    assert cli.main(["--config", str(conf)]) == 0
    assert fake_reddit.fetched == ["https://i.redd.it/a.jpg"]
