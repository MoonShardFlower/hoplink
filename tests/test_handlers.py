"""
Tests for the simple handlers (image, link, text) and the built-in chain.

Browser-free: Handlers decide from the harvested post attributes and the job's accepted formats alone.
"""

from __future__ import annotations

from typing import Any, List

from reddit_extract.core.context import ExtractionContext
from reddit_extract.handlers import (
    CrosspostHandler,
    GalleryHandler,
    ImageHandler,
    LinkImageHandler,
    PollHandler,
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


class FakePageContext(FakeContext):
    """A fuller fake: serves canned page text for handlers that visit a post page."""

    def __init__(self, page_result="", formats=FORMATS) -> None:
        super().__init__(formats)
        self.page_result = page_result
        self.evaluated: List[tuple[str, str, Any]] = []
        self.skips: List[tuple[str, str]] = []
        self.sleeps = 0

    async def evaluate_on(
        self, url: str, js: str, arg: Any = None, wait_ms: int = 0
    ) -> Any:
        self.evaluated.append((url, js, arg))
        if isinstance(self.page_result, Exception):
            raise self.page_result
        return self.page_result

    async def skip(self, url: str, reason: str) -> None:
        self.skips.append((url, reason))

    async def sleep(self) -> None:
        self.sleeps += 1


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


def text_post(**overrides: Any) -> Post:
    """One harvested self post."""
    data: dict[str, Any] = {
        "id": "t3_t1",
        "type": "text",
        "permalink": "/r/tifu/comments/t1/story/",
        "author": "bob",
        "title": "TIFU by testing",
        "subreddit": "r/tifu",
        "created": "2026-07-16T11:45:34.060000+0000",
        "score": "42",
    }
    data.update(overrides)
    return Post.from_harvest(data)


def test_text_posts_are_claimed():
    assert TextHandler().can_handle(post(type="text")) is True


def test_non_text_posts_are_left_alone_by_the_text_handler():
    assert TextHandler().can_handle(post(type="image")) is False


async def test_a_text_post_becomes_a_markdown_file():
    ctx = FakePageContext("Here is the body of my story.")
    candidates = await TextHandler().resolve(text_post(), ctx)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.ext == "md"
    assert cand.media_type is MediaType.TEXT
    assert cand.url == "https://www.reddit.com/r/tifu/comments/t1/story/"
    doc = cand.body.decode("utf-8")
    assert "Here is the body of my story." in doc
    assert 'title: "TIFU by testing"' in doc
    assert 'author: "bob"' in doc
    assert "# TIFU by testing" in doc


async def test_a_text_post_body_is_read_from_its_own_page():
    ctx = FakePageContext("body")
    p = text_post()
    await TextHandler().resolve(p, ctx)
    url, _js, arg = ctx.evaluated[0]
    assert url == p.url
    assert arg == p.id  # scopes the body read to the main post


async def test_a_text_post_with_no_body_still_records_its_metadata():
    ctx = FakePageContext("")  # empty self-text (title-only post)
    cand = (await TextHandler().resolve(text_post(), ctx))[0]
    doc = cand.body.decode("utf-8")
    assert 'title: "TIFU by testing"' in doc


async def test_a_failed_page_visit_still_yields_a_metadata_document():
    # One unreachable post must not lose the record; the body is just empty.
    ctx = FakePageContext(RuntimeError("Timeout"))
    cand = (await TextHandler().resolve(text_post(), ctx))[0]
    assert cand.ext == "md"
    assert 'author: "bob"' in cand.body.decode("utf-8")


# -- PollHandler ------------------------------------------------------------


def test_poll_posts_are_claimed():
    assert PollHandler().can_handle(post(type="poll")) is True


def test_non_poll_posts_are_left_alone_by_the_poll_handler():
    assert PollHandler().can_handle(post(type="text")) is False


async def test_a_poll_post_becomes_a_metadata_markdown_file():
    # A poll has no downloadable media, so it is archived from what the harvest already knows.
    cand = (
        await PollHandler().resolve(post(type="poll", title="Best pet?"), FakeContext())
    )[0]
    assert cand.ext == "md"
    assert cand.media_type is MediaType.POLL
    assert 'title: "Best pet?"' in cand.body.decode("utf-8")


# -- CrosspostHandler -------------------------------------------------------


def cross_post(**overrides: Any) -> Post:
    """One harvested crosspost."""
    data: dict[str, Any] = {
        "id": "t3_x1",
        "type": "crosspost",
        "permalink": "/r/pics/comments/x1/shared/",
    }
    data.update(overrides)
    return Post.from_harvest(data)


def test_crossposts_with_a_permalink_are_claimed():
    assert CrosspostHandler().can_handle(cross_post()) is True


def test_a_crosspost_with_no_permalink_is_left_alone():
    assert CrosspostHandler().can_handle(cross_post(permalink=None)) is False


def test_other_post_types_are_left_alone_by_the_crosspost_handler():
    assert CrosspostHandler().can_handle(cross_post(type="image")) is False


async def test_a_crosspost_of_an_image_is_rebuilt_to_full_resolution():
    ctx = FakePageContext(
        {
            "gallery": None,
            "packaged": None,
            "player_src": None,
            "content_href": "/r/aww/comments/orig/x/",
            "img_src": "https://preview.redd.it/some-slug-v0-abc123def.jpg?width=640&s=z",
        }
    )
    candidates = await CrosspostHandler().resolve(cross_post(), ctx)
    assert [(c.url, c.media_type) for c in candidates] == [
        ("https://i.redd.it/abc123def.jpg", MediaType.IMAGE)
    ]


async def test_a_crosspost_of_a_gallery_yields_every_slide():
    carousel = (
        "<gallery-carousel>"
        '<img src="https://preview.redd.it/p-v0-aaaaaa1.jpg?width=640">'
        '<img src="https://preview.redd.it/p-v0-bbbbbb2.jpg?width=640">'
        "</gallery-carousel>"
    )
    ctx = FakePageContext({"gallery": carousel, "img_src": None})
    candidates = await CrosspostHandler().resolve(cross_post(), ctx)
    assert [c.url for c in candidates] == [
        "https://i.redd.it/aaaaaa1.jpg",
        "https://i.redd.it/bbbbbb2.jpg",
    ]
    assert all(c.media_type is MediaType.GALLERY for c in candidates)


async def test_a_crosspost_of_a_packaged_video_yields_the_mp4():
    packaged = (
        '{"m":{"x":{"source":{"url":"https://v.redd.it/xyz/720.mp4",'
        '"dimensions":{"height":720}}}}}'
    )
    ctx = FakePageContext(
        {"gallery": None, "img_src": None, "packaged": packaged, "player_src": None}
    )
    candidates = await CrosspostHandler().resolve(cross_post(), ctx)
    assert [(c.url, c.media_type, c.ext) for c in candidates] == [
        ("https://v.redd.it/xyz/720.mp4", MediaType.VIDEO, "mp4")
    ]


async def test_a_crosspost_with_no_recognized_media_is_skipped():
    ctx = FakePageContext({"gallery": None, "packaged": None, "img_src": None})
    assert await CrosspostHandler().resolve(cross_post(), ctx) == []
    assert ctx.skips and "no recognized media" in ctx.skips[0][1]


async def test_a_failed_crosspost_page_visit_is_skipped_rather_than_raised():
    ctx = FakePageContext(RuntimeError("Timeout 60000ms exceeded"))
    p = cross_post()
    assert await CrosspostHandler().resolve(p, ctx) == []
    assert ctx.skips == [(p.url, "crosspost failed: Timeout 60000ms exceeded")]


# -- the built-in chain -----------------------------------------------------


def test_the_default_chain_is_in_selection_order():
    assert [type(h) for h in default_handlers()] == [
        ImageHandler,
        GalleryHandler,
        VideoHandler,
        LinkImageHandler,
        CrosspostHandler,
        TextHandler,
        PollHandler,
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
