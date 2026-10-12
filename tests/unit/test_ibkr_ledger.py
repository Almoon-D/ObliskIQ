"""The ledger: NAV in the configured base currency only, cash per currency, never a guess.

Ported in intent from ObliskIQ-lite-MCP's ``tests/test_collect_nav.py`` (the decoding cases; the
NAV store writer arrives with Session 7) and its live reading [CP ✓ 2026-09-10]: the BASE row
labels itself ``BASE`` on ``currency`` and ``secondkey``, and the per-currency row at an exchange
rate of exactly one names the base currency.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from obliskiq.ibkr.decode import decode_ledger, ledger_base_currency

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ibkr"


@pytest.fixture
def ledger() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "ledger.json").read_text(encoding="utf-8"))
    return data


def test_the_nav_is_read_in_the_base_currency(ledger):
    reading = decode_ledger(ledger, base_currency="EUR")
    assert reading.nav is not None
    assert reading.nav.currency == "EUR"
    assert reading.nav.nav == Decimal("11170.338885808784")
    assert reading.nav_refusal is None


def test_the_base_currency_is_read_from_the_row_whose_rate_is_one(ledger):
    assert ledger_base_currency(ledger) == "EUR"


def test_cash_is_reported_per_currency_and_exactly(ledger):
    reading = decode_ledger(ledger, base_currency="EUR")
    by_currency = {balance.currency: balance for balance in reading.balances}
    assert set(by_currency) == {"EUR", "USD"}, "the BASE row is not a currency"
    assert by_currency["USD"].cash == Decimal("594.50")
    assert by_currency["EUR"].settled_cash == Decimal("10000.0")


def test_a_ledger_whose_base_is_another_currency_is_refused_not_converted(ledger):
    reading = decode_ledger(ledger, base_currency="USD")
    assert reading.nav is None
    assert reading.nav_refusal is not None
    assert "refused, not converted" in reading.nav_refusal
    assert reading.base_currency == "EUR"


def test_a_ledger_naming_no_currency_at_par_reports_none(ledger):
    ledger["EUR"]["exchangerate"] = 0.99
    assert ledger_base_currency(ledger) is None
    reading = decode_ledger(ledger, base_currency="EUR")
    assert reading.nav is None
    assert reading.nav_refusal == "the ledger's base currency cannot be identified"


def test_two_currencies_at_par_are_an_ambiguity_and_not_a_coin_flip(ledger):
    ledger["USD"]["exchangerate"] = 1
    assert ledger_base_currency(ledger) is None


def test_a_rate_near_one_is_not_one(ledger):
    ledger["EUR"]["exchangerate"] = 1.0000001
    assert ledger_base_currency(ledger) is None


def test_a_route_that_names_the_code_is_believed_over_the_rates(ledger):
    ledger["BASE"]["currency"] = "EUR"
    ledger["EUR"]["exchangerate"] = 0.5
    assert ledger_base_currency(ledger) == "EUR"


def test_a_ledger_with_no_base_row_reads_as_no_nav_not_zero(ledger):
    del ledger["BASE"]
    reading = decode_ledger(ledger, base_currency="EUR")
    assert reading.nav is None
    assert reading.nav_refusal == "the ledger has no BASE row"
    assert reading.balances, "cash is still reported"


def test_a_base_row_with_no_value_reads_as_no_nav_not_zero(ledger):
    ledger["BASE"]["netliquidationvalue"] = None
    reading = decode_ledger(ledger, base_currency="EUR")
    assert reading.nav is None
    assert "no readable net liquidation value" in (reading.nav_refusal or "")


@pytest.mark.parametrize("value", ["NaN", float("inf"), "inf", "", "1e999"])
def test_a_non_finite_nav_is_absent(ledger, value):
    ledger["BASE"]["netliquidationvalue"] = value
    assert decode_ledger(ledger, base_currency="EUR").nav is None


def test_a_cash_row_without_a_readable_balance_is_named_as_unreadable(ledger):
    ledger["USD"]["cashbalance"] = "n/a"
    reading = decode_ledger(ledger, base_currency="EUR")
    assert [balance.currency for balance in reading.balances] == ["EUR"]
    assert reading.unreadable_currencies == ("USD",)


@pytest.mark.parametrize("body", [None, [], "BASE", 7])
def test_a_ledger_that_is_not_an_object_reads_as_nothing(body):
    reading = decode_ledger(body, base_currency="EUR")
    assert reading.nav is None
    assert reading.balances == ()
