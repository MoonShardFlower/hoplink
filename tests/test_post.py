"""Tests for the normalized Post, which both listing UIs have to funnel into.

`test_filters` already covers the fields filtering reads. This file covers the normalization
itself: the two timestamp formats, the numeric parsing, and the permalink.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reddit_extract.models.post import Post


def harvest(**overrides) -> Post:
    """A post built from one harvested attribute dict."""
    data = {"id": "t3_abc", "type": "image"}
    data.update(overrides)
    return Post.from_harvest(data)


# -- from_harvest: the basics -----------------------------------------------


def test_the_harvested_attributes_are_carried_over():
    post = harvest(
        permalink="/r/pics/comments/abc/t/",
        content_href="https://i.redd.it/a.jpg",
        author="alice",
        title="a photo",
        domain="i.redd.it",
    )
    assert post.id == "t3_abc"
    assert post.type == "image"
    assert post.permalink == "/r/pics/comments/abc/t/"
    assert post.content_href == "https://i.redd.it/a.jpg"
    assert post.author == "alice"
    assert post.title == "a photo"
    assert post.domain == "i.redd.it"


def test_a_post_with_no_id_gets_an_empty_one_rather_than_none():
    assert Post.from_harvest({}).id == ""


def test_absent_attributes_stay_none():
    post = Post.from_harvest({"id": "a"})
    assert (post.type, post.permalink, post.content_href, post.author) == (None,) * 4
    assert (post.title, post.subreddit, post.domain, post.flair) == (None,) * 4


def test_the_full_harvest_is_kept_for_custom_handlers():
    # VideoHandler reads player_src off it; a custom handler may read anything else.
    post = harvest(player_src="https://v.redd.it/x/DASHPlaylist.mpd")
    assert post.raw["player_src"] == "https://v.redd.it/x/DASHPlaylist.mpd"


def test_the_raw_mapping_is_copied_not_aliased():
    data = {"id": "a", "type": "image"}
    post = Post.from_harvest(data)
    data["type"] = "video"
    assert post.raw["type"] == "image"


def test_packaged_media_is_lifted_off_the_harvest():
    assert harvest(packaged_media='{"x": 1}').packaged_media_json == '{"x": 1}'


def test_a_post_is_frozen():
    with pytest.raises(Exception):
        harvest().id = "other"  # type: ignore[misc]


# -- from_harvest: the subreddit prefix -------------------------------------


@pytest.mark.parametrize("given", ["r/pics", "R/pics", "  r/pics  ", "pics"])
def test_the_subreddit_prefix_is_stripped(given):
    # The modern UI stamps subreddit-prefixed-name; the storage key wants the bare name.
    assert harvest(subreddit=given).subreddit == "pics"


def test_an_absent_subreddit_stays_none():
    assert harvest(subreddit=None).subreddit is None
    assert harvest(subreddit="").subreddit is None
    assert harvest(subreddit="   ").subreddit is None


# -- from_harvest: numbers --------------------------------------------------


def test_scores_and_comment_counts_parse():
    post = harvest(score="1000", comment_count="50")
    assert post.score == 1000
    assert post.comment_count == 50


def test_numbers_are_parsed_from_whitespace_padded_text():
    assert harvest(score="  1000  ").score == 1000


def test_a_negative_score_parses():
    assert harvest(score="-5").score == -5


def test_an_already_numeric_value_parses():
    # The legacy UI hands back strings, but a custom harvest may not.
    assert harvest(score=1000).score == 1000


def test_a_missing_number_stays_none_rather_than_zero():
    # A hidden score is not the same as a score of 0, and min_score must tell them apart.
    assert harvest(score=None).score is None
    assert harvest().comment_count is None


@pytest.mark.parametrize("value", ["", "   ", "1.2k", "many", "12,000", "1.5", []])
def test_an_unparseable_number_stays_none(value):
    assert harvest(score=value).score is None


# -- from_harvest: flair and stickied ---------------------------------------


def test_flair_text_is_trimmed():
    assert harvest(flair="  Politics  ").flair == "Politics"


def test_an_unflaired_post_has_no_flair():
    assert harvest(flair=None).flair is None
    assert harvest(flair="   ").flair is None


def test_stickied_is_always_a_boolean():
    # The legacy UI reports a CSS class check, the modern one an attribute presence check.
    assert harvest(stickied=True).stickied is True
    assert harvest(stickied=False).stickied is False
    assert harvest(stickied=None).stickied is False
    assert harvest().stickied is False


# -- created_at: both listing UIs -------------------------------------------


def test_the_modern_uis_iso_timestamp_parses():
    post = harvest(created="2026-07-16T11:45:34.060000+0000")
    assert post.created_at == datetime(
        2026, 7, 16, 11, 45, 34, 60000, tzinfo=timezone.utc
    )


def test_the_legacy_uis_epoch_milliseconds_parse():
    # 1_768_000_000_000 ms == 2026-01-09T23:06:40Z
    assert harvest(created="1768000000000").created_at == datetime(
        2026, 1, 9, 23, 6, 40, tzinfo=timezone.utc
    )


def test_a_naive_iso_timestamp_is_assumed_to_be_utc():
    # Reddit serves UTC; a naive stamp compared against an aware filter bound would raise.
    parsed = harvest(created="2026-07-16T11:45:34").created_at
    assert parsed == datetime(2026, 7, 16, 11, 45, 34, tzinfo=timezone.utc)
    assert parsed.tzinfo is not None


def test_an_offset_timestamp_keeps_its_offset():
    assert harvest(created="2026-07-16T11:45:34+02:00").created_at == datetime(
        2026, 7, 16, 9, 45, 34, tzinfo=timezone.utc
    )


def test_a_missing_timestamp_is_none():
    assert harvest(created=None).created_at is None
    assert harvest(created="").created_at is None


@pytest.mark.parametrize("value", ["not a date", "2026-13-45", "yesterday"])
def test_an_unparseable_timestamp_is_none(value):
    assert harvest(created=value).created_at is None


def test_an_out_of_range_epoch_is_none():
    assert harvest(created="9" * 30).created_at is None


# -- created ----------------------------------------------------------------


def test_created_renders_the_timestamp_as_iso_text():
    assert harvest(created="1768000000000").created == "2026-01-09T23:06:40+00:00"


def test_created_normalizes_both_uis_to_the_same_text():
    # A manifest must not record two formats depending on which UI Reddit served.
    modern = harvest(created="2026-01-09T23:06:40+00:00").created
    legacy = harvest(created="1768000000000").created
    assert modern == legacy


def test_created_is_none_when_the_time_is_unknown():
    assert harvest(created=None).created is None
    assert harvest(created="not a date").created is None


# -- url --------------------------------------------------------------------


def test_a_relative_permalink_is_made_absolute():
    assert harvest(permalink="/r/pics/comments/abc/t/").url == (
        "https://www.reddit.com/r/pics/comments/abc/t/"
    )


def test_an_absolute_permalink_is_left_alone():
    assert harvest(permalink="https://www.reddit.com/r/pics/comments/abc/t/").url == (
        "https://www.reddit.com/r/pics/comments/abc/t/"
    )


def test_an_http_permalink_is_not_prefixed_twice():
    assert harvest(permalink="http://old.reddit.com/r/pics/comments/abc/t/").url == (
        "http://old.reddit.com/r/pics/comments/abc/t/"
    )


def test_a_post_with_no_permalink_has_no_url():
    assert harvest(permalink=None).url is None
    assert harvest(permalink="").url is None
