"""
Tests for the interactive sign-in (``--login``).

No Chromium is launched: `async_login` is handed a fake browser manager standing in for the real one, so every
branch of the session check and of the wait loop is exercised in memory.
"""

from __future__ import annotations

import json
from typing import Any, List

import pytest

from hoplink import cli
from hoplink.core import login as login_module
from hoplink.core.browser import FetchResult
from hoplink.core.login import (
    REDDIT_LOGIN_URL,
    LoginResult,
    async_login,
    login,
    login_state,
)
from hoplink.exceptions import BrowserError
from hoplink.models.config import ExtractorConfig


def api_answer(payload: Any) -> FetchResult:
    """A successful JSON reply from Reddit's session endpoint."""
    return FetchResult(
        ok=True,
        status=200,
        content_type="application/json",
        body=json.dumps(payload).encode("utf-8"),
    )


#: Reddit answering "this session belongs to nobody".
LOGGED_OUT = api_answer({})
#: Reddit naming the signed-in account.
SIGNED_IN = api_answer({"kind": "t2", "data": {"name": "someuser"}})
#: No usable answer at all (blocked, or an interstitial instead of JSON).
NO_ANSWER = FetchResult(ok=False, status=403, error="HTTP 403")


class FakePage:
    """A Playwright page: records navigation, answers the logged-in probe, and can be closed."""

    def __init__(self, logged_in: bool = False) -> None:
        self.logged_in = logged_in
        self.closed = False
        self.goto_calls: List[str] = []
        self.evaluate_error: Exception | None = None

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append(url)

    async def evaluate(self, script: str) -> bool:
        if self.evaluate_error is not None:
            raise self.evaluate_error
        return self.logged_in

    def is_closed(self) -> bool:
        return self.closed


class FakeBrowser:
    """Stands in for BrowserManager: canned session answers, canned cookies, no Chromium."""

    def __init__(
        self,
        answers: List[FetchResult] | None = None,
        cookies: List[dict[str, Any]] | None = None,
        page: FakePage | None = None,
    ) -> None:
        #: consumed one per check; the last one repeats once exhausted
        self.answers = list(answers or [LOGGED_OUT])
        self.cookie_list = list(cookies or [])
        self.page = page if page is not None else FakePage()
        self.started = False
        self.closed = False
        self.gates_dismissed = 0
        self.fetched: List[str] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def new_page(self) -> FakePage:
        return self.page

    async def dismiss_gates(self, page: Any) -> None:
        self.gates_dismissed += 1

    async def cookies(self, url: str) -> List[dict[str, Any]]:
        return self.cookie_list

    async def fetch(self, url: str, timeout_ms: Any = None, headers: Any = None):
        self.fetched.append(url)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def profile_config(tmp_path) -> ExtractorConfig:
    """A config pointing at a throwaway profile directory."""
    return ExtractorConfig(profile_dir=str(tmp_path / "profile"))


async def run_login(browser: FakeBrowser, tmp_path, **kwargs: Any) -> LoginResult:
    """Run `async_login` against ``browser``, polling as fast as the test allows."""
    kwargs.setdefault("poll_interval", 0)
    kwargs.setdefault("timeout", 0.05)
    return await async_login(profile_config(tmp_path), browser=browser, **kwargs)


# -- the session check ------------------------------------------------------


async def test_reddits_own_answer_names_the_account():
    assert await login_state(FakeBrowser([SIGNED_IN])) == (True, "someuser")


async def test_an_answer_without_a_name_is_a_logged_out_session():
    assert await login_state(FakeBrowser([LOGGED_OUT])) == (False, None)


async def test_a_session_cookie_confirms_a_login_the_endpoint_could_not():
    browser = FakeBrowser(
        [NO_ANSWER], cookies=[{"name": "reddit_session", "value": "x"}]
    )
    assert await login_state(browser) == (True, None)


async def test_other_cookies_prove_nothing():
    browser = FakeBrowser([NO_ANSWER], cookies=[{"name": "loid", "value": "x"}])
    assert await login_state(browser) == (False, None)


async def test_a_valueless_session_cookie_proves_nothing():
    browser = FakeBrowser(
        [NO_ANSWER], cookies=[{"name": "reddit_session", "value": ""}]
    )
    assert await login_state(browser) == (False, None)


async def test_the_rendered_page_is_the_last_resort():
    assert await login_state(FakeBrowser([NO_ANSWER]), FakePage(logged_in=True)) == (
        True,
        None,
    )


async def test_a_page_that_cannot_be_asked_is_not_logged_in():
    page = FakePage(logged_in=True)
    page.evaluate_error = RuntimeError("page closed")
    assert await login_state(FakeBrowser([NO_ANSWER]), page) == (False, None)


async def test_a_non_json_body_is_not_an_answer():
    browser = FakeBrowser(
        [FetchResult(ok=True, status=200, content_type="text/html", body=b"<html>")]
    )
    assert await login_state(browser) == (False, None)


async def test_malformed_json_is_not_an_answer():
    browser = FakeBrowser(
        [FetchResult(ok=True, status=200, content_type="application/json", body=b"{ n")]
    )
    assert await login_state(browser) == (False, None)


async def test_a_json_body_that_is_not_an_object_is_not_an_answer():
    assert await login_state(FakeBrowser([api_answer([1, 2])])) == (False, None)


# -- the interactive flow ---------------------------------------------------


async def test_a_login_needs_somewhere_to_keep_the_session():
    with pytest.raises(ValueError, match="profile"):
        await async_login(ExtractorConfig(), browser=FakeBrowser())


async def test_the_login_page_is_opened_and_its_gates_dismissed(tmp_path):
    browser = FakeBrowser([SIGNED_IN])
    await run_login(browser, tmp_path)
    assert browser.started is True
    assert browser.page.goto_calls == [REDDIT_LOGIN_URL]
    assert browser.gates_dismissed == 1


async def test_an_already_signed_in_profile_returns_at_once(tmp_path):
    result = await run_login(FakeBrowser([SIGNED_IN]), tmp_path)
    assert result == LoginResult(True, "someuser", None, True)


async def test_waiting_ends_when_the_sign_in_lands(tmp_path):
    browser = FakeBrowser([LOGGED_OUT, LOGGED_OUT, SIGNED_IN])
    result = await run_login(browser, tmp_path, timeout=10)
    assert (result.ok, result.username, result.already_logged_in) == (
        True,
        "someuser",
        False,
    )


async def test_a_closed_window_ends_the_wait(tmp_path):
    page = FakePage()
    page.closed = True
    result = await run_login(FakeBrowser([LOGGED_OUT], page=page), tmp_path, timeout=10)
    assert result.ok is False
    assert "closed" in (result.reason or "")


async def test_a_sign_in_completed_just_before_the_window_closed_still_counts(tmp_path):
    page = FakePage()
    page.closed = True
    result = await run_login(FakeBrowser([SIGNED_IN], page=page), tmp_path, timeout=10)
    assert result.ok is True


async def test_the_wait_gives_up_after_the_timeout(tmp_path):
    result = await run_login(FakeBrowser([LOGGED_OUT]), tmp_path, timeout=0)
    assert result.ok is False
    assert "sign-in" in (result.reason or "")


async def test_progress_is_reported_while_waiting(tmp_path):
    messages: List[str] = []
    await run_login(
        FakeBrowser([LOGGED_OUT, SIGNED_IN]),
        tmp_path,
        timeout=10,
        on_status=messages.append,
    )
    assert any("Sign in" in m for m in messages)
    assert any("Signed in" in m for m in messages)


async def test_an_already_signed_in_profile_reports_nothing_to_do(tmp_path):
    messages: List[str] = []
    await run_login(FakeBrowser([SIGNED_IN]), tmp_path, on_status=messages.append)
    assert messages == []


async def test_a_borrowed_browser_is_left_open_for_its_owner(tmp_path):
    browser = FakeBrowser([SIGNED_IN])
    await run_login(browser, tmp_path)
    assert browser.closed is False


async def test_a_launched_browser_is_always_closed(tmp_path, monkeypatch):
    """The profile is only written to disk when the context closes, including when the page blows up."""
    browser = FakeBrowser([SIGNED_IN])
    browser.page.goto = None  # type: ignore[assignment]  # navigating raises
    monkeypatch.setattr(login_module, "BrowserManager", lambda config: browser)
    with pytest.raises(TypeError):
        await async_login(profile_config(tmp_path))
    assert browser.closed is True


async def test_the_window_is_shown_even_when_the_config_says_headless(
    tmp_path, monkeypatch
):
    seen: List[ExtractorConfig] = []

    def capture(config: ExtractorConfig) -> FakeBrowser:
        seen.append(config)
        return FakeBrowser([SIGNED_IN])

    monkeypatch.setattr(login_module, "BrowserManager", capture)
    await async_login(profile_config(tmp_path).replace(headless=True))
    assert seen[0].headless is False


def test_the_sync_wrapper_runs_the_whole_thing(tmp_path):
    result = login(profile_config(tmp_path), browser=FakeBrowser([SIGNED_IN]))
    assert result == LoginResult(True, "someuser", None, True)


# -- CLI wiring -------------------------------------------------------------


def parse(argv: List[str]):
    """Parse ``argv`` with the real parser."""
    return cli.build_parser().parse_args(argv)


def test_login_takes_no_sources():
    args = parse(["--login", "--profile", "./p"])
    assert (args.login, args.sources, args.profile) == (True, [], "./p")


def test_login_without_a_profile_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--login"])
    assert exc.value.code == 2
    assert "--profile" in capsys.readouterr().err


def test_a_run_needs_sources_but_a_login_does_not(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "login", lambda config, **kw: LoginResult(True, "someuser")
    )
    assert cli.main(["--login", "--profile", "./p"]) == 0
    assert "Signed in as u/someuser" in capsys.readouterr().out


def test_the_login_carries_the_profile_and_user_agent(monkeypatch):
    seen: List[ExtractorConfig] = []

    def fake_login(config: ExtractorConfig, **kwargs: Any) -> LoginResult:
        seen.append(config)
        return LoginResult(True, "someuser")

    monkeypatch.setattr(cli, "login", fake_login)
    cli.main(["--login", "--profile", "./p", "--user-agent", "TestAgent/1.0"])
    assert seen[0].profile_dir == "./p"
    assert seen[0].user_agent == "TestAgent/1.0"
    assert seen[0].headless is False


def test_an_already_signed_in_profile_says_so(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "login", lambda config, **kw: LoginResult(True, "someuser", None, True)
    )
    assert cli.main(["--login", "--profile", "./p"]) == 0
    assert "Already signed in as u/someuser" in capsys.readouterr().out


def test_a_confirmed_login_without_a_name_still_succeeds(monkeypatch, capsys):
    monkeypatch.setattr(cli, "login", lambda config, **kw: LoginResult(True))
    assert cli.main(["--login", "--profile", "./p"]) == 0
    assert "account name unavailable" in capsys.readouterr().out


def test_a_failed_login_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "login",
        lambda config, **kw: LoginResult(False, reason="no sign-in within 300s"),
    )
    assert cli.main(["--login", "--profile", "./p"]) == 1
    assert "no sign-in within 300s" in capsys.readouterr().err


def test_sources_given_alongside_login_are_ignored_with_a_note(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "login", lambda config, **kw: LoginResult(True, "someuser")
    )
    assert cli.main(["r/pics", "--login", "--profile", "./p"]) == 0
    assert "ignores the sources" in capsys.readouterr().err


def test_a_browser_that_will_not_launch_is_reported(monkeypatch, capsys):
    def boom(config: ExtractorConfig, **kwargs: Any) -> LoginResult:
        raise BrowserError("could not launch Chromium: nope")

    monkeypatch.setattr(cli, "login", boom)
    assert cli.main(["--login", "--profile", "./p"]) == 1
    assert "could not launch Chromium" in capsys.readouterr().err


def test_an_interrupted_login_exits_130(monkeypatch):
    def interrupt(config: ExtractorConfig, **kwargs: Any) -> LoginResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "login", interrupt)
    assert cli.main(["--login", "--profile", "./p"]) == 130
