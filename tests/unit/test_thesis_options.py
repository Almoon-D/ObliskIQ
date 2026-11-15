"""A thesis held as an option, piece by piece: what an idea may be held as, the store of its
option and twin, the gates a thesis adds to the sleeve's, its activation, and the pair record.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from options_rig import AS_OF, EXPIRIES, SPY, position, seed_options
from pydantic import ValidationError
from thesis_rig import HORIZON, NOW, TODAY, draft, facts, instrument, thesis

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.decision.gates import CandidateGates, Gate
from obliskiq.domain.enums import OptionRight
from obliskiq.domain.options import PairGrade, ThesisOption
from obliskiq.domain.orders import ExecutionMode
from obliskiq.domain.thesis import Expression, View
from obliskiq.engines.options_sleeve.activation import thesis_activation
from obliskiq.engines.options_sleeve.penalty import PenaltyLedger
from obliskiq.engines.options_sleeve.thesis_link import Chosen, ThesisLink
from obliskiq.engines.thesis.exits import exit_due
from obliskiq.engines.thesis.gate import ThesisGate, gain_to_loss, screen, view_findings
from obliskiq.engines.thesis.grading import pair_record
from obliskiq.errors import StoreError
from obliskiq.store import Store, migrations
from obliskiq.store import options as options_repo
from obliskiq.store import theses as thesis_repo
from obliskiq.store import thesis_options as link_repo

D = Decimal
FALL: dict[str, Any] = {"target": D(30), "invalidation": D(50), "view": "bearish"}
FAR = EXPIRIES[1]


def codes(found: Any) -> list[str]:
    return [finding.code.value for finding in found]


# ── what an idea may be held as ──


def test_a_fall_is_a_bearish_idea_held_as_an_option_with_no_entry_limit() -> None:
    assert screen(draft(expression="option", **FALL), today=TODAY) == []
    assert screen(draft(expression="auto", **FALL), today=TODAY) == []
    assert codes(screen(draft(**FALL), today=TODAY)) == ["short"]
    not_bearish = screen(draft(expression="option", **{**FALL, "view": "bullish"}), today=TODAY)
    assert codes(not_bearish) == ["levels"]
    limited = screen(draft(expression="option", entry_max=D(40), **FALL), today=TODAY)
    assert (codes(limited), limited[0].reason) == (
        ["levels"],
        "a fall held as a long put names no entry limit",
    )
    same = screen(draft(target=D(40), invalidation=D(40)), today=TODAY)
    assert codes(same) == ["levels"]
    # Its own fall needs no defensive instrument; a bearish idea held long still does.
    assert view_findings(View.BEARISH, instrument(), falls=True) == []
    assert codes(view_findings(View.BEARISH, instrument())) == ["bearish_not_defensive"]


def test_a_thesis_that_falls_is_never_held_as_shares() -> None:
    put = thesis(expression="option", **FALL)
    assert (put.falls, put.as_option) == (True, False)
    with pytest.raises(ValidationError, match="long put or as nothing"):
        thesis(
            expression="auto", held_as="shares", mode="shadow", size_base=D(1), approved_at=NOW,
            **FALL,
        )  # fmt: skip
    with pytest.raises(ValidationError, match="as it allowed"):
        thesis(held_as="auto", mode="shadow", size_base=D(1), approved_at=NOW)


def test_a_falls_levels_are_read_the_other_way_round() -> None:
    put = thesis(expression="option", **FALL)
    assert gain_to_loss(put, D(45)) == D(3)  # 15 to gain, 5 to lose
    assert gain_to_loss(put, D(55)) is None
    assert exit_due(put, D(29), TODAY) == "target"
    assert exit_due(put, D(50), TODAY) == "invalidation"
    assert exit_due(put, D(40), TODAY) is None
    assert exit_due(put, None, HORIZON + timedelta(days=1)) == "time_window"
    gate = ThesisGate(_settings().thesis)
    assert codes(gate.market(put, facts(reference=D(28)))) == ["target_passed"]
    assert codes(gate.market(put, facts(reference=D(51)))) == ["invalidated"]
    assert "at or above the invalidation 50" in gate.market(put, facts(reference=D(51)))[0].reason
    assert codes(gate.market(put, facts(reference=D(35)))) == ["gain_to_loss"]
    assert gate.market(put, facts(reference=D(45))) == []
    full = facts(slots=tuple(_slot(n) for n in range(8)))
    assert codes(gate.slot(put, D(100), full)) == ["book_full"]


def _slot(n: int) -> Any:
    from obliskiq.engines.thesis.book import Slot

    return Slot(conid=n + 1, size_base=D(1), sector=None, country=None, currency=None)


def _settings() -> ObliskConfig:
    from obliskiq.config import load_config

    root = Path(__file__).resolve().parents[2]
    return load_config(root / "config").settings


# ── the store ──


def chosen(**changes: Any) -> ThesisOption:
    fields: dict[str, Any] = {
        "thesis_id": 1,
        "option_conid": 900013,
        "underlying_conid": SPY,
        "symbol": "SPY 2027-04-18 475 PUT",
        "exchange": "CBOE",
        "description": "SPY 2027-04-18 475 PUT on SS SPDR S&P 500 ETF TRUST-US",
        "currency": "USD",
        "expiry": FAR,
        "strike": 475.0,
        "right": OptionRight.PUT,
        "multiplier": 100.0,
        "contracts": 1,
        "close_by": date(2027, 4, 2),
        "prob_target_at_entry": 0.2,
        "underlying_entry": D(500),
        "twin_size_base": D(1250),
    }
    return ThesisOption(**{**fields, **changes})


def approved_put(store: Store) -> None:
    stored = thesis_repo.insert_thesis(
        store, thesis(expression="option", instrument=instrument(conid=SPY), **FALL), actor="o"
    )
    thesis_repo.approve(
        store, stored.thesis_id, mode=ExecutionMode.SHADOW, size_base=D(300), at=NOW,
        actor="b", detail="approved", held_as=Expression.OPTION,
    )  # fmt: skip
    link_repo.record(store, chosen())


def test_a_thesiss_option_is_fixed_its_position_set_once_and_its_twin_closed_once(
    store: Store,
) -> None:
    approved_put(store)
    held = thesis_repo.get_thesis(store, 1)
    assert held is not None
    assert (held.expression, held.held_as, held.as_option) == (
        Expression.OPTION,
        Expression.OPTION,
        True,
    )
    assert link_repo.get(store, 1) == chosen()
    assert link_repo.get(store, 2) is None
    assert link_repo.open_twins(store) == []  # no position yet: nothing was bought
    with pytest.raises(StoreError, match="no position yet"):
        link_repo.record(store, chosen(thesis_id=2, position_id=3))
    opened = options_repo.open_position(store, position(underlying_conid=SPY))
    assert opened.position_id is not None
    link_repo.set_position(store, 1, opened.position_id)
    assert [c.thesis_id for c in link_repo.open_twins(store)] == [1]
    assert link_repo.pairs(store, as_of=TODAY) == []  # the option has not closed
    for statement in (
        "UPDATE thesis_options SET position_id = 9 WHERE thesis_id = 1",
        "UPDATE thesis_options SET contracts = 2 WHERE thesis_id = 1",
        "DELETE FROM thesis_options",
        "UPDATE theses SET held_as = 'shares' WHERE thesis_id = 1",
        "UPDATE theses SET expression = 'auto' WHERE thesis_id = 1",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(statement)
    link_repo.close_twin(store, 1, price=D(450), reason="target", at=NOW)
    with pytest.raises(StoreError):
        link_repo.close_twin(store, 1, price=D(440), reason="target", at=NOW)
    closed = link_repo.get(store, 1)
    assert closed is not None
    assert (closed.twin_open, closed.twin_payoff) == (False, D("0.1"))  # a fall is its gain
    assert chosen(right=OptionRight.CALL, twin_exit=D(450), twin_exit_reason="t",
                  twin_closed_at=NOW).twin_payoff == D("-0.1")  # fmt: skip
    with pytest.raises(ValidationError, match="together"):
        chosen(twin_exit=D(450))
    with pytest.raises(ValidationError, match="on or before its expiry"):
        chosen(close_by=FAR + timedelta(days=1))


def test_the_pairs_are_read_from_the_sleeves_ledger(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    approved_put(store)
    day = date(2026, 11, 2)
    opened = options_repo.open_position(store, position(underlying_conid=SPY))
    assert opened.position_id is not None
    link_repo.set_position(store, 1, opened.position_id)
    ledger = PenaltyLedger(store, shipped_settings, now=lambda: NOW)
    ledger.book_premium_committed(position_id=opened.position_id, premium=D(300), on=day)
    ledger.reconcile_loss_closed(position_id=opened.position_id, actual_loss=D(120), on=day)
    options_repo.close_position(store, opened.position_id, closed_on=day, exit_reason="zombie")
    assert link_repo.pairs(store, as_of=day - timedelta(days=1)) == []
    (pair,) = link_repo.pairs(store, as_of=day)
    assert (pair.option_pnl, pair.premium_base, pair.twin_pnl, pair.exit_reason) == (
        D(-120),
        D(300),
        None,  # the twin still runs
        "zombie",
    )
    link_repo.close_twin(
        store, 1, price=D(450), reason="target", at=datetime(2026, 11, 20, tzinfo=UTC)
    )
    assert link_repo.pairs(store, as_of=day)[0].twin_pnl is None  # not known on that day
    assert link_repo.pairs(store, as_of=date(2026, 11, 20))[0].twin_pnl == D(125)


def test_the_migration_keeps_every_thesis_as_shares(tmp_path: Path) -> None:
    source = Path(migrations.__file__).parent / "migrations"
    before = tmp_path / "before"
    before.mkdir()
    for path in sorted(source.glob("*.sql")):
        if not path.name.startswith("016"):
            (before / path.name).write_text(path.read_text())
    connection = sqlite3.connect(tmp_path / "db.sqlite", autocommit=True)
    migrations.migrate(connection, before)
    columns = (
        "source, conviction, view, symbol, conid, listing_exchange, currency, asset_class, "
        "description, defensive, expectation, target, invalidation, horizon, enter_by, state, "
        "created_at"
    )
    stamp = "2026-10-26T09:00:00Z"
    row = ("owner", "normal", "bullish", "EMIM", 1, "AEB", "EUR", "etf", "d", 0, "e", "60", "40",
           "2027-04-30", "2026-11-15")  # fmt: skip
    connection.execute(
        f"INSERT INTO theses ({columns}) VALUES ({', '.join('?' * 17)})",
        (*row, "candidate", stamp),
    )
    connection.execute(
        f"INSERT INTO theses ({columns}, mode, size_base, approved_at) "
        f"VALUES ({', '.join('?' * 20)})",
        (*row, "approved", stamp, "shadow", "125", stamp),
    )
    connection.execute(
        "INSERT INTO thesis_log VALUES (2, 1, ?, 'owner', 'intake', 'handed in')", (stamp,)
    )
    assert migrations.migrate(connection) == ["016_thesis_options.sql"]
    rows = connection.execute(
        "SELECT thesis_id, state, expression, held_as FROM theses ORDER BY thesis_id"
    ).fetchall()
    assert rows == [(1, "candidate", "shares", None), (2, "approved", "shares", "shares")]
    assert connection.execute("SELECT COUNT(*) FROM thesis_log").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError, match="never change"):
        connection.execute("UPDATE theses SET target = '70' WHERE thesis_id = 2")
    connection.execute(
        f"INSERT INTO theses ({columns}, expression) VALUES ({', '.join('?' * 18)})",
        (*row, "candidate", stamp, "auto"),
    )
    assert connection.execute("SELECT MAX(thesis_id) FROM theses").fetchone() == (3,)
    connection.close()


# ── the gates a thesis adds, its activation and the record ──


def test_an_option_must_still_be_held_on_the_thesiss_horizon(
    shipped_settings: ObliskConfig,
) -> None:
    gates = CandidateGates(shipped_settings)
    day = date(2027, 3, 15)
    assert gates.horizon_cover(close_by=day, horizon=day).fully_tested
    short = gates.horizon_cover(close_by=day - timedelta(days=1), horizon=day)
    assert (short.failed_gates, "does not cover the idea" in (short.reason() or "")) == (
        (Gate.HORIZON,),
        True,
    )
    unknown = gates.horizon_cover(close_by=None, horizon=day, gap="no calendar")
    assert (unknown.untested_gates, unknown.untested[0].reason) == ((Gate.HORIZON,), "no calendar")
    unmeasured = gates.entry_range(entry_range_pctile=None, catalyst_date=None, gap="few bars")
    assert unmeasured.untested[0].reason == "few bars"


def test_a_thesiss_activation_is_the_book_trading_and_a_premium_it_can_pay() -> None:
    active = thesis_activation(book_capital=D(10000), available=D(5000))
    assert (active.active, active.reason()) == (
        True,
        "active: the thesis book trades on 10000.00; 5000.00 of the option budget is available",
    )
    locked = thesis_activation(book_capital=D(10000), available=D(0))
    assert locked.reason() == "shadow: premium fails: 0.00 of the option budget is available"
    idle = thesis_activation(book_capital=D(0), available=None)
    assert idle.reason() == (
        "shadow: book fails: the thesis book has no capital in this mode; premium untested: no "
        "pool without a book that trades"
    )


def test_the_record_keeps_calls_and_puts_apart_and_never_values_a_running_twin() -> None:
    def pair(n: int, right: OptionRight, made: str, twin: str | None) -> PairGrade:
        return PairGrade(
            thesis_id=n, right=right, exit_reason="target", premium_base=D(300),
            option_pnl=D(made), twin_size_base=D(1250), twin_pnl=None if twin is None else D(twin),
        )  # fmt: skip

    assert pair_record([]) == []
    calls, puts = pair_record(
        [
            pair(1, OptionRight.PUT, "-300", "-50"),
            pair(2, OptionRight.CALL, "900", "200"),
            pair(3, OptionRight.PUT, "450", None),
        ]
    )
    assert (calls.direction, calls.closed, calls.option_pnl, calls.twin_pnl) == (
        "calls", 1, D(900), D(200),
    )  # fmt: skip
    assert (puts.closed, puts.premium, puts.option_pnl) == (2, D(600), D(150))
    assert (puts.twins_closed, puts.twins_open, puts.twin_pnl) == (1, 1, D(-50))


# ── the link, on an offline store ──


def falling(**changes: Any) -> Any:
    fields = {
        "thesis_id": 1,
        "expression": "option",
        "instrument": instrument(conid=SPY, symbol="SPY", currency="USD", listing_exchange="ARCA"),
        "target": D(420),
        "invalidation": D(530),
        "view": "bearish",
        "horizon": date(2027, 3, 15),
        "enter_by": AS_OF + timedelta(days=10),
        "created_at": datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
    }
    return thesis(**{**fields, **changes})


def choose(link: ThesisLink, store: Store, held: Any, **changes: Any) -> Any:
    amounts = {"investable": D(100000), "book_capital": D(10000), "spendable": D(10000)}
    return link.choose(store, held, as_of=AS_OF, **{**amounts, **changes})


@pytest.fixture(scope="module")
def nyse(shipped_settings: ObliskConfig) -> Calendars:
    return Calendars.from_settings(shipped_settings, reference=AS_OF, venues=("XNYS",))


def test_the_link_chooses_the_put_that_pays_most_at_the_target_in_one_block(
    store: Store, shipped_settings: ObliskConfig, nyse: Calendars
) -> None:
    seed_options(store, shipped_settings, first_conid=900001, history=260, risk_free=4.0)
    link = ThesisLink(shipped_settings, nyse)
    picked = choose(link, store, falling())
    assert isinstance(picked, Chosen), picked
    row = picked.candidate.row
    assert (row.right, row.expiry, row.strike, picked.contracts) == (OptionRight.PUT, FAR, 475, 1)
    assert picked.result.fully_tested
    assert picked.payoff_ratio == pytest.approx((475 - 420) / row.mid)
    assert picked.prob_target is not None
    assert 0 < picked.prob_target < 0.5
    assert picked.friction.share is not None
    assert picked.friction.share < D("0.15")
    assert link.budget(investable=D(100000), book_capital=D(2000)) == D(2000)
    # A bigger block buys whole contracts, never past what the book may spend now.
    wide = shipped_settings.option_sleeve.model_copy(update={"block_share": D("0.6")})
    roomy = ThesisLink(shipped_settings.model_copy(update={"option_sleeve": wide}), nyse)
    assert choose(roomy, store, falling()).contracts == 2
    assert choose(roomy, store, falling(), spendable=D(1500)).contracts == 1
    short = choose(roomy, store, falling(), spendable=D(500))
    assert codes(short) == ["option_gates"]
    assert "position_size" in short[0].reason


def test_the_link_says_why_no_option_is_bought(
    store: Store, shipped_settings: ObliskConfig, nyse: Calendars
) -> None:
    link = ThesisLink(shipped_settings, nyse)
    none = choose(link, store, falling(instrument=instrument()))  # EMIM collects no chain
    assert [(f.code.value, f.permanent) for f in none] == [("no_options", True)]
    assert codes(choose(link, store, falling())) == ["no_options"]  # no chain stored yet
    idle = choose(link, store, falling(), book_capital=D(0))
    assert (codes(idle), "the thesis book has no capital" in idle[0].reason) == (
        ["option_budget"],
        True,
    )
    # A bare chain: no year of closes and no rate, so two gates cannot be applied.
    seed_options(store, shipped_settings, first_conid=900001)
    bare = choose(link, store, falling())
    assert codes(bare) == ["option_gates"]
    assert "untested: " in bare[0].reason
    assert "entry_range_catalyst 8" in bare[0].reason
    assert "theta_budget 8" in bare[0].reason


def test_an_expiry_inside_its_mandatory_close_window_is_not_opened(
    store: Store, shipped_settings: ObliskConfig, nyse: Calendars
) -> None:
    day = FAR - timedelta(days=9)  # inside the ten sessions before its last tradable one
    tenor = shipped_settings.tenor.model_copy(update={"entry_dte_min": 1})
    settings = shipped_settings.model_copy(update={"tenor": tenor})
    seed_options(
        store, settings, as_of=day, first_conid=900010, history=260, risk_free=4.0,
        expiries=(FAR,),
    )  # fmt: skip
    late = falling(
        horizon=FAR,
        enter_by=day,
        created_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
    )
    found = ThesisLink(settings, nyse).choose(
        store, late, as_of=day, investable=D(100000), book_capital=D(10000), spendable=D(10000)
    )
    assert codes(found) == ["option_gates"]
    assert "mandatory_close 4" in found[0].reason


def test_a_review_needs_a_fresh_chain_since_the_position_opened(
    store: Store, shipped_settings: ObliskConfig, nyse: Calendars
) -> None:
    link = ThesisLink(shipped_settings, nyse)
    held = position(
        underlying_conid=SPY, expiry=FAR, strike=475.0, right=OptionRight.PUT, currency="USD",
        opened_on=AS_OF, close_by=date(2027, 4, 2), premium_per_unit=D("13.8"), target=420.0,
        prob_target_at_entry=0.2,
    )  # fmt: skip
    assert "no chain of" in str(link.review(store, held, as_of=AS_OF))
    seed_options(store, shipped_settings, first_conid=900001)
    assert "no risk-free rate for USD" in str(link.review(store, held, as_of=AS_OF))
    seed_options(store, shipped_settings, first_conid=900001, risk_free=4.0)
    risk = link.review(store, held, as_of=AS_OF)
    assert not isinstance(risk, str)
    assert (risk.as_of, risk.strike, risk.target.level, risk.prob_target_at_entry) == (
        AS_OF, 475.0, 420.0, 0.2,
    )  # fmt: skip
    assert risk.bid is not None
    later = held.model_copy(update={"opened_on": AS_OF + timedelta(days=1)})
    assert "since the position opened" in str(
        link.review(store, later, as_of=AS_OF + timedelta(days=1))
    )
    assert "too old to review on" in str(link.review(store, held, as_of=AS_OF + timedelta(days=9)))
    gone = held.model_copy(update={"expiry": FAR + timedelta(days=7), "close_by": FAR})
    assert "no smile survived hygiene" in str(link.review(store, gone, as_of=AS_OF))
