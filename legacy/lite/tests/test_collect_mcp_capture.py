"""The MCP capture path: translation, and the day-1 photograph it produced.

Every fixture under ``tests/fixtures/mcp_capture/raw`` is a **verbatim live IBKR
MCP response**, recorded 2026-08-24 during European market hours. Nothing here is
synthetic, which is the point: the translation rules exist because the two routes
disagree, and a hand-written fixture would encode what was assumed rather than
what arrives.

``tests/fixtures/mcp_capture/connector_2026_09_03`` holds a second live capture,
taken through the **Custom Connector** — the same tools behind a remote HTTPS
endpoint — from a headless container. It exists because the 2026-08-24 session
never requested ``implied_vol_underlying``, and without that field a percentile
cannot be trusted; see the IV-percentile tests below for why that matters more
than it sounds.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from obliskiq.collect.mcp_capture import (
    CaptureBuilder,
    CaptureError,
    bars_from_history,
    contract_rows,
    expirations_of,
    futures_rows,
    implied_vol_of,
    month_key,
    open_interest_for,
    parse_compact_date,
    quote_row,
    select_expirations,
    strike_ladder,
)
from obliskiq.collect.source import FixtureMarketData, _quote_from

RAW = Path(__file__).parent / "fixtures" / "mcp_capture" / "raw"
CONNECTOR = Path(__file__).parent / "fixtures" / "mcp_capture" / "connector_2026_09_03"
SESSION = date(2026, 8, 24)


def load(name: str) -> dict:
    return json.loads((RAW / name).read_text(encoding="utf-8"))


def load_connector(name: str) -> dict:
    return json.loads((CONNECTOR / name).read_text(encoding="utf-8"))


@pytest.fixture
def option_data() -> dict:
    return load("option_data_117589399_FTA_20261218_ASL.json")


@pytest.fixture
def parameters() -> dict:
    return load("option_parameters_117589399.json")


# ── dates and months ──


def test_month_key_uses_ibkrs_mmmyy_form():
    assert month_key(date(2026, 12, 18)) == "DEC26"
    assert month_key(date(2027, 1, 15)) == "JAN27"
    assert month_key(date(2030, 6, 1)) == "JUN30"


@pytest.mark.parametrize("value", ["", None, "2026-12-18", "202612", "20261332", "2026121X"])
def test_an_unreadable_compact_date_is_none_rather_than_a_raise(value):
    """One bad expiry costs that expiry, not the whole capture."""
    assert parse_compact_date(value) is None


# ── expiry selection ──


def test_only_expiries_inside_the_tenor_window_are_selected(parameters):
    """The live listing brackets the window on both sides, so this is a real filter.

    ASML on 2026-08-24 lists 2026-11-20 at 88 DTE (below the 90 floor) and
    2027-03-19 at 207 (above the 200 ceiling). Exactly one date survives.
    """
    chosen = select_expirations(
        expirations_of(parameters), session=SESSION, dte_min=90, dte_max=200
    )
    assert [e.expiry for e in chosen] == [date(2026, 12, 18)]


def test_one_expiration_per_date_even_though_four_trading_classes_list_on_it(parameters):
    """ASML lists AS9, AS9C, ASL and ASLM on 2026-12-18. A capture takes one."""
    same_date = [e for e in expirations_of(parameters) if e.expiry == date(2026, 12, 18)]
    assert len(same_date) == 4

    chosen = select_expirations(
        expirations_of(parameters), session=SESSION, dte_min=90, dte_max=200
    )
    assert len(chosen) == 1


def test_the_trading_class_can_be_pinned(parameters):
    chosen = select_expirations(
        expirations_of(parameters),
        session=SESSION,
        dte_min=90,
        dte_max=200,
        trading_class="ASLM",
    )
    assert [e.trading_class for e in chosen] == ["ASLM"]


def test_expiry_selection_is_deterministic(parameters):
    """Two captures of the same day must agree, or the series is not one series."""
    first = select_expirations(expirations_of(parameters), session=SESSION, dte_min=90, dte_max=200)
    second = select_expirations(
        expirations_of(parameters), session=SESSION, dte_min=90, dte_max=200
    )
    assert [e.expiration_id for e in first] == [e.expiration_id for e in second]


# ── the chain ──


def test_strikes_arrive_as_strings_and_come_back_as_floats(option_data):
    assert option_data["contracts"][0]["strike"] == "880"
    ladder = strike_ladder(option_data)
    assert ladder["call"][0] == 880.0
    assert isinstance(ladder["call"][0], float)


def test_both_rights_are_read_off_one_row(option_data):
    """MCP is per-expiration and carries both sides per strike; CP is per-month."""
    ladder = strike_ladder(option_data)
    assert ladder["call"] == ladder["put"]
    assert len(ladder["call"]) == 39


def test_contract_rows_carry_the_key_the_chain_collector_reads(option_data):
    rows = contract_rows(option_data, expiry=date(2026, 12, 18), right="C")
    assert len(rows) == 39
    row = next(r for r in rows if r["strike"] == 1500.0)
    assert row["conid"] == 843226255
    assert row["maturityDate"] == "2026-12-18"


def test_an_unknown_right_is_refused(option_data):
    with pytest.raises(CaptureError):
        contract_rows(option_data, expiry=date(2026, 12, 18), right="X")


# ── open interest: the pair ──


def test_open_interest_is_selected_by_the_contracts_own_right():
    """The unrelated slot reads 0, not absent, so taking the wrong one yields 0."""
    call = load("snapshot_843226255.json")
    put = load("snapshot_843226257.json")

    assert open_interest_for(call, right="C") == 213
    assert open_interest_for(put, right="P") == 453

    # The trap this function exists to close: both snapshots answer the wrong
    # question with a plausible zero rather than with nothing.
    assert open_interest_for(call, right="P") == 0
    assert open_interest_for(put, right="C") == 0


def test_open_interest_is_none_when_the_block_is_absent():
    assert open_interest_for({}, right="C") is None


# ── implied vol: the sentinel ──


def test_the_minus_one_sentinel_never_becomes_a_volatility():
    """Live, on an ASML 1400 call carrying 654 open interest — not a thin contract."""
    snapshot = load("snapshot_683450807.json")
    assert snapshot["implied-vol"]["annual_iv"] == -1.0
    assert snapshot["implied-vol"]["is_valid"] is False
    assert implied_vol_of(snapshot) is None


def test_a_valid_implied_vol_is_returned_as_a_decimal():
    assert implied_vol_of(load("snapshot_843226255.json")) == pytest.approx(0.4604277959834679)


def test_validity_is_read_from_the_flag_and_never_inferred_from_magnitude():
    """A positive, entirely plausible number still fails if IBKR flagged it invalid."""
    assert implied_vol_of({"implied-vol": {"annual_iv": 0.42, "is_valid": False}}) is None
    assert implied_vol_of({"implied-vol": {"annual_iv": 0.42, "is_valid": True}}) == 0.42


def test_implied_vol_is_per_strike_not_per_contract():
    """The 1500 call and put agree exactly; the 1600 call does not.

    Put-call parity holding, not a stuck field. Pinned because an identical pair
    reads like a bug and someone will otherwise "fix" it.
    """
    call_1500 = implied_vol_of(load("snapshot_843226255.json"))
    put_1500 = implied_vol_of(load("snapshot_843226257.json"))
    call_1600 = implied_vol_of(load("snapshot_689641419.json"))

    assert call_1500 == put_1500
    assert call_1600 != call_1500


# ── quotes ──


def test_an_absent_field_decodes_to_none_and_never_to_zero():
    """A bid IBKR did not serve is not a bid of nothing."""
    row = quote_row(load("snapshot_843226255.json"), right="C")
    assert row["bid"] is None
    assert row["ask"] is None
    assert row["volume"] == 0  # option-volume WAS served, and it really is zero


def test_the_underlying_quote_carries_the_full_field_set_in_one_response():
    """One call, every field the collector wants — which is how a capture must take it.

    An earlier capture took the price and the volatility fields in two separate
    calls and saved only the first, so `underlying_snapshots.volume` came out
    NULL for a session that traded 164,455 shares. Neither response alone is a
    complete underlying reading.
    """
    row = quote_row(load("snapshot_117589399.json"))
    assert row["last"] == pytest.approx(1506.8)
    assert row["volume"] == 164455
    assert row["market_data_status"] == "REALTIME"
    assert row["historical_vol_30d"] == pytest.approx(0.5398650258724675)
    # The percentile is NOT carried from this capture, and that is correct: the
    # 2026-08-24 session did not request `implied_vol_underlying`, so nothing in
    # the response establishes that the IV being ranked was valid. See
    # `test_a_percentile_without_its_validity_flag_is_refused`.
    assert "iv_percentile_52w" not in row


def test_an_empty_bid_ask_object_decodes_to_absent_and_not_to_zero():
    """IBKR returns `"bid-ask": {}` when no quote is currently available.

    Verbatim in this fixture, taken while ASML was trading and reading
    `top-status: REALTIME` — so an empty book is not the same as an unentitled
    or closed one, and a bid of None is not a bid of nothing.
    """
    row = quote_row(load("snapshot_117589399.json"))
    assert row["bid"] is None
    assert row["ask"] is None


def test_a_quote_row_round_trips_through_the_shared_quote_constructor():
    """The capture must decode through the same path the CP route uses."""
    quote = _quote_from(843226255, quote_row(load("snapshot_843226255.json"), right="C"))
    assert quote.implied_vol == pytest.approx(0.4604277959834679)
    assert quote.open_interest == 213
    assert quote.bid is None


def test_market_data_status_is_none_when_the_route_reported_nothing():
    """Absence is not evidence of freshness — see migration 006."""
    assert quote_row(load("snapshot_843226255.json"), right="C")["market_data_status"] is None
    assert quote_row({"top-status": "FROZEN"})["market_data_status"] == "FROZEN"
    assert quote_row({"top-status": "  "})["market_data_status"] is None


# ── bars: the columnar divergence ──


def test_mcp_history_is_columnar_and_is_translated_to_rows():
    """Passing an MCP body to the CP decoder yields zero bars and no error."""
    body = load("history_117589399_1d.json")
    assert "data" not in body
    assert isinstance(body["time"], list)

    translated = bars_from_history(body)
    assert len(translated["data"]) == 40
    first = translated["data"][0]
    assert first["c"] == pytest.approx(1721.4)
    assert first["t"] == 1782802800000.0  # 2026-06-30T07:00:00Z in epoch ms


def test_a_ragged_column_never_produces_a_frankenstein_bar():
    """Pairing one day's close with another day's volume fabricates a bar."""
    translated = bars_from_history(
        {
            "time": ["2026-08-20T13:30:00Z", "2026-08-21T13:30:00Z", "2026-08-24T13:30:00Z"],
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [10, 20],
        }
    )
    assert len(translated["data"]) == 2


def test_an_empty_history_is_no_bars_rather_than_a_raise():
    assert bars_from_history({})["data"] == []


def test_a_body_already_in_row_form_is_passed_through():
    """A capture may hold recorded CP responses beside recorded MCP ones."""
    builder = CaptureBuilder()
    builder.add_bars(1, "1d", {"data": [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]})
    assert builder.bars["1_1d"]["data"] == [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]


# ── futures ──


def test_the_futures_ladder_drops_the_symbol_it_cannot_distinguish_contracts_by():
    rows = futures_rows(load("futures_GC.json"))
    assert len(rows) == 5
    assert all("symbol" not in row for row in rows)
    assert rows[0]["expirationDate"] == "2026-08-27"


# ── the writer, and the shape the collectors actually read ──


def test_a_written_capture_is_readable_by_the_collectors_own_provider(
    tmp_path, option_data, parameters
):
    """The whole point: no collector changes, only the source tag."""
    expiration = select_expirations(
        expirations_of(parameters), session=SESSION, dte_min=90, dte_max=200
    )[0]

    builder = CaptureBuilder()
    builder.add_expiration("ASML", 117589399, expiration, option_data, exchange="FTA")
    builder.add_quote(843226255, load("snapshot_843226255.json"), right="C")
    out = builder.write(tmp_path / "capture")

    provider = FixtureMarketData(out, source="mcp")
    assert provider.source == "mcp"

    import asyncio

    months, exchanges = asyncio.run(provider.option_months("ASML", conid=117589399))
    assert months == ("DEC26",)
    assert exchanges == ("FTA",)

    ladder = asyncio.run(provider.option_strikes(117589399, month="DEC26", exchange="FTA"))
    assert 1500.0 in ladder["call"]

    contracts = asyncio.run(
        provider.option_contracts(
            117589399, month="DEC26", exchange="FTA", strike=1500.0, right="C"
        )
    )
    assert contracts[0]["conid"] == 843226255

    quotes = asyncio.run(provider.quotes([843226255]))
    assert quotes[843226255].implied_vol == pytest.approx(0.4604277959834679)


def test_folding_a_second_expiration_extends_rather_than_replaces(
    tmp_path, option_data, parameters
):
    """Two expiries in one month share a ladder file and must not overwrite it."""
    expirations = expirations_of(parameters)
    december = next(e for e in expirations if e.expiry == date(2026, 12, 18))

    builder = CaptureBuilder()
    builder.add_expiration("ASML", 117589399, december, option_data, exchange="FTA")
    before = len(builder.contracts["117589399_DEC26_C"])

    # The same response folded twice must not duplicate a single row.
    builder.add_expiration("ASML", 117589399, december, option_data, exchange="FTA")
    assert len(builder.contracts["117589399_DEC26_C"]) == before
    assert builder.option_months["ASML"]["months"] == ["DEC26"]


def test_a_contract_month_is_refused_rather_than_read_as_a_day():
    """``strptime("%Y%m%d")`` is lenient without separators: "202612" -> 2026-01-02.

    IBKR serves ``contract_month`` ("202612") in the same row as
    ``last_trading_date`` ("20261229"). A lenient parse turns the wrong field
    into a plausible date instead of an error, which is the worst outcome
    available: nothing downstream could tell the curve was misdated.
    """
    assert parse_compact_date("202612") is None
    assert parse_compact_date("20261229") == date(2026, 12, 29)


def test_volume_is_read_out_of_the_block_ibkr_nests_it_in():
    """Both forms wrap the number one level deep, and reading the outer value loses it.

    An option's is ``{"option-volume": {"volume": 1, "trades": 1, ...}}`` and an
    underlying's is ``{"volume": {"volume": 136099.0}}``. Handing the outer dict
    to ``safe_int`` yields ``None``, so a real reading of 136,099 becomes "no
    volume" in a column the turnover indicator reads — found in this module's own
    code during the Session 7 self-review.
    """
    # Verbatim from ASML's live underlying snapshot, 2026-08-24.
    assert quote_row({"volume": {"volume": 136099.0}})["volume"] == 136099
    # Verbatim from the ASML 1000 call.
    option = {"option-volume": {"total_cost": 1562.0, "volume": 3, "trades": 3}}
    assert quote_row(option, right="C")["volume"] == 3


def test_a_genuine_zero_volume_stays_zero_and_does_not_become_absent():
    """A contract that did not trade is not a contract with no volume field.

    ``liquidity.require_session_volume`` treats the two differently: zero fails
    the gate, absent leaves it untested.
    """
    row = quote_row({"option-volume": {"volume": 0, "trades": 0}}, right="C")
    assert row["volume"] == 0
    assert row["volume"] is not None


def test_no_volume_block_at_all_is_none():
    assert quote_row({})["volume"] is None


# ── the IV percentile, and the flag that makes it mean anything ──


def test_all_three_percentile_windows_are_carried():
    """`underlying_snapshots` has had 13w and 26w columns since migration 005 and
    only 52w was ever written, so two thirds of a field the connector serves for
    free were discarded at the decoder."""
    row = quote_row(load_connector("snapshot_756733.json"))
    assert row["iv_percentile_13w"] == pytest.approx(0.078125)
    assert row["iv_percentile_26w"] == pytest.approx(0.03937007859349251)
    assert row["iv_percentile_52w"] == pytest.approx(0.075396828353405)


def test_a_percentile_without_its_validity_flag_is_refused():
    """IBKR serves the percentile even when the IV it ranks is invalid, and the
    rank then reads 0.0 rather than being omitted — observed on three EUR-listed
    names outside venue hours.

    A response carrying no `implied-vol-underlying` at all cannot distinguish that
    case from a real reading, so it is refused rather than trusted. Absence is not
    a passed gate, which is the same rule `implied_vol_of` already applies one
    level down.
    """
    served_but_unverifiable = {
        "implied-volatility-percentile": {"high_13w": 0.0, "high_26w": 0.0, "high_52w": 0.28}
    }
    row = quote_row(served_but_unverifiable)
    assert "iv_percentile_52w" not in row


def test_a_percentile_ranking_an_invalid_iv_is_dropped_not_stored_as_zero():
    """The live shape from the EUR-listed names: `is_valid: false`, and the
    shorter windows reading exactly 0.0.

    Stored, that is a name whose implied volatility sits at the very bottom of its
    13- and 26-week range — the single strongest 'coiled' signal the grid can
    receive — manufactured out of a quote IBKR declined to give.
    """
    invalid = {
        "implied-vol-underlying": {"daily_iv": 0.0, "annual_iv": 0.0, "is_valid": False},
        "implied-volatility-percentile": {
            "high_13w": 0.0,
            "high_26w": 0.0,
            "high_52w": 0.2863,
        },
    }
    row = quote_row(invalid)
    assert "iv_percentile_13w" not in row
    assert "iv_percentile_52w" not in row


def test_a_genuine_zero_percentile_survives_on_a_valid_iv():
    """The reason no magnitude test is allowed anywhere near this field.

    ASML on 2026-09-03, trading and REALTIME, returned `high_13w` and `high_26w`
    of exactly 0.0 with `is_valid: true` and a current IV of 0.3779 — a true
    zeroth percentile. It is byte-identical to the broken reading above, so a rule
    like "drop zeros to be safe" would throw away the most compressed name on the
    board while a rule that keeps them ungated stores the artefact. Only the flag
    separates them.
    """
    row = quote_row(load_connector("snapshot_117589399.json"))
    assert row["iv_percentile_13w"] == 0.0
    assert row["iv_percentile_26w"] == 0.0
    assert row["iv_percentile_52w"] == pytest.approx(0.2078431397676468)


def test_the_camelCase_validity_spelling_is_accepted_too():
    """IBKR mixes `is_valid` and `isValid` across sibling fields of one response,
    which is recorded in the playbook and has already cost one field."""
    row = quote_row(
        {
            "implied-vol-underlying": {"annual_iv": 0.31, "isValid": True},
            "implied-volatility-percentile": {"high_52w": 0.44},
        }
    )
    assert row["iv_percentile_52w"] == pytest.approx(0.44)


def test_historical_vol_is_unaffected_by_the_percentile_gate():
    """`historical-vol` is its own measurement and does not rank the current IV,
    so an invalid IV must not take it down with the percentile."""
    row = quote_row(load("snapshot_117589399.json"))
    assert row["historical_vol_30d"] == pytest.approx(0.5398650258724675)
