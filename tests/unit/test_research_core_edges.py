"""The core's edges: a pre-registration's core block refused with its problem, the pricing of
month-end closes in the base currency (kept per panel, or read afresh for a series published
late), the book's answers from the lot engine, the structure check when its data fail, and the
report's rows when nothing respects a point or no interval exists.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from dataclasses import replace
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from research_core_toy import CORE_TOY, NOW, month_ends, put_core_toy

from obliskiq.domain.enums import AssetClass
from obliskiq.domain.research import Observation
from obliskiq.domain.tax import SecurityIdentity, SecurityKind
from obliskiq.errors import PreregistrationError, SimulationError
from obliskiq.jurisdiction import load_profile
from obliskiq.jurisdiction.tax_es_irpf import EsIrpfTaxPolicy
from obliskiq.quant.trend import TrendRule
from obliskiq.research.decide import AdequacyCheck
from obliskiq.research.prereg import StudySpec, load_prereg
from obliskiq.research.registry import TrialRegistry
from obliskiq.research.report import _grid, render
from obliskiq.research.simulate import (
    Book,
    MarketView,
    Panel,
    PanelIndex,
    Pricing,
    SimInstrument,
    SimulationSettings,
    share,
    whole,
)
from obliskiq.research.strategies import build_strategy
from obliskiq.research.structure import overlay_returns, structure_check
from obliskiq.research.study import FrontierPoint, StudyRunner
from obliskiq.store import open_research_store
from obliskiq.tax.lots import LotEngine

D = Decimal


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "core_toy.md"
    path.write_text(text, encoding="utf-8")
    return path


# ── the pre-registration's core block ──


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('        - [EQ, BD, "0"]', '        - [EQ, EQ, "0"]', "pairs two different roles"),
        ('        - [EQ, BD, "0"]', '        - [EQ, BD, "0"]\n        - [BD, EQ, "0"]',
         "exactly one correlation"),
        ("      growth_down: [BD]", "      growth_down: []", "at least one role"),
        ('      volatilities: {EQ: "0.16", BD: "0.05"}\n      correlations:\n'
         '        - [EQ, BD, "0"]',
         '      volatilities: {EQ: "0.16"}\n      correlations: []', "cover exactly the roles"),
        ("    cash_role: CASH", "    cash_role: BD", "belongs to no environment"),
        ("      primary: BD\n", "      primary: XX\n", "names unknown instruments"),
        ("      partner: EQ_SWAP", "      partner: EQ", "swap partner is its primary"),
        ("    cash_role: CASH", "    cash_role: GOLD", "roles without vehicles"),
        ('      weights: {EQ: "0.6", BD: "0.4"}', '      weights: {EQ: "0.6", XX: "0.4"}',
         "weights unknown roles"),
        ('      weights: {EQ: "0.6", BD: "0.4"}', '      weights: {EQ: "0.7", BD: "0.4"}',
         "weights sum above 1"),
        ('      budgets: {growth_up: "0.5", growth_down: "0.5"}', '      budgets: {growth_up: "1"}',
         "one budget per environment"),
        ('      budgets: {growth_up: "0.5", growth_down: "0.5"}',
         '      budgets: {growth_up: "0.5", growth_down: "0.4"}', "must sum to 1"),
        ("      equity: EQ", "      equity: CASH", "not a risky role"),
        ("  end: 1999-12-31", "  end: 1989-12-31", "end must follow its start"),
        ("    - [1995-01-01, 1999-12-31]", "    - [1995-01-01, 2001-12-31]",
         "outside its span"),
        ("  core:\n    cash_role", "  core_x:\n    cash_role", "core"),
    ],
)  # fmt: skip
def test_an_incoherent_core_block_is_refused_with_its_problem(tmp_path, old, new, message):
    assert old in CORE_TOY, old
    with pytest.raises(PreregistrationError, match=message):
        load_prereg(write(tmp_path, CORE_TOY.replace(old, new, 1)))


def test_a_structure_check_needs_the_core(tmp_path):
    text = CORE_TOY
    start = text.index("  roles:\n")
    end = text.index("  reference:\n")
    core_less = text[:start] + "  roles: null\n  core: null\n" + text[end:]
    for name in ("60/40", "balance", "overlay"):
        core_less = core_less.replace(f"    - name: {name}\n", f"    - name: {name}\n", 1)
    with pytest.raises(PreregistrationError, match=r"needs study\.roles and study\.core"):
        load_prereg(write(tmp_path, core_less))


def test_a_core_strategy_built_without_a_core_is_refused(tmp_path, shipped_settings):
    study = load_prereg(write(tmp_path, CORE_TOY)).spec.study
    bare = StudySpec.model_construct(**{**dict(study), "core": None})
    with pytest.raises(PreregistrationError, match=r"needs study\.roles and study\.core"):
        build_strategy(study.candidates[1], bare, shipped_settings)


# ── month-end closes and the book ──

SETTINGS = SimulationSettings(
    base_currency="EUR", initial_capital=D(1000), no_trade_band=D("0.005"),
    max_price_age_days=7, max_fx_age_days=7, whole_units=True, days_per_year=365.25,
    cost_multiplier=D(1),
)  # fmt: skip


def instrument(key: str, currency: str = "EUR") -> SimInstrument:
    return SimInstrument(
        key=key, venue="XETR", asset_class=AssetClass.ETF, currency=currency,
        security=SecurityIdentity(
            key=key, kind=SecurityKind.ETF, isin=None, issuer=f"{key} issuer",
            tracked_index=f"{key} index", listed=True,
        ),
        issuer_country=None, market_cap_usd=None, income_country=None,
    )  # fmt: skip


def obs(day: date, value: float, available: date | None = None) -> Observation:
    return Observation(observed_on=day, available_on=available or day, value=value)


def pricing(prices, fx=None, *, instruments=None) -> Pricing:
    panel = Panel(prices=prices, income={}, fx=fx or {})
    known = instruments or {key: instrument(key) for key in prices}
    return Pricing(known, PanelIndex(panel), SETTINGS)


def test_month_end_closes_are_converted_and_kept_per_panel():
    # A USD series against the ECB rate (units per euro): 110 USD at 1.1 is 100 EUR.
    prices = {"U": (obs(date(2024, 1, 31), 110.0), obs(date(2024, 2, 29), 121.0))}
    fx = {"USD": (obs(date(2024, 1, 31), 1.1), obs(date(2024, 2, 29), 1.1))}
    priced = pricing(prices, fx, instruments={"U": instrument("U", "USD")})
    closes = priced.month_end_closes("U", date(2024, 2, 29))
    assert closes == (
        (date(2024, 1, 31), pytest.approx(100.0)),
        (date(2024, 2, 29), pytest.approx(110.0)),
    )
    # The completed month is kept: asked again later it is the same value.
    assert priced.index.completed_months["U"] == [closes[0]]
    assert priced.month_end_closes("U", date(2024, 2, 29)) == closes
    assert pricing({"E": ()}).month_end_closes("E", date(2024, 2, 29)) == ()
    assert priced.to_base(D(11), "USD", date(2024, 2, 29), date(2024, 2, 29)) == D(10)
    assert priced.to_base(D(11), "USD", date(2023, 1, 2), date(2024, 2, 29)) is None


def test_a_series_published_late_is_read_afresh_as_of_each_date():
    late = (
        obs(date(2024, 1, 31), 100.0, available=date(2024, 2, 2)),
        obs(date(2024, 2, 29), 120.0),
    )
    priced = pricing({"L": late})
    # On 31 January the January close is not public: the month has no close.
    with pytest.raises(SimulationError, match="no month-end close on 2024-01-31"):
        priced.month_end_closes("L", date(2024, 1, 31))
    assert priced.month_end_closes("L", date(2024, 2, 29)) == (
        (date(2024, 1, 31), 100.0),
        (date(2024, 2, 29), 120.0),
    )
    assert "L" not in priced.index.completed_months
    # A price read as of a later date is not kept beside the one read on its own date.
    assert priced.listing_price("L", date(2024, 1, 31), date(2024, 2, 5)) == D(100)
    assert priced.listing_price("L", date(2024, 1, 31)) is None


def test_a_view_over_a_bare_panel_has_no_closes_or_trends():
    view = MarketView(Panel(prices={}, income={}, fx={}), date(2024, 1, 31))
    with pytest.raises(SimulationError, match="bare panel"):
        view.month_end_closes("A")
    rule = TrendRule(momentum_months=1, average_months=2, hysteresis=0.0, floor=0.5)
    with pytest.raises(SimulationError, match="bare panel"):
        view.trend("A", "C", rule)


def test_a_trend_is_computed_once_per_panel_and_date():
    days = [date(2024, m, 28) for m in range(1, 5)]
    priced = pricing(
        {
            "A": tuple(obs(d, 100.0 + k) for k, d in enumerate(days)),
            "C": tuple(obs(d, 1.0) for d in days),
        }
    )
    rule = TrendRule(momentum_months=1, average_months=2, hysteresis=0.0, floor=0.5)
    view = MarketView(priced, days[-1])
    first = view.trend("A", "C", rule)
    assert first is not None and first.scale == 1.0  # noqa: PT018
    assert view.trend("A", "C", rule) is first
    assert priced.trend("A", "C", rule, days[0]) is None  # one month end is too few


@pytest.fixture(scope="module")
def es(repo_root) -> EsIrpfTaxPolicy:
    profile = load_profile(repo_root / "config", "es")
    assert profile.tax is not None
    return EsIrpfTaxPolicy(profile.tax)


def test_the_book_answers_from_the_lot_engine(es):
    engine = LotEngine(es, [instrument(k).security for k in ("A", "B")])
    engine.acquire("A", on=date(2025, 1, 2), quantity=D(10), cost=D(1000))
    engine.acquire("B", on=date(2025, 1, 2), quantity=D(10), cost=D(1000))
    on = date(2025, 6, 30)
    prices = {"A": D(120), "B": D(80)}
    book = Book({"A": D(10), "B": D(10)}, prices.__getitem__, D(2000), engine, on)
    assert (book.price("A"), book.value("A"), book.value("B")) == (D(120), D(1200), D(800))
    assert book.weight("A") == D("0.6")
    assert book.basis("A") == D(1000)
    assert book.units_for("A", D(250)) == D(2)
    # A value a rounding residue short of whole units is those units.
    assert book.units_for("A", D(360) - D("1e-24")) == D(3)
    # Selling 5 A at 120 realises 100 of gain: 19 % of it. Selling 5 B at 80 alone loses 100
    # and saves nothing this year; together the loss offsets the gain.
    assert book.sales_tax([("A", D(5))]) == D(19)
    assert book.sales_tax([("B", D(5))]) == D(0)
    assert book.sales_tax([("A", D(5)), ("B", D(5))]) == D(0)
    assert book.sales_tax([("A", D(0))]) == book.sales_tax([]) == D(0)
    assert book.purchase_defers_loss("B") is False
    empty = Book({}, lambda _: D(1), D(0), engine, on)
    assert empty.weight("A") == D(0)


@settings(max_examples=200, deadline=None)
@given(
    held=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=100_000),
            st.decimals(min_value="0.01", max_value="5000", places=6),
        ),
        min_size=1,
        max_size=12,
    ),
    cash=st.decimals(min_value="0", max_value="1000", places=9),
)
def test_shares_never_sum_above_one_and_round_trip_to_the_units_held(held, cash):
    values = [D(units) * price for units, price in held]
    total = sum(values, D(0)) + cash
    weights = [share(value, total) for value in values]
    assert sum(weights, D(0)) <= 1
    for (units, price), weight in zip(held, weights, strict=True):
        assert whole(total * weight / price) == units


def test_whole_units_round_as_asked_away_from_a_residue():
    assert whole(D("2.9999999999999")) == D(3)
    assert whole(D("3.0000000000001"), ROUND_CEILING) == D(3)
    assert whole(D("2.99")) == D(2)
    assert whole(D("2.01"), ROUND_CEILING) == D(3)
    assert share(D(1), D(3)) == D("0.333333333333333333333333")


def test_one_unit_worth_a_millionth_of_a_percent_comes_back_as_one_unit():
    # Hypothesis found it: at a weight quantum of 1e-20, one unit at 0.01 in an account of
    # about a billion lost its round trip (the weight kept nine significant digits).
    held = [(1, D("0.01")), (1, D("0.01")), (20892, D("4999.999911")), (3, D("0.012351")),
            (16879, D("4521.311154")), (93949, D("4042.621742")), (93949, D("5000"))]  # fmt: skip
    values = [D(units) * price for units, price in held]
    total = sum(values, D(0))
    for (units, price), value in zip(held, values, strict=True):
        assert whole(total * share(value, total) / price) == units


def test_the_engine_says_a_purchase_would_defer_a_loss_as_its_conflict_hook_does(es):
    from obliskiq.domain.tax import ProposedTrade

    engine = LotEngine(es, [instrument(k).security for k in ("A", "B")])
    engine.acquire("A", on=date(2025, 1, 2), quantity=D(10), cost=D(1000))
    engine.dispose("A", on=date(2025, 3, 3), quantity=D(10), proceeds=D(800))
    for day in (date(2025, 3, 10), date(2025, 6, 10)):
        trade = ProposedTrade(security="A", side="buy", quantity=D(1), price=D(80), on=day)
        conflict = engine.wash_sale_conflict(trade) is not None
        assert engine.purchase_defers_loss("A", day) is conflict
    assert engine.purchase_defers_loss("A", date(2025, 3, 10)) is True
    assert engine.purchase_defers_loss("B", date(2025, 3, 10)) is False


def test_a_copy_of_the_engine_is_independent(es):
    engine = LotEngine(es, [instrument("A").security])
    engine.acquire("A", on=date(2025, 1, 2), quantity=D(10), cost=D(1000))
    twin = engine._copy()
    twin.dispose("A", on=date(2025, 2, 3), quantity=D(4), proceeds=D(500))
    assert engine.quantity("A") == D(10)
    assert twin.quantity("A") == D(6)
    assert engine.ledger.provisional(2025).gains == D(0)
    assert twin.ledger.provisional(2025).gains == D(100)


# ── the structure check and the report's edges ──


def test_a_structure_check_whose_data_fail_is_not_run(tmp_path, repo_root, shipped_settings):
    prereg = load_prereg(write(tmp_path, CORE_TOY))
    assert prereg.spec.structure is not None and prereg.spec.study.core is not None  # noqa: PT018
    failed = (AdequacyCheck("data loaded", False, "never loaded: mkt.RF"),)
    rule = TrendRule(momentum_months=3, average_months=2, hysteresis=0.02, floor=0.25)
    result = structure_check(
        prereg.spec.structure, [], [], [], failed, rule=rule, bootstrap=prereg.spec.bootstrap,
        limits=prereg.spec.drawdown_limits, alpha=0.05, days_per_year=365.25,
    )  # fmt: skip
    assert (result.overlay, result.interval, result.first) == (None, None, None)
    runner = StudyRunner(shipped_settings, load_profile(repo_root / "config", "es"))
    with open_research_store(shipped_settings, tmp_path / "data") as store:
        empty = runner._structure(prereg, prereg.spec.structure, rule, store, alpha=0.05)
    assert [c.passed for c in empty.checks] == [False, True, False]


def test_a_subperiod_without_months_is_left_out(tmp_path):
    prereg = load_prereg(write(tmp_path, CORE_TOY))
    spec = prereg.spec.structure
    assert spec is not None
    spec = spec.model_copy(update={"subperiods": [(date(1990, 1, 1), date(1990, 2, 1))]})
    months = [date(1990, m, 28) for m in range(1, 13)]
    rule = TrendRule(momentum_months=3, average_months=2, hysteresis=0.02, floor=0.25)
    checks = (AdequacyCheck("history", True, "ok"),)
    result = structure_check(
        spec, months, [0.01] * 12, [0.001] * 12, checks, rule=rule,
        bootstrap=prereg.spec.bootstrap, limits=prereg.spec.drawdown_limits, alpha=0.05,
        days_per_year=365.25,
    )  # fmt: skip
    assert result.subperiods == ()
    assert result.months_reduced == 0  # a steady rise is never reduced


def test_a_structure_subperiod_is_annualised_over_the_months_it_holds(tmp_path):
    prereg = load_prereg(write(tmp_path, CORE_TOY))
    spec = prereg.spec.structure
    assert spec is not None
    year = (date(1990, 1, 1), date(1990, 12, 31))
    spec = spec.model_copy(update={"subperiods": [year]})
    months = month_ends(date(1990, 1, 31), 24)
    market, cash = [-0.02] * 24, [0.001] * 24
    rule = TrendRule(momentum_months=3, average_months=2, hysteresis=0.02, floor=0.25)
    checks = (AdequacyCheck("history", True, "ok"),)
    result = structure_check(
        spec, months, market, cash, checks, rule=rule, bootstrap=prereg.spec.bootstrap,
        limits=prereg.spec.drawdown_limits, alpha=0.05, days_per_year=365.25,
    )  # fmt: skip
    overlay, _, start = overlay_returns(
        np.array(market), np.array(cash), rule, float(spec.cost_per_leg)
    )
    # The tilt first trades after April: the year holds May to December, eight months.
    held = [d for d in months[start + 1 :] if d <= year[1]]
    assert (months[start], len(held)) == (date(1990, 4, 30), 8)
    gap = math.log(float(np.prod(1 + overlay[: len(held)])) / 0.98 ** len(held))
    years = (held[-1] - months[start]).days / 365.25
    assert result.subperiods[0].growth_difference == pytest.approx(gap / years)


def test_a_grid_label_keeps_the_digits_it_was_written_with():
    assert [_grid(D(x)) for x in ("0.025", "0.1", "0.125", "0.5", "1")] == [
        "2.5%", "10%", "12.5%", "50%", "100%",
    ]  # fmt: skip


def test_the_report_says_when_nothing_respects_a_point_or_a_check_failed(
    tmp_path, repo_root, shipped_settings
):
    prereg_path = write(tmp_path, CORE_TOY)
    runner = StudyRunner(shipped_settings, load_profile(repo_root / "config", "es"))
    with open_research_store(shipped_settings, tmp_path / "data") as store:
        put_core_toy(store)
        result = runner.run(
            load_prereg(prereg_path), store, TrialRegistry(store), explore=True, lock=None, now=NOW
        )
    nobody = FrontierPoint(D("0.1"), D("0.05"), (), None, (), None)
    assert result.structure is not None
    broken = replace(
        result,
        frontier=(nobody,),
        structure=replace(result.structure, overlay=None, held=None),
        tests=(
            replace(result.tests[0], decision=replace(result.tests[0].decision, interval=None)),
        ),
    )
    text = render(broken)
    assert "none respects this point" in text
    assert "Not run: a check failed." in text
    assert "| n/a |" in text
    # The toy's paths sell nothing at a loss of its costs; a pre-registered frontier the
    # bootstrap never reached says so, rather than that none was pre-registered.
    assert "equity 0 on the historical path, on 0 bootstrap paths" in render(result)
    unreached = render(replace(result, frontier=()))
    assert "Not computed: the bootstrap did not run" in unreached
    assert "No frontier was pre-registered." not in unreached


def test_a_structure_check_without_a_core_is_refused(tmp_path):
    start = CORE_TOY.index("  roles:\n")
    end = CORE_TOY.index("  reference:\n")
    head = CORE_TOY[:start] + "  roles: null\n  core: null\n" + CORE_TOY[end:]
    candidates = head.index("    - name: 60/40")
    tail = head.index("bootstrap:")
    text = head[:candidates] + head[tail:]
    text = text.replace("tests_in_confirmation: 4", "tests_in_confirmation: 1").replace(
        '  by_candidate:\n    overlay: "0.02"\n', "  by_candidate: null\n"
    )
    with pytest.raises(PreregistrationError, match="uses the core's tilt"):
        load_prereg(write(tmp_path, text))


def test_a_report_without_data_says_n_a_for_the_aspiration(tmp_path, repo_root, shipped_settings):
    runner = StudyRunner(shipped_settings, load_profile(repo_root / "config", "es"))
    with open_research_store(shipped_settings, tmp_path / "empty") as store:
        result = runner.run(
            load_prereg(write(tmp_path, CORE_TOY)), store, TrialRegistry(store), explore=True,
            lock=None, now=NOW,
        )  # fmt: skip
    assert result.frontier == ()
    text = render(result)
    assert "**not evaluable**" in text
    row = next(line for line in text.splitlines() if line.startswith("| overlay | n/a"))
    assert row.rstrip(" |").endswith("n/a")
