"""The shape of the configuration.

Every model is frozen and refuses unknown keys, and no field carries a default value: the
values live in ``config/defaults/*.yaml`` and ``config/owner.yaml``, so a key missing from
both fails validation instead of taking a number nobody chose. A section joins this schema
with its first consumer.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, PositiveInt, model_validator

from ..domain.base import CurrencyCode
from ..domain.calendar import Mic
from .base import FrozenModel
from .schema_calendars import (
    CalendarsConfig,
    IbkrExchangeCode,
    IbkrExchangeConfig,
    LunchBreakConfig,
    SeasonalCloseConfig,
    SyntheticCalendarConfig,
    VenueConfig,
)
from .schema_costs import CostsConfig
from .schema_numerics import LiquidityConfig, NumericsConfig, SmileConfig

_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_]*")


def check_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            "must be a lower-case identifier (letters, digits, underscores), such as es"
        )
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


type Identifier = Annotated[str, AfterValidator(check_identifier)]
type DataFileName = Annotated[str, AfterValidator(_file_name_in_data_dir)]


class AccountConfig(FrozenModel):
    """The account as the owner describes it."""

    base_currency: CurrencyCode


class PathsConfig(FrozenModel):
    """Where runtime files live, relative to ``OBLISK_DATA_DIR``."""

    settings_audit_log: DataFileName
    database: DataFileName
    manual_incidents: DataFileName


class StoreConfig(FrozenModel):
    """SQLite settings. Write-ahead logging and foreign keys are fixed in code, not offered."""

    busy_timeout_ms: PositiveInt
    synchronous: Literal["NORMAL", "FULL", "EXTRA"]


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
    store: StoreConfig
    numerics: NumericsConfig
    smile: SmileConfig
    liquidity: LiquidityConfig
    costs: CostsConfig
    calendars: CalendarsConfig
    venues: dict[Mic, VenueConfig]
    synthetic_calendars: dict[Mic, SyntheticCalendarConfig]
    lunch_breaks: dict[Mic, LunchBreakConfig]
    seasonal_closes: dict[Mic, SeasonalCloseConfig]
    ibkr_exchanges: dict[IbkrExchangeCode, IbkrExchangeConfig]

    @model_validator(mode="after")
    def _calendar_references_resolve(self) -> Self:
        """Every venue named by a side section or an IBKR code must be declared in ``venues``."""
        known = set(self.venues)
        problems: list[str] = []
        sections: dict[str, set[str]] = {
            "synthetic_calendars": set(self.synthetic_calendars),
            "lunch_breaks": set(self.lunch_breaks),
            "seasonal_closes": set(self.seasonal_closes),
            "calendars.venue_order_offsets": set(self.calendars.venue_order_offsets),
        }
        for section, mics in sections.items():
            problems += [
                f"{section}.{mic}: not a venue declared in venues" for mic in sorted(mics - known)
            ]
        problems += [
            f"ibkr_exchanges.{code}.venue: {entry.venue} is not a venue declared in venues"
            for code, entry in sorted(self.ibkr_exchanges.items())
            if entry.venue not in known
        ]
        problems += [
            f"seasonal_closes.{mic}: a synthetic calendar has no library close to replace"
            for mic in sorted(set(self.seasonal_closes) & set(self.synthetic_calendars))
        ]
        if problems:
            raise ValueError("; ".join(problems))
        return self
