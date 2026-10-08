"""The universe resolver: seeding, the portfolio union, and reconciliation.

Four separations are load-bearing and each has a way of failing silently:

* a macro series must never reach the IBKR conid resolver;
* an option or a note held in the account is not an underlying root;
* a PRIIPs-restricted ETF still gets its chain collected;
* a declared row never expires, a closed position eventually does.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
import dataclasses
import re
from pathlib import Path

import pytest
import yaml

from obliskiq.collect.runlog import CollectorRun
from obliskiq.collect.universe import (
    ORIGIN_DECLARED,
    ORIGIN_PORTFOLIO,
    UniverseSync,
    assert_seeded,
    load_universe_file,
)
from obliskiq.config import Config
from obliskiq.errors import ConfigError, StoreError

MINIMAL = {
    "instruments": [
        {"symbol": "SPY", "asset_class": "ETF", "currency": "USD", "exchange": "ARCA"},
    ],
    "futures_roots": [{"root": "CL", "exchange": "NYMEX", "currency": "USD", "bucket": "energy"}],
}


def write_universe(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "universe.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def config_with(config, path: Path, **universe_overrides) -> Config:
    data = copy.deepcopy(config.as_dict())
    data["collectors"]["universe"]["path"] = str(path)
    data["collectors"]["universe"].update(universe_overrides)
    data["collectors"].update(
        {
            "target_backoff_seconds": 0.0,
            "target_backoff_max_seconds": 0.0,
            "request_spacing_seconds": 0.0,
        }
    )
    return Config(data)


async def sync(store, cfg, market_data) -> CollectorRun:
    with CollectorRun(store, "universe") as run:
        await UniverseSync(store, cfg, market_data).run(run=run)
    return run


# ── the macro namespace guard ──


def test_a_macro_series_declared_as_an_instrument_is_refused(config, tmp_path):
    """€STR has no conid. Handing it to /iserver/secdef/search would resolve it to
    whatever ticker looks closest, and that row would then generate chain requests
    forever."""
    payload = copy.deepcopy(MINIMAL)
    payload["instruments"].append({"symbol": "ESTR", "asset_class": "STK", "currency": "EUR"})
    with pytest.raises(ConfigError, match="collides with the macro series"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_the_risk_free_rate_series_is_also_protected(config, tmp_path):
    """macro.risk_free_rate names its series separately from macro.series; both
    namespaces are off limits."""
    series = config.get("macro.risk_free_rate.series_by_currency")["USD"]
    payload = copy.deepcopy(MINIMAL)
    payload["instruments"].append({"symbol": series, "asset_class": "STK", "currency": "USD"})
    with pytest.raises(ConfigError, match="collides with the macro series"):
        load_universe_file(write_universe(tmp_path, payload), config)


async def test_the_resolver_never_searches_for_a_macro_series(store, config, tmp_path, market_data):
    """The positive control: after a sync, no macro series id appears in the store
    and none was ever looked up."""
    cfg = config_with(config, write_universe(tmp_path, MINIMAL))
    await sync(store, cfg, market_data)

    macro_ids = {str(entry["id"]).upper() for entry in config.require_list("macro.series")}
    symbols = {row[0].upper() for row in store.execute("SELECT symbol FROM universe")}
    assert not (symbols & macro_ids)
    assert not any(call[1].upper() in macro_ids for call in market_data.calls)


# ── explicit conid ──


async def test_an_explicit_conid_bypasses_search_entirely(store, config, tmp_path, market_data):
    """The escape hatch for dual-listed and ambiguous symbols: secdef/search ranks
    by its own relevance, which is not the same as correctness."""
    payload = copy.deepcopy(MINIMAL)
    payload["instruments"] = [
        {"symbol": "SAN", "asset_class": "STK", "currency": "EUR", "conid": 999_111}
    ]
    cfg = config_with(config, write_universe(tmp_path, payload))
    await sync(store, cfg, market_data)

    row = store.execute("SELECT contract_id FROM universe WHERE symbol = 'SAN'").fetchone()
    assert row["contract_id"] == 999_111
    # The conids fixture is what a search would have consulted; it was never opened.
    assert not any(call[1] == "conids" for call in market_data.calls)


async def test_a_symbol_that_will_not_resolve_fails_its_target_and_names_the_remedy(
    store, config, tmp_path, market_data
):
    payload = copy.deepcopy(MINIMAL)
    payload["instruments"] = [{"symbol": "NOSUCHTHING", "asset_class": "STK", "currency": "USD"}]
    cfg = config_with(config, write_universe(tmp_path, payload))
    run = await sync(store, cfg, market_data)

    failed = [o for o in run.outcomes if not o.ok]
    assert failed and "explicit conid" in failed[0].reason


# ── the portfolio union ──


async def test_only_underlying_security_types_enter_the_universe(
    store, config, tmp_path, market_data
):
    """An option position, a FOP, a structured note and a cash line are HOLDINGS.
    A chain request against an option conid is meaningless and a bar series against
    a note is unobtainable."""
    cfg = config_with(config, write_universe(tmp_path, MINIMAL))
    await sync(store, cfg, market_data)

    stored = {
        row["contract_id"]: row["asset_class"]
        for row in store.execute("SELECT contract_id, asset_class FROM universe")
    }
    # From tests/fixtures/ibkr/positions.json: the OPT, FOP, BOND and CASH conids.
    for excluded in (712345678, 712345679, 712345680, 712345681):
        assert excluded not in stored
    # ...and the STK lines did come through.
    assert stored.get(265598) == "STK"
    assert stored.get(495512563) == "STK"


async def test_portfolio_rows_are_distinguishable_from_declared_ones(
    store, config, tmp_path, market_data
):
    cfg = config_with(config, write_universe(tmp_path, MINIMAL))
    await sync(store, cfg, market_data)

    origins = {
        row["symbol"]: row["origin"] for row in store.execute("SELECT symbol, origin FROM universe")
    }
    assert origins["SPY"] == ORIGIN_DECLARED  # declared, even though it is also held
    assert origins["AAPL"] == ORIGIN_PORTFOLIO


async def test_a_portfolio_row_never_gets_chain_collection_on_its_own(
    store, config, tmp_path, market_data
):
    """Chain collection is a deliberate choice per instrument, not a side effect of
    happening to hold something."""
    cfg = config_with(config, write_universe(tmp_path, MINIMAL))
    await sync(store, cfg, market_data)
    row = store.execute(
        "SELECT collect_chain, collect_bars FROM universe WHERE symbol = 'AAPL'"
    ).fetchone()
    assert row["collect_chain"] == 0
    assert row["collect_bars"] == 1


async def test_the_union_can_be_switched_off(store, config, tmp_path, market_data):
    cfg = config_with(config, write_universe(tmp_path, MINIMAL), portfolio_union=False)
    await sync(store, cfg, market_data)
    symbols = {row[0] for row in store.execute("SELECT symbol FROM universe")}
    assert symbols == {"SPY"}


# ── reconciliation ──


async def test_a_stale_portfolio_row_stops_being_collected(store, config, tmp_path, market_data):
    """A closed position that keeps drawing calls burns API budget forever for data
    nobody will read."""
    cfg = config_with(config, write_universe(tmp_path, MINIMAL), portfolio_stale_days=7)
    await sync(store, cfg, market_data)

    with store.transaction() as connection:
        connection.execute(
            "UPDATE universe SET last_seen_at = ? WHERE symbol = 'AAPL'",
            ("2020-01-01T00:00:00Z",),
        )
    await sync(store, cfg, market_data)  # AAPL is still in the fixture, so it revives
    assert (
        store.execute("SELECT collect_bars FROM universe WHERE symbol = 'AAPL'").fetchone()[
            "collect_bars"
        ]
        == 1
    )


async def test_a_position_no_longer_held_is_retired_but_never_deleted(
    store, config, tmp_path, market_data, monkeypatch
):
    """bars, chain_snapshots and curve_snapshots all cascade off universe, so
    deleting a row to tidy up would destroy the irreplaceable history collected
    against it."""
    cfg = config_with(config, write_universe(tmp_path, MINIMAL), portfolio_stale_days=7)
    await sync(store, cfg, market_data)
    assert store.execute("SELECT 1 FROM universe WHERE symbol = 'AAPL'").fetchone()

    with store.transaction() as connection:
        connection.execute(
            "UPDATE universe SET last_seen_at = ? WHERE symbol = 'AAPL'",
            ("2020-01-01T00:00:00Z",),
        )
    # The account no longer reports AAPL.
    original = market_data.portfolio_positions

    async def without_aapl():
        return [row for row in await original() if row.get("ticker") != "AAPL"]

    monkeypatch.setattr(market_data, "portfolio_positions", without_aapl)
    await sync(store, cfg, market_data)

    row = store.execute(
        "SELECT collect_chain, collect_bars FROM universe WHERE symbol = 'AAPL'"
    ).fetchone()
    assert row is not None, "the row must survive; its history hangs off it"
    assert row["collect_chain"] == 0 and row["collect_bars"] == 0


async def test_a_declared_row_never_expires(store, config, tmp_path, market_data):
    """A declared instrument the account does not hold is still something the grid
    wants a reading on."""
    cfg = config_with(config, write_universe(tmp_path, MINIMAL), portfolio_stale_days=0)
    payload = copy.deepcopy(MINIMAL)
    payload["instruments"][0]["collect_chain"] = True
    cfg = config_with(config, write_universe(tmp_path, payload), portfolio_stale_days=0)

    await sync(store, cfg, market_data)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE universe SET last_seen_at = ? WHERE symbol = 'SPY'",
            ("2020-01-01T00:00:00Z",),
        )
    await sync(store, cfg, market_data)

    row = store.execute(
        "SELECT collect_chain, origin FROM universe WHERE symbol = 'SPY'"
    ).fetchone()
    assert row["origin"] == ORIGIN_DECLARED
    assert row["collect_chain"] == 1


# ── validation and loud failure ──


def test_the_shipped_universe_file_loads_and_declares_a_chain(config):
    universe = load_universe_file("universe.yaml", config)
    assert universe.instruments and universe.futures_roots
    assert any(i.collect_chain for i in universe.instruments)


def test_a_priips_restricted_etf_is_still_chain_collected(config):
    """An EU retail account cannot buy SPY for want of a KID but may trade listed
    options on it — which is the whole reason the option sleeve exists. PRIIPs gates
    the candidate list, never collection."""
    universe = load_universe_file("universe.yaml", config)
    restricted = [i for i in universe.instruments if i.priips_restricted]
    assert restricted, "the shipped universe should exercise this"
    assert all(i.collect_chain for i in restricted)


async def test_a_priips_restricted_underlying_reaches_the_chain_target_list(
    seeded_universe, config
):
    from obliskiq.collect.chain_collector import chain_targets

    targets = {t.symbol for t in chain_targets(seeded_universe)}
    restricted = {
        row["symbol"]
        for row in seeded_universe.execute(
            "SELECT symbol FROM universe WHERE priips_restricted = 1"
        )
    }
    assert restricted and restricted <= targets


def test_an_untradeable_instrument_must_say_why(config, tmp_path):
    """A silent exclusion is a coverage gap nobody can audit later."""
    payload = copy.deepcopy(MINIMAL)
    payload["instruments"][0]["tradable"] = False
    with pytest.raises(ConfigError, match="untradeable_reason"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_an_option_asset_class_is_refused_in_the_declared_file(config, tmp_path):
    payload = copy.deepcopy(MINIMAL)
    payload["instruments"][0]["asset_class"] = "OPT"
    with pytest.raises(ConfigError, match="asset_class"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_a_duplicate_symbol_is_refused(config, tmp_path):
    payload = copy.deepcopy(MINIMAL)
    payload["instruments"].append(dict(payload["instruments"][0]))
    with pytest.raises(ConfigError, match="declared twice"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_a_futures_root_outside_the_configured_buckets_is_refused(config, tmp_path):
    payload = copy.deepcopy(MINIMAL)
    payload["futures_roots"][0]["bucket"] = "agriculture"  # replaced by three buckets
    with pytest.raises(ConfigError, match=re.escape("commodities.buckets")):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_a_missing_universe_file_names_the_config_key(config):
    with pytest.raises(ConfigError, match=re.escape("collectors.universe.path")):
        load_universe_file("/nonexistent/universe.yaml", config)


def test_a_collector_on_an_empty_universe_fails_loudly_with_the_remedy(store, config):
    """A run reporting success over zero targets is indistinguishable from a healthy
    one in the log."""
    with pytest.raises(StoreError, match="obliskiq-collect universe"):
        assert_seeded(store, config)


def test_the_seeded_check_can_be_switched_off(store, config):
    data = copy.deepcopy(config.as_dict())
    data["collectors"]["universe"]["require_seeded"] = False
    assert_seeded(store, Config(data))


async def test_futures_roots_are_seeded_from_the_same_file(store, config, tmp_path, market_data):
    cfg = config_with(config, write_universe(tmp_path, MINIMAL))
    await sync(store, cfg, market_data)
    row = store.execute("SELECT root, exchange, bucket FROM futures_roots").fetchone()
    assert (row["root"], row["exchange"], row["bucket"]) == ("CL", "NYMEX", "energy")


async def test_a_second_sync_writes_no_duplicate_rows(store, config, tmp_path, market_data):
    cfg = config_with(config, write_universe(tmp_path, MINIMAL))
    await sync(store, cfg, market_data)
    before = store.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    await sync(store, cfg, market_data)
    assert store.execute("SELECT COUNT(*) FROM universe").fetchone()[0] == before


# ── leveraged and inverse ETPs, rejected AT INGESTION ──
#
# Not at recommendation time. A row that never enters the universe cannot be
# recommended by a later code path that forgot to check, and there is always a
# later code path.


def test_a_declared_leveraged_etp_never_enters_the_universe(tmp_path, config):
    universe_file = tmp_path / "universe.yaml"
    universe_file.write_text(
        yaml.safe_dump(
            {
                "instruments": [
                    {
                        "symbol": "TQQQ",
                        "asset_class": "ETF",
                        "currency": "USD",
                        "leveraged": True,
                        "tradable": False,
                        "untradeable_reason": "3x daily",
                    },
                    {"symbol": "ASML", "asset_class": "STK", "currency": "EUR"},
                ]
            }
        )
    )
    loaded = load_universe_file(universe_file, config)

    assert [i.symbol for i in loaded.instruments] == ["ASML"]
    assert [r.symbol for r in loaded.rejected] == ["TQQQ"]
    assert "daily reset" in loaded.rejected[0].reason


def test_a_declared_inverse_etp_never_enters_the_universe(tmp_path, config):
    universe_file = tmp_path / "universe.yaml"
    universe_file.write_text(
        yaml.safe_dump(
            {
                "instruments": [
                    {
                        "symbol": "SQQQ",
                        "asset_class": "ETF",
                        "currency": "USD",
                        "inverse": True,
                        "tradable": False,
                        "untradeable_reason": "inverse",
                    }
                ]
            }
        )
    )
    loaded = load_universe_file(universe_file, config)
    assert loaded.instruments == []
    assert loaded.rejected[0].symbol == "SQQQ"


def test_the_prohibition_matches_the_config_list(config):
    prohibited = config.require_list("instruments.prohibited")
    assert "leveraged_etps" in prohibited
    assert "inverse_etps" in prohibited


def test_the_markers_match_a_description_and_never_a_bare_ticker(config):
    """A ticker promises nothing: BULL is a substring of plenty of innocent names."""
    from obliskiq.instruments import LeveragedInverseFilter

    leverage_filter = LeveragedInverseFilter(config)

    assert leverage_filter.check_description("TQQQ", "ProShares UltraPro QQQ") is not None
    assert leverage_filter.check_description("SQQQ", "Direxion Daily Bear 3X Shares") is not None
    assert leverage_filter.check_description("ASML", "ASML Holding NV") is None
    # No description is not evidence of leverage; rejecting on absence would drop
    # every row the resolver failed to name.
    assert leverage_filter.check_description("MYSTERY", None) is None


async def test_a_held_leveraged_etp_is_refused_and_recorded(
    store, fast_collect_config, market_data, monkeypatch
):
    """A leveraged position in the account is a fact, not a licence to fold it in."""

    async def positions():
        return [
            {
                "conid": 999,
                "ticker": "TQQQ",
                "assetClass": "ETF",
                "currency": "USD",
                "contractDesc": "PROSHARES ULTRAPRO QQQ",
            },
            {
                "conid": 1001,
                "ticker": "ASML",
                "assetClass": "STK",
                "currency": "EUR",
                "contractDesc": "ASML HOLDING NV",
            },
        ]

    monkeypatch.setattr(market_data, "portfolio_positions", positions, raising=False)

    with CollectorRun(store, "universe") as run:
        await UniverseSync(store, fast_collect_config, market_data).run(run=run)

    symbols = {row[0] for row in store.execute("SELECT symbol FROM universe").fetchall()}
    assert "TQQQ" not in symbols
    assert 999 not in {
        row[0] for row in store.execute("SELECT contract_id FROM universe").fetchall()
    }

    rejected = store.execute(
        "SELECT symbol, reason FROM universe_rejections WHERE origin = 'portfolio_sync'"
    ).fetchall()
    assert [row["symbol"] for row in rejected] == ["TQQQ"]
    assert "marker" in rejected[0]["reason"]


def test_the_shipped_universe_declares_no_leveraged_or_inverse_row(config):
    loaded = load_universe_file(config.require_str("collectors.universe.path"), config)
    assert loaded.rejected == []
    assert not any(i.leveraged or i.inverse for i in loaded.instruments)


# ── history_exchange: the listing venue and the request are not the same thing ──


def test_history_exchange_defaults_to_the_listing_venue(config, tmp_path):
    """A row that says nothing must be requested exactly as it always was."""
    payload = copy.deepcopy(MINIMAL)
    declared = load_universe_file(write_universe(tmp_path, payload), config)
    instrument = declared.instruments[0]
    assert instrument.exchange == "ARCA"
    assert instrument.history_exchange == "ARCA"


def test_an_explicitly_null_history_exchange_means_send_none(config, tmp_path):
    """The TSEJ case, and the reason an absent key and a null one must differ.

    Probed live 2026-08-29: NF TPX-17 contracts return "No market data
    permissions" with exchange=TSEJ and full daily history with the exchange
    omitted. `exchange` stays the listing venue because that is what identifies
    the contract; only the history request changes.
    """
    payload = copy.deepcopy(MINIMAL)
    payload["instruments"][0]["exchange"] = "TSEJ"
    payload["instruments"][0]["history_exchange"] = None
    declared = load_universe_file(write_universe(tmp_path, payload), config)
    instrument = declared.instruments[0]
    assert instrument.exchange == "TSEJ"
    assert instrument.history_exchange is None


def test_the_shipped_file_omits_the_exchange_on_every_tsej_row(config):
    """Declared once per row, so no code path can forget it for one of eleven."""
    declared = load_universe_file(Path("universe.yaml"), config)
    tsej = [i for i in declared.instruments if i.exchange == "TSEJ"]
    assert tsej, "the shipped file should declare the TOPIX-17 funds"
    assert all(i.history_exchange is None for i in tsej)


def test_the_shipped_file_omits_the_exchange_on_the_two_european_rows_that_need_it(config):
    """SAN and TTE, measured both ways on the collection route before declaring.

    [CP 2026-09-10] ``exchange=BM`` and ``exchange=SBF`` each answer HTTP 500
    ``{"error":"Chart data unavailable"}``; the same conids with the exchange
    omitted serve daily bars. ASML is deliberately NOT in this list: it answers
    on ``AEB`` and with the exchange omitted alike, so its bars were a conid
    problem and there is nothing here to declare.
    """
    declared = load_universe_file(Path("universe.yaml"), config)
    rows = {i.symbol: i for i in declared.instruments}

    assert rows["SAN"].exchange == "BM" and rows["SAN"].history_exchange is None
    assert rows["TTE"].exchange == "SBF" and rows["TTE"].history_exchange is None
    assert rows["ASML"].history_exchange == "AEB"


def test_every_collecting_row_whose_ticker_is_ambiguous_pins_its_conid(config):
    """The rows a live search resolves to the wrong instrument, pinned.

    Measured [CP 2026-09-10] across all 74 declared rows: **19** resolve to a
    different conid with the declared venue than without it, because every
    listing of a ticker answers that bare ticker. Eighteen were already pinned.
    The nineteenth was EEM, and the row it resolved to was not another listing of
    the fund — it was `690179584`, "Micro Ether Euro", an IND on CME in EUR,
    under a row declaring an ARCA USD emerging-markets ETF with `collect_chain`
    and `collect_bars` both set.

    The venue tiebreak in `resolve_conid` fixes it and this pin makes it a
    declaration, which is what every other row in that list already has.
    """
    declared = load_universe_file(Path("universe.yaml"), config)
    rows = {i.symbol: i for i in declared.instruments}

    assert rows["EEM"].conid == 6604766
    assert rows["EEM"].exchange == "ARCA"


# ── the declared venue reaches the search that resolves the conid ──


async def test_the_declared_exchange_travels_with_the_conid_search(store, config, fixtures_dir):
    """The wiring, and the fact that makes it load-bearing.

    Measured [CP 2026-09-10]: every row of a dual-listed search answers the bare
    ticker, so the exact-match rule returns the API's first choice — the US line
    for ASML, SAN and TTE alike. The listing venue was declared on the universe
    row the whole time and never reached the search. A row with an explicit
    `conid:` still issues no search at all, which is the case below this one.
    """
    from obliskiq.collect.source import FixtureMarketData
    from obliskiq.collect.universe import UniverseSync

    class Recording(FixtureMarketData):
        def __init__(self, root):
            super().__init__(root)
            self.asked: list[tuple[str, str | None]] = []

        async def resolve_conid(self, symbol, *, sec_type="STK", exchange=None):
            self.asked.append((symbol, exchange))
            return await super().resolve_conid(symbol, sec_type=sec_type, exchange=exchange)

    market_data = Recording(fixtures_dir)
    sync = UniverseSync(store, config, market_data)
    declared = load_universe_file(Path("universe.yaml"), config)
    row = next(i for i in declared.instruments if i.symbol == "SPY")
    row = dataclasses.replace(row, conid=None)

    await sync._conid_for(row)

    assert market_data.asked == [("SPY", row.exchange)]


async def test_a_pinned_conid_still_issues_no_search_at_all(store, config, fixtures_dir):
    """The venue tiebreak sharpens a SEARCH. An explicit conid: skips the search,
    and that is still the escape hatch for anything genuinely ambiguous."""
    from obliskiq.collect.source import FixtureMarketData
    from obliskiq.collect.universe import UniverseSync

    class Refusing(FixtureMarketData):
        async def resolve_conid(self, symbol, *, sec_type="STK", exchange=None):
            raise AssertionError("a pinned conid must not be searched for")

    declared = load_universe_file(Path("universe.yaml"), config)
    asml = next(i for i in declared.instruments if i.symbol == "ASML")

    assert await UniverseSync(store, config, Refusing(fixtures_dir))._conid_for(asml) == 117589399


# ── options_unavailable_reason: which kind of gap a chainless cell is ──


def test_a_measuring_row_without_a_chain_must_say_why(config, tmp_path):
    """Silence would decide the coverage metric by accident.

    Grid indicators 7, 8 and 9 need a chain. Whether one is merely uncollected or
    structurally unreachable decides whether those cells count against
    grid.min_coverage_pct, and the absence of collect_chain cannot answer it —
    absence is the ambiguity being removed.
    """
    payload = copy.deepcopy(MINIMAL)
    payload["sectors"] = ["broad"]
    payload["instruments"][0].update({"market": "us", "sector": "broad", "grid_measures": True})
    with pytest.raises(ConfigError, match="options_unavailable_reason"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_a_row_cannot_both_collect_a_chain_and_declare_none_reachable(config, tmp_path):
    payload = copy.deepcopy(MINIMAL)
    payload["sectors"] = ["broad"]
    payload["instruments"][0].update(
        {
            "market": "us",
            "sector": "broad",
            "grid_measures": True,
            "collect_chain": True,
            "options_unavailable_reason": "no chain here",
        }
    )
    with pytest.raises(ConfigError, match="both reachable and unreachable"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_a_non_measuring_row_needs_no_options_declaration(config, tmp_path):
    """The requirement is about grid cells, not about every instrument held."""
    payload = copy.deepcopy(MINIMAL)
    payload["instruments"][0]["grid_measures"] = False
    declared = load_universe_file(write_universe(tmp_path, payload), config)
    assert declared.instruments[0].options_unavailable_reason is None


def test_every_measuring_row_in_the_shipped_file_answers_the_question(config):
    declared = load_universe_file(Path("universe.yaml"), config)
    for instrument in declared.instruments:
        if not instrument.grid_measures:
            continue
        assert instrument.collect_chain or instrument.options_unavailable_reason, (
            f"{instrument.symbol} measures a cell and says nothing about its chain"
        )


# ── a market is measured in its own currency, or not at all ──


def test_a_market_measured_in_a_foreign_currency_is_refused(config, tmp_path):
    """The anti-wrapper rule, made checkable.

    It used to be spelled `is_native_index`, which any row could simply assert.
    What the rule is about is the currency trade a foreign listing embeds: an
    HKD fund measuring a CNY market reports HKD returns under China's name, and
    indicator 11 then decomposes them against an EURCNY series that does not
    describe them.
    """
    payload = copy.deepcopy(MINIMAL)
    payload["sectors"] = ["broad"]
    payload["instruments"] = [
        {
            "symbol": "2823",
            "asset_class": "ETF",
            "currency": "HKD",
            "exchange": "SEHK",
            "market": "china",
            "sector": "broad",
            "grid_measures": True,
            "options_unavailable_reason": "no chain reachable",
        }
    ]
    payload["markets"] = {"china": {"currency": "CNY", "native_index": "2823"}}
    with pytest.raises(ConfigError, match="denominated in"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_a_market_may_declare_no_measuring_instrument_at_all(config, tmp_path):
    """An explicit gap is auditable; a missing key is not."""
    payload = copy.deepcopy(MINIMAL)
    payload["sectors"] = ["broad"]
    payload["instruments"] = []
    payload["markets"] = {"south_africa": {"currency": "ZAR", "native_index": None}}
    payload["unmapped_cells"] = [
        {"market": "south_africa", "sectors": "remaining", "reason": "no local tracker listed"}
    ]
    declared = load_universe_file(write_universe(tmp_path, payload), config)
    assert declared.markets["south_africa"]["native_index"] is None
    assert declared.unmapped_cells[("south_africa", "broad")] == "no local tracker listed"

    payload["markets"] = {"south_africa": {"currency": "ZAR"}}
    with pytest.raises(ConfigError, match="Declare it null"):
        load_universe_file(write_universe(tmp_path, payload), config)


# ── every cell is measured or declared, and neither is inferred ──


def _gapped(sectors, markets, unmapped=None):
    payload = copy.deepcopy(MINIMAL)
    payload["sectors"] = sectors
    payload["instruments"] = []
    payload["markets"] = markets
    if unmapped is not None:
        payload["unmapped_cells"] = unmapped
    return payload


ZA = {"south_africa": {"currency": "ZAR", "native_index": None}}


def test_a_cell_neither_measured_nor_declared_is_refused(config, tmp_path):
    """The rule the whole block exists for.

    An unmapped cell used to become `inapplicable` by INFERENCE from the absence
    of a grid_measures row, so "nobody looked" and "somebody looked and there is
    nothing" arrived at the grid wearing the same status. Only a declaration
    separates them, so a universe that omits one is refused.
    """
    payload = _gapped(["broad", "energy"], ZA, [])
    with pytest.raises(ConfigError, match="neither measured by an instrument nor declared"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_the_refusal_names_the_cells_it_could_not_account_for(config, tmp_path):
    payload = _gapped(["broad", "energy"], ZA, [])
    with pytest.raises(ConfigError) as raised:
        load_universe_file(write_universe(tmp_path, payload), config)
    assert "south_africa/broad" in str(raised.value)
    assert "south_africa/energy" in str(raised.value)


def test_a_declared_gap_needs_an_actual_reason(config, tmp_path):
    payload = _gapped(["broad"], ZA, [{"market": "south_africa", "sectors": "remaining"}])
    with pytest.raises(ConfigError, match="gives no reason"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_a_cell_cannot_be_both_measured_and_declared_unmapped(config, tmp_path):
    """Contradictory declarations are refused rather than resolved by ordering."""
    payload = copy.deepcopy(MINIMAL)
    payload["sectors"] = ["broad"]
    payload["instruments"] = [
        {
            "symbol": "STW",
            "asset_class": "ETF",
            "currency": "ZAR",
            "market": "south_africa",
            "sector": "broad",
            "grid_measures": True,
            "options_unavailable_reason": "no chain reachable",
        }
    ]
    payload["markets"] = {"south_africa": {"currency": "ZAR", "native_index": "STW"}}
    payload["unmapped_cells"] = [
        {"market": "south_africa", "sectors": ["broad"], "reason": "nothing listed"}
    ]
    with pytest.raises(ConfigError, match="is declared unmapped and is also measured"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_one_cell_cannot_carry_two_reasons(config, tmp_path):
    payload = _gapped(
        ["broad"],
        ZA,
        [
            {"market": "south_africa", "sectors": ["broad"], "reason": "first"},
            {"market": "south_africa", "sectors": ["broad"], "reason": "second"},
        ],
    )
    with pytest.raises(ConfigError, match="declared unmapped twice"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_a_remaining_block_that_claims_nothing_is_refused(config, tmp_path):
    """Prose asserting a gap that no longer exists is worse than no prose.

    Same failure as a config key nothing reads: it looks like a decision and is
    not one. A market whose sectors all acquired instruments must lose its block.
    """
    payload = _gapped(
        ["broad"],
        ZA,
        [
            {"market": "south_africa", "sectors": ["broad"], "reason": "nothing listed"},
            {"market": "south_africa", "sectors": "remaining", "reason": "left over"},
        ],
    )
    with pytest.raises(ConfigError, match="there are none left"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_a_gap_may_not_name_a_market_or_sector_off_the_axes(config, tmp_path):
    payload = _gapped(["broad"], ZA, [{"market": "narnia", "sectors": "remaining", "reason": "x"}])
    with pytest.raises(ConfigError, match="not on the declared market axis"):
        load_universe_file(write_universe(tmp_path, payload), config)

    payload = _gapped(
        ["broad"], ZA, [{"market": "south_africa", "sectors": ["mining"], "reason": "x"}]
    )
    with pytest.raises(ConfigError, match="not on the declared sector axis"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_a_collector_fixture_with_no_axes_needs_no_declarations(config, tmp_path):
    """A collector walks an instrument list and needs no grid.

    Requiring every such fixture to enumerate a taxonomy it never reads would put
    this file's whole market x sector table into each of them.
    """
    declared = load_universe_file(write_universe(tmp_path, copy.deepcopy(MINIMAL)), config)
    assert declared.unmapped_cells == {}


def test_declaring_gaps_without_axes_is_refused(config, tmp_path):
    payload = copy.deepcopy(MINIMAL)
    payload["unmapped_cells"] = [{"market": "us", "sectors": "remaining", "reason": "x"}]
    with pytest.raises(ConfigError, match="without both a"):
        load_universe_file(write_universe(tmp_path, payload), config)


def test_the_shipped_universe_accounts_for_every_cell(config):
    """The real file, against its own axes. No pair may be silent."""
    declared = load_universe_file("universe.yaml", config)
    measured = {(row.market, row.sector) for row in declared.instruments if row.grid_measures}
    for market in declared.markets:
        for sector in declared.sectors:
            cell = (market, sector)
            assert cell in measured or cell in declared.unmapped_cells, (
                f"{market}/{sector} is neither measured nor declared"
            )
            assert not (cell in measured and cell in declared.unmapped_cells)
