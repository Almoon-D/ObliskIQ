"""The Java runtime the Client Portal Gateway runs on, and the check that it is a known-good one.

The Gateway build IBKR publishes (20230424154245) accepts the paper password on Java 21 and then
re-renders the login form instead of opening the session; on Java 11 the same login succeeds
[CP ✓ 2026-10-12]. So the runtime is chosen (``OBLISK_IBKR_GATEWAY_JAVA_HOME``, else the ``java``
on ``PATH``) and its major version must be one ``ibkr.gateway.java_major_versions`` lists; any
other is refused before the Gateway starts, with the reason, rather than failing at login with
none.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..errors import GatewayError

#: ``java -version`` prints ``version "11.0.32.1"`` (or ``"1.8.0_392"`` for Java 8).
_VERSION_RE = re.compile(r'version "(\d+)(?:\.(\d+))?')

#: Java 8 and earlier report ``1.<major>``.
_LEGACY_PREFIX = 1


def parse_major_version(output: str) -> int | None:
    match = _VERSION_RE.search(output)
    if match is None:
        return None
    first = int(match.group(1))
    if first == _LEGACY_PREFIX and match.group(2) is not None:
        return int(match.group(2))
    return first


#: The prefix of ObliskIQ's environment variables, kept out of child processes.
OWN_PREFIX = "OBLISK_"


@dataclass(frozen=True)
class JavaRuntime:
    """A Java home, or ``None`` for whatever ``java`` the ``PATH`` holds."""

    home: Path | None

    def tool(self, name: str) -> str:
        """The path of a JDK/JRE tool (``java``, ``keytool``)."""
        if self.home is not None:
            candidate = self.home / "bin" / name
            if candidate.is_file():
                return str(candidate)
            raise GatewayError(f"the Java runtime has no {name}")
        found = shutil.which(name)
        if found is None:
            raise GatewayError(
                f"{name} was not found: the Gateway needs a Java runtime "
                "(set OBLISK_IBKR_GATEWAY_JAVA_HOME)"
            )
        return found

    def environment(self) -> dict[str, str]:
        """The environment the Gateway's launcher runs with: this runtime first on ``PATH``.

        ObliskIQ's own variables are left out: the Gateway and the JDK tools need none of them,
        and they carry the broker credentials.
        """
        env = {name: value for name, value in os.environ.items() if not name.startswith(OWN_PREFIX)}
        if self.home is not None:
            env["JAVA_HOME"] = str(self.home)
            env["PATH"] = f"{self.home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        return env

    async def major_version(self) -> int | None:
        process = await asyncio.create_subprocess_exec(
            self.tool("java"),
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.environment(),
        )
        output, _ = await process.communicate()
        return parse_major_version(output.decode("utf-8", errors="replace"))

    async def require(self, allowed: Sequence[int]) -> int:
        """The runtime's major version, refused unless it is one of ``allowed``."""
        major = await self.major_version()
        if major is None or major not in allowed:
            raise GatewayError(
                f"the Gateway's Java runtime is version {major or 'unknown'}; this Gateway build "
                f"is known to work on {', '.join(str(v) for v in allowed)} "
                "(set OBLISK_IBKR_GATEWAY_JAVA_HOME to such a runtime)"
            )
        return major
