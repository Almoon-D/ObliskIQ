"""The field-definition audit: scale, units and the values that must be refused.

Ported from ObliskIQ-lite-MCP's ``tests/test_ibkr_field_units.py``, adapted to the pure decoders
in :mod:`obliskiq.ibkr.decode`. The most dangerous bug in this layer is a field that parses
cleanly and means something else; each case below is one lite found by reading its own code or
measured on a live route.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from typing import Any

import pytest

from obliskiq.ibkr.decode import PERCENT_TO_DECIMAL, decode_futures, decode_quote, field_map
from obliskiq.ibkr.models import Quote
from obliskiq.util.numeric import safe_float, safe_int

#: A dividend-yield field id for the decoder tests. The shipped map declares the yield ``null``
#: (no verified id), and a null id is neither requested nor decoded, so a test exercising the
#: decoder supplies one.
YIELD_ID = "7287"


def with_marketdata(ibkr: Any, **values: Any) -> Any:
    return ibkr.model_copy(update={"marketdata": ibkr.marketdata.model_copy(update=values)})


def underlying(ibkr: Any, row: dict[str, Any], *, yield_id: str | None = None) -> Quote:
    fields = field_map(ibkr.marketdata.underlying_snapshot_fields)
    if yield_id is not None:
        fields["dividend_yield"] = yield_id
    return decode_quote(1, {"conid": 1, **row}, fields, ibkr)


def option(ibkr: Any, row: dict[str, Any]) -> Quote:
    return decode_quote(1, {"conid": 1, **row}, field_map(ibkr.marketdata.snapshot_fields), ibkr)


@pytest.fixture
def ibkr(shipped_settings: Any) -> Any:
    return shipped_settings.ibkr


@pytest.fixture
def iv_id(ibkr: Any) -> str:
    """The underlying's implied-vol field id (7283, "13.126%" live [CP ✓ 2026-09-10])."""
    return field_map(ibkr.marketdata.underlying_snapshot_fields)["implied_vol"]


# ── the percent-suffix hazard ──


def test_a_percent_suffixed_number_parses_instead_of_vanishing():
    """lite's safe_float("31.42%") once returned the DEFAULT: an IV became absent, with no error
    and no trace, the worse of the two failures."""
    assert safe_float("31.42%", None) == 31.42
    assert safe_float(" 31.42 % ", None) == 31.42
    assert safe_int("1,204%", None) == 1204


def test_a_bare_percent_sign_is_still_absent():
    assert safe_float("%", None) is None


def test_the_other_ibkr_shapes_still_parse():
    """Thousands separators, the C/H prefixes, and FRED's "." missing-observation placeholder."""
    assert safe_float("1,234.5", None) == 1234.5
    assert safe_float("C31.42", None) == 31.42
    assert safe_float(".", None) is None


# ── units are declared, never guessed ──


def test_a_percent_suffixed_iv_is_scaled_to_a_decimal_under_auto(ibkr, iv_id):
    config = with_marketdata(ibkr, implied_vol_units="auto")
    assert underlying(config, {iv_id: "31.42%"}).implied_vol == pytest.approx(
        31.42 / PERCENT_TO_DECIMAL
    )


def test_a_bare_iv_uses_the_configured_fallback_under_auto(ibkr, iv_id):
    as_decimal = with_marketdata(
        ibkr, implied_vol_units="auto", implied_vol_units_fallback="decimal"
    )
    assert underlying(as_decimal, {iv_id: "0.3142"}).implied_vol == 0.3142
    as_percent = with_marketdata(
        ibkr, implied_vol_units="auto", implied_vol_units_fallback="percent"
    )
    assert underlying(as_percent, {iv_id: "31.42"}).implied_vol == pytest.approx(0.3142)


def test_the_units_can_be_pinned_outright(ibkr, iv_id):
    """Even without a % suffix, a pinned source is taken at its word."""
    pinned = with_marketdata(ibkr, implied_vol_units="percent")
    assert underlying(pinned, {iv_id: "31.42"}).implied_vol == pytest.approx(0.3142)
    as_decimal = with_marketdata(ibkr, implied_vol_units="decimal")
    assert underlying(as_decimal, {iv_id: "0.3142"}).implied_vol == 0.3142


def test_a_genuine_two_hundred_percent_iv_survives_as_a_decimal(ibkr, iv_id):
    """There is deliberately NO magnitude heuristic: reading 2.0 as "200%, therefore 0.02"
    would turn a real 200%-IV name into a 2% one and nobody would notice."""
    config = with_marketdata(ibkr, implied_vol_units="auto", implied_vol_units_fallback="decimal")
    assert underlying(config, {iv_id: "2.0"}).implied_vol == 2.0


# ── implausible values are refused, not stored ──


def test_an_iv_beyond_the_plausible_ceiling_is_dropped_to_none(ibkr, iv_id):
    """A value out of range after normalisation is a unit error: 4,200% vol is not a quote."""
    config = with_marketdata(ibkr, implied_vol_units="decimal", implied_vol_max_plausible=5.0)
    assert underlying(config, {iv_id: "42.0"}).implied_vol is None


def test_ibkrs_negative_iv_sentinel_is_dropped_not_stored(ibkr, iv_id):
    """Measured in lite 2026-08-23: -1.0 is IBKR's "no implied vol" on a liquid contract that
    served a valid 0.1462 beside it. A ceiling on abs(value) let -1.0 through; the floor is
    what catches it."""
    config = with_marketdata(ibkr, implied_vol_units="decimal")
    assert underlying(config, {iv_id: "-1.0"}).implied_vol is None
    assert underlying(config, {iv_id: "-15.874507866387544"}).implied_vol is None
    assert underlying(config, {iv_id: "-0.5"}).implied_vol is None, "small negatives too"
    assert underlying(config, {iv_id: "0.14615885631260156"}).implied_vol == pytest.approx(
        0.14615885631260156
    )


def test_the_shipped_iv_floor_catches_the_sentinel_under_auto(ibkr, iv_id):
    assert ibkr.marketdata.implied_vol_min_plausible == 0.0
    assert underlying(ibkr, {iv_id: -1.0}).implied_vol is None
    assert underlying(ibkr, {iv_id: "0.0"}).implied_vol == 0.0, "zero is a reading"


def test_the_iv_floor_is_configured_rather_than_written_in_code(ibkr, iv_id):
    """Raising the floor turns a zero-vol quote into an absence too."""
    config = with_marketdata(ibkr, implied_vol_units="decimal", implied_vol_min_plausible=0.01)
    assert underlying(config, {iv_id: "0.0"}).implied_vol is None
    assert underlying(config, {iv_id: "0.05"}).implied_vol == 0.05


def test_the_historical_vol_shares_the_volatility_guards(ibkr):
    hv_id = field_map(ibkr.marketdata.underlying_snapshot_fields)["historical_vol_30d"]
    assert underlying(ibkr, {hv_id: "-1"}).historical_vol_30d is None
    assert underlying(ibkr, {hv_id: "18%"}).historical_vol_30d == pytest.approx(0.18)


def test_a_delta_outside_minus_one_to_one_is_dropped(ibkr):
    """Delta keeps a magnitude-only guard, unlike implied vol: a put's delta is legitimately
    negative, so a floor at zero would discard half the chain."""
    delta_id = field_map(ibkr.marketdata.snapshot_fields)["delta"]
    assert option(ibkr, {delta_id: "0.55"}).delta == 0.55
    assert option(ibkr, {delta_id: "-0.55"}).delta == -0.55
    assert option(ibkr, {delta_id: "-1"}).delta == -1.0
    assert option(ibkr, {delta_id: "55"}).delta is None
    assert option(ibkr, {delta_id: "-1.2"}).delta is None


def test_an_absent_field_is_absent_not_zero(ibkr):
    """A missing bid is not a bid of zero."""
    quote = option(ibkr, {"31": "10.0"})
    assert quote.last == 10.0
    assert quote.bid is None
    assert quote.ask is None
    assert quote.implied_vol is None
    assert quote.volume is None
    assert quote.open_interest is None
    assert quote.delta is None
    assert quote.market_data_status is None


def test_counts_decode_as_integers(ibkr):
    """Open interest is IBKR's 7638; lite's 7089 is option VOLUME (docs/DECISIONS.md)."""
    fields = field_map(ibkr.marketdata.snapshot_fields)
    quote = option(ibkr, {fields["volume"]: "1,204", fields["open_interest"]: "49,114"})
    assert quote.volume == 1204
    assert isinstance(quote.volume, int)
    assert quote.open_interest == 49114
    assert isinstance(quote.open_interest, int)
    assert fields["open_interest"] != "7089"


def test_the_raw_count_is_preferred_over_the_abbreviated_display(ibkr):
    """Field 87 shows "53.4K" beside "87_raw": 53400.0 [CP ✓ 2026-09-10]; the abbreviation
    alone would make liquid names' volume absent."""
    volume_id = field_map(ibkr.marketdata.snapshot_fields)["volume"]
    quote = option(ibkr, {volume_id: "53.4K", f"{volume_id}_raw": 53400.0})
    assert quote.volume == 53400
    assert option(ibkr, {volume_id: "53.4K"}).volume is None, "never guessed from the suffix"


def test_a_negative_count_is_absent(ibkr):
    volume_id = field_map(ibkr.marketdata.snapshot_fields)["volume"]
    assert option(ibkr, {volume_id: "-5"}).volume is None


# ── the two year bases ──


def test_the_two_year_bases_are_distinct_and_both_configured(shipped_settings):
    """numerics.bsm.day_count_basis measures CONTRACTUAL time to expiry for pricing;
    ibkr.history.sessions_per_year annualises REALISED trading sessions. They are not
    interchangeable, and both exist rather than one standing in for the other."""
    calendar = shipped_settings.numerics.bsm.day_count_basis
    trading = shipped_settings.ibkr.history.sessions_per_year
    assert calendar > trading > 0
    assert calendar == 365.0
    assert trading == 252


# ── the futures ladder ──


def test_the_futures_ladder_decodes_every_root_asked_for():
    body = {
        "CL": [{"conid": 1, "symbol": "CLG6", "expirationDate": "20260220"}],
        "NG": [{"conid": 2, "symbol": "NGG6", "expirationDate": "20260226"}],
    }
    ladder = decode_futures(body, ["CL", "NG", "GC"])
    assert set(ladder) == {"CL", "NG", "GC"}
    assert ladder["GC"] == (), "a root IBKR did not answer is an empty ladder, not a missing key"
    assert ladder["CL"][0].root == "CL"
    assert ladder["CL"][0].symbol == "CLG6"


def test_an_empty_root_list_decodes_to_nothing():
    assert decode_futures({"CL": []}, []) == {}


def test_a_futures_row_missing_a_conid_or_expiry_is_dropped():
    body = {
        "CL": [
            {"symbol": "CLG6", "expirationDate": "20260220"},
            {"conid": 2, "symbol": "CLH6"},
            {"conid": 4, "symbol": "CLK6", "expirationDate": "2026-05-20"},
            {"conid": 3, "symbol": "CLJ6", "expirationDate": "20260420"},
            "junk",
        ]
    }
    assert [rung.conid for rung in decode_futures(body, ["CL"])["CL"]] == [3]


def test_the_futures_ladder_is_sorted_by_expiry_not_by_ibkrs_order():
    body = {
        "CL": [
            {"conid": 3, "expirationDate": "20260420"},
            {"conid": 1, "expirationDate": "20260220"},
            {"conid": 2, "expirationDate": "20260320"},
        ]
    }
    rungs = decode_futures(body, ["CL"])["CL"]
    assert [rung.conid for rung in rungs] == [1, 2, 3]
    assert rungs[0].expiry == date(2026, 2, 20)


def test_a_futures_body_that_is_not_an_object_is_empty_ladders():
    assert decode_futures([{"conid": 1}], ["CL"]) == {"CL": ()}


# ── the dividend yield: a percent under a name that does not say so ──
#
# Measured in lite 2026-09-09: one response carried a dividend yield of 0.98 (PERCENT) beside a
# historical vol of 0.0956 (a FRACTION) under the same `_pct` suffix, which is why the units are
# configured and never inferred.


def test_an_unverified_yield_field_id_is_neither_requested_nor_decoded(ibkr):
    """``null`` means no verified id: a guessed id would put another quantity into a yield."""
    fields = field_map(ibkr.marketdata.underlying_snapshot_fields)
    assert "dividend_yield" not in fields
    assert YIELD_ID not in fields.values()
    assert underlying(ibkr, {YIELD_ID: "0.98"}).dividend_yield is None


def test_a_served_percent_yield_becomes_a_decimal(ibkr):
    """0.98 PERCENT is 0.0098 as a decimal, the unit the whole schema uses."""
    config = with_marketdata(ibkr, dividend_yield_units="percent")
    decoded = underlying(config, {YIELD_ID: "0.98"}, yield_id=YIELD_ID).dividend_yield
    assert decoded == pytest.approx(0.98 / PERCENT_TO_DECIMAL)
    assert decoded == pytest.approx(0.0098)


def test_the_two_decimal_places_ibkr_actually_serves_survive(ibkr):
    """A route description claimed one decimal place; SPY came back 0.98 and XLP 2.62."""
    config = with_marketdata(ibkr, dividend_yield_units="percent")
    for served, expected in (("0.98", 0.0098), ("2.62", 0.0262)):
        decoded = underlying(config, {YIELD_ID: served}, yield_id=YIELD_ID).dividend_yield
        assert decoded == pytest.approx(expected)


def test_auto_falls_back_to_percent_for_the_yield_by_default(ibkr):
    """The fallback differs from implied vol's on purpose: a bare 0.98 is 0.98%, and reading it
    as a decimal would price a 1% payer as a 98% one."""
    assert ibkr.marketdata.dividend_yield_units == "auto"
    assert ibkr.marketdata.dividend_yield_units_fallback == "percent"
    assert ibkr.marketdata.implied_vol_units_fallback == "decimal"
    decoded = underlying(ibkr, {YIELD_ID: 0.98}, yield_id=YIELD_ID).dividend_yield
    assert decoded == pytest.approx(0.0098)


def test_a_percent_suffix_still_wins_under_auto(ibkr):
    config = with_marketdata(
        ibkr, dividend_yield_units="auto", dividend_yield_units_fallback="decimal"
    )
    decoded = underlying(config, {YIELD_ID: "2.62%"}, yield_id=YIELD_ID).dividend_yield
    assert decoded == pytest.approx(0.0262)


def test_a_negative_yield_is_a_sentinel_and_is_dropped(ibkr):
    """An instrument cannot pay out less than nothing; -1.0 is IBKR's "no value"."""
    config = with_marketdata(ibkr, dividend_yield_units="percent")
    assert underlying(config, {YIELD_ID: -1.0}, yield_id=YIELD_ID).dividend_yield is None


def test_a_zero_yield_is_a_reading_and_is_kept(ibkr):
    """A non-payer is data; absent means nobody collected one."""
    config = with_marketdata(ibkr, dividend_yield_units="percent")
    assert underlying(config, {YIELD_ID: 0.0}, yield_id=YIELD_ID).dividend_yield == 0.0


def test_an_unconverted_percent_is_refused_by_the_ceiling(ibkr):
    """98.0 read as a decimal is a 9800% yield: a unit error, not a payout."""
    config = with_marketdata(ibkr, dividend_yield_units="decimal")
    assert underlying(config, {YIELD_ID: 98.0}, yield_id=YIELD_ID).dividend_yield is None
