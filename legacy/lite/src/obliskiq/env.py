"""Environment loader.

Every path, key, port, token and timezone comes from the environment, so the same
image deploys unchanged to a cloud VM. Nothing here has a hardcoded secret, and
nothing falls back to a value that would be wrong in production.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import EnvVarError

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

#: Interfaces that keep the MCP server on the local machine. Binding anywhere
#: else exposes the position ledger and the tools that write to it, so an auth
#: token stops being optional.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def require(name: str) -> str:
    value = _get(name)
    if value is None:
        raise EnvVarError(f"required environment variable {name} is unset or empty")
    return value


def optional(name: str, default: str | None = None) -> str | None:
    return _get(name, default)


def require_int(name: str, default: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        if default is None:
            raise EnvVarError(f"required environment variable {name} is unset or empty")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise EnvVarError(f"environment variable {name} must be an integer, got {raw!r}") from exc


def _optional_int(name: str) -> int | None:
    """An integer that may be absent. No port, size or count has a code default."""
    raw = _get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise EnvVarError(f"environment variable {name} must be an integer, got {raw!r}") from exc


def require_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise EnvVarError(f"environment variable {name} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    """Resolved runtime environment. Constructed once at process start."""

    db_path: Path
    config_path: Path
    log_level: str
    log_format: str
    timezone: str

    mcp_host: str
    mcp_port: int | None
    mcp_auth_token: str | None

    fred_api_key: str | None
    eia_api_key: str | None
    marketdata_api_token: str | None

    ibkr_cp_base_url: str | None
    #: The IBKR Custom Connector's remote MCP endpoint. Diagnostic only today:
    #: `obliskiq-collect connector` reads its discovery documents. Unset simply
    #: means that command has nothing to probe, which is not an error anywhere
    #: else — no collector reads this.
    ibkr_connector_url: str | None
    ibkr_consumer_key: str | None
    ibkr_access_token: str | None
    ibkr_access_token_secret: str | None
    ibkr_signature_key_path: Path | None
    ibkr_encryption_key_path: Path | None
    ibkr_dh_param_path: Path | None
    ibkr_ca_bundle: str | None

    @property
    def binds_publicly(self) -> bool:
        return self.mcp_host not in LOOPBACK_HOSTS

    @property
    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise EnvVarError(f"OBLISK_TZ is not a known timezone: {self.timezone!r}") from exc

    def require_mcp_port(self) -> int:
        """The port the MCP server binds. Required only when the server starts —
        the collectors run as a separate process and bind nothing."""
        if self.mcp_port is None:
            raise EnvVarError(
                "OBLISK_MCP_PORT must be set to start the MCP server. There is no "
                "default port: a wrong one binds silently to the wrong place."
            )
        return self.mcp_port

    def require_mcp_auth_token(self) -> str:
        """The server refuses to serve a ledger unauthenticated off loopback."""
        if self.mcp_auth_token:
            return self.mcp_auth_token
        raise EnvVarError(
            "OBLISK_MCP_AUTH_TOKEN must be set when binding to a non-loopback interface "
            f"(OBLISK_MCP_HOST={self.mcp_host!r}). This server exposes a position ledger "
            "and the tools that write to it; it must never be reachable unauthenticated."
        )

    def validate(self) -> Settings:
        if self.binds_publicly:
            self.require_mcp_auth_token()
        for label, key_path in (
            ("OBLISK_IBKR_SIGNATURE_KEY", self.ibkr_signature_key_path),
            ("OBLISK_IBKR_ENCRYPTION_KEY", self.ibkr_encryption_key_path),
            ("OBLISK_IBKR_DH_PARAM", self.ibkr_dh_param_path),
        ):
            if key_path is not None and not key_path.is_file():
                raise EnvVarError(f"{label} points at a file that does not exist: {key_path}")
        if self.ibkr_ca_bundle is not None and not Path(self.ibkr_ca_bundle).is_file():
            raise EnvVarError(
                "OBLISK_IBKR_CA_BUNDLE points at a file that does not exist: "
                f"{self.ibkr_ca_bundle}. "
                "TLS verification is never disabled to work around this."
            )
        _ = self.tzinfo  # resolves the zone, raising if it is unknown
        return self


def load_settings(*, load_dotenv_file: bool = True) -> Settings:
    """Read the environment into a Settings. Loads a local .env first if present."""
    if load_dotenv_file:
        _load_dotenv()

    def _path_or_none(name: str) -> Path | None:
        raw = _get(name)
        return Path(raw).expanduser() if raw else None

    settings = Settings(
        db_path=Path(require("OBLISK_DB_PATH")).expanduser(),
        config_path=Path(_get("OBLISK_CONFIG_PATH") or _repo_root() / "config.yaml").expanduser(),
        log_level=(_get("OBLISK_LOG_LEVEL") or "INFO").upper(),
        log_format=(_get("OBLISK_LOG_FORMAT") or "json").lower(),
        timezone=_get("OBLISK_TZ") or "UTC",
        mcp_host=_get("OBLISK_MCP_HOST") or "127.0.0.1",
        mcp_port=_optional_int("OBLISK_MCP_PORT"),
        mcp_auth_token=_get("OBLISK_MCP_AUTH_TOKEN"),
        fred_api_key=_get("FRED_API_KEY"),
        eia_api_key=_get("EIA_API_KEY"),
        marketdata_api_token=_get("MARKETDATA_API_TOKEN"),
        ibkr_cp_base_url=_get("OBLISK_IBKR_CP_BASE_URL"),
        ibkr_connector_url=_get("OBLISK_IBKR_CONNECTOR_URL"),
        ibkr_consumer_key=_get("OBLISK_IBKR_CONSUMER_KEY"),
        ibkr_access_token=_get("OBLISK_IBKR_ACCESS_TOKEN"),
        ibkr_access_token_secret=_get("OBLISK_IBKR_ACCESS_TOKEN_SECRET"),
        ibkr_signature_key_path=_path_or_none("OBLISK_IBKR_SIGNATURE_KEY"),
        ibkr_encryption_key_path=_path_or_none("OBLISK_IBKR_ENCRYPTION_KEY"),
        ibkr_dh_param_path=_path_or_none("OBLISK_IBKR_DH_PARAM"),
        ibkr_ca_bundle=_get("OBLISK_IBKR_CA_BUNDLE"),
    )
    return settings.validate()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is optional at runtime
        return
    candidate = Path(os.environ.get("OBLISK_ENV_FILE") or _repo_root() / ".env")
    if candidate.is_file():
        load_dotenv(candidate, override=False)
