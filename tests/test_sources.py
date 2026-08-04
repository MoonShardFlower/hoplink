"""Tests for sources: what each one points at, and how free-form text becomes one."""

from __future__ import annotations

import pytest

from hoplink.models.source import (
    MultiReddit,
    Subreddit,
    UserProfile,
    parse_source,
    parse_subreddit,
    parse_username,
)

# -- Subreddit: the listing URL ---------------------------------------------


def test_hot_is_the_bare_listing():
    # /r/x/hot/ works, but /r/x/ is the canonical form Reddit itself links to.
    assert Subreddit("pics", sort="hot").url == "https://www.reddit.com/r/pics/"


def test_new_and_rising_hang_off_the_sort_name():
    assert Subreddit("pics", sort="new").url == "https://www.reddit.com/r/pics/new/"
    assert (
        Subreddit("pics", sort="rising").url == "https://www.reddit.com/r/pics/rising/"
    )


def test_top_carries_the_time_window():
    assert (
        Subreddit("pics", sort="top", time_filter="month").url
        == "https://www.reddit.com/r/pics/top/?t=month"
    )


def test_the_time_window_is_ignored_by_sorts_that_have_none():
    assert Subreddit("pics", sort="new", time_filter="week").url.endswith("/new/")


def test_new_is_the_default_sort():
    assert Subreddit("pics").sort == "new"
    assert Subreddit("pics").limit == 100


# -- listing_url: the flair filter Reddit applies for us --------------------


def test_a_listing_url_without_a_flair_is_the_plain_url():
    sub = Subreddit("pics", sort="new")
    assert sub.listing_url() == sub.url
    assert sub.listing_url(flair=None) == sub.url
    assert sub.listing_url(flair="") == sub.url


def test_a_subreddit_pushes_the_flair_into_the_listing_query():
    assert (
        Subreddit("pics", sort="new").listing_url(flair="art")
        == "https://www.reddit.com/r/pics/new/?f=flair_name%3A%22art%22"
    )


def test_a_flair_joins_a_url_that_already_has_a_query():
    # top/ carries ?t=..., so the flair has to be appended with & rather than ?.
    assert (
        Subreddit("pics", sort="top", time_filter="week").listing_url(flair="art")
        == "https://www.reddit.com/r/pics/top/?t=week&f=flair_name%3A%22art%22"
    )


def test_a_flair_containing_spaces_is_encoded():
    assert (
        Subreddit("pics")
        .listing_url(flair="Daily Thread")
        .endswith("f=flair_name%3A%22Daily%20Thread%22")
    )


@pytest.mark.parametrize(
    "source",
    [
        MultiReddit(("pics", "art")),
        UserProfile("spez"),
    ],
)
def test_sources_whose_listing_ignores_the_flair_filter_keep_their_plain_url(source):
    # Reddit serves these on markup that drops ?f= and returns everything, so the flair must stay
    # with the client-side PostFilter. Returning url unchanged is how the engine detects that.
    assert source.listing_url(flair="art") == source.url


# -- Subreddit: normalization and validation --------------------------------


@pytest.mark.parametrize(
    "given",
    [
        "pics",
        "r/pics",
        "/r/pics",
        "/r/pics/",
        "R/pics",
        "  pics  ",
        "https://www.reddit.com/r/pics/",
        "https://old.reddit.com/r/pics/top/?t=all",
        "reddit.com/r/pics",
    ],
)
def test_every_way_of_naming_a_subreddit_normalizes_to_the_bare_name(given):
    assert Subreddit(given).name == "pics"


def test_the_storage_key_is_the_bare_name():
    assert Subreddit("r/EarthPorn").key == "EarthPorn"


def test_a_source_stringifies_to_its_key():
    assert str(Subreddit("r/EarthPorn")) == "EarthPorn"


@pytest.mark.parametrize("name", ["", "has space", "has/slash", "has-dash", "!"])
def test_an_invalid_subreddit_name_is_rejected(name):
    with pytest.raises(ValueError, match="invalid subreddit name"):
        Subreddit(name)


def test_an_unknown_sort_is_rejected():
    with pytest.raises(ValueError, match="sort must be one of hot/new/top/rising"):
        Subreddit("pics", sort="controversial")  # a user-page sort, not a subreddit one


def test_an_unknown_time_filter_is_rejected():
    with pytest.raises(ValueError, match="time_filter must be one of"):
        Subreddit("pics", time_filter="decade")


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_below_one_is_rejected(limit):
    with pytest.raises(ValueError, match="limit must be >= 1"):
        Subreddit("pics", limit=limit)


def test_a_subreddit_is_frozen():
    with pytest.raises(Exception):
        Subreddit("pics").name = "other"  # type: ignore[misc]


# -- MultiReddit ------------------------------------------------------------


def test_a_multireddit_joins_its_names_with_plus():
    assert MultiReddit(("pics", "art")).joined == "pics+art"
    assert MultiReddit(("pics", "art")).url == "https://www.reddit.com/r/pics+art/new/"


def test_a_multireddits_key_is_the_joined_name():
    assert MultiReddit(("pics", "art")).key == "pics+art"


def test_a_multireddit_sorts_like_a_subreddit():
    assert MultiReddit(("a", "b"), sort="hot").url == "https://www.reddit.com/r/a+b/"
    assert (
        MultiReddit(("a", "b"), sort="top", time_filter="year").url
        == "https://www.reddit.com/r/a+b/top/?t=year"
    )


def test_each_name_in_a_multireddit_is_normalized():
    assert MultiReddit(("r/pics", "https://www.reddit.com/r/art/")).names == (
        "pics",
        "art",
    )


def test_a_multireddit_needs_at_least_one_name():
    with pytest.raises(ValueError, match="at least one subreddit name"):
        MultiReddit(())


def test_an_invalid_name_anywhere_in_a_multireddit_is_rejected():
    with pytest.raises(ValueError, match="invalid subreddit name 'bad name'"):
        MultiReddit(("pics", "bad name"))


def test_a_multireddit_validates_its_sort():
    with pytest.raises(ValueError, match="sort must be one of"):
        MultiReddit(("a", "b"), sort="nonsense")


# -- UserProfile ------------------------------------------------------------


def test_a_user_page_points_at_submitted_posts():
    assert (
        UserProfile("alice").url
        == "https://www.reddit.com/user/alice/submitted/?sort=new&t=all"
    )


def test_a_user_page_carries_sort_and_window_in_its_query():
    assert UserProfile("alice", sort="top", time_filter="year").url.endswith(
        "?sort=top&t=year"
    )


def test_a_user_key_is_prefixed_so_it_cannot_collide_with_a_subreddit():
    # r/alice and u/alice must not share an output folder.
    assert UserProfile("alice").key == "u_alice"
    assert UserProfile("alice").key != Subreddit("alice").key


@pytest.mark.parametrize(
    "given",
    [
        "alice",
        "u/alice",
        "/u/alice",
        "user/alice",
        "/user/alice/",
        "https://www.reddit.com/user/alice/",
        "https://www.reddit.com/u/alice",
    ],
)
def test_every_way_of_naming_a_user_normalizes_to_the_bare_name(given):
    assert UserProfile(given).name == "alice"


def test_a_username_may_contain_a_dash():
    # Reddit allows hyphens in usernames, unlike subreddit names.
    assert UserProfile("some-user").name == "some-user"


@pytest.mark.parametrize("name", ["", "has space", "has/slash", "!"])
def test_an_invalid_username_is_rejected(name):
    with pytest.raises(ValueError, match="invalid username"):
        UserProfile(name)


def test_a_user_page_accepts_controversial_but_not_rising():
    assert UserProfile("alice", sort="controversial").sort == "controversial"
    with pytest.raises(
        ValueError, match="sort must be one of hot/new/top/controversial"
    ):
        UserProfile("alice", sort="rising")


# -- parse_source -----------------------------------------------------------


def test_a_plain_name_becomes_a_subreddit():
    source = parse_source("pics")
    assert isinstance(source, Subreddit) and source.name == "pics"


def test_a_plus_joined_name_becomes_a_multireddit():
    source = parse_source("r/pics+art+EarthPorn")
    assert isinstance(source, MultiReddit)
    assert source.names == ("pics", "art", "EarthPorn")


@pytest.mark.parametrize(
    "text",
    [
        "u/alice",
        "/u/alice",
        "user/alice",
        "/user/alice",
        "https://www.reddit.com/user/alice/",
    ],
)
def test_a_user_reference_becomes_a_user_profile(text):
    source = parse_source(text)
    assert isinstance(source, UserProfile) and source.name == "alice"


def test_a_subreddit_named_like_a_user_is_still_a_subreddit():
    # "user" only means a profile at the start of the string or after reddit.com/.
    source = parse_source("r/users")
    assert isinstance(source, Subreddit) and source.name == "users"


def test_a_reddit_url_for_a_subreddit_is_not_mistaken_for_a_user():
    assert isinstance(parse_source("https://www.reddit.com/r/pics/"), Subreddit)


def test_surrounding_whitespace_is_ignored():
    assert parse_source("  r/pics  ").key == "pics"


def test_overrides_reach_the_constructed_source():
    source = parse_source("r/pics", sort="top", time_filter="week", limit=25)
    assert (source.sort, source.time_filter, source.limit) == ("top", "week", 25)


def test_none_overrides_fall_back_to_the_sources_own_defaults():
    # The CLI passes every flag through, set or not; None must not clobber a default.
    source = parse_source("r/pics", sort=None, time_filter=None, limit=None)
    assert (source.sort, source.time_filter, source.limit) == ("new", "all", 100)


def test_overrides_reach_a_multireddit_and_a_user_page_too():
    assert parse_source("r/a+b", limit=5).limit == 5
    assert parse_source("u/alice", sort="top").sort == "top"


def test_an_invalid_source_still_raises_through_the_parser():
    with pytest.raises(ValueError, match="invalid subreddit name"):
        parse_source("has space")


# -- the normalizing helpers ------------------------------------------------


def test_parse_subreddit_keeps_a_multireddits_plus():
    # parse_source relies on this to tell r/a+b apart from a plain subreddit.
    assert parse_subreddit("https://www.reddit.com/r/a+b/") == "a+b"


def test_parse_subreddit_stops_at_the_query_and_fragment():
    assert parse_subreddit("https://www.reddit.com/r/pics/?f=flair") == "pics"
    assert parse_subreddit("/r/pics#top") == "pics"


def test_parse_subreddit_passes_an_unrecognized_string_through():
    assert parse_subreddit("/pics/") == "pics"


def test_parse_username_stops_at_the_query_and_fragment():
    assert parse_username("https://www.reddit.com/user/alice/?sort=new") == "alice"


def test_parse_username_passes_an_unrecognized_string_through():
    assert parse_username("alice") == "alice"
