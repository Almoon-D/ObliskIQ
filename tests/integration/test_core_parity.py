"""Parity: on a fixture history the live core engine and the research simulator make the same
target weights and the same trades.

The research side is Session 4c's registered strategy itself: ``research.strategies.
build_strategy`` on the locked pre-registration (``research/preregistrations/core-allocation.md``),
run by ``research.simulate.Simulator`` over a panel of month-end closes, before tax (the tax
answers a tax-aware reduction asks for are still the lot engine's). The live side, at every
rebalance date, reads the same closes from the market store (``engines.core.history``), sees the
simulator's book as the live book (units, prices, the budget and the cash, the lots' answers),
and plans with ``engines.core.engine.plan_portfolio`` under the live minimum trade. Both cost
every leg with the same cost book. The chosen variant (A) is checked, and B, the tilt with
tax-aware execution and swap partners, beside it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from core_rig import seed_closes, seed_vehicle

from obliskiq.config import ObliskConfig
from obliskiq.decision.rebalance_core import minimum_trade
from obliskiq.domain.core import TargetPortfolio, Trigger
from obliskiq.domain.enums import AssetClass
from obliskiq.domain.orders import OrderSide
from obliskiq.domain.research import Observation
from obliskiq.domain.tax import SecurityIdentity, SecurityKind
from obliskiq.engines.core.engine import BookState, RoleChoice, Sizing, plan_portfolio
from obliskiq.engines.core.history import HistoryView, VehicleHistory, month_end, read_month_ends
from obliskiq.engines.core.variants import build_variant
from obliskiq.jurisdiction import TAX_POLICIES, load_profile
from obliskiq.quant.costs import CostBook
from obliskiq.research.prereg import StudySpec, load_prereg
from obliskiq.research.simulate import (
    Book,
    MarketView,
    Panel,
    SimInstrument,
    SimulationSettings,
    Simulator,
)
from obliskiq.research.strategies import CoreStrategy, build_strategy
from obliskiq.store import Store

D = Decimal
REPO = Path(__file__).resolve().parents[2]
PREREG = REPO / "research" / "preregistrations" / "core-allocation.md"
#: Each pre-registered role code's conid on the live side, and its swap partner's.
CONIDS = {
    "WORLD": 11, "EM": 12, "COMMOD": 13, "EGB_INT": 14, "EGB_LONG": 15, "UST": 16, "IL": 17,
    "GOLD": 18, "CASH": 19,
}  # fmt: skip
PARTNER = 10
#: Yearly volatility and drift of each role's fixture path, and the months equities fall hard.
PATHS = {
    "WORLD": (0.15, 0.07), "EM": (0.20, 0.06), "COMMOD": (0.17, 0.03), "EGB_INT": (0.04, 0.02),
    "EGB_LONG": (0.12, 0.02), "UST": (0.10, 0.02), "IL": (0.10, 0.02), "GOLD": (0.16, 0.04),
    "CASH": (0.001, 0.02),
}  # fmt: skip
CRASH = {date(2025, 3, 31): -0.12, date(2025, 4, 30): -0.10, date(2025, 5, 31): -0.06}
#: A rally that leaves the equity lots with large gains before the crash: a trend reduction then
#: costs more tax than its benefit and is held.
RALLY = 0.05
FIRST, START, END = date(2023, 1, 31), date(2024, 2, 29), date(2026, 9, 30)


def months(first: date, last: date) -> list[date]:
    out = [first]
    while out[-1] < last:
        year, month = divmod(out[-1].year * 12 + out[-1].month, 12)
        out.append(month_end(year, month + 1))
    return out


def keys(study: StudySpec) -> dict[str, int]:
    """Each research instrument key's conid: a role's primary its code's, its partner ten more."""
    assert study.roles is not None
    out: dict[str, int] = {}
    for code, vehicles in study.roles.items():
        out[vehicles.primary] = CONIDS[code]
        if vehicles.partner is not None:
            out[vehicles.partner] = CONIDS[code] + PARTNER
    return out


def role_ids(study: StudySpec, settings: ObliskConfig) -> dict[str, str]:
    """Each role code as the live role id (the role priced on the code's series)."""
    assert study.roles is not None
    series = {i.key: i.price_series for i in study.instruments}
    by_series = {role.exposure_series: name for name, role in settings.core.roles.items()}
    return {code: by_series[series[v.primary]] for code, v in study.roles.items()}


def closes(study: StudySpec, seed: int, *, rally: bool) -> dict[str, list[tuple[date, float]]]:
    """Month-end closes for every instrument key, a role's two vehicles on one path; with
    ``rally``, equities gain :data:`RALLY` more a month from the start until the crash."""
    assert study.roles is not None
    rng = np.random.default_rng(seed)
    days = months(FIRST, END)
    out: dict[str, list[tuple[date, float]]] = {}
    for code, vehicles in study.roles.items():
        vol, drift = PATHS[code]
        shocks = rng.normal(drift / 12, vol / np.sqrt(12), size=len(days))
        level, path = 100.0, []
        for day, shock in zip(days, shocks, strict=True):
            equity = code in ("WORLD", "EM")
            hit = CRASH.get(day, 0.0) if equity else 0.0
            boost = RALLY if rally and equity and START <= day < min(CRASH) else 0.0
            level *= float(np.exp(shock)) * (1 + hit) * (1 + boost)
            path.append((day, round(level, 4)))
        for key in (vehicles.primary, vehicles.partner):
            if key is not None:
                out[key] = path
    return out


def instruments(study: StudySpec) -> list[SimInstrument]:
    return [
        SimInstrument(
            key=key,
            venue="XETR",
            asset_class=AssetClass.ETF,
            currency="EUR",
            security=SecurityIdentity(
                key=key,
                kind=SecurityKind.ETF,
                isin=None,
                issuer=f"issuer of {key}",
                tracked_index=f"index of {key}",
                listed=True,
            ),
            issuer_country=None,
            market_cap_usd=None,
            income_country=None,
        )
        for key in keys(study)
    ]


@dataclass
class Lots:
    """The research book's tax answers, by conid."""

    book: Book
    key_of: Mapping[int, str]

    def complete(self, conid: int, units: Decimal) -> bool:
        return True

    def basis(self, conid: int) -> Decimal:
        return self.book.basis(self.key_of[conid])

    def trial_tax(self, sales: Sequence[tuple[int, Decimal, Decimal]], *, on: date) -> Decimal:
        return self.book.sales_tax([(self.key_of[c], q) for c, q, _ in sales])

    def purchase_defers_loss(self, conid: int, on: date) -> bool:
        return self.book.purchase_defers_loss(self.key_of[conid])


@dataclass
class Probe:
    """Runs the registered strategy and, on the same view and book, the live engine."""

    strategy: CoreStrategy
    live: Any
    seen: list[tuple[date, dict[str, Decimal], Mapping[str, Decimal] | None, TargetPortfolio]] = (
        field(default_factory=list)
    )

    @property
    def name(self) -> str:
        return self.strategy.name

    def targets(self, view: MarketView, book: Book) -> Mapping[str, Decimal] | None:
        portfolio = self.live(view.as_of, book)
        found = self.strategy.targets(view, book)
        self.seen.append((view.as_of, dict(book.units), found, portfolio))
        return found


@pytest.fixture(scope="module")
def study() -> StudySpec:
    return load_prereg(PREREG).spec.study


@pytest.mark.parametrize(
    ("name", "seed", "rally"),
    [
        ("A", 20261025, False),
        ("A", 7, True),
        ("B", 20261025, False),
        ("B", 7, False),
        ("B", 20261025, True),
    ],
)
def test_the_live_engine_makes_the_simulators_targets_and_trades(
    name: str,
    seed: int,
    rally: bool,
    study: StudySpec,
    shipped_settings: ObliskConfig,
    store: Store,
) -> None:
    settings = shipped_settings
    conid_of = keys(study)
    key_of = {conid: key for key, conid in conid_of.items()}
    role_of = role_ids(study, settings)
    paths = closes(study, seed, rally=rally)
    for key, conid in conid_of.items():
        seed_vehicle(store, conid, key)
        seed_closes(store, conid, dict(paths[key]))
    panel = Panel(
        prices={
            key: tuple(Observation(observed_on=d, available_on=d, value=v) for d, v in path)
            for key, path in paths.items()
        },
        income={},
        fx={},
    )
    simulation = settings.research.simulation
    sim = Simulator(
        instruments(study),
        panel,
        CostBook.from_config(settings),
        TAX_POLICIES.resolve(load_profile(REPO / "config", "es"), settings),
        SimulationSettings(
            base_currency="EUR",
            initial_capital=D(100000),
            no_trade_band=simulation.no_trade_band_fraction,
            max_price_age_days=simulation.max_price_age_days,
            max_fx_age_days=settings.research.data.max_fx_age_days,
            whole_units=simulation.whole_units,
            days_per_year=float(simulation.days_per_year),
            cost_multiplier=D(1),
            taxed=False,
        ),
    )
    registered = next(spec for spec in study.candidates if spec.name == name)
    strategy = build_strategy(registered, study, settings)
    assert isinstance(strategy, CoreStrategy)
    variant = build_variant(settings, name)
    assert {role_of[r]: w for r, w in strategy.weights.items()} == variant.weights
    assert study.roles is not None
    choices = {
        role_of[code]: RoleChoice(
            role=role_of[code],
            primary=conid_of[vehicles.primary],
            partner=(
                conid_of[vehicles.partner]
                if variant.tilts and vehicles.partner is not None
                else None
            ),
            held=(),
            closed=frozenset(),
            reason="the fixture's vehicles",
        )
        for code, vehicles in study.roles.items()
        if role_of[code] in variant.roles
    }
    histories = {str(c): VehicleHistory(c, "EUR") for c in conid_of.values()}
    least = minimum_trade(settings.trading, D(100000))

    def live(on: date, book: Book) -> TargetPortfolio:
        values = {key: book.value(key) for key in book.units}
        state = BookState(
            units={conid_of[k]: q for k, q in book.units.items()},
            prices={conid: book.price(key) for key, conid in conid_of.items()},
            budget=book.investable,
            cash=book.investable - sum(values.values(), D(0)),
            on=on,
        )
        sizing = Sizing(
            no_trade_band=simulation.no_trade_band_fraction,
            minimum=least,
            buy_cost=lambda c, q: sim._buy_cost(key_of[c], q, on),
            sell_cost=lambda c, q: sim._quote(key_of[c], "sell", q, on)[0],
        )
        history = HistoryView(read_month_ends(store, settings, histories, as_of=on), on)
        return plan_portfolio(variant, choices, history, state, Lots(book, key_of), sizing)

    probe = Probe(strategy, live)
    dates = months(START, END)
    result = sim.run(probe, dates)
    assert len(probe.seen) == len(dates) - 1
    triggered = 0
    for index, (on, before, research, portfolio) in enumerate(probe.seen):
        after = {k: D(repr(q)) for k, q in result.holdings[index].units.items()}
        made = {
            conid_of[k]: after.get(k, D(0)) - before.get(k, D(0))
            for k in set(after) | set(before)
            if after.get(k, D(0)) != before.get(k, D(0))
        }
        engine = {
            t.conid: t.quantity if t.side is OrderSide.BUY else -t.quantity
            for t in portfolio.trades
        }
        assert portfolio.trigger is not Trigger.CASH, on  # no cash beyond the targets arises
        if research is None:
            assert portfolio.trigger is Trigger.HOLD, on
            assert engine == {}, on
        else:
            triggered += 1
            assert portfolio.trigger in (Trigger.START, Trigger.BAND), on
            weights = {v.conid: v.weight for v in portfolio.vehicles if v.weight > 0}
            assert weights == {conid_of[k]: w for k, w in research.items()}, on
        assert engine == made, on
    # What each case compared: a start and a band breach with its sales; with the tilt, scaled
    # roles and purchases through swap partners; after the rally, reductions held for their tax.
    seen = [p for *_, p in probe.seen]
    assert triggered >= 2
    assert any(p.trigger is Trigger.BAND for p in seen)
    assert any(t.side is OrderSide.SELL for p in seen for t in p.trades)
    if variant.tilts:
        assert any(r.scale < 1 for p in seen for r in p.roles)
        assert any(t.conid > max(CONIDS.values()) for p in seen for t in p.trades)
    if variant.tilts and rally:
        assert any(r.reduction_held for p in seen for r in p.roles)
