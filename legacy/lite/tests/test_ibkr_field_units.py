"""The field-definition audit: scale, units and the values that must be refused.

The most dangerous class of bug in this layer is a field that parses cleanly and
means something else. Each case here corresponds to an entry in
``references/ibkr-playbook.md``, and each was found by reading the shipped code
rather than by guessing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy

import httpx
import pytest

from conftest import without_backoff
from obliskiq.config import Config
from obliskiq.ibkr.cp_client import PERCENT_TO_DECIMAL, CpApiClient
from obliskiq.util.numeric import safe_float, safe_int


class StubOAuth:
    def sign_request(self, method, url, *, params=None):
        return {"Authorization": 'OAuth realm="limited_poa"'}


def make_client(config, **marketdata_overrides) -> CpApiClient:
    data = copy.deepcopy(without_backoff(config).as_dict())
    data["ibkr"]["cp_api"]["marketdata"].update(marketdata_overrides)
    return CpApiClient(
        "https://api.ibkr.com",
        StubOAuth(),
        Config(data),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        ),
    )


# ── the percent-suffix hazard ──


def test_a_percent_suffixed_number_parses_instead_of_vanishing():
    """Before this, safe_float("31.42%") returned the DEFAULT, so an IV became
    *absent* rather than wrong — no error, no row, no trace, which is the worse
    failure of the two."""
    assert safe_float("31.42%", None) == 31.42
    assert safe_float(" 31.42 % ", None) == 31.42
    assert safe_int("1,204%", None) == 1204


def test_a_bare_percent_sign_is_still_absent():
    assert safe_float("%", None) is None


def test_the_other_ibkr_shapes_still_parse():
    """Thousands separators, and the C/H/L prefixes for close and halted bounds."""
    assert safe_float("1,234.5", None) == 1234.5
    assert safe_float("C31.42", None) == 31.42
    assert safe_float(".", None) is None  # FRED's missing-observation placeholder


# ── units are declared, never guessed ──


async def test_a_percent_suffixed_iv_is_scaled_to_a_decimal_under_auto(config):
    client = make_client(config, implied_vol_units="auto")
    decoded = client.decode_snapshot({"conid": 1, "7283": "31.42%"})
    assert decoded["implied_vol"] == pytest.approx(31.42 / PERCENT_TO_DECIMAL)
    await client.aclose()


async def test_a_bare_iv_uses_the_configured_fallback_under_auto(config):
    decimal_client = make_client(
        config, implied_vol_units="auto", implied_vol_units_fallback="decimal"
    )
    assert decimal_client.decode_snapshot({"conid": 1, "7283": "0.3142"})["implied_vol"] == 0.3142
    await decimal_client.aclose()

    percent_client = make_client(
        config, implied_vol_units="auto", implied_vol_units_fallback="percent"
    )
    assert percent_client.decode_snapshot({"conid": 1, "7283": "31.42"})[
        "implied_vol"
    ] == pytest.approx(0.3142)
    await percent_client.aclose()


async def test_the_units_can_be_pinned_outright(config):
    pinned = make_client(config, implied_vol_units="percent")
    # Even without a % suffix, a pinned source is taken at its word.
    assert pinned.decode_snapshot({"conid": 1, "7283": "31.42"})["implied_vol"] == pytest.approx(
        0.3142
    )
    await pinned.aclose()


async def test_a_genuine_two_hundred_percent_iv_survives_as_a_decimal(config):
    """The reason there is deliberately NO magnitude heuristic: a rule that read
    2.0 as '200%, therefore 0.02' would turn a real 200%-IV name into a 2% one and
    nobody would notice."""
    client = make_client(config, implied_vol_units="auto", implied_vol_units_fallback="decimal")
    assert client.decode_snapshot({"conid": 1, "7283": "2.0"})["implied_vol"] == 2.0
    await client.aclose()


# ── implausible values are refused, not stored ──


async def test_an_iv_beyond_the_plausible_ceiling_is_dropped_to_none(config):
    """A value that survives normalisation but lands out of range is a unit error,
    not a quote. Storing it would put a 4,000% volatility into a percentile."""
    client = make_client(config, implied_vol_units="decimal", implied_vol_max_plausible=5.0)
    assert client.decode_snapshot({"conid": 1, "7283": "42.0"})["implied_vol"] is None
    await client.aclose()


async def test_ibkrs_negative_iv_sentinel_is_dropped_not_stored(config):
    """VERIFIED LIVE 2026-08-23 against the IBKR MCP connector.

    ``option-midpoint-iv`` came back ``{dailyIv: -1.0, annualIv: -15.87,
    isValid: false}`` on SPY DEC 18 '26 770C — a liquid contract carrying 9,334
    open interest — while ``implied-vol`` served a valid 0.1462 for the same
    strike. -1.0 is the sentinel for "no implied vol here".

    The ceiling alone did not catch it: the plausibility guard tests
    ``abs(value)``, which is right for delta and wrong for volatility, so -1.0
    sat comfortably inside a max of 5.0 and was stored as an implied vol of -1.0.
    The annualised form was rejected only because 15.87 happens to exceed the
    ceiling — luck, not a guard.
    """
    client = make_client(config, implied_vol_units="decimal")
    assert client.decode_snapshot({"conid": 1, "7283": "-1.0"})["implied_vol"] is None
    assert (
        client.decode_snapshot({"conid": 1, "7283": "-15.874507866387544"})["implied_vol"] is None
    )
    # A small negative is no more a volatility than a large one.
    assert client.decode_snapshot({"conid": 1, "7283": "-0.5"})["implied_vol"] is None
    # And the valid quote for that same contract still lands.
    assert client.decode_snapshot({"conid": 1, "7283": "0.14615885631260156"})[
        "implied_vol"
    ] == pytest.approx(0.14615885631260156)
    await client.aclose()


async def test_the_iv_floor_is_configured_rather_than_written_in_code(config):
    """The floor is policy, so it comes from config like every other threshold.
    Raising it turns a zero-vol quote into an absence too, which is the point of
    having it be a knob at all."""
    client = make_client(config, implied_vol_units="decimal", implied_vol_min_plausible=0.01)
    assert client.decode_snapshot({"conid": 1, "7283": "0.0"})["implied_vol"] is None
    assert client.decode_snapshot({"conid": 1, "7283": "0.05"})["implied_vol"] == 0.05
    await client.aclose()


async def test_a_delta_outside_minus_one_to_one_is_dropped(config):
    """Delta keeps a magnitude-only guard, unlike implied vol above: a put's
    delta is legitimately negative, so a floor at zero would discard half the
    chain."""
    client = make_client(config)
    assert client.decode_snapshot({"conid": 1, "7308": "0.55"})["delta"] == 0.55
    assert client.decode_snapshot({"conid": 1, "7308": "-0.55"})["delta"] == -0.55
    assert client.decode_snapshot({"conid": 1, "7308": "55"})["delta"] is None
    await client.aclose()


async def test_an_absent_field_is_still_absent_not_zero(config):
    """A missing bid is not a bid of zero."""
    client = make_client(config)
    decoded = client.decode_snapshot({"conid": 1, "31": "10.0"})
    assert decoded["last"] == 10.0
    assert decoded["bid"] is None and decoded["ask"] is None and decoded["implied_vol"] is None
    await client.aclose()


async def test_counts_decode_as_integers(config):
    client = make_client(config)
    decoded = client.decode_snapshot({"conid": 1, "87": "1,204", "7089": "49,114"})
    assert decoded["volume"] == 1204 and isinstance(decoded["volume"], int)
    assert decoded["open_interest"] == 49114
    await client.aclose()


# ── the two year bases ──


def test_the_two_year_bases_are_distinct_and_both_configured(config):
    """day_count_basis measures CONTRACTUAL time to expiry for pricing;
    trading_days_per_year annualises REALISED diffusion. They are not
    interchangeable, and both must exist rather than one standing in for the
    other."""
    calendar = config.require_float("numerics.bsm.day_count_basis")
    trading = config.require_float("data.trading_days_per_year")
    assert calendar > trading > 0


def test_the_strike_bound_formula_names_its_own_annualisation_basis(config):
    """data.strike_bounds_sigma and data.trading_days_per_year are the k and the
    252 in spot * exp(+/- k*sigma*sqrt(T/252))."""
    assert config.require_float("data.strike_bounds_sigma") > 0
    assert config.require_float("data.trading_days_per_year") > 0


# ── the futures ladder ──


async def test_the_futures_ladder_requests_every_root_in_one_call(config):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "CL": [{"conid": 1, "symbol": "CLG6", "expirationDate": "20260220"}],
                "NG": [{"conid": 2, "symbol": "NGG6", "expirationDate": "20260226"}],
            },
        )

    client = CpApiClient(
        "https://api.ibkr.com",
        StubOAuth(),
        without_backoff(config),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    ladder = await client.futures_ladder(["CL", "NG"])
    assert seen["symbols"] == "CL,NG"
    assert set(ladder) == {"CL", "NG"}
    await client.aclose()


async def test_an_empty_root_list_short_circuits(config):
    client = make_client(config)
    assert await client.futures_ladder([]) == {}
    await client.aclose()


async def test_a_futures_row_missing_a_conid_or_expiry_is_dropped(config):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "CL": [
                    {"symbol": "CLG6", "expirationDate": "20260220"},
                    {"conid": 2, "symbol": "CLH6"},
                    {"conid": 3, "symbol": "CLJ6", "expirationDate": "20260420"},
                ]
            },
        )

    client = CpApiClient(
        "https://api.ibkr.com",
        StubOAuth(),
        without_backoff(config),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert [row["conid"] for row in (await client.futures_ladder(["CL"]))["CL"]] == [3]
    await client.aclose()


# ── the dividend yield: a percent under a name that does not say so ──
#
# Every case here is measured. On 2026-09-09 a single MCP-connector response for
# SPY carried dividend-yield {yield_pct: 0.98} — 0.98 PERCENT — beside
# historical-vol {annual_pct: 0.0956}, a FRACTION. The `_pct` suffix states no
# unit on that route, which is why these are configured and not inferred.


def _underlying_yield_client(config, **overrides):
    """A client with a REAL field id for the yield.

    config.yaml declares it ``null`` because no id is verified on cp_api yet, and
    a null id is dropped from the request entirely — so a test that wants to
    exercise the decoder has to supply one.
    """
    fields = dict(config.as_dict()["ibkr"]["cp_api"]["marketdata"]["underlying_snapshot_fields"])
    fields["dividend_yield"] = "7287"
    return make_client(config, underlying_snapshot_fields=fields, **overrides)


async def test_an_unverified_yield_field_id_is_dropped_from_the_request(config):
    """``null`` means no verified id: not requested, not decoded, column stays NULL.

    The same mechanism implied_vol_percentile and market_data_status ride on. A
    guessed id would put some other quantity into a yield column, and a yield is
    consumed by a pricer.
    """
    client = make_client(config)
    assert "dividend_yield" not in client.decode_underlying_snapshot({"conid": 1, "7287": "0.98"})


async def test_a_served_percent_yield_becomes_a_decimal(config):
    """0.98 PERCENT is 0.0098 as a decimal — the unit the whole schema uses."""
    client = _underlying_yield_client(config, dividend_yield_units="percent")
    decoded = client.decode_underlying_snapshot({"conid": 1, "7287": "0.98"})
    assert decoded["dividend_yield"] == pytest.approx(0.98 / PERCENT_TO_DECIMAL)
    assert decoded["dividend_yield"] == pytest.approx(0.0098)


async def test_the_two_decimal_places_ibkr_actually_serves_survive(config):
    """The connector's own tool description says "rounded to 1 decimal place".

    It is wrong. SPY came back 0.98 and XLP 2.62 on 2026-09-09, both two places.
    Nothing here may round to one, and no tolerance may be sized from that claim.
    """
    client = _underlying_yield_client(config, dividend_yield_units="percent")
    for served, expected in (("0.98", 0.0098), ("2.62", 0.0262)):
        decoded = client.decode_underlying_snapshot({"conid": 1, "7287": served})
        assert decoded["dividend_yield"] == pytest.approx(expected)


async def test_auto_falls_back_to_percent_rather_than_decimal(config):
    """The fallback differs from implied vol's on purpose.

    A bare 0.98 on this field is 0.98%, not a 98% yield. Reading it as a decimal
    would price a 1% payer as a 98% one.
    """
    client = _underlying_yield_client(config, dividend_yield_units="auto")
    decoded = client.decode_underlying_snapshot({"conid": 1, "7287": 0.98})
    assert decoded["dividend_yield"] == pytest.approx(0.0098)


async def test_a_percent_suffix_still_wins_under_auto(config):
    client = _underlying_yield_client(config, dividend_yield_units="auto")
    decoded = client.decode_underlying_snapshot({"conid": 1, "7287": "2.62%"})
    assert decoded["dividend_yield"] == pytest.approx(0.0262)


async def test_a_negative_yield_is_a_sentinel_and_is_dropped(config):
    """An instrument cannot pay out less than nothing. -1.0 is IBKR's "no value"."""
    client = _underlying_yield_client(config, dividend_yield_units="percent")
    assert client.decode_underlying_snapshot({"conid": 1, "7287": -1.0})["dividend_yield"] is None


async def test_a_zero_yield_is_a_reading_and_is_kept(config):
    """A non-payer is data. NULL means nobody collected one; they are not the same."""
    client = _underlying_yield_client(config, dividend_yield_units="percent")
    assert client.decode_underlying_snapshot({"conid": 1, "7287": 0.0})["dividend_yield"] == 0.0


async def test_an_unconverted_percent_is_refused_by_the_ceiling(config):
    """98.0 read as a decimal is a 9800% yield. That is a unit error, not a payout."""
    client = _underlying_yield_client(config, dividend_yield_units="decimal")
    assert client.decode_underlying_snapshot({"conid": 1, "7287": 98.0})["dividend_yield"] is None
