"""The core engine on stand-in books and histories: a start, a band breach and only then a sale,
cash beyond the targets bought without a sale, a hold; an unfilled role left in cash and a held one
capped at what it holds; the tilt and its honest fallbacks; tax-aware holds and swap partners;
trades the cost model cannot price; the live book's tax answers.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal

import pytest
from core_rig import D, Lots, book, choice, history, path, sizing

from obliskiq.config import ObliskConfig
from obliskiq.domain.core import RoleStatus, Trigger
from obliskiq.domain.orders import OrderSide
from obliskiq.engines.core.engine import BookState, LiveBook, RoleChoice, plan_portfolio
from obliskiq.engines.core.history import HistoryView
from obliskiq.engines.core.variants import Variant, build_variant

WORLD, EM, EGB_INT, EGB_LONG, IL, UST, GOLD, COMMOD, CASH = range(11, 20)
WORLD_PARTNER = 21
ROLE_CONIDS = {
    "world_equity": WORLD,
    "em_equity": EM,
    "euro_govt_intermediate": EGB_INT,
    "euro_govt_long": EGB_LONG,
    "inflation_linked": IL,
    "us_treasuries": UST,
    "gold": GOLD,
    "broad_commodities": COMMOD,
    "cash": CASH,
}
PRICES = dict.fromkeys([*ROLE_CONIDS.values(), WORLD_PARTNER], "100")
EMPTY = HistoryView({}, book().on.replace(day=1))


@pytest.fixture(scope="module")
def a(shipped_settings: ObliskConfig) -> Variant:
    return build_variant(shipped_settings, "A")


@pytest.fixture(scope="module")
def b(shipped_settings: ObliskConfig) -> Variant:
    return build_variant(shipped_settings, "B")


def every_role(variant: Variant, *, without: tuple[str, ...] = ()) -> dict[str, RoleChoice]:
    """A choice for each of the variant's roles, its conid the primary; ``without`` roles have
    no vehicle."""
    return {
        role: choice(role, None if role in without else ROLE_CONIDS[role]) for role in variant.roles
    }


def at_targets(variant: Variant) -> dict[int, str]:
    """Units holding each role at its static weight of 10,000 (prices of 100), rounded down."""
    return {
        ROLE_CONIDS[role]: str(int(weight * 10000 / 100))
        for role, weight in variant.weights.items()
        if weight > 0
    }


def trades_by_conid(portfolio: object) -> dict[int, tuple[OrderSide, Decimal]]:
    return {t.conid: (t.side, t.quantity) for t in portfolio.trades}  # type: ignore[attr-defined]


# ── the triggers ──


def test_an_empty_book_buys_every_role_and_leaves_an_unfilled_one_in_cash(a: Variant) -> None:
    choices = every_role(a, without=("gold", "broad_commodities"))
    portfolio = plan_portfolio(a, choices, EMPTY, book(prices=PRICES), None, sizing())
    assert portfolio.trigger is Trigger.START
    assert portfolio.trend_as_of is None
    trades = trades_by_conid(portfolio)
    for role, weight in a.weights.items():
        conid = ROLE_CONIDS[role]
        if role in ("gold", "broad_commodities"):
            assert conid not in trades
            target = portfolio.role(role)
            assert target is not None
            assert (target.status, target.target_weight) == (RoleStatus.UNFILLED, 0)
            assert "its weight stays in the book's cash" in target.reason
        else:
            assert trades[conid] == (OrderSide.BUY, D(int(weight * 10000 / 100)))
    invested = sum((t.notional for t in portfolio.trades), D(0))
    left = (a.weights["gold"] + a.weights["broad_commodities"]) * 10000
    assert invested == pytest.approx(D(10000) - left, abs=D(600))  # whole units below each target


def test_a_book_inside_its_bands_holds_and_a_breach_takes_every_role_back(a: Variant) -> None:
    held = at_targets(a)
    # Whole units leave a residue below each target; below the minimum trade it is not deployed.
    inside = plan_portfolio(
        a, every_role(a), EMPTY, book(held, PRICES), None, sizing(minimum="1000")
    )
    assert inside.trigger is Trigger.HOLD
    assert inside.trades == ()
    # World equity at twice its weight leaves its band: every role goes back to its target,
    # the overweight sold, the rest bought with the proceeds.
    doubled = {**held, WORLD: str(int(held[WORLD]) * 2)}
    breach = plan_portfolio(a, every_role(a), EMPTY, book(doubled, PRICES), None, sizing())
    assert breach.trigger is Trigger.BAND
    assert trades_by_conid(breach)[WORLD][0] is OrderSide.SELL
    assert {side for side, _ in trades_by_conid(breach).values()} >= {OrderSide.SELL}


def test_cash_beyond_the_targets_buys_the_roles_below_them_and_sells_nothing(a: Variant) -> None:
    held = at_targets(a)
    # A deposit: the budget grows by a tenth and the cash with it. Every role is now below its
    # target but inside its band, and world equity, above its new target, is not sold.
    over = {**held, WORLD: str(int(held[WORLD]) + 2)}
    grown = book(over, PRICES, budget="11000", cash="2000")
    portfolio = plan_portfolio(a, every_role(a), EMPTY, grown, None, sizing())
    assert portfolio.trigger is Trigger.CASH
    sides = {side for side, _ in trades_by_conid(portfolio).values()}
    assert sides == {OrderSide.BUY}
    assert WORLD not in trades_by_conid(portfolio)
    held_value = sum((D(q) * 100 for q in over.values()), D(0))
    assert sum((t.notional for t in portfolio.trades), D(0)) <= D(11000) - held_value
    # Free cash below the minimum trade is not deployed.
    small = book(held, PRICES, budget="10100", cash="2000")
    assert plan_portfolio(a, every_role(a), EMPTY, small, None, sizing(minimum="2000")).trigger is (
        Trigger.HOLD
    )


def test_the_equity_reference_buys_once_and_holds(shipped_settings: ObliskConfig) -> None:
    reference = build_variant(shipped_settings, "equity reference")
    choices = {"world_equity": choice("world_equity", WORLD)}
    start = plan_portfolio(reference, choices, EMPTY, book(prices=PRICES), None, sizing())
    assert trades_by_conid(start) == {WORLD: (OrderSide.BUY, D(100))}
    # Half its budget in cash is far outside any band, but a buy and hold never rebalances; only
    # the cash beyond its target (a deposit) is bought.
    held = book({WORLD: "100"}, PRICES, budget="10000", cash="0")
    assert plan_portfolio(reference, choices, EMPTY, held, None, sizing()).trigger is Trigger.HOLD


# ── roles that cannot be bought ──


def test_a_held_role_with_no_vehicle_to_buy_keeps_at_most_what_it_holds(a: Variant) -> None:
    held = at_targets(a)
    held[GOLD] = str(int(held[GOLD]) // 2)  # half its weight, held through a closed vehicle
    choices = every_role(a)
    choices["gold"] = choice("gold", None, held=[GOLD], closed=[GOLD])
    portfolio = plan_portfolio(a, choices, EMPTY, book(held, PRICES), None, sizing())
    gold = portfolio.role("gold")
    assert gold is not None
    assert gold.status is RoleStatus.HELD_ONLY
    assert gold.target_weight == gold.held_weight  # capped: no perpetual band breach
    assert portfolio.trigger is not Trigger.BAND
    assert GOLD not in trades_by_conid(portfolio)


def test_a_holding_no_role_names_is_sold_at_the_breach(a: Variant) -> None:
    held = {**at_targets(a), 99: "5"}
    choices = {**every_role(a), "unlisted:99": choice("unlisted:99", None, held=[99], closed=[99])}
    portfolio = plan_portfolio(a, choices, EMPTY, book(held, {**PRICES, 99: "100"}), None, sizing())
    assert portfolio.trigger is Trigger.BAND
    assert trades_by_conid(portfolio)[99] == (OrderSide.SELL, D(5))


def test_a_trade_the_cost_model_cannot_price_is_withheld(a: Variant) -> None:
    portfolio = plan_portfolio(
        a, every_role(a), EMPTY, book(prices=PRICES), None, sizing(unpriced=[EM])
    )
    withheld = portfolio.withheld_for(EM)
    assert withheld is not None
    assert (withheld.side, withheld.reason) == (OrderSide.BUY, "the cost model cannot price it")
    assert EM not in trades_by_conid(portfolio)
    held = {**at_targets(a), WORLD: "50"}
    costly = plan_portfolio(
        a, every_role(a), EMPTY, book(held, PRICES), None, sizing(sale_cost="1e9")
    )
    found = costly.withheld_for(WORLD)
    assert found is not None
    assert found.reason == "it costs more than it raises"


# ── the tilt ──


def tilted(b: Variant, falling: tuple[str, ...] = (), short: tuple[str, ...] = ()) -> HistoryView:
    """30 month ends for each role (6 for the ``short`` ones), rising 1 % a month or falling 4 %;
    cash accrues 0.1 % a month."""
    paths = {
        str(ROLE_CONIDS[role]): path(
            100, -0.04 if role in falling else 0.01, 6 if role in short else 30
        )
        for role in b.roles
    }
    paths[str(CASH)] = path(100, 0.001, 30)
    return history(paths)


def test_a_falling_trend_scales_a_role_to_its_floor_and_cash_takes_the_rest(b: Variant) -> None:
    portfolio = plan_portfolio(
        b, every_role(b), tilted(b, ("world_equity",)), book(prices=PRICES), None, sizing()
    )
    assert portfolio.trend_as_of == tilted(b).month
    world = portfolio.role("world_equity")
    cash = portfolio.role("cash")
    assert world is not None
    assert cash is not None
    assert world.scale == D("0.25")
    assert world.target_weight == b.weights["world_equity"] * D("0.25")
    assert cash.target_weight == b.weights["world_equity"] * D("0.75")


def test_a_role_without_enough_history_or_cash_history_is_left_whole(b: Variant) -> None:
    short = plan_portfolio(
        b,
        every_role(b),
        tilted(b, ("world_equity",), short=("world_equity",)),
        book(prices=PRICES),
        None,
        sizing(),
    )
    world = short.role("world_equity")
    assert world is not None
    assert world.scale == 1
    assert "untilted: 6 of the 13 month ends the tilt needs" in world.reason
    view = tilted(b, ("world_equity",))
    no_cash = HistoryView({k: v for k, v in view.history.items() if k != str(CASH)}, view.month)
    portfolio = plan_portfolio(b, every_role(b), no_cash, book(prices=PRICES), None, sizing())
    world = portfolio.role("world_equity")
    assert world is not None
    assert world.scale == 1
    assert "momentum has no cash history" in world.reason


def test_a_role_reads_its_trend_on_another_vehicle_with_the_history(b: Variant) -> None:
    view = tilted(b, ("world_equity",))
    paths = dict(view.history)
    paths[str(WORLD_PARTNER)] = paths.pop(str(WORLD))
    choices = every_role(b)
    choices["world_equity"] = choice("world_equity", WORLD, partner=WORLD_PARTNER)
    portfolio = plan_portfolio(
        b, choices, HistoryView(paths, view.month), book(prices=PRICES), None, sizing()
    )
    world = portfolio.role("world_equity")
    assert world is not None
    assert world.scale == D("0.25")


# ── tax-aware execution ──


def test_a_costly_trend_reduction_is_held_and_an_unknown_tax_is_held_too(b: Variant) -> None:
    held = at_targets(b)
    view = tilted(b, ("world_equity",))
    costly = Lots(tax_per_unit={WORLD: D(50)})  # half the price of 100 in tax: above 5 %
    portfolio = plan_portfolio(b, every_role(b), view, book(held, PRICES), costly, sizing())
    world = portfolio.role("world_equity")
    assert world is not None
    assert world.reduction_held
    assert "its trend reduction is held" in world.reason
    assert WORLD not in trades_by_conid(portfolio)
    cheap = plan_portfolio(b, every_role(b), view, book(held, PRICES), Lots(), sizing())
    assert trades_by_conid(cheap)[WORLD][0] is OrderSide.SELL
    unknown = plan_portfolio(b, every_role(b), view, book(held, PRICES), None, sizing())
    assert "the tax lots cannot be read" in " ".join(unknown.notes)
    assert WORLD not in trades_by_conid(unknown)
    broken = plan_portfolio(b, every_role(b), view, book(held, PRICES), Lots(broken=True), sizing())
    assert WORLD not in trades_by_conid(broken)


def test_a_purchase_that_would_defer_a_loss_goes_to_the_swap_partner(b: Variant) -> None:
    choices = every_role(b)
    choices["world_equity"] = choice("world_equity", WORLD, partner=WORLD_PARTNER)
    lots = Lots(defers={WORLD})
    portfolio = plan_portfolio(b, choices, tilted(b), book(prices=PRICES), lots, sizing())
    trades = trades_by_conid(portfolio)
    assert WORLD not in trades
    assert trades[WORLD_PARTNER][0] is OrderSide.BUY


# ── the live book ──


def test_the_live_book_answers_tax_only_for_complete_lots() -> None:
    state = BookState(
        units={WORLD: D(10), EM: D(5)},
        prices={WORLD: D(100), EM: D(50)},
        budget=D(2000),
        cash=D(0),
        on=book().on,
    )
    lots = Lots(bases={WORLD: D(800)}, incomplete={EM}, tax_per_unit={WORLD: D(2)})
    live = LiveBook(state, lots)
    assert live.units == {str(WORLD): D(10), str(EM): D(5)}
    assert (live.value(str(WORLD)), live.weight(str(WORLD))) == (D(1000), D("0.5"))
    assert live.basis(str(WORLD)) == D(800)
    assert live.basis(str(EM)) is None
    assert live.sales_tax([(str(WORLD), D(3))]) == D(6)
    assert live.sales_tax([(str(EM), D(1))]) is None
    assert live.units_for(str(WORLD), D(250)) == D(2)
    assert live.units_for("unfilled:gold", D(250)) == D(0)
    assert not live.purchase_defers_loss("unfilled:gold")
    bare = LiveBook(state, None)
    assert bare.basis(str(WORLD)) is None
    assert bare.sales_tax([(str(WORLD), D(1))]) is None
    assert not bare.purchase_defers_loss(str(WORLD))
    assert LiveBook(state, Lots(broken=True)).sales_tax([(str(WORLD), D(1))]) is None
    empty = LiveBook(BookState(units={}, prices={}, budget=D(0), cash=D(0), on=book().on), None)
    assert empty.weight(str(WORLD)) == 0
