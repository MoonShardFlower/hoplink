"""
Tests for ExtractorConfig: what it normalizes and what it refuses.

`test_retry` already covers the retry fields. This file covers the rest of the validation, which
exists so a typo fails at construction rather than halfway through a long run.
"""

from __future__ import annotations

import pytest

from hoplink.models.config import DEFAULT_FORMATS, ExtractorConfig
from hoplink.models.media import MediaType

# -- formats ----------------------------------------------------------------


def test_the_default_formats_are_the_common_lossy_and_lossless_ones():
    assert ExtractorConfig().formats == DEFAULT_FORMATS
    assert "gif" not in ExtractorConfig().formats  # opt-in, since most gifs are video


def test_a_comma_string_becomes_a_tuple():
    # This is what the CLI's --formats flag hands over.
    assert ExtractorConfig(formats="jpg,png").formats == ("jpg", "png")


def test_an_iterable_of_formats_is_kept():
    assert ExtractorConfig(formats=["jpg", "png"]).formats == ("jpg", "png")


def test_formats_are_lower_cased_and_trimmed():
    assert ExtractorConfig(formats=" JPG , PnG ").formats == ("jpg", "png")


def test_a_leading_dot_is_dropped():
    # ".jpg" is how a person writes it; extension_of hands back "jpg".
    assert ExtractorConfig(formats=".jpg,.png").formats == ("jpg", "png")


def test_empty_parts_are_dropped():
    assert ExtractorConfig(formats="jpg,,  ,png").formats == ("jpg", "png")


def test_no_formats_at_all_is_allowed():
    # Nothing image-like matches, but a video-only job has no use for formats.
    assert ExtractorConfig(formats="").formats == ()


# -- default_media_types ----------------------------------------------------


def test_images_and_galleries_are_the_default():
    assert ExtractorConfig().default_media_types == MediaType.IMAGE | MediaType.GALLERY


def test_default_media_types_accept_a_comma_string():
    config = ExtractorConfig(default_media_types="image,video")
    assert config.default_media_types == MediaType.IMAGE | MediaType.VIDEO


def test_default_media_types_accept_a_list():
    assert (
        ExtractorConfig(default_media_types=["video"]).default_media_types
        is MediaType.VIDEO
    )


def test_an_unknown_default_media_type_is_rejected():
    with pytest.raises(ValueError, match="unknown media type"):
        ExtractorConfig(default_media_types="sculpture")


# -- validation: non-negative fields ----------------------------------------


@pytest.mark.parametrize(
    "field", ["scroll_pause", "delay", "api_pause", "max_retries", "retry_backoff"]
)
def test_a_negative_pacing_field_is_rejected(field):
    with pytest.raises(ValueError, match="{} must be >= 0".format(field)):
        ExtractorConfig(**{field: -1})


@pytest.mark.parametrize(
    "field", ["scroll_pause", "delay", "api_pause", "max_retries", "retry_backoff"]
)
def test_zero_is_allowed_for_pacing(field):
    # Tests and local runs disable pacing entirely.
    assert getattr(ExtractorConfig(**{field: 0}), field) == 0


def test_gallery_wait_may_be_zero_but_not_negative():
    assert ExtractorConfig(gallery_wait_ms=0).gallery_wait_ms == 0
    with pytest.raises(ValueError, match="gallery_wait_ms must be >= 0"):
        ExtractorConfig(gallery_wait_ms=-1)


# -- validation: positive fields --------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "max_stale_scrolls",
        "scroll_px",
        "manifest_flush_every",
        "download_concurrency",
        "nav_timeout_ms",
        "post_wait_timeout_ms",
        "request_timeout_ms",
    ],
)
def test_a_field_that_must_be_positive_rejects_zero_and_below(field):
    # Zero would mean "never scroll", "never flush", or "time out instantly".
    with pytest.raises(ValueError, match="{} must be >= 1".format(field)):
        ExtractorConfig(**{field: 0})
    with pytest.raises(ValueError, match="{} must be >= 1".format(field)):
        ExtractorConfig(**{field: -1})


def test_one_is_allowed_for_the_positive_fields():
    assert ExtractorConfig(manifest_flush_every=1).manifest_flush_every == 1


# -- defaults ---------------------------------------------------------------


def test_the_browser_defaults_are_headless_and_profile_less():
    config = ExtractorConfig()
    assert config.headless is True
    assert config.profile_dir is None
    assert config.locale == "en-US"
    assert config.viewport == (1366, 900)
    assert (
        "Chrome/" in config.user_agent
    )  # Reddit serves the modern UI to a real-looking browser


def test_the_output_defaults_are_conservative():
    config = ExtractorConfig()
    assert config.output_dir == "downloads"
    assert config.manifest_flush_every == 1  # a crash loses at most one file's record
    assert config.dedupe_by_hash is False  # hashing is opt-in


def test_the_pacing_defaults_are_relaxed():
    # Reddit throttles aggressive clients, so the defaults err slow.
    config = ExtractorConfig()
    assert config.scroll_pause == 2.0
    assert config.delay == 0.5
    # Downloads are serial until asked otherwise.
    assert config.download_concurrency == 1
    # Try to download directly (outside the browser) for speedup. If refused we fall back to the browser automatically.
    assert config.direct_download is True
    # API paging waits on no renderer, so it is paced far more lightly than scrolling.
    assert config.api_pause == 0.5
    assert config.api_pause < config.scroll_pause


# -- replace ----------------------------------------------------------------


def test_replace_changes_a_field():
    assert ExtractorConfig().replace(headless=False).headless is False


def test_replace_leaves_the_original_alone():
    original = ExtractorConfig()
    original.replace(headless=False)
    assert original.headless is True


def test_replace_keeps_the_untouched_fields():
    replaced = ExtractorConfig(output_dir="/out").replace(headless=False)
    assert replaced.output_dir == "/out"


def test_replace_revalidates():
    # dataclasses.replace re-runs __post_init__, so a bad override cannot sneak through.
    with pytest.raises(ValueError, match="delay must be >= 0"):
        ExtractorConfig().replace(delay=-1.0)


def test_replace_renormalizes():
    assert ExtractorConfig().replace(formats="JPG, .PNG").formats == ("jpg", "png")


def test_a_config_is_frozen():
    with pytest.raises(Exception):
        ExtractorConfig().headless = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "given", ["redgifs, Soundgasm", ["redgifs", "Soundgasm"], ("REDGIFS", "soundgasm")]
)
def test_scrape_all_hosts_normalizes_however_it_was_written(given):
    assert ExtractorConfig(scrape_all_hosts=given).scrape_all_hosts == (
        "redgifs",
        "soundgasm",
    )


def test_scrape_all_hosts_defaults_to_none():
    assert ExtractorConfig().scrape_all_hosts == ()


def test_host_options_lower_case_their_host_names():
    cfg = ExtractorConfig(host_options={"Imgur": {"client_id": "abc"}})
    assert cfg.host_options["imgur"]["client_id"] == "abc"


def test_host_options_are_read_only():
    cfg = ExtractorConfig(host_options={"imgur": {"client_id": "abc"}})
    with pytest.raises(TypeError):
        cfg.host_options["imgur"]["client_id"] = "hijacked"
    with pytest.raises(TypeError):
        cfg.host_options["other"] = {}


def test_a_host_entry_that_is_not_a_table_is_ignored():
    cfg = ExtractorConfig(host_options={"imgur": "nonsense", "ok": {"k": "v"}})
    assert "imgur" not in cfg.host_options
    assert cfg.host_options["ok"]["k"] == "v"


def test_host_options_survive_a_replace():
    cfg = ExtractorConfig(host_options={"imgur": {"client_id": "abc"}}).replace(
        delay=1.0
    )
    assert cfg.host_options["imgur"]["client_id"] == "abc"


def test_no_host_options_are_invented_when_nothing_is_configured():
    assert dict(ExtractorConfig().host_options) == {}
