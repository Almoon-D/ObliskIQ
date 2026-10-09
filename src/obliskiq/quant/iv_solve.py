"""Solving an at-the-money implied volatility where the route served none.

A historical vendor chain carries prices and no implied volatility, so no quantity of bought
history turns on the implied-volatility half of the picture. What the history does carry is a
bid and an ask, and an implied volatility is what an inverted mid price gives. This module is
the pure part of that solve: choosing which contracts are worth solving and solving them. It
holds no store, no rate lookup and no clock; the caller supplies the rate and the yield for the
session and decides what to do with the result.

What the solve refuses to do, each of which produces a quietly wrong number:

* **Claim to have priced an American option.** BSM prices a European option; the ETFs here are
  American-style and nothing in this repository handles early exercise. The solve is bounded to
  the near-the-money, ``min_dte``-to-``max_dte`` region where the early-exercise premium is small,
  and every value it feeds onward is labelled :data:`SOURCE_SOLVED_BSM_EUROPEAN` so it can never
  be mistaken for a quote in the column beside it.
* **Discount at a rate nobody chose.** The rate and the dividend yield are arguments.
* **Report a sigma it did not find.** Non-convergence is counted, never clamped to the edge of the
  bracket; past ``max_non_converged_fraction`` a run reports degraded.

The rows :func:`solve_rows` returns carry the solved sigma in ``iv`` and the model delta in
``delta`` because that is the shape :class:`obliskiq.quant.smile.Smile` reads. They are inputs to
a smile and are never stored as quotes: a solved and a quoted volatility are different
measurements, and a series mixing them has a methodology break at the join.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from ..config import ObliskConfig
from ..domain.enums import OptionRight
from ..domain.market import ChainRow
from .bsm import BsmInputs, IvSolverInputs, bsm_greeks, implied_vol

#: The method, and the word ``european`` in it is load-bearing: a reader who sees this beside a
#: number must be able to tell that the model priced a European option and the instrument was
#: not one.
SOURCE_SOLVED_BSM_EUROPEAN = "solved_bsm_european"


@dataclass(frozen=True, slots=True)
class SolveInputs:
    """``numerics.implied_vol.solve``, bound once per run."""

    min_dte: int
    max_dte: int
    moneyness_band: float
    max_non_converged_fraction: float

    @classmethod
    def from_config(cls, settings: ObliskConfig) -> SolveInputs:
        solve = settings.numerics.implied_vol.solve
        return cls(
            min_dte=solve.min_dte,
            max_dte=solve.max_dte,
            moneyness_band=solve.moneyness_band,
            max_non_converged_fraction=solve.max_non_converged_fraction,
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

    def add(self, result: SolvedRows) -> None:
        """Fold one underlying's outcome into the run's totals."""
        self.attempted_contracts += result.attempted
        self.converged_contracts += result.converged
        for name, count in result.reasons.items():
            self.reasons[name] = self.reasons.get(name, 0) + count

    @property
    def non_converged_fraction(self) -> float:
        """Nothing attempted is not a failure: 0.0, so an empty run is not degraded."""
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


def select_contracts(
    rows: Sequence[ChainRow], *, spot: float, session: date, inputs: SolveInputs
) -> list[ChainRow]:
    """The one expiry, and the strikes on it, worth solving.

    Expiries outside ``[min_dte, max_dte]`` are dropped before anything is solved: that band is
    where a European model on an American option is a small error and not a large one, and a
    value outside it is not made acceptable by being labelled.

    One expiry, the nearest to the middle of the band, because one implied volatility is read per
    underlying and a smile needs neighbouring strikes on the same expiry to have a shape at all.
    Strikes within ``moneyness_band`` of spot are kept, and a contract with no positive mid is
    not a price to invert.
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
        if not low <= row.strike <= high:
            continue
        by_expiry.setdefault(row.expiry, []).append(row)
    if not by_expiry:
        return []
    target = (inputs.min_dte + inputs.max_dte) / 2.0
    best = min(by_expiry, key=lambda expiry: abs((expiry - session).days - target))
    return by_expiry[best]


@dataclass(frozen=True, slots=True)
class SolvedRows:
    """The contracts that converged, and the tally of those that did not."""

    rows: tuple[ChainRow, ...]
    converged: int
    attempted: int
    reasons: dict[str, int]


def solve_rows(
    rows: Sequence[ChainRow],
    *,
    spot: float,
    session: date,
    rate: float,
    dividend_yield: float,
    bsm: BsmInputs,
    solver: IvSolverInputs,
) -> SolvedRows:
    """Invert the mid price of each row, returning only what converged.

    Delta is computed from the solved sigma and not left missing: the smile's delta band is one
    of its four hygiene filters, and on a vendor chain that serves no greeks it would otherwise be
    a filter that never fires, hygiene reported as applied while one of its tests sat inert.
    """
    mids = [row.mid for row in rows]
    prices = np.array([mid if mid is not None else np.nan for mid in mids], dtype=float)
    strikes = np.array([row.strike for row in rows], dtype=float)
    dtes = np.array([(row.expiry - session).days for row in rows], dtype=float)
    calls = np.array([row.right is OptionRight.CALL for row in rows], dtype=bool)

    result = implied_vol(prices, spot, strikes, dtes, rate, dividend_yield, calls, bsm, solver)
    greeks = bsm_greeks(spot, strikes, dtes, result.sigma, rate, dividend_yield, calls, bsm)

    solved: list[ChainRow] = []
    for index, row in enumerate(rows):
        if not bool(result.converged[index]):
            continue
        sigma = float(result.sigma[index])
        delta = float(greeks.delta[index])
        solved.append(row.model_copy(update={"iv": sigma, "delta": delta}))
    return SolvedRows(
        rows=tuple(solved),
        converged=len(solved),
        attempted=len(rows),
        reasons=result.reason_counts(),
    )
