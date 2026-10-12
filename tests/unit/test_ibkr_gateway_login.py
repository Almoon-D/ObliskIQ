"""The Playwright login driver, against a fake browser (CI has no browsers).

The real page is exercised by the live trial (docs/knowledge/ibkr.md); these tests pin the
driver's steps and verdicts: paper first, typed credentials, a pause, submit, then success, an
error shown on the form, or a password IBKR accepted without a session.
"""

import sys
import types
from typing import Any

import pytest

from obliskiq.ibkr.transport.gateway_login import PlaywrightLoginDriver


class FakeError(Exception):
    """Stands in for ``playwright.async_api.Error``."""


class FakeResponse:
    def __init__(self, url: str, body: str, *, fail: bool = False) -> None:
        self.url = url
        self._body = body
        self._fail = fail

    async def text(self) -> str:
        if self._fail:
            raise FakeError("body gone")
        return self._body


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        return len(self.page.elements.get(self.selector, []))

    @property
    def first(self) -> FakeLocator:
        return self

    def nth(self, index: int) -> FakeElement:
        return FakeElement(self.page.elements[self.selector][index])

    async def click(self) -> None:
        self.page.actions.append(("click", self.selector))
        if self.selector == self.page.submit:
            for handler in self.page.handlers:
                for response in self.page.responses:
                    await handler(response)


class FakeElement:
    def __init__(self, text: str) -> None:
        self.text = text

    async def inner_text(self) -> str:
        return self.text


class FakeKeyboard:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def type(self, text: str, delay: float) -> None:
        self.page.actions.append(("type", text, delay))

    async def press(self, key: str) -> None:
        self.page.actions.append(("press", key))


class FakePage:
    def __init__(self, scenario: dict[str, Any], submit: str) -> None:
        self.scenario = scenario
        self.submit = submit
        self.elements: dict[str, list[str]] = scenario.get("elements", {})
        self.responses: list[FakeResponse] = scenario.get("responses", [])
        self.handlers: list[Any] = []
        self.actions: list[tuple[Any, ...]] = []
        self.keyboard = FakeKeyboard(self)

    def on(self, event: str, handler: Any) -> None:
        assert event == "response"
        self.handlers.append(handler)

    async def goto(self, url: str, timeout: float) -> None:
        if self.scenario.get("goto_fails"):
            raise FakeError("net::ERR_CONNECTION_REFUSED")
        self.actions.append(("goto", url))

    async def wait_for_selector(self, selector: str, timeout: float) -> None:
        self.actions.append(("wait", selector))

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def click(self, selector: str) -> None:
        self.actions.append(("click", selector))

    async def wait_for_timeout(self, ms: float) -> None:
        self.actions.append(("pause", ms))

    async def wait_for_function(self, script: str, arg: str, timeout: float) -> None:
        if not self.scenario.get("succeeds"):
            raise FakeError("timeout")
        self.actions.append(("success", arg))


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, browser: FakeBrowser, launches: list[dict[str, Any]]) -> None:
        self.browser = browser
        self.launches = launches
        self.chromium = self

    async def launch(self, **kwargs: Any) -> FakeBrowser:
        self.launches.append(kwargs)
        return self.browser

    async def __aenter__(self) -> FakePlaywright:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def run_login(monkeypatch, shipped_settings):
    async def run(scenario: dict[str, Any]) -> tuple[Any, FakePage, FakeBrowser, list[Any]]:
        login = shipped_settings.ibkr.gateway.login
        page = FakePage(scenario, login.submit_selector)
        browser = FakeBrowser(page)
        launches: list[dict[str, Any]] = []
        module = types.ModuleType("playwright.async_api")
        module.Error = FakeError  # type: ignore[attr-defined]
        module.Response = FakeResponse  # type: ignore[attr-defined]
        module.async_playwright = lambda: FakePlaywright(browser, launches)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "playwright.async_api", module)
        outcome = await PlaywrightLoginDriver(executable_path="/opt/chrome").login(
            url="https://localhost:5000/sso/Login",
            username="paper-user",
            password="paper-pass",
            spki_pin="PIN=",
            ibkr=shipped_settings.ibkr,
        )
        return outcome, page, browser, launches

    return run


def _elements(login: Any, **extra: list[str]) -> dict[str, list[str]]:
    return {login.paper_toggle_selector: ["Paper"], **extra}


async def test_a_successful_login_switches_to_paper_types_pauses_and_submits(
    run_login, shipped_settings
):
    login = shipped_settings.ibkr.gateway.login
    outcome, page, browser, launches = await run_login(
        {"succeeds": True, "elements": _elements(login)}
    )
    assert outcome.succeeded
    assert outcome.credentials_accepted
    (launch,) = launches
    assert launch["headless"] is True
    assert launch["args"] == ["--ignore-certificate-errors-spki-list=PIN="]
    assert launch["executable_path"] == "/opt/chrome"
    kinds = [action[0] for action in page.actions]
    assert kinds.index("click") < kinds.index("type"), "paper is selected before typing"
    typed = [action for action in page.actions if action[0] == "type"]
    assert [t[1] for t in typed] == ["paper-user", "paper-pass"]
    assert all(t[2] == login.keystroke_delay_ms for t in typed)
    assert ("pause", login.presubmit_wait_seconds * 1000.0) in page.actions
    assert ("success", login.success_text) in page.actions
    assert browser.closed


async def test_a_form_without_a_paper_toggle_is_refused(run_login):
    outcome, page, browser, _ = await run_login({"succeeds": True, "elements": {}})
    assert not outcome.succeeded
    assert "no paper toggle" in outcome.detail
    assert not [a for a in page.actions if a[0] == "type"], "nothing is typed"
    assert browser.closed


async def test_an_error_on_the_form_is_reported_without_the_credentials(
    run_login, shipped_settings
):
    login = shipped_settings.ibkr.gateway.login
    outcome, _, _, _ = await run_login(
        {
            "elements": _elements(login, **{login.error_selector: ["Authentication failed", " "]}),
            "responses": [
                FakeResponse("https://localhost:5000/sso/Authenticator", '{"B": "x"}'),
                FakeResponse("https://localhost:5000/sso/report", "{}"),
            ],
        }
    )
    assert not outcome.succeeded
    assert outcome.credentials_accepted is None
    assert outcome.detail == "the login did not succeed (Authentication failed)"
    assert "paper-pass" not in outcome.detail
    assert "paper-user" not in outcome.detail


async def test_an_accepted_password_without_a_session_says_so(run_login, shipped_settings):
    login = shipped_settings.ibkr.gateway.login
    outcome, _, _, _ = await run_login(
        {
            "elements": _elements(login),
            "responses": [
                FakeResponse("https://localhost:5000/sso/Authenticator", "not json"),
                FakeResponse("https://localhost:5000/sso/Authenticator", "", fail=True),
                FakeResponse(
                    "https://localhost:5000/sso/Authenticator",
                    '{"M2": "proof", "reached_max_login": false}',
                ),
            ],
        }
    )
    assert not outcome.succeeded
    assert outcome.credentials_accepted is True
    assert "accepted the credentials" in outcome.detail
    assert "no error shown" in outcome.detail


async def test_a_refused_password_is_reported_as_refused(run_login, shipped_settings):
    login = shipped_settings.ibkr.gateway.login
    outcome, _, _, _ = await run_login(
        {
            "elements": _elements(login),
            "responses": [
                FakeResponse(
                    "https://localhost:5000/sso/Authenticator", '{"reached_max_login": true}'
                )
            ],
        }
    )
    assert outcome.credentials_accepted is False


async def test_a_page_that_fails_to_load_is_reported_and_the_browser_closed(run_login):
    outcome, _, browser, _ = await run_login({"goto_fails": True})
    assert not outcome.succeeded
    assert outcome.detail == "the login page failed: FakeError"
    assert browser.closed
