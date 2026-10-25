"""Capital, cash flows and tax assembled over an order path (MASTER_PLAN §5.15, §5.16).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..capital.core_value import CoreStrategyValue
from ..capital.managed import CapitalReader
from ..capital.planner import ReservationPlanner
from ..capital.questions import OwnerQuestions
from ..capital.strategy import DefaultStrategyValue
from ..capital.valuation import Valuer
from ..config.schema import ObliskConfig
from ..execution.intent_maker import IntentMaker
from ..execution.runtime import OrderPath
from ..jurisdiction import TAX_POLICIES
from ..quant.costs import CostBook
from ..tax.book import TaxBook


@dataclass(frozen=True, slots=True)
class CapitalParts:
    """Capital, cash flows and tax over an order path: the one capital reader, the live tax
    book, the owner questions, the reservation planner, the intent maker they share and the
    strategy value the core notes its target portfolio on."""

    capital: CapitalReader
    tax: TaxBook
    questions: OwnerQuestions
    planner: ReservationPlanner
    maker: IntentMaker
    value: CoreStrategyValue


def build_capital(
    settings: ObliskConfig, path: OrderPath, *, now: Callable[[], datetime]
) -> CapitalParts:
    """The capital machinery over an open order path."""
    valuer = Valuer(settings, reader=path.reader, directory=path.directory)
    capital = CapitalReader(
        settings,
        reader=path.reader,
        directory=path.directory,
        worker=path.worker,
        valuer=valuer,
        now=now,
    )
    tax = TaxBook(
        settings,
        TAX_POLICIES.resolve(path.profile, settings),
        worker=path.worker,
        directory=path.directory,
        now=now,
    )
    maker = IntentMaker(
        settings,
        reader=path.reader,
        contracts=path.client.contracts,
        directory=path.directory,
        calendars=path.calendars,
        book=path.book,
        pricer=path.pricer,
        now=now,
    )
    questions = OwnerQuestions(path.worker, now=now)
    value = CoreStrategyValue(DefaultStrategyValue(settings.capital))
    planner = ReservationPlanner(
        settings,
        capital=capital,
        tax=tax,
        costs=CostBook.from_config(settings),
        calendars=path.calendars,
        strategy=value,
        worker=path.worker,
        maker=maker,
        questions=questions,
        kill=path.kill,
        now=now,
    )
    return CapitalParts(capital, tax, questions, planner, maker, value)
