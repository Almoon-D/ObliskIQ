"""The vehicles that fill the core's roles (MASTER_PLAN §5.4, §5.8, DECISIONS D120, D122, D140).

The vehicle selector ranks each role's candidates, and the best fills the role, with two rules of
inertia: a vehicle the core already holds for the role stays while the selector still proposes it
(a better score never moves the book by itself), and a role whose vehicle is not proposable is
unfilled, with why (the engine then holds its weight in cash and says so). A tilting variant also
takes the swap partner the swap rules name, so a loss sale is not deferred by buying the vehicle
back (the partner already held stays, too).

The selector is pure and the inputs it reads come from the stores: :class:`RoleInputs` gathers them
(``orchestration/vehicle_inputs.py``); a choice is kept for the day, since its inputs move slowly.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ..config.schema import ObliskConfig
from ..config.schema_universe import UniverseInstrument
from ..engines.core.mechanism import RoleVehicles
from ..quant.tracking import CorrelationReading
from .swap_partners import SwapPartners
from .vehicle_selector import (
    Exposure,
    RankedVehicle,
    Selection,
    SelectorInputs,
    VehicleSelector,
    resolve_exposure,
)


@dataclass(frozen=True, slots=True)
class RoleChoice:
    """The vehicles of one role, or why none fills it."""

    role: str
    primary: RankedVehicle | None
    partner: RankedVehicle | None
    why: str

    @property
    def vehicles(self) -> RoleVehicles | None:
        if self.primary is None:
            return None
        return RoleVehicles(
            str(self.primary.conid), None if self.partner is None else str(self.partner.conid)
        )


@dataclass(frozen=True, slots=True)
class VehicleChoice:
    """Every asked role's choice."""

    roles: Mapping[str, RoleChoice]

    @property
    def vehicles(self) -> dict[str, RoleVehicles]:
        """The roles a vehicle fills, by the keys the book names them (a contract id)."""
        return {role: found for role, choice in self.roles.items() if (found := choice.vehicles)}

    @property
    def conids(self) -> dict[str, int]:
        """Each chosen vehicle's contract id by its key."""
        out: dict[str, int] = {}
        for choice in self.roles.values():
            for vehicle in (choice.primary, choice.partner):
                if vehicle is not None:
                    out[str(vehicle.conid)] = vehicle.conid
        return out

    @property
    def unfilled(self) -> dict[str, str]:
        return {role: choice.why for role, choice in self.roles.items() if choice.primary is None}


class VehicleSource(Protocol):
    """Chooses the vehicles of the core's roles."""

    async def choose(
        self, roles: Iterable[str], *, held: Collection[int], aware: bool, today: date
    ) -> VehicleChoice: ...


class RoleInputs(Protocol):
    """What the selector and the swap rules read, gathered from the stores."""

    async def selector_inputs(
        self, exposure: Exposure, candidates: Sequence[UniverseInstrument], today: date
    ) -> SelectorInputs: ...

    async def partner_readings(
        self, pairs: Sequence[tuple[str, str]], today: date
    ) -> Mapping[tuple[str, str], CorrelationReading]: ...


class VehicleChooser:
    """Fills the roles through the selector."""

    def __init__(
        self,
        settings: ObliskConfig,
        selector: VehicleSelector,
        swaps: SwapPartners,
        inputs: RoleInputs,
    ) -> None:
        self._settings = settings
        self._selector = selector
        self._swaps = swaps
        self._inputs = inputs
        self._selections: dict[tuple[str, date], Selection] = {}
        self._partners: dict[tuple[str, str, date], tuple[str, ...]] = {}

    async def choose(
        self, roles: Iterable[str], *, held: Collection[int], aware: bool, today: date
    ) -> VehicleChoice:
        """The choice for ``roles``; ``held`` are the contract ids the core holds."""
        chosen: dict[str, RoleChoice] = {}
        for role in dict.fromkeys(roles):
            selection = await self._selection(role, today)
            primary = _kept_or_best(selection.ranked, held)
            if primary is None:
                chosen[role] = RoleChoice(role, None, None, _why_none(selection))
                continue
            partner = None
            if aware:
                partner = await self._partner(selection, primary, held, today)
            chosen[role] = RoleChoice(role, primary, partner, "")
        return VehicleChoice(chosen)

    async def _selection(self, role: str, today: date) -> Selection:
        key = (role, today)
        if key not in self._selections:
            exposure = resolve_exposure(self._settings, role)
            candidates = self._selector.candidates(exposure)
            inputs = await self._inputs.selector_inputs(exposure, candidates, today)
            self._selections[key] = self._selector.select(exposure, inputs)
        return self._selections[key]

    async def _partner(
        self, selection: Selection, primary: RankedVehicle, held: Collection[int], today: date
    ) -> RankedVehicle | None:
        cached = (selection.exposure.key, primary.symbol, today)
        if cached not in self._partners:
            others = [v.symbol for v in selection.ranked if v.symbol != primary.symbol]
            pairs = [(primary.symbol, other) for other in others]
            readings = await self._inputs.partner_readings(pairs, today)
            group = self._swaps.group(selection, primary.symbol, readings)
            self._partners[cached] = tuple(p.symbol for p in group.partners)
        partners = [v for v in selection.ranked if v.symbol in self._partners[cached]]
        return _kept_or_best(tuple(partners), held)


def _kept_or_best(ranked: Sequence[RankedVehicle], held: Collection[int]) -> RankedVehicle | None:
    """The best-ranked vehicle already held, else the best ranked."""
    return next((v for v in ranked if v.conid in held), ranked[0] if ranked else None)


def _why_none(selection: Selection) -> str:
    if not selection.rejected and not selection.ranked:
        return f"{selection.exposure.key} has no candidate in the universe"
    reasons = "; ".join(f"{r.symbol} [{r.gate.value}]" for r in selection.rejected)
    return f"no vehicle of {selection.exposure.key} is proposable: {reasons}"
