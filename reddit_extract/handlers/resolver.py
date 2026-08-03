"""
Per-host link resolvers: turn one external URL into downloadable media.

A `MediaHandler` answers this post is mine; a `LinkResolver` answers the narrower this URL is mine. Important because
the same host may show up multiple times: as a link post's target, URL behind a crosspost, or as a link in a
text post's body. A resolver is written against a URL and reused by all of them

Resolvers are looked up by domain, so registration order carries no meaning. That also makes the registry a whitelist:
Only registered hosts not any arbitrary URL

Writing one::

    class ImgchestResolver(LinkResolver):
        host = "imgchest"
        domains = ("imgchest.com",)
        media_type = MediaType.GALLERY

        async def resolve(self, url, ctx, *, ref):
            result = await ctx.fetch(url)
            if not result.ok or result.body is None:
                await ctx.skip(ref, "imgchest: {} is unreadable".format(url))
                return []
            return [MediaCandidate(u, self.media_type) for u in files_in(result.body)]

Keep the parsing in plain module-level functions taking bytes and returning data, as the built-in resolvers do:
those test without a browser, an event loop, or a fake context.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, ClassVar, Iterable, Iterator, List, Sequence
from urllib.parse import urlparse

from ..models.media import MediaCandidate, MediaType

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext


def host_of(url: str) -> str:
    """The lower-case host of a URL, without any port ('' when it has none)."""
    netloc = urlparse(url).netloc.lower()
    if not netloc:
        return ""
    if (
        "@" in netloc
    ):  # strip userinfo before the port, so a password containing ':' can't confuse the split
        netloc = netloc.rsplit("@", 1)[1]
    return netloc.split(":", 1)[0]


def domain_matches(url: str, domains: Iterable[str]) -> bool:
    """
    Whether ``url``'s host is one of ``domains`` or a subdomain of one.

    The boundary is pinned to a dot, so ``notredgifs.com`` does not match ``redgifs.com`` while ``media.redgifs.com`` does.

    Args:
        url: The URL to test.
        domains: Bare domains to match against, lower-case (``("redgifs.com",)``).

    Returns:
        True when the URL belongs to one of the domains.
    """
    host = host_of(url)
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)


class LinkResolver(abc.ABC):
    """
    Resolves URLs on one external host into downloadable media candidates.

    Attributes:
        host: Short name for the host, used as the key for its `ExtractionContext.host_option` settings, its
            `ExtractionContext.state` namespace, and ``config.scrape_all_hosts`` (``"redgifs"``).
        domains: The bare domains this resolver claims. Subdomains are included (see `domain_matches`).
        media_type: What this resolver produces. A resolver is not consulted at all when the job asked for none of
            it, so an audio host costs nothing on an images-only run.
    """

    host: ClassVar[str]
    domains: ClassVar[tuple[str, ...]] = ()
    media_type: ClassVar[MediaType]

    def claims(self, url: str) -> bool:
        """
        Whether this resolver handles ``url``.

        The default matches the resolver's ``domains``. Override for a host that needs to look at the path too
        (claiming ``/a/<id>`` album URLs but not the rest of the site).
        """
        return domain_matches(url, self.domains)

    @abc.abstractmethod
    async def resolve(
        self, url: str, ctx: "ExtractionContext", *, ref: str
    ) -> List[MediaCandidate]:
        """
        Return the downloadable media behind ``url``.

        Args:
            url: The URL this resolver claimed.
            ctx: The active extraction context. Use ``ctx.fetch`` for API calls: it paces per host and retries
                transient failures for you.
            ref: What to name in skip messages (the post the URL came from)

        Returns:
            The resolved candidates, or ``[]`` when the URL yields nothing. Report *why* with ``await
            ctx.skip(ref, reason)`` rather than raising: one unreachable host should not end the post.
        """

    @property
    def name(self) -> str:
        """The resolver's class name (used in logs)."""
        return type(self).__name__

    def __repr__(self) -> str:  # pragma: no cover
        return "<{}>".format(self.name)


class ResolverRegistry:
    """
    The set of hosts a job knows how to follow, looked up by domain.

    Args:
        resolvers: The resolvers to register. Two claiming one domain is a configuration mistake.
    """

    def __init__(self, resolvers: Sequence[LinkResolver] = ()) -> None:
        self._resolvers: List[LinkResolver] = list(resolvers)

    def add(self, resolver: LinkResolver) -> None:
        """Register another resolver."""
        self._resolvers.append(resolver)

    def __iter__(self) -> Iterator[LinkResolver]:
        return iter(self._resolvers)

    def __len__(self) -> int:
        return len(self._resolvers)

    @property
    def media_type(self) -> MediaType:
        """Everything the registered resolvers can produce, combined (empty when none are registered)."""
        combined = MediaType(0)
        for resolver in self._resolvers:
            combined |= resolver.media_type
        return combined

    def resolver_for(
        self, url: str, wanted: MediaType = MediaType.ALL
    ) -> LinkResolver | None:
        """
        The resolver claiming ``url``, or None when no registered host does.

        Args:
            url: The URL to route.
            wanted: The media types the job asked for. A resolver producing none of them is passed over, so a run
                that only wants images never calls an audio host's API.

        Returns:
            The claiming resolver, or None.
        """
        if not url:
            return None
        for resolver in self._resolvers:
            if (resolver.media_type & wanted) and resolver.claims(url):
                return resolver
        return None

    def claims(self, url: str, wanted: MediaType = MediaType.ALL) -> bool:
        """Whether any registered resolver would take ``url`` (see `resolver_for`)."""
        return self.resolver_for(url, wanted) is not None


def default_resolvers() -> List[LinkResolver]:
    """Fresh instances of the built-in link resolvers."""
    # Imported here rather than at module scope: the host modules import this one for the base class.
    from .redgifs import RedGifsResolver

    return [RedGifsResolver()]
