"""CommandParser's grammar: owner messages in English and Spanish to commands, with no LLM.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal

import pytest

from obliskiq.domain.owner_commands import (
    AnswerCommand,
    DepositCommand,
    SettingCommand,
    WithdrawalCommand,
)
from obliskiq.server.commands import describe, parse_amount, parse_command

TODAY = date(2026, 11, 16)
D = Decimal


def _parse(text: str) -> object:
    return parse_command(text, today=TODAY, base_currency="EUR")


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("I added 1237,57€ to your cash", DepositCommand(amount=D("1237.57"), currency="EUR")),
        ("he ingresado 1.000 €", DepositCommand(amount=D("1000"), currency="EUR")),
        ("deposited $1,500.25", DepositCommand(amount=D("1500.25"), currency="USD")),
        ("I added 500 to your cash", DepositCommand(amount=D("500"), currency="EUR")),
        ("ingresé 2.000,50 euros", DepositCommand(amount=D("2000.50"), currency="EUR")),
        (
            "en 15 días necesito retirar 1.000 €",
            WithdrawalCommand(amount=D("1000"), currency="EUR", due=date(2026, 12, 1)),
        ),
        (
            "in 2 weeks I need 300 usd",
            WithdrawalCommand(amount=D("300"), currency="USD", due=date(2026, 11, 30)),
        ),
        (
            "withdraw 500 EUR by 2026-12-20",
            WithdrawalCommand(amount=D("500"), currency="EUR", due=date(2026, 12, 20)),
        ),
        (
            "retirar 300 € el 20/12/2026",
            WithdrawalCommand(amount=D("300"), currency="EUR", due=date(2026, 12, 20)),
        ),
        ("set thesis.caps.position 0.20", SettingCommand(key="thesis.caps.position", value="0.20")),
        (
            "pon thesis.caps.position a 0.20",
            SettingCommand(key="thesis.caps.position", value="0.20"),
        ),
        ("Q3 postpone", AnswerCommand(question_id=3, option="postpone")),
        ("answer q12 reduce", AnswerCommand(question_id=12, option="reduce")),
        ("responde Q4: proceed", AnswerCommand(question_id=4, option="proceed")),
    ],
)
def test_the_grammar_reads_the_owners_messages(text: str, command: object) -> None:
    assert _parse(text) == command


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "necesito 5 €",  # a withdrawal without a date
        "added 0 €",  # no amount
        "withdraw 500 € by 2026-02-30",  # no such date
        "withdraw 500 € by 2026-01-01",  # already past
        "the market fell 3 %",  # neither a deposit nor a withdrawal
        "Q0 postpone",  # no question 0
    ],
)
def test_anything_else_is_not_understood(text: str) -> None:
    assert _parse(text) is None


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1.000", D("1000")),
        ("1,000", D("1000")),
        ("1.5", D("1.5")),
        ("1237,57", D("1237.57")),
        ("1.234.567,89", D("1234567.89")),
        ("1,234,567.89", D("1234567.89")),
        ("12 500", D("12500")),
        ("0", None),
        ("1.2.3", None),
    ],
)
def test_amounts_take_either_decimal_mark(text: str, value: Decimal | None) -> None:
    assert parse_amount(text) == value


def test_each_command_is_described_for_confirmation() -> None:
    assert describe(DepositCommand(amount=D("5"), currency="EUR")) == "record a deposit of 5 EUR"
    assert describe(WithdrawalCommand(amount=D("5"), currency="USD", due=TODAY)) == (
        "reserve 5 USD for a withdrawal by 2026-11-16"
    )
    assert describe(SettingCommand(key="a.b", value="1")) == "set a.b to 1"
    assert describe(AnswerCommand(question_id=2, option="x")) == "answer Q2 with x"
