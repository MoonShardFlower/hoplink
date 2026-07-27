"""
The async-native extraction engine.

One `AsyncRedditExtractor` owns one browser and runs any number of jobs through it, sequentially or concurrently.
The synchronous `RedditExtractor` facade wraps this class; all behavior lives here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterable,
    List,
    Optional,
    Sequence,
    Union,
)
from urllib.parse import urlparse

from ..events import Events, emit
from ..exceptions import NoPostsFoundError
from ..handlers import default_handlers
from ..handlers.base import MediaHandler
from ..models.config import ExtractorConfig
from ..models.filters import PostFilter
from ..models.media import MediaCandidate, MediaItem, MediaType, MediaTypeLike
from ..models.post import Post
from ..models.result import ExtractionResult
from ..models.source import Source, parse_source
from ..storage import FilesystemStorage, Manifest, StorageBackend
from .browser import BrowserManager, FetchResult
from .context import ExtractionContext

log = logging.getLogger(__name__)

SourceLike = Union[Source, str]

# JS run in the listing page: read every rendered post card's key attributes. Packaged-media-json (direct MP4) lives on
# the embedded player element, not on the post card itself. `stickied` is a boolean attribute (it renders as
# stickied="" ), so it must be tested with hasAttribute: getAttribute would hand back the empty string, which is falsy.
JS_HARVEST = """
() => Array.from(document.querySelectorAll('shreddit-post')).map(p => {
  const titleEl = p.querySelector('[slot="title"]');
  const player = p.querySelector('shreddit-player, shreddit-player-2, [packaged-media-json]');
  const flairEl = p.querySelector('[slot="post-flair"]');
  return {
    id: p.getAttribute('id'),
    type: p.getAttribute('post-type'),
    permalink: p.getAttribute('permalink'),
    content_href: p.getAttribute('content-href'),
    author: p.getAttribute('author'),
    created: p.getAttribute('created-timestamp'),
    subreddit: p.getAttribute('subreddit-prefixed-name'),
    domain: p.getAttribute('domain'),
    score: p.getAttribute('score'),
    comment_count: p.getAttribute('comment-count'),
    flair: flairEl ? flairEl.textContent.trim() : null,
    stickied: p.hasAttribute('stickied'),
    packaged_media: (player && player.getAttribute('packaged-media-json'))
                    || p.getAttribute('packaged-media-json'),
    player_src: player ? player.getAttribute('src') : null,
    title: titleEl ? titleEl.textContent.trim() : (p.getAttribute('post-title') || null),
  };
})
"""

#: whether the page is the modern UI or Reddit's legacy layout
JS_IS_MODERN = "() => !!document.querySelector('shreddit-post')"

# Reddit still serves some listings (notably multireddits like r/a+b) on the legacy UI. Its .thing elements carry
# data-* attributes we map onto the same harvest shape. The post type is derived from those attributes. The legacy
# markup has no data-stickied attribute (it marks a sticky with a CSS class instead) and spells creation time as epoch
# milliseconds rather than the modern UI's ISO text (Post.created_at reads both).
JS_HARVEST_LEGACY = """
() => Array.from(document.querySelectorAll('#siteTable .thing'))
  .filter(t => t.getAttribute('data-promoted') !== 'true')
  .map(t => {
    const domain = t.getAttribute('data-domain') || '';
    let type = 'link';
    if (t.getAttribute('data-is-gallery') === 'true') type = 'gallery';
    else if (t.getAttribute('data-type') === 'self') type = 'text';
    else if (domain === 'v.redd.it') type = 'video';
    else if (domain === 'i.redd.it') type = 'image';
    const titleEl = t.querySelector('a.title');
    const flairEl = t.querySelector('.linkflairlabel');
    return {
      id: t.getAttribute('data-fullname'),
      type,
      permalink: t.getAttribute('data-permalink'),
      content_href: t.getAttribute('data-url'),
      author: t.getAttribute('data-author'),
      created: t.getAttribute('data-timestamp'),
      subreddit: t.getAttribute('data-subreddit-prefixed'),
      domain,
      score: t.getAttribute('data-score'),
      comment_count: t.getAttribute('data-comments-count'),
      flair: flairEl ? flairEl.textContent.trim() : null,
      stickied: t.classList.contains('stickied'),
      packaged_media: null,
      player_src: null,
      title: titleEl ? titleEl.textContent.trim() : null,
    };
  })
"""

#: Advance the modern infinite feed. `page.mouse.wheel` is unreliable here. On some listings (notably user profiles).
#: It silently fails to move the page at all (scrollY stays 0), so the feed never requests its next batch and harvest
#: stalls after the first ~25 posts. Driving the scroll from JS moves the document directly.
JS_SCROLL = "(px) => window.scrollBy(0, px)"

#: legacy listings paginate instead of infinite-scrolling
JS_NEXT_PAGE = """
() => { const a = document.querySelector('span.next-button a'); return a ? a.href : null; }
"""

#: Non-2xx statuses a retry could plausibly clear, alongside anything >= 500. Status 0 is how
#: `BrowserManager.fetch` reports a transport error (timeout, connection reset, DNS failure).
RETRY_STATUSES = frozenset({0, 408, 425, 429})


class AsyncRedditExtractor:
    """
    Async extraction engine.

    Use it as an async context manager so the browser is closed for you::

        async with AsyncRedditExtractor() as rex:
            result = await rex.extract("r/EarthPorn")

    Args:
        config: Full configuration object. Individual fields may instead be overridden with keyword arguments, e.g.
            ``AsyncRedditExtractor(headless=False)``.
        storage: Where files and manifests go. Defaults to the filesystem under ``config.output_dir``.
        handlers: Replaces the default handler chain entirely.
        events: Default progress callbacks, merged with per-call events.
        post_filter: Default post predicates, overridable per call.
        browser: Custom browser manager (mainly for tests).
    """

    def __init__(
        self,
        config: ExtractorConfig | None = None,
        *,
        storage: StorageBackend | None = None,
        handlers: Sequence[MediaHandler] | None = None,
        events: Events | None = None,
        post_filter: PostFilter | None = None,
        browser: BrowserManager | None = None,
        **config_overrides: Any,
    ) -> None:
        cfg = config or ExtractorConfig()
        if config_overrides:
            cfg = cfg.replace(**config_overrides)
        self._config = cfg
        self._storage = storage
        self._storage_explicit = storage is not None
        self._handlers: List[MediaHandler] = (
            list(handlers) if handlers is not None else default_handlers()
        )
        self._events = events or Events()
        self._post_filter = post_filter
        self._browser = browser or BrowserManager(cfg)
        self._start_lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------

    @property
    def config(self) -> ExtractorConfig:
        """The active ExtractorConfig."""
        return self._config

    @property
    def handlers(self) -> List[MediaHandler]:
        """A copy of the current handler chain, in selection order."""
        return list(self._handlers)

    def register_handler(self, handler: MediaHandler, *, prepend: bool = True) -> None:
        """
        Add a custom handler to the chain.

        Args:
            handler: The handler to register.
            prepend: If True (default), insert it at the front so it takes precedence over the built-in handlers; otherwise append it.
        """
        if prepend:
            self._handlers.insert(0, handler)
        else:
            self._handlers.append(handler)

    async def start(self) -> "AsyncRedditExtractor":
        """Launch the browser if needed and return self (idempotent)."""
        async with self._start_lock:
            await self._browser.start()
        return self

    def _reset_loop_state(self) -> None:
        """Drop loop-bound primitives; called by the sync facade when it replaces its event loop."""
        self._start_lock = asyncio.Lock()

    async def close(self) -> None:
        """Close the browser and release its resources."""
        await self._browser.close()

    async def __aenter__(self) -> "AsyncRedditExtractor":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- public API ------------------------------------------------------------

    async def extract(
        self,
        source: SourceLike,
        *,
        media_types: MediaTypeLike | None = None,
        output_dir: str | None = None,
        dry_run: bool = False,
        post_filter: PostFilter | None = None,
        events: Events | None = None,
    ) -> ExtractionResult:
        """
        Extract one source and return its result.

        Args:
            source: A Subreddit, UserProfile, or MultiReddit, or a string such as ``"r/EarthPorn"`` (parsed via `parse_source`).
            media_types: Which media to extract. Defaults to the config's ``default_media_types`` (images and galleries).
            output_dir: Override the storage root for this call only.
            dry_run: Resolve every candidate but download nothing.
            post_filter: Predicates narrowing which harvested posts to keep, replacing the extractor's default for this call.
            events: Extra progress callbacks for this call, merged over the extractor's defaults.

        Returns:
            The ExtractionResult for this source.
        """
        await self.start()
        src = parse_source(source) if isinstance(source, str) else source
        wanted = (
            MediaType.coerce(media_types)
            if media_types is not None
            else self._config.default_media_types
        )
        storage = self._resolve_storage(output_dir)
        ev = self._events.merged_with(events)
        filt = post_filter if post_filter is not None else self._post_filter
        return await self._run_job(src, wanted, storage, dry_run, filt, ev)

    async def batch(
        self,
        sources: Iterable[SourceLike],
        *,
        concurrency: int = 1,
        raise_on_error: bool = False,
        **extract_kwargs: Any,
    ) -> List[ExtractionResult]:
        """
        Extract many sources, returning results in input order.

        Failures don't abort the batch: a failed source's result carries a populated ``.error`` instead (unless ``raise_on_error=True``).

        Args:
            sources: The sources (or source strings) to extract.
            concurrency: How many jobs to run at once, each on its own page of the shared browser.
                Don't list the same source twice in one concurrent batch, or the jobs will race on a shared manifest.
            raise_on_error: Re-raise the first job's exception instead of recording it on the result.
            **extract_kwargs: Forwarded to `extract` (``media_types``, ``output_dir``, ``dry_run``, ``events``).

        Returns:
            One ExtractionResult per source, in the order given.
        """
        await self.start()
        runner = self._job_runner(raise_on_error, extract_kwargs)
        tasks = self._spawn(sources, concurrency, runner)
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            # With raise_on_error=True, a failing job must not leave its siblings running detached.
            await self._reap(tasks)
            raise

    async def iter_batch(
        self,
        sources: Iterable[SourceLike],
        *,
        concurrency: int = 1,
        raise_on_error: bool = False,
        **extract_kwargs: Any,
    ) -> AsyncGenerator[ExtractionResult, None]:
        """
        Like `batch`, but yields each result as soon as it finishes.

        Args:
            sources: The sources (or source strings) to extract.
            concurrency: How many jobs to run at once (see `batch`).
            raise_on_error: Re-raise the first job's exception instead of recording it on the result.
            **extract_kwargs: Forwarded to `extract`.

        Yields:
            Each source's ExtractionResult in completion order (not input order).
        """
        await self.start()
        runner = self._job_runner(raise_on_error, extract_kwargs)
        tasks = self._spawn(sources, concurrency, runner)
        try:
            for fut in asyncio.as_completed(tasks):
                yield await fut
        finally:
            # Runs when the consumer abandons the iterator early (or a job raised with raise_on_error=True):
            # stop and *await* the rest so their pages are closed before control returns.
            await self._reap(tasks)

    @staticmethod
    def _spawn(
        sources: Iterable[SourceLike],
        concurrency: int,
        runner: Callable[[SourceLike], Awaitable[ExtractionResult]],
    ) -> List["asyncio.Task[ExtractionResult]"]:
        """
        Schedule one concurrency-bounded task per source.

        Args:
            sources: The sources (or source strings) to run.
            concurrency: Maximum number of jobs in flight at once.
            runner: The async callable applied to each source.

        Returns:
            The scheduled tasks, in input order.
        """
        sem = asyncio.Semaphore(max(1, concurrency))

        async def guarded(src: SourceLike) -> ExtractionResult:
            async with sem:
                return await runner(src)

        return [asyncio.ensure_future(guarded(s)) for s in sources]

    @staticmethod
    async def _reap(tasks: List["asyncio.Task[ExtractionResult]"]) -> None:
        """Cancel any unfinished tasks and wait for all of them to settle."""
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- internals ---------------------------------------------------------

    def _job_runner(
        self, raise_on_error: bool, extract_kwargs: dict[str, Any]
    ) -> Callable[[SourceLike], Awaitable[ExtractionResult]]:
        """
        Build a coroutine that runs one source and captures its errors.

        Args:
            raise_on_error: If True, let job exceptions propagate; otherwise convert them into an ExtractionResult with ``.error`` set.
            extract_kwargs: Keyword arguments forwarded to `extract`.

        Returns:
            An async callable ``run(src) -> ExtractionResult``.
        """

        async def run(src: SourceLike) -> ExtractionResult:
            source = parse_source(src) if isinstance(src, str) else src
            try:
                return await self.extract(source, **extract_kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if raise_on_error:
                    raise
                log.exception("job failed: %s", source.key)
                result = ExtractionResult(
                    source=source, error="{}: {}".format(type(exc).__name__, exc)
                )
                result.finished_at = datetime.now(timezone.utc)
                ev = self._events.merged_with(extract_kwargs.get("events"))
                await emit(ev.on_job_end, result)
                return result

        return run

    def _resolve_storage(self, output_dir: str | None) -> StorageBackend:
        """
        Pick the storage backend for a call.

        Args:
            output_dir: A per-call output root, or None to use the configured storage (or the default filesystem location).

        Returns:
            The storage backend to use.

        Raises:
            ValueError: If ``output_dir`` is combined with an explicit custom storage backend.
        """
        if output_dir is not None:
            if self._storage_explicit:
                raise ValueError(
                    "output_dir cannot be combined with a custom storage backend. Configure the backend's location instead."
                )
            return FilesystemStorage(output_dir)
        if self._storage is None:
            self._storage = FilesystemStorage(self._config.output_dir)
        return self._storage

    def _select_handler(self, post: Post, wanted: MediaType) -> Optional[MediaHandler]:
        """
        Return the first handler that both wants ``post`` and is requested.

        Args:
            post: The harvested post to match.
            wanted: The media types requested for this job.

        Returns:
            The selected handler, or None if no handler applies.
        """
        for handler in self._handlers:
            if (handler.media_type & wanted) and handler.can_handle(post):
                return handler
        return None

    async def _harvest(
        self, page: Any, source: Source, ev: Events, flair: str | None = None
    ) -> List[Post]:
        """
        Walk the listing, accumulating posts round by round.

        Modern listings are virtualized infinite scroll, so posts are collected on every scroll before they're
        recycled out of the DOM. Reddit's legacy UI (served for some listings, e.g., multireddits) paginates via a "next"
        link instead.

        Args:
            page: The page to drive.
            source: The source being harvested (provides URL and limit).
            ev: Progress callbacks (``on_scroll`` fires each round).
            flair: A link flair to ask Reddit to filter the listing by, honored only by sources that support it
                (see `Source.listing_url`). ``source.limit`` then counts matching posts rather than posts scrolled past.

        Returns:
            Up to ``source.limit`` posts, in listing order.

        Raises:
            NoPostsFoundError: If the listing never renders any posts. A listing Reddit filtered by flair is
                exempt: "no posts carry this flair" is an empty result, not a broken source.
        """
        cfg = self._config
        url = source.listing_url(flair=flair)
        filtered = url != source.url
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
        await self._browser.dismiss_gates(page)
        if not await self._browser.wait_for_posts(page, cfg.post_wait_timeout_ms):
            if filtered:
                return []
            raise NoPostsFoundError(url)
        modern = bool(await page.evaluate(JS_IS_MODERN))
        harvest_js = JS_HARVEST if modern else JS_HARVEST_LEGACY

        harvested: dict[str, dict[str, Any]] = {}
        order: List[str] = []
        stale = 0
        while len(harvested) < source.limit and stale < cfg.max_stale_scrolls:
            new_this_round = 0
            for data in await page.evaluate(harvest_js):
                pid = data.get("id")
                if pid and pid not in harvested:
                    harvested[pid] = data
                    order.append(pid)
                    new_this_round += 1
            stale = stale + 1 if new_this_round == 0 else 0
            await emit(ev.on_scroll, source, len(harvested), new_this_round)
            if len(harvested) >= source.limit:
                break
            if modern:
                await page.evaluate(JS_SCROLL, cfg.scroll_px)
            else:
                next_url = await page.evaluate(JS_NEXT_PAGE)
                if not next_url:
                    break
                await page.goto(
                    next_url,
                    wait_until="domcontentloaded",
                    timeout=cfg.nav_timeout_ms,
                )
            await page.wait_for_timeout(int(cfg.scroll_pause * 1000))

        return [Post.from_harvest(harvested[pid]) for pid in order[: source.limit]]

    @staticmethod
    def _filename(index: int, part: int | None, cand: MediaCandidate) -> str:
        """
        Build a zero-padded filename for a candidate.

        Args:
            index: The post's allocated index (e.g. ``1`` -> ``0001``).
            part: The 1-based slide number for multi-file posts, or None for single-file posts.
            cand: The candidate being saved (supplies the extension).

        Returns:
            A name like ``"0001.jpg"`` or ``"0001_02.jpg"``.
            The extension falls back to ``mp4`` for video and ``jpg`` otherwise.
        """
        ext = cand.ext or os.path.splitext(urlparse(cand.url).path)[1].lower().lstrip(
            "."
        )
        if not ext:
            ext = "mp4" if cand.media_type == MediaType.VIDEO else "jpg"
        if part is None:
            return "{:04d}.{}".format(index, ext)
        return "{:04d}_{:02d}.{}".format(index, part, ext)

    @staticmethod
    def _is_transient(result: FetchResult) -> bool:
        """
        Whether a failed fetch is worth retrying.

        Args:
            result: The unsuccessful FetchResult to classify.

        Returns:
            True for faults that a later attempt could plausibly survive: transport errors
            (`BrowserManager.fetch` reports those with status 0 -- a timeout, reset, or DNS failure),
            rate limiting, and server-side errors. A 404 or 403 is the CDN's settled answer, so it is not retried.
        """
        return result.status in RETRY_STATUSES or result.status >= 500

    @staticmethod
    def _validate(
        ctx: ExtractionContext, cand: MediaCandidate, result: FetchResult
    ) -> FetchResult:
        """
        Check a successful response's Content-Type against what the candidate expects.

        Args:
            ctx: The active extraction context.
            cand: The candidate that was downloaded.
            result: The successful FetchResult to validate.

        Returns:
            ``result`` unchanged when the type is acceptable, otherwise an unsuccessful FetchResult
            explaining the mismatch. These verdicts are deterministic, so `_download` never retries them.
        """
        ctype = result.content_type
        expects_image = any(p.startswith("image/") for p in cand.content_prefixes)
        if expects_image and ctype == "image/gif" and "gif" not in ctx.formats:
            return FetchResult(ok=False, status=result.status, error="is a gif")
        if ctype and not any(ctype.startswith(p) for p in cand.content_prefixes):
            return FetchResult(
                ok=False,
                status=result.status,
                error="unexpected content-type ({})".format(ctype),
            )
        return result

    @staticmethod
    async def _download(ctx: ExtractionContext, cand: MediaCandidate) -> FetchResult:
        """
        Fetch a candidate, retrying transient failures, and validate its Content-Type.

        A flaky CDN shouldn't turn into a permanent entry in ``result.failures``, so a fetch that fails
        transiently (see `_is_transient`) is retried up to ``config.max_retries`` times with exponential
        backoff. Content-Type rejections are final: they'd fail identically on every attempt.

        Args:
            ctx: The active extraction context.
            cand: The candidate to download.

        Returns:
            The successful FetchResult, or an unsuccessful one if the last attempt failed, the response was
            a GIF while GIFs are disabled, or the Content-Type matched none of ``cand.content_prefixes``.
        """
        # A handler-composed candidate carries its own bytes (e.g. a text post's Markdown document).
        # There is nothing to fetch, retry, or Content-Type check.
        if cand.body is not None:
            return FetchResult(ok=True, status=200, body=cand.body)
        cfg = ctx.config
        attempts = cfg.max_retries + 1
        result = FetchResult(ok=False, error="no attempt made")
        for attempt in range(1, attempts + 1):
            result = await ctx.fetch(cand.url)
            if result.ok:
                return AsyncRedditExtractor._validate(ctx, cand, result)
            if attempt >= attempts or not AsyncRedditExtractor._is_transient(result):
                return result
            # Back off exponentially, but never faster than the job's politeness delay.
            delay = max(cfg.img_delay, cfg.retry_backoff * (2 ** (attempt - 1)))
            log.info(
                "attempt %d/%d for %s failed (%s); retrying in %.1fs",
                attempt,
                attempts,
                cand.url,
                result.error,
                delay,
            )
            await asyncio.sleep(delay)
        return result

    async def _run_job(
        self,
        source: Source,
        wanted: MediaType,
        storage: StorageBackend,
        dry_run: bool,
        post_filter: PostFilter | None,
        ev: Events,
    ) -> ExtractionResult:
        """
        Run the full pipeline for one source: harvest, resolve, download.

        Heart of the engine: It harvests the listing, selects a handler per post, resolves candidates, downloads, and
        records each one, skipping media already present on disk or in the manifest.

        Args:
            source: The source to extract.
            wanted: The media types to keep.
            storage: The storage backend to write to.
            dry_run: If True, resolve everything but write nothing.
            post_filter: Predicates a post must satisfy to be kept, or None to keep every post a handler wants.
            ev: Progress callbacks for this job.

        Returns:
            The populated ExtractionResult.
        """
        cfg = self._config
        result = ExtractionResult(source=source, dry_run=dry_run)
        await emit(ev.on_job_start, source)

        if not dry_run:
            storage.prepare(source.key)
        manifest = Manifest(
            source.key,
            storage.read_manifest(source.key),
            existing_files=storage.list_files(source.key),
        )
        known = manifest.known_urls()
        known_hashes = manifest.known_hashes()
        unflushed = 0

        page = await self._browser.new_page()
        ctx = ExtractionContext(
            source=source, config=cfg, events=ev, browser=self._browser, page=page
        )
        try:
            # Hand the flair to Reddit when both the filter and the source support it. The client-side flair
            # check below still runs, keeping the result correct if a listing ignores ?f=.
            flair = post_filter.server_side_flair if post_filter is not None else None
            posts = await self._harvest(page, source, ev, flair=flair)
            result.posts_scanned = len(posts)
            await emit(ev.on_harvested, source, len(posts))

            for post in posts:
                handler = self._select_handler(post, wanted)
                if handler is None:
                    continue
                # Filter after handler selection, so the count reflects posts this job would
                # otherwise have downloaded rather than every post the listing happened to hold.
                if post_filter is not None:
                    verdict = post_filter.rejection(post)
                    if verdict is not None:
                        result.posts_filtered += 1
                        await emit(ev.on_skip, source, post.url or post.id, verdict)
                        continue
                try:
                    candidates = await handler.resolve(post, ctx)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # One bad post (or a buggy custom handler) must not abort a long job; record it and keep going.
                    log.exception("handler %s failed on post %s", handler.name, post.id)
                    ref = post.url or post.id
                    reason = "handler {} error: {}".format(handler.name, exc)
                    result.failures.append((ref, reason))
                    await emit(ev.on_skip, source, ref, reason)
                    continue
                if not candidates and not handler.metadata_only:
                    continue
                # min_gallery can only be judged now: the listing card doesn't carry a gallery's size.
                if post_filter is not None:
                    verdict = post_filter.rejection_after_resolve(post, len(candidates))
                    if verdict is not None:
                        result.posts_filtered += 1
                        await emit(ev.on_skip, source, post.url or post.id, verdict)
                        continue

                result.posts_matched += 1
                result.posts.append(post)
                await emit(ev.on_post, source, post, handler)
                if not candidates:
                    continue

                result.media_found += len(candidates)
                multi = len(candidates) > 1
                index: int | None = None
                for part, cand in enumerate(candidates, 1):
                    if cand.url in known:
                        result.skipped_known += 1
                        continue
                    if index is None:
                        index = manifest.allocate_index()
                    fname = self._filename(index, part if multi else None, cand)
                    item = MediaItem(
                        url=cand.url,
                        media_type=cand.media_type,
                        filename=fname,
                        post_id=post.id,
                        post_url=post.url,
                        title=post.title,
                        author=post.author,
                        created=post.created,
                        source_key=source.key,
                    )
                    if dry_run:
                        result.items.append(item)
                        await emit(ev.on_media_found, source, item)
                        continue
                    if storage.exists(source.key, fname):
                        result.skipped_existing += 1
                        continue

                    outcome = await self._download(ctx, cand)
                    if outcome.ok and outcome.body is not None:
                        if cfg.dedupe_by_hash:
                            digest = hashlib.sha256(outcome.body).hexdigest()
                            if digest in known_hashes:
                                # Same bytes as a file already saved for this source (a repost/crosspost at a new URL).
                                result.skipped_duplicate += 1
                                await emit(
                                    ev.on_skip, source, cand.url, "duplicate content"
                                )
                                await asyncio.sleep(cfg.img_delay)
                                continue
                            known_hashes.add(digest)
                            item.sha256 = digest
                        item.path = storage.write(source.key, fname, outcome.body)
                        item.size = len(outcome.body)
                        item.downloaded = True
                        manifest.add(fname, item.to_manifest_entry())
                        known.add(cand.url)
                        result.items.append(item)
                        result.media_saved += 1
                        unflushed += 1
                        if unflushed >= max(1, cfg.manifest_flush_every):
                            path = storage.write_manifest(
                                source.key, manifest.to_dict()
                            )
                            result.manifest_path = path or result.manifest_path
                            unflushed = 0
                        await emit(ev.on_media_saved, source, item)
                    else:
                        reason = outcome.error or "download failed"
                        result.failures.append((cand.url, reason))
                        await emit(ev.on_skip, source, cand.url, reason)
                    await asyncio.sleep(cfg.img_delay)
        finally:
            await ctx.aclose()
            try:
                await page.close()
            except Exception:  # pragma: no cover
                pass

        if not dry_run:
            path = storage.write_manifest(source.key, manifest.to_dict())
            result.manifest_path = path or result.manifest_path
        result.output_dir = storage.location(source.key)
        result.finished_at = datetime.now(timezone.utc)
        await emit(ev.on_job_end, result)
        return result
