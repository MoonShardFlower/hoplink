"""Command-line interface."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, List

from . import __version__
from .config_file import EXTRACTOR_ONLY_KEYS, load_config_file
from .core.sync import RedditExtractor
from .events import Events
from .exceptions import ConfigFileError, RedditExtractError
from .models.config import DEFAULT_FORMATS, DEFAULT_UA, ExtractorConfig
from .models.filters import PostFilter
from .models.media import MediaType
from .models.result import ExtractionResult
from .models.source import Source, parse_source

#: Levels accepted by --log-level, lowest to highest.
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")

#: The package logger the CLI attaches its handler to.
PKG_LOGGER = __name__.split(".")[0]


def _add_filter_arguments(p: argparse.ArgumentParser) -> None:
    """
    Add the post-filtering options to ``p``, as their own help group.

    Every option's argparse destination doubles as its config-file key, so these need no separate config plumbing.
    See `build_post_filter`, which turns them into a `PostFilter`.
    """
    g = p.add_argument_group(
        "post filters",
        "Narrow which harvested posts are kept. A post must satisfy every filter given. "
        "Posts rejected here cost no page visits and no downloads.",
    )
    g.add_argument(
        "--min-score",
        type=int,
        metavar="N",
        default=None,
        help="Keep posts scoring >= N.",
    )
    g.add_argument(
        "--min-comments",
        type=int,
        metavar="N",
        default=None,
        help="Keep posts with >= N comments.",
    )
    g.add_argument(
        "--title-include",
        metavar="TEXT",
        default=None,
        help="Keep only posts whose title contains TEXT (case-insensitive).",
    )
    g.add_argument(
        "--title-exclude",
        metavar="TEXT",
        default=None,
        help="Drop posts whose title contains TEXT (case-insensitive).",
    )
    g.add_argument(
        "--title-regex",
        action="store_true",
        help="Treat --title-include/--title-exclude as regular expressions instead of plain substrings.",
    )
    g.add_argument(
        "--author",
        metavar="NAMES",
        default=None,
        help="Comma-separated allow list of authors to keep.",
    )
    g.add_argument(
        "--block-author",
        metavar="NAMES",
        default=None,
        help="Comma-separated deny list of authors to drop (wins over --author).",
    )
    g.add_argument(
        "--flair",
        metavar="TEXTS",
        default=None,
        help="Comma-separated link flairs to keep (case-insensitive, exact match). A single flair on a subreddit is filtered by Reddit itself, so --limit counts matching posts.",
    )
    g.add_argument(
        "--min-gallery",
        type=int,
        metavar="N",
        default=None,
        help="Keep gallery posts only when they hold >= N images. Other post types are unaffected.",
    )
    g.add_argument(
        "--after",
        metavar="DATE",
        default=None,
        help="Keep posts created at or after DATE (ISO-8601, e.g. 2026-01-01).",
    )
    g.add_argument(
        "--before",
        metavar="DATE",
        default=None,
        help="Keep posts created before DATE (ISO-8601).",
    )
    g.add_argument(
        "--skip-stickied", action="store_true", help="Drop stickied/pinned posts."
    )


def build_post_filter(args: argparse.Namespace) -> PostFilter:
    """
    Build a `PostFilter` from parsed arguments.

    Args:
        args: The parsed namespace (from the parser `build_parser` returns).

    Returns:
        The assembled filter; check ``.active`` to tell a real filter from a no-op one.

    Raises:
        ValueError: If a date is not ISO-8601, a title regex is invalid, or a count is negative.
    """
    return PostFilter(
        min_score=args.min_score,
        min_comments=args.min_comments,
        title_include=args.title_include,
        title_exclude=args.title_exclude,
        title_regex=args.title_regex,
        authors=args.author,
        block_authors=args.block_author,
        flairs=args.flair,
        min_gallery=args.min_gallery,
        after=args.after,
        before=args.before,
        skip_stickied=args.skip_stickied,
    )


def resolve_log_level(log_level: str | None, verbose: int) -> int | None:
    """
    Decide the logging level for a run.

    Args:
        log_level: An explicit ``--log-level`` name, which wins when given.
        verbose: How many times ``-v`` was passed: 1 for info, 2 or more for debug.

    Returns:
        The level as a ``logging`` constant, or None to leave logging untouched (the default,
        which keeps the library quiet and lets the Events callbacks do the reporting).
    """
    if log_level:
        level = getattr(logging, log_level.upper(), None)
        if isinstance(level, int):
            return level
    if verbose >= 2:
        return logging.DEBUG
    if verbose == 1:
        return logging.INFO
    return None


def setup_logging(level: int | None) -> None:
    """
    Send the library's log records to stderr at ``level``.

    Only the ``reddit_extract`` logger is touched, not the root one, so importing the CLI doesn't hijack logging for a
    host application. stderr keeps the records out of the progress output on stdout.

    Args:
        level: The level to set, or None to do nothing at all.
    """
    if level is None:
        return
    logger = logging.getLogger(PKG_LOGGER)
    logger.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%H:%M:%S"
            )
        )
        logger.addHandler(handler)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``reddit-extract`` argument parser."""
    p = argparse.ArgumentParser(
        prog="reddit-extract",
        description="Download media from Reddit via a real browser (resilient against Reddit's HTTP API blocks).",
    )
    p.add_argument(
        "sources",
        nargs="*",
        help="One or more sources: subreddit URL, r/name, bare name, u/username, or a+b+c multireddit. "
        "May be omitted when supplied via --config.",
    )
    p.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help="Load options from a TOML config file. Explicit command-line arguments override it.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max posts to scan per source (default: 100).",
    )
    p.add_argument(
        "--sort",
        choices=["new", "hot", "top", "rising", "controversial"],
        default="new",
        help="Listing sort (default: new). 'rising' is subreddit-only, 'controversial' is user-page-only.",
    )
    p.add_argument(
        "--time",
        dest="time_filter",
        default="all",
        choices=["hour", "day", "week", "month", "year", "all"],
        help="Time window for --sort top/controversial (default: all).",
    )
    p.add_argument(
        "--types",
        default="image,gallery",
        help="Comma-separated media types: image, gallery, video, text, link, poll, crosspost, "
        "or 'all' (default: image,gallery). text/poll are saved as .md documents.",
    )
    p.add_argument(
        "--out", default="downloads", help="Base output directory (default: downloads)."
    )
    p.add_argument(
        "--report",
        metavar="PATH",
        default=None,
        help="Write a run report to PATH. '.csv' gives one summary row per source; "
        "any other extension gives a full JSON document.",
    )
    p.add_argument(
        "--formats",
        default=",".join(DEFAULT_FORMATS),
        help="Comma-separated image extensions to keep (default: %(default)s).",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Resolve media but download nothing."
    )
    p.add_argument(
        "--dedupe",
        action="store_true",
        help="Skip a download whose content hash matches a file already saved for the source (catches reposts).",
    )
    p.add_argument(
        "--redgifs-scrape-all",
        action="store_true",
        help="For each RedGIFs post, scrape the uploader's entire RedGIFs profile, not just the "
        "linked clip. Each profile is scraped at most once per run. Implies --types video.",
    )
    p.add_argument(
        "--redgifs-blacklist",
        metavar="NAMES",
        default=None,
        help="Comma-separated RedGIFs uploader names never to download (case-insensitive). A post "
        "whose RedGIFs uploader is listed here is skipped, in both single-clip and scrape-all mode.",
    )
    _add_filter_arguments(p)
    p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Sources scraped in parallel (default: 1).",
    )
    p.add_argument(
        "--scroll-pause",
        type=float,
        default=2.0,
        help="Seconds between feed scrolls (default: 2.0).",
    )
    p.add_argument(
        "--img-delay",
        type=float,
        default=0.5,
        help="Seconds between downloads (default: 0.5).",
    )
    p.add_argument(
        "--max-stale-scrolls",
        type=int,
        default=10,
        help="Stop after this many scrolls with no new posts (default: 10).",
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=2,
        metavar="N",
        help="Extra attempts for a download that fails transiently, e.g. a CDN timeout or 5xx "
        "(default: 2; 0 disables retrying).",
    )
    p.add_argument(
        "--show", action="store_true", help="Show the browser window (headful)."
    )
    p.add_argument(
        "--profile",
        default=None,
        help="Persistent browser profile dir (stay logged in).",
    )
    p.add_argument("--user-agent", default=DEFAULT_UA, help="Custom User-Agent.")
    p.add_argument(
        "-q", "--quiet", action="store_true", help="Only print per-source summaries."
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Log the library's diagnostics to stderr: -v for info (shows download retries), -vv for debug.",
    )
    p.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default=None,
        help="Log at an explicit level; overrides --verbose.",
    )
    p.add_argument(
        "--version", action="version", version="reddit-extract {}".format(__version__)
    )
    return p


def make_events(quiet: bool) -> Events:
    """
    Build console-printing progress callbacks.

    Args:
        quiet: If True, wire only the per-source end summary; otherwise wire the full set of progress callbacks.

    Returns:
        The Events bundle to hand to the extractor.
    """

    def on_job_start(source: Source) -> None:
        print("Opening {} ...".format(source.url))

    def on_scroll(source: Source, total: int, new: int) -> None:
        print(
            "  ...{} posts collected{}".format(
                total, " (no new this scroll)" if new == 0 else ""
            )
        )

    def on_harvested(source: Source, count: int) -> None:
        print("\nCollected {} posts. Resolving media ...\n".format(count))

    def on_media_saved(source: Source, item: Any) -> None:
        print("  saved {:<16} <- {}".format(item.filename, item.post_url))

    def on_media_found(source: Source, item: Any) -> None:
        print("  found {:<16} {}".format(item.filename, item.url))

    def on_skip(source: Source, url: str, reason: str) -> None:
        print("  skip  {} ({})".format(url, reason))

    def on_job_end(result: ExtractionResult) -> None:
        print()
        if result.error:
            print("[{}] FAILED: {}".format(result.source.key, result.error))
            return
        print("[{}] done.".format(result.source.key))
        print("  posts scanned       : {}".format(result.posts_scanned))
        print("  posts matched       : {}".format(result.posts_matched))
        if result.posts_filtered:
            print("  filtered out        : {}".format(result.posts_filtered))
        if result.dry_run:
            print("  would download      : {}".format(len(result.items)))
        else:
            print("  media saved (new)   : {}".format(result.media_saved))
        if result.skipped_known:
            print("  already in manifest : {}".format(result.skipped_known))
        if result.skipped_existing:
            print("  already on disk     : {}".format(result.skipped_existing))
        if result.skipped_duplicate:
            print("  duplicate content   : {}".format(result.skipped_duplicate))
        if result.failures:
            print("  failed              : {}".format(len(result.failures)))
        if not result.dry_run:
            print("  output folder       : {}".format(result.output_dir))
            print("  manifest            : {}".format(result.manifest_path))
        print()

    if quiet:
        return Events(on_job_end=on_job_end)
    return Events(
        on_job_start=on_job_start,
        on_scroll=on_scroll,
        on_harvested=on_harvested,
        on_media_saved=on_media_saved,
        on_media_found=on_media_found,
        on_skip=on_skip,
        on_job_end=on_job_end,
    )


def _load_cli_config(
    parser: argparse.ArgumentParser, argv: List[str] | None
) -> dict[str, Any]:
    """
    Seed ``parser`` defaults from a ``--config`` TOML file, if one was given.

    The file's values that correspond to CLI options become parser defaults (so anything typed on the command line still
    overrides them); values naming advanced ``ExtractorConfig`` fields are returned for the caller to forward.

    Args:
        parser: The fully built argument parser; seeded in place via ``set_defaults``.
        argv: The raw argument list (``None`` means ``sys.argv``).

    Returns:
        The advanced ``ExtractorConfig`` overrides from the file (empty when no ``--config`` was given).
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("-c", "--config")
    pre_args, _ = pre.parse_known_args(argv)
    if not pre_args.config:
        return {}
    # Destinations argparse knows about. Parsing an empty list is side-effect-free
    # (sources is nargs="*"), and version/help use SUPPRESS dests so they're absent.
    valid_cli_keys = set(vars(parser.parse_args([]))) - {"config"}
    try:
        file_cfg = load_config_file(pre_args.config, valid_cli_keys=valid_cli_keys)
    except ConfigFileError as exc:
        parser.error(str(exc))  # prints usage and exits with status 2
    parser.set_defaults(**{k: v for k, v in file_cfg.items() if k in valid_cli_keys})
    return {k: v for k, v in file_cfg.items() if k in EXTRACTOR_ONLY_KEYS}


_REPORT_CSV_COLUMNS = (
    "source",
    "ok",
    "error",
    "dry_run",
    "posts_scanned",
    "posts_matched",
    "posts_filtered",
    "media_found",
    "media_saved",
    "skipped_existing",
    "skipped_known",
    "skipped_duplicate",
    "failures",
    "output_dir",
)


def write_report(path: str, results: List[ExtractionResult]) -> None:
    """
    Write a machine-readable report of a run.

    The format is chosen from ``path``'s extension: ``.csv`` writes one summary row per source; anything else writes a
    structured JSON document with per-source and per-item detail plus run totals.

    Args:
        path: Destination file path.
        results: The results collected from the run.

    Raises:
        OSError: If the file cannot be written.
    """
    if path.lower().endswith(".csv"):
        _write_report_csv(path, results)
    else:
        _write_report_json(path, results)


def _write_report_json(path: str, results: List[ExtractionResult]) -> None:
    """Write the full run as an indented JSON document with a totals block."""
    document = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "sources": len(results),
            "sources_failed": sum(1 for r in results if r.error),
            "posts_scanned": sum(r.posts_scanned for r in results),
            "posts_matched": sum(r.posts_matched for r in results),
            "posts_filtered": sum(r.posts_filtered for r in results),
            "media_found": sum(r.media_found for r in results),
            "media_saved": sum(r.media_saved for r in results),
            "skipped_existing": sum(r.skipped_existing for r in results),
            "skipped_known": sum(r.skipped_known for r in results),
            "skipped_duplicate": sum(r.skipped_duplicate for r in results),
            "failures": sum(len(r.failures) for r in results),
        },
        "sources": [r.to_report_dict() for r in results],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2, ensure_ascii=False)


def _write_report_csv(path: str, results: List[ExtractionResult]) -> None:
    """Write one per-source summary row (see ``_REPORT_CSV_COLUMNS``)."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_REPORT_CSV_COLUMNS)
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "source": r.source.key,
                    "ok": r.ok,
                    "error": r.error or "",
                    "dry_run": r.dry_run,
                    "posts_scanned": r.posts_scanned,
                    "posts_matched": r.posts_matched,
                    "posts_filtered": r.posts_filtered,
                    "media_found": r.media_found,
                    "media_saved": r.media_saved,
                    "skipped_existing": r.skipped_existing,
                    "skipped_known": r.skipped_known,
                    "skipped_duplicate": r.skipped_duplicate,
                    "failures": len(r.failures),
                    "output_dir": r.output_dir or "",
                }
            )


def main(argv: List[str] | None = None) -> int:
    """
    Run the command-line interface.

    Args:
        argv: Argument list to parse; defaults to ``sys.argv`` when None.

    Returns:
        A process exit code: 0 on success, 1 if any source failed, 130 on keyboard interrupt.
    """
    parser = build_parser()
    extractor_overrides = _load_cli_config(parser, argv)
    args = parser.parse_args(argv)
    setup_logging(resolve_log_level(args.log_level, args.verbose))

    if not args.sources:
        parser.error(
            "no sources given (provide them on the command line or via --config)"
        )

    # coerce accepts a comma string ("image,gallery") from the CLI or a list from a config file.
    try:
        media_types = MediaType.coerce(args.types)
    except ValueError as exc:
        parser.error(str(exc))
    if not media_types:
        parser.error("--types must name at least one media type")
    # RedGIFs clips are video; scraping profiles is pointless if video isn't wanted, so opt it in.
    if args.redgifs_scrape_all:
        media_types |= MediaType.VIDEO

    try:
        post_filter = build_post_filter(args)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        sources = [
            parse_source(
                text, sort=args.sort, time_filter=args.time_filter, limit=args.limit
            )
            for text in args.sources
        ]
    except ValueError as exc:
        parser.error(str(exc))

    try:
        config = ExtractorConfig(
            formats=args.formats,
            headless=not args.show,
            profile_dir=args.profile,
            user_agent=args.user_agent,
            output_dir=args.out,
            scroll_pause=args.scroll_pause,
            img_delay=args.img_delay,
            max_stale_scrolls=args.max_stale_scrolls,
            max_retries=args.max_retries,
            dedupe_by_hash=args.dedupe,
            redgifs_scrape_all=args.redgifs_scrape_all,
            redgifs_blacklist=args.redgifs_blacklist,
            # Advanced knobs a config file may set (viewport, locale, timeouts, ...).
            # Empty for a plain command line. None overlaps the keywords above.
            **extractor_overrides,
        )
    except ValueError as exc:
        parser.error(str(exc))

    results: List[ExtractionResult] = []
    try:
        with RedditExtractor(
            config, events=make_events(args.quiet), post_filter=post_filter
        ) as rex:
            for result in rex.iter_batch(
                sources,
                media_types=media_types,
                dry_run=args.dry_run,
                concurrency=args.concurrency,
            ):
                results.append(result)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except RedditExtractError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1

    if len(results) > 1:
        if args.dry_run:
            tally = "{} files would be downloaded".format(
                sum(len(r.items) for r in results)
            )
        else:
            tally = "{} new files".format(sum(r.media_saved for r in results))
        failed = [r for r in results if r.error]
        print(
            "Batch finished: {} sources, {}{}.".format(
                len(results),
                tally,
                ", {} source(s) FAILED".format(len(failed)) if failed else "",
            )
        )
        for r in results:
            print("  " + r.summary())

    if args.report:
        try:
            write_report(args.report, results)
        except OSError as exc:
            print(
                "error: could not write report {}: {}".format(args.report, exc),
                file=sys.stderr,
            )
            return 1
        print("Report written to {}".format(args.report))

    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
