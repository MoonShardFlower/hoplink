"""
Tests for gallery slide reconstruction and the handler that drives it.

Browser-free: `gallery_image_urls` is a plain function over carousel HTML, and `GalleryHandler.resolve` only touches
``ctx.evaluate_on``, ``ctx.skip``, ``ctx.sleep``, and ``ctx.formats``. A fake context stands in for the post-page visit.
"""

from __future__ import annotations

from typing import Any, List

from reddit_extract.core.context import ExtractionContext
from reddit_extract.handlers.gallery import (
    JS_GALLERY,
    GalleryHandler,
    full_res_from_preview,
    gallery_image_urls,
)
from reddit_extract.models.config import ExtractorConfig
from reddit_extract.models.media import MediaType
from reddit_extract.models.post import Post

FORMATS = ("jpg", "jpeg", "png", "webp")


def preview(
    slug: str, token: str, ext: str = "jpg", query: str = "?width=640&s=abc"
) -> str:
    """One slide's preview URL, in the shape Reddit renders inside a carousel."""
    return "https://preview.redd.it/{}-v0-{}.{}{}".format(slug, token, ext, query)


def carousel(*urls: str) -> str:
    """A gallery carousel's outer HTML wrapping the given slide URLs."""
    slides = "".join('<li><img src="{}"></li>'.format(u) for u in urls)
    return "<gallery-carousel><ul>{}</ul></gallery-carousel>".format(slides)


class FakeContext:
    """Stands in for ExtractionContext: canned carousel HTML, recorded skips and pauses."""

    def __init__(self, html: str | Exception = "", formats=FORMATS) -> None:
        self.html = html
        self.formats = frozenset(formats)
        self.config = ExtractorConfig(gallery_wait_ms=2000)
        self.evaluated: List[tuple[str, str, Any, int]] = []
        self.skips: List[tuple[str, str]] = []
        self.sleeps = 0

    async def evaluate_on(
        self, url: str, js: str, arg: Any = None, wait_ms: int = 0
    ) -> Any:
        self.evaluated.append((url, js, arg, wait_ms))
        if isinstance(self.html, Exception):
            raise self.html
        return self.html

    async def skip(self, url: str, reason: str) -> None:
        self.skips.append((url, reason))

    async def sleep(self) -> None:
        self.sleeps += 1

    def extension_of(self, url: str) -> str:
        return ExtractionContext.extension_of(url)


def gallery_post(**overrides: Any) -> Post:
    """One harvested gallery post."""
    data: dict[str, Any] = {
        "id": "g1",
        "type": "gallery",
        "permalink": "/r/pics/comments/g1/album/",
    }
    data.update(overrides)
    return Post.from_harvest(data)


# -- gallery_image_urls: rebuilding the originals ---------------------------


def test_a_preview_url_becomes_a_full_resolution_i_redd_it_url():
    # The trailing token of the preview path is the i.redd.it media id; the rest is a display slug.
    html = carousel(preview("some-photo", "3ps5r77zi1ah1"))
    assert gallery_image_urls(html, FORMATS) == ["https://i.redd.it/3ps5r77zi1ah1.jpg"]


def test_the_downscaling_query_string_is_discarded():
    # Keeping ?width=640 would save Reddit's thumbnail instead of the original.
    html = carousel(preview("p", "abc123def", query="?width=320&crop=smart&auto=webp"))
    assert gallery_image_urls(html, FORMATS) == ["https://i.redd.it/abc123def.jpg"]


def test_slide_order_is_preserved():
    html = carousel(
        preview("p", "aaaaaa1"), preview("p", "bbbbbb2"), preview("p", "cccccc3")
    )
    assert gallery_image_urls(html, FORMATS) == [
        "https://i.redd.it/aaaaaa1.jpg",
        "https://i.redd.it/bbbbbb2.jpg",
        "https://i.redd.it/cccccc3.jpg",
    ]


def test_a_slide_referenced_twice_is_only_returned_once():
    # A carousel names each slide in both an <img src> and its <a href>.
    html = carousel(
        preview("p", "aaaaaa1"), preview("p", "aaaaaa1", query="?width=108")
    )
    assert gallery_image_urls(html, FORMATS) == ["https://i.redd.it/aaaaaa1.jpg"]


def test_extensions_outside_the_allowed_formats_are_dropped():
    html = carousel(
        preview("p", "aaaaaa1", ext="gif"), preview("p", "bbbbbb2", ext="jpg")
    )
    assert gallery_image_urls(html, FORMATS) == ["https://i.redd.it/bbbbbb2.jpg"]


def test_a_format_is_kept_once_it_is_allowed():
    html = carousel(preview("p", "aaaaaa1", ext="gif"))
    assert gallery_image_urls(html, ("gif",)) == ["https://i.redd.it/aaaaaa1.gif"]


def test_extension_case_is_normalized():
    html = carousel(preview("p", "aaaaaa1", ext="JPG"))
    assert gallery_image_urls(html, FORMATS) == ["https://i.redd.it/aaaaaa1.jpg"]


def test_a_url_with_no_extension_is_ignored():
    html = '<gallery-carousel><img src="https://preview.redd.it/slug-v0-aaaaaa1"></gallery-carousel>'
    assert gallery_image_urls(html, FORMATS) == []


def test_a_token_too_short_to_be_a_media_id_is_ignored():
    assert gallery_image_urls(carousel(preview("p", "abc")), FORMATS) == []


def test_a_token_with_non_alphanumeric_characters_is_ignored():
    assert gallery_image_urls(carousel(preview("p", "abc_def")), FORMATS) == []


def test_an_empty_carousel_yields_nothing():
    assert gallery_image_urls("", FORMATS) == []
    assert gallery_image_urls(carousel(), FORMATS) == []


def test_non_preview_hosts_are_ignored():
    html = '<gallery-carousel><img src="https://external-preview.example/x-v0-aaaaaa1.jpg"></gallery-carousel>'
    assert gallery_image_urls(html, FORMATS) == []


# -- full_res_from_preview: one URL at a time (shared with crossposts) -------


def test_a_single_preview_url_is_rebuilt():
    url = "https://preview.redd.it/some-photo-v0-3ps5r77zi1ah1.jpg?width=640&s=x"
    assert full_res_from_preview(url, FORMATS) == "https://i.redd.it/3ps5r77zi1ah1.jpg"


def test_a_cdn_subdomain_preview_url_is_still_rebuilt():
    # Crossposts render the shared image from a CDN mirror like cf.preview.redd.it.
    url = "https://cf.preview.redd.it/slug-v0-abc123def.jpg?width=320&auto=webp"
    assert full_res_from_preview(url, FORMATS) == "https://i.redd.it/abc123def.jpg"


def test_an_external_preview_host_is_not_mistaken_for_reddits():
    # external-preview.redd.it hosts previews of *off-site* links, not i.redd.it uploads.
    url = "https://external-preview.redd.it/slug-v0-abc123def.jpg?width=320"
    assert full_res_from_preview(url, FORMATS) is None


def test_a_non_preview_url_yields_none():
    assert full_res_from_preview("https://i.imgur.com/a.jpg", FORMATS) is None


def test_a_preview_url_in_a_disallowed_format_yields_none():
    url = "https://preview.redd.it/slug-v0-abc123def.gif?width=640"
    assert full_res_from_preview(url, FORMATS) is None


# -- can_handle -------------------------------------------------------------


def test_gallery_posts_with_a_permalink_are_claimed():
    assert GalleryHandler().can_handle(gallery_post()) is True


def test_a_gallery_with_no_permalink_is_left_alone():
    # There is no post-page to open, so the carousel is unreachable.
    assert GalleryHandler().can_handle(gallery_post(permalink=None)) is False


def test_other_post_types_are_left_alone():
    assert GalleryHandler().can_handle(gallery_post(type="image")) is False


# -- resolve ----------------------------------------------------------------


async def test_every_slide_becomes_a_candidate():
    ctx = FakeContext(carousel(preview("p", "aaaaaa1"), preview("p", "bbbbbb2")))
    candidates = await GalleryHandler().resolve(gallery_post(), ctx)
    assert [c.url for c in candidates] == [
        "https://i.redd.it/aaaaaa1.jpg",
        "https://i.redd.it/bbbbbb2.jpg",
    ]
    assert all(c.media_type is MediaType.GALLERY and c.ext == "jpg" for c in candidates)


async def test_the_carousel_is_read_from_the_posts_own_page():
    ctx = FakeContext(carousel(preview("p", "aaaaaa1")))
    post = gallery_post()
    await GalleryHandler().resolve(post, ctx)
    url, js, arg, wait_ms = ctx.evaluated[0]
    assert url == post.url
    assert js == JS_GALLERY
    assert arg == "g1"  # scopes the query to the main post, not sidebar thumbnails
    assert wait_ms == ctx.config.gallery_wait_ms


async def test_a_politeness_pause_follows_the_post_page_visit():
    ctx = FakeContext(carousel(preview("p", "aaaaaa1")))
    await GalleryHandler().resolve(gallery_post(), ctx)
    assert ctx.sleeps == 1


async def test_a_failed_page_visit_is_skipped_rather_than_raised():
    # One unreachable gallery must not abort a long job.
    ctx = FakeContext(RuntimeError("Timeout 60000ms exceeded"))
    post = gallery_post()
    assert await GalleryHandler().resolve(post, ctx) == []
    assert ctx.skips == [(post.url, "gallery failed: Timeout 60000ms exceeded")]


async def test_a_post_with_no_carousel_yields_nothing():
    ctx = FakeContext("")
    assert await GalleryHandler().resolve(gallery_post(), ctx) == []
    assert ctx.skips == []  # an empty carousel is not an error


async def test_a_null_evaluate_result_is_tolerated():
    ctx = FakeContext(None)
    assert await GalleryHandler().resolve(gallery_post(), ctx) == []


async def test_the_configured_formats_narrow_the_slides():
    ctx = FakeContext(
        carousel(
            preview("p", "aaaaaa1", ext="png"), preview("p", "bbbbbb2", ext="jpg")
        ),
        formats=("png",),
    )
    candidates = await GalleryHandler().resolve(gallery_post(), ctx)
    assert [c.url for c in candidates] == ["https://i.redd.it/aaaaaa1.png"]
