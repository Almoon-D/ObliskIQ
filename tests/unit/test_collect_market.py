"""The market-data collectors over the fake market.

Ported from lite's collector tests (``test_collect_universe``, ``test_collect_conid_supersede``,
``test_collect_backfill``, ``test_collect_chain``, ``test_collect_european_chain``,
``test_collect_curve``, ``test_collect_idempotence``, ``test_collect_run_continuity``,
``test_collect_sequence_isolation``, ``test_collector_outage``), adapted to the run log and the
store of this repository.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from fake_market import Chain, Option
from market_rig import AFTER_CLOSE, ASML, EMIM, IBGM, SPX, SPY, MarketRig, build_market_rig

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.data.market import universe as universe_module
from obliskiq.data.market.backfill import session_bars, years_before
from obliskiq.data.market.chains import maturity, option_exchange
from obliskiq.data.market.curves import annualised_roll
from obliskiq.data.market.plumbing import Collection, PermanentCollectionError
from obliskiq.data.market.targets import declared_targets, root_targets
from obliskiq.domain.enums import OptionRight
from obliskiq.domain.universe import CandidateResolution
from obliskiq.ibkr.models import HistoryBar
from obliskiq.orchestration.runlog import DEGRADED, ERROR, OK, PARTIAL, SKIPPED
from obliskiq.store import repos
from obliskiq.store import runs as run_repo
from obliskiq.store import universe as universe_repo

SESSION = date(2026, 10, 21)
XAMS = {"XAMS"}
#: The universe's one unpinned instrument on XAMS, and the conid the fake search resolves it to.
JDEP = 555001


@pytest.fixture
async def rig(
    make_session: Callable[..., Any],
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    tmp_path: Path,
) -> AsyncIterator[MarketRig]:
    with respx.mock(assert_all_called=False) as router:
        built = await build_market_rig(
            settings=shipped_settings,
            session=make_session(),
            router=router,
            calendars=calendars,
            tmp_path=tmp_path,
        )
        yield built
        await built.aclose()


def declare(rig: MarketRig, *conids: int) -> None:
    """The contract details IBKR answers for the universe's own declarations, and a contract
    for its one unpinned instrument on XAMS."""
    rig.market.contracts[JDEP] = ("JDEP", "AEB", "EUR")
    for declared in rig.settings.universe.instruments:
        if declared.conid in conids:
            rig.market.contracts[declared.conid] = (
                declared.symbol,
                declared.listing_exchange,
                declared.currency,
            )


async def seeded(rig: MarketRig, *conids: int) -> None:
    declare(rig, *conids)
    venues = {t.venue for t in declared_targets(rig.settings) if t.conid in conids}
    await rig.collector.universe.run(venues)


def target(rig: MarketRig, conid: int) -> Any:
    return next(t for t in declared_targets(rig.settings) if t.conid == conid)


# ── the universe ──


async def test_pinned_instruments_are_confirmed_and_seeded_and_a_rerun_changes_nothing(
    rig: MarketRig,
) -> None:
    declare(rig, EMIM, IBGM, ASML)
    collection = await rig.collector.universe.run(XAMS)
    outcomes = {o.target: o for o in collection.outcomes}
    assert set(outcomes) == {"EMIM", "IBGM", "ASML", "JDEP"}
    assert all(outcomes[s].ok and not outcomes[s].degraded for s in ("EMIM", "IBGM", "ASML"))
    assert outcomes["JDEP"].degraded  # resolved, not pinned
    assert collection.status == DEGRADED
    stored = await rig.store(repos.list_instruments, as_of=SESSION, include_superseded=False)
    assert {i.conid for i in stored} == {EMIM, IBGM, ASML}
    emim = next(i for i in stored if i.conid == EMIM)
    assert (emim.ucits, emim.domicile, emim.listing_exchange) == (True, "IE", "AEB")
    again = await rig.collector.universe.run(XAMS)
    assert again.notes["superseded"] == []
    assert len(await rig.store(repos.list_instruments, as_of=SESSION, include_superseded=True)) == 3


async def test_a_pin_that_does_not_confirm_is_not_seeded_and_not_collected(rig: MarketRig) -> None:
    declare(rig, EMIM, IBGM)
    del rig.market.contracts[JDEP]  # and the unpinned one resolves to nothing
    rig.market.contracts[ASML] = ("ASML", "AEB", "USD")  # IBKR says another currency
    collection = await rig.collector.universe.run(XAMS)
    assert collection.status == PARTIAL
    assert "JDEP on AEB resolved to 0 contracts" in str([o.reason for o in collection.outcomes])
    collection.outcomes = [o for o in collection.outcomes if o.target != "JDEP"]
    (failed,) = [o for o in collection.outcomes if not o.ok]
    assert failed.target == "ASML"
    assert "its pin does not confirm" in failed.reason
    assert "not the declared EUR" in failed.reason
    targets = await rig.collector.targets(XAMS)
    assert ASML not in {t.conid for t in targets}


async def test_a_corrected_pin_supersedes_the_old_conid_and_keeps_its_history(
    rig: MarketRig, shipped_settings: ObliskConfig
) -> None:
    await seeded(rig, EMIM)
    bars = session_bars(EMIM, [_bar(SESSION, 46.0)], timezone_of(rig, "XAMS"))
    await rig.store(repos.upsert_bars, bars)
    corrected = _with_conid(shipped_settings, "EMIM", 999001)
    rig.market.contracts[999001] = ("EMIM", "AEB", "EUR")
    collector = _collector_with(rig, corrected)
    collection = await collector.universe.run(XAMS)
    assert collection.notes["superseded"] == [f"EMIM: {EMIM} by 999001"]
    old = await rig.store(repos.get_instrument, EMIM, as_of=SESSION)
    assert old.superseded_by == 999001
    assert await rig.store(repos.bar_count, EMIM, as_of=SESSION) == 1  # nothing deleted


async def test_an_unpinned_instrument_is_reported_with_the_conid_it_resolves_to(
    rig: MarketRig, shipped_settings: ObliskConfig
) -> None:
    unpinned = _with_conid(shipped_settings, "IBGM", None)
    rig.market.contracts[43614383] = ("IBGM", "AEB", "EUR")
    declare(rig, EMIM, ASML)
    collection = await _collector_with(rig, unpinned).universe.run(XAMS)
    (ibgm,) = [o for o in collection.outcomes if o.target == "IBGM"]
    assert ibgm.ok
    assert ibgm.degraded
    assert "`obliskiq universe pin IBGM`" in ibgm.reason
    assert ibgm.detail["resolved"] == IBGM
    stored = await rig.store(repos.list_instruments, as_of=SESSION, include_superseded=False)
    assert IBGM not in {i.conid for i in stored}  # not collected until pinned


async def test_a_leveraged_or_inverse_product_is_rejected_at_ingestion_and_recorded(
    rig: MarketRig, shipped_settings: ObliskConfig
) -> None:
    leveraged = _described(shipped_settings, "IBGM", "ISHARES DAILY 2X LEVERAGED BUND")
    declare(rig, EMIM, IBGM, ASML)
    collection = await _collector_with(rig, leveraged).universe.run(XAMS)
    assert collection.notes["rejected"] == ["IBGM"]
    assert "IBGM" not in {o.target for o in collection.outcomes}
    (refusal,) = await rig.store(universe_repo.rejections, as_of=SESSION)
    assert refusal.symbol == "IBGM"
    assert "leveraged or inverse" in refusal.reason
    assert IBGM not in {t.conid for t in declared_targets(leveraged)}


async def test_pinned_stock_candidates_are_seeded_as_tradable_shares(rig: MarketRig) -> None:
    await rig.store(
        universe_repo.record_resolution,
        CandidateResolution(
            candidate_key="jp:toyota", resolved_at=AFTER_CLOSE, conid=13976, symbol="7203",
            listing_exchange="TSEJ", currency="JPY", pinned=True, pinned_for="h-1", failure=None,
        ),
    )  # fmt: skip
    collection = await rig.collector.universe.run(set())
    assert collection.notes["candidates_seeded"] == 1
    targets = await rig.collector.targets({"XTKS"})
    assert [(t.conid, t.origin) for t in targets if t.conid == 13976] == [(13976, "candidate")]


def test_pin_in_file_writes_the_conid_and_keeps_every_comment(config_dir: Path) -> None:
    path = config_dir / "universe.yaml"
    before = path.read_text(encoding="utf-8")
    universe_module.pin_in_file(path, "TOPIX", 12345)
    after = path.read_text(encoding="utf-8")
    assert after.count("conid: 12345") == 1
    assert len(after.splitlines()) == len(before.splitlines())
    assert [line for line in after.splitlines() if line.lstrip().startswith("#")] == [
        line for line in before.splitlines() if line.lstrip().startswith("#")
    ]
    with pytest.raises(Exception, match="no `conid: null`"):
        universe_module.pin_in_file(path, "TOPIX", 1)
    with pytest.raises(Exception, match="declared 0 times"):
        universe_module.pin_in_file(path, "NOPE", 1)


# ── backfill ──


async def test_an_empty_history_is_backfilled_over_the_window(rig: MarketRig) -> None:
    await seeded(rig, EMIM)
    rig.market.bars(EMIM, [40.0 + i * 0.01 for i in range(600)], last=SESSION)
    collection = await rig.collector.backfill.run([target(rig, EMIM)])
    assert collection.status == OK
    held = await rig.store(repos.bar_series, EMIM, as_of=SESSION)
    window = years_before(SESSION, rig.settings.collectors.backfill.window_years)
    assert held[-1].session_date == SESSION
    assert window <= held[0].session_date <= window + timedelta(days=7)
    assert {bar.source for bar in held} == {"cp"}


async def test_a_held_history_is_extended_at_the_newest_end_with_an_overlap_only(
    rig: MarketRig,
) -> None:
    await seeded(rig, EMIM)
    rig.market.bars(EMIM, [40.0 + i * 0.01 for i in range(600)], last=SESSION)
    await rig.collector.backfill.run([target(rig, EMIM)])
    asked = len(rig.market.asked("history"))
    rig.now.at = AFTER_CLOSE + timedelta(days=1)
    rig.market.bars(EMIM, [40.0 + i * 0.01 for i in range(601)], last=SESSION + timedelta(days=1))
    collection = await rig.collector.backfill.run([target(rig, EMIM)])
    (outcome,) = collection.outcomes
    overlap = rig.settings.collectors.backfill.overlap_days
    assert outcome.rows <= overlap + 1  # the overlap and the new session, not the window again
    recent = rig.market.asked("history")[asked:]
    assert all(int(p["period"].rstrip("d")) <= 400 for p in recent)
    assert (await rig.store(repos.latest_bar_date, EMIM, as_of=SESSION + timedelta(days=1))) == (
        SESSION + timedelta(days=1)
    )


async def test_a_refused_history_fails_the_target_and_the_others_still_run(rig: MarketRig) -> None:
    await seeded(rig, EMIM, IBGM)
    rig.market.bars(EMIM, [40.0] * 30, last=SESSION)
    rig.market.history_refused.add(IBGM)
    collection = await rig.collector.backfill.run([target(rig, IBGM), target(rig, EMIM)])
    assert collection.status == PARTIAL
    failed = [o for o in collection.outcomes if not o.ok]
    assert [o.target for o in failed] == ["IBGM"]
    assert await rig.store(repos.bar_count, EMIM, as_of=SESSION) == 30


def test_a_bar_is_dated_by_its_venue_session_not_its_utc_date(rig: MarketRig) -> None:
    tokyo_midnight = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)  # 00:00 on the 21st in Tokyo
    (bar,) = session_bars(1, [HistoryBar(start=tokyo_midnight, open=1, high=1, low=1, close=1,
                                          volume=1)], timezone_of(rig, "XTKS"))  # fmt: skip
    assert bar.session_date == date(2026, 10, 21)
    no_close = HistoryBar(start=tokyo_midnight, open=1, high=1, low=1, close=None, volume=1)
    assert session_bars(1, [no_close], timezone_of(rig, "XTKS")) == []
    assert years_before(date(2028, 2, 29), 2) == date(2026, 2, 28)


# ── snapshots ──


async def test_snapshots_record_each_quote_with_its_availability_against_the_plan(
    rig: MarketRig,
) -> None:
    await seeded(rig, EMIM, SPY)
    rig.market.quote(EMIM, availability="DP")  # delayed: no feed of the plan covers XAMS
    rig.market.quote(SPY, availability="DP")  # delayed although us_complimentary streams it
    targets = [target(rig, EMIM), target(rig, SPY)]
    collection = await rig.collector.snapshots.run(targets)
    outcomes = {o.target: o for o in collection.outcomes}
    assert not outcomes["EMIM"].degraded
    assert outcomes["SPY"].degraded
    assert "us_complimentary streams XNYS in real time" in outcomes["SPY"].reason
    assert collection.status == DEGRADED
    rig.market.quote(SPY, availability="RP")
    again = await rig.collector.snapshots.run(targets)
    assert again.status == OK
    assert again.notes["availability"] == {"XAMS: delayed": 1, "XNYS: realtime": 1}
    (row,) = await rig.store(repos.snapshots, SPY, as_of=SESSION)
    assert (row.source, row.market_data_raw, row.snapshot_date) == ("cp", "RP", SESSION)


async def test_an_index_without_a_level_is_degraded_and_a_stock_without_a_price_fails(
    rig: MarketRig,
) -> None:
    targets = [target(rig, SPX), target(rig, EMIM)]
    collection = await rig.collector.snapshots.run(targets)
    outcomes = {o.target: o for o in collection.outcomes}
    assert outcomes["SPX"].ok
    assert outcomes["SPX"].degraded
    assert not outcomes["EMIM"].ok
    rig.market.unavailable.add("snapshot")
    down = await rig.collector.snapshots.run(targets)
    assert down.status == ERROR
    assert (await rig.collector.snapshots.run([])).status == SKIPPED


# ── chains ──


def european_chain(rig: MarketRig) -> None:
    """ASML's options on Euronext (FTA), which the search lists only as SMART and EUREX."""
    months = ["NOV26", "JAN27", "FEB27", "MAR27", "DEC27"]
    chain = Chain(months=months, exchanges=["FTA"])
    # 86, 121 and 149 days out: January falls short of the 90-day window.
    expiries = {"JAN27": date(2027, 1, 15), "FEB27": date(2027, 2, 19), "MAR27": date(2027, 3, 19)}
    strikes = [float(s) for s in range(400, 1201, 20)]
    for month, expiry in expiries.items():
        chain.strikes[month] = strikes
        for index, strike in enumerate(strikes):
            for right in ("C", "P"):
                conid = 900000 + index * 10 + (0 if right == "C" else 1) + expiry.month * 1000
                chain.contracts[(month, strike, right)] = [Option(conid, expiry)]
                rig.market.option_quote(conid, bid=10.0, ask=10.4, iv=0.3, delta=0.4)
    rig.market.chains[ASML] = chain


async def test_the_european_chain_sends_the_declared_venue_and_stores_its_surface(
    rig: MarketRig,
) -> None:
    await seeded(rig, ASML)
    rig.market.bars(ASML, [800.0 * (1.01 if i % 2 else 0.99) for i in range(40)], last=SESSION)
    rig.market.quote(ASML, bid="799", ask="801", last="800")
    european_chain(rig)
    chain = rig.market.chains[ASML]
    chain.exchanges, chain.serves = ["SMART", "EUREX"], ["FTA"]  # the search's list omits FTA
    await rig.collector.backfill.run([target(rig, ASML)])  # the band is sized from the bars
    collection = await rig.collector.chains.run([target(rig, ASML)])
    (outcome,) = collection.outcomes
    assert outcome.ok, outcome.reason
    assert {p["exchange"] for p in rig.market.asked("strikes")} == {"FTA"}
    rows = await rig.store(repos.chain_rows, as_of=SESSION, underlying_conid=ASML)
    assert rows
    assert {r.option_exchange for r in rows} == {"FTA"}
    assert {r.multiplier for r in rows} == {100.0}
    assert {r.expiry for r in rows} == {date(2027, 2, 19), date(2027, 3, 19)}  # the window only
    assert all(r.sigma_source == "realised_30d" for r in rows)
    assert {r.right for r in rows} == {OptionRight.CALL, OptionRight.PUT}
    band = max(r.strike for r in rows) - min(r.strike for r in rows)
    assert band < 800  # a band around spot, not the whole ladder of 400..1200
    (snapshot,) = await rig.store(repos.snapshots, ASML, as_of=SESSION)
    assert snapshot.last == 800


async def test_zero_chain_rows_fail_and_write_neither_half(rig: MarketRig) -> None:
    await seeded(rig, ASML)
    rig.market.bars(ASML, [800.0] * 40, last=SESSION)
    rig.market.quote(ASML, bid="799", ask="801", last="800")
    european_chain(rig)  # its options answer on FTA only
    on_eurex = _option_exchange(rig.settings, "ASML", "EUREX")
    collector = _collector_with(rig, on_eurex)
    asml = next(t for t in declared_targets(on_eurex) if t.conid == ASML)
    collection = await collector.chains.run([asml])
    (outcome,) = collection.outcomes
    assert not outcome.ok
    assert "resolved no option contract on EUREX" in outcome.reason
    assert await rig.store(repos.snapshots, ASML, as_of=SESSION) == ()
    assert await rig.store(repos.chain_row_count, as_of=SESSION) == 0


async def test_a_thin_history_falls_back_and_marks_the_target_degraded(rig: MarketRig) -> None:
    await seeded(rig, ASML)
    rig.market.quote(ASML, bid="799", ask="801", last="800")
    european_chain(rig)
    collection = await rig.collector.chains.run([target(rig, ASML)])
    (outcome,) = collection.outcomes
    assert outcome.degraded
    assert outcome.detail["sigma_source"] == "vendor_iv" or outcome.detail["sigma_source"] == (
        "default"
    )
    rows = await rig.store(repos.chain_rows, as_of=SESSION, underlying_conid=ASML)
    per_expiry = max(sum(1 for r in rows if r.expiry == e and r.right == "C") for e in
                     {r.expiry for r in rows})  # fmt: skip
    assert per_expiry <= rig.settings.collectors.chain.max_strikes_per_expiry_on_fallback


async def test_no_spot_is_a_permanent_failure_and_no_month_in_window_too(rig: MarketRig) -> None:
    await seeded(rig, ASML)
    collection = await rig.collector.chains.run([target(rig, ASML)])
    assert "no spot for ASML" in collection.outcomes[0].reason
    rig.market.quote(ASML, bid="799", ask="801", last="800")
    rig.market.chains[ASML] = Chain(months=["NOV26"], exchanges=["FTA"])
    later = await rig.collector.chains.run([target(rig, ASML)])
    assert "no option month in the" in later.outcomes[0].reason
    del rig.market.chains[ASML]
    none = await rig.collector.chains.run([target(rig, ASML)])
    assert "reports no option months" in none.outcomes[0].reason


def test_the_option_venue_and_the_maturity_form(rig: MarketRig) -> None:
    spy = target(rig, SPY)
    assert option_exchange(spy, ("SMART", "CBOE")) == "SMART"
    assert option_exchange(spy, ("CBOE", "ARCA")) == "ARCA"
    assert option_exchange(spy, ()) == "ARCA"
    assert option_exchange(target(rig, ASML), ("SMART",)) == "FTA"
    assert maturity("20270115") == date(2027, 1, 15)
    for bad in (None, "2027011", "2027O115", "20271315"):
        assert maturity(bad) is None


# ── curves ──


async def test_a_curve_records_each_rung_and_rolls_against_the_front(rig: MarketRig) -> None:
    rig.market.futures_ladders["CL"] = [
        (5001, date(2026, 10, 20)),  # expired: not on today's curve
        (5002, date(2026, 11, 20)),
        (5003, date(2026, 12, 18)),
        (5004, date(2027, 1, 20)),
    ]
    rig.market.quote(5002, bid="70", ask="70.2", last="70.1")
    rig.market.quote(5003, bid="70.9", ask="71.1", last="71")
    cl = [r for r in root_targets(rig.settings) if r.root.root == "CL"]
    collection = await rig.collector.curves.run(cl)
    (outcome,) = collection.outcomes
    assert outcome.ok
    assert not outcome.degraded
    points = await rig.store(repos.curve, "CL", as_of=SESSION)
    assert [p.expiry for p in points] == [date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 20)]
    front, second, third = points
    assert front.annualised_roll is None
    assert second.annualised_roll is not None
    assert second.annualised_roll < 0  # contango: the deferred contract is dearer
    assert (third.price, third.annualised_roll) == (None, None)
    assert {p.exchange for p in points} == {"NYMEX"}


async def test_an_unpriced_ladder_is_degraded_and_an_empty_one_fails(rig: MarketRig) -> None:
    rig.market.futures_ladders["CL"] = [(5002, date(2026, 11, 20)), (5003, date(2026, 12, 18))]
    cl = [r for r in root_targets(rig.settings) if r.root.root == "CL"]
    unpriced = await rig.collector.curves.run(cl)
    assert unpriced.outcomes[0].degraded
    rig.market.futures_ladders["CL"] = []
    empty = await rig.collector.curves.run(cl)
    assert "lists no future" in empty.outcomes[0].reason
    assert annualised_roll(front_price=0, deferred_price=1, days_apart=30,
                           calendar_days_per_year=365) is None  # fmt: skip


# ── the job: continuity, isolation, idempotence, outage ──


async def test_a_job_records_every_step_and_a_broken_step_does_not_stop_the_next(
    rig: MarketRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare(rig, EMIM, IBGM, ASML)
    rig.market.bars(EMIM, [46.0] * 40, last=SESSION)
    rig.market.bars(IBGM, [170.0] * 40, last=SESSION)
    rig.market.quote(EMIM)
    rig.market.quote(IBGM)

    async def broken(*args: Any, **kwargs: Any) -> Collection:
        raise RuntimeError("the backfill broke")

    monkeypatch.setattr(rig.collector.backfill, "run", broken)
    record = await rig.collector.run("collect:XAMS", SESSION, venues=XAMS)
    statuses = {o.name: o.status for o in record.outcomes}
    assert statuses["backfill"] == ERROR
    assert statuses["universe"] == DEGRADED  # JDEP resolves but is not pinned
    assert statuses["snapshots"] in (OK, PARTIAL, ERROR)
    assert statuses["curves"] == SKIPPED  # no root on XAMS
    assert list(statuses) == ["universe", "backfill", "snapshots", "chains", "curves"]
    (run,) = await rig.store(run_repo.runs_for, command="collect:XAMS", session_date=SESSION)
    assert run.status == record.status
    steps = await rig.store(run_repo.steps_of, run.run_id)
    assert [s.name for s in steps] == list(statuses)


async def test_a_step_that_outruns_its_budget_is_an_error_and_the_next_still_runs(
    rig: MarketRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare(rig, EMIM)

    async def slow(*args: Any, **kwargs: Any) -> Collection:
        await asyncio.sleep(10)
        raise AssertionError("never reached")

    monkeypatch.setattr(rig.collector.backfill, "run", slow)
    budgets = {**rig.settings.collectors.step_deadline_seconds, "backfill": 1}
    monkeypatch.setattr(rig.collector, "_settings", _with_budgets(rig.settings, budgets))
    record = await asyncio.wait_for(
        rig.collector.run("collect:XAMS", SESSION, venues=XAMS, steps=("backfill", "curves")),
        timeout=5,
    )
    backfill, curves = record.outcomes
    assert backfill.status == ERROR
    assert "outran its 1 s budget" in backfill.reason
    assert curves.status == SKIPPED


async def test_a_rerun_of_the_same_session_replaces_its_rows(rig: MarketRig) -> None:
    await seeded(rig, ASML)
    rig.market.bars(ASML, [800.0 * (1.01 if i % 2 else 0.99) for i in range(40)], last=SESSION)
    rig.market.quote(ASML, bid="799", ask="801", last="800")
    european_chain(rig)
    first = await rig.collector.run("collect:XAMS", SESSION, venues=XAMS)
    count = await rig.store(repos.chain_row_count, as_of=SESSION)
    second = await rig.collector.run("collect:XAMS", SESSION, venues=XAMS)
    assert first.status == second.status
    assert await rig.store(repos.chain_row_count, as_of=SESSION) == count
    assert await rig.store(repos.bar_count, ASML, as_of=SESSION) == 40
    assert (
        len(await rig.store(run_repo.runs_for, command="collect:XAMS", session_date=SESSION)) == 2
    )


async def test_an_outage_fails_every_step_and_writes_nothing(rig: MarketRig) -> None:
    await seeded(rig, EMIM, IBGM, ASML)
    rig.market.unavailable.add("snapshot")
    # History, strikes and contract details answer nothing at all.
    record = await rig.collector.run(
        "collect:XAMS", SESSION, venues=XAMS, steps=("backfill", "snapshots", "chains")
    )
    assert [o.status for o in record.outcomes] == [ERROR, ERROR, ERROR]
    assert record.status == ERROR
    assert await rig.store(repos.bar_count, EMIM, as_of=SESSION) == 0
    assert await rig.store(repos.chain_row_count, as_of=SESSION) == 0


async def test_a_dry_run_writes_nothing(rig: MarketRig) -> None:
    declare(rig, EMIM, IBGM, ASML)
    record = await rig.collector.run("collect:XAMS", SESSION, venues=XAMS, dry_run=True)
    assert record.outcomes
    assert await rig.store(repos.list_instruments, as_of=SESSION, include_superseded=True) == ()
    assert await rig.store(run_repo.runs_for, command="collect:XAMS", session_date=SESSION) == []


async def test_a_permanent_failure_is_not_retried(rig: MarketRig) -> None:
    calls = 0

    async def once() -> None:
        nonlocal calls
        calls += 1
        raise PermanentCollectionError("no spot")

    pacer = rig.collector.chains._pacer
    with pytest.raises(PermanentCollectionError):
        await pacer.retry("X", once)
    assert calls == 1
    slept = len(rig.session.clock.slept)  # type: ignore[attr-defined]

    async def transient() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("a hiccup")

    with pytest.raises(RuntimeError):
        await pacer.retry("X", transient)
    assert calls == 1 + 1 + rig.settings.collectors.retry.target_retries
    backoffs = rig.session.clock.slept[slept:]  # type: ignore[attr-defined]
    assert backoffs == [
        pacer.delay_for(n) for n in range(rig.settings.collectors.retry.target_retries)
    ]


# ── helpers ──


def _bar(day: date, close: float) -> HistoryBar:
    start = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)
    return HistoryBar(start=start, open=close, high=close, low=close, close=close, volume=1)


def timezone_of(rig: MarketRig, mic: str) -> Any:
    return rig.collector.backfill._calendars.venue(mic).zone


def _replace_instrument(settings: ObliskConfig, symbol: str, **update: Any) -> ObliskConfig:
    universe = settings.universe
    instruments = [
        i.model_copy(update=update) if i.symbol == symbol else i for i in universe.instruments
    ]
    return settings.model_copy(
        update={"universe": universe.model_copy(update={"instruments": instruments})}
    )


def _with_conid(settings: ObliskConfig, symbol: str, conid: int | None) -> ObliskConfig:
    return _replace_instrument(settings, symbol, conid=conid)


def _described(settings: ObliskConfig, symbol: str, description: str) -> ObliskConfig:
    return _replace_instrument(settings, symbol, description=description)


def _option_exchange(settings: ObliskConfig, symbol: str, exchange: str) -> ObliskConfig:
    return _replace_instrument(settings, symbol, option_exchange=exchange)


def _with_budgets(settings: ObliskConfig, budgets: dict[str, int]) -> ObliskConfig:
    collectors = settings.collectors.model_copy(update={"step_deadline_seconds": budgets})
    return settings.model_copy(update={"collectors": collectors})


def _collector_with(rig: MarketRig, settings: ObliskConfig) -> Any:
    from obliskiq.data.market.daily import MarketCollector

    return MarketCollector(
        settings,
        client=rig.client,
        worker=rig.worker,
        calendars=rig.collector.backfill._calendars,
        now=rig.now,
        sleep=rig.session.clock.sleep,
    )
