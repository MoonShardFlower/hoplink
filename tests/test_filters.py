"""
Tests for post-level filtering and the harvested Post fields it reads.

Browser-free: The harvest dicts mirror the shapes Reddit really serves.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reddit_extract.models.filters import PostFilter, coerce_datetime, coerce_str_list
from reddit_extract.models.post import Post


def make_post(**overrides) -> Post:
    """A harvested post with sensible defaults, overridable per test."""
    data = {
        "id": "t3_abc",
        "type": "image",
        "permalink": "/r/pics/comments/abc/title/",
        "author": "alice",
        "title": "A photo of a cat",
        "subreddit": "r/pics",
        "created": "2026-07-16T11:45:34.060000+0000",
        "score": "1000",
        "comment_count": "50",
        "flair": "Politics",
        "stickied": False,
    }
    data.update(overrides)
    return Post.from_harvest(data)


# -- Post: the fields filtering depends on --------------------------------


def test_modern_iso_timestamp_parses():
    # The modern UI stamps ISO text; this used to fall through and yield None.
    post = make_post(created="2026-07-16T11:45:34.060000+0000")
    assert post.created_at == datetime(
        2026, 7, 16, 11, 45, 34, 60000, tzinfo=timezone.utc
    )
    assert post.created == "2026-07-16T11:45:34.060000+00:00"


def test_legacy_epoch_ms_timestamp_parses():
    post = make_post(created="1646697768000")
    assert post.created_at == datetime(2022, 3, 8, 0, 2, 48, tzinfo=timezone.utc)


def test_unparseable_timestamp_is_none():
    assert make_post(created="not-a-date").created_at is None
    assert make_post(created=None).created_at is None


def test_numeric_fields_parse():
    post = make_post(score="2757", comment_count="310")
    assert (post.score, post.comment_count) == (2757, 310)


def test_missing_numeric_fields_stay_none_not_zero():
    # A hidden score must not read as 0, or min_score=0 would silently keep it.
    post = make_post(score=None, comment_count=None)
    assert post.score is None and post.comment_count is None


def test_empty_flair_becomes_none():
    assert make_post(flair="").flair is None
    assert make_post(flair="  Politics  ").flair == "Politics"


def test_stickied_is_boolean():
    assert make_post(stickied=True).stickied is True
    assert make_post(stickied=False).stickied is False


# -- coercion helpers ------------------------------------------------------


def test_coerce_str_list_splits_and_lowercases():
    assert coerce_str_list("Alice, BOB ,, ") == ("alice", "bob")
    assert coerce_str_list(["Alice", "Bob"]) == ("alice", "bob")
    assert coerce_str_list(None) == ()


def test_coerce_datetime_forms():
    assert coerce_datetime("2026-01-01") == datetime(2026, 1, 1, tzinfo=timezone.utc)
    # a naive datetime is read as UTC rather than local time
    assert coerce_datetime(datetime(2026, 1, 1)) == datetime(
        2026, 1, 1, tzinfo=timezone.utc
    )
    assert coerce_datetime(None) is None


def test_coerce_datetime_rejects_garbage():
    with pytest.raises(ValueError, match="ISO-8601"):
        coerce_datetime("last tuesday", field_name="after")


# -- the empty filter ------------------------------------------------------


def test_empty_filter_is_inactive_and_keeps_everything():
    filt = PostFilter()
    assert filt.active is False
    assert filt.accepts(make_post()) is True
    assert filt.rejection(make_post()) is None


def test_any_predicate_makes_it_active():
    assert PostFilter(min_score=1).active is True
    assert PostFilter(skip_stickied=True).active is True


# -- score / comments ------------------------------------------------------


def test_min_score():
    filt = PostFilter(min_score=500)
    assert filt.accepts(make_post(score="500")) is True  # boundary is inclusive
    assert filt.accepts(make_post(score="499")) is False
    assert "below min 500" in (filt.rejection(make_post(score="499")) or "")


def test_min_comments():
    filt = PostFilter(min_comments=100)
    assert filt.accepts(make_post(comment_count="100")) is True
    assert filt.accepts(make_post(comment_count="99")) is False


def test_unknown_score_is_rejected_with_a_reason():
    # Rejecting is the honest reading of "score >= N", and the reason keeps it visible.
    filt = PostFilter(min_score=10)
    assert filt.rejection(make_post(score=None)) == "score unknown (min 10)"


def test_unknown_comment_count_is_rejected():
    filt = PostFilter(min_comments=10)
    assert (
        filt.rejection(make_post(comment_count=None))
        == "comment count unknown (min 10)"
    )


# -- title -----------------------------------------------------------------


def test_title_include_substring_is_case_insensitive():
    filt = PostFilter(title_include="CAT")
    assert filt.accepts(make_post(title="A photo of a cat")) is True
    assert filt.accepts(make_post(title="A photo of a dog")) is False


def test_title_exclude_substring():
    filt = PostFilter(title_exclude="meta")
    assert filt.accepts(make_post(title="[META] rules")) is False
    assert filt.accepts(make_post(title="A cat")) is True


def test_substring_mode_does_not_treat_input_as_regex():
    # Without title_regex, "c.t" is a literal, so it must not match "cat".
    filt = PostFilter(title_include="c.t")
    assert filt.accepts(make_post(title="A photo of a cat")) is False
    assert filt.accepts(make_post(title="A photo of a c.t")) is True


def test_title_regex_mode():
    filt = PostFilter(title_include=r"^\[OC\]", title_regex=True)
    assert filt.accepts(make_post(title="[OC] my shot")) is True
    assert filt.accepts(make_post(title="not [OC] my shot")) is False


def test_invalid_regex_raises_at_construction():
    with pytest.raises(ValueError, match="invalid title regex"):
        PostFilter(title_include="[unclosed", title_regex=True)


def test_include_and_exclude_combine():
    filt = PostFilter(title_include="cat", title_exclude="meta")
    assert filt.accepts(make_post(title="a cat")) is True
    assert filt.accepts(make_post(title="[meta] a cat")) is False


# -- authors ---------------------------------------------------------------


def test_author_allow_list():
    filt = PostFilter(authors="alice,bob")
    assert filt.accepts(make_post(author="Alice")) is True  # case-insensitive
    assert filt.accepts(make_post(author="carol")) is False


def test_author_deny_list():
    filt = PostFilter(block_authors=["spammer"])
    assert filt.accepts(make_post(author="Spammer")) is False
    assert filt.accepts(make_post(author="alice")) is True


def test_deny_list_wins_over_allow_list():
    filt = PostFilter(authors="alice", block_authors="alice")
    assert "blocked" in (filt.rejection(make_post(author="alice")) or "")


# -- flair -----------------------------------------------------------------


def test_flair_match_is_case_insensitive():
    filt = PostFilter(flairs="politics")
    assert filt.accepts(make_post(flair="Politics")) is True
    assert filt.accepts(make_post(flair="Software")) is False


def test_flair_filter_rejects_unflaired_posts():
    filt = PostFilter(flairs="politics")
    assert filt.accepts(make_post(flair="")) is False


# -- stickied --------------------------------------------------------------


def test_skip_stickied():
    filt = PostFilter(skip_stickied=True)
    assert filt.rejection(make_post(stickied=True)) == "post is stickied"
    assert filt.accepts(make_post(stickied=False)) is True


def test_stickied_kept_when_not_skipping():
    assert PostFilter(min_score=1).accepts(make_post(stickied=True)) is True


# -- date window -----------------------------------------------------------


def test_after_is_inclusive():
    filt = PostFilter(after="2026-07-16")
    assert filt.accepts(make_post(created="2026-07-16T00:00:00+00:00")) is True
    assert filt.accepts(make_post(created="2026-07-15T23:59:59+00:00")) is False


def test_before_is_exclusive():
    filt = PostFilter(before="2026-07-16")
    assert filt.accepts(make_post(created="2026-07-15T23:59:59+00:00")) is True
    assert filt.accepts(make_post(created="2026-07-16T00:00:00+00:00")) is False


def test_date_window_combines():
    filt = PostFilter(after="2026-01-01", before="2027-01-01")
    assert filt.accepts(make_post(created="2026-07-16T11:45:34+00:00")) is True
    assert filt.accepts(make_post(created="2025-12-31T23:00:00+00:00")) is False


def test_date_window_works_on_legacy_epoch_timestamps():
    filt = PostFilter(after="2022-01-01", before="2023-01-01")
    assert filt.accepts(make_post(created="1646697768000")) is True  # 2022-03-08


def test_unknown_creation_time_is_rejected_when_a_window_is_set():
    filt = PostFilter(after="2026-01-01")
    assert filt.rejection(make_post(created=None)) == "creation time unknown"


def test_missing_creation_time_is_fine_without_a_window():
    assert PostFilter(min_score=1).accepts(make_post(created=None)) is True


def test_after_must_precede_before():
    with pytest.raises(ValueError, match="must be earlier than"):
        PostFilter(after="2027-01-01", before="2026-01-01")


# -- validation ------------------------------------------------------------


@pytest.mark.parametrize("field", ["min_score", "min_comments", "min_gallery"])
def test_negative_counts_rejected(field):
    with pytest.raises(ValueError, match="must be >= 0"):
        PostFilter(**{field: -1})


# -- min_gallery (decided after resolve) -----------------------------------


def test_min_gallery_rejects_small_galleries():
    filt = PostFilter(min_gallery=3)
    gallery = make_post(type="gallery")
    assert filt.rejection_after_resolve(gallery, 2) is not None
    assert filt.rejection_after_resolve(gallery, 3) is None


def test_min_gallery_ignores_non_gallery_posts():
    # A single image isn't a small gallery; --min-gallery must leave it alone.
    filt = PostFilter(min_gallery=3)
    assert filt.rejection_after_resolve(make_post(type="image"), 1) is None


def test_min_gallery_is_not_checked_at_listing_time():
    # The listing card can't report a gallery's size, so rejection() must stay quiet about it.
    filt = PostFilter(min_gallery=99)
    assert filt.rejection(make_post(type="gallery")) is None


def test_no_min_gallery_accepts_any_size():
    assert PostFilter().rejection_after_resolve(make_post(type="gallery"), 1) is None


# -- combinations ----------------------------------------------------------


def test_every_predicate_must_pass():
    filt = PostFilter(min_score=500, authors="alice", title_include="cat")
    assert filt.accepts(make_post()) is True
    assert filt.accepts(make_post(score="10")) is False
    assert filt.accepts(make_post(author="bob")) is False
    assert filt.accepts(make_post(title="a dog")) is False


def test_rejection_reason_reports_the_first_failure():
    filt = PostFilter(min_score=500, block_authors="alice")
    # the author check runs first, so that's the reason given
    assert "blocked" in (filt.rejection(make_post(score="1", author="alice")) or "")
