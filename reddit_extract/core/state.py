"""
State handlers keep across posts and jobs.

A handler resolving one post might learn something the next post can reuse: a bearer token, the set of profiles already
scraped. Keeping that on the handler instance would scope it. One handler chain serves every job, including the ones a
concurrent `batch` runs at the same time, so the cache is silently shared with no lock around it.

This module gives that state an explicit home. `ExtractionContext.state` hands a handler its own namespace, shared
by every job of one extractor, together with the lock to guard it::

    store = ctx.state("redgifs")
    async with store.lock:
        token = store.data.get("token")

Namespaces are created on first use and never removed. Their locks belong to the running event loop, so the store
is reset whenever the loop is replaced (see `AsyncRedditExtractor._reset_loop_state`).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict


class HostState:
    """
    One namespace's shared dictionary, plus the lock that guards it.

    Attributes:
        data: The namespace's contents. Anything may be stored here; keys are the handler's own business.
        lock: Held while reading-then-writing ``data``, so two concurrent jobs can't both decide they are the one
            to do a piece of work.
    """

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}
        self.lock = asyncio.Lock()


class SharedState:
    """Namespaced state shared by every job a single extractor runs."""

    def __init__(self) -> None:
        self._namespaces: Dict[str, HostState] = {}

    def namespace(self, name: str) -> HostState:
        """
        The state for ``name``, created empty on first use.

        Args:
            name: A namespace key, conventionally the host a handler serves (``"redgifs"``, ``"imgur"``).

        Returns:
            The same HostState for the rest of the extractor's life, so two jobs asking for one namespace get one
            store and one lock.
        """
        store = self._namespaces.get(name)
        if store is None:
            store = HostState()
            self._namespaces[name] = store
        return store

    def reset(self) -> None:
        """Drop every namespace, discarding its loop-bound lock along with its contents."""
        self._namespaces.clear()
