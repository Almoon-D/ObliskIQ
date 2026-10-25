"""Each core role's vehicles (MASTER_PLAN §5.4, §5.8; DECISIONS D120-D123).

For every role the core holds or its variant weights, the vehicle selector ranks the role's
vehicles (the gates, then the weighted criteria) and:

* the **primary** is the best-ranked vehicle whose identity IBKR confirmed: where a purchase goes
  when the role holds no vehicle that may be bought;
* the **partner**, for a variant that trades tax-aware, is the primary's best swap partner
  (``SwapPartners``): where a purchase goes when buying the primary would defer a loss;
* every **held** vehicle of the role stays one of its vehicles: it counts in the role's weight, a
  purchase goes to it first while it may be bought, and a sale takes from it like any other. A
  held vehicle the selector no longer proposes (or IBKR has not confirmed) is **closed**: sold,
  never bought. A vehicle is never switched for a better-ranked one, which would realise its gain.

A role without a proposable vehicle has no primary; a held vehicle of no core role is reported
under its own name and, having no target, is sold. Tracking and partner correlations are measured
on research history and handed in (:class:`Correlations`): the decision path never reads research
(DECISIONS D64).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from ..config.schema import ObliskConfig
from ..config.schema_universe import UniverseInstrument
from ..engines.core.engine import RoleChoice
from ..quant.tracking import CorrelationReading
from .swap_partners import SwapPartners
from .vehicle_selector import (
    Exposure,
    Selection,
    SelectorInputs,
    VehicleSelector,
    resolve_exposure,
)

#: The prefix of a held vehicle no core role names.
UNLISTED = "unlisted:"


class Correlations(Protocol):
    """Correlations measured on research history, by vehicle symbol."""

    def tracking(
        self, exposure: Exposure, symbols: Sequence[str]
    ) -> Mapping[str, CorrelationReading]:
        """Each vehicle's correlation with the exposure's series."""
        ...

    def pairs(
        self, pairs: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], CorrelationReading]:
        """Each pair of vehicles' correlation."""
        ...


@dataclass(frozen=True, slots=True)
class Unmeasured:
    """No research history to measure on: every correlation is unmeasured, with why."""

    reason: str

    def tracking(
        self, exposure: Exposure, symbols: Sequence[str]
    ) -> Mapping[str, CorrelationReading]:
        del exposure
        return {symbol: CorrelationReading.unmeasured(self.reason) for symbol in symbols}

    def pairs(
        self, pairs: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], CorrelationReading]:
        return {pair: CorrelationReading.unmeasured(self.reason) for pair in pairs}


@dataclass(frozen=True, slots=True)
class MeasuredCorrelations:
    """Correlations read once, by exposure key and symbol, and by pair of symbols; anything not
    read is unmeasured, with ``missing``."""

    by_exposure: Mapping[str, Mapping[str, CorrelationReading]]
    by_pair: Mapping[tuple[str, str], CorrelationReading]
    missing: str

    def tracking(
        self, exposure: Exposure, symbols: Sequence[str]
    ) -> Mapping[str, CorrelationReading]:
        found = self.by_exposure.get(exposure.key, {})
        unread = CorrelationReading.unmeasured(self.missing)
        return {symbol: found.get(symbol, unread) for symbol in symbols}

    def pairs(
        self, pairs: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], CorrelationReading]:
        unread = CorrelationReading.unmeasured(self.missing)
        return {pair: self.by_pair.get(pair, unread) for pair in pairs}


class CorrelationSource(Protocol):
    """Where the rebalancer gets its correlations each survey (the service reads them from
    research history, once a day)."""

    async def read(self, today: date) -> Correlations: ...


@dataclass(frozen=True, slots=True)
class CoreVehicles:
    """Every role's choice, and the selections behind them (for the plan's report)."""

    choices: dict[str, RoleChoice]
    selections: dict[str, Selection] = field(default_factory=dict)


class CoreVehicleChooser:
    """Chooses each core role's vehicles through the vehicle selector and the swap-partner rule."""

    def __init__(
        self, settings: ObliskConfig, selector: VehicleSelector, partners: SwapPartners
    ) -> None:
        self._settings = settings
        self._selector = selector
        self._partners = partners

    @property
    def selector(self) -> VehicleSelector:
        return self._selector

    def role_of(self, declared: UniverseInstrument, roles: Sequence[str]) -> str | None:
        """The role a held instrument counts toward: the first of ``roles`` it declares, else the
        first core role it declares."""
        declared_roles = declared.exposures.core_roles
        return next((r for r in roles if r in declared_roles), next(iter(declared_roles), None))

    def choose(
        self,
        roles: Sequence[str],
        held: Mapping[int, Decimal],
        inputs: SelectorInputs,
        correlations: Correlations,
        *,
        partners: bool,
        refused: Mapping[int, str],
    ) -> CoreVehicles:
        """The vehicles of ``roles`` and of every role ``held`` (conid to value) holds;
        ``partners`` chooses swap partners; ``refused`` vehicles (IBKR did not confirm them) are
        never bought."""
        universe = self._settings.universe
        by_role: dict[str, list[int]] = {}
        unlisted: list[int] = []
        for conid in sorted(held, key=lambda c: (-held[c], c)):
            declared = universe.pinned(conid)
            role = None if declared is None else self.role_of(declared, roles)
            if role is None:
                unlisted.append(conid)
            else:
                by_role.setdefault(role, []).append(conid)
        ordered = list(dict.fromkeys([*roles, *by_role]))
        out = CoreVehicles({})
        for role in ordered:
            exposure = resolve_exposure(self._settings, role)
            symbols = [i.symbol for i in self._selector.candidates(exposure)]
            role_inputs = SelectorInputs(
                as_of=inputs.as_of,
                rates=inputs.rates,
                tracking=correlations.tracking(exposure, symbols),
                windows=inputs.windows,
                market=inputs.market,
            )
            selection = self._selector.select(exposure, role_inputs)
            out.selections[role] = selection
            out.choices[role] = self._choice(
                role, selection, by_role.get(role, []), correlations, partners, refused
            )
        for conid in unlisted:
            name = f"{UNLISTED}{conid}"
            out.choices[name] = RoleChoice(
                role=name,
                primary=None,
                partner=None,
                held=(conid,),
                closed=frozenset({conid}),
                reason=f"conid {conid} fills no core role: it has no target and is sold",
            )
        return out

    def _choice(
        self,
        role: str,
        selection: Selection,
        held: Sequence[int],
        correlations: Correlations,
        partners: bool,
        refused: Mapping[int, str],
    ) -> RoleChoice:
        proposable = [v for v in selection.ranked if v.conid not in refused]
        open_conids = {v.conid for v in proposable}
        closed = frozenset(conid for conid in held if conid not in open_conids)
        reasons: list[str] = []
        primary = proposable[0] if proposable else None
        if primary is None:
            unconfirmed = [
                f"{v.symbol} ({refused[v.conid]})" for v in selection.ranked if v.conid in refused
            ]
            first = selection.rejected[0] if selection.rejected else None
            why = (
                f"IBKR did not confirm {', '.join(unconfirmed)}"
                if unconfirmed
                else "it has no candidate"
                if first is None
                else f"none of its {len(selection.rejected)} candidates passes the gates (first, "
                f"{first.symbol} [{first.gate.value}]: {first.reason})"
            )
            reasons.append(f"no vehicle may be bought: {why}")
        else:
            reasons.append(f"primary {primary.symbol} (score {primary.score:.3f})")
        partner: int | None = None
        if partners and primary is not None:
            pairs = [(primary.symbol, v.symbol) for v in selection.ranked if v is not primary]
            group = self._partners.group(selection, primary.symbol, correlations.pairs(pairs))
            conids = {v.symbol: v.conid for v in selection.ranked}
            found = next((p for p in group.partners if conids[p.symbol] not in refused), None)
            if found is None:
                refusal = group.refused[0] if group.refused else None
                reasons.append(
                    "no swap partner"
                    + ("" if refusal is None else f" ({refusal.symbol}: {refusal.reason})")
                )
            else:
                partner = conids[found.symbol]
                reasons.append(f"swap partner {found.symbol}")
        others = [
            conid
            for conid in held
            if conid not in {partner} and (primary is None or conid != primary.conid)
        ]
        if closed:
            reasons.append(
                "held and no longer bought: " + ", ".join(str(conid) for conid in sorted(closed))
            )
        return RoleChoice(
            role=role,
            primary=None if primary is None else primary.conid,
            partner=partner,
            held=tuple(others),
            closed=closed,
            reason="; ".join(reasons),
        )
