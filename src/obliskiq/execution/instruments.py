"""What the gate knows about the instrument an order trades, and what is never traded.

* **The directory.** :class:`InstrumentDirectory` describes a contract by conid
  (:class:`~obliskiq.domain.instrument.TradedInstrument`); a contract it cannot describe is never
  traded. :class:`ConfiguredDirectory` describes the skeleton allowlist's instruments, the only
  ones this build can trade (Session 8 replaces it with the universe).
* **Prohibited categories** (lite's ``instruments.prohibited``): futures contracts, futures
  options (unless ``allow_futures_options``), and leveraged or inverse products, recognised by
  ``instruments.leveraged_inverse_markers`` in the resolved description (an option's
  underlying's too). A written option — an option sold beyond the long position — is the gate's
  no-short check (I2), reported as ``written_option``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from typing import Protocol

from ..config.schema import ObliskConfig
from ..domain.enums import AssetClass
from ..domain.instrument import TradedInstrument


class InstrumentDirectory(Protocol):
    """Describes the contracts orders may name."""

    def describe(self, conid: int) -> TradedInstrument | None:
        """The contract ``conid``; ``None`` when it cannot be described."""
        ...


class ConfiguredDirectory:
    """The instruments the configuration describes: the skeleton allowlist."""

    def __init__(self, instruments: Iterable[TradedInstrument]) -> None:
        self._instruments = {instrument.conid: instrument for instrument in instruments}

    @classmethod
    def from_settings(cls, settings: ObliskConfig) -> ConfiguredDirectory:
        return cls(settings.eligibility.skeleton_allowlist)

    def describe(self, conid: int) -> TradedInstrument | None:
        return self._instruments.get(conid)

    def listing_exchanges(self) -> tuple[str, ...]:
        return tuple(sorted({i.listing_exchange for i in self._instruments.values()}))

    def instruments(self) -> tuple[TradedInstrument, ...]:
        """Every described instrument, by conid."""
        return tuple(self._instruments[conid] for conid in sorted(self._instruments))


def _marker(description: str, markers: Iterable[str]) -> str | None:
    folded = description.casefold()
    return next((marker for marker in markers if marker.casefold() in folded), None)


def prohibited_reasons(
    instrument: TradedInstrument, underlying: TradedInstrument | None, settings: ObliskConfig
) -> list[str]:
    """Why ``instrument`` is a prohibited product (empty when it is not)."""
    config = settings.instruments
    prohibited = set(config.prohibited)
    reasons: list[str] = []
    if "futures_contracts" in prohibited and instrument.asset_class is AssetClass.FUTURE:
        reasons.append(f"{instrument.symbol} is a futures contract")
    if (
        "futures_options" in prohibited
        and not config.allow_futures_options
        and instrument.asset_class is AssetClass.OPTION
        and underlying is not None
        and underlying.asset_class is AssetClass.FUTURE
    ):
        reasons.append(f"{instrument.symbol} is an option on a futures contract")
    if prohibited & {"leveraged_etps", "inverse_etps"}:
        for described in (instrument, underlying):
            if described is None:
                continue
            marker = _marker(described.description, config.leveraged_inverse_markers)
            if marker is not None:
                reasons.append(
                    f"{described.symbol} reads as a leveraged or inverse product "
                    f"(marker {marker!r} in {described.description!r})"
                )
    return reasons
