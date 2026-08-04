"""
Tests for the per-host link resolver layer.

Browser-free: matching a URL to its host is a pure function, and the registry only ever calls ``claims``, so a
resolver that records its calls covers the routing without any network.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from hoplink.handlers.redgifs import RedGifsResolver
from hoplink.handlers.resolver import (
    LinkResolver,
    ResolverRegistry,
    default_resolvers,
    domain_matches,
    host_of,
)
from hoplink.models.media import MediaCandidate, MediaType


class FakeResolver(LinkResolver):
    """A resolver over ``example.com`` that records what it was asked to resolve."""

    host = "example"
    domains = ("example.com",)
    media_type = MediaType.IMAGE

    def __init__(self, urls: List[str] | None = None) -> None:
        self._urls = urls or []
        self.calls: List[tuple[str, str]] = []

    async def resolve(self, url: str, ctx: Any, *, ref: str) -> List[MediaCandidate]:
        self.calls.append((url, ref))
        return [MediaCandidate(u, self.media_type) for u in self._urls]


class OtherResolver(FakeResolver):
    """A second host, serving a different kind of media."""

    host = "other"
    domains = ("other.test",)
    media_type = MediaType.AUDIO


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://redgifs.com/watch/abc", "redgifs.com"),
        ("https://MEDIA.RedGifs.com/A.mp4", "media.redgifs.com"),
        ("https://example.com:8443/a", "example.com"),  # the port is not the host
        ("https://user:pw@example.com/a", "example.com"),  # nor is the userinfo
        ("https://user:p@ss@example.com/a", "example.com"),  # even with an '@' in it
        ("not a url", ""),
        ("", ""),
    ],
)
def test_host_of(url, expected):
    assert host_of(url) == expected


def test_a_domain_matches_itself_and_its_subdomains():
    assert domain_matches("https://redgifs.com/watch/a", ("redgifs.com",))
    assert domain_matches("https://media.redgifs.com/A.mp4", ("redgifs.com",))


def test_a_lookalike_domain_does_not_match():
    assert not domain_matches("https://notredgifs.com/watch/a", ("redgifs.com",))
    assert not domain_matches("https://redgifs.com.evil.test/a", ("redgifs.com",))


def test_any_of_several_domains_matches():
    assert domain_matches("https://b.test/x", ("a.test", "b.test"))


def test_a_url_with_no_host_matches_nothing():
    assert not domain_matches("relative/path", ("example.com",))
    assert not domain_matches("", ("example.com",))


def test_a_resolver_claims_its_own_domains_by_default():
    resolver = FakeResolver()
    assert resolver.claims("https://example.com/a/1") is True
    assert resolver.claims("https://elsewhere.test/a/1") is False


def test_a_resolver_reports_its_class_name():
    assert FakeResolver().name == "FakeResolver"


def test_an_empty_registry_serves_no_media_type():
    registry = ResolverRegistry()
    assert registry.media_type == MediaType(0)
    assert len(registry) == 0


def test_the_registrys_media_type_is_every_resolvers_combined():
    registry = ResolverRegistry([FakeResolver(), OtherResolver()])
    assert registry.media_type == MediaType.IMAGE | MediaType.AUDIO


def test_a_url_is_routed_to_the_host_that_claims_it():
    image, audio = FakeResolver(), OtherResolver()
    registry = ResolverRegistry([image, audio])
    assert registry.resolver_for("https://other.test/track") is audio
    assert registry.resolver_for("https://example.com/pic") is image


def test_an_unregistered_host_routes_nowhere():
    registry = ResolverRegistry([FakeResolver()])
    assert registry.resolver_for("https://stranger.test/whatever") is None
    assert registry.claims("https://stranger.test/whatever") is False


def test_an_empty_url_routes_nowhere():
    assert ResolverRegistry([FakeResolver()]).resolver_for("") is None


def test_a_resolver_serving_nothing_the_job_wants_is_passed_over():
    registry = ResolverRegistry([FakeResolver(), OtherResolver()])
    assert registry.resolver_for("https://other.test/track", MediaType.IMAGE) is None
    assert (
        registry.resolver_for("https://other.test/track", MediaType.AUDIO) is not None
    )


def test_the_first_registered_resolver_wins_an_overlap():
    first, second = FakeResolver(), FakeResolver()
    registry = ResolverRegistry([first, second])
    assert registry.resolver_for("https://example.com/a") is first


def test_a_resolver_can_be_added_after_construction():
    registry = ResolverRegistry()
    registry.add(FakeResolver())
    assert registry.claims("https://example.com/a") is True
    assert list(registry) and len(registry) == 1


def test_redgifs_is_registered_by_default():
    assert any(isinstance(r, RedGifsResolver) for r in default_resolvers())


def test_each_call_hands_out_fresh_resolver_instances():
    assert default_resolvers()[0] is not default_resolvers()[0]


def test_every_default_resolver_names_a_host_and_serves_a_media_type():
    for resolver in default_resolvers():
        assert resolver.host and resolver.host == resolver.host.lower()
        assert isinstance(resolver.media_type, MediaType) and resolver.media_type
