"""The account NAV: read from the ledger, recorded as a series, refused in the
wrong currency.

Nothing wrote ``nav_history``. The table has existed since migration 003 and its
only writer was a risk-engine method nothing in ``src/`` called, so on a store the
collectors filled alone every NAV reader got the same answer — none. The weekly
census was therefore scheduled to refuse forever, correctly and uselessly, and the
rolling-month drawdown breaker could only ever report ``n/a``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta

import pytest

from obliskiq.collect.nav import NAV_TARGET, NavCollector
from obliskiq.collect.runlog import CollectorRun
from obliskiq.collect.source import SOURCE_CP_API, AccountNav, FixtureMarketData
from obliskiq.collect.universe import UniverseSync
from obliskiq.compute.frames import latest_nav_observation
from obliskiq.config import Config
from obliskiq.store.nav import record_nav
from obliskiq.util.timeutil import utc_today

#: A round invented figure. A fixture is evidence about our parsing and never
#: about this account.
FIXTURE_NAV = 1_000_000.0


class StubMarketData(FixtureMarketData):
    """The fixture provider with the ledger reading replaced, and counted."""

    def __init__(self, root, observation, *, raises: Exception | None = None):
        super().__init__(root)
        self._observation = observation
        self._raises = raises
        self.reads = 0

    async def account_nav(self):
        self.reads += 1
        if self._raises is not None:
            raise self._raises
        return self._observation


async def run_nav(store, config, market_data, *, dry_run: bool = False):
    collector = NavCollector(store, config, market_data)
    with CollectorRun(store, "nav") as run:
        await collector.run(run, dry_run=dry_run)
    return run


def fast(config, **universe_overrides) -> Config:
    data = copy.deepcopy(config.as_dict())
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    data["collectors"]["universe"].update(universe_overrides)
    return Config(data)


# ── reading the ledger ──


async def test_the_base_row_is_decoded_from_the_string_it_arrives_as(market_data):
    """``netliquidationvalue`` is a STRING on this route, and the BASE row names
    itself BASE on ``currency`` AND ``secondkey`` — so the code comes from the
    rates. Measured 2026-09-10; the fixture used to encode the hope instead."""
    observation = await market_data.account_nav()

    assert observation is not None
    assert observation.nav_base == FIXTURE_NAV
    assert observation.currency == "EUR"


# ── which currency the BASE figure is in, when the row will not say ──
#
# The refusal below this section exists to stop a NAV in the wrong currency
# mis-sizing every gate. Measured live, the BASE row labels itself "BASE" on both
# keys, so the label alone could never disagree with account.base_currency and
# the refusal could never fire. These pin the arithmetic that replaced it, and
# every failure mode of that arithmetic falls back to the old answer of None.


def ledger_bytes(**rows) -> str:
    base = {"currency": "BASE", "secondkey": "BASE", "netliquidationvalue": "1"}
    return json.dumps({"acctId": "U0", "BASE": base, **rows})


async def test_the_currency_is_read_from_the_row_whose_rate_is_one(tmp_path):
    """The consolidation is INTO the currency that needs no conversion."""
    (tmp_path / "ledger.json").write_text(
        ledger_bytes(
            EUR={"currency": "EUR", "exchangerate": 1.0},
            USD={"currency": "USD", "exchangerate": "0.8612968"},
        )
    )

    observation = await FixtureMarketData(tmp_path).account_nav()

    assert observation is not None
    assert observation.currency == "EUR"


async def test_a_ledger_naming_no_currency_at_par_reports_none(tmp_path):
    """None is the answer that travelled before this identification existed, so
    a route that changes shape degrades to the old behaviour and not to a guess."""
    (tmp_path / "ledger.json").write_text(
        ledger_bytes(USD={"currency": "USD", "exchangerate": "0.86"})
    )

    observation = await FixtureMarketData(tmp_path).account_nav()

    assert observation is not None
    assert observation.currency is None


async def test_two_currencies_at_par_are_an_ambiguity_and_not_a_coin_flip(tmp_path):
    """A pegged pair would otherwise be resolved by dictionary order."""
    (tmp_path / "ledger.json").write_text(
        ledger_bytes(
            EUR={"currency": "EUR", "exchangerate": 1.0},
            CHF={"currency": "CHF", "exchangerate": 1},
        )
    )

    observation = await FixtureMarketData(tmp_path).account_nav()

    assert observation is not None
    assert observation.currency is None


async def test_a_rate_near_one_is_not_one(tmp_path):
    """No tolerance is declared, because a tolerance would be a threshold nothing
    measured. A currency the consolidation converted is not the base currency."""
    (tmp_path / "ledger.json").write_text(
        ledger_bytes(CHF={"currency": "CHF", "exchangerate": "0.999"})
    )

    assert (await FixtureMarketData(tmp_path).account_nav()).currency is None


async def test_a_route_that_does_name_the_code_is_believed_over_the_rates(tmp_path):
    """The rate arithmetic is the fallback for a route that will not say, never a
    correction to one that does."""
    (tmp_path / "ledger.json").write_text(
        json.dumps(
            {
                "acctId": "U0",
                "BASE": {"secondkey": "GBP", "netliquidationvalue": "1"},
                "EUR": {"currency": "EUR", "exchangerate": 1.0},
            }
        )
    )

    assert (await FixtureMarketData(tmp_path).account_nav()).currency == "GBP"


async def test_both_routes_resolve_the_currency_through_one_implementation(tmp_path):
    """They decoded it separately until the live reading showed the label is never
    a code; two copies of a rule just proven wrong is two places to fix next time."""
    from obliskiq.collect import source

    assert source.CpApiMarketData.account_nav.__code__.co_names.count("_nav_currency")
    assert source.FixtureMarketData.account_nav.__code__.co_names.count("_nav_currency")


async def test_a_ledger_with_no_base_row_reads_as_none_not_zero(tmp_path):
    (tmp_path / "ledger.json").write_text('{"USD": {"netliquidationvalue": "1"}}')

    assert await FixtureMarketData(tmp_path).account_nav() is None


async def test_a_base_row_with_no_value_reads_as_none_not_zero(tmp_path):
    (tmp_path / "ledger.json").write_text('{"BASE": {"cashbalance": "1"}}')

    assert await FixtureMarketData(tmp_path).account_nav() is None


# ── recording it ──


async def test_the_collector_writes_one_observation(store, fast_collect_config, market_data):
    run = await run_nav(store, fast_collect_config, market_data)

    assert run.status() == "ok"
    observation = latest_nav_observation(store)
    assert observation is not None
    assert observation.nav == FIXTURE_NAV
    assert observation.as_of == utc_today()


async def test_the_row_records_the_route_that_read_it(store, fast_collect_config, market_data):
    await run_nav(store, fast_collect_config, market_data)

    assert store.execute("SELECT source FROM nav_history").fetchone()[0] == SOURCE_CP_API


async def test_a_dry_run_reads_and_writes_nothing(store, fast_collect_config, fixtures_dir):
    market_data = StubMarketData(
        fixtures_dir, AccountNav(nav_base=FIXTURE_NAV, currency="EUR", account_id="U0")
    )

    run = await run_nav(store, fast_collect_config, market_data, dry_run=True)

    assert run.status() == "ok"
    assert market_data.reads == 1
    assert store.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0] == 0


def test_re_reading_the_same_day_corrects_it_rather_than_duplicating(store):
    """One row per day. Two observations of one day would leave the breaker
    silently preferring whichever it happened to read first."""
    record_nav(store, as_of=date(2026, 1, 15), nav=100.0)
    record_nav(store, as_of=date(2026, 1, 15), nav=110.0)

    rows = store.execute("SELECT as_of, nav_base FROM nav_history").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("2026-01-15", 110.0)]


# ── what it refuses ──


async def test_a_currency_that_is_not_the_base_currency_is_refused(
    store, fast_collect_config, fixtures_dir
):
    """A NAV in the wrong currency does not fail — it mis-sizes every gate that
    reads it, by an exchange rate nobody would see. Refused rather than scaled."""
    base = fast_collect_config.require_str("account.base_currency")
    other = "USD" if base != "USD" else "GBP"
    market_data = StubMarketData(
        fixtures_dir, AccountNav(nav_base=FIXTURE_NAV, currency=other, account_id="U0")
    )

    run = await run_nav(store, fast_collect_config, market_data)

    assert run.status() != "ok"
    assert store.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0] == 0
    reason = next(outcome.reason for outcome in run.outcomes if not outcome.ok) or ""
    assert other in reason and base in reason


async def test_an_unreported_currency_is_recorded_rather_than_assumed_wrong(
    store, fast_collect_config, fixtures_dir
):
    """None is a third answer: the route said nothing, which is not a claim that
    it disagreed."""
    market_data = StubMarketData(
        fixtures_dir, AccountNav(nav_base=FIXTURE_NAV, currency=None, account_id="U0")
    )

    run = await run_nav(store, fast_collect_config, market_data)

    assert run.status() == "ok"
    assert latest_nav_observation(store) is not None


async def test_a_ledger_that_cannot_be_read_fails_the_target_and_writes_nothing(
    store, fast_collect_config, fixtures_dir
):
    market_data = StubMarketData(fixtures_dir, None, raises=RuntimeError("upstream down"))

    run = await run_nav(store, fast_collect_config, market_data)

    assert run.status() != "ok"
    assert store.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0] == 0
    assert [outcome.target for outcome in run.outcomes if not outcome.ok] == [NAV_TARGET]


async def test_an_absent_nav_is_a_failure_and_not_a_nav_of_zero(
    store, fast_collect_config, fixtures_dir
):
    run = await run_nav(store, fast_collect_config, StubMarketData(fixtures_dir, None))

    assert run.status() != "ok"
    assert store.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0] == 0


# ── the two cadences ──


async def test_the_universe_sync_records_one_too(store, fast_collect_config, market_data):
    """It already resolves the account for the portfolio union, and it runs an
    hour before the weekly census."""
    with CollectorRun(store, "universe") as run:
        await UniverseSync(store, fast_collect_config, market_data).run(run=run)

    assert latest_nav_observation(store) is not None
    assert any(outcome.target == NAV_TARGET for outcome in run.outcomes)


async def test_the_universe_sync_respects_the_config_switch(store, config, market_data):
    with CollectorRun(store, "universe") as run:
        await UniverseSync(store, fast(config, record_nav=False), market_data).run(run=run)

    assert latest_nav_observation(store) is None
    assert not any(outcome.target == NAV_TARGET for outcome in run.outcomes)


def test_nav_leads_the_daily_sequence():
    """``/portfolio/{id}/ledger`` serves TODAY's figure and no other, so a missed
    day is an observation nobody can ask for again — and it is one call."""
    from obliskiq.collect.cli import DAILY_SEQUENCE

    assert DAILY_SEQUENCE[0] == "nav"


def test_the_census_computes_a_ceiling_once_a_collector_has_written_a_nav(seeded_store, config):
    """The whole point. Before this, the scheduled census refused every week on a
    store the collectors filled alone — a chain in the store and no NAV to size
    it against is exactly the state the collectors leave behind."""
    from obliskiq.compute.universe_census import UniverseCensus

    as_of = date(2026, 1, 15)
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, "
            "right, bid, ask, open_interest, volume, source) "
            "VALUES (?, 1, ?, 100.0, 'C', 1.0, 1.1, 500, 50, 'cp_api')",
            (as_of.isoformat(), (as_of + timedelta(days=120)).isoformat()),
        )

    census = UniverseCensus(seeded_store, config)
    assert "no NAV is available" in (census.run(as_of=as_of).reason or "")

    record_nav(seeded_store, as_of=as_of, nav=FIXTURE_NAV, source=SOURCE_CP_API)

    result = census.run(as_of=as_of)
    assert "no NAV is available" not in (result.reason or "")
    assert result.nav == FIXTURE_NAV
    assert result.available_budget is not None


@pytest.mark.parametrize("writer", ["store", "risk_engine"])
def test_both_names_for_the_writer_are_one_implementation(seeded_store, config, writer):
    """``RiskEngine.record_nav`` delegates. Two upserts would eventually disagree
    about which currency the figure was in."""
    if writer == "store":
        record_nav(seeded_store, as_of=date(2026, 2, 1), nav=42.0, source="mcp")
    else:
        from obliskiq.compute.penalty import PenaltyLedger
        from obliskiq.compute.risk_engine import RiskEngine

        RiskEngine(seeded_store, config, penalty=PenaltyLedger(seeded_store, config)).record_nav(
            as_of=date(2026, 2, 1), nav=42.0, source="mcp"
        )

    row = seeded_store.execute("SELECT nav_base, source FROM nav_history").fetchone()
    assert (row[0], row[1]) == (42.0, "mcp")
