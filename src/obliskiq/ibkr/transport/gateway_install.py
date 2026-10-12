"""Installing the Client Portal Gateway: a pinned archive, verified, with its own certificate.

IBKR publishes the Gateway at one unversioned URL. The archive is accepted only when its size and
SHA-256 match ``ibkr.gateway`` exactly; anything else is refused before a byte of it is unpacked,
because a changed archive is a changed program that has not been reviewed.

The shipped certificate expired in 2019 and names no host (``CN=Client Portal Web API``), so a
client could only talk to it with verification disabled. Instead each install generates its own
RSA key pair for ``localhost`` (``keytool``, which the Gateway's Java runtime provides), points
``root/conf.yaml`` at it, and exports the certificate. Clients then verify the Gateway against
exactly that certificate, with hostname checks on: the certificate is pinned, never ignored.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import base64
import hashlib
import re
import secrets
import shutil
import ssl
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from ...config.schema_ibkr import IbkrConfig
from ...logging_setup import get_logger
from ...util.http import RetryPolicy, request_with_retry
from ..errors import GatewayError
from .gateway_java import JavaRuntime

log = get_logger("ibkr.gateway.install")

HTTP_OK = 200

#: Characters of the archive digest that name its install directory.
_DIGEST_PREFIX_CHARS = 12

#: Owner-only permissions for the keystore password and key material.
_PRIVATE_MODE = 0o600

#: The owner-execute bit, set on the launcher script.
_OWNER_EXECUTE = 0o100

#: Bytes of entropy in a generated keystore password (hex-encoded: twice as many characters).
_KEYSTORE_PASSWORD_BYTES = 24

#: Files inside the archive the install depends on.
RUN_SCRIPT = PurePosixPath("bin/run.sh")
CONF_FILE = PurePosixPath("root/conf.yaml")
KEYSTORE_FILE = "obliskiq.jks"
CERT_FILE = "gateway-cert.pem"
PASSWORD_FILE = "keystore.pass"
MARKER_FILE = ".installed"
KEY_ALIAS = "localhost"
KEY_SIZE_BITS = 2048


@dataclass(frozen=True)
class GatewayInstall:
    """An unpacked, verified Gateway with its pinned certificate."""

    root: Path
    cert_path: Path

    def ssl_context(self) -> ssl.SSLContext:
        """Verification against exactly the install's certificate, hostname checks on."""
        context = ssl.create_default_context(cafile=str(self.cert_path))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    def spki_pin(self) -> str:
        """Base64 SHA-256 of the certificate's public key: the browser pins this key."""
        cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
        spki = cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def install_root(ibkr: IbkrConfig, install_dir: Path) -> Path:
    return install_dir / ibkr.gateway.sha256[:_DIGEST_PREFIX_CHARS]


def existing_install(ibkr: IbkrConfig, install_dir: Path) -> GatewayInstall | None:
    """The install for the pinned archive, when it is complete."""
    root = install_root(ibkr, install_dir)
    cert = root / CERT_FILE
    if (root / MARKER_FILE).is_file() and cert.is_file() and (root / str(RUN_SCRIPT)).is_file():
        return GatewayInstall(root=root, cert_path=cert)
    return None


def verify_archive(data: bytes, ibkr: IbkrConfig) -> None:
    """Refuse an archive whose size or SHA-256 is not the pinned one."""
    pinned = ibkr.gateway.size_bytes
    if len(data) != pinned:
        raise GatewayError(
            f"the Gateway archive is {len(data)} bytes, not the pinned {pinned}; "
            "IBKR may have published a new build. Review it and update ibkr.gateway before use."
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != ibkr.gateway.sha256:
        raise GatewayError(
            f"the Gateway archive's SHA-256 is {digest}, not the pinned {ibkr.gateway.sha256}; "
            "refusing to install it"
        )


def extract_archive(archive: Path, root: Path) -> None:
    """Unpack ``archive`` into ``root``, refusing any member that would land outside it."""
    resolved = root.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (root / member.filename).resolve()
            if target != resolved and resolved not in target.parents:
                raise GatewayError(f"the Gateway archive has a member outside its root: {member}")
        bundle.extractall(root)


def patch_conf(conf: Path, *, port: int, keystore: str, password: str) -> None:
    """Point ``conf.yaml`` at the generated keystore and the configured port.

    Edited line by line rather than re-serialised, so the rest of IBKR's file stays as shipped.
    """
    replacements = {
        "sslCert": f'"{keystore}"',
        "sslPwd": f'"{password}"',
        "listenPort": str(port),
        "listenSsl": "true",
    }
    lines = conf.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(sslCert|sslPwd|listenPort|listenSsl):", line)
        if match:
            indent, key = match.groups()
            lines[index] = f"{indent}{key}: {replacements[key]}"
            seen.add(key)
    missing = sorted(set(replacements) - seen)
    if missing:
        raise GatewayError(f"the Gateway's conf.yaml has no {', '.join(missing)} setting to set")
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")


#: The variable that hands keytool the keystore password.
KEYSTORE_PASSWORD_ENV = "GATEWAY_KEYSTORE_PASSWORD"


async def _run(*args: str, env: dict[str, str]) -> None:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        # keytool echoes its arguments on some failures; only the exit status is reported.
        raise GatewayError(f"{args[0]} failed with exit status {process.returncode}")
    del output


async def generate_keystore(
    root: Path, *, validity_days: int, password: str, java: JavaRuntime
) -> Path:
    """A fresh key pair for localhost in a keystore, and its certificate exported as PEM."""
    keytool = java.tool("keytool")
    keystore = root / "root" / KEYSTORE_FILE
    cert = root / CERT_FILE
    keystore.unlink(missing_ok=True)
    # The password reaches keytool through its environment, never its command line, which any
    # local user can read in the process list.
    env = {**java.environment(), KEYSTORE_PASSWORD_ENV: password}
    await _run(
        keytool, "-genkeypair", "-alias", KEY_ALIAS, "-keyalg", "RSA",
        "-keysize", str(KEY_SIZE_BITS), "-validity", str(validity_days),
        "-dname", "CN=localhost", "-ext", "SAN=dns:localhost,ip:127.0.0.1",
        "-keystore", str(keystore), "-storetype", "JKS",
        "-storepass:env", KEYSTORE_PASSWORD_ENV, "-keypass:env", KEYSTORE_PASSWORD_ENV,
        env=env,
    )  # fmt: skip
    await _run(
        keytool, "-exportcert", "-rfc", "-alias", KEY_ALIAS, "-keystore", str(keystore),
        "-storepass:env", KEYSTORE_PASSWORD_ENV, "-file", str(cert),
        env=env,
    )  # fmt: skip
    keystore.chmod(_PRIVATE_MODE)
    return cert


async def download_archive(
    ibkr: IbkrConfig, client: httpx.AsyncClient, policy: RetryPolicy
) -> bytes:
    url = ibkr.gateway.download_url
    response = await request_with_retry(client, "GET", url, policy=policy)
    if response.status_code != HTTP_OK:
        raise GatewayError(
            f"downloading the Gateway from {urlsplit(url).netloc} returned "
            f"HTTP {response.status_code}"
        )
    return response.content


async def ensure_installed(
    ibkr: IbkrConfig,
    install_dir: Path,
    *,
    client: httpx.AsyncClient,
    policy: RetryPolicy,
    java: JavaRuntime,
) -> GatewayInstall:
    """The verified install for the pinned archive, downloading and preparing it if needed."""
    found = existing_install(ibkr, install_dir)
    if found is not None:
        return found
    root = install_root(ibkr, install_dir)
    data = await download_archive(ibkr, client, policy)
    verify_archive(data, ibkr)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    archive = root / "clientportal.gw.zip"
    archive.write_bytes(data)
    extract_archive(archive, root)
    archive.unlink()
    password = secrets.token_hex(_KEYSTORE_PASSWORD_BYTES)
    password_file = root / PASSWORD_FILE
    password_file.write_text(password, encoding="utf-8")
    password_file.chmod(_PRIVATE_MODE)
    cert = await generate_keystore(
        root, validity_days=ibkr.gateway.keystore_validity_days, password=password, java=java
    )
    port = urlsplit(ibkr.gateway.local_address).port
    if port is None:  # pragma: no cover - the schema requires a port
        raise GatewayError("ibkr.gateway.local_address names no port")
    patch_conf(root / str(CONF_FILE), port=port, keystore=KEYSTORE_FILE, password=password)
    (root / str(CONF_FILE)).chmod(_PRIVATE_MODE)
    script = root / str(RUN_SCRIPT)
    script.chmod(script.stat().st_mode | _OWNER_EXECUTE)
    (root / MARKER_FILE).write_text(ibkr.gateway.sha256 + "\n", encoding="utf-8")
    log.info("Gateway installed", extra={"install": root.name})
    return GatewayInstall(root=root, cert_path=cert)
