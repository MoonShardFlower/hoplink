"""
Wall-clock accounting for individual job phases.

This module defines the `Timings` class, which accumulates elapsed seconds and call counts for phases like fetching,
hashing, and writing. The breakdown helps identify bottlenecks (CDN latency, drive transfer, delays).

Because the pipeline is asynchronous, measurements include time spent suspended on awaits. Caveats:
- With download concurrency >1, fetch and pace phases overlap, so their summed seconds can exceed the job's wall-clock duration.
- With multiple jobs in a batch, each phase also includes time spent on other jobs due to event-loop switching.

The `lines()` method flags these cases in the logged summary
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

#: Recorded phases, in the order a file passes through them (also the display order).
PHASES = (
    "harvest",
    "resolve",
    "fetch_wait",
    "fetch_body",
    "pace",
    "hash",
    "write",
    "manifest",
)

#: What each phase covers, spelled out in the logged breakdown.
PHASE_LABELS = {
    "harvest": "scrolling the listing",
    "resolve": "resolving posts to media (RedGIFs profile paging lands here)",
    "fetch_wait": "fetching responses into the browser",
    "fetch_body": "copying out of Playwright into Python",
    "pace": "configured delay between downloads",
    "hash": "hashing bodies to de-duplicate",
    "write": "writing files to storage",
    "manifest": "rewriting manifest.json",
}


def human_bytes(count: int) -> str:
    """Format a byte count for a log line (``1.4 GB``, ``812.0 MB``, ``37 B``)."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return (
                "{:.0f} {}".format(size, unit)
                if unit == "B"
                else "{:.1f} {}".format(size, unit)
            )
        size /= 1024
    return "{:.1f} GB".format(size)  # pragma: no cover


@dataclass
class Timings:
    """Seconds and call counts accumulated per phase of one job."""

    seconds: Dict[str, float] = field(default_factory=dict)
    calls: Dict[str, int] = field(default_factory=dict)
    #: bytes of every response body that arrived, successful downloads or not
    downloaded_bytes: int = 0

    def record(self, phase: str, elapsed: float) -> None:
        """
        Add one measurement to a phase.

        Args:
            phase: A name from `PHASES` (anything else still accumulates, but is not displayed).
            elapsed: Seconds it took. Negative values are clamped to zero rather than credited back.
        """
        self.seconds[phase] = self.seconds.get(phase, 0.0) + max(0.0, elapsed)
        self.calls[phase] = self.calls.get(phase, 0) + 1

    @contextmanager
    def measure(self, phase: str) -> Iterator[None]:
        """
        Time a block and record it against ``phase``.

        Safe to wrap an ``await``: the measurement is wall-clock, so it counts the time the awaited work was suspended too
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(phase, time.perf_counter() - start)

    @property
    def total(self) -> float:
        """Seconds across every recorded phase (may exceed wall clock when phases overlap)."""
        return sum(self.seconds.values())

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the run report, in pipeline order and rounded to milliseconds."""
        return {
            "seconds": {
                phase: round(self.seconds[phase], 3)
                for phase in PHASES
                if phase in self.seconds
            },
            "calls": {
                phase: self.calls[phase] for phase in PHASES if phase in self.calls
            },
            "downloaded_bytes": self.downloaded_bytes,
        }

    def lines(
        self, *, wall: Optional[float] = None, overlapped: bool = False
    ) -> List[str]:
        """
        Render the breakdown, slowest phase first within pipeline order.

        Args:
            wall: The job's wall-clock seconds, used for the share column.
            overlapped: Whether downloads ran concurrently, in which case the phases that overlap are flagged

        Returns:
            The lines of the summary, header first. Empty if nothing was recorded.
        """
        rows = [phase for phase in PHASES if self.seconds.get(phase)]
        if not rows:
            return []
        header = "where the time went"
        if wall:
            header += " (wall {:.1f}s)".format(wall)
        if overlapped:
            header += " -- fetch/pace overlap, so shares can exceed 100%"
        out = [header + ":"]
        width = max(len(phase) for phase in rows)
        for phase in rows:
            secs = self.seconds[phase]
            calls = self.calls.get(phase, 0)
            share = "{:>6.1f}%".format(100 * secs / wall) if wall else " " * 7
            average = "avg {:>6.2f}s".format(secs / calls) if calls > 1 else " " * 11
            out.append(
                "  {:<{width}} {:>8.1f}s {}  x{:<5} {}   {}".format(
                    phase,
                    secs,
                    share,
                    calls,
                    average,
                    PHASE_LABELS.get(phase, ""),
                    width=width,
                )
            )
        transfer = self.seconds.get("fetch_body", 0.0)
        if self.downloaded_bytes and transfer > 0:
            out.append(
                "  transferred {} at {:.1f} MB/s".format(
                    human_bytes(self.downloaded_bytes),
                    self.downloaded_bytes / transfer / 1_000_000,
                )
            )
        return out
