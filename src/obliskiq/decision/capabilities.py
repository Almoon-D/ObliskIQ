"""What the broker can do through the Web API, per asset class and venue (MASTER_PLAN §5.3, §5.12).

The readings live in ``config/defaults/capabilities.yaml`` (``broker_capabilities``), each with its
route, date and evidence; docs/knowledge/ibkr-capabilities.md keeps them in prose. A vehicle may be
proposed only where IBKR's contract search found it, a snapshot quoted it and a preview accepted
an order on it, each read on the Client Portal route: :meth:`BrokerCapabilities.assess` says
whether all three hold and, when they do not, which are missing and why. An accepted preview also
shows the account holds the venue's trading permission.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from ..config.schema import ObliskConfig
from ..config.schema_vehicles import CapabilityCheck, CapabilityReading
from ..domain.enums import AssetClass

#: What a vehicle needs the Web API to do on its venue before it is proposed.
REQUIRED_CHECKS: Final = ("search", "quote", "order")


@dataclass(frozen=True, slots=True)
class CapabilityVerdict:
    """Whether the Web API's search, quote and order are verified for an asset class on a venue."""

    asset_class: AssetClass
    venue: str
    verified: bool
    missing: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class PermissionReading:
    """A venue's trading permission: its name, and whether an accepted preview showed it held."""

    venue: str
    permission: str
    held: bool
    evidence: str


def _readings(check: CapabilityCheck) -> Mapping[str, CapabilityReading]:
    return {"search": check.search, "quote": check.quote, "order": check.order}


class BrokerCapabilities:
    """The configured capability readings, and the verdicts the vehicle selector asks for."""

    def __init__(self, settings: ObliskConfig) -> None:
        self._settings = settings
        self._config = settings.broker_capabilities

    @property
    def checks(self) -> tuple[CapabilityCheck, ...]:
        return tuple(self._config.checks)

    def assess(self, asset_class: AssetClass, venue: str) -> CapabilityVerdict:
        check = self._config.check(asset_class, venue)
        if check is None:
            return CapabilityVerdict(
                asset_class,
                venue,
                False,
                REQUIRED_CHECKS,
                f"nothing is known of {asset_class.value} on {venue} through the Web API: no "
                "search, quote or order has been read there",
            )
        readings = _readings(check)
        missing = tuple(name for name in REQUIRED_CHECKS if not readings[name].verified)
        if not missing:
            dates = ", ".join(f"{name} {readings[name].observed_on}" for name in REQUIRED_CHECKS)
            return CapabilityVerdict(
                asset_class, venue, True, (), f"{asset_class.value} on {venue} verified ({dates})"
            )
        detail = "; ".join(
            f"{name} {readings[name].status}: {readings[name].evidence}" for name in missing
        )
        return CapabilityVerdict(
            asset_class,
            venue,
            False,
            missing,
            f"{asset_class.value} on {venue} is not verified through the Web API ({detail})",
        )

    def permissions(self) -> tuple[PermissionReading, ...]:
        """Every configured venue's trading permission, held where an order was verified there."""
        out: list[PermissionReading] = []
        for mic, venue in sorted(self._settings.venues.items()):
            shown = [
                check
                for check in self._config.checks
                if check.venue == mic and check.order.verified
            ]
            evidence = (
                "; ".join(
                    f"a {check.asset_class.value} preview accepted {check.order.observed_on}"
                    for check in shown
                )
                or "no accepted preview yet"
            )
            out.append(PermissionReading(mic, venue.trading_permission, bool(shown), evidence))
        return tuple(out)
