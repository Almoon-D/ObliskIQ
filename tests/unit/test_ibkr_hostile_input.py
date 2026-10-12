"""Hostile broker payloads decode to explicit absence, never to a fabricated number.

Ported in intent from ObliskIQ-lite-MCP's ``tests/test_hostile_input.py``: its ledger and server
cases arrive with their modules; the IBKR-facing rules are here. Non-finite and overflowing
amounts, sentinels, crossed books, wrong shapes and impossible timestamps from the broker become
``None`` or an empty result, and the decoders never raise on them.
"""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.ibkr import decode
from obliskiq.ibkr.errors import DecodeError
from obliskiq.ibkr.guard import AccountGuard
from obliskiq.ibkr.models import Account, LastKind, Position

WRONG_SHAPES: list[Any] = [None, "", "error", 0, 1.5, True, [], {}, [None, 1, "x"], {"x": [1]}]


@pytest.fixture
def fields(shipped_settings) -> dict[str, str]:
    return decode.field_map(shipped_settings.ibkr.marketdata.snapshot_fields)


@pytest.mark.parametrize(
    "raw", ["nan", "NaN", "inf", "-Infinity", "1e999", "-1e999", float("nan"), float("inf")]
)
def test_non_finite_and_overflowing_amounts_are_absent(raw):
    assert decode.to_decimal(raw) is None


@pytest.mark.parametrize("raw", [None, True, "", "  ", "abc", "1.2.3", "--1"])
def test_unparseable_amounts_are_absent(raw):
    assert decode.to_decimal(raw) is None


def test_amounts_keep_their_exact_digits():
    assert decode.to_decimal(0.1) == Decimal("0.1")
    assert decode.to_decimal("1,234.50") == Decimal("1234.50")
    assert decode.to_decimal(7) == Decimal(7)


def test_a_non_finite_amount_cannot_be_put_into_a_model():
    with pytest.raises(ValidationError, match="finite number"):
        Position(
            conid=1,
            quantity=Decimal("NaN"),
            market_price=None,
            market_value=None,
            average_cost=None,
            currency=None,
            asset_class=None,
            description=None,
            listing_exchange=None,
        )


@pytest.mark.parametrize("raw", [None, -1, 0, "x", 1e30, float("nan"), "inf"])
def test_impossible_timestamps_are_absent(raw):
    assert decode.epoch_ms_to_utc(raw) is None
    assert decode.epoch_s_to_utc(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (None, None)),
        (True, (None, None)),
        (float("nan"), (None, None)),
        (101.5, (101.5, LastKind.TRADE)),
        ("C101.5", (101.5, LastKind.PRIOR_CLOSE)),
        ("H101.5", (101.5, LastKind.HALTED)),
        ("L9.5", (9.5, LastKind.HALTED)),
        ("X101.5", (None, None)),
        ("C", (None, None)),
        ("Cnan", (None, None)),
        ("", (None, None)),
    ],
)
def test_prices_carry_their_prefix_and_refuse_unknown_ones(raw, expected):
    assert decode.decode_price(raw) == expected


@pytest.mark.parametrize(
    "flag", [None, "", "  ", "?", "Q", 7, "REALTIME_X", "Dx", "RPBdd", "NOT_REALTIME", "Delayed?"]
)
def test_an_unknown_availability_flag_is_kept_raw_and_undecoded(flag, shipped_settings):
    status, raw, delay, basis = decode.decode_availability(
        flag, delayed_bound_seconds=shipped_settings.ibkr.marketdata.delayed_quote_bound_seconds
    )
    assert status is None
    assert delay is None
    assert basis is None
    assert raw in (None, str(flag).strip())


@pytest.mark.parametrize(
    ("flag", "status"),
    [
        ("DPB", "delayed"),
        ("ZBd", "frozen"),
        ("Y", "frozen"),
        ("N", "none"),
        ("REALTIME", "realtime"),
        ("DELAYED_FROZEN", "frozen"),
        ("Delayed-Frozen", "frozen"),
        ("delayed", "delayed"),
    ],
)
def test_codes_and_route_spellings_each_decode_by_their_own_rule(flag, status, shipped_settings):
    """A 6509 code by its first letter; a spelled-out flag as a whole word, so DELAYED_FROZEN is
    frozen (the stricter claim), never delayed by its first letter."""
    decoded, raw, _, _ = decode.decode_availability(
        flag, delayed_bound_seconds=shipped_settings.ibkr.marketdata.delayed_quote_bound_seconds
    )
    assert decoded is not None
    assert decoded.value == status
    assert raw == flag


def test_a_crossed_book_is_flagged_and_has_no_mid(shipped_settings, fields):
    quote = decode.decode_quote(
        1, {fields["bid"]: "10.5", fields["ask"]: "10.0"}, fields, shipped_settings.ibkr
    )
    assert quote.crossed
    assert quote.mid is None


def test_a_snapshot_row_of_junk_decodes_to_absence(shipped_settings, fields):
    row = dict.fromkeys(fields.values(), "garbage")
    quote = decode.decode_quote(1, row, fields, shipped_settings.ibkr)
    for name in fields:
        if name != "market_data_status":
            assert getattr(quote, name, None) is None, name
    assert quote.market_data_status is None
    assert not quote.crossed


def test_sentinels_and_negative_counts_are_dropped(shipped_settings, fields):
    row = {
        fields["implied_vol"]: "-1",
        fields["volume"]: "-5",
        fields["open_interest"]: "-1",
        fields["delta"]: "3.5",
    }
    quote = decode.decode_quote(1, row, fields, shipped_settings.ibkr)
    assert quote.implied_vol is None
    assert quote.volume is None
    assert quote.open_interest is None
    assert quote.delta is None


@pytest.mark.parametrize("body", WRONG_SHAPES)
def test_every_decoder_survives_a_wrong_shape(body, shipped_settings):
    bound = shipped_settings.ibkr.marketdata.delayed_quote_bound_seconds
    assert decode.decode_history(1, body, delayed_bound_seconds=bound).bars == ()
    assert decode.decode_accounts(body) == []
    if body is None or body == []:
        assert decode.decode_positions(body) == []
    else:
        with pytest.raises(DecodeError):
            decode.decode_positions(body)
    assert decode.decode_search(body) == []
    assert decode.decode_contract_info(1, body).conid == 1
    assert decode.decode_algos(body) == ()
    assert decode.decode_trading_schedule(1, None, body).sessions == ()
    assert decode.decode_scanner_params(body).scan_types == ()
    assert decode.decode_scanner_run(body) == ()
    assert decode.decode_futures(body, ["ES"]) == {"ES": ()}
    assert decode.decode_bond_filters(body) == ()


def test_history_rows_without_a_valid_time_are_dropped(shipped_settings):
    body = {
        "data": [
            "row",
            {"t": None, "c": 1},
            {"t": -5, "c": 1},
            {"t": 1_760_000_000_000, "o": "nan", "h": "inf", "l": 1, "c": 2, "v": "x"},
        ],
        "mdAvailability": "Zz",
    }
    history = decode.decode_history(
        1, body, delayed_bound_seconds=shipped_settings.ibkr.marketdata.delayed_quote_bound_seconds
    )
    (bar,) = history.bars
    assert bar.open is None
    assert bar.high is None
    assert bar.volume is None
    assert bar.close == 2.0


def test_account_and_search_rows_that_are_not_objects_are_skipped():
    assert decode.decode_accounts(["DU_PAPER", None, {"currency": "USD"}]) == []
    assert decode.decode_search(["x", None]) == []


def test_a_search_section_without_a_type_is_skipped():
    (match,) = decode.decode_search(
        [
            {
                "conid": "5",
                "symbol": "X",
                "sections": ["STK", {"months": "JAN26"}, {"secType": "stk"}],
            }
        ]
    )
    assert [section.sec_type for section in match.sections] == ["STK"]


def test_scanner_rows_without_a_conid_are_dropped():
    body = {"contracts": ["x", {"con_id": 0}, {"con_id": "abc"}, {"con_id": 42, "symbol": "Z"}]}
    assert [row.conid for row in decode.decode_scanner_run(body)] == [42]


def test_scanner_location_trees_of_the_wrong_shape_are_skipped():
    body = {"location_tree": ["x", {"locations": [{"type": "STK.US"}, "y", {"type": None}]}]}
    assert decode.decode_scanner_params(body).locations == ("STK.US",)


def test_a_bad_currency_is_absent():
    assert decode.currency_code("usd") == "USD"
    assert decode.currency_code("US Dollar") is None
    assert decode.currency_code(None) is None


def test_the_guard_never_shows_the_account_id_in_its_repr():
    guard = AccountGuard(account_id="DU12345", profile="paper", paper_prefixes=("DU",))
    assert "12345" not in repr(guard)
    assert "paper" in repr(guard)


def test_an_account_never_prints_its_id():
    account = Account(account_id="DU12345", currency="EUR", account_type="DEMO")
    for shown in (repr(account), str(account), f"{account}", f"{account!s}"):
        assert "12345" not in shown
