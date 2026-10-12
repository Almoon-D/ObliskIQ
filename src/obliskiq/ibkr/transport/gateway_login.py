"""Logging the Client Portal Gateway in with a headless browser (paper accounts only).

The Gateway holds the brokerage session once someone logs in through its web page. Paper logins
need no second factor, so a headless Chromium driven by Playwright does it: switch the form to
Paper, type the username and password one key at a time, pause, submit, and wait for the
Gateway's "Client login succeeds" page or the form's error. In the style of IBeam (Apache-2.0,
reference only); the selectors are configuration because IBKR changes the page.

The browser trusts the Gateway's per-install certificate by pinning its public key
(``--ignore-certificate-errors-spki-list``): only that key is accepted, which is pinning, not
verification switched off. The password is typed into the page and never logged; the outcome
names which step failed, and whether IBKR accepted the credentials (the SRP proof ``M2`` came
back), which separates a wrong password from a login that did not reach the Gateway.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol

from ...config.schema_ibkr import IbkrConfig
from ...logging_setup import get_logger

log = get_logger("ibkr.gateway.login")

#: The SSO step whose answer carries the server's proof that the password matched.
AUTHENTICATOR_PATH = "/sso/Authenticator"

#: Milliseconds per second, for Playwright's timeouts.
_MS_PER_SECOND = 1000.0


@dataclass(frozen=True)
class LoginOutcome:
    """What one login attempt achieved. ``detail`` never carries a credential."""

    succeeded: bool
    credentials_accepted: bool | None
    detail: str


class LoginDriver(Protocol):
    async def login(
        self,
        *,
        url: str,
        username: str,
        password: str,
        spki_pin: str,
        ibkr: IbkrConfig,
    ) -> LoginOutcome: ...


def _proof_returned(body: str) -> bool | None:
    """``True`` when an Authenticator answer carries the server proof, ``False`` when it says
    the credentials failed, ``None`` when the answer is not one of those."""
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("M2"):
        return True
    if "M2" in payload or payload.get("reached_max_login"):
        return False
    return None


class PlaywrightLoginDriver:
    """:class:`LoginDriver` over a headless Chromium (Playwright)."""

    def __init__(self, *, executable_path: str | None = None) -> None:
        self._executable_path = executable_path

    async def login(
        self,
        *,
        url: str,
        username: str,
        password: str,
        spki_pin: str,
        ibkr: IbkrConfig,
    ) -> LoginOutcome:
        login = ibkr.gateway.login
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import Response, async_playwright

        accepted: list[bool] = []

        async def on_response(response: Response) -> None:
            if AUTHENTICATOR_PATH not in response.url:
                return
            try:
                verdict = _proof_returned(await response.text())
            except PlaywrightError:
                return
            if verdict is not None:
                accepted.append(verdict)

        page_timeout = login.page_timeout_seconds * _MS_PER_SECOND
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=self._executable_path,
                args=[f"--ignore-certificate-errors-spki-list={spki_pin}"],
            )
            try:
                page = await browser.new_page()
                page.on("response", on_response)
                await page.goto(url, timeout=page_timeout)
                await page.wait_for_selector(login.username_selector, timeout=page_timeout)
                toggle = page.locator(login.paper_toggle_selector)
                if await toggle.count() == 0:
                    return LoginOutcome(False, None, "the login form offers no paper toggle")
                await toggle.first.click()
                await page.click(login.username_selector)
                await page.keyboard.type(username, delay=login.keystroke_delay_ms)
                await page.click(login.password_selector)
                await page.keyboard.type(password, delay=login.keystroke_delay_ms)
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(login.presubmit_wait_seconds * _MS_PER_SECOND)
                await page.locator(login.submit_selector).first.click()
                success_script = (
                    "text => document.body !== null && document.body.innerText.includes(text)"
                )
                try:
                    await page.wait_for_function(
                        success_script,
                        arg=login.success_text,
                        timeout=login.success_timeout_seconds * _MS_PER_SECOND,
                    )
                except PlaywrightError:
                    errors = page.locator(login.error_selector)
                    texts = [
                        (await errors.nth(i).inner_text()).strip()
                        for i in range(await errors.count())
                    ]
                    shown = "; ".join(text for text in texts if text) or "no error shown"
                    verdict = accepted[-1] if accepted else None
                    if verdict is True:
                        detail = (
                            "IBKR accepted the credentials but the Gateway session did not "
                            f"authenticate ({shown})"
                        )
                    else:
                        detail = f"the login did not succeed ({shown})"
                    return LoginOutcome(False, verdict, detail)
                return LoginOutcome(True, True, "logged in")
            except PlaywrightError as exc:
                return LoginOutcome(False, None, f"the login page failed: {type(exc).__name__}")
            finally:
                await browser.close()
