"""
Progress callbacks.

The library never prints. Wire these callbacks to ``print``, tqdm, webhooks, queues... Callbacks may be plain functions
or coroutine functions; both are supported. With the synchronous `HoplinkExtractor`, callbacks run on the extractor's
internal event-loop thread.

Exceptions raised by a callback are not swallowed: they abort the current job (in batches this surfaces as
``result.error`` instead of crashing the whole run).
"""

from __future__ import annotations

import dataclasses
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - import cycle guards, types only
    from .handlers.base import MediaHandler
    from .models.media import MediaItem
    from .models.post import Post
    from .models.result import ExtractionResult
    from .models.source import Source


@dataclass
class Events:
    """Optional callbacks fired during extraction. Any field may be None."""

    #: a job begins: (source)
    on_job_start: Optional[Callable[["Source"], Any]] = None
    #: after each scroll round: (source, total_collected, new_this_round)
    on_scroll: Optional[Callable[["Source", int, int], Any]] = None
    #: listing fully harvested: (source, post_count)
    on_harvested: Optional[Callable[["Source", int], Any]] = None
    #: a handler matched a post: (source, post, handler)
    on_post: Optional[Callable[["Source", "Post", "MediaHandler"], Any]] = None
    #: a candidate resolved during a dry run: (source, item)
    on_media_found: Optional[Callable[["Source", "MediaItem"], Any]] = None
    #: a file was downloaded and stored: (source, item)
    on_media_saved: Optional[Callable[["Source", "MediaItem"], Any]] = None
    #: something was skipped or failed: (source, url, reason)
    on_skip: Optional[Callable[["Source", str, str], Any]] = None
    #: a job finished: (result)
    on_job_end: Optional[Callable[["ExtractionResult"], Any]] = None

    def merged_with(self, override: "Events | None") -> "Events":
        """
        Merge other Events over this one, field by field.

        Args:
            override: Callbacks to layer on top. Each non-None field replaces the corresponding one here.
                None returns ``self`` unchanged.

        Returns:
            The merged Events (a new instance when ``override`` is not None).
        """
        if override is None:
            return self
        merged = {}
        for f in dataclasses.fields(self):
            value = getattr(override, f.name)
            merged[f.name] = value if value is not None else getattr(self, f.name)
        return Events(**merged)


async def emit(callback: Optional[Callable[..., Any]], *args: Any) -> None:
    """
    Invoke a callback if set, awaiting it when it returns an awaitable.

    Args:
        callback: The callback to call, or None to do nothing.
        *args: Positional arguments passed to the callback.
    """
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result
