"""The owner's cockpit (``server``) and the settings it may change remotely (``remote_settings``).

``server`` is lite's block of the same name (lite's config.yaml:720-740, @b1ebd49), the keys
the cockpit consumes, with where it listens and how its Telegram bot polls. ``remote_settings``
says which settings Telegram ``/set`` and MCP ``update_setting`` may change and how
(MASTER_PLAN §5.17): a ``tighten`` key only in the direction that lowers risk, a ``bounded`` key
only inside its bounds; everything else, and loosening a risk limit, needs the command line on the
machine.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveFloat, PositiveInt, model_validator

from .base import FrozenModel

type SettingKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[A-Za-z0-9_]+)+$")]


class CockpitTelegramConfig(FrozenModel):
    """How the cockpit's Telegram bot reads the owner's messages."""

    poll_timeout_seconds: PositiveInt
    confirm_minutes: PositiveInt
    retry_seconds: PositiveFloat


class ServerConfig(FrozenModel):
    """The MCP cockpit: lite's response bounds, its address and its Telegram bot."""

    max_rows_per_response: PositiveInt
    stale_after_days: PositiveInt
    host: Annotated[str, Field(min_length=1)]
    port: Annotated[int, Field(ge=1, le=65535)]
    telegram: CockpitTelegramConfig


class TightenRule(FrozenModel):
    """A risk limit a remote change may only tighten: ``lower`` or ``higher`` is the safe way."""

    key: SettingKey
    tighter: Literal["lower", "higher"]


class BoundedRule(FrozenModel):
    """A budget or cash-flow setting a remote change may set anywhere inside ``[min, max]``."""

    key: SettingKey
    min: Decimal
    max: Decimal

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min > self.max:
            raise ValueError(f"{self.key}: min {self.min} is above max {self.max}")
        return self


class RemoteSettingsConfig(FrozenModel):
    """The settings Telegram and the MCP cockpit may change, and how."""

    tighten: tuple[TightenRule, ...]
    bounded: tuple[BoundedRule, ...]

    @model_validator(mode="after")
    def _one_rule_per_key(self) -> Self:
        keys = [rule.key for rule in self.tighten] + [rule.key for rule in self.bounded]
        repeated = sorted({key for key in keys if keys.count(key) > 1})
        if repeated:
            raise ValueError(f"one rule per setting: {', '.join(repeated)} repeated")
        return self

    def rules(self) -> tuple[TightenRule | BoundedRule, ...]:
        """Every rule, the risk limits first."""
        return (*self.tighten, *self.bounded)

    def rule(self, key: str) -> TightenRule | BoundedRule | None:
        """The rule for ``key``; ``None`` when remote channels may not change it."""
        for rule in self.rules():
            if rule.key == key:
                return rule
        return None
