"""The option surface per underlying (MASTER_PLAN §5.7): delta x tenor x implied vol, its
percentile, skew and term slope, liquidity and theta, as leaves of the underlying's instrument
entity in the measurement store (indicator ``option_surface``).

For each tenor in ``grid.option_surface.tenors_days`` the listed expiry nearest it is read, and
only one within ``smile.term_structure.max_target_deviation_days`` of it (a different tenor is
never published under this one's name; the window carries the actual days). Off that expiry's
hygiene-filtered smile (the one the zombie test and the gates read): the at-the-money vol, the vol
at each |delta| of ``grid.option_surface.deltas`` on each wing, the risk reversal at
``smile.skew_delta``, the at-the-money call's theta as a share of its value a day, and the
expiry's liquidity (the median spread, the open interest and the share of contracts the candidate
gates' liquidity test passes). Per underlying: the implied-vol percentile and the term slope, by
the grid's own families (one implementation).

Every leaf not ``ok`` says why: no chain collected, no expiry near the tenor, no smile surviving
hygiene, no wing reaching the delta, no rate to discount at. A chain older than
``grid.freshness_days.option_chain`` makes every leaf it feeds stale.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Final

from ..config.schema import ObliskConfig
from ..config.schema_universe import UniverseInstrument
from ..data.macro.catalog import MacroCatalog
from ..decision.gates import CandidateGates
from ..domain.market import ChainRow
from ..domain.measurement import EntityKind, Measurement
from ..quant.bsm import BsmInputs, bsm_greeks, bsm_price
from ..quant.smile import Smile
from ..store import measurements as measurements_repo
from ..store import repos
from ..store.db import Store
from .cells import Cell, inputs_hash, to_measurement
from .equity_grid import Freshness, resolve_conid
from .frames import UNREPORTED, ChainSmile, MacroReader, worst_status
from .indicators import GridParams, iv_percentile, term_structure_cells
from .vocabulary import Input, Reason, Units

SURFACE: Final = "option_surface"
#: A |delta| as the whole number a leaf name carries (0.25 -> 25).
PERCENT: Final = 100
#: The dividend-yield basis when none was collected: zero, and labelled so (as the solve does).
Q_NONE: Final = "none collected (priced at zero)"
#: The grid leaves the surface carries under its own names.
TERM_SLOPE_LEAF: Final = "implied_vol_term_slope"


def tenor_leaf(tenor: int, what: str) -> str:
    """``t95_iv_atm``: the leaf of one tenor."""
    return f"t{tenor}_{what}"


def wing_leaf(tenor: int, delta: float, *, calls: bool) -> str:
    """``t95_iv_c25``: the vol at a |delta| on one wing of one tenor."""
    return tenor_leaf(tenor, f"iv_{'c' if calls else 'p'}{round(delta * PERCENT)}")


def surface_leaves(settings: ObliskConfig) -> tuple[str, ...]:
    """Every leaf the surface writes for one underlying, in order."""
    surface = settings.grid.option_surface
    leaves = ["iv_percentile", "term_slope"]
    for tenor in surface.tenors_days:
        leaves.append(tenor_leaf(tenor, "iv_atm"))
        for delta in surface.deltas:
            leaves += [wing_leaf(tenor, delta, calls=False), wing_leaf(tenor, delta, calls=True)]
        leaves += [
            tenor_leaf(tenor, what)
            for what in ("skew", "theta_share", "spread_median", "open_interest", "liquid_share")
        ]
    return tuple(leaves)


def underlying_spot(store: Store, conid: int, session: date) -> float | None:
    """Spot at a chain's own session, never a later one: the snapshot's last, else the session's
    close (as the session's implied-vol solve reads it)."""
    snapshot = repos.snapshots(store, conid, as_of=session, limit=1)
    if snapshot and snapshot[-1].last is not None and snapshot[-1].last > 0:
        return snapshot[-1].last
    bars = repos.bar_series(store, conid, as_of=session, limit=1)
    return bars[-1].close if bars and bars[-1].close > 0 else None


def _digest(rows: object) -> str:
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class UnderlyingChain:
    """One underlying's chain, read by the grid's option families (``indicators.ChainInputs``)."""

    store: Store
    params: GridParams
    as_of: date
    conid: int | None
    name: str
    chain: ChainSmile

    @staticmethod
    def options_unavailable() -> str | None:
        """Only an underlying whose chain is collected is on the surface."""
        return None


class OptionSurfaceBuilder:
    """Writes the surface of every option underlying the universe collects a chain for."""

    def __init__(self, store: Store, settings: ObliskConfig) -> None:
        self._store = store
        self._settings = settings
        self._params = GridParams.from_settings(settings)
        self._surface = settings.grid.option_surface
        self._terms = settings.smile.term_structure
        self._skew_delta = settings.smile.skew_delta
        self._gates = CandidateGates(settings)
        self._bsm = BsmInputs.from_config(settings)
        self._catalog = MacroCatalog.from_settings(settings)
        self._rates = settings.numerics.implied_vol.solve.risk_free_series
        self._freshness = settings.grid.freshness_days.option_chain
        self._settings_digest = inputs_hash(
            {
                "params": repr(self._params),
                "surface": repr(self._surface),
                "terms": repr(self._terms),
                "liquidity": repr(settings.liquidity),
            }
        )

    def build(self, *, as_of: date, computed_at: datetime) -> list[Measurement]:
        rows: list[Measurement] = []
        for declared in self._settings.universe.instruments:
            if not declared.collects_chain:
                continue
            conid = resolve_conid(self._store, declared, as_of=as_of)
            if conid is None:
                continue
            rows += self.for_underlying(declared, conid, as_of=as_of, computed_at=computed_at)
        return rows

    def write(self, rows: Sequence[Measurement]) -> int:
        return measurements_repo.upsert_measurements(self._store, rows)

    def for_underlying(
        self, declared: UniverseInstrument, conid: int, *, as_of: date, computed_at: datetime
    ) -> list[Measurement]:
        store = self._store
        session = repos.latest_chain_date(store, as_of=as_of, underlying_conid=conid)
        spot = None if session is None else underlying_spot(store, conid, session)
        chain = ChainSmile(
            store=store,
            conid=conid,
            as_of=as_of,
            spot=spot,
            filters=self._params.smile_filters,
        )
        inputs = UnderlyingChain(store, self._params, as_of, conid, declared.symbol, chain)
        cells = self._underlying_cells(inputs)
        rate = None if session is None else self._rate(declared.currency, session)
        dividend = None if session is None else self._dividend(conid, session)
        expiries = chain.expiries()
        for tenor in self._surface.tenors_days:
            cells += self._tenor_cells(tenor, chain, expiries, spot, rate, dividend, as_of)
        freshness = Freshness(Input.OPTION_CHAIN, session, as_of, self._freshness)
        if freshness.is_stale:
            cells = [cell.stale(freshness.describe()) for cell in cells]
        fingerprint = {
            "settings": self._settings_digest,
            "as_of": as_of,
            "chain": _digest(chain.rows()),
            "snapshots": _digest(repos.snapshots(store, conid, as_of=as_of, limit=1)),
            "spot": spot,
            "rate": rate,
            "dividend": dividend,
        }
        return [
            to_measurement(
                cell,
                entity_kind=EntityKind.INSTRUMENT,
                entity_id=str(conid),
                as_of=as_of,
                source=f"chain:{conid}",
                fingerprint=fingerprint,
                computed_at=computed_at,
            )
            for cell in cells
        ]

    # ── per underlying ──

    @staticmethod
    def _underlying_cells(inputs: UnderlyingChain) -> list[Cell]:
        percentile = [replace(cell, family=SURFACE) for cell in iv_percentile(inputs)]
        slope = [
            replace(cell, leaf="term_slope", family=SURFACE)
            for cell in term_structure_cells(inputs)
            if cell.leaf == TERM_SLOPE_LEAF
        ]
        return percentile + slope

    # ── per tenor ──

    def _tenor_cells(
        self,
        tenor: int,
        chain: ChainSmile,
        expiries: list[date],
        spot: float | None,
        rate: float | None,
        dividend: tuple[float, str] | None,
        as_of: date,
    ) -> list[Cell]:
        names = self._tenor_names(tenor)
        expiry = self._nearest(expiries, as_of, tenor)
        if expiry is None:
            listed = ", ".join(f"{(e - as_of).days}d" for e in expiries) or "none"
            reason = (
                f"no collected expiry lies within {self._terms.max_target_deviation_days}d of "
                f"{tenor}d (listed: {listed})"
                if chain.rows()
                else f"no option chain is collected for this underlying by {as_of}"
            )
            code = Reason.NO_TENOR if chain.rows() else Reason.NO_CHAIN
            return [Cell.absent(name, SURFACE, reason, code=code) for name in names]
        dte = (expiry - as_of).days
        window = f"{dte}d (target {tenor}d) on the {chain.session} chain"
        rows = [row for row in chain.rows() if row.expiry == expiry]
        smile = chain.at_expiry(expiry)
        cells = self._vol_cells(tenor, smile, window, spot, rate, dividend, dte)
        return cells + self._liquidity_cells(tenor, rows, window)

    def _tenor_names(self, tenor: int) -> list[str]:
        leaves = surface_leaves(self._settings)
        prefix = tenor_leaf(tenor, "")
        return [leaf for leaf in leaves if leaf.startswith(prefix)]

    def _nearest(self, expiries: list[date], as_of: date, tenor: int) -> date | None:
        if not expiries:
            return None
        nearest = min(expiries, key=lambda expiry: abs((expiry - as_of).days - tenor))
        deviation = abs((nearest - as_of).days - tenor)
        return nearest if deviation <= self._terms.max_target_deviation_days else None

    def _vol_cells(
        self,
        tenor: int,
        smile: Smile | None,
        window: str,
        spot: float | None,
        rate: float | None,
        dividend: tuple[float, str] | None,
        dte: int,
    ) -> list[Cell]:
        vol_names = [tenor_leaf(tenor, "iv_atm")]
        for delta in self._surface.deltas:
            vol_names += [
                wing_leaf(tenor, delta, calls=False),
                wing_leaf(tenor, delta, calls=True),
            ]
        vol_names += [tenor_leaf(tenor, "skew"), tenor_leaf(tenor, "theta_share")]
        if spot is None or smile is None:
            reason = (
                "no underlying price at the chain's session, so no smile can be placed"
                if spot is None
                else "no strike of this expiry survived smile hygiene (spread, depth, delta band, "
                "convexity)"
            )
            code = Reason.NO_SPOT if spot is None else Reason.NO_SMILE
            return [
                Cell.absent(name, SURFACE, reason, code=code, window=window) for name in vol_names
            ]
        status = worst_status(smile.market_data_statuses)
        hygiene = smile.hygiene.summary()
        atm = smile.atm()
        cells = [
            Cell.measured(
                vol_names[0],
                SURFACE,
                atm.iv,
                units=Units.DECIMAL,
                window=window,
                n_obs=len(smile),
                reason=f"{atm.provenance()}; {hygiene}",
                market_data_status=status,
            )
        ]
        for delta in self._surface.deltas:
            for calls in (False, True):
                name = wing_leaf(tenor, delta, calls=calls)
                quote = smile.iv_at_delta(delta, calls=calls)
                if quote is None:
                    cells.append(
                        Cell.absent(
                            name,
                            SURFACE,
                            f"the {'call' if calls else 'put'} wing does not reach |delta| "
                            f"{delta:g}: {smile.delta_reach().describe()}; {hygiene}",
                            code=Reason.NO_SMILE,
                            window=window,
                        )
                    )
                    continue
                cells.append(
                    Cell.measured(
                        name,
                        SURFACE,
                        quote.iv,
                        units=Units.DECIMAL,
                        window=window,
                        n_obs=len(smile),
                        reason=quote.provenance(),
                        market_data_status=status,
                    )
                )
        cells.append(self._skew(tenor, smile, window))
        cells.append(self._theta(tenor, smile, window, spot, rate, dividend, dte))
        return cells

    def _skew(self, tenor: int, smile: Smile, window: str) -> Cell:
        name = tenor_leaf(tenor, "skew")
        quote = smile.risk_reversal(self._skew_delta)
        if quote is None:
            return Cell.absent(
                name,
                SURFACE,
                f"the wings do not both reach |delta| {self._skew_delta:g}: "
                f"{smile.delta_reach().describe()}",
                code=Reason.NO_SMILE,
                window=window,
            )
        return Cell.measured(
            name,
            SURFACE,
            quote.value,
            units=Units.DECIMAL,
            window=window,
            n_obs=quote.n_points,
            reason=quote.provenance(),
            market_data_status=worst_status(smile.market_data_statuses),
        )

    def _theta(
        self,
        tenor: int,
        smile: Smile,
        window: str,
        spot: float,
        rate: float | None,
        dividend: tuple[float, str] | None,
        dte: int,
    ) -> Cell:
        """The at-the-money call's daily decay as a share of its value: comparable across
        underlyings, where a decay in money is not."""
        name = tenor_leaf(tenor, "theta_share")
        if rate is None:
            return Cell.absent(
                name,
                SURFACE,
                "no risk-free rate for the underlying's currency was released by the chain's "
                "session (numerics.implied_vol.solve.risk_free_series)",
                code=Reason.NO_RATE,
                window=window,
            )
        q, basis = dividend if dividend is not None else (0.0, Q_NONE)
        sigma = smile.atm().iv
        value = float(bsm_price(spot, spot, dte, sigma, rate, q, True, self._bsm))
        theta = float(bsm_greeks(spot, spot, dte, sigma, rate, q, True, self._bsm).theta)
        if value <= 0:
            return Cell.absent(
                name,
                SURFACE,
                "the at-the-money call prices at zero, so no share of it is measured",
                code=Reason.NO_SMILE,
                window=window,
            )
        return Cell.measured(
            name,
            SURFACE,
            theta / value,
            units=Units.RATIO,
            window=window,
            reason=f"BSM theta a day over value at the money, sigma {sigma:.4f}, rate {rate:.4f}, "
            f"dividend yield {basis}",
            market_data_status=worst_status(smile.market_data_statuses),
        )

    def _liquidity_cells(self, tenor: int, rows: list[ChainRow], window: str) -> list[Cell]:
        spread_name = tenor_leaf(tenor, "spread_median")
        oi_name = tenor_leaf(tenor, "open_interest")
        share_name = tenor_leaf(tenor, "liquid_share")
        status = worst_status(
            Counter(
                row.market_data_status.value if row.market_data_status else UNREPORTED
                for row in rows
            )
        )
        spreads = [
            spread for row in rows if (spread := row.spread_pct_of_mid) is not None and spread >= 0
        ]
        cells = [
            Cell.measured(
                spread_name,
                SURFACE,
                statistics.median(spreads),
                units=Units.DECIMAL,
                window=window,
                n_obs=len(spreads),
                reason=f"median bid-ask spread over mid of {len(spreads)} of {len(rows)} contracts",
                market_data_status=status,
            )
            if spreads
            else Cell.absent(
                spread_name,
                SURFACE,
                f"none of the expiry's {len(rows)} contracts carries a two-sided, uncrossed book",
                code=Reason.NO_CHAIN,
                window=window,
            )
        ]
        interest = [row.open_interest for row in rows if row.open_interest is not None]
        cells.append(
            Cell.measured(
                oi_name,
                SURFACE,
                float(sum(interest)),
                units=Units.INDEX,
                window=window,
                n_obs=len(interest),
                reason=f"open interest summed over {len(interest)} of {len(rows)} contracts",
                market_data_status=status,
            )
            if interest
            else Cell.absent(
                oi_name,
                SURFACE,
                f"the route reported no open interest on the expiry's {len(rows)} contracts",
                code=Reason.NO_CHAIN,
                window=window,
            )
        )
        tested = [
            self._gates.liquidity(
                spread_pct_of_mid=row.spread_pct_of_mid,
                open_interest=row.open_interest,
                volume=row.volume,
            )
            for row in rows
        ]
        liquid = sum(1 for result in tested if result.fully_tested)
        cells.append(
            Cell.measured(
                share_name,
                SURFACE,
                liquid / len(rows),
                units=Units.RATIO,
                window=window,
                n_obs=len(rows),
                reason=f"{liquid} of {len(rows)} contracts clear every liquidity gate, each tested "
                "(open interest, spread or its depth waiver, session volume)",
                market_data_status=status,
            )
            if rows
            else Cell.absent(
                share_name, SURFACE, "the expiry lists no contract", code=Reason.NO_CHAIN
            )
        )
        return cells

    # ── inputs at the chain's session ──

    def _rate(self, currency: str, session: date) -> float | None:
        series_id = self._rates.get(currency)
        if series_id is None:
            return None
        return MacroReader(self._store, self._catalog, session).value_at(series_id, session)

    def _dividend(self, conid: int, session: date) -> tuple[float, str] | None:
        snapshot = repos.snapshots(self._store, conid, as_of=session, limit=1)
        if snapshot and snapshot[-1].dividend_yield is not None:
            return snapshot[-1].dividend_yield, f"collected ({snapshot[-1].dividend_yield_basis})"
        return None


# ── the view ──


def surface_view(store: Store, *, as_of: date) -> dict[int, dict[str, Measurement]]:
    """The newest surface built on or before ``as_of``, by underlying conid then leaf: a query
    over the measurements, nothing stored twice."""
    session = measurements_repo.measured_session(store, EntityKind.INSTRUMENT, as_of=as_of)
    if session is None:
        return {}
    view: dict[int, dict[str, Measurement]] = {}
    for row in measurements_repo.session_measurements(
        store, EntityKind.INSTRUMENT, as_of=session, indicator=SURFACE
    ):
        view.setdefault(int(row.entity_id), {})[row.leaf] = row
    return view
