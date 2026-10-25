"""The live core and the tested core cannot drift apart: on one fixture history the core engine and
the research simulator's strategy name the same target weights at every month end, and the
engine's trades are the trades the simulator made (DECISIONS D61, D140).

The research side is the pre-registration's own constants through the harness's builders
(``research.strategies``); the engine side is the configuration through ``engines/core`` and the
month-end closes read from the raw prices by the account's own function.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
import random
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sim_rig import es_policy, flat_costs, instrument, series, settings
from toy_book import month_ends

from obliskiq.config import ObliskConfig
from obliskiq.domain.portfolio import TargetPortfolio
from obliskiq.engines.core.closes import month_end_closes
from obliskiq.engines.core.engine import CoreEngine
from obliskiq.engines.core.mechanism import Bands, CoreStrategy, RoleVehicles, TaxAware
from obliskiq.engines.core.view import MonthEnds
from obliskiq.research.prereg import load_prereg
from obliskiq.research.simulate import Book, MarketView, Panel, Simulator
from obliskiq.research.strategies import static_weights, tilt_rule

D = Decimal
#: Forty months of history ending in September 2026; the runs start once a tilt can be read.
MONTHS = month_ends(date(2026, 9, 30), 40)
FIRST_RUN = 14
HISTORY = 12


@dataclass
class Trace:
    """What both sides said at each date, and what the engine would trade."""

    dates: list[date] = field(default_factory=list)
    research: list[dict[str, Decimal] | None] = field(default_factory=list)
    live: list[dict[str, Decimal] | None] = field(default_factory=list)
    portfolios: list[TargetPortfolio] = field(default_factory=list)
    predicted: list[dict[str, Decimal]] = field(default_factory=list)


class Both:
    """Asks the research strategy and the engine the same question; follows the research's answer
    so that the simulator, whatever the engine says, shows what the harness did."""

    name = "both"

    def __init__(
        self,
        research: CoreStrategy,
        engine: CoreEngine,
        variant: str,
        vehicles: dict[str, RoleVehicles],
        daily: dict[str, list[tuple[date, float]]],
        band: Decimal,
    ) -> None:
        self.research = research
        self.engine = engine
        self.variant = variant
        self.vehicles = vehicles
        self.daily = daily
        self.band = band
        self.trace = Trace()

    def targets(self, view: MarketView, book: Book) -> Any:
        theirs = self.research.targets(view, book)
        closes = {
            key: month_end_closes(
                points, lambda _day, value: value, through=view.as_of, max_age_days=7, name=key
            )
            for key, points in self.daily.items()
        }
        portfolio = self.engine.portfolio(
            vehicles=self.vehicles,
            book=book,
            view=MonthEnds(view.as_of, closes),
            as_of=view.as_of,
            variant=self.variant,
        )
        ours = {v.key: v.weight for v in portfolio.vehicles} if portfolio.rebalance else None
        units = dict(book.units)
        if portfolio.rebalance:
            deltas = self.engine.deltas(portfolio, book, band=self.band)
            for key, count in deltas.sells:
                units[key] = units.get(key, D(0)) - count
            for key, count in deltas.buys:
                units[key] = units.get(key, D(0)) + count
        self.trace.dates.append(view.as_of)
        self.trace.research.append(None if theirs is None else dict(theirs))
        self.trace.live.append(ours)
        self.trace.portfolios.append(portfolio)
        self.trace.predicted.append({k: v for k, v in units.items() if v > 0})
        return theirs


def history(role: str, rng: random.Random) -> list[float]:
    """A month-end path: a drift, noise, and for risky roles a long fall in the middle."""
    drift = {"cash": 0.001, "euro_govt_intermediate": 0.002}.get(role, 0.006)
    level, out = 100.0, []
    for k in range(len(MONTHS)):
        shock = rng.gauss(0, 0.02)
        if role in {"world_equity", "em_equity", "broad_commodities"} and 18 <= k < 30:
            shock -= 0.045
        level *= math.exp(drift + shock)
        out.append(level)
    return out


@pytest.mark.parametrize("name", ["A", "B", "C", "D50"])
def test_the_engine_and_the_simulator_name_the_same_weights_and_trades(
    name: str, shipped_settings: ObliskConfig, repo_root: Path
) -> None:
    study = load_prereg(
        repo_root / "research" / "preregistrations" / "core-allocation.md"
    ).spec.study
    code_of = {
        role: config.name.rsplit("(", 1)[1].rstrip(")")
        for role, config in shipped_settings.core.roles.items()
    }
    role_of = {code: role for role, code in code_of.items()}
    spec = next(s for s in study.candidates if s.name == name)
    core = study.core
    assert core is not None

    rng = random.Random(20261025)
    roles = list(shipped_settings.core.roles)
    daily = {role: list(zip(MONTHS, history(role, rng), strict=True)) for role in roles}
    primaries = {role: f"{role}" for role in roles}
    vehicles = {role: RoleVehicles(role, f"{role}_swap") for role in roles}
    panel_prices = {}
    instruments = []
    for role in roles:
        rows = series(daily[role])  # a role's two vehicles share one series
        panel_prices[role] = rows
        panel_prices[f"{role}_swap"] = rows
        instruments += [instrument(role, f"I_{role}"), instrument(f"{role}_swap", f"J_{role}")]
    daily = {
        key: points
        for role in roles
        for key, points in ((role, daily[role]), (f"{role}_swap", daily[role]))
    }
    assert set(primaries) == set(roles)

    weights = {role_of[code]: w for code, w in static_weights(spec, core, shipped_settings).items()}
    aware = spec.tilt
    research = CoreStrategy(
        name=name,
        weights=weights,
        vehicles={r: RoleVehicles(r, f"{r}_swap" if aware else None) for r in roles},
        bands=Bands(core.bands.absolute, core.bands.relative),
        cash_role="cash",
        tilt=tilt_rule(core) if aware else None,
        tax_aware=TaxAware(core.tax_aware.benefit_fraction) if aware else None,  # type: ignore[union-attr]
    )
    both = Both(
        research,
        CoreEngine(shipped_settings),
        name,
        vehicles,
        daily,
        D(shipped_settings.research.simulation.no_trade_band_fraction),
    )
    simulator = Simulator(
        instruments,
        Panel(prices=panel_prices, income={}, fx={}),
        flat_costs(),
        es_policy(repo_root),
        settings("5000000"),
    )
    dates = MONTHS[FIRST_RUN:]
    result = simulator.run(both, dates)
    trace = both.trace

    assert len(trace.dates) == len(dates) - 1  # every date but the horizon asks
    assert trace.live == trace.research  # the same weights, or the same decision to keep
    assert any(w is not None for w in trace.research[1:]), "nothing rebalanced after the entry"
    if aware:
        scales = {r.scale for p in trace.portfolios for r in p.roles if r.role == "world_equity"}
        assert len(scales) > 1, "the fixture never moved the tilt"

    # The simulator scales what it buys when the cash does not cover the list (a role inside the
    # no-trade band keeps units above its target), a limit the rebalancer meets with its own cash
    # rules. So every sale is the engine's exactly and no purchase is more than the engine's, and
    # on most dates the two agree to the unit.
    paid = {p.paid_on for p in result.taxes}
    checked = exact = 0
    before: dict[str, Decimal] = {}
    for index, holding in enumerate(result.holdings):
        got = {key: D(repr(units)) for key, units in holding.units.items()}
        if trace.dates[index] not in paid:
            wanted = trace.predicted[index]
            for key in set(got) | set(wanted) | set(before):
                moved = got.get(key, D(0)) - before.get(key, D(0))
                planned = wanted.get(key, D(0)) - before.get(key, D(0))
                if planned < 0:
                    assert moved == planned, (trace.dates[index], key)
                else:
                    assert 0 <= moved <= planned, (trace.dates[index], key)
            checked += 1
            exact += got == wanted
        before = got
    assert checked >= len(result.holdings) - 2
    assert exact >= checked // 2
