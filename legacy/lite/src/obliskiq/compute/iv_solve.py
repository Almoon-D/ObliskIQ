"""Solve an ATM implied vol where the route served none.

A historical vendor chain carries prices and no implied volatility — measured
2026-09-09, 0 of 608 contracts on a dated SPY call — so no quantity of bought
history turns on the implied-vol half of the grid. What the history does carry is
a bid and an ask, and an implied volatility is what you get when you invert a mid
price through a pricing model.

**This is the compute layer, and that is the whole point of it.** The recorded
decision against solving IV in the COLLECT layer stands: a collector holds
neither a risk-free rate at each contract's maturity nor a dividend yield per
underlying, and inventing either is worse than serving a NULL. This layer holds
both — the rate from the macro store at the session's own vintage, the yield from
``underlying_snapshots`` — so the objection that closed the question there does
not apply here. Nothing in this module changes the collector's behaviour of
writing ``iv NULL`` and reporting ``degraded``.

Four things this refuses to do, each of which produces a quietly wrong number:

**It never writes into the vendor's column.** ``implied_vol_solved`` is separate
from ``implied_vol`` because ``iv_percentile`` ranks today's reading against its
own history: a series whose early half is solved and whose recent half is
exchange-quoted has a methodology break at the join, and the percentile reports a
regime shift that did not happen. Two columns make that impossible rather than
merely discouraged.

**It never claims to have priced an American option.** BSM is European. SPY and
the sector ETFs are American-style and nothing in this repository handles early
exercise. The solve is bounded to the near-the-money, ``min_dte``-to-``max_dte``
region where the early-exercise premium is small, and every value it writes is
labelled :data:`SOURCE_SOLVED_BSM_EUROPEAN` so it can never be mistaken for a
quote in the column beside it.

**It never discounts a past session at today's rate.** The rate comes from
:func:`risk_free_rate_as_of`, bounded on both the reference and release dates. A
session with no rate knowable on it FAILS rather than defaulting —
``macro.risk_free_rate.on_failure`` is ``error`` and explicitly never a literal
fallback, and a rate substituted here would travel straight into the answer.

**It never reports a sigma it did not find.** Non-convergence is NULL and
counted, never clamped to the edge of the search bracket. Past
``numerics.implied_vol.solve.max_non_converged_fraction`` the run reports
``degraded``, mirroring what the vendor collector already does with the vendor's
own nulls.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

import numpy as np

from ..config import Config
from ..logging_setup import get_logger
from ..macro.series import risk_free_rate_as_of
from ..quant.bsm import BsmInputs, IvSolverInputs, bsm_greeks, implied_vol
from ..store.db import Store
from .frames import (
    ChainRow,
    chain_rows,
    collected_dividend_yield,
    latest_chain_date,
    universe_row,
)
from .smile import Smile, SmileFilters

log = get_logger("compute.iv_solve")

#: The method, and the word `european` in it is load-bearing. A reader who sees
#: this beside a number must be able to tell that the model priced a European
#: option and the instrument was not one.
SOURCE_SOLVED_BSM_EUROPEAN = "solved_bsm_european"

#: Why a row could not be solved. Reported per underlying rather than aggregated
#: away, because "no rate for that session" and "the book was too thin" are
#: different problems with different fixes.
SKIP_DISABLED = "solve_disabled"
SKIP_NO_CHAIN = "no_chain"
SKIP_NO_SPOT = "no_spot"
SKIP_NO_RATE = "no_rate_as_of_session"
SKIP_NO_CONTRACTS = "no_contracts_in_band"
SKIP_NO_SMILE = "no_smile_survived_hygiene"
SKIP_VENDOR_SERVED = "vendor_already_served_iv"


@dataclass(frozen=True)
class SolveInputs:
    """``numerics.implied_vol.solve``, bound once per run."""

    enabled: bool
    min_dte: int
    max_dte: int
    moneyness_band: float
    max_non_converged_fraction: float

    @classmethod
    def from_config(cls, config: Config) -> SolveInputs:
        cfg = config.section("numerics.implied_vol.solve")
        return cls(
            enabled=cfg.require_bool("enabled"),
            min_dte=cfg.require_int("min_dte"),
            max_dte=cfg.require_int("max_dte"),
            moneyness_band=cfg.require_float("moneyness_band"),
            max_non_converged_fraction=cfg.require_float("max_non_converged_fraction"),
        )


@dataclass
class SolveReport:
    """What one run solved, and what it could not."""

    as_of: date
    solved: int = 0
    attempted_contracts: int = 0
    converged_contracts: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def non_converged_fraction(self) -> float:
        """Nothing attempted is not a failure. 0.0, so an empty run is not degraded."""
        if self.attempted_contracts == 0:
            return 0.0
        return 1.0 - (self.converged_contracts / self.attempted_contracts)

    def is_degraded(self, inputs: SolveInputs) -> bool:
        return self.non_converged_fraction > inputs.max_non_converged_fraction

    def summary(self, inputs: SolveInputs) -> str:
        if self.attempted_contracts == 0:
            return "no contract was in a solvable band on any underlying"
        return (
            f"{self.solved} underlying(s) solved; {self.converged_contracts} of "
            f"{self.attempted_contracts} contracts converged "
            f"({self.non_converged_fraction:.0%} did not, against the "
            f"{inputs.max_non_converged_fraction:.0%} ceiling in "
            f"numerics.implied_vol.solve.max_non_converged_fraction)"
        )


def solve_session(store: Store, config: Config, *, as_of: date) -> SolveReport:
    """Solve and store one ATM implied vol per underlying for ``as_of``.

    One per underlying per session, not one per contract: the grid reads a single
    implied vol for indicators 7 and 9 and the IV percentile, and a full-chain
    solve mostly produces failures on strikes nothing reads.
    """
    inputs = SolveInputs.from_config(config)
    report = SolveReport(as_of=as_of)
    if not inputs.enabled:
        report.skip(SKIP_DISABLED)
        return report

    bsm = BsmInputs.from_config(config)
    solver = IvSolverInputs.from_config(config)
    filters = SmileFilters.from_config(config)

    for contract_id in _underlyings_with_chains(store, as_of=as_of):
        _solve_one(
            store,
            config,
            contract_id=contract_id,
            as_of=as_of,
            inputs=inputs,
            bsm=bsm,
            solver=solver,
            filters=filters,
            report=report,
        )
    return report


def _underlyings_with_chains(store: Store, *, as_of: date) -> list[int]:
    """Every underlying with a chain the store held on ``as_of``, ascending.

    Bounded at ``as_of`` like every other read here: a grid rebuilt for a past
    session must not discover an underlying from a chain collected after it.
    """
    rows = store.execute(
        "SELECT DISTINCT underlying_id FROM chain_snapshots "
        "WHERE snapshot_date <= ? ORDER BY underlying_id",
        (as_of.isoformat(),),
    ).fetchall()
    return [int(row["underlying_id"]) for row in rows]


def _solve_one(
    store: Store,
    config: Config,
    *,
    contract_id: int,
    as_of: date,
    inputs: SolveInputs,
    bsm: BsmInputs,
    solver: IvSolverInputs,
    filters: SmileFilters,
    report: SolveReport,
) -> None:
    session = latest_chain_date(store, contract_id, until=as_of)
    if session is None:
        report.skip(SKIP_NO_CHAIN)
        return

    rows = chain_rows(store, underlying_id=contract_id, snapshot_date=session)
    if not rows:
        report.skip(SKIP_NO_CHAIN)
        return

    # A route that served an implied vol is not improved by inverting its own
    # mid price. Solving anyway would spend the work to produce a second,
    # worse answer to a question already answered.
    if any(row.iv is not None for row in rows):
        report.skip(SKIP_VENDOR_SERVED)
        return

    spot = _spot_for(store, contract_id, session=session, as_of=as_of)
    if spot is None or spot <= 0:
        report.skip(SKIP_NO_SPOT)
        return

    universe = universe_row(store, contract_id)
    currency = str(universe.get("currency")) if universe and universe.get("currency") else None
    # Point-in-time, and it FAILS the underlying rather than defaulting. A rate is
    # not a nuisance parameter here: it sets the forward the whole solve is
    # relative to.
    rate = risk_free_rate_as_of(store, config, session, currency)
    if rate is None:
        report.skip(SKIP_NO_RATE)
        log.info(
            "no risk-free rate was knowable on this session; the underlying is left "
            "unsolved rather than discounted at a rate nobody chose",
            extra={"contract_id": contract_id, "session": session.isoformat()},
        )
        return

    q, q_source = _dividend_yield_for(store, universe, contract_id=contract_id, as_of=session)

    candidates = _solvable(rows, spot=spot, session=session, inputs=inputs)
    if not candidates:
        report.skip(SKIP_NO_CONTRACTS)
        return

    solved_rows, converged, attempted, reasons = _solve_rows(
        candidates,
        spot=spot,
        session=session,
        rate=rate.rate,
        dividend_yield=q,
        bsm=bsm,
        solver=solver,
    )
    report.attempted_contracts += attempted
    report.converged_contracts += converged
    for name, count in reasons.items():
        report.reasons[name] = report.reasons.get(name, 0) + count

    if not solved_rows:
        report.skip(SKIP_NO_SMILE)
        return

    # The SAME hygiene the vendor path and the risk engine use. Running a laxer
    # standard on a solved smile would make the grid and the risk engine disagree
    # about the same instrument on the same day, which is the defect
    # _atm_implied_vol's docstring already refuses.
    smile = Smile.from_rows(
        solved_rows,
        spot=spot,
        underlying_id=contract_id,
        expiry=solved_rows[0].expiry,
        filters=filters,
    )
    quote = None if smile is None else smile.atm()
    if quote is None:
        report.skip(SKIP_NO_SMILE)
        return

    _write(
        store,
        contract_id=contract_id,
        session=session,
        value=quote.iv,
        source=f"{SOURCE_SOLVED_BSM_EUROPEAN}/q_{q_source}",
    )
    report.solved += 1


def _spot_for(store: Store, contract_id: int, *, session: date, as_of: date) -> float | None:
    """Spot as at the chain's own session, never a later one.

    The underlying snapshot for that session first, then the daily close. Both
    bounded: pricing a past chain against today's spot would put the whole smile
    at the wrong moneyness and every solved sigma with it.
    """
    row = store.execute(
        "SELECT last FROM underlying_snapshots "
        "WHERE contract_id = ? AND snapshot_date <= ? AND last IS NOT NULL "
        "ORDER BY snapshot_date DESC LIMIT 1",
        (contract_id, session.isoformat()),
    ).fetchone()
    if row is not None and row["last"] is not None:
        return float(row["last"])
    bar = store.execute(
        "SELECT close FROM bars WHERE contract_id = ? AND ts <= ? AND close IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1",
        (contract_id, f"{session.isoformat()}T23:59:59Z"),
    ).fetchone()
    return None if bar is None or bar["close"] is None else float(bar["close"])


def _dividend_yield_for(
    store: Store, universe: dict[str, object] | None, *, contract_id: int, as_of: date
) -> tuple[float, str]:
    """q for the solve, and which tier it came from.

    Collected outranks declared, for the reason the ledger and the risk tool both
    give: a dated measurement beats a figure typed into a file at some past point.

    Zero when neither exists, and the tier travels into
    ``implied_vol_solved_source`` so the reader can see it. Zero is defensible
    HERE in a way it is not in a gate: q shifts a solved ATM sigma only slightly
    at these tenors, and refusing to solve at all would leave the grid dark on
    every underlying until a yield is collected — which is the outcome the whole
    exercise exists to end. It is labelled precisely so it is not mistaken for a
    measured non-payer.
    """
    collected = collected_dividend_yield(store, contract_id, as_of=as_of)
    if collected is not None:
        return collected[0], "collected"
    declared = None if universe is None else universe.get("dividend_yield")
    if declared is not None:
        return float(declared), "declared"
    return 0.0, "none_declared"


def _solvable(
    rows: list[ChainRow], *, spot: float, session: date, inputs: SolveInputs
) -> list[ChainRow]:
    """The one expiry, and the strikes on it, worth solving.

    Expiries outside ``[min_dte, max_dte]`` are dropped before anything is solved:
    that band is where a European model on an American option is a small error
    rather than a large one, and a value outside it is not made acceptable by
    being labelled.

    One expiry, the nearest to the middle of the band, because the grid reads one
    implied vol per underlying and a smile needs neighbouring strikes on the SAME
    expiry to have a shape at all.
    """
    low = spot * (1.0 - inputs.moneyness_band)
    high = spot * (1.0 + inputs.moneyness_band)
    by_expiry: dict[date, list[ChainRow]] = {}
    for row in rows:
        dte = (row.expiry - session).days
        if dte < inputs.min_dte or dte > inputs.max_dte:
            continue
        if row.mid is None or row.mid <= 0:
            continue
        if not (low <= row.strike <= high):
            continue
        by_expiry.setdefault(row.expiry, []).append(row)
    if not by_expiry:
        return []
    target = (inputs.min_dte + inputs.max_dte) / 2.0
    best = min(by_expiry, key=lambda e: abs((e - session).days - target))
    return by_expiry[best]


def _solve_rows(
    rows: list[ChainRow],
    *,
    spot: float,
    session: date,
    rate: float,
    dividend_yield: float,
    bsm: BsmInputs,
    solver: IvSolverInputs,
) -> tuple[list[ChainRow], int, int, dict[str, int]]:
    """Invert the mid price of each row, returning only what converged.

    Delta is computed from the solved sigma rather than left NULL. The smile's
    delta band is one of the four hygiene filters, and on a vendor chain that
    serves no greeks it would otherwise be a filter that never fires — hygiene
    reported as applied while one of its tests sat inert.
    """
    prices = np.array([row.mid for row in rows], dtype=float)
    strikes = np.array([row.strike for row in rows], dtype=float)
    dtes = np.array([(row.expiry - session).days for row in rows], dtype=float)
    calls = np.array([row.right.upper().startswith("C") for row in rows], dtype=bool)

    result = implied_vol(prices, spot, strikes, dtes, rate, dividend_yield, calls, bsm, solver)
    greeks = bsm_greeks(spot, strikes, dtes, result.sigma, rate, dividend_yield, calls, bsm)

    solved: list[ChainRow] = []
    for index, row in enumerate(rows):
        if not bool(result.converged[index]):
            continue
        sigma = float(result.sigma[index])
        if not np.isfinite(sigma):
            continue
        delta = float(greeks.delta[index])
        solved.append(replace(row, iv=sigma, delta=delta if np.isfinite(delta) else None))
    return solved, len(solved), len(rows), result.reason_counts()


def _write(store: Store, *, contract_id: int, session: date, value: float, source: str) -> None:
    """Upsert the solved value, leaving every vendor column untouched.

    An INSERT rather than an UPDATE because the underlying may have a chain and no
    snapshot row for that session at all — which is exactly the vendor-backfill
    case this exists for. ``source`` on a row created here is the vendor route
    that supplied the chain, defaulted by the schema; what this module owns is the
    two solved columns and nothing else.
    """
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO underlying_snapshots (
                snapshot_date, contract_id, implied_vol_solved, implied_vol_solved_source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (snapshot_date, contract_id) DO UPDATE SET
                implied_vol_solved = excluded.implied_vol_solved,
                implied_vol_solved_source = excluded.implied_vol_solved_source
            """,
            (session.isoformat(), contract_id, value, source),
        )


__all__ = [
    "SOURCE_SOLVED_BSM_EUROPEAN",
    "SolveInputs",
    "SolveReport",
    "solve_session",
]
