"""The option candidates of a session: every contract of each underlying's newest chain on or
before the day, with what the candidate gates read about it.

One assembly for every caller of :meth:`~obliskiq.decision.gates.CandidateGates.candidate` (the
census, the sleeve's shadow log, the decision layer), so the gates read the same inputs wherever
they run: the expiry's hygiene-filtered smile at the chain's own spot, the underlying's
implied-vol percentile from the option surface built for the session, the session the option is
sold by, the rate into the base currency, the pool and the sleeve's open positions. An input that
cannot be read is passed as absent with why, and its gate is untested. A chain, or a surface,
older than ``grid.freshness_days.option_chain`` is no reading of the session: the chain is
skipped with why, the surface's percentile left untested.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from ...calendars.service import Calendars
from ...config.schema import ObliskConfig
from ...config.schema_universe import UniverseInstrument
from ...decision.gates import Affordability, OptionCandidate
from ...domain.market import ChainRow
from ...domain.measurement import Measurement, MeasurementStatus
from ...grid.frames import ChainSmile
from ...grid.option_surface import surface_session, surface_view
from ...grid.option_surface import underlying_spot as spot_at
from ...quant.smile import SmileFilters
from ...store import repos
from ...store.db import Store
from ..fx.rates import BaseRates
from .dates import SleeveDates

#: The surface leaf the IV-percentile gate reads.
IV_PERCENTILE_LEAF = "iv_percentile"


@dataclass(frozen=True, slots=True)
class Pool:
    """The sleeve's pool and open positions as a caller knows them, or why it does not."""

    affordability: Affordability | None
    affordability_gap: str | None
    open_positions: int | None
    open_positions_gap: str | None


@dataclass(frozen=True, slots=True)
class Skipped:
    """A chain the session could not turn into candidates, and why."""

    underlying_conid: int
    reason: str


class CandidateBook:
    """Assembles a session's option candidates."""

    def __init__(
        self, store: Store, settings: ObliskConfig, calendars: Calendars | None = None
    ) -> None:
        self._store = store
        self._settings = settings
        self._filters = SmileFilters.from_config(settings)
        self._dates = None if calendars is None else SleeveDates(settings, calendars)
        self._freshness = settings.grid.freshness_days.option_chain
        self.skipped: list[Skipped] = []
        self._close_cache: dict[tuple[str, date, int | None], tuple[date | None, str | None]] = {}

    def candidates(
        self, *, as_of: date, pool: Pool, catalyst_date: date | None = None
    ) -> Iterator[OptionCandidate]:
        """Every contract of every underlying's newest chain on or before ``as_of``."""
        self.skipped = []
        store = self._store
        rates = BaseRates(store, self._settings, as_of=as_of)
        surface, surface_gap = self._surface(as_of)
        for conid, session in repos.latest_chain_sessions(store, as_of=as_of):
            declared = self._settings.universe.pinned(conid)
            if declared is None:
                self.skipped.append(
                    Skipped(conid, f"the chain of {conid} is of no instrument the universe pins")
                )
                continue
            age = (as_of - session).days
            if age > self._freshness:
                self.skipped.append(
                    Skipped(
                        conid,
                        f"the newest chain of {declared.symbol} is from {session}, {age} days old, "
                        f"beyond the {self._freshness}-day grid.freshness_days.option_chain "
                        "window: a stale quote is no candidate",
                    )
                )
                continue
            spot = spot_at(store, conid, session)
            chain = ChainSmile(store, conid, as_of, spot, self._filters)
            rate = rates.reading(declared.currency)
            percentile, percentile_gap = self._percentile(
                surface.get(conid, {}), as_of, surface_gap
            )
            for row in chain.rows():
                smile = None if spot is None else chain.at_expiry(row.expiry)
                close_by, close_gap = self._close_by(declared, row, conid)
                yield OptionCandidate(
                    row=row,
                    underlying=declared,
                    as_of=as_of,
                    currency=declared.currency,
                    smile=smile,
                    smile_gap=(
                        f"no price of {declared.symbol} at the chain's session {session}, so no "
                        "smile can be placed"
                        if spot is None
                        else None
                    ),
                    iv_percentile=percentile,
                    iv_percentile_gap=percentile_gap,
                    close_by=close_by,
                    close_by_gap=close_gap,
                    affordability=pool.affordability,
                    affordability_gap=pool.affordability_gap,
                    fx_to_base=rate.value,
                    fx_gap=None if rate.value is not None else rate.reason,
                    open_positions=pool.open_positions,
                    open_positions_gap=pool.open_positions_gap,
                    catalyst_date=catalyst_date,
                )

    def _surface(self, as_of: date) -> tuple[dict[int, dict[str, Measurement]], str | None]:
        """The newest surface on or before ``as_of`` while it is fresh; an older one is no
        reading of this session's implied vol."""
        session = surface_session(self._store, as_of=as_of)
        if session is None:
            return {}, None
        age = (as_of - session).days
        if age > self._freshness:
            return {}, (
                f"the newest option surface is from {session}, {age} days old, beyond the "
                f"{self._freshness}-day grid.freshness_days.option_chain window"
            )
        return surface_view(self._store, as_of=as_of), None

    @staticmethod
    def _percentile(
        leaves: dict[str, Measurement], as_of: date, surface_gap: str | None
    ) -> tuple[float | None, str | None]:
        found = leaves.get(IV_PERCENTILE_LEAF)
        if found is None:
            return None, surface_gap or (
                f"the option surface was not built for this underlying by {as_of}"
            )
        if found.status is not MeasurementStatus.OK or found.value is None:
            return None, (
                f"the surface's implied-vol percentile is {found.status.value}: {found.reason}"
            )
        return found.value, None

    def _close_by(
        self, declared: UniverseInstrument, row: ChainRow, conid: int
    ) -> tuple[date | None, str | None]:
        if self._dates is None:
            return None, "no trading calendars were given, so the sell-by session is not computed"
        code = row.option_exchange or declared.option_exchange or declared.listing_exchange
        key = (code, row.expiry, row.option_conid)
        if key not in self._close_cache:
            found = self._dates.close_by(
                option_exchange=code,
                underlying_exchange=declared.listing_exchange,
                expiry=row.expiry,
                option_conid=row.option_conid,
                underlying_conid=conid,
            )
            self._close_cache[key] = (
                found.session,
                None if found.session is not None else found.reason,
            )
        return self._close_cache[key]
