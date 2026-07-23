"""Tests for the TOML config-file loader and its CLI merge precedence."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reddit_extract import cli
from reddit_extract.config_file import EXTRACTOR_ONLY_KEYS, load_config_file
from reddit_extract.exceptions import ConfigFileError


def valid_cli_keys() -> set[str]:
    """The argparse destinations a config file may set (minus ``config``)."""
    return set(vars(cli.build_parser().parse_args([]))) - {"config"}


def write_toml(tmp_path, text: str):
    """Write ``text`` to a temp ``job.toml`` and return its path."""
    path = tmp_path / "job.toml"
    path.write_text(text, encoding="utf-8")
    return path


# -- loader normalization & coercions --------------------------------------


def test_sources_string_becomes_list(tmp_path):
    cfg = load_config_file(
        write_toml(tmp_path, 'sources = "r/EarthPorn"\n'),
        valid_cli_keys=valid_cli_keys(),
    )
    assert cfg["sources"] == ["r/EarthPorn"]


def test_sources_list_preserved(tmp_path):
    cfg = load_config_file(
        write_toml(tmp_path, 'sources = ["r/a", "r/b"]\n'),
        valid_cli_keys=valid_cli_keys(),
    )
    assert cfg["sources"] == ["r/a", "r/b"]


def test_time_aliases_to_time_filter(tmp_path):
    cfg = load_config_file(
        write_toml(tmp_path, 'time = "month"\n'),
        valid_cli_keys=valid_cli_keys(),
    )
    assert cfg["time_filter"] == "month"
    assert "time" not in cfg


def test_dashes_normalize_to_underscores(tmp_path):
    cfg = load_config_file(
        write_toml(tmp_path, "scroll-pause = 3.0\n"),
        valid_cli_keys=valid_cli_keys(),
    )
    assert cfg["scroll_pause"] == 3.0


def test_viewport_list_becomes_tuple(tmp_path):
    cfg = load_config_file(
        write_toml(tmp_path, "viewport = [1366, 900]\n"),
        valid_cli_keys=valid_cli_keys(),
    )
    assert cfg["viewport"] == (1366, 900)


def test_extractor_only_key_kept(tmp_path):
    cfg = load_config_file(
        write_toml(tmp_path, "nav_timeout_ms = 90000\n"),
        valid_cli_keys=valid_cli_keys(),
    )
    assert cfg["nav_timeout_ms"] == 90000
    assert "nav_timeout_ms" in EXTRACTOR_ONLY_KEYS


# -- loader error handling -------------------------------------------------


def test_unknown_key_raises(tmp_path):
    with pytest.raises(ConfigFileError, match="limytt"):
        load_config_file(
            write_toml(tmp_path, "limytt = 5\n"),
            valid_cli_keys=valid_cli_keys(),
        )


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigFileError, match="not found"):
        load_config_file(tmp_path / "nope.toml", valid_cli_keys=valid_cli_keys())


def test_invalid_toml_raises(tmp_path):
    with pytest.raises(ConfigFileError, match="invalid TOML"):
        load_config_file(
            write_toml(tmp_path, "sources = [\n"),
            valid_cli_keys=valid_cli_keys(),
        )


def test_a_directory_instead_of_a_file_raises(tmp_path):
    # POSIX reports IsADirectoryError here and Windows a PermissionError; either way the
    # user gets a ConfigFileError naming the path, not a traceback.
    with pytest.raises(ConfigFileError, match=re.escape(str(tmp_path))):
        load_config_file(tmp_path, valid_cli_keys=valid_cli_keys())


def test_an_unreadable_file_raises(tmp_path, monkeypatch):
    path = write_toml(tmp_path, 'sources = ["r/pics"]\n')

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ConfigFileError, match="could not read config file"):
        load_config_file(path, valid_cli_keys=valid_cli_keys())


def test_aliased_duplicate_raises(tmp_path):
    with pytest.raises(ConfigFileError, match="conflicts"):
        load_config_file(
            write_toml(tmp_path, 'time = "day"\ntime_filter = "week"\n'),
            valid_cli_keys=valid_cli_keys(),
        )


def test_non_string_sources_raises(tmp_path):
    with pytest.raises(ConfigFileError, match="string or a list"):
        load_config_file(
            write_toml(tmp_path, "sources = 5\n"),
            valid_cli_keys=valid_cli_keys(),
        )


# -- CLI merge precedence: CLI arg > config file > default -----------------


def parse(argv):
    """Build the parser, apply any --config, and parse ``argv``."""
    parser = cli.build_parser()
    overrides = cli._load_cli_config(parser, argv)
    return parser.parse_args(argv), overrides


def test_config_supplies_values(tmp_path):
    conf = write_toml(
        tmp_path,
        'sources = ["r/EarthPorn"]\n'
        "limit = 50\n"
        'sort = "top"\n'
        'time = "month"\n'
        'out = "mydownloads"\n'
        "viewport = [800, 600]\n",
    )
    args, overrides = parse(["--config", str(conf)])
    assert args.sources == ["r/EarthPorn"]
    assert args.limit == 50
    assert args.time_filter == "month"
    assert args.out == "mydownloads"
    assert overrides["viewport"] == (800, 600)


def test_cli_overrides_config(tmp_path):
    conf = write_toml(tmp_path, 'sources = ["r/a"]\nlimit = 50\nout = "fromfile"\n')
    args, _ = parse(["--config", str(conf), "--limit", "5", "r/pics"])
    assert args.limit == 5  # CLI wins
    assert args.sources == ["r/pics"]  # CLI wins
    assert args.out == "fromfile"  # untouched -> from file


def test_no_config_uses_defaults():
    args, overrides = parse(["r/pics", "--limit", "7"])
    assert args.limit == 7
    assert args.sources == ["r/pics"]
    assert args.sort == "new"
    assert args.out == "downloads"
    assert overrides == {}


def test_bare_toml_dates_build_a_filter(tmp_path):
    # Unquoted dates are the natural TOML spelling, and tomllib hands them over as
    # `date` objects rather than the strings the CLI's own flags produce.
    conf = write_toml(
        tmp_path,
        'sources = ["r/pics"]\nafter = 2026-01-01\nbefore = 2026-07-01\n',
    )
    args, _ = parse(["--config", str(conf)])
    post_filter = cli.build_post_filter(args)
    assert post_filter.after == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert post_filter.before == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_quoted_and_bare_toml_dates_agree(tmp_path):
    """The two spellings a config file allows must land on the same instant."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    bare, _ = parse(["--config", str(write_toml(a, "after = 2026-01-01\n"))])
    quoted, _ = parse(["--config", str(write_toml(b, 'after = "2026-01-01"\n'))])
    assert cli.build_post_filter(bare).after == cli.build_post_filter(quoted).after
