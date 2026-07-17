"""
Tests for the simple handlers (image, link, text) and the built-in chain.

Browser-free: Handlers decide from the harvested post attributes and the job's accepted formats alone.
"""

from __future__ import annotations

from typing import Any, List

from reddit_extract.core.context import ExtractionContext
from reddit_extract.handlers import (
    GalleryHandler,
    ImageHandler,
    LinkImageHandler,
    TextHandler,
    VideoHandler,
    default_handlers,
)
from reddit_extract.handlers.base import MediaHandler
from reddit_extract.models.media import MediaCandidate, MediaType
from reddit_extract.models.post import Post

FORMATS = ("jpg", "jpeg", "png", "webp")


class FakeContext:
    """Stands in for ExtractionContext: just the accepted formats and the URL helper."""

    def __init__(self, formats=FORMATS) -> None:
        self.formats = frozenset(formats)

    def extension_of(self, url: str) -> str:
        return ExtractionContext.extension_of(url)


def post(**overrides: Any) -> Post:
    """One harvested post."""
    data: dict[str, Any] = {
        "id": "p1",
        "type": "image",
        "content_href": "https://i.redd.it/a.jpg",
    }
    data.update(overrides)
    return Post.from_harvest(data)


# -- ImageHandler -----------------------------------------------------------


def test_image_posts_with_a_content_href_are_claimed():
    assert ImageHandler().can_handle(post()) is True


def test_an_image_post_with_no_href_is_left_alone():
    assert ImageHandler().can_handle(post(content_href=None)) is False


def test_non_image_posts_are_left_alone():
    assert ImageHandler().can_handle(post(type="video")) is False


async def test_the_cards_href_is_already_the_original_file():
    # i.redd.it serves the full-resolution upload, so no post-page visit is needed.
    candidates = await ImageHandler().resolve(post(), FakeContext())
    assert [(c.url, c.ext, c.media_type) for c in candidates] == [
        ("https://i.redd.it/a.jpg", "jpg", MediaType.IMAGE)
    ]


async def test_an_image_outside_the_accepted_formats_yields_nothing():
    handler_ctx = FakeContext(formats=("png",))
    assert await ImageHandler().resolve(post(), handler_ctx) == []


async def test_a_gif_image_post_is_dropped_unless_gifs_are_enabled():
    gif = post(content_href="https://i.redd.it/a.gif")
    assert await ImageHandler().resolve(gif, FakeContext()) == []
    assert len(await ImageHandler().resolve(gif, FakeContext(formats=("gif",)))) == 1


# -- LinkImageHandler -------------------------------------------------------


def test_link_posts_with_a_content_href_are_claimed():
    assert LinkImageHandler().can_handle(post(type="link")) is True


def test_a_link_post_with_no_href_is_left_alone():
    assert LinkImageHandler().can_handle(post(type="link", content_href=None)) is False


def test_non_link_posts_are_left_alone_by_the_link_handler():
    assert LinkImageHandler().can_handle(post(type="image")) is False


async def test_a_link_straight_to_an_image_file_is_kept():
    link = post(type="link", content_href="https://example.com/photo.png")
    candidates = await LinkImageHandler().resolve(link, FakeContext())
    assert [(c.url, c.ext, c.media_type) for c in candidates] == [
        ("https://example.com/photo.png", "png", MediaType.LINK)
    ]


async def test_a_link_to_a_web_page_is_not_downloaded():
    # Following it would save an HTML document, not media.
    link = post(type="link", content_href="https://example.com/article")
    assert await LinkImageHandler().resolve(link, FakeContext()) == []


async def test_a_link_to_a_disallowed_image_format_is_not_downloaded():
    link = post(type="link", content_href="https://example.com/loop.gif")
    assert await LinkImageHandler().resolve(link, FakeContext()) == []


# -- TextHandler ------------------------------------------------------------


def test_text_posts_are_claimed():
    assert TextHandler().can_handle(post(type="text")) is True


def test_non_text_posts_are_left_alone_by_the_text_handler():
    assert TextHandler().can_handle(post(type="image")) is False


async def test_a_text_post_produces_no_candidates():
    assert await TextHandler().resolve(post(type="text"), FakeContext()) == []


def test_the_text_handler_is_the_only_metadata_only_one():
    # metadata_only is what lets a self post count as matched despite yielding no files.
    assert TextHandler.metadata_only is True
    assert [h.name for h in default_handlers() if h.metadata_only] == ["TextHandler"]


# -- the built-in chain -----------------------------------------------------


def test_the_default_chain_is_in_selection_order():
    assert [type(h) for h in default_handlers()] == [
        ImageHandler,
        GalleryHandler,
        VideoHandler,
        LinkImageHandler,
        TextHandler,
    ]


def test_the_link_handler_comes_after_the_specific_ones():
    # LinkImageHandler is the catch-all for external hosts; it must not shadow i.redd.it posts.
    order = [type(h) for h in default_handlers()]
    assert order.index(LinkImageHandler) > order.index(ImageHandler)
    assert order.index(LinkImageHandler) > order.index(VideoHandler)


def test_each_call_hands_out_fresh_instances():
    # Two extractors must never share handler state.
    assert default_handlers()[0] is not default_handlers()[0]


def test_every_default_handler_declares_a_single_media_type():
    for handler in default_handlers():
        assert isinstance(handler.media_type, MediaType)
        assert handler.media_type.name is not None  # not a combined flag


# -- MediaHandler.name ------------------------------------------------------


def test_a_handler_reports_its_class_name():
    assert ImageHandler().name == "ImageHandler"


def test_a_custom_handler_reports_its_own_name():
    class StickerHandler(MediaHandler):
        media_type = MediaType.LINK

        def can_handle(self, post: Post) -> bool:
            return False

        async def resolve(self, post: Post, ctx: Any) -> List[MediaCandidate]:
            return []

    assert StickerHandler().name == "StickerHandler"
