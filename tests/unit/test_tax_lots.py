"""The lot engine and the tax ledger under the shipped ``es_irpf`` policy, against cases worked
out by hand: FIFO, a loss deferred by a purchase before and after the sale and its release, a
carry-forward, the cross offset, withholding and its credit, and the hooks.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from obliskiq.domain.tax import ProposedTrade, SecurityIdentity, SecurityKind
from obliskiq.errors import TaxError
from obliskiq.jurisdiction import load_profile
from obliskiq.jurisdiction.tax_es_irpf import EsIrpfTaxPolicy
from obliskiq.jurisdiction.tax_generic import GenericTaxPolicy
from obliskiq.jurisdiction.tax_profile import TaxProfile
from obliskiq.store import Store
from obliskiq.store.lots import save_tax_lots, tax_lots
from obliskiq.tax.ledger import TaxLedger
from obliskiq.tax.lots import LotEngine, add_months

D = Decimal

WORLD = SecurityIdentity(
    key="WORLD", kind=SecurityKind.ETF, isin="IE00B4L5Y983", issuer="iShares",
    tracked_index="MSCI World", listed=True,
)  # fmt: skip
WORLD_DIST = SecurityIdentity(
    key="WORLD_DIST", kind=SecurityKind.ETF, isin="IE00B0M62Q58", issuer="iShares",
    tracked_index="MSCI World", listed=True,
)  # fmt: skip
PARTNER = SecurityIdentity(
    key="PARTNER", kind=SecurityKind.ETF, isin="IE00BK5BQT80", issuer="Vanguard",
    tracked_index="FTSE All-World", listed=True,
)  # fmt: skip
US_SHARE = SecurityIdentity(
    key="USCO", kind=SecurityKind.SHARE, isin=None, issuer="US Co", tracked_index=None,
    listed=True,
)  # fmt: skip


@pytest.fixture(scope="module")
def tax_profile(repo_root) -> TaxProfile:
    profile = load_profile(repo_root / "config", "es")
    assert profile.tax is not None
    return profile.tax


@pytest.fixture(scope="module")
def es(tax_profile) -> EsIrpfTaxPolicy:
    return EsIrpfTaxPolicy(tax_profile)


@pytest.fixture
def engine(es) -> LotEngine:
    return LotEngine(es, [WORLD, WORLD_DIST, PARTNER, US_SHARE])


def counted(results) -> Decimal:
    return sum((r.counted for r in results), D(0))


# ── months ──


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2025, 3, 15), 2, date(2025, 5, 15)),
        (date(2025, 3, 15), -2, date(2025, 1, 15)),
        (date(2025, 12, 31), 2, date(2026, 2, 28)),
        (date(2024, 12, 31), 2, date(2025, 2, 28)),
        (date(2023, 12, 31), 2, date(2024, 2, 29)),
        (date(2025, 1, 10), -2, date(2024, 11, 10)),
        (date(2025, 1, 10), -12, date(2024, 1, 10)),
    ],
)
def test_months_move_by_the_calendar(day, months, expected):
    assert add_months(day, months) == expected


# ── FIFO ──


def test_a_sale_consumes_the_oldest_lots_first(engine):
    engine.acquire("WORLD", on=date(2025, 1, 10), quantity=D(10), cost=D(1000))
    engine.acquire("WORLD", on=date(2025, 3, 1), quantity=D(10), cost=D(1200))
    results = engine.dispose("WORLD", on=date(2025, 9, 1), quantity=D(15), proceeds=D(1800))
    # Lot 1: 10 units for 1,200 against 1,000 -> +200. Lot 2: 5 units for 600 against 600 -> 0.
    assert [(r.quantity, r.cost, r.proceeds, r.gain) for r in results] == [
        (D(10), D(1000), D(1200), D(200)),
        (D(5), D(600), D(600), D(0)),
    ]
    assert counted(results) == D(200)
    [left] = engine.lots("WORLD")
    assert (left.quantity, left.cost) == (D(5), D(600))
    assert engine.ledger.provisional(2025).gains == D(200)


def test_a_sale_cannot_exceed_what_is_held_or_name_an_unknown_security(engine):
    engine.acquire("WORLD", on=date(2025, 1, 10), quantity=D(10), cost=D(1000))
    with pytest.raises(TaxError, match="exceeds the 10 held"):
        engine.dispose("WORLD", on=date(2025, 2, 1), quantity=D(11), proceeds=D(1100))
    with pytest.raises(TaxError, match="no registered identity"):
        engine.dispose("NOPE", on=date(2025, 2, 1), quantity=D(1), proceeds=D(1))
    for bad in (D(0), D(-1), D("NaN")):
        with pytest.raises(TaxError, match="quantity must be positive"):
            engine.acquire("WORLD", on=date(2025, 1, 10), quantity=bad, cost=D(1))
        with pytest.raises(TaxError, match="quantity must be positive"):
            engine.dispose("WORLD", on=date(2025, 1, 10), quantity=bad, proceeds=D(1))
    with pytest.raises(TaxError, match="cost must be finite"):
        engine.acquire("WORLD", on=date(2025, 1, 10), quantity=D(1), cost=D(-1))
    with pytest.raises(TaxError, match="proceeds must be finite"):
        engine.dispose("WORLD", on=date(2025, 1, 10), quantity=D(1), proceeds=D(-1))


def test_an_identity_registers_once(engine):
    engine.register(WORLD)
    with pytest.raises(TaxError, match="already registered"):
        engine.register(WORLD.model_copy(update={"issuer": "Other"}))


# ── the two-month rule (LIRPF art. 33.5.f) ──


def test_a_loss_is_deferred_by_a_purchase_before_it_and_released_when_those_units_go(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.acquire("WORLD", on=date(2025, 3, 1), quantity=D(50), cost=D(400))
    # Sell the first lot at 8: a loss of 200, 2 a unit. 50 homogeneous units bought on 1 March,
    # within the two months before, are still held: the loss on 50 units (100) is deferred.
    sale = engine.dispose("WORLD", on=date(2025, 3, 15), quantity=D(100), proceeds=D(800))
    assert [(r.gain, r.deferred) for r in sale] == [(D(-200), D(100))]
    assert counted(sale) == D(-100)
    [replacement] = engine.lots("WORLD")
    assert (replacement.deferred_loss, replacement.replacement_units) == (D(100), D(50))
    # The 50 units go at 9: a gain of 50 on them, less the 100 they carried, counts -50.
    later = engine.dispose("WORLD", on=date(2025, 7, 1), quantity=D(50), proceeds=D(450))
    assert [(r.gain, r.released, r.deferred) for r in later] == [(D(50), D(100), D(0))]
    assert counted(later) == D(-50)
    # Over the year the whole economic loss (1,400 paid, 1,250 received) counts.
    assert engine.ledger.provisional(2025).gains == D(-150)


def test_a_part_sale_of_replacement_units_never_leaves_more_replacements_than_units(engine):
    # Fourteen units bought, then fourteen more; the first fourteen sold at a loss inside the
    # window, so every unit of the second lot is a replacement.
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(14), cost=D(140))
    engine.acquire("WORLD", on=date(2025, 3, 1), quantity=D(14), cost=D(140))
    engine.dispose("WORLD", on=date(2025, 3, 15), quantity=D(14), proceeds=D(70))
    [replaced] = engine.lots("WORLD")
    assert replaced.replacement_units == replaced.quantity == D(14)
    # Five go: 5/14 rounds down at Decimal's precision, so 14 - 14 x 5/14 is a hair above the
    # nine units left; the lot's invariant refused that before the replacements were capped.
    engine.dispose("WORLD", on=date(2025, 7, 1), quantity=D(5), proceeds=D(45))
    [left] = engine.lots("WORLD")
    assert left.quantity == D(9)
    assert left.replacement_units == D(9)


def test_a_purchase_within_two_months_after_a_loss_defers_it_out_of_its_year(engine):
    engine.acquire("WORLD", on=date(2025, 10, 1), quantity=D(100), cost=D(1000))
    sale = engine.dispose("WORLD", on=date(2025, 12, 15), quantity=D(100), proceeds=D(800))
    assert counted(sale) == D(-200)
    assert engine.open_loss_units(date(2026, 2, 15)) == D(100)
    assert engine.open_loss_units(date(2026, 2, 16)) == D(0)
    # A homogeneous purchase (another share class of the same index) on 10 February 2026.
    lot = engine.acquire("WORLD_DIST", on=date(2026, 2, 10), quantity=D(60), cost=D(540))
    assert (lot.deferred_loss, lot.replacement_units) == (D(120), D(60))
    assert engine.ledger.provisional(2025).gains == D(-80)
    # The other 40 loss units wait for the rest of the window; a swap partner does not take them.
    partner = engine.acquire("PARTNER", on=date(2026, 2, 12), quantity=D(10), cost=D(100))
    assert partner.deferred_loss == D(0)
    assert engine.open_loss_units(date(2026, 2, 12)) == D(40)
    # Selling the replacement releases the 120 into 2026.
    release = engine.dispose("WORLD_DIST", on=date(2026, 5, 4), quantity=D(60), proceeds=D(600))
    assert counted(release) == D(60) - D(120)


def test_a_purchase_after_the_window_closes_defers_nothing(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.dispose("WORLD", on=date(2025, 3, 15), quantity=D(100), proceeds=D(800))
    lot = engine.acquire("WORLD", on=date(2025, 5, 16), quantity=D(100), cost=D(800))
    assert lot.deferred_loss == D(0)
    assert engine.ledger.provisional(2025).gains == D(-200)


def test_a_gain_is_never_deferred_and_a_unit_takes_one_loss(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(10), cost=D(100))
    engine.acquire("WORLD", on=date(2025, 1, 3), quantity=D(10), cost=D(200))
    engine.acquire("WORLD", on=date(2025, 1, 20), quantity=D(5), cost=D(75))
    # 10 units at 15: the first lot gains 50; nothing is deferred.
    gain = engine.dispose("WORLD", on=date(2025, 2, 1), quantity=D(10), proceeds=D(150))
    assert [(r.gain, r.deferred) for r in gain] == [(D(50), D(0))]
    # 5 units of the second lot at 12: a loss of 40. 10 units remain of homogeneous purchases
    # within the window (5 of lot 2, 5 of lot 3): the first 5 take it.
    loss = engine.dispose("WORLD", on=date(2025, 2, 2), quantity=D(5), proceeds=D(60))
    assert [(r.gain, r.deferred, r.counted) for r in loss] == [(D(-40), D(40), D(0))]
    lots = {lot.lot_id: lot for lot in engine.lots("WORLD")}
    assert lots["WORLD#2"].replacement_units == D(5)
    assert lots["WORLD#2"].deferred_loss == D(40)


def test_the_generic_policy_never_defers(tax_profile):
    generic = GenericTaxPolicy("generic", tax_profile)
    engine = LotEngine(generic, [WORLD])
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.acquire("WORLD", on=date(2025, 3, 1), quantity=D(50), cost=D(400))
    sale = engine.dispose("WORLD", on=date(2025, 3, 15), quantity=D(100), proceeds=D(800))
    assert counted(sale) == D(-200)


# ── income and withholding ──


def test_foreign_income_is_withheld_at_the_treaty_rate_and_credited(engine):
    engine.acquire("USCO", on=date(2025, 1, 2), quantity=D(10), cost=D(1000))
    event = engine.receive_income("USCO", on=date(2025, 6, 1), gross=D(100), source_country="US")
    assert event.withheld == D(15)
    summary = engine.ledger.settle(2025)
    # Base 100, tax 19 at 19 %, the credit is the lesser of 15 and 19 % of 100: 15. Due: 4.
    assert (summary.base, summary.tax_before_credit, summary.foreign_tax_credit) == (
        D(100),
        D(19),
        D(15),
    )
    assert summary.tax_due == D(4)


def test_income_from_a_country_without_a_stated_rate_is_refused(engine):
    engine.acquire("USCO", on=date(2025, 1, 2), quantity=D(10), cost=D(1000))
    with pytest.raises(TaxError, match="no withholding rate for income from FR"):
        engine.receive_income("USCO", on=date(2025, 6, 1), gross=D(100), source_country="FR")
    domestic = engine.receive_income("USCO", on=date(2025, 6, 1), gross=D(50), source_country=None)
    assert domestic.withheld == D(0)


# ── the ledger: offsets, carry-forward, payment ──


def test_a_loss_carries_forward_and_offsets_a_later_gain(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(10000))
    engine.dispose("WORLD", on=date(2025, 6, 2), quantity=D(100), proceeds=D(9000))
    first = engine.ledger.settle(2025)
    assert (first.gains, first.base, first.tax_due) == (D(-1000), D(0), D(0))
    assert [(c.component, c.arising_year, c.amount) for c in first.carried_out] == [
        ("gains", 2025, D(1000))
    ]
    assert first.payment_date == date(2026, 6, 30)
    engine.acquire("PARTNER", on=date(2026, 1, 2), quantity=D(100), cost=D(10000))
    engine.dispose("PARTNER", on=date(2026, 9, 1), quantity=D(100), proceeds=D(13000))
    second = engine.ledger.settle(2026)
    # 3,000 of gains less the 1,000 carried: a base of 2,000 taxed at 19 %.
    assert (second.gains, second.carried_in_used, second.base) == (D(3000), D(1000), D(2000))
    assert second.tax_due == D(380)
    assert second.carried_out == ()


def test_a_gains_loss_offsets_a_quarter_of_income_and_carries_the_rest(engine):
    engine.acquire("USCO", on=date(2025, 1, 2), quantity=D(100), cost=D(10000))
    engine.receive_income("USCO", on=date(2025, 3, 1), gross=D(400), source_country=None)
    engine.dispose("USCO", on=date(2025, 6, 2), quantity=D(100), proceeds=D(9000))
    summary = engine.ledger.settle(2025)
    # Gains -1,000 take 25 % of the income of 400 (100): base 300, tax 57, 900 carried.
    assert (summary.base, summary.tax_due) == (D(300), D(57))
    assert [(c.component, c.amount) for c in summary.carried_out] == [("gains", D(900))]
    # Next year: income 400 and no gains. The 900 of gains losses take 25 % of it again.
    engine.acquire("USCO", on=date(2026, 1, 2), quantity=D(1), cost=D(1))
    engine.receive_income("USCO", on=date(2026, 3, 1), gross=D(400), source_country=None)
    later = engine.ledger.settle(2026)
    assert (later.carried_in_used, later.base, later.tax_due) == (D(100), D(300), D(57))
    assert [(c.arising_year, c.amount) for c in later.carried_out] == [(2025, D(800))]


def test_an_income_loss_offsets_a_quarter_of_gains_and_a_carried_income_loss_its_own_first(es):
    ledger = TaxLedger(es)
    engine = LotEngine(es, [WORLD], ledger)
    # An income loss is unusual (a negative capital return); the ledger takes a negative gross
    # through a realisation-free path: record it as income adjusted by hand.
    ledger._years[2025].income = D(-400)
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(10), cost=D(100))
    engine.dispose("WORLD", on=date(2025, 5, 2), quantity=D(10), proceeds=D(1100))
    summary = ledger.settle(2025)
    # Gains 1,000: the income loss takes 25 % of it (250); base 750; 150 of income loss carried.
    assert (summary.base, summary.tax_due) == (D(750), D("142.50"))
    assert [(c.component, c.amount) for c in summary.carried_out] == [("income", D(150))]
    ledger._years[2026].income = D(100)
    ledger._years[2026].gains = D(1000)
    later = ledger.settle(2026)
    # The carried 150 takes the 100 of income first, then 50 of the gains (within 25 %).
    assert (later.carried_in_used, later.base) == (D(150), D(950))


def test_a_carried_loss_expires_after_four_years(es):
    ledger = TaxLedger(es)
    ledger._years[2020].gains = D(-1000)
    assert ledger.settle(2020).carried_out[0].amount == D(1000)
    for year in range(2021, 2025):
        ledger.settle(year)
    assert ledger.carried_forward()[0].amount == D(1000)
    ledger._years[2025].gains = D(5000)
    summary = ledger.settle(2025)
    assert (summary.expired, summary.carried_in_used, summary.base) == (D(1000), D(0), D(5000))


def test_a_rate_table_used_outside_its_years_is_labelled(es):
    ledger = TaxLedger(es)
    ledger._years[2019].gains = D(100)
    assert ledger.settle(2019).rate_table_covers_year is False


def test_years_settle_in_order_and_a_settled_year_is_closed(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.dispose("WORLD", on=date(2025, 12, 15), quantity=D(50), proceeds=D(400))
    engine.dispose("WORLD", on=date(2026, 3, 1), quantity=D(10), proceeds=D(120))
    with pytest.raises(TaxError, match="2025 must be settled before 2026"):
        engine.ledger.settle(2026)
    engine.ledger.settle(2025)
    with pytest.raises(TaxError, match="already settled"):
        engine.ledger.settle(2025)
    with pytest.raises(TaxError, match="is settled"):
        engine.dispose("WORLD", on=date(2025, 12, 31), quantity=D(1), proceeds=D(10))
    assert engine.quantity("WORLD") == D(40)  # refused before any lot was touched
    with pytest.raises(TaxError, match="is settled"):
        engine.acquire("WORLD", on=date(2025, 12, 31), quantity=D(1), cost=D(10))
    with pytest.raises(TaxError, match="is settled"):
        engine.receive_income("WORLD", on=date(2025, 12, 31), gross=D(1), source_country=None)
    engine.ledger.settle(2026)
    with pytest.raises(TaxError, match="precedes the settled year 2026"):
        engine.ledger.settle(2024)
    with pytest.raises(TaxError, match="precedes the settled year"):
        engine.ledger.record_deferral(2024, D(1))
    with pytest.raises(ValueError, match="magnitude"):
        engine.ledger.record_deferral(2027, D(-1))
    assert [s.tax_year for s in engine.ledger.settled()] == [2025, 2026]


def test_a_deferral_found_after_its_year_settled_is_refused(engine):
    engine.acquire("WORLD", on=date(2025, 10, 1), quantity=D(100), cost=D(1000))
    engine.dispose("WORLD", on=date(2025, 12, 15), quantity=D(100), proceeds=D(800))
    engine.ledger.settle(2025)
    with pytest.raises(TaxError, match="tax year 2025 is settled"):
        engine.acquire("WORLD", on=date(2026, 1, 10), quantity=D(10), cost=D(80))
    assert engine.lots() == ()


# ── the hooks ──


def test_tax_cost_of_a_sale_is_the_marginal_tax_it_adds(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    estimate = engine.tax_cost(
        ProposedTrade(
            security="WORLD", side="sell", quantity=D(100), price=D(80), on=date(2025, 6, 1)
        )
    )
    # A gain of 7,000: 6,000 at 19 % and 1,000 at 21 %.
    assert (estimate.realised, estimate.deferred, estimate.tax) == (D(7000), D(0), D(1350))
    assert estimate.rate_table_covers_year
    # The engine itself is untouched.
    assert engine.quantity("WORLD") == D(100)
    assert engine.ledger.provisional(2025).gains == D(0)


def test_a_trial_of_several_trades_offsets_a_loss_against_a_gain(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.acquire("PARTNER", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    on = date(2025, 6, 1)
    gain = ProposedTrade(security="WORLD", side="sell", quantity=D(100), price=D(30), on=on)
    loss = ProposedTrade(security="PARTNER", side="sell", quantity=D(100), price=D(5), on=on)
    # Alone, the gain of 2,000 costs 380 and the loss of 500 saves nothing this year; together
    # the year counts 1,500 of gain.
    assert engine.trial_tax([gain]) == engine.tax_cost(gain).tax == D(380)
    assert engine.trial_tax([loss]) == D(0)
    assert engine.trial_tax([gain, loss]) == D(285)
    assert engine.trial_tax([]) == D(0)
    # The engine itself is untouched.
    assert (engine.quantity("WORLD"), engine.quantity("PARTNER")) == (D(100), D(100))
    assert engine.ledger.provisional(2025).gains == D(0)


def test_a_trial_of_trades_in_two_tax_years_is_refused(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    trades = [
        ProposedTrade(security="WORLD", side="sell", quantity=D(1), price=D(30), on=day)
        for day in (date(2025, 12, 30), date(2026, 1, 2))
    ]
    with pytest.raises(TaxError, match="one tax year, got \\[2025, 2026\\]"):
        engine.trial_tax(trades)


def test_tax_cost_of_a_buy_counts_the_loss_it_would_defer(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.acquire("PARTNER", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.dispose("PARTNER", on=date(2025, 2, 1), quantity=D(100), proceeds=D(3000))
    engine.dispose("WORLD", on=date(2025, 3, 1), quantity=D(100), proceeds=D(500))
    trade = ProposedTrade(
        security="WORLD", side="buy", quantity=D(100), price=D(5), on=date(2025, 4, 1)
    )
    estimate = engine.tax_cost(trade)
    # The buy would defer the 500 loss: gains rise from 1,500 to 2,000, tax by 95.
    assert (estimate.realised, estimate.deferred, estimate.tax) == (D(0), D(500), D(95))
    conflict = engine.wash_sale_conflict(trade)
    assert conflict is not None
    assert (conflict.kind, conflict.deferred, conflict.dates) == (
        "purchase_defers_earlier_loss",
        D(500),
        (date(2025, 3, 1),),
    )
    partner = trade.model_copy(update={"security": "PARTNER"})
    assert engine.wash_sale_conflict(partner) is None


def test_a_loss_sale_beside_a_recent_purchase_is_a_conflict(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.acquire("WORLD", on=date(2025, 3, 1), quantity=D(10), cost=D(100))
    trade = ProposedTrade(
        security="WORLD", side="sell", quantity=D(100), price=D(8), on=date(2025, 3, 10)
    )
    conflict = engine.wash_sale_conflict(trade)
    assert conflict is not None
    assert (conflict.kind, conflict.deferred) == ("loss_sale_deferred", D(20))
    gain = trade.model_copy(update={"price": D(20)})
    assert engine.wash_sale_conflict(gain) is None


def test_harvestable_losses_say_what_a_sale_would_count_now(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.acquire("PARTNER", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.acquire("PARTNER", on=date(2025, 5, 20), quantity=D(100), cost=D(900))
    engine.acquire("USCO", on=date(2025, 1, 2), quantity=D(10), cost=D(100))
    found = engine.harvestable_losses(
        {"WORLD": D(7), "PARTNER": D(8), "USCO": D(20)}, on=date(2025, 6, 1)
    )
    # WORLD: a loss of 300, all of it countable. PARTNER: 1,900 cost against 1,600 of value; the
    # whole position is sold, so no homogeneous unit is left to take the loss: 300 counts.
    assert [(h.security, h.unrealised_loss, h.harvestable, h.deferred) for h in found] == [
        ("PARTNER", D(300), D(300), D(0)),
        ("WORLD", D(300), D(300), D(0)),
    ]
    assert engine.harvestable_losses({}, on=date(2025, 6, 1)) == ()


# ── saving and restoring ──


def test_lots_round_trip_through_the_store(engine, es, store: Store):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(100), cost=D(1000))
    engine.acquire("WORLD", on=date(2025, 3, 1), quantity=D("50.5"), cost=D("404.04"))
    engine.dispose("WORLD", on=date(2025, 3, 15), quantity=D(100), proceeds=D(800))
    saved = engine.lots()
    save_tax_lots(store, saved, saved_at=datetime(2025, 3, 15, 18, tzinfo=UTC))
    assert tax_lots(store) == list(saved)
    restored = LotEngine(es, [WORLD, WORLD_DIST, PARTNER, US_SHARE])
    restored.restore(tax_lots(store))
    assert restored.lots() == saved
    assert (
        restored.acquire("WORLD", on=date(2025, 6, 1), quantity=D(1), cost=D(9)).lot_id == "WORLD#3"
    )
    with pytest.raises(TaxError, match="only into an empty engine"):
        restored.restore(saved)
    save_tax_lots(store, [], saved_at=datetime(2025, 3, 16, tzinfo=UTC))
    assert tax_lots(store) == []


def test_a_mixed_sale_defers_only_its_losses_and_a_unit_is_used_once(engine):
    engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(10), cost=D(50))
    engine.acquire("WORLD", on=date(2025, 1, 20), quantity=D(10), cost=D(150))
    engine.acquire("WORLD", on=date(2025, 2, 1), quantity=D(10), cost=D(150))
    engine.acquire("WORLD", on=date(2025, 3, 5), quantity=D(5), cost=D(50))
    results = engine.dispose("WORLD", on=date(2025, 3, 10), quantity=D(30), proceeds=D(300))
    # The first lot gains 50; the next two lose 50 each (5 a unit). The 5 units bought on 5 March
    # take 25 of the first loss and none of the second; 15 loss units wait for a purchase.
    assert [(r.gain, r.deferred) for r in results] == [
        (D(50), D(0)),
        (D(-50), D(25)),
        (D(-50), D(0)),
    ]
    assert counted(results) == D(-25)
    assert engine.open_loss_units(date(2025, 3, 10)) == D(15)


def test_a_purchase_fills_the_oldest_open_loss_first_and_stops_when_it_is_used(engine):
    engine.acquire("WORLD", on=date(2024, 10, 1), quantity=D(20), cost=D(200))
    engine.dispose("WORLD", on=date(2025, 1, 10), quantity=D(10), proceeds=D(50))
    engine.dispose("WORLD", on=date(2025, 1, 20), quantity=D(10), proceeds=D(40))
    lot = engine.acquire("WORLD", on=date(2025, 2, 1), quantity=D(5), cost=D(30))
    # The 10 January sale lost 5 a unit: its loss takes the 5 units; the later one waits.
    assert (lot.deferred_loss, lot.replacement_units) == (D(25), D(5))
    assert engine.open_loss_units(date(2025, 2, 1)) == D(15)


def test_a_saved_lot_named_otherwise_keeps_the_running_number(engine, es):
    saved = engine.acquire("WORLD", on=date(2025, 1, 2), quantity=D(1), cost=D(10))
    restored = LotEngine(es, [WORLD])
    restored.restore([saved.model_copy(update={"lot_id": "imported-lot"})])
    assert restored.acquire("WORLD", on=date(2025, 2, 1), quantity=D(1), cost=D(9)).lot_id == (
        "WORLD#1"
    )
