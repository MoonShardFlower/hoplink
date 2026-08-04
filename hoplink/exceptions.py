"""Exception hierarchy for hoplink."""

from __future__ import annotations


class HoplinkExtractError(Exception):
    """Base class for all hoplink errors."""


class BrowserError(HoplinkExtractError):
    """The underlying browser could not be started or has died."""


class ConfigFileError(HoplinkExtractError):
    """A ``--config`` TOML file is missing, malformed, or names an unknown key."""


class NoPostsFoundError(HoplinkExtractError):
    """
    A listing page rendered no posts.

    The source is empty, private, banned, or (for NSFW sources when logged-out) requires a login.
    Configure ``profile_dir`` and run once with ``headless=False`` to sign in. The session is reused afterward.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(
            "No posts rendered at {}. The source may be empty, private, banned, or (if NSFW) require login."
            " -- configure profile_dir and run once with headless=False to sign in.".format(
                url
            )
        )
