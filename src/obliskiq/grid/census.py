"""The census: how much of the option universe clears every candidate gate at once, and how much
of the declared grid each family can see.

Ported from lite's ``compute/universe_census.py``. Each gate on its own looks survivable; the
intersection is what the sleeve can trade, and it is much smaller than any single count suggests.
Below ``census.min_viable_contracts`` the census reports it: either the gates are miscalibrated or
the sleeve is too small, and neither is fixed by relaxing a threshold.

The candidate gates are composed in Session 12 (``SESSION_PROMPTS.md``), so until they exist the
census scans the newest chain, counts its contracts and reports every gate untested: an untested
gate is never a passed one, and no contract is said to clear a gate nobody applied. Family reach is
a property of the declarations and of the built grid, and is reported either way.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from ..config.schema import ObliskConfig
from ..store import repos
from ..store.db import Store
from .views import FamilyReach, grid_reach


class Verdict(StrEnum):
    NO_DATA = "no_data"
    UNTESTED = "untested"


@dataclass(frozen=True, slots=True)
class CensusResult:
    as_of: date
    snapshot_date: date | None
    contracts_scanned: int
    min_required: int
    verdict: Verdict
    reason: str
    reach: tuple[FamilyReach, ...] = field(default_factory=tuple)
    thin_families: tuple[str, ...] = field(default_factory=tuple)

    def report(self) -> str:
        lines = [f"census for {self.as_of}: {self.verdict.value}. {self.reason}"]
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
    """Counts the option universe and reports family reach; never relaxes a gate."""

    def __init__(self, settings: ObliskConfig) -> None:
        self._min_required = settings.census.min_viable_contracts
        self._min_cells = settings.grid.min_cells_for_ranked_view

    def run(self, store: Store, *, as_of: date) -> CensusResult:
        reach = tuple(grid_reach(store, as_of=as_of))
        thin = self._thin(reach)
        session = repos.latest_chain_date(store, as_of=as_of)
        if session is None:
            return CensusResult(
                as_of=as_of,
                snapshot_date=None,
                contracts_scanned=0,
                min_required=self._min_required,
                verdict=Verdict.NO_DATA,
                reason="no chain snapshot had been collected by this day",
                reach=reach,
                thin_families=thin,
            )
        scanned = len(repos.chain_rows(store, as_of=as_of, snapshot_date=session))
        return CensusResult(
            as_of=as_of,
            snapshot_date=session,
            contracts_scanned=scanned,
            min_required=self._min_required,
            verdict=Verdict.UNTESTED,
            reason=(
                f"{scanned} contracts on the {session} chain; the candidate gates are composed in "
                "Session 12, so every gate is untested and none is reported passed (the "
                f"viability floor is {self._min_required})"
            ),
            reach=reach,
            thin_families=thin,
        )

    def _thin(self, reach: Sequence[FamilyReach]) -> tuple[str, ...]:
        """Families reaching fewer cells than a ranking can be built from, on reachable cells: a
        family a declaration blocks is still thin tomorrow, one merely uncollected fills in."""
        return tuple(item.family for item in reach if item.cells_reachable < self._min_cells)
