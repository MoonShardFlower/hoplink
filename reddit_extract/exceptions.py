"""Exception hierarchy for reddit_extract."""

from __future__ import annotations


class RedditExtractError(Exception):
    """Base class for all reddit_extract errors."""


class BrowserError(RedditExtractError):
    """The underlying browser could not be started or has died."""


class NoPostsFoundError(RedditExtractError):
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
