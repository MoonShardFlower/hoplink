"""Tests for the naming rules: post titles into slugs, candidates into file names."""

from __future__ import annotations

import pytest

from hoplink.storage.naming import (
    MAX_SLUG_LENGTH,
    collection_key,
    extension_for_content_type,
    media_extension,
    media_filename,
    slugify,
)


def test_a_plain_title_becomes_underscore_separated_words():
    assert slugify("Hello World") == "Hello_World"


def test_case_is_preserved():
    assert slugify("Sunset Over Lofoten") == "Sunset_Over_Lofoten"


def test_punctuation_collapses_into_single_underscores():
    assert slugify("Hello,   World!!! (again)") == "Hello_World_again"


def test_leading_and_trailing_separators_are_trimmed():
    assert slugify("  ...Hello World...  ") == "Hello_World"


@pytest.mark.parametrize("char", list('<>:"/\\|?*'))
def test_characters_windows_forbids_never_survive(char):
    assert char not in slugify("a{}b".format(char))


def test_control_characters_never_survive():
    assert slugify("a\x00b\tc\nd") == "a_b_c_d"


def test_a_path_traversal_attempt_cannot_escape_its_folder():
    assert slugify("../../etc/passwd") == "etc_passwd"


def test_digits_and_hyphens_are_kept():
    assert slugify("Top-10 shots of 2026") == "Top-10_shots_of_2026"


def test_dots_do_not_survive_as_extensions():
    assert slugify("v1.2 final.") == "v1_2_final"


def test_letters_outside_ascii_are_kept():
    assert slugify("Grüße aus Köln") == "Grüße_aus_Köln"


def test_compatibility_forms_are_folded_to_plain_text():
    assert slugify("ＦＵＬＬ width") == "FULL_width"


def test_emoji_are_dropped():
    assert slugify("Sunset 🌅 today") == "Sunset_today"


def test_a_title_with_nothing_sluggable_yields_an_empty_slug():
    assert slugify("!!! ***") == ""
    assert slugify("") == ""
    assert slugify(None) == ""


def test_a_long_title_is_cut_at_a_word_boundary():
    slug = slugify("word " * 40)
    assert len(slug) <= MAX_SLUG_LENGTH
    assert not slug.endswith("_")
    assert slug.split("_")[-1] == "word"  # never cut mid-word


def test_a_single_over_long_word_is_cut_hard():
    assert slugify("x" * 200) == "x" * MAX_SLUG_LENGTH


def test_the_length_cap_is_adjustable():
    assert slugify("Hello World", max_length=5) == "Hello"


@pytest.mark.parametrize("name", ["CON", "nul", "CoM1", "LPT9"])
def test_a_windows_device_name_is_made_storable(name):
    assert slugify(name) == "_" + name


def test_a_device_name_inside_a_longer_title_is_left_alone():
    assert slugify("CON artists") == "CON_artists"


def test_the_handlers_extension_wins():
    assert media_extension("https://x/a.png", "jpg") == "jpg"


def test_an_extension_is_normalized():
    assert media_extension("https://x/a", ".JPG") == "jpg"


def test_an_extension_falls_back_to_the_urls_own():
    assert media_extension("https://x/a.PNG") == "png"


def test_a_query_string_is_not_mistaken_for_an_extension():
    assert media_extension("https://i.redd.it/a.jpg?width=640&s=abc") == "jpg"


def test_a_url_with_no_extension_takes_the_default():
    assert media_extension("https://v.redd.it/abc123", default="mp4") == "mp4"
    assert media_extension("https://x/a") == "jpg"


def test_a_file_is_named_by_its_index_and_slug():
    assert media_filename(1, "jpg", slug="Hello_World") == "0001_Hello_World.jpg"


def test_the_index_is_zero_padded_to_four_digits():
    assert media_filename(42, "jpg", slug="x") == "0042_x.jpg"


def test_an_index_beyond_four_digits_still_works():
    assert media_filename(12345, "jpg", slug="x") == "12345_x.jpg"


def test_a_multi_file_post_numbers_its_parts_before_the_slug():
    assert media_filename(1, "jpg", slug="Hello", part=2) == "0001_02_Hello.jpg"


def test_a_file_with_no_slug_is_named_by_its_index_alone():
    assert media_filename(1, "jpg") == "0001.jpg"
    assert media_filename(1, "jpg", part=2) == "0001_02.jpg"


def test_a_collection_nests_inside_its_source():
    assert collection_key("pics", "someuploader") == "pics/someuploader"


@pytest.mark.parametrize(
    "content_type, expected",
    [
        ("image/jpeg", "jpg"),  # not mimetypes' ".jpe"
        ("image/png", "png"),
        ("image/webp", "webp"),
        ("IMAGE/PNG", "png"),  # header casing is not meaningful
        ("image/png; charset=binary", "png"),  # parameters are not part of the type
        ("video/mp4", "mp4"),
        ("audio/x-m4a", "m4a"),
        ("application/pdf", "pdf"),
    ],
)
def test_a_content_type_names_its_extension(content_type, expected):
    assert extension_for_content_type(content_type) == expected


@pytest.mark.parametrize(
    "content_type",
    [
        "application/octet-stream",  # the generic "some bytes" answer names no format
        "binary/octet-stream",
        "",
        None,
        "   ",
        "not/a-real-type",
    ],
)
def test_a_content_type_naming_no_format_corrects_nothing(content_type):
    assert extension_for_content_type(content_type) is None
