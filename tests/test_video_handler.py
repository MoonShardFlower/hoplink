"""
Tests for the video handler's two-tier strategy: packaged MP4 first, DASH fallback second.

Browser-free: the module's three helpers are plain functions, and `VideoHandler.resolve` only ever uses ``ctx.fetch``
and ``ctx.skip``. A small fake context exercises the whole strategy with no Playwright.
"""

from __future__ import annotations

import json
from typing import Any, List

from reddit_extract.core.browser import FetchResult
from reddit_extract.handlers.video import (
    VideoHandler,
    best_dash_video,
    best_mp4,
    vreddit_base,
)
from reddit_extract.models.media import MediaType
from reddit_extract.models.post import Post

BASE = "https://v.redd.it/abc123"


class FakeContext:
    """Stands in for ExtractionContext: one canned fetch result, recorded skips."""

    def __init__(self, result: FetchResult | None = None) -> None:
        self.result = result or FetchResult(ok=True, status=200, body=b"")
        self.fetches: List[str] = []
        self.skips: List[tuple[str, str]] = []

    async def fetch(self, url: str) -> FetchResult:
        self.fetches.append(url)
        return self.result

    async def skip(self, url: str, reason: str) -> None:
        self.skips.append((url, reason))


def video_post(**overrides: Any) -> Post:
    """One harvested video post, shaped like the modern UI's JS_HARVEST output."""
    data: dict[str, Any] = {
        "id": "v1",
        "type": "video",
        "permalink": "/r/videos/comments/v1/clip/",
        "content_href": BASE,
        "packaged_media": None,
        "player_src": None,
    }
    data.update(overrides)
    return Post.from_harvest(data)


def packaged(*heights: int) -> str:
    """Reddit's ``packaged-media-json``: one permutation per pre-muxed MP4 rendition."""
    return json.dumps(
        {
            "playbackMp4s": {
                "permutations": [
                    {
                        "source": {
                            "url": "{}/DASH_{}.mp4?source=fallback".format(BASE, h),
                            "dimensions": {"width": h, "height": h},
                        }
                    }
                    for h in heights
                ]
            }
        }
    )


def mpd(*heights: int, audio: bool = True) -> str:
    """A namespaced DASHPlaylist.mpd: one video Representation per height, plus optional audio."""
    videos = "".join(
        '<Representation id="v{h}" mimeType="video/mp4" height="{h}">'
        "<BaseURL>DASH_{h}.mp4</BaseURL></Representation>".format(h=h)
        for h in heights
    )
    audio_rep = (
        '<Representation id="a" mimeType="audio/mp4">'
        "<BaseURL>DASH_AUDIO_128.mp4</BaseURL></Representation>"
        if audio
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">'
        "<Period><AdaptationSet>{}{}</AdaptationSet></Period></MPD>"
    ).format(videos, audio_rep)


def manifest_result(xml: str) -> FetchResult:
    return FetchResult(
        ok=True, status=200, content_type="application/dash+xml", body=xml.encode()
    )


# -- best_mp4: reading the packaged renditions ------------------------------


def test_the_tallest_rendition_wins():
    assert best_mp4(packaged(220, 720, 480)) == (
        BASE + "/DASH_720.mp4?source=fallback",
        720,
    )


def test_absent_packaged_media_is_none():
    assert best_mp4(None) is None
    assert best_mp4("") is None


def test_invalid_json_is_none():
    # A malformed attribute must not take the whole post down; DASH can still rescue it.
    assert best_mp4("{not json") is None


def test_json_carrying_no_mp4_is_none():
    assert best_mp4(json.dumps({"playbackMp4s": {"permutations": []}})) is None
    assert best_mp4(json.dumps({"url": "https://i.redd.it/x.jpg"})) is None


def test_height_is_read_from_a_flat_field_when_there_are_no_dimensions():
    data = json.dumps({"url": BASE + "/DASH_480.mp4", "height": 480})
    assert best_mp4(data) == (BASE + "/DASH_480.mp4", 480)


def test_a_rendition_with_no_height_at_all_counts_as_zero():
    data = json.dumps(
        [{"url": BASE + "/DASH_x.mp4"}, {"url": BASE + "/DASH_1.mp4", "height": 1}]
    )
    # the height-less one sorts last rather than being dropped
    assert best_mp4(data) == (BASE + "/DASH_1.mp4", 1)


def test_a_lone_height_less_rendition_is_still_returned():
    assert best_mp4(json.dumps({"url": BASE + "/DASH_x.mp4"})) == (
        BASE + "/DASH_x.mp4",
        0,
    )


def test_renditions_nested_in_lists_are_found():
    data = json.dumps({"a": [{"b": [{"url": BASE + "/DASH_360.mp4", "height": 360}]}]})
    assert best_mp4(data) == (BASE + "/DASH_360.mp4", 360)


def test_non_mp4_urls_are_ignored():
    data = json.dumps(
        [
            {"url": "https://preview.redd.it/thumb.jpg", "height": 9999},
            {"url": BASE + "/DASH_240.mp4", "height": 240},
        ]
    )
    assert best_mp4(data) == (BASE + "/DASH_240.mp4", 240)


def test_a_non_string_url_is_ignored():
    assert best_mp4(json.dumps({"url": 42})) is None


def test_a_non_integer_height_falls_back_to_zero():
    data = json.dumps({"url": BASE + "/DASH_x.mp4", "dimensions": {"height": "480"}})
    assert best_mp4(data) == (BASE + "/DASH_x.mp4", 0)


def test_an_uppercase_extension_still_counts_as_mp4():
    assert (
        best_mp4(json.dumps({"url": BASE + "/DASH_480.MP4", "height": 480})) is not None
    )


# -- best_dash_video: reading the DASH manifest -----------------------------


def test_the_tallest_video_stream_wins():
    assert best_dash_video(mpd(240, 1080, 480), BASE) == BASE + "/DASH_1080.mp4"


def test_the_audio_representation_is_never_chosen():
    # DASH keeps audio in its own file, so picking it would save a soundtrack instead of a video.
    assert best_dash_video(mpd(480), BASE) == BASE + "/DASH_480.mp4"


def test_an_unparseable_manifest_is_none():
    assert best_dash_video("<MPD><unclosed>", BASE) is None


def test_a_manifest_with_no_video_representation_is_none():
    assert best_dash_video(mpd(audio=True), BASE) is None


def test_a_representation_without_a_base_url_is_skipped():
    xml = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet>'
        '<Representation id="v1" mimeType="video/mp4" height="1080"/>'
        '<Representation id="v2" mimeType="video/mp4" height="480">'
        "<BaseURL>DASH_480.mp4</BaseURL></Representation>"
        "</AdaptationSet></Period></MPD>"
    )
    # the taller stream names no file, so it cannot be downloaded
    assert best_dash_video(xml, BASE) == BASE + "/DASH_480.mp4"


def test_a_trailing_slash_on_the_base_is_not_doubled():
    assert best_dash_video(mpd(480), BASE + "/") == BASE + "/DASH_480.mp4"


def test_a_non_numeric_height_sorts_as_zero():
    xml = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet>'
        '<Representation mimeType="video/mp4" height="auto">'
        "<BaseURL>DASH_auto.mp4</BaseURL></Representation>"
        '<Representation mimeType="video/mp4" height="360">'
        "<BaseURL>DASH_360.mp4</BaseURL></Representation>"
        "</AdaptationSet></Period></MPD>"
    )
    assert best_dash_video(xml, BASE) == BASE + "/DASH_360.mp4"


def test_a_representation_with_a_height_but_no_mime_type_still_counts_as_video():
    xml = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet>'
        '<Representation height="720"><BaseURL>DASH_720.mp4</BaseURL></Representation>'
        "</AdaptationSet></Period></MPD>"
    )
    assert best_dash_video(xml, BASE) == BASE + "/DASH_720.mp4"


def test_an_empty_base_url_element_is_skipped():
    xml = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet>'
        '<Representation mimeType="video/mp4" height="720"><BaseURL>  </BaseURL></Representation>'
        "</AdaptationSet></Period></MPD>"
    )
    assert best_dash_video(xml, BASE) is None


# -- vreddit_base: finding the video id -------------------------------------


def test_the_base_comes_from_the_content_href():
    assert vreddit_base(video_post()) == BASE


def test_extra_path_beyond_the_id_is_dropped():
    post = video_post(content_href=BASE + "/DASH_480.mp4?source=fallback")
    assert vreddit_base(post) == BASE


def test_the_player_src_is_consulted_when_the_href_is_a_comment_link():
    post = video_post(
        content_href="https://www.reddit.com/r/videos/comments/v1/clip/",
        player_src=BASE + "/DASHPlaylist.mpd",
    )
    assert vreddit_base(post) == BASE


def test_an_empty_content_href_falls_through_to_the_player_src():
    assert vreddit_base(video_post(content_href=None, player_src=BASE)) == BASE


def test_a_post_with_no_vreddit_id_anywhere_is_none():
    post = video_post(content_href="https://youtube.com/watch?v=x", player_src=None)
    assert vreddit_base(post) is None


# -- can_handle -------------------------------------------------------------


def test_video_and_gif_posts_are_claimed():
    # Reddit's silent "gif" posts are v.redd.it videos wearing a different post-type.
    assert VideoHandler().can_handle(video_post(type="video")) is True
    assert VideoHandler().can_handle(video_post(type="gif")) is True


def test_other_post_types_are_left_alone():
    assert VideoHandler().can_handle(video_post(type="image")) is False


# -- resolve: packaged media is preferred -----------------------------------


async def test_a_packaged_rendition_is_used_without_touching_the_network():
    ctx = FakeContext()
    candidates = await VideoHandler().resolve(
        video_post(packaged_media=packaged(480)), ctx
    )
    assert [c.url for c in candidates] == [BASE + "/DASH_480.mp4?source=fallback"]
    assert ctx.fetches == []  # the DASH manifest is never requested


async def test_a_packaged_candidate_is_an_mp4_that_accepts_video_content_types():
    ctx = FakeContext()
    cand = (
        await VideoHandler().resolve(video_post(packaged_media=packaged(480)), ctx)
    )[0]
    assert cand.ext == "mp4"
    assert cand.media_type is MediaType.VIDEO
    # some CDN edges label MP4 bytes as application/octet-stream
    assert cand.content_prefixes == ("video/", "application/octet-stream")


# -- resolve: the DASH fallback ---------------------------------------------


async def test_a_post_without_packaged_media_falls_back_to_dash():
    ctx = FakeContext(manifest_result(mpd(480, 720)))
    candidates = await VideoHandler().resolve(video_post(), ctx)
    assert ctx.fetches == [BASE + "/DASHPlaylist.mpd"]
    assert [c.url for c in candidates] == [BASE + "/DASH_720.mp4"]


async def test_invalid_packaged_json_falls_back_to_dash_rather_than_failing():
    ctx = FakeContext(manifest_result(mpd(480)))
    candidates = await VideoHandler().resolve(video_post(packaged_media="{broken"), ctx)
    assert [c.url for c in candidates] == [BASE + "/DASH_480.mp4"]


# -- resolve: everything that can go wrong ----------------------------------


async def test_a_non_vreddit_post_is_skipped_with_a_reason():
    ctx = FakeContext()
    post = video_post(content_href="https://youtube.com/watch?v=x")
    assert await VideoHandler().resolve(post, ctx) == []
    assert ctx.fetches == []
    assert ctx.skips == [(post.url, "not a v.redd.it video (no packaged media)")]


async def test_an_unavailable_manifest_is_skipped_with_its_error():
    ctx = FakeContext(FetchResult(ok=False, status=0, error="error: Timeout"))
    assert await VideoHandler().resolve(video_post(), ctx) == []
    assert ctx.skips == [(BASE, "DASH manifest unavailable (error: Timeout)")]


async def test_a_manifest_failure_with_no_error_text_reports_its_status():
    ctx = FakeContext(FetchResult(ok=False, status=404))
    await VideoHandler().resolve(video_post(), ctx)
    assert ctx.skips == [(BASE, "DASH manifest unavailable (404)")]


async def test_an_ok_response_with_no_body_is_skipped():
    ctx = FakeContext(FetchResult(ok=True, status=200, body=None))
    assert await VideoHandler().resolve(video_post(), ctx) == []
    assert "DASH manifest unavailable" in ctx.skips[0][1]


async def test_a_manifest_with_no_video_rendition_is_skipped():
    ctx = FakeContext(manifest_result(mpd(audio=True)))
    assert await VideoHandler().resolve(video_post(), ctx) == []
    assert ctx.skips == [(BASE, "no video rendition in DASH manifest")]


async def test_undecodable_manifest_bytes_do_not_raise():
    # 'replace' keeps a mis-encoded manifest from crashing the job; it just yields no rendition.
    ctx = FakeContext(FetchResult(ok=True, status=200, body=b"\xff\xfe not xml"))
    assert await VideoHandler().resolve(video_post(), ctx) == []
    assert ctx.skips == [(BASE, "no video rendition in DASH manifest")]
