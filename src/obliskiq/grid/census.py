"""The census: how much of the option universe clears every candidate gate at once, and how much
of the declared grid each family can see.

Ported from lite's ``compute/universe_census.py``. Each gate on its own looks survivable; the
intersection is what the sleeve can trade, and it is much smaller than any single count suggests.
Below ``census.min_viable_contracts`` the census reports it: either the gates are miscalibrated or
the sleeve is too small, and neither is fixed by relaxing a threshold. The census runs the same
:meth:`~obliskiq.decision.gates.CandidateGates.candidate` the sleeve does, over the same assembled
candidates (``engines/options_sleeve/candidates.py``), so the two can never disagree about a gate.

**An untested gate is not a passed gate.** A contract that failed nothing but left a gate
unmeasured (no budget known to the compute run, no rate, no percentile yet) is counted apart, and
a shortfall that untested gates could explain is reported as ``untested``, never as ``thin``.
Family reach is a property of the declarations and of the built grid, and is reported either way.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..decision.gates import CandidateGates, Gate
from ..engines.options_sleeve.candidates import CandidateBook, Pool
from ..store import options as options_repo
from ..store import repos
from ..store.db import Store
from .views import FamilyReach, grid_reach


class Verdict(StrEnum):
    NO_DATA = "no_data"
    VIABLE = "viable"
    THIN = "thin"
    UNTESTED = "untested"


#: The gates counted one by one beside the intersection, in the order a report prints them.
COUNTED_GATES = (
    Gate.INSTRUMENT,
    Gate.TENOR,
    Gate.MANDATORY_CLOSE,
    Gate.OPEN_INTEREST,
    Gate.SPREAD,
    Gate.VOLUME,
    Gate.SMILE,
    Gate.CONCURRENCY,
    Gate.PRICE_CEILING,
    Gate.IV_PERCENTILE,
)


@dataclass(frozen=True, slots=True)
class CensusResult:
    as_of: date
    snapshot_date: date | None
    contracts_scanned: int
    #: Cleared every gate with every gate applied: what the floor is measured against.
    passed_all_gates: int
    #: Failed nothing but left at least one gate unmeasured.
    passed_with_untested: int
    per_gate: dict[Gate, int]
    untested: dict[Gate, int]
    min_required: int
    verdict: Verdict
    reason: str
    survivors: tuple[str, ...] = field(default_factory=tuple)
    reach: tuple[FamilyReach, ...] = field(default_factory=tuple)
    thin_families: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_viable(self) -> bool:
        return self.verdict is Verdict.VIABLE

    def report(self) -> str:
        lines = [f"census for {self.as_of}: {self.verdict.value}. {self.reason}"]
        if self.contracts_scanned:
            for gate in COUNTED_GATES:
                untested = self.untested.get(gate, 0)
                suffix = f", {untested} untested" if untested else ""
                lines.append(f"  {gate.value}: {self.per_gate.get(gate, 0)} pass{suffix}")
        if self.reach:
            lines += ["", "what each indicator family can reach, worst first:"]
            lines += [f"  {item.describe()}" for item in self.reach]
        if self.thin_families:
            lines.append(
                f"  THIN: {', '.join(self.thin_families)} reach fewer cells than "
                "grid.min_cells_for_ranked_view; an ordering that short is a shortlist, not a "
                "ranking, and the ranked views say so on every row"
            )
        return "\n".join(lines)


class Census:
    """Counts what clears every candidate gate and reports family reach; never relaxes a gate."""

    def __init__(self, settings: ObliskConfig) -> None:
        self._settings = settings
        self._gates = CandidateGates(settings)
        self._min_required = settings.census.min_viable_contracts
        self._min_cells = settings.grid.min_cells_for_ranked_view

    def run(
        self,
        store: Store,
        *,
        as_of: date,
        budget_gap: str,
        calendars: Calendars | None = None,
    ) -> CensusResult:
        """The census on ``as_of``. The pool's budget is not known to an offline run, so its gate
        is untested with ``budget_gap``; the sleeve's open positions are read from the store."""
        reach = tuple(grid_reach(store, as_of=as_of))
        thin = self._thin(reach)
        session = repos.latest_chain_date(store, as_of=as_of)
        if session is None:
            return CensusResult(
                as_of=as_of,
                snapshot_date=None,
                contracts_scanned=0,
                passed_all_gates=0,
                passed_with_untested=0,
                per_gate={},
                untested={},
                min_required=self._min_required,
                verdict=Verdict.NO_DATA,
                reason="no chain snapshot had been collected by this day",
                reach=reach,
                thin_families=thin,
            )
        pool = Pool(
            affordability=None,
            affordability_gap=budget_gap,
            open_positions=len(options_repo.open_positions(store, as_of=as_of)),
            open_positions_gap=None,
        )
        book = CandidateBook(store, self._settings, calendars)
        per_gate: Counter[Gate] = Counter()
        untested: Counter[Gate] = Counter()
        survivors: list[str] = []
        incomplete = scanned = 0
        for candidate in book.candidates(as_of=as_of, pool=pool):
            scanned += 1
            result = self._gates.candidate(candidate)
            failed = set(result.failed_gates)
            missing = set(result.untested_gates)
            untested.update(missing)
            per_gate.update(gate for gate in COUNTED_GATES if gate not in failed | missing)
            row = candidate.row
            if result.fully_tested:
                survivors.append(
                    f"{row.underlying_conid}:{row.expiry}:{row.right.value}{row.strike:g}"
                )
            elif result.passed:
                incomplete += 1
        if not scanned:
            skipped = "; ".join(item.reason for item in book.skipped) or "no contract listed"
            verdict, reason = Verdict.NO_DATA, f"no chain could be put through the gates: {skipped}"
        else:
            verdict, reason = self._verdict(scanned, len(survivors), incomplete, session)
        return CensusResult(
            as_of=as_of,
            snapshot_date=session,
            contracts_scanned=scanned,
            passed_all_gates=len(survivors),
            passed_with_untested=incomplete,
            per_gate=dict(per_gate),
            untested=dict(untested),
            min_required=self._min_required,
            verdict=verdict,
            reason=reason,
            survivors=tuple(survivors),
            reach=reach,
            thin_families=thin,
        )

    def _verdict(
        self, scanned: int, passed: int, incomplete: int, session: date
    ) -> tuple[Verdict, str]:
        head = (
            f"{passed} of {scanned} contracts on the chains up to {session} clear every gate with "
            f"every gate applied (floor {self._min_required})"
        )
        if passed >= self._min_required:
            return Verdict.VIABLE, head
        if incomplete:
            return Verdict.UNTESTED, (
                f"{head}; a further {incomplete} failed nothing but left a gate unmeasured, so the "
                "shortfall is not measured: an untested gate is not a passed gate"
            )
        return Verdict.THIN, (
            f"{head}: below the floor. Either the gates are miscalibrated or the sleeve is too "
            "small for this universe; neither is fixed by relaxing a threshold"
        )

    def _thin(self, reach: Sequence[FamilyReach]) -> tuple[str, ...]:
        """Families reaching fewer cells than a ranking can be built from, on reachable cells: a
        family a declaration blocks is still thin tomorrow, one merely uncollected fills in."""
        return tuple(item.family for item in reach if item.cells_reachable < self._min_cells)
