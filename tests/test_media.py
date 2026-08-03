"""Tests for media types and the objects that flow through the pipeline."""

from __future__ import annotations

import json

import pytest

from reddit_extract.models.media import MediaCandidate, MediaItem, MediaType

# -- MediaType.coerce -------------------------------------------------------


def test_a_media_type_passes_through():
    assert MediaType.coerce(MediaType.IMAGE) is MediaType.IMAGE


def test_a_combined_flag_passes_through():
    combined = MediaType.IMAGE | MediaType.VIDEO
    assert MediaType.coerce(combined) is combined


@pytest.mark.parametrize(
    "name, expected",
    [
        ("image", MediaType.IMAGE),
        ("gallery", MediaType.GALLERY),
        ("video", MediaType.VIDEO),
        ("text", MediaType.TEXT),
        ("link", MediaType.LINK),
        ("poll", MediaType.POLL),
        ("crosspost", MediaType.CROSSPOST),
        ("all", MediaType.ALL),
    ],
)
def test_every_advertised_name_resolves(name, expected):
    assert MediaType.coerce(name) is expected


def test_names_are_case_insensitive_and_trimmed():
    assert MediaType.coerce("  ImAgE  ") is MediaType.IMAGE


def test_a_comma_string_combines_its_parts():
    # This is what the CLI's --types flag hands over.
    assert MediaType.coerce("image,gallery") == MediaType.IMAGE | MediaType.GALLERY


def test_a_comma_string_tolerates_spacing_and_empty_parts():
    assert (
        MediaType.coerce(" image , , gallery ") == MediaType.IMAGE | MediaType.GALLERY
    )


def test_an_iterable_combines_its_parts():
    # This is what a config file's types = ["image", "video"] hands over.
    assert MediaType.coerce(["image", "video"]) == MediaType.IMAGE | MediaType.VIDEO


def test_an_iterable_may_mix_names_flags_and_comma_strings():
    assert MediaType.coerce(["image,gallery", MediaType.VIDEO]) == (
        MediaType.IMAGE | MediaType.GALLERY | MediaType.VIDEO
    )


def test_an_empty_iterable_coerces_to_nothing():
    # main() checks for this and exits with usage rather than running a job that matches nothing.
    assert not MediaType.coerce([])


def test_repeating_a_type_is_harmless():
    assert MediaType.coerce("image,image") is MediaType.IMAGE


def test_all_is_every_single_type_combined():
    combined = MediaType(0)
    for member in MediaType:
        combined |= member
    assert MediaType.ALL == combined


def test_an_unknown_name_is_rejected_and_lists_the_allowed_ones():
    with pytest.raises(ValueError, match="unknown media type 'sculpture'") as exc:
        MediaType.coerce("sculpture")
    message = str(exc.value)
    assert all(
        name in message for name in ("image", "gallery", "video", "text", "link", "all")
    )


def test_an_unknown_name_inside_a_comma_string_is_rejected():
    with pytest.raises(ValueError, match="unknown media type 'sculpture'"):
        MediaType.coerce("image,sculpture")


def test_a_string_naming_nothing_is_rejected():
    with pytest.raises(ValueError, match="no media type given"):
        MediaType.coerce(" , ")
    with pytest.raises(ValueError, match="no media type given"):
        MediaType.coerce("")


# -- MediaType.label --------------------------------------------------------


def test_a_single_type_labels_as_its_lower_case_name():
    assert MediaType.IMAGE.label == "image"
    assert MediaType.ALL.label == "all"


def test_the_empty_flag_is_the_only_thing_that_labels_as_mixed():
    assert MediaType(0).label == "mixed"


# -- MediaCandidate ---------------------------------------------------------


def test_a_candidate_expects_images_by_default():
    cand = MediaCandidate(url="https://i.redd.it/a.jpg", media_type=MediaType.IMAGE)
    assert cand.content_prefixes == ("image/",)
    assert cand.ext is None  # inferred from the URL at save time
    assert cand.body is None  # a fetched candidate, not one carrying its own bytes


# -- MediaItem.to_manifest_entry --------------------------------------------


def item(**overrides) -> MediaItem:
    """One saved media item with full provenance."""
    fields = {
        "url": "https://i.redd.it/a.jpg",
        "media_type": MediaType.IMAGE,
        "filename": "0001.jpg",
        "path": "/out/pics/0001.jpg",
        "downloaded": True,
        "size": 2048,
        "post_id": "t3_abc",
        "post_url": "https://www.reddit.com/r/pics/comments/abc/t/",
        "title": "a photo",
        "author": "alice",
        "created": "2026-07-16T11:45:34+00:00",
        "source_key": "pics",
    }
    fields.update(overrides)
    return MediaItem(**fields)


def test_a_manifest_entry_maps_a_file_back_to_its_post():
    assert item().to_manifest_entry() == {
        "post_id": "t3_abc",
        "post_url": "https://www.reddit.com/r/pics/comments/abc/t/",
        "title": "a photo",
        "author": "alice",
        "created": "2026-07-16T11:45:34+00:00",
        "media_url": "https://i.redd.it/a.jpg",
        "media_type": "image",
    }


def test_a_manifest_entry_omits_the_hash_when_dedupe_did_not_run():
    # Manifests written without --dedupe stay byte-for-byte what they always were.
    assert "sha256" not in item().to_manifest_entry()


def test_a_manifest_entry_carries_the_hash_once_dedupe_ran():
    assert item(sha256="abc123").to_manifest_entry()["sha256"] == "abc123"


def test_a_manifest_entry_is_json_serializable():
    assert json.loads(json.dumps(item(sha256="abc").to_manifest_entry()))


# -- MediaItem.to_report_dict -----------------------------------------------


def test_a_report_entry_describes_the_stored_file_and_its_post():
    assert item(sha256="abc").to_report_dict() == {
        "filename": "0001.jpg",
        "path": "/out/pics/0001.jpg",
        "url": "https://i.redd.it/a.jpg",
        "media_type": "image",
        "downloaded": True,
        "size": 2048,
        "sha256": "abc",
        "post_id": "t3_abc",
        "post_url": "https://www.reddit.com/r/pics/comments/abc/t/",
        "title": "a photo",
        "author": "alice",
        "created": "2026-07-16T11:45:34+00:00",
        "collection": None,
    }


def test_a_planned_file_reports_as_not_downloaded():
    # Dry runs record what would be fetched, with no path or size yet.
    planned = item(downloaded=False, path=None, size=None)
    report = planned.to_report_dict()
    assert (report["downloaded"], report["path"], report["size"]) == (False, None, None)
    assert report["filename"] == "0001.jpg"  # the name it would take is still known


def test_a_report_entry_is_json_serializable():
    assert json.loads(json.dumps(item().to_report_dict()))


def test_a_report_entry_labels_a_combined_media_type_by_its_parts():
    # In practice a handler's media_type is always a single flag, so this stays hypothetical.
    report = item(media_type=MediaType.IMAGE | MediaType.VIDEO).to_report_dict()
    assert report["media_type"] == "image|video"


def test_an_item_defaults_to_an_unsaved_file():
    bare = MediaItem(url="https://i.redd.it/a.jpg", media_type=MediaType.IMAGE)
    assert bare.downloaded is False
    assert (bare.filename, bare.path, bare.size, bare.sha256) == (
        None,
        None,
        None,
        None,
    )
