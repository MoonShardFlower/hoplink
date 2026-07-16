"""
Synchronous facade over the async engine.

`RedditExtractor` runs a private asyncio event loop on a daemon thread and proxies every call to `AsyncRedditExtractor`,
so sync and async users get the same behavior. On Windows the loop is explicitly a ProactorEventLoop, because
Playwright launches Chromium as a subprocess, which the selector loop can't do.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import sys
import threading
from typing import Any, Coroutine, Iterable, Iterator, List, Sequence, TypeVar

from ..events import Events
from ..handlers.base import MediaHandler
from ..models.config import ExtractorConfig
from ..models.media import MediaTypeLike
from ..models.result import ExtractionResult
from ..storage.storage import StorageBackend
from .browser import BrowserManager
from .extractor import AsyncRedditExtractor, SourceLike

T = TypeVar("T")

_CLOSE_TIMEOUT = 60.0


class RedditExtractor:
    """
    Browser-driven Reddit media extractor (synchronous API).

    The context manager owns the browser; ``extract`` and ``batch`` may be called any number of times inside it.
    Calls also auto-start the browser if you skip the ``with`` block (call `close` yourself in that case).

    Constructor arguments match `AsyncRedditExtractor`.

    Example:
        >>> with RedditExtractor(headless=True) as rex:
        ...     result = rex.extract(
        ...         Subreddit("EarthPorn", limit=50, sort="top", time_filter="month")
        ...     )
        ...     print(result.summary())
    """

    def __init__(
        self,
        config: ExtractorConfig | None = None,
        *,
        storage: StorageBackend | None = None,
        handlers: Sequence[MediaHandler] | None = None,
        events: Events | None = None,
        browser: BrowserManager | None = None,
        **config_overrides: Any,
    ) -> None:
        """Create a synchronous extractor. Arguments match `AsyncRedditExtractor`."""
        self._async = AsyncRedditExtractor(
            config,
            storage=storage,
            handlers=handlers,
            events=events,
            browser=browser,
            **config_overrides,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pending: set[concurrent.futures.Future[Any]] = set()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def config(self) -> ExtractorConfig:
        """The active ExtractorConfig."""
        return self._async.config

    @property
    def handlers(self) -> List[MediaHandler]:
        """A copy of the current handler chain, in selection order."""
        return self._async.handlers

    def register_handler(self, handler: MediaHandler, *, prepend: bool = True) -> None:
        """Add a custom handler. See `AsyncRedditExtractor.register_handler`."""
        self._async.register_handler(handler, prepend=prepend)

    def start(self) -> "RedditExtractor":
        """Launch the browser (idempotent; re-usable after close())."""
        with self._lock:
            if self._loop is None:
                if sys.platform == "win32":
                    loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()
                else:
                    loop = asyncio.new_event_loop()
                # a fresh loop must not inherit primitives bound to the old one
                self._async._reset_loop_state()
                thread = threading.Thread(
                    target=self._run_loop,
                    args=(loop,),
                    name="reddit-extract-loop",
                    daemon=True,
                )
                thread.start()
                self._loop, self._thread = loop, thread
        self._run(self._async.start())
        return self

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        """Run the event loop forever on the daemon thread."""
        loop.run_forever()

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on the loop thread and wait for its result."""
        if self._loop is None:
            self.start()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)
        try:
            return future.result()
        except (KeyboardInterrupt, SystemExit):
            future.cancel()
            raise

    def close(self) -> None:
        """Cancel outstanding work, close the browser, stop the loop."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None:
            return
        for future in list(self._pending):
            future.cancel()
        try:
            closing = asyncio.run_coroutine_threadsafe(self._async.close(), loop)
            closing.result(timeout=_CLOSE_TIMEOUT)
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            pass
        finally:
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=10)
            with contextlib.suppress(Exception):
                loop.close()

    def __enter__(self) -> "RedditExtractor":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- public API ----------------------------------------------------------

    def extract(
        self,
        source: SourceLike,
        *,
        media_types: MediaTypeLike | None = None,
        output_dir: str | None = None,
        dry_run: bool = False,
        events: Events | None = None,
    ) -> ExtractionResult:
        """Extract one source. See `AsyncRedditExtractor.extract` for arguments."""
        return self._run(
            self._async.extract(
                source,
                media_types=media_types,
                output_dir=output_dir,
                dry_run=dry_run,
                events=events,
            )
        )

    def batch(
        self,
        sources: Iterable[SourceLike],
        *,
        concurrency: int = 1,
        raise_on_error: bool = False,
        **extract_kwargs: Any,
    ) -> List[ExtractionResult]:
        """
        Extract many sources, returning results in input order.

        Blocks until every job finishes. Failed sources get a result with ``.error`` set unless ``raise_on_error=True``.
        Use `iter_batch` to stream results as they complete instead. See `AsyncRedditExtractor.batch`.

        Args:
            sources: The sources (or source strings) to extract.
            concurrency: How many jobs to run at once.
            raise_on_error: Re-raise a job's exception instead of recording it.
            **extract_kwargs: Forwarded to `extract`.

        Returns:
            One ExtractionResult per source, in the order given.
        """
        return self._run(
            self._async.batch(
                sources,
                concurrency=concurrency,
                raise_on_error=raise_on_error,
                **extract_kwargs,
            )
        )

    def iter_batch(
        self,
        sources: Iterable[SourceLike],
        *,
        concurrency: int = 1,
        raise_on_error: bool = False,
        **extract_kwargs: Any,
    ) -> Iterator[ExtractionResult]:
        """
        Extract many sources, yielding each result as soon as it finishes.

        This is a lazy generator: iterate it (or wrap it in ``list``) to run the jobs.
        Results arrive in completion order, not input order.
        Failed sources yield a result with ``.error`` set unless ``raise_on_error=True``.

        Args:
            sources: The sources (or source strings) to extract.
            concurrency: How many jobs to run at once.
            raise_on_error: Re-raise a job's exception instead of recording it.
            **extract_kwargs: Forwarded to `extract`.

        Yields:
            Each source's ExtractionResult, in completion order.
        """
        agen = self._async.iter_batch(
            sources,
            concurrency=concurrency,
            raise_on_error=raise_on_error,
            **extract_kwargs,
        )
        try:
            while True:
                try:
                    yield self._run(agen.__anext__())
                except StopAsyncIteration:
                    break
        finally:
            if self._loop is not None:
                with contextlib.suppress(Exception):
                    self._run(agen.aclose())
