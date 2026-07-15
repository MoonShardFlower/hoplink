"""Extraction results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .media import MediaItem
from .post import Post
from .source import Source


@dataclass
class ExtractionResult:
    """Outcome of extracting one source."""

    source: Source
    dry_run: bool = False
    posts_scanned: int = 0
    posts_matched: int = 0  #: posts a handler produced media (or metadata) for
    media_found: int = 0  #: candidates resolved (including known/existing)
    media_saved: int = 0  #: files actually written this run
    skipped_existing: int = 0  #: file already on disk
    skipped_known: int = 0  #: URL already recorded in the manifest
    failures: list[tuple[str, str]] = field(default_factory=list)  #: (url, reason)
    items: list[MediaItem] = field(default_factory=list)
    posts: list[Post] = field(default_factory=list)  #: the matched posts
    output_dir: str | None = None
    manifest_path: str | None = None
    error: str | None = None  #: set instead of raising in batch jobs
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    @property
    def key(self) -> str:
        """The source's storage key (subreddit name, ``u_name``, ``a+b``, ...)."""
        return self.source.key

    @property
    def ok(self) -> bool:
        """Whether the job finished without an error."""
        return self.error is None

    @property
    def duration(self) -> float | None:
        """Wall-clock duration in seconds, or None if the job hasn't finished."""
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def summary(self) -> str:
        """One-line human summary."""
        if self.error:
            return "{}: ERROR - {}".format(self.source.key, self.error)
        bits = [
            "{} posts".format(self.posts_scanned),
            "{} matched".format(self.posts_matched),
        ]
        if self.dry_run:
            bits.append("{} media found (dry run)".format(self.media_found))
        else:
            bits.append("{} saved".format(self.media_saved))
        if self.skipped_existing or self.skipped_known:
            bits.append(
                "{} already present".format(self.skipped_existing + self.skipped_known)
            )
        if self.failures:
            bits.append("{} failed".format(len(self.failures)))
        return "{}: {}".format(self.source.key, ", ".join(bits))
