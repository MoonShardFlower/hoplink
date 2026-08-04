"""
Tests for the state handlers keep across posts and jobs.

Browser-free: a dict and a lock, whose whole job is to be shared deliberately rather than by accident.
"""

from __future__ import annotations

import asyncio

from hoplink.core.state import SharedState


def test_a_namespace_starts_empty():
    assert SharedState().namespace("redgifs").data == {}


def test_asking_twice_gives_the_same_store():
    state = SharedState()
    assert state.namespace("redgifs") is state.namespace("redgifs")


def test_namespaces_do_not_see_each_other():
    state = SharedState()
    state.namespace("redgifs").data["token"] = "abc"
    assert state.namespace("imgur").data == {}


def test_each_namespace_has_its_own_lock():
    state = SharedState()
    assert state.namespace("redgifs").lock is not state.namespace("imgur").lock


async def test_the_lock_serializes_a_check_then_set():
    state = SharedState()
    claimed: list[str] = []

    async def claim(name: str) -> None:
        store = state.namespace("redgifs")
        async with store.lock:
            seen = store.data.setdefault("seen", set())
            if name not in seen:
                await asyncio.sleep(0)  # yield inside the critical section
                seen.add(name)
                claimed.append(name)

    await asyncio.gather(*(claim("creator") for _ in range(5)))
    assert claimed == ["creator"]  # exactly one caller won


def test_reset_drops_every_namespace():
    state = SharedState()
    state.namespace("redgifs").data["token"] = "abc"
    state.reset()
    assert state.namespace("redgifs").data == {}
