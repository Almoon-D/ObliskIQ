"""The live tax book: the account's fills become lots under the active policy, checked against
Session 4b's lot engine and by hand; the two-month rule triggered by a live buy after a loss sale,
deferred and later released; currency lots on a conversion and its repatriation; carry-forward
expiry; what the book cannot account for; persistence across a restart; the annual report; and a
jurisdiction switch that swaps the policy with no code change.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import csv
import io
import shutil
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from obliskiq.config import ObliskConfig, load_config
from obliskiq.domain.orders import Fill, OrderSide
from obliskiq.domain.tax import SecurityKind
from obliskiq.errors import TaxError
from obliskiq.execution.account import CashLine, CashRead
from obliskiq.execution.instruments import ConfiguredDirectory
from obliskiq.jurisdiction import TAX_POLICIES, load_profile
from obliskiq.store import fx as fx_repo
from obliskiq.store import lots as lots_repo
from obliskiq.store import open_store
from obliskiq.store import orders as repo
from obliskiq.store.fx import FxRate
from obliskiq.store.worker import StoreWorker
from obliskiq.tax.book import TaxBook, identity_of, security_key
from obliskiq.tax.lots import LotEngine
from obliskiq.tax.report import annual_csv, year_bounds

D = Decimal
#: The skeleton's euro bond fund on Amsterdam, its world fund on London in dollars, the pair.
IBGM, IWDA, EXV1, EURUSD = 43614383, 78999785, 89005163, 12087792


class Clock:
    def __init__(self, at: datetime) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def worker(shipped_settings: ObliskConfig, tmp_path: Path) -> AsyncIterator[StoreWorker]:
    made = StoreWorker(lambda: open_store(shipped_settings, tmp_path))
    yield made
    await made.aclose()


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 12, 1, 12, 0, tzinfo=UTC))


def book_of(
    settings: ObliskConfig, worker: StoreWorker, clock: Callable[[], datetime], jurisdiction: str = "es",
    config_dir: Path | None = None,
) -> TaxBook:  # fmt: skip
    root = config_dir or Path(__file__).resolve().parents[2] / "config"
    policy = TAX_POLICIES.resolve(load_profile(root, jurisdiction), settings)
    return TaxBook(
        settings,
        policy,
        worker=worker,
        directory=ConfiguredDirectory.from_settings(settings),
        now=clock,
    )


_counter = iter(range(1, 10_000))


def fill(
    conid: int, side: OrderSide, quantity: str, price: str, on: date, commission: str = "0"
) -> Fill:
    return Fill(
        execution_id=f"x{next(_counter)}",
        coid=None,
        conid=conid,
        side=side,
        quantity=D(quantity),
        price=D(price),
        commission=D(commission),
        executed_at=datetime(on.year, on.month, on.day, 10, 0, tzinfo=UTC),
    )


async def record(worker: StoreWorker, *fills: Fill) -> None:
    await worker.run(repo.upsert_fills, list(fills))


BUY, SELL = OrderSide.BUY, OrderSide.SELL


# ── fills open and close lots ──


async def test_fills_open_and_close_lots_with_fees_as_the_engine_does(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock
) -> None:
    book = book_of(shipped_settings, worker, clock)
    first = fill(IBGM, BUY, "10", "100", date(2026, 3, 2), "2")
    second = fill(IBGM, BUY, "5", "110", date(2026, 4, 1), "1")
    sale = fill(IBGM, SELL, "12", "120", date(2026, 9, 1), "3")
    await record(worker, first, second, sale)
    outcome = await book.catch_up()
    assert outcome.applied == [first.execution_id, second.execution_id, sale.execution_id]
    assert outcome.incomplete == [] and outcome.waiting is None
    # The same events on Session 4b's engine directly.
    engine = LotEngine(book.policy, [identity_of(IBGM, book._directory, book._facts)])  # noqa: SLF001
    key = security_key(IBGM)
    engine.acquire(key, on=date(2026, 3, 2), quantity=D(10), cost=D(1002))
    engine.acquire(key, on=date(2026, 4, 1), quantity=D(5), cost=D(551))
    expected = engine.dispose(key, on=date(2026, 9, 1), quantity=D(12), proceeds=D(1437))
    live = await book.engine()
    assert live.lots() == engine.lots()
    (left,) = live.lots()
    assert (left.quantity, left.cost) == (D(3), D("330.6"))  # the partial lot: 3 of 5 at 551
    realised = await worker.run(
        lots_repo.realisations_between, since=date(2026, 1, 1), until=date(2026, 12, 31)
    )
    assert realised == list(expected)
    assert sum(r.counted for r in realised) == D(1437) - D(1002) - D("220.4")
    # Nothing is taken twice.
    again = await book.catch_up()
    assert again.applied == [] and again.incomplete == []


async def test_a_live_buy_after_a_loss_sale_defers_the_loss_until_those_units_are_sold(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock
) -> None:
    book = book_of(shipped_settings, worker, clock)
    await record(
        worker,
        fill(IBGM, BUY, "10", "100", date(2026, 1, 5)),
        fill(IBGM, SELL, "10", "80", date(2026, 3, 2)),  # a loss of 200
    )
    await book.catch_up()
    hooks = await book.hooks()
    assert hooks.provisional(2026).gains == D(-200)
    assert hooks.purchase_defers_loss(IBGM, date(2026, 3, 20))  # inside the two months after
    assert not hooks.purchase_defers_loss(IBGM, date(2026, 5, 3))
    assert hooks.purchase_defers_loss(EXV1, date(2026, 3, 20)) is False  # not homogeneous
    # The rebuy inside the window takes the loss: it leaves the year.
    await record(worker, fill(IBGM, BUY, "10", "85", date(2026, 3, 20)))
    await book.catch_up()
    engine = await book.engine()
    (lot,) = engine.lots()
    assert (lot.deferred_loss, lot.replacement_units) == (D(200), D(10))
    assert engine.ledger.provisional(2026).gains == 0
    # Its sale after the window releases the loss with its own result: 900 - 850 - 200.
    await record(worker, fill(IBGM, SELL, "10", "90", date(2026, 6, 1)))
    await book.catch_up()
    assert (await book.engine()).ledger.provisional(2026).gains == D(-150)


async def test_a_conversion_and_its_repatriation_keep_currency_lots(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock
) -> None:
    book = book_of(shipped_settings, worker, clock)
    assert book.policy.currency_lots
    out = fill(EURUSD, SELL, "100", "1.10", date(2026, 5, 4), "2")  # 100 EUR for 110 USD
    back = fill(EURUSD, BUY, "100", "1.05", date(2026, 7, 1), "2")  # 105 USD for 100 EUR
    await record(worker, out)
    await book.catch_up()
    (dollars,) = (await book.engine()).lots()
    assert dollars.security == "CCY:USD"
    assert (dollars.quantity, dollars.cost) == (D(110), D(102))  # 100 EUR and its commission
    await record(worker, back)
    await book.catch_up()
    (left,) = (await book.engine()).lots()
    assert left.quantity == D(5)
    (gain,) = await worker.run(
        lots_repo.realisations_between, since=date(2026, 7, 1), until=date(2026, 7, 1)
    )
    assert gain.security == "CCY:USD"
    assert gain.quantity == D(105)
    assert gain.proceeds == D(98)  # 100 EUR received less the commission
    assert gain.cost == D(102) * D(105) / D(110)


async def test_a_dollar_buy_pays_out_currency_lots_at_the_trade_dates_rate(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock
) -> None:
    book = book_of(shipped_settings, worker, clock)
    day = date(2026, 5, 4)
    await worker.run(
        fx_repo.record_rates,
        [FxRate("USD", day, D("0.9"), "test", datetime(2026, 5, 4, 9, tzinfo=UTC))],
    )
    await record(
        worker,
        fill(EURUSD, SELL, "100", "1.10", day),  # 110 USD at 100 EUR
        fill(IWDA, BUY, "1", "100", day, "1"),  # 101 USD paid at 0.9: 90.90 EUR
    )
    outcome = await book.catch_up()
    assert len(outcome.applied) == 2
    engine = await book.engine()
    (fund,) = engine.lots(security_key(IWDA))
    assert fund.cost == D("90.9")
    (left,) = engine.lots("CCY:USD")
    assert left.quantity == D(9)
    (paid,) = await worker.run(lots_repo.realisations_between, since=day, until=day)
    assert (paid.quantity, paid.proceeds) == (D(101), D("90.9"))
    # The sale brings dollars back in as a new currency lot.
    await worker.run(
        fx_repo.record_rates,
        [FxRate("USD", date(2026, 6, 1), D("0.95"), "test", datetime(2026, 6, 1, tzinfo=UTC))],
    )
    await record(worker, fill(IWDA, SELL, "1", "120", date(2026, 6, 1), "1"))
    await book.catch_up()
    assert (await book.engine()).quantity("CCY:USD") == D(9) + D(119)


async def test_losses_carried_forward_expire_after_the_profiles_years(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock
) -> None:
    book = book_of(shipped_settings, worker, clock)
    await record(
        worker,
        fill(IBGM, BUY, "10", "100", date(2026, 1, 5)),
        fill(IBGM, SELL, "10", "50", date(2026, 6, 1)),  # a loss of 500
    )
    await book.catch_up()
    clock.now = datetime(2026, 7, 15, tzinfo=UTC)
    assert (await book.catch_up()).settled == []  # the year is not over
    clock.now = datetime(2027, 1, 2, tzinfo=UTC)
    assert (await book.catch_up()).settled == [2026]  # over, and its loss window closed
    (carry,) = (await book.engine()).ledger.carried_forward()
    assert (carry.arising_year, carry.amount) == (2026, D(500))
    gain_day = date(2031, 3, 2)
    await record(
        worker,
        fill(IBGM, BUY, "10", "100", date(2031, 1, 5)),
        fill(IBGM, SELL, "10", "200", gain_day),  # a gain of 1,000 five years later
    )
    clock.now = datetime(2031, 6, 1, tzinfo=UTC)
    await book.catch_up()
    clock.now = datetime(2032, 3, 1, tzinfo=UTC)
    await book.catch_up()
    summary = {s.tax_year: s for s in (await book.engine()).ledger.settled()}[2031]
    assert summary.expired == D(500)  # 2026's loss is beyond its four years
    assert summary.base == D(1000)


# ── what the book cannot account for ──


async def test_what_the_book_cannot_account_for_is_recorded_incomplete(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock
) -> None:
    book = book_of(shipped_settings, worker, clock)
    unknown = fill(424242, BUY, "1", "10", date(2026, 3, 2))
    orphan_sale = fill(IBGM, SELL, "5", "100", date(2026, 3, 3))
    no_rate = fill(IWDA, BUY, "1", "100", date(2026, 3, 4))
    await record(worker, unknown, orphan_sale, no_rate)
    outcome = await book.catch_up()
    assert outcome.applied == []
    assert [line.split(":")[0] for line in outcome.incomplete] == [
        unknown.execution_id,
        orphan_sale.execution_id,
        no_rate.execution_id,
    ]
    assert "no instrument" in outcome.incomplete[0]
    assert "units the bot did not buy" in outcome.incomplete[1]
    assert "no USD rate was recorded on 2026-03-04" in outcome.incomplete[2]
    hooks = await book.hooks()
    assert not hooks.complete(IBGM, D(5))  # an incomplete fill: its tax is not answered
    taken = await worker.run(lots_repo.taken_fills)
    assert {t.outcome for t in taken.values()} == {"incomplete"}
    # A fill of today without a rate waits for the day's ledger read; the rate arrives.
    clock.now = datetime(2026, 12, 1, 12, tzinfo=UTC)
    today = fill(IWDA, BUY, "1", "100", clock.now.date())
    await record(worker, today)
    waiting = await book.catch_up()
    assert waiting.waiting is not None and "no USD rate recorded yet today" in waiting.waiting
    read = CashRead(
        lines=(CashLine("EUR", D(1), D(1)), CashLine("USD", D(1), D("0.9")),
               CashLine("GBP", D(1), None)),
        nav=D(1), nav_refusal=None, observed_at=clock.now,
    )  # fmt: skip
    assert await book.note_rates(read) == 1
    assert (await book.catch_up()).applied == [today.execution_id]
    empty = CashRead(lines=(), nav=None, nav_refusal=None, observed_at=clock.now)
    assert await book.note_rates(empty) == 0


async def test_currency_paid_from_a_balance_without_lots_is_noted(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock
) -> None:
    book = book_of(shipped_settings, worker, clock)
    back = fill(EURUSD, BUY, "100", "1.05", date(2026, 7, 1))
    await record(worker, back)
    outcome = await book.catch_up()
    assert outcome.applied == [back.execution_id]
    assert "105 USD paid from a balance with no lots" in outcome.notes[0].replace(".00", "")


async def test_saved_lots_that_no_tax_facts_describe_refuse_to_load(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock
) -> None:
    book = book_of(shipped_settings, worker, clock)
    await record(worker, fill(IBGM, BUY, "1", "100", date(2026, 3, 2)))
    await book.catch_up()
    bare = shipped_settings.model_copy(
        update={
            "eligibility": shipped_settings.eligibility.model_copy(
                update={"skeleton_tax_facts": []}
            )
        }
    )
    with pytest.raises(TaxError, match="no tax facts describe"):
        await book_of(bare, worker, clock).engine()


def test_an_instrument_without_tax_facts_has_no_identity(shipped_settings: ObliskConfig) -> None:
    directory = ConfiguredDirectory.from_settings(shipped_settings)
    facts = {f.conid: f for f in shipped_settings.eligibility.skeleton_tax_facts}
    identity = identity_of(IWDA, directory, facts)
    assert identity is not None and identity.kind is SecurityKind.ETF
    assert identity_of(424242, directory, facts) is None
    wrong = {IWDA: facts[IWDA].model_copy(update={"issuer": None})}
    assert identity_of(IWDA, directory, wrong) is None  # an ETF names its issuer


# ── the annual report ──


async def test_the_annual_report_lists_realised_results_and_deferred_losses(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock
) -> None:
    book = book_of(shipped_settings, worker, clock)
    await record(
        worker,
        fill(IBGM, BUY, "10", "100", date(2026, 1, 5)),
        fill(IBGM, SELL, "10", "80", date(2026, 11, 2)),  # a loss waiting for a buy-back
        fill(EXV1, BUY, "4", "20", date(2026, 2, 2)),
        fill(EXV1, SELL, "2", "15", date(2026, 3, 2)),  # deferred: two units still held
    )
    await book.catch_up()
    engine = await book.engine()
    first, last = year_bounds(2026)
    assert (first, last) == (date(2026, 1, 1), date(2026, 12, 31))
    text = annual_csv(
        2026,
        realisations=await worker.run(lots_repo.realisations_between, since=first, until=last),
        lots=engine.lots(),
        windows=engine.loss_windows(),
        summary=engine.ledger.provisional(2026),
        settled=False,
        identities={str(IBGM): "IE00B1FZS806"},
    )
    rows = list(csv.DictReader(io.StringIO(text)))
    sections = [row["section"] for row in rows]
    assert sections.count("realised") == 2
    assert "deferred_on_lot" in sections and "deferred_pending" in sections
    assert f"IE00B1FZS806 ({IBGM})" in [row["security"] for row in rows]
    assert rows[-1]["section"] == "summary"
    assert "provisional" in rows[-1]["note"] and "not tax advice" in rows[-1]["note"]


# ── a jurisdiction switch swaps the policy, with no code change ──


async def test_switching_the_jurisdiction_swaps_the_policy_without_code(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[2] / "config", config_dir)
    spain = (config_dir / "jurisdictions" / "es.yaml").read_text(encoding="utf-8")
    elsewhere = spain.replace("id: es", "id: xx").replace("name: Spain", "name: Elsewhere")
    elsewhere = elsewhere.replace("  tax: es_irpf", "  tax: generic")
    (config_dir / "jurisdictions" / "xx.yaml").write_text(elsewhere, encoding="utf-8")
    owner = config_dir / "owner.yaml"
    owner.write_text(owner.read_text(encoding="utf-8").replace("jurisdiction: es", "jurisdiction: xx"))
    settings = load_config(config_dir).settings
    assert settings.jurisdiction == "xx"
    book = book_of(settings, worker, clock, settings.jurisdiction, config_dir)
    assert book.policy.policy_id == "generic"
    await record(
        worker,
        fill(IBGM, BUY, "10", "100", date(2026, 1, 5)),
        fill(IBGM, SELL, "10", "80", date(2026, 3, 2)),
        fill(IBGM, BUY, "10", "85", date(2026, 3, 20)),
    )
    await book.catch_up()
    # Without the two-month rule the loss stays in the year; under es_irpf it would leave it.
    assert (await book.engine()).ledger.provisional(2026).gains == D(-200)
    assert (await book.hooks()).purchase_defers_loss(IBGM, date(2026, 3, 21)) is False



async def test_a_year_with_a_loss_window_still_open_waits_to_settle(
    shipped_settings: ObliskConfig, worker: StoreWorker, clock: Clock
) -> None:
    book = book_of(shipped_settings, worker, clock)
    await record(
        worker,
        fill(IBGM, BUY, "10", "100", date(2026, 1, 5)),
        fill(IBGM, SELL, "10", "90", date(2026, 12, 15)),  # its window runs to February
    )
    clock.now = datetime(2027, 1, 10, tzinfo=UTC)
    assert (await book.catch_up()).settled == []
    clock.now = datetime(2027, 2, 16, tzinfo=UTC)
    assert (await book.catch_up()).settled == [2026]
