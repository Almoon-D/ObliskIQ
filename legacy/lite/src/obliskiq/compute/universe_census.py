"""The weekly census: how many contracts clear EVERY gate at once.

This settles empirically a question that is otherwise argued: is the
sub-affordability-ceiling option universe actually viable? Each gate on its own
looks survivable. The intersection is what the sleeve can trade, and the
intersection is much smaller than any single count suggests.

If the count is routinely below ``census.min_viable_contracts``, either the gates
are miscalibrated or the sleeve is too small. **Report it.** Do not silently
relax a threshold: a gate quietly loosened to manufacture candidates is how a
discipline becomes decoration, and nothing here has the power to change one — the
census runs the same :class:`~obliskiq.compute.screen.OptionScreen` evaluation the
screener does and writes down what it found.

An untested gate is not a passed gate. A contract whose open interest never came
back has not cleared the open-interest floor; it has not been measured, and the
census reports the two separately so a thin data day cannot look like a healthy
universe.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from ..config import Config
from ..logging_setup import get_logger
from ..store.db import Store
from ..util.timeutil import iso_date
from .build_grid import FamilyReach, family_reach
from .frames import ChainRow, chain_rows, latest_chain_date, latest_nav
from .gates import (
    GATE_IV_PERCENTILE,
    GATE_OPEN_INTEREST,
    GATE_PRICE_CEILING,
    GATE_SPREAD,
    GATE_TENOR,
    GATE_VOLUME,
    AffordabilityCeiling,
    GateResult,
)
from .screen import OptionScreen

log = get_logger("compute.census")

#: Verdicts written to ``universe_census.verdict``.
VERDICT_VIABLE = "viable"
VERDICT_THIN = "thin"
VERDICT_NO_DATA = "no_data"

#: The gates counted individually alongside the intersection.
COUNTED_GATES = (
    GATE_PRICE_CEILING,
    GATE_OPEN_INTEREST,
    GATE_SPREAD,
    GATE_TENOR,
    GATE_IV_PERCENTILE,
)


@dataclass(frozen=True)
class CensusResult:
    """One census run."""

    as_of: date
    snapshot_date: date | None
    nav: float | None
    available_budget: float | None
    contracts_scanned: int
    #: Cleared every gate, with EVERY gate actually applied. This is the number
    #: the viability floor is measured against.
    passed_all_gates: int
    #: Cleared every gate that could be applied, with at least one left
    #: unmeasured. Reported apart, because a contract whose IV percentile was
    #: never served has not cleared the IV percentile gate — it was not asked.
    passed_with_untested: int
    per_gate: dict[str, int]
    untested: dict[str, int]
    min_required: int
    verdict: str
    survivors: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None
    #: How much of the declared grid each indicator family can actually reach,
    #: ascending. The census is the report that answers "what can this machine
    #: see at all", and a family reaching a handful of cells was discoverable
    #: only by reading universe.yaml and working out which rows collect a chain.
    #: Reported here so it is read, not derived by whoever thinks to look.
    grid_reach: tuple[FamilyReach, ...] = field(default_factory=tuple)
    #: Families below ``grid.min_cells_for_ranked_view``, named rather than left
    #: to be spotted in the list.
    thin_families: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_viable(self) -> bool:
        return self.verdict == VERDICT_VIABLE

    def report(self) -> str:
        if self.verdict == VERDICT_NO_DATA:
            return "\n".join(
                [f"census could not run: {self.reason}", *self._reach_lines()]
            ).rstrip()
        lines = [
            f"{self.passed_all_gates} of {self.contracts_scanned} contracts clear every "
            f"gate simultaneously (floor {self.min_required})."
        ]
        if self.passed_with_untested:
            lines.append(
                f"a further {self.passed_with_untested} failed nothing but left at least "
                "one gate unmeasured. An untested gate is not a passed gate."
            )
        for gate in COUNTED_GATES:
            untested = self.untested.get(gate, 0)
            suffix = f", {untested} untested" if untested else ""
            lines.append(f"  {gate}: {self.per_gate.get(gate, 0)} pass{suffix}")
        if not self.is_viable:
            lines.append(
                "BELOW THE FLOOR. Either the gates are miscalibrated or the sleeve is too "
                "small for this universe. Neither is fixed by relaxing a threshold."
            )
        lines.extend(self._reach_lines())
        return "\n".join(lines)

    def _reach_lines(self) -> list[str]:
        """What each indicator family can see, worst first.

        Printed even when the census itself could not run: the option universe
        needs a NAV and a chain snapshot, while family reach is a property of the
        DECLARATIONS and is readable from any built grid. A run that cannot count
        contracts can still say which fifteenth of the grid is a coin flip.
        """
        if not self.grid_reach:
            return []
        lines = ["", "what each indicator family can reach, worst first:"]
        lines.extend(f"  {item.describe()}" for item in self.grid_reach)
        if self.thin_families:
            lines.append(
                f"  THIN: {', '.join(self.thin_families)} rank below "
                "grid.min_cells_for_ranked_view. An ordering that short is a shortlist, "
                "not a ranking, and the ranked views say so on every row."
            )
        return lines


class UniverseCensus:
    """Counts what clears every gate, weekly, and never relaxes one."""

    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config
        # The census and the screener must never drift apart on what a gate is,
        # so they share one evaluation rather than each building their own.
        self._screen = OptionScreen(store, config)
        self._min_required = config.require_int("census.min_viable_contracts")
        self._frequency = config.require_str("data.universe_census_frequency")
        # The same floor the ranked views label themselves against, read once. Two
        # numbers for "too few cells to rank" would let the census and the table
        # disagree about the same view on the same day.
        self._min_cells = config.require_int("grid.min_cells_for_ranked_view")

    @property
    def frequency(self) -> str:
        return self._frequency

    def run(self, *, as_of: date, nav: float | None = None) -> CensusResult:
        """Scan the most recent chain snapshot and count the survivors."""
        resolved_nav = nav if nav is not None else latest_nav(self._store)
        snapshot_date = latest_chain_date(self._store, until=as_of)
        reach = self._reach(as_of)

        if snapshot_date is None:
            return self._no_data(
                as_of, resolved_nav, "no chain snapshots have been collected", reach
            )
        if resolved_nav is None:
            return self._no_data(
                as_of,
                None,
                "no NAV is available, so the affordability ceiling cannot be computed. "
                "The budget defines the universe, and discovering it last is the most "
                "repeated failure mode in this account.",
                reach,
            )

        ceiling = self._screen.affordability(resolved_nav)
        rows = chain_rows(self._store, snapshot_date=snapshot_date)
        per_gate = dict.fromkeys(COUNTED_GATES, 0)
        untested = dict.fromkeys(COUNTED_GATES, 0)
        survivors: list[str] = []

        iv_percentiles: dict[int, float | None] = {}
        incomplete = 0
        for row in rows:
            outcome = self._evaluate(row, ceiling, as_of, iv_percentiles)
            for gate in COUNTED_GATES:
                if gate in outcome.untested:
                    untested[gate] += 1
                elif gate not in outcome.failed_gates:
                    per_gate[gate] += 1
            if outcome.fully_tested:
                survivors.append(f"{row.underlying_id}:{row.expiry.isoformat()}:{row.strike:g}")
            elif outcome.passed:
                incomplete += 1

        verdict = VERDICT_VIABLE if len(survivors) >= self._min_required else VERDICT_THIN
        result = CensusResult(
            as_of=as_of,
            snapshot_date=snapshot_date,
            nav=resolved_nav,
            available_budget=ceiling.available_budget,
            contracts_scanned=len(rows),
            passed_all_gates=len(survivors),
            passed_with_untested=incomplete,
            per_gate=per_gate,
            untested=untested,
            min_required=self._min_required,
            verdict=verdict,
            survivors=tuple(survivors),
            reason=(
                None
                if verdict == VERDICT_VIABLE
                else (
                    f"{len(survivors)} contract(s) clear every gate against a floor of "
                    f"{self._min_required}"
                    + (
                        f"; a further {incomplete} failed nothing but left a gate unmeasured"
                        if incomplete
                        else ""
                    )
                    + ". Reported, not fixed by relaxing a threshold."
                )
            ),
            grid_reach=reach,
            thin_families=self._thin(reach),
        )
        if not result.is_viable:
            log.warning(
                "the option universe is below the viability floor",
                extra={
                    "passed_all_gates": result.passed_all_gates,
                    "min_required": self._min_required,
                    "contracts_scanned": result.contracts_scanned,
                },
            )
        return result

    def write(self, result: CensusResult) -> None:
        detail = json.dumps(
            {
                "snapshot_date": (
                    result.snapshot_date.isoformat() if result.snapshot_date else None
                ),
                "nav": result.nav,
                "available_budget": result.available_budget,
                "untested": result.untested,
                "passed_with_untested": result.passed_with_untested,
                "survivors": list(result.survivors),
                "reason": result.reason,
                "grid_reach": [
                    {
                        "family": item.family,
                        "cells_total": item.cells_total,
                        "cells_reachable": item.cells_reachable,
                        "cells_populated": item.cells_populated,
                        "dominant_blocker": item.dominant_blocker,
                        "blocked_cells": item.blocked_cells,
                    }
                    for item in result.grid_reach
                ],
                "thin_families": list(result.thin_families),
            }
        )
        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO universe_census (
                    as_of, contracts_scanned, passed_all_gates, passed_price,
                    passed_open_interest, passed_spread, passed_tenor, passed_iv_pctile,
                    min_required, verdict, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (as_of) DO UPDATE SET
                    contracts_scanned = excluded.contracts_scanned,
                    passed_all_gates = excluded.passed_all_gates,
                    passed_price = excluded.passed_price,
                    passed_open_interest = excluded.passed_open_interest,
                    passed_spread = excluded.passed_spread,
                    passed_tenor = excluded.passed_tenor,
                    passed_iv_pctile = excluded.passed_iv_pctile,
                    min_required = excluded.min_required,
                    verdict = excluded.verdict,
                    detail = excluded.detail
                """,
                (
                    iso_date(result.as_of),
                    result.contracts_scanned,
                    result.passed_all_gates,
                    result.per_gate.get(GATE_PRICE_CEILING, 0),
                    result.per_gate.get(GATE_OPEN_INTEREST, 0),
                    result.per_gate.get(GATE_SPREAD, 0),
                    result.per_gate.get(GATE_TENOR, 0),
                    result.per_gate.get(GATE_IV_PERCENTILE, 0),
                    result.min_required,
                    result.verdict,
                    detail,
                ),
            )

    def history(self, *, limit: int) -> Sequence[dict[str, object]]:
        rows = self._store.execute(
            "SELECT as_of, contracts_scanned, passed_all_gates, min_required, verdict "
            "FROM universe_census ORDER BY as_of DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def is_routinely_thin(self, *, limit: int) -> bool:
        """Is the universe below the floor on most recent runs?

        The brief's word is "routinely": one thin week is a market, several is a
        finding about the gates or the sleeve.
        """
        recent = self.history(limit=limit)
        if not recent:
            return False
        thin = sum(1 for row in recent if row["verdict"] == VERDICT_THIN)
        return thin * _MAJORITY_MULTIPLE > len(recent)

    # ── internals ──

    def _evaluate(
        self,
        row: ChainRow,
        ceiling: AffordabilityCeiling,
        as_of: date,
        iv_cache: dict[int, float | None],
    ) -> GateResult:
        """Every gate against one strike, through the shared screen.

        Delegated deliberately: a gate applied here and forgotten in
        ``screen_options`` would make the weekly count a measurement of
        something the screener never enforces.
        """
        return self._screen.evaluate(row, ceiling, as_of, iv_cache)

    def _reach(self, as_of: date) -> tuple[FamilyReach, ...]:
        """Family reach off the last built grid, or empty if none was built."""
        return tuple(family_reach(self._store, as_of=as_of))

    def _thin(self, reach: Sequence[FamilyReach]) -> tuple[str, ...]:
        """Families reaching fewer cells than a ranking can be built from.

        Measured on cells REACHABLE rather than populated: a family blocked by a
        declaration will still be thin tomorrow, while one merely uncollected
        fills in. Only the first is a fact about the machine.
        """
        return tuple(item.family for item in reach if item.cells_reachable < self._min_cells)

    def _no_data(
        self,
        as_of: date,
        nav: float | None,
        reason: str,
        reach: Sequence[FamilyReach] = (),
    ) -> CensusResult:
        return CensusResult(
            as_of=as_of,
            snapshot_date=None,
            nav=nav,
            available_budget=None,
            contracts_scanned=0,
            passed_all_gates=0,
            passed_with_untested=0,
            per_gate=dict.fromkeys(COUNTED_GATES, 0),
            untested=dict.fromkeys(COUNTED_GATES, 0),
            min_required=self._min_required,
            verdict=VERDICT_NO_DATA,
            reason=reason,
            grid_reach=tuple(reach),
            thin_families=self._thin(reach),
        )


#: "Routinely" means a majority of recent runs. Two of three, three of five.
_MAJORITY_MULTIPLE = 2

#: Gates whose absence is reported separately from a failure.
UNTESTED_GATES = (GATE_VOLUME,)
