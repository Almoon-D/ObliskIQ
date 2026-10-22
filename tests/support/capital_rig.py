"""Capital, cash flows and tax over the order rig: the capital reader, the live tax book, owner
questions, the intent maker and the reservation planner, wired as ``orchestration/parts.py`` wires
them over an order path.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from pathlib import Path
from typing import Any

from obliskiq.capital.managed import CapitalReader
from obliskiq.capital.planner import ReservationPlanner
from obliskiq.capital.questions import OwnerQuestions
from obliskiq.capital.strategy import DefaultStrategyValue, StrategyValue
from obliskiq.capital.valuation import Valuer
from obliskiq.config import ObliskConfig
from obliskiq.decision.rebalance_basic import BasicRebalancer
from obliskiq.execution.instruments import InstrumentDirectory, UniverseDirectory
from obliskiq.execution.intent_maker import IntentMaker
from obliskiq.execution.pricing import Pricer
from obliskiq.jurisdiction import TAX_POLICIES, load_profile
from obliskiq.orchestration.parts import CapitalParts
from obliskiq.quant.costs import CostBook
from obliskiq.tax.book import TaxBook

REPO = Path(__file__).resolve().parents[2]


def capital_parts(
    rig: Any,
    settings: ObliskConfig | None = None,
    *,
    directory: InstrumentDirectory | None = None,
    strategy: StrategyValue | None = None,
    jurisdiction: str = "es",
    config_dir: Path | None = None,
) -> CapitalParts:
    chosen = settings if settings is not None else rig.settings
    described = directory if directory is not None else UniverseDirectory.from_settings(chosen)
    valuer = Valuer(chosen, reader=rig.reader, directory=described)
    capital = CapitalReader(
        chosen,
        reader=rig.reader,
        directory=described,
        worker=rig.worker,
        valuer=valuer,
        now=rig.clock,
    )
    profile = load_profile(config_dir or REPO / "config", jurisdiction)
    tax = TaxBook(
        chosen,
        TAX_POLICIES.resolve(profile, chosen),
        worker=rig.worker,
        directory=described,
        now=rig.clock,
    )
    maker = IntentMaker(
        chosen,
        reader=rig.reader,
        contracts=rig.ibkr.contracts,
        directory=described,
        calendars=rig.calendars,
        book=rig.book,
        pricer=Pricer(chosen),
        now=rig.clock,
    )
    questions = OwnerQuestions(rig.worker, now=rig.clock)
    planner = ReservationPlanner(
        chosen,
        capital=capital,
        tax=tax,
        costs=CostBook.from_config(chosen),
        calendars=rig.calendars,
        strategy=strategy or DefaultStrategyValue(chosen.capital),
        worker=rig.worker,
        maker=maker,
        questions=questions,
        kill=rig.kill,
        now=rig.clock,
    )
    return CapitalParts(capital, tax, questions, planner, maker)


def make_rebalancer(rig: Any, settings: ObliskConfig | None = None, **kw: Any) -> BasicRebalancer:
    chosen = settings if settings is not None else rig.settings
    directory = kw.pop("directory", UniverseDirectory.from_settings(chosen))
    parts = capital_parts(rig, chosen, directory=directory)
    return BasicRebalancer(
        chosen,
        capital=parts.capital,
        directory=directory,
        calendars=rig.calendars,
        worker=rig.worker,
        maker=parts.maker,
        costs=CostBook.from_config(chosen),
        kill=rig.kill,
        tax=kw.pop("tax", parts.tax),
        now=rig.clock,
    )
