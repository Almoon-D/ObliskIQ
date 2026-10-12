"""Transport ``gateway``: IBKR's Client Portal Gateway on this machine, logged in headlessly.

``prepare`` installs the pinned Gateway (once), starts it unless one is serving, and logs it in
with the paper username and password unless it already holds a session. Requests then go to the
Gateway's local address, verified against its per-install certificate; the Gateway adds the
session itself, so no request is signed. ``recover`` restarts a dead Gateway and logs in again.

It refuses profile ``live``: a live login needs the owner's second factor on IBKR Mobile, which
an unattended process can never supply, so the Gateway never holds a live session here.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import ssl
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from ...config.schema_ibkr import IbkrConfig
from ...logging_setup import get_logger
from ...util.http import RetryPolicy
from ..errors import GatewayError, ProfileRefusedError, RecoveryDeferredError
from .base import TransportKind
from .gateway_install import GatewayInstall, ensure_installed
from .gateway_java import JavaRuntime
from .gateway_login import LoginDriver, LoginOutcome, PlaywrightLoginDriver
from .gateway_process import GatewayProcess

log = get_logger("ibkr.gateway")

HTTP_OK = 200

#: The endpoint, under the API base path, the Gateway answers with 200 once logged in and 401
#: before.
SESSION_PROBE_ENDPOINT = "tickle"


class GatewayTransport:
    """The Client Portal Gateway, installed, started and logged in on demand."""

    def __init__(
        self,
        ibkr: IbkrConfig,
        *,
        profile: Literal["paper", "live"],
        username: SecretStr,
        password: SecretStr,
        base_path: str,
        install_dir: Path,
        policy: RetryPolicy,
        timeout_seconds: float,
        java_home: Path | None = None,
        driver: LoginDriver | None = None,
        download_client: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if profile != "paper":
            raise ProfileRefusedError(
                "the Client Portal Gateway is paper-only here: a live login needs the owner's "
                "second factor, so profile live never logs in through it"
            )
        self._ibkr = ibkr
        self._username = username
        self._password = password
        self._base_url = ibkr.gateway.local_address.rstrip("/") + base_path
        self._probe_path = f"{base_path}/{SESSION_PROBE_ENDPOINT}"
        self._monotonic = monotonic
        self._last_login_at: float | None = None
        self._install_dir = install_dir
        self._policy = policy
        self._timeout = timeout_seconds
        self._java = JavaRuntime(java_home, inherit=tuple(ibkr.gateway.inherited_environment))
        self._driver: LoginDriver = (
            driver if driver is not None else PlaywrightLoginDriver(env=self._java.environment())
        )
        self._download_client = download_client
        self._install: GatewayInstall | None = None
        self._process: GatewayProcess | None = None
        self._lock = asyncio.Lock()

    @property
    def kind(self) -> TransportKind:
        return TransportKind.GATEWAY

    @property
    def base_url(self) -> str:
        return self._base_url

    def verify(self) -> ssl.SSLContext | bool:
        if self._install is None:
            raise GatewayError("the Gateway transport is used before prepare()")
        return self._install.ssl_context()

    async def _ensure_install(self) -> GatewayInstall:
        if self._install is None:
            await self._java.require(self._ibkr.gateway.java_major_versions)
            if self._download_client is not None:
                self._install = await ensure_installed(
                    self._ibkr,
                    self._install_dir,
                    client=self._download_client,
                    policy=self._policy,
                    java=self._java,
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    self._install = await ensure_installed(
                        self._ibkr,
                        self._install_dir,
                        client=client,
                        policy=self._policy,
                        java=self._java,
                    )
        return self._install

    async def _logged_in(self, install: GatewayInstall) -> bool:
        try:
            async with httpx.AsyncClient(
                verify=install.ssl_context(), timeout=self._timeout, trust_env=False
            ) as client:
                response = await client.post(
                    self._ibkr.gateway.local_address.rstrip("/") + self._probe_path
                )
        except httpx.HTTPError:
            return False
        return response.status_code == HTTP_OK

    async def _login(self, install: GatewayInstall) -> None:
        login = self._ibkr.gateway.login
        url = self._ibkr.gateway.local_address.rstrip("/") + login.route
        interval = login.min_relogin_interval_seconds
        if self._last_login_at is not None:
            since = self._monotonic() - self._last_login_at
            if since < interval:
                raise RecoveryDeferredError(
                    f"the last Gateway login attempt was {since:.0f} s ago; the next is allowed in "
                    f"{interval - since:.0f} s, so repeated logins cannot lock the username"
                )
        details: list[str] = []
        self._last_login_at = self._monotonic()
        for attempt in range(1, login.max_attempts + 1):
            try:
                outcome = await self._driver.login(
                    url=url,
                    username=self._username.get_secret_value(),
                    password=self._password.get_secret_value(),
                    spki_pin=install.spki_pin(),
                    ibkr=self._ibkr,
                )
            except Exception as exc:  # a browser that fails is a failed login
                outcome = LoginOutcome(
                    False, None, f"the login browser failed: {type(exc).__name__}"
                )
            if outcome.succeeded and await self._logged_in(install):
                log.info("Gateway logged in", extra={"attempt": attempt})
                return
            details.append(outcome.detail if not outcome.succeeded else "no session after login")
            if outcome.credentials_accepted is False:
                break  # a refused password is not retried: repeats can lock the username
        raise GatewayError("the Gateway's headless login failed: " + " | ".join(details))

    async def _start_and_login(self) -> None:
        install = await self._ensure_install()
        if self._process is None:
            self._process = GatewayProcess(
                install,
                ibkr=self._ibkr,
                readiness_path=self._probe_path,
                java=self._java,
            )
        if not await self._process.answering():
            await self._process.start()
        if not await self._logged_in(install):
            await self._login(install)

    async def prepare(self) -> None:
        async with self._lock:
            await self._start_and_login()

    async def headers(self, method: str, url: str, params: Mapping[str, Any]) -> dict[str, str]:
        del method, url, params  # the Gateway adds the session itself; nothing is signed
        return {"Accept": "application/json"}

    async def recover(self) -> None:
        async with self._lock:
            log.warning("re-establishing the Gateway session")
            await self._start_and_login()

    async def aclose(self) -> None:
        if self._process is not None:
            await self._process.stop()
