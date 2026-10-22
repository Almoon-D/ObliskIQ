"""What the gate knows about the instrument an order trades, and what is never traded.

* **The directory.** :class:`InstrumentDirectory` describes a contract by conid
  (:class:`~obliskiq.domain.instrument.TradedInstrument`); a contract it cannot describe is never
  traded. :class:`UniverseDirectory` describes every tradable candidate and option underlying the
  universe pins with its resolved description, eligible or not, and the option contracts the
  chain collector recorded on them, so an option on an ineligible ETP and its underlying can both
  be described; eligibility is the jurisdiction policy's to judge, never the directory's.
* **Prohibited categories** (lite's ``instruments.prohibited``): futures contracts, futures
  options (unless ``allow_futures_options``), and leveraged or inverse products, recognised by
  ``instruments.leveraged_inverse_markers`` in the resolved description (an option's
  underlying's too). A written option — an option sold beyond the long position — is the gate's
  no-short check (I2), reported as ``written_option``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from decimal import Decimal
from typing import Final, Protocol

from ..config.schema import ObliskConfig
from ..config.schema_universe import UniverseInstrument
from ..domain.enums import AssetClass
from ..domain.instrument import TradedInstrument, leveraged_inverse_marker

#: What one unit of a share, fund or bond costs per unit of its price.
UNIT_MULTIPLIER: Final = Decimal(1)


class InstrumentDirectory(Protocol):
    """Describes the contracts orders may name."""

    def describe(self, conid: int) -> TradedInstrument | None:
        """The contract ``conid``; ``None`` when it cannot be described."""
        ...


def traded_instrument(declared: UniverseInstrument) -> TradedInstrument | None:
    """``declared`` as an order trades it; ``None`` until it is pinned and its description
    resolved (an instrument nobody can describe is never traded)."""
    if declared.conid is None or declared.description is None:
        return None
    return TradedInstrument(
        conid=declared.conid,
        symbol=declared.symbol,
        listing_exchange=declared.listing_exchange,
        currency=declared.currency,
        asset_class=declared.asset_class,
        description=declared.description,
        multiplier=UNIT_MULTIPLIER,
        underlying_conid=None,
        expiry=None,
    )


class UniverseDirectory:
    """The universe's pinned instruments and the option contracts recorded on them."""

    def __init__(self, instruments: Iterable[TradedInstrument]) -> None:
        self._instruments = {instrument.conid: instrument for instrument in instruments}

    @classmethod
    def from_settings(
        cls, settings: ObliskConfig, *, options: Iterable[TradedInstrument] = ()
    ) -> UniverseDirectory:
        """The universe's describable tradable candidates and option underlyings, less those
        whose description reads as leveraged or inverse (rejected at ingestion), and
        ``options``. A data-only instrument is measured, never named by an order."""
        markers = settings.instruments.leveraged_inverse_markers
        described = [
            instrument
            for declared in settings.universe.instruments
            if (declared.tradable or declared.collects_chain)
            and (instrument := traded_instrument(declared)) is not None
            and leveraged_inverse_marker(instrument.description, markers) is None
        ]
        return cls([*described, *options])

    def describe(self, conid: int) -> TradedInstrument | None:
        return self._instruments.get(conid)

    def listing_exchanges(self) -> tuple[str, ...]:
        return tuple(sorted({i.listing_exchange for i in self._instruments.values()}))

    def instruments(self) -> tuple[TradedInstrument, ...]:
        """Every described instrument, by conid."""
        return tuple(self._instruments[conid] for conid in sorted(self._instruments))


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
            marker = leveraged_inverse_marker(
                described.description, config.leveraged_inverse_markers
            )
            if marker is not None:
                reasons.append(
                    f"{described.symbol} reads as a leveraged or inverse product "
                    f"(marker {marker!r} in {described.description!r})"
                )
    return reasons
