"""
Interactive Reddit authentication using a persistent browser profile.

This module creates and verifies a logged-in Reddit session by launching a visible browser window to the login page,
waiting for the user to complete authentication, and saving the profile to disk. The profile is stored in the
configured `profile_dir` and reused in subsequent runs, enabling access to NSFW and private sources.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from typing import Any, Callable, NamedTuple

from ..models.config import ExtractorConfig
from .browser import BrowserManager

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LOGIN_TIMEOUT",
    "REDDIT_LOGIN_URL",
    "LoginResult",
    "LoginState",
    "async_login",
    "login",
    "login_state",
]

#: Reddit's sign-in page. An already-authenticated profile is redirected off it.
REDDIT_LOGIN_URL = "https://www.reddit.com/login/"

#: The endpoint Reddit's own web app asks who it is talking to. Logged out, its answer carries no ``data.name``.
REDDIT_ME_URL = "https://www.reddit.com/api/me.json"

#: Seconds `async_login` waits for a sign-in to be completed before giving up.
DEFAULT_LOGIN_TIMEOUT = 300.0

#: Seconds between two checks while waiting. Every check is one request to Reddit, so this stays polite.
DEFAULT_POLL_INTERVAL = 2.0

#: Cookies Reddit sets only for an authenticated session.
SESSION_COOKIE_NAMES = ("reddit_session",)

#: Rendered markers of a signed-in page: the modern UI tags its root element, old.reddit renders a logout form.
#: Only consulted when `REDDIT_ME_URL` gave no answer, and a miss merely means the wait goes on.
JS_LOGGED_IN = """
() => {
  const app = document.querySelector('shreddit-app');
  if (app && app.hasAttribute('user-logged-in')) {
    return app.getAttribute('user-logged-in') === 'true';
  }
  return !!document.querySelector('form.logout, #header-bottom-right .logout');
}
"""


class LoginState(NamedTuple):
    """What a check of the browser profile's current session found."""

    #: whether the profile is signed in to Reddit
    logged_in: bool
    #: the account name, when the check that confirmed the session carried one
    username: str | None = None


class LoginResult(NamedTuple):
    """The outcome of an interactive sign-in."""

    #: whether the profile ended up signed in
    ok: bool
    #: the account name, when it could be read
    username: str | None = None
    #: why an unsuccessful attempt ended, phrased for a user (None when ``ok``)
    reason: str | None = None
    #: whether the profile was already signed in when the window opened, so nothing had to be typed
    already_logged_in: bool = False


def _notify(on_status: Callable[[str], None] | None, message: str) -> None:
    """Report progress to the caller's callback, if it wants any."""
    if on_status is not None:
        on_status(message)


async def _api_identity(browser: BrowserManager) -> tuple[bool, str | None]:
    """
    Ask Reddit who the session belongs to.

    Args:
        browser: The started browser whose cookies carry the session.

    Returns:
        ``(answered, username)``: whether Reddit replied with JSON at all, and the account name it named.
        ``(True, None)`` is a definitive "this session is logged out"; ``(False, None)`` means "no answer".
    """
    result = await browser.fetch(REDDIT_ME_URL)
    if not result.ok or not result.body or "json" not in (result.content_type or ""):
        log.debug("session check got no usable answer (%s)", result.error or "no body")
        return False, None
    try:
        payload = json.loads(result.body.decode("utf-8", "replace"))
    except ValueError:
        return False, None
    if not isinstance(payload, dict):
        return False, None
    data = payload.get("data")
    name = data.get("name") if isinstance(data, dict) else None
    return True, name if isinstance(name, str) and name else None


async def _has_session_cookie(browser: BrowserManager) -> bool:
    """Whether the context holds a cookie Reddit hands out only to an authenticated session."""
    return any(
        c.get("name") in SESSION_COOKIE_NAMES and c.get("value")
        for c in await browser.cookies(REDDIT_LOGIN_URL)
    )


async def _page_looks_logged_in(page: Any) -> bool:
    """Whether the rendered page shows a signed-in marker (see `JS_LOGGED_IN`); False if it cannot be asked."""
    try:
        return bool(await page.evaluate(JS_LOGGED_IN))
    except Exception:
        return False


async def login_state(browser: BrowserManager, page: Any | None = None) -> LoginState:
    """
    Check whether the browser profile is signed in to Reddit.

    The context must have visited Reddit at least once: `REDDIT_ME_URL` answers a cold context with nothing at all,
    and the fallbacks below need cookies or a rendered page. `async_login` navigates before it asks.

    Args:
        browser: A started browser manager whose context holds the profile's cookies.
        page: An open Reddit page, consulted only as a last resort.

    Returns:
        The state found. A signed-in state carries the account name whenever the check that confirmed it knew one.
    """
    answered, username = await _api_identity(browser)
    if answered:
        return LoginState(username is not None, username)
    if await _has_session_cookie(browser):
        return LoginState(True)
    if page is not None and await _page_looks_logged_in(page):
        return LoginState(True)
    return LoginState(False)


async def _wait_for_login(
    browser: BrowserManager,
    page: Any,
    *,
    timeout: float,
    poll_interval: float,
) -> LoginResult:
    """
    Poll until the session is signed in, the window is gone, or ``timeout`` seconds have passed.

    Args:
        browser: The started browser manager.
        page: The page showing Reddit's login form.
        timeout: Seconds to wait in total.
        poll_interval: Seconds between checks.

    Returns:
        The outcome, with ``reason`` set when the sign-in did not happen.
    """
    deadline = time.monotonic() + timeout
    while True:
        state = await login_state(browser, page)
        if state.logged_in:
            return LoginResult(True, state.username)
        # Checked after the state, so a sign-in completed just before the window was closed still counts.
        if page.is_closed():
            return LoginResult(
                False,
                reason="the browser window was closed before a sign-in was confirmed",
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return LoginResult(
                False, reason="no sign-in within {:.0f}s".format(timeout)
            )
        await asyncio.sleep(min(poll_interval, remaining))


async def async_login(
    config: ExtractorConfig,
    *,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    on_status: Callable[[str], None] | None = None,
    browser: BrowserManager | None = None,
) -> LoginResult:
    """
    Open Reddit's login page and wait for the sign-in, leaving the session in the configured profile directory.

    The window is always visible: ``headless`` is overridden, since a sign-in nobody can see cannot be completed.
    A profile that turns out to be signed in already returns immediately, which makes this double as a session check.

    Args:
        config: Browser configuration. ``profile_dir`` is required, as that is where the session is kept.
        timeout: Seconds to wait for the sign-in before giving up.
        poll_interval: Seconds between two checks while waiting.
        on_status: Called with progress messages meant for a human.
        browser: A browser manager to use, taken as configured and left open for the caller to close.

    Returns:
        The outcome. Check ``ok``; ``reason`` says what went wrong otherwise.

    Raises:
        ValueError: If ``config`` names no profile directory.
        BrowserError: If Chromium cannot be launched.
    """
    if not config.profile_dir:
        raise ValueError(
            "logging in needs a persistent profile directory (profile_dir / --profile): "
            "an incognito session is thrown away when the browser closes"
        )
    owned = browser is None
    manager = (
        browser
        if browser is not None
        else BrowserManager(config.replace(headless=False))
    )
    try:
        await manager.start()
        page = await manager.new_page()
        await page.goto(
            REDDIT_LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=config.nav_timeout_ms,
        )
        await manager.dismiss_gates(page)

        state = await login_state(manager, page)
        if state.logged_in:
            return LoginResult(True, state.username, already_logged_in=True)

        _notify(on_status, "Sign in to Reddit in the browser window that just opened.")
        result = await _wait_for_login(
            manager, page, timeout=timeout, poll_interval=poll_interval
        )
        if result.ok:
            _notify(on_status, "Signed in. Saving the session ...")
        return result
    finally:
        # Closing the context is what flushes the profile (cookies included) to disk.
        if owned:
            await manager.close()


def login(config: ExtractorConfig, **kwargs: Any) -> LoginResult:
    """
    Blocking wrapper around `async_login`, for callers with no event loop of their own.

    On Windows the loop is explicitly a ProactorEventLoop, because Playwright launches Chromium as a subprocess,
    which the selector loop cannot do.

    Args:
        config: Browser configuration; ``profile_dir`` is required.
        **kwargs: Forwarded to `async_login` (``timeout``, ``poll_interval``, ``on_status``, ``browser``).

    Returns:
        The outcome of the sign-in.

    Raises:
        KeyboardInterrupt: If the wait is interrupted the browser is closed first, so a session collected up to
            that point is still saved.
    """
    if sys.platform == "win32":
        loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    task = loop.create_task(async_login(config, **kwargs))
    try:
        return loop.run_until_complete(task)
    except KeyboardInterrupt:
        task.cancel()
        with contextlib.suppress(Exception):
            loop.run_until_complete(task)
        raise
    finally:
        with contextlib.suppress(Exception):
            loop.close()
