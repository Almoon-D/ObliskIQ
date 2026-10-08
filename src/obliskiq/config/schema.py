"""The shape of the configuration.

Every model is frozen and refuses unknown keys, and no field carries a default value: the
values live in ``config/defaults/*.yaml`` and ``config/owner.yaml``, so a key missing from
both fails validation instead of taking a number nobody chose. A section joins this schema
with its first consumer.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict

_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_]*")


class FrozenModel(BaseModel):
    """Base for configuration models: immutable, and unknown keys are errors."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _currency_code(value: str) -> str:
    if not _CURRENCY_RE.fullmatch(value):
        raise ValueError("must be a three-letter ISO 4217 currency code in capitals, such as EUR")
    return value


def check_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            "must be a lower-case identifier (letters, digits, underscores), such as es"
        )
    return value


def _non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


def _file_name_in_data_dir(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.name
        or value.endswith("/")
        or "\\" in value
    ):
        raise ValueError(
            "must be a relative path inside the data directory, such as settings-audit.jsonl"
        )
    return value


type CurrencyCode = Annotated[str, AfterValidator(_currency_code)]
type Identifier = Annotated[str, AfterValidator(check_identifier)]
type NonEmptyText = Annotated[str, AfterValidator(_non_empty)]
type DataFileName = Annotated[str, AfterValidator(_file_name_in_data_dir)]


class AccountConfig(FrozenModel):
    """The account as the owner describes it."""

    base_currency: CurrencyCode


class PathsConfig(FrozenModel):
    """Where runtime files live, relative to ``OBLISK_DATA_DIR``."""

    settings_audit_log: DataFileName


class LoggingConfig(FrozenModel):
    """Log level and format. ``OBLISK_LOG_LEVEL``/``OBLISK_LOG_FORMAT`` override them."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    format: Literal["json", "text"]


class ObliskConfig(FrozenModel):
    """The effective configuration: expert defaults, then the owner's overrides."""

    account: AccountConfig
    jurisdiction: Identifier
    paths: PathsConfig
    logging: LoggingConfig
