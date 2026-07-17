"""Tests for the progress callbacks: merging layers of them and invoking either flavor."""

from __future__ import annotations

import dataclasses
from typing import Any, List

import pytest

from reddit_extract.events import Events, emit

# -- merged_with ------------------------------------------------------------


def test_merging_nothing_returns_the_original_untouched():
    base = Events(on_skip=lambda s, u, r: None)
    assert base.merged_with(None) is base


def test_an_override_replaces_the_matching_callback():
    base_skip, override_skip = (lambda s, u, r: None), (lambda s, u, r: None)
    merged = Events(on_skip=base_skip).merged_with(Events(on_skip=override_skip))
    assert merged.on_skip is override_skip


def test_callbacks_the_override_leaves_unset_survive():
    # A per-call event=Events(on_media_saved=...) must not silently drop the CLI's printers.
    base_start = lambda source: None  # noqa: E731
    override_saved = lambda source, item: None  # noqa: E731
    merged = Events(on_job_start=base_start).merged_with(
        Events(on_media_saved=override_saved)
    )
    assert merged.on_job_start is base_start
    assert merged.on_media_saved is override_saved


def test_merging_returns_a_new_instance():
    base = Events()
    merged = base.merged_with(Events())
    assert merged is not base


def test_merging_does_not_mutate_either_side():
    base_skip = lambda s, u, r: None  # noqa: E731
    base = Events(on_skip=base_skip)
    override = Events(on_skip=lambda s, u, r: None)
    base.merged_with(override)
    assert base.on_skip is base_skip


def test_every_callback_field_takes_part_in_a_merge():
    # A field added to Events but forgotten here would silently never merge.
    names = [f.name for f in dataclasses.fields(Events)]
    override = Events(**{name: (lambda *a: name) for name in names})
    merged = Events().merged_with(override)
    assert all(getattr(merged, name) is getattr(override, name) for name in names)


def test_an_empty_override_changes_nothing():
    base = Events(on_job_end=lambda r: None)
    assert base.merged_with(Events()).on_job_end is base.on_job_end


def test_every_callback_defaults_to_none():
    events = Events()
    assert all(getattr(events, f.name) is None for f in dataclasses.fields(events))


# -- emit -------------------------------------------------------------------


async def test_emitting_an_unset_callback_does_nothing():
    await emit(None, "anything")  # must not raise


async def test_a_plain_function_is_called():
    seen: List[Any] = []
    await emit(lambda *args: seen.append(args), "pics", 3)
    assert seen == [("pics", 3)]


async def test_a_coroutine_function_is_awaited():
    seen: List[Any] = []

    async def callback(*args: Any) -> None:
        seen.append(args)

    await emit(callback, "pics", 3)
    assert seen == [("pics", 3)]


async def test_a_callback_returning_an_awaitable_is_awaited():
    # Not just coroutine functions: anything awaitable is honored.
    seen: List[Any] = []

    async def inner() -> None:
        seen.append("awaited")

    await emit(lambda: inner())
    assert seen == ["awaited"]


async def test_a_return_value_is_ignored():
    assert await emit(lambda: "ignored") is None


async def test_emitting_with_no_arguments_works():
    calls: List[int] = []
    await emit(lambda: calls.append(1))
    assert calls == [1]


# -- callbacks are not insulated from their own bugs ------------------------


async def test_an_exception_from_a_plain_callback_propagates():
    # Swallowing it would hide the caller's bug; batch turns it into result.error instead.
    def boom(*args: Any) -> None:
        raise ValueError("callback bug")

    with pytest.raises(ValueError, match="callback bug"):
        await emit(boom, "pics")


async def test_an_exception_from_an_async_callback_propagates():
    async def boom(*args: Any) -> None:
        raise ValueError("async callback bug")

    with pytest.raises(ValueError, match="async callback bug"):
        await emit(boom, "pics")
