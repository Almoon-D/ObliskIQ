"""One unit of a currency in the base currency, as known on a day (lite's ``BaseCurrencyRates``).

**The direction is decided here, once.** The board's FX series are quoted as units of the local
currency per unit of the base (``config/universe.yaml``, each region's ``fx_vs_base``, its units
declared as ``"<local> per <base>"``), so an amount in the local currency becomes base by
**dividing** by the rate; :meth:`BaseRates.to_base` returns that multiplier. Inverting it at each
call site is how a conversion silently runs backwards.

Every lookup can fail, and failure is ``None`` with the reason, never 1.0: a caller that wanted a
rate and did not get one leaves its comparison untested, because an assumed parity is invisible
and admits what an affordability gate exists to refuse. Rates are the newest released on or
before the day asked (``MacroReader``), so a read for a past day never converts at a later rate.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ...config.schema import ObliskConfig
from ...data.macro.catalog import MacroCatalog
from ...grid.frames import MacroReader, market_spec
from ...store.db import Store

ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class RateReading:
    """A multiplier into the base currency, or why there is none (``value`` ``None``)."""

    currency: str
    value: Decimal | None
    reason: str


class BaseRates:
    """Converts amounts into the base currency on one day, each currency read once."""

    def __init__(self, store: Store, settings: ObliskConfig, *, as_of: date) -> None:
        self._base = settings.account.base_currency
        self._as_of = as_of
        self._reader = MacroReader(store, MacroCatalog.from_settings(settings), as_of)
        universe = settings.universe
        self._series: dict[str, str] = {}
        for region, entry in sorted(universe.macro_board.regions.items()):
            spec = market_spec(universe, region)
            if spec.fx is not None and entry.currency is not None:
                self._series.setdefault(spec.currency, spec.fx)
        self._cache: dict[str, RateReading] = {}

    @property
    def base_currency(self) -> str:
        return self._base

    def reading(self, currency: str) -> RateReading:
        if currency not in self._cache:
            self._cache[currency] = self._read(currency)
        return self._cache[currency]

    def to_base(self, currency: str) -> Decimal | None:
        """The multiplier taking an amount in ``currency`` to the base; ``None`` when unknown."""
        return self.reading(currency).value

    def per_base_on(self, currency: str, day: date, *, max_age_days: int) -> RateReading:
        """Units of ``currency`` per unit of the base on ``day`` (the divisor into the base): the
        rate observed on or before ``day``, released by the reader's date and at most
        ``max_age_days`` older than ``day``. A past day is converted at its own rate."""
        if currency == self._base:
            return RateReading(currency, ONE, f"{currency} is the base currency")
        series, refused = self._series_of(currency)
        if series is None:
            return RateReading(currency, None, refused)
        found = self._reader.series(series)
        observations = () if found is None else found.observations
        latest = next((o for o in reversed(observations) if o.reference_date <= day), None)
        if latest is None or latest.value <= 0:
            return RateReading(currency, None, f"{series} has no rate on or before {day}")
        if (day - latest.reference_date).days > max_age_days:
            return RateReading(
                currency,
                None,
                f"{series}'s rate of {latest.reference_date} is more than {max_age_days} days "
                f"older than {day}",
            )
        return RateReading(
            currency,
            Decimal(repr(latest.value)),
            f"{latest.value:g} {currency} per {self._base} ({series}, {latest.reference_date})",
        )

    def _series_of(self, currency: str) -> tuple[str | None, str]:
        """The board's FX series for ``currency`` against the base, or why there is none."""
        series = self._series.get(currency)
        if series is None:
            return None, (
                f"no macro-board region in {currency} declares an FX series against {self._base}"
            )
        expected = f"{currency} per {self._base}"
        units = self._reader.units(series)
        if units != expected:
            return None, (
                f"{series} is declared in {units!r}, not {expected!r}; read the other way up it "
                "would invert every conversion"
            )
        return series, ""

    def _read(self, currency: str) -> RateReading:
        if currency == self._base:
            return RateReading(currency, ONE, f"{currency} is the base currency")
        series, refused = self._series_of(currency)
        if series is None:
            return RateReading(currency, None, refused)
        expected = f"{currency} per {self._base}"
        value = self._reader.value_at(series, self._as_of)
        if value is None or value <= 0:
            return RateReading(currency, None, self._reader.absence(series))
        return RateReading(
            currency,
            ONE / Decimal(repr(value)),
            f"1 / {value:g} {expected} ({series}, known on {self._as_of})",
        )
