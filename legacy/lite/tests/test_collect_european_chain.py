"""The European chains: the venue that is asked for, and what silence costs.

Phase 4 collected 3,832 option rows and none of them were European. `ASML`
collected its NEW YORK chain under a row declaring Amsterdam, `SAN` and `TTE`
collected nothing at all, and all three targets reported `ok` — which is the part
that matters, because option-chain history cannot be retrieved retrospectively at
any price and a run log saying "collected" is the only thing that would ever have
raised the alarm.

Three separate mechanisms failed together, and each is pinned here:

* the OPT section was read from whichever LISTING a symbol search matched, while
  the strikes were requested against the conid the universe holds;
* the exchange list travelling with that section decided the venue, so a European
  underlying was asked for its options on SMART;
* a target that resolved no contract at all was counted as a success.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from obliskiq.collect.chain_collector import ChainCollector, ChainTarget, chain_targets
from obliskiq.collect.runlog import CollectorRun
from obliskiq.collect.source import FixtureMarketData
from obliskiq.collect.vol import FREQ_DAILY
from obliskiq.config import Config

SESSION = date(2026, 1, 15)
SPY_CONID = 756733


def config_for(config) -> Config:
    data = copy.deepcopy(config.as_dict())
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    return Config(data)


class RecordingMarketData(FixtureMarketData):
    """The fixture provider, with the exchange every option call was sent."""

    def __init__(self, root, *, exchanges=None, strikes_empty=False):
        super().__init__(root)
        self.sent: list[str] = []
        self._exchanges = exchanges
        self._strikes_empty = strikes_empty

    async def option_months(self, symbol, *, conid):
        months, exchanges = await super().option_months(symbol, conid=conid)
        return months, (self._exchanges if self._exchanges is not None else exchanges)

    async def option_strikes(self, conid, *, month, exchange):
        self.sent.append(exchange)
        if self._strikes_empty:
            return {"call": [], "put": []}
        return await super().option_strikes(conid, month=month, exchange=exchange)

    async def option_contracts(self, conid, *, month, exchange, strike, right):
        self.sent.append(exchange)
        return await super().option_contracts(
            conid, month=month, exchange=exchange, strike=strike, right=right
        )


def seed_bars(store, contract_id: int, count: int, *, start: float):
    rows, price, day = [], start, SESSION - timedelta(days=count * 2)
    for index in range(count):
        price *= 1.008 if index % 2 else 0.993
        rows.append((contract_id, f"{day.isoformat()}T00:00:00Z", FREQ_DAILY, price))
        day += timedelta(days=2)
    with store.transaction() as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO bars (contract_id, ts, freq, close) VALUES (?, ?, ?, ?)", rows
        )


@pytest.fixture()
def chain_store(seeded_universe):
    seed_bars(seeded_universe, SPY_CONID, 60, start=590.0)
    return seeded_universe


async def collect(store, cfg, market_data, *, only=None):
    collector = ChainCollector(store, cfg, market_data)
    with CollectorRun(store, "chain") as run:
        await collector.run(run, only=only, today=SESSION)
    return run


def declare_option_exchange(store, symbol: str, venue: str | None) -> None:
    with store.transaction() as connection:
        connection.execute(
            "UPDATE universe SET option_exchange = ? WHERE symbol = ?", (venue, symbol)
        )


# ── the declaration reaches the wire ──


async def test_the_declared_option_exchange_is_what_is_sent(chain_store, config, fixtures_dir):
    """And the row's own listing venue is not, and neither is SMART."""
    declare_option_exchange(chain_store, "SPY", "FTA")
    market_data = RecordingMarketData(fixtures_dir)

    run = await collect(chain_store, config_for(config), market_data, only=["SPY"])

    assert all(outcome.ok for outcome in run.outcomes)
    assert set(market_data.sent) == {"FTA"}


async def test_a_declared_venue_the_search_never_mentions_is_still_sent(
    chain_store, config, fixtures_dir
):
    """The inversion that fixes SAN and TTE.

    The exchange list belongs to whichever listing the SYMBOL search matched, so
    for a European underlying it can be a true statement about New York and a
    false one here. A declared venue therefore outranks it rather than being
    validated against it — validating against it is what sent SMART.
    """
    declare_option_exchange(chain_store, "SPY", "MEFFRV")
    market_data = RecordingMarketData(fixtures_dir, exchanges=("SMART", "AMEX", "BATS"))

    await collect(chain_store, config_for(config), market_data, only=["SPY"])

    assert set(market_data.sent) == {"MEFFRV"}


async def test_an_undeclared_row_keeps_the_listing_venue_then_the_reported_one(
    chain_store, config, fixtures_dir
):
    """Nothing changes for the US rows, which have never declared a venue."""
    declare_option_exchange(chain_store, "SPY", None)
    market_data = RecordingMarketData(fixtures_dir, exchanges=("NASDAQ", "SMART"))

    await collect(chain_store, config_for(config), market_data, only=["SPY"])

    # ARCA is SPY's listing venue and is not in the list, so the first reported
    # exchange wins — the pre-existing rule, unchanged.
    assert set(market_data.sent) == {"NASDAQ"}


def test_the_declared_venue_reaches_the_collector_from_the_universe_row(chain_store):
    declare_option_exchange(chain_store, "SPY", "FTA")
    target = next(t for t in chain_targets(chain_store, only=["SPY"]))
    assert target.option_exchange == "FTA"


def test_an_undeclared_venue_is_none_and_not_a_guess(chain_store):
    declare_option_exchange(chain_store, "SPY", None)
    target = next(t for t in chain_targets(chain_store, only=["SPY"]))
    assert target.option_exchange is None


# ── the venue is recorded, not inferred ──


async def test_every_row_records_the_venue_it_was_collected_from(chain_store, config, fixtures_dir):
    """`source` says which ROUTE was asked; this says what VENUE answered.

    Without it, a US chain collected under a European row is invisible in the
    data and survives only in a run log that rotates away.
    """
    declare_option_exchange(chain_store, "SPY", "FTA")
    await collect(chain_store, config_for(config), RecordingMarketData(fixtures_dir), only=["SPY"])

    venues = {
        row[0]
        for row in chain_store.execute("SELECT DISTINCT option_exchange FROM chain_snapshots")
    }
    assert venues == {"FTA"}


# ── silence is a failure ──


async def test_a_target_that_resolves_no_contract_is_reported_failed(
    chain_store, config, fixtures_dir
):
    """SAN and TTE reported ok over zero rows on 2026-09-10. Never again."""
    market_data = RecordingMarketData(fixtures_dir, strikes_empty=True)

    run = await collect(chain_store, config_for(config), market_data, only=["SPY"])

    failed = [outcome for outcome in run.outcomes if not outcome.ok]
    assert [outcome.target for outcome in failed] == ["SPY"]
    assert "resolved no option contract" in (failed[0].reason or "")


async def test_a_target_that_resolves_nothing_writes_neither_half(
    chain_store, config, fixtures_dir
):
    """Not the chain rows and not the underlying snapshot.

    The two commit together precisely so a snapshot with no chain against it
    cannot exist — that half-state feeds the self-computed IV percentile and is
    indistinguishable from a chain that legitimately returned nothing.
    """
    market_data = RecordingMarketData(fixtures_dir, strikes_empty=True)

    await collect(chain_store, config_for(config), market_data, only=["SPY"])

    for table in ("chain_snapshots", "underlying_snapshots"):
        count = chain_store.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 0, f"{table} holds a row for a target that collected nothing"


async def test_the_failure_names_the_venue_and_the_list_it_disagreed_with(
    chain_store, config, fixtures_dir
):
    """Whoever reads this at 03:00 needs both halves to tell a wrong venue from
    an empty one."""
    declare_option_exchange(chain_store, "SPY", "MEFFRV")
    market_data = RecordingMarketData(fixtures_dir, exchanges=("SMART", "AMEX"), strikes_empty=True)

    run = await collect(chain_store, config_for(config), market_data, only=["SPY"])

    reason = next(outcome.reason for outcome in run.outcomes if not outcome.ok) or ""
    assert "MEFFRV" in reason
    assert "SMART" in reason


# ── the section belongs to the conid, or there is none ──


async def test_an_underlying_whose_conid_has_no_section_collects_nothing_and_says_so(
    chain_store, config, fixtures_dir
):
    """The fixture section is keyed on SPY's real conid; ask under another and the
    provider refuses rather than handing back a foreign listing's months."""

    class WrongConid(FixtureMarketData):
        async def option_months(self, symbol, *, conid):
            return await super().option_months(symbol, conid=conid + 1)

    run = await collect(chain_store, config_for(config), WrongConid(fixtures_dir), only=["SPY"])

    failed = [outcome for outcome in run.outcomes if not outcome.ok]
    assert [outcome.target for outcome in failed] == ["SPY"]
    assert "reports no option months" in (failed[0].reason or "")


def test_the_venue_precedence_is_stated_once_rather_than_inferred():
    """Declared, then the listing venue if the chain lists it, then what it
    reported first. The last rung is where SMART comes from, and is why the first
    exists at all."""
    collector = ChainCollector.__new__(ChainCollector)
    declared = ChainTarget(
        contract_id=1, symbol="SAN", exchange="BM", listed_on=None, option_exchange="EUREX"
    )
    assert collector._exchange_for(declared, ("SMART", "AMEX", "BM")) == "EUREX"

    undeclared = ChainTarget(contract_id=1, symbol="SAN", exchange="BM", listed_on=None)
    assert collector._exchange_for(undeclared, ("SMART", "AMEX", "BM")) == "BM"
    assert collector._exchange_for(undeclared, ("SMART", "AMEX")) == "SMART"

    with pytest.raises(ValueError, match="no option exchange known"):
        collector._exchange_for(
            ChainTarget(contract_id=1, symbol="SAN", exchange=None, listed_on=None), ()
        )
