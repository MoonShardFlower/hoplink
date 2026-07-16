"""Load reusable CLI settings from a TOML config file.

A config file lets you store a whole run -- its sources plus any command-line
option -- so that::

    reddit-extract --config myjob.toml

replaces a long, easily-mistyped command line. The file is flat TOML whose keys
mirror the long-form CLI options (dashes or underscores both work). It may also
set a handful of advanced :class:`~reddit_extract.models.config.ExtractorConfig`
knobs that have no dedicated flag (see :data:`EXTRACTOR_ONLY_KEYS`).

Example ``myjob.toml``::

    sources = ["r/EarthPorn", "r/wallpapers"]
    sort    = "top"
    time    = "month"          # alias for time_filter
    limit   = 200
    types   = "image,gallery"
    out     = "downloads"
    # advanced knobs (no CLI flag):
    locale         = "en-US"
    nav_timeout_ms = 90000

Precedence at runtime is ``explicit CLI argument > config file > built-in
default``: anything you type on the command line overrides the file. One caveat
follows from argparse's boolean flags -- ``dry_run``, ``show`` and ``quiet`` can
be turned *on* by a config file, but a plain command line cannot turn them back
*off* (there are no ``--no-*`` flags yet).

This module is deliberately free of any browser or Playwright import, so it can
be unit-tested on its own.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Iterable, Union

from .exceptions import ConfigFileError

#: Advanced ``ExtractorConfig`` fields that have no dedicated CLI flag but may be
#: set from a config file. The CLI forwards these straight into ``ExtractorConfig``.
EXTRACTOR_ONLY_KEYS = frozenset(
    {
        "default_media_types",
        "viewport",
        "locale",
        "manifest_flush_every",
        "scroll_px",
        "nav_timeout_ms",
        "post_wait_timeout_ms",
        "gallery_wait_ms",
        "request_timeout_ms",
    }
)

#: Config keys whose canonical name differs from what the user naturally writes.
#: ``--time`` stores into the ``time_filter`` destination; accept both spellings.
KEY_ALIASES = {"time": "time_filter"}


def _normalize_key(key: str) -> str:
    """Canonicalize a raw TOML key: trim, dashes to underscores, apply aliases."""
    canonical = key.strip().replace("-", "_")
    return KEY_ALIASES.get(canonical, canonical)


def load_config_file(
    path: Union[str, Path],
    *,
    valid_cli_keys: Iterable[str],
) -> dict[str, Any]:
    """
    Read a TOML config file into a normalized dict of settings.

    Args:
        path: Path to the TOML file.
        valid_cli_keys: The argparse destinations the caller accepts (used
            together with :data:`EXTRACTOR_ONLY_KEYS` to reject unknown keys and
            catch typos).

    Returns:
        A dict mapping canonical keys (underscored, aliases resolved) to their
        values. ``sources`` is always a list; ``viewport`` is a tuple when set.

    Raises:
        ConfigFileError: If the file is missing, is not valid TOML, names an
            unknown key, contains two keys that canonicalize to the same setting,
            or gives ``sources`` a value that is not a string or list.
    """
    path = Path(path)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigFileError("config file not found: {}".format(path)) from None
    except IsADirectoryError:
        raise ConfigFileError(
            "config path is a directory, not a file: {}".format(path)
        ) from None
    except OSError as exc:
        raise ConfigFileError(
            "could not read config file {}: {}".format(path, exc)
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError("invalid TOML in {}: {}".format(path, exc)) from None

    allowed = set(valid_cli_keys) | set(EXTRACTOR_ONLY_KEYS)
    allowed.discard("config")  # a config file can't point at another config file

    settings: dict[str, Any] = {}
    for raw_key, value in raw.items():
        key = _normalize_key(raw_key)
        if key not in allowed:
            raise ConfigFileError(
                "unknown config key {!r} in {} (allowed: {})".format(
                    raw_key, path, ", ".join(sorted(allowed))
                )
            )
        if key in settings:
            raise ConfigFileError(
                "config key {!r} in {} conflicts with another key for the same "
                "setting {!r}".format(raw_key, path, key)
            )
        settings[key] = value

    _coerce_sources(settings, path)
    _coerce_viewport(settings)
    return settings


def _coerce_sources(settings: dict[str, Any], path: Path) -> None:
    """Normalize ``sources`` to a list in place.

    A bare string is wrapped in a one-element list; argparse stores the positional
    as a list, and a raw string would otherwise be iterated character by character.
    """
    if "sources" not in settings:
        return
    value = settings["sources"]
    if isinstance(value, str):
        settings["sources"] = [value]
    elif isinstance(value, list):
        settings["sources"] = [str(item) for item in value]
    else:
        raise ConfigFileError(
            "'sources' in {} must be a string or a list of strings, got {}".format(
                path, type(value).__name__
            )
        )


def _coerce_viewport(settings: dict[str, Any]) -> None:
    """Convert a ``viewport`` TOML array (a list) to the tuple ExtractorConfig wants."""
    if isinstance(settings.get("viewport"), list):
        settings["viewport"] = tuple(settings["viewport"])
