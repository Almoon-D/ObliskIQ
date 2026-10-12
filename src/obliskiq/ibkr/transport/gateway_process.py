"""Starting and stopping the Client Portal Gateway's Java process.

The Gateway is launched with IBKR's own script (``bin/run.sh root/conf.yaml``) from its install
root, in its own process group so that stopping it also stops the JVM the script starts. It is up
when its TLS port answers any HTTP status through the pinned certificate; before login every API
path answers 401. A Gateway already listening on the port (started by a supervisor, or left
running) is used as it is and never stopped by this process.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import contextlib
import os
import signal
from pathlib import Path

import httpx

from ...config.schema_ibkr import IbkrConfig
from ...logging_setup import get_logger
from ..errors import GatewayError
from .gateway_install import CONF_FILE, RUN_SCRIPT, GatewayInstall
from .gateway_java import JavaRuntime

log = get_logger("ibkr.gateway.process")

LOG_FILE = "gateway-stdout.log"


class GatewayProcess:
    """The Gateway's JVM: started on demand, stopped only if this process started it."""

    def __init__(
        self,
        install: GatewayInstall,
        *,
        ibkr: IbkrConfig,
        readiness_path: str,
        java: JavaRuntime,
    ) -> None:
        self._install = install
        self._java = java
        # Any status on this path means the Gateway is serving; before login it answers 401.
        self._readiness_url = ibkr.gateway.local_address.rstrip("/") + readiness_path
        self._ibkr = ibkr
        self._process: asyncio.subprocess.Process | None = None

    @property
    def started_here(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def answering(self) -> bool:
        """Is a Gateway serving on the configured address, verified by the pinned certificate?"""
        try:
            async with httpx.AsyncClient(
                verify=self._install.ssl_context(),
                timeout=self._ibkr.gateway.probe_interval_seconds,
                trust_env=False,
            ) as client:
                await client.get(self._readiness_url)
        except httpx.HTTPError:
            return False
        return True

    async def start(self) -> None:
        """Start the Gateway unless one is already serving; wait until it answers."""
        if await self.answering():
            log.info("a Gateway is already serving; using it")
            return
        root: Path = self._install.root
        log_path = root / LOG_FILE
        with log_path.open("ab") as output:
            self._process = await asyncio.create_subprocess_exec(
                "bash",
                str(RUN_SCRIPT),
                str(CONF_FILE),
                cwd=root,
                env=self._java.environment(),
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        loop = asyncio.get_running_loop()
        timeout = self._ibkr.gateway.startup_timeout_seconds
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._process.returncode is not None:
                raise GatewayError(
                    f"the Gateway exited with status {self._process.returncode} while starting; "
                    f"see {log_path.name} in its install directory"
                )
            if await self.answering():
                log.info("Gateway started")
                return
            await asyncio.sleep(self._ibkr.gateway.probe_interval_seconds)
        await self.stop()
        raise GatewayError(f"the Gateway did not answer within {timeout:.0f} s")

    async def stop(self) -> None:
        """Stop the Gateway if this process started it."""
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self._ibkr.gateway.stop_grace_seconds)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        log.info("Gateway stopped")
