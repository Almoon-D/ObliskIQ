"""The process environment: credentials, deployment locations and logging overrides.

Credentials and deployment locations come from the environment, so the same code runs
unchanged in a cloud session, on the bot's machine and in CI; values that are decisions
(thresholds, budgets, schedules) live in ``config/`` instead. The one overlap is logging:
``OBLISK_LOG_LEVEL`` and ``OBLISK_LOG_FORMAT`` override the ``logging`` block of the
configuration for one process.

Anything that grants access or identifies the owner is a :class:`~pydantic.SecretStr` —
keys, tokens, the password, the account id, the Gateway username, the Telegram chat id and
the SEC contact string (it carries an email address) — so it never appears in a repr, a
log line or an error message. Validation errors name the variable, never its value.

LLM keys are not declared here: the LLM layer reads them by the names ``config/llm.yaml``
declares. The OAuth realm and the Gateway's local address are configuration.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import EnvVarError

type LogLevelName = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
type LogFormatName = Literal["json", "text"]


def repository_root() -> Path | None:
    """The source checkout this package runs from, or ``None`` for an installed wheel."""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "obliskiq").is_dir():
        return candidate
    return None


def default_env_file() -> Path | None:
    """``.env`` at the root of a source checkout, when one exists (it is gitignored)."""
    root = repository_root()
    if root is None:
        return None
    candidate = root / ".env"
    return candidate if candidate.is_file() else None


class Environment(BaseSettings):
    """The environment, read once at process start. Every variable is optional here; the
    code that needs one asks for it and fails loudly when it is absent."""

    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    # Data sources.
    fred_api_key: SecretStr | None = Field(default=None, validation_alias="FRED_API_KEY")
    eia_api_key: SecretStr | None = Field(default=None, validation_alias="EIA_API_KEY")
    sec_user_agent: SecretStr | None = Field(default=None, validation_alias="SEC_USER_AGENT")

    # Telegram.
    telegram_bot_token: SecretStr | None = Field(
        default=None, validation_alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_chat_id: SecretStr | None = Field(default=None, validation_alias="TELEGRAM_CHAT_ID")

    # IBKR: profile, account and transport.
    ibkr_profile: Literal["paper", "live"] | None = Field(
        default=None, validation_alias="OBLISK_IBKR_PROFILE"
    )
    ibkr_account_id: SecretStr | None = Field(
        default=None, validation_alias="OBLISK_IBKR_ACCOUNT_ID"
    )
    ibkr_transport: Literal["oauth", "gateway"] | None = Field(
        default=None, validation_alias="OBLISK_IBKR_TRANSPORT"
    )

    # IBKR: Client Portal Gateway login.
    ibkr_gateway_username: SecretStr | None = Field(
        default=None, validation_alias="OBLISK_IBKR_GATEWAY_USERNAME"
    )
    ibkr_gateway_password: SecretStr | None = Field(
        default=None, validation_alias="OBLISK_IBKR_GATEWAY_PASSWORD"
    )

    # IBKR: OAuth 1.0a.
    ibkr_cp_base_url: str | None = Field(default=None, validation_alias="OBLISK_IBKR_CP_BASE_URL")
    ibkr_consumer_key: SecretStr | None = Field(
        default=None, validation_alias="OBLISK_IBKR_CONSUMER_KEY"
    )
    ibkr_access_token: SecretStr | None = Field(
        default=None, validation_alias="OBLISK_IBKR_ACCESS_TOKEN"
    )
    ibkr_access_token_secret: SecretStr | None = Field(
        default=None, validation_alias="OBLISK_IBKR_ACCESS_TOKEN_SECRET"
    )
    ibkr_signature_key_path: Path | None = Field(
        default=None, validation_alias="OBLISK_IBKR_SIGNATURE_KEY"
    )
    ibkr_encryption_key_path: Path | None = Field(
        default=None, validation_alias="OBLISK_IBKR_ENCRYPTION_KEY"
    )
    ibkr_dh_param_path: Path | None = Field(default=None, validation_alias="OBLISK_IBKR_DH_PARAM")

    # Deployment locations.
    config_dir: Path | None = Field(default=None, validation_alias="OBLISK_CONFIG_DIR")
    data_dir: Path | None = Field(default=None, validation_alias="OBLISK_DATA_DIR")

    # Per-process logging overrides.
    log_level: LogLevelName | None = Field(default=None, validation_alias="OBLISK_LOG_LEVEL")
    log_format: LogFormatName | None = Field(default=None, validation_alias="OBLISK_LOG_FORMAT")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        """Whitespace-only counts as unset, so a stray space never becomes a credential."""
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("log_format", mode="before")
    @classmethod
    def _lower_format(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value

    @field_validator("ibkr_signature_key_path", "ibkr_encryption_key_path", "ibkr_dh_param_path")
    @classmethod
    def _key_file_exists(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        resolved = value.expanduser()
        if not resolved.is_file():
            raise ValueError(f"points at a file that does not exist: {resolved}")
        return resolved

    @field_validator("config_dir", "data_dir")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @field_validator("ibkr_cp_base_url")
    @classmethod
    def _base_url_has_no_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("must be an https:// URL, such as https://api.ibkr.com")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("must be a bare origin without a path, such as https://api.ibkr.com")
        return f"{parts.scheme}://{parts.netloc}"

    def resolved_config_dir(self) -> Path:
        """``OBLISK_CONFIG_DIR``, else ``config/`` of the source checkout."""
        if self.config_dir is not None:
            return self.config_dir
        root = repository_root()
        if root is None:
            raise EnvVarError(
                "OBLISK_CONFIG_DIR is unset and this is not a source checkout; point it at "
                "the directory that holds owner.yaml"
            )
        return root / "config"

    def require_data_dir(self) -> Path:
        """``OBLISK_DATA_DIR``: where runtime state lives. There is no default location."""
        if self.data_dir is None:
            raise EnvVarError(
                "OBLISK_DATA_DIR must be set: it is where runtime state such as the "
                "settings audit log is written"
            )
        return self.data_dir

    def config_overrides(self) -> dict[str, tuple[str, str]]:
        """Configuration keys this environment overrides: dotted key → (value, source)."""
        overrides: dict[str, tuple[str, str]] = {}
        if self.log_level is not None:
            overrides["logging.level"] = (self.log_level, "environment (OBLISK_LOG_LEVEL)")
        if self.log_format is not None:
            overrides["logging.format"] = (self.log_format, "environment (OBLISK_LOG_FORMAT)")
        return overrides


def variable_names() -> frozenset[str]:
    """Every environment variable :class:`Environment` reads."""
    names: set[str] = set()
    for field in Environment.model_fields.values():
        alias = field.validation_alias
        if not isinstance(alias, str):  # pragma: no cover - every field declares a plain alias
            raise TypeError(f"field without a plain environment alias: {field!r}")
        names.add(alias)
    return frozenset(names)


def load_environment(*, env_file: Path | None) -> Environment:
    """Read the environment (and ``env_file``, when given), failing loudly on a bad value.

    The error names each offending variable and what is wrong with it, never the value. A
    named env file that does not exist is an error, not an empty file.
    """
    if env_file is not None and not env_file.is_file():
        raise EnvVarError(f"env file not found: {env_file}")
    try:
        return Environment(_env_file=env_file)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc'])}: {_plain(error['msg'])}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise EnvVarError("invalid environment: " + "; ".join(problems)) from None


def _plain(message: str) -> str:
    return message.removeprefix("Value error, ").replace("Input should be", "must be")
