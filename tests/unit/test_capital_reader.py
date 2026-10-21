"""The capital reader over the fake broker: a capped book follows the owner's flows and its own
returns, the whole account manages the NAV less what no sleeve owns, a deposit is deployed in
tranches, and every reading it cannot make stops it with why.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
from capital_rig import capital_parts, make_rebalancer
from fake_broker import FakeBroker
from order_rig import build_rig, terms
from service_rig import EMIM, IBGM, SERVICE_VENUES, skeleton_market

from obliskiq.calendars.service import Calendars
from obliskiq.capital.managed import CapitalView
from obliskiq.config import ObliskConfig
from obliskiq.domain.capital import Deposit, Withdrawal, WithdrawalReservation
from obliskiq.domain.orders import (
    UNMANAGED,
    ExecutionMode,
    Fill,
    IntentState,
    OrderSide,
    Sleeve,
)
from obliskiq.execution.instruments import ConfiguredDirectory
from obliskiq.store import capital as capital_repo
from obliskiq.store import orders as repo

D = Decimal
NOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
PAPER, SHADOW = ExecutionMode.PAPER, ExecutionMode.SHADOW


@pytest.fixture(scope="module")
def calendars(shipped_settings: ObliskConfig) -> Calendars:
    return Calendars.from_settings(
        shipped_settings, reference=date(2026, 10, 9), venues=SERVICE_VENUES
    )


@pytest.fixture
async def rig(
    make_session: Callable[..., Any],
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    tmp_path: Path,
) -> AsyncIterator[Any]:
    with respx.mock(assert_all_called=False) as router:
        broker = FakeBroker(router)
        built = await build_rig(
            settings=shipped_settings,
            session=make_session(),
            broker=broker,
            tmp_path=tmp_path,
            calendars=calendars,
        )
        skeleton_market(broker)
        built.clock.now = NOW
        await built.to_paper()
        yield built
        await built.aclose()


def settings_with(settings: ObliskConfig, **capital: Any) -> ObliskConfig:
    return settings.model_copy(update={"capital": settings.capital.model_copy(update=capital)})


async def read(rig: Any, mode: ExecutionMode = PAPER, **kw: Any) -> CapitalView | str:
    return await capital_parts(rig, **kw).capital.read(mode)


async def bought(rig: Any, conid: int, units: str, price: str, *, mode: ExecutionMode) -> None:
    stored = await rig.intent(
        terms(conid=conid, quantity=units, limit=price), state=IntentState.SUBMITTED, mode=mode
    )
    await rig.store(
        repo.upsert_fills,
        [
            Fill(
                execution_id=f"{conid}-{units}-{mode}",
                coid=stored.coid,
                conid=conid,
                side=OrderSide.BUY,
                quantity=D(units),
                price=D(price),
                commission=D(1),
                executed_at=NOW,
                simulated=mode is SHADOW,
            )
        ],
    )
    await rig.book.advance(stored.intent.intent_id, IntentState.FILLED, "test")
    if mode is not SHADOW:
        rig.broker.hold(conid, units, "EUR")
        await rig.assign(conid, Sleeve.CORE)


def deposit(amount: str, **kw: Any) -> Deposit:
    return Deposit(
        amount=D(amount), currency="EUR", on=NOW.date(), recorded_at=NOW, recorded_by="o", **kw
    )


# ── a capped book ──


async def test_a_capped_book_follows_its_fills_flows_and_returns(rig: Any) -> None:
    await bought(rig, IBGM, "3", "170", mode=PAPER)  # 511 paid with the commission
    view = await read(rig)
    assert isinstance(view, CapitalView)
    assert view.book_cash == D(489)
    assert view.book_value == D("532.50")  # marked at the mid: a gain of 21.50 kept
    assert view.managed == D("1021.50")
    await rig.store(capital_repo.record_flow, deposit("500"))
    await rig.store(
        capital_repo.record_flow,
        Withdrawal(amount=D(100), currency="EUR", on=NOW.date(), recorded_at=NOW, recorded_by="o"),
    )
    after = await read(rig)
    assert isinstance(after, CapitalView)
    assert after.managed == D("1421.50")  # a paper account's cash never moved: the book did
    outside = deposit("50", in_book=False)
    await rig.store(capital_repo.record_flow, outside)
    assert (await read(rig)).managed == D("1421.50")  # type: ignore[union-attr]


async def test_shadow_reads_the_books_simulated_fills_only(rig: Any) -> None:
    await bought(rig, EMIM, "2", "46", mode=SHADOW)
    shadow = await read(rig, SHADOW)
    assert isinstance(shadow, CapitalView)
    assert [(h.conid, h.units) for h in shadow.holdings] == [(EMIM, D(2))]
    assert shadow.book_cash == D(907)  # 1,000 less 92 and the commission
    paper = await read(rig, PAPER)
    assert isinstance(paper, CapitalView)
    assert paper.holdings == ()


async def test_a_deposit_is_deployed_in_tranches(rig: Any) -> None:
    deployment = rig.settings.capital.deployment.model_copy(update={"tranche_days": 4})
    settings = settings_with(rig.settings, deployment=deployment)
    await rig.store(capital_repo.record_flow, deposit("800"))
    plan = await make_rebalancer(rig, settings).survey(PAPER)
    assert plan.capital is not None
    assert plan.capital.managed == D(1800)
    # A quarter of the 800 released today: 1,800 less 600 held back and the floor of 25.
    assert plan.spendable == D(1800) - D(600) - D(25)
    rig.clock.now = NOW + timedelta(days=3)
    later = await make_rebalancer(rig, settings).survey(PAPER)
    assert later.spendable == D(1800) - D(25)


# ── the whole account ──


async def test_the_whole_account_counts_unmanaged_holdings_but_never_invests_them(
    rig: Any,
) -> None:
    whole = settings_with(rig.settings, mode="whole_account", cap_base=None)
    rig.broker.hold(EMIM, "100", "EUR")  # the owner's own: unmanaged
    await rig.assign(EMIM, UNMANAGED)
    view = await read(rig, settings=whole)
    assert isinstance(view, CapitalView)
    assert view.unmanaged == D("4642")  # IBKR's mark
    assert view.investable == D(100000) - D(4642)
    assert view.detail == "the whole account"
    await rig.store(
        capital_repo.record_reservation,
        WithdrawalReservation(
            amount=D(1000), currency="EUR", due_date=NOW.date() + timedelta(days=5),
            reason="r", created_at=NOW, created_by="o", updated_at=NOW,
        ),
    )  # fmt: skip
    reserved = await read(rig, settings=whole)
    assert isinstance(reserved, CapitalView)
    assert reserved.investable == D(100000) - D(4642) - D(1000)


# ── what stops it ──


async def test_every_reading_it_cannot_make_stops_it_with_why(rig: Any) -> None:
    rig.broker.hold(EMIM, "10", "EUR")  # unregistered: unmanaged
    del rig.broker.marks[EMIM]
    assert "carries no market value" in str(await read(rig))
    rig.broker.marks[EMIM] = D("46.42")
    rig.broker.hold(EMIM, "10", "GBP")  # a currency with no rate
    assert "has no rate" in str(await read(rig))
    rig.broker.positions.pop(EMIM)
    await rig.store(
        capital_repo.record_reservation,
        WithdrawalReservation(
            amount=D(10), currency="CHF", due_date=NOW.date(), reason="r", created_at=NOW,
            created_by="o", updated_at=NOW,
        ),
    )  # fmt: skip
    assert "CHF has no rate" in str(await read(rig))


async def test_a_book_fill_nobody_describes_or_foreign_cash_without_a_rate_stops_it(
    rig: Any,
) -> None:
    await bought(rig, IBGM, "1", "170", mode=PAPER)
    narrow = ConfiguredDirectory(
        i for i in rig.settings.eligibility.skeleton_allowlist if i.conid != IBGM
    )
    assert "a held position has no price" in str(await read(rig, directory=narrow))
    rig.broker.positions.pop(IBGM)
    assert "cannot be priced in cash" in str(await read(rig, directory=narrow))
    await rig.store(
        capital_repo.record_flow,
        Deposit(amount=D(5), currency="GBP", on=NOW.date(), recorded_at=NOW, recorded_by="o"),
    )
    assert "cannot be valued" in str(await read(rig))


async def test_an_unreadable_account_or_nav_manages_nothing(rig: Any) -> None:
    rig.broker.unavailable.add("positions")
    assert "the account cannot be read" in str(await read(rig))
    rig.broker.unavailable.clear()
    rig.broker.nav = None
    assert "nothing is managed" in str(await read(rig))
