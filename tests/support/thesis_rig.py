"""Helpers for the thesis book's tests: drafts, theses, gate facts and fills.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from obliskiq.domain.enums import AssetClass
from obliskiq.domain.orders import ExecutionMode, Fill, OrderSide
from obliskiq.domain.thesis import (
    UNSTORED,
    Thesis,
    ThesisDraft,
    ThesisInstrument,
    ThesisSource,
    ThesisState,
)
from obliskiq.engines.thesis.gate import GateFacts

D = Decimal
#: Monday 26 October 2026, 09:00 UTC: Amsterdam and London trade.
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
TODAY = NOW.date()
HORIZON = date(2027, 4, 30)
#: EMIM on Euronext Amsterdam (config/universe.yaml), quoted near 46.42 by the skeleton market.
EMIM_CONID = 153454120
ISIN = "US0378331005"


def instrument(**changes: Any) -> ThesisInstrument:
    fields: dict[str, Any] = {
        "symbol": "EMIM",
        "candidate_key": None,
        "conid": EMIM_CONID,
        "listing_exchange": "AEB",
        "currency": "EUR",
        "asset_class": AssetClass.ETF,
        "description": "ISHARES CORE MSCI EM IMI",
        "sector": None,
        "country": None,
        "defensive": False,
        "isin": None,
    }
    return ThesisInstrument(**{**fields, **changes})


def draft(**changes: Any) -> ThesisDraft:
    fields: dict[str, Any] = {
        "source": ThesisSource.OWNER,
        "symbol": "EMIM",
        "expectation": "emerging markets re-rate as the dollar weakens",
        "target": D(60),
        "invalidation": D(40),
        "horizon": HORIZON,
    }
    return ThesisDraft(**{**fields, **changes})


def thesis(**changes: Any) -> Thesis:
    fields: dict[str, Any] = {
        "thesis_id": UNSTORED,
        "source": ThesisSource.OWNER,
        "conviction": "normal",
        "view": "bullish",
        "instrument": instrument(),
        "expectation": "emerging markets re-rate as the dollar weakens",
        "target": D(60),
        "invalidation": D(40),
        "horizon": HORIZON,
        "entry_max": None,
        "enter_by": date(2026, 11, 15),
        "state": ThesisState.CANDIDATE,
        "created_at": NOW,
    }
    return Thesis(**{**fields, **changes})


def approved(**changes: Any) -> Thesis:
    fields: dict[str, Any] = {
        "thesis_id": 1,
        "state": ThesisState.APPROVED,
        "mode": ExecutionMode.SHADOW,
        "size_base": D(125),
        "held_as": "shares",
        "approved_at": NOW,
    }
    return thesis(**{**fields, **changes})


def facts(**changes: Any) -> GateFacts:
    fields: dict[str, Any] = {
        "capital": D(1000),
        "base_currency": "EUR",
        "prohibited": (),
        "ineligible": None,
        "held_elsewhere": None,
        "reference": D(45),
        "rate": D(1),
        "daily_value": D(5_000_000),
        "sessions": 20,
        "slots": (),
        "minimum": D(50),
    }
    return GateFacts(**{**fields, **changes})


def fill(side: str, quantity: str, price: str, at: datetime = NOW, n: int = 1) -> Fill:
    return Fill(
        execution_id=f"x{n}",
        coid=None,
        conid=EMIM_CONID,
        side=OrderSide(side),
        quantity=D(quantity),
        price=D(price),
        commission=None,
        executed_at=at,
        simulated=True,
    )
