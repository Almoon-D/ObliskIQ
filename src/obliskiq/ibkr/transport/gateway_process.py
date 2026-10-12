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

from ...logging_setup import get_logger
from ..errors import GatewayError
from .gateway_install import CONF_FILE, RUN_SCRIPT, GatewayInstall
from .gateway_java import JavaRuntime

log = get_logger("ibkr.gateway.process")

#: The path probed for readiness. Any status means the Gateway is serving.
READINESS_PATH = "/v1/api/tickle"

#: Seconds between readiness probes, and the timeout of one probe.
_PROBE_INTERVAL_SECONDS = 1.0

#: Seconds a stopping Gateway is given before it is killed.
_STOP_GRACE_SECONDS = 10.0

LOG_FILE = "gateway-stdout.log"


class GatewayProcess:
    """The Gateway's JVM: started on demand, stopped only if this process started it."""

    def __init__(
        self,
        install: GatewayInstall,
        *,
        local_address: str,
        startup_timeout: float,
        java: JavaRuntime,
    ) -> None:
        self._install = install
        self._java = java
        self._address = local_address.rstrip("/")
        self._timeout = startup_timeout
        self._process: asyncio.subprocess.Process | None = None

    @property
    def started_here(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def answering(self) -> bool:
        """Is a Gateway serving on the configured address, verified by the pinned certificate?"""
        try:
            async with httpx.AsyncClient(
                verify=self._install.ssl_context(), timeout=_PROBE_INTERVAL_SECONDS, trust_env=False
            ) as client:
                await client.get(self._address + READINESS_PATH)
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
        deadline = loop.time() + self._timeout
        while loop.time() < deadline:
            if self._process.returncode is not None:
                raise GatewayError(
                    f"the Gateway exited with status {self._process.returncode} while starting; "
                    f"see {log_path.name} in its install directory"
                )
            if await self.answering():
                log.info("Gateway started")
                return
            await asyncio.sleep(_PROBE_INTERVAL_SECONDS)
        await self.stop()
        raise GatewayError(f"the Gateway did not answer within {self._timeout:.0f} s")

    async def stop(self) -> None:
        """Stop the Gateway if this process started it."""
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=_STOP_GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        log.info("Gateway stopped")
