"""
Tests for the handler that routes a post's external links to their host's resolver.

Browser-free: the handler only picks URLs off a post and hands them to a resolver, so a recording resolver and a
context carrying nothing but ``wanted`` cover it.
"""

from __future__ import annotations

from typing import Any, List

from reddit_extract.handlers.external import ExternalLinkHandler
from reddit_extract.handlers.resolver import LinkResolver
from reddit_extract.models.media import MediaCandidate, MediaType
from reddit_extract.models.post import Post


class RecordingResolver(LinkResolver):
    """Resolves ``example.com`` to canned candidates, recording what it was asked for."""

    host = "example"
    domains = ("example.com",)
    media_type = MediaType.IMAGE

    def __init__(self, candidates: List[MediaCandidate] | None = None) -> None:
        self._candidates = candidates
        self.calls: List[tuple[str, str]] = []

    async def resolve(self, url: str, ctx: Any, *, ref: str) -> List[MediaCandidate]:
        self.calls.append((url, ref))
        if self._candidates is None:
            return [MediaCandidate("https://cdn.example.com/a.jpg", MediaType.IMAGE)]
        return list(self._candidates)


class FakeContext:
    """Stands in for ExtractionContext: the handler reads only ``wanted``."""

    def __init__(self, wanted: MediaType = MediaType.ALL) -> None:
        self.wanted = wanted


def post(**overrides: Any) -> Post:
    """One harvested link post pointing at the registered host."""
    data: dict[str, Any] = {
        "id": "p1",
        "type": "link",
        "permalink": "/r/pics/comments/p1/thing/",
        "content_href": "https://example.com/album/1",
    }
    data.update(overrides)
    return Post.from_harvest(data)


def handler(*resolvers: LinkResolver) -> ExternalLinkHandler:
    return ExternalLinkHandler(list(resolvers))


def test_a_post_linking_a_registered_host_is_claimed():
    assert handler(RecordingResolver()).can_handle(post()) is True


def test_a_post_linking_an_unregistered_host_is_left_alone():
    assert (
        handler(RecordingResolver()).can_handle(
            post(content_href="https://stranger.test/x")
        )
        is False
    )


def test_an_embedded_player_is_claimed_through_its_source():
    claimed = post(content_href=None, player_src="https://example.com/embed/1")
    assert handler(RecordingResolver()).can_handle(claimed) is True


def test_a_post_carrying_no_urls_is_left_alone():
    assert (
        handler(RecordingResolver()).can_handle(
            post(content_href=None, player_src=None)
        )
        is False
    )


def test_the_post_type_does_not_decide():
    for post_type in ("link", "video", "crosspost", "gif"):
        assert handler(RecordingResolver()).can_handle(post(type=post_type)) is True


def test_the_handler_serves_everything_its_resolvers_do():
    class AudioResolver(RecordingResolver):
        host = "audio"
        domains = ("audio.test",)
        media_type = MediaType.AUDIO

    assert (
        handler(RecordingResolver(), AudioResolver()).media_type
        == MediaType.IMAGE | MediaType.AUDIO
    )


def test_the_default_handler_covers_the_built_in_hosts():
    assert ExternalLinkHandler().can_handle(
        post(content_href="https://www.redgifs.com/watch/somecliphere")
    )


async def test_the_claimed_url_goes_to_its_resolver_named_by_its_post():
    resolver = RecordingResolver()
    candidates = await handler(resolver).resolve(post(), FakeContext())
    assert resolver.calls == [
        (
            "https://example.com/album/1",
            "https://www.reddit.com/r/pics/comments/p1/thing/",
        )
    ]
    assert [c.url for c in candidates] == ["https://cdn.example.com/a.jpg"]


async def test_the_cards_target_is_preferred_over_an_embedded_player():
    resolver = RecordingResolver()
    await handler(resolver).resolve(
        post(player_src="https://example.com/embed/9"), FakeContext()
    )
    assert resolver.calls[0][0] == "https://example.com/album/1"


async def test_a_post_no_resolver_claims_yields_nothing():
    resolver = RecordingResolver()
    assert (
        await handler(resolver).resolve(
            post(content_href="https://stranger.test/x"), FakeContext()
        )
        == []
    )
    assert resolver.calls == []


async def test_candidates_of_an_unwanted_kind_are_dropped():
    mixed = RecordingResolver(
        [
            MediaCandidate("https://cdn.example.com/a.jpg", MediaType.IMAGE),
            MediaCandidate("https://cdn.example.com/a.mp4", MediaType.VIDEO),
        ]
    )
    candidates = await handler(mixed).resolve(post(), FakeContext(MediaType.IMAGE))
    assert [c.url for c in candidates] == ["https://cdn.example.com/a.jpg"]


async def test_a_host_serving_nothing_wanted_is_never_asked():
    resolver = RecordingResolver()
    assert await handler(resolver).resolve(post(), FakeContext(MediaType.AUDIO)) == []
    assert resolver.calls == []  # its API was not called just to discard the answer
