"""CommandParser's deterministic grammar: an owner message → an :data:`OwnerCommand`, no LLM.

MASTER_PLAN §5.10 gives CommandParser a grammar that works without the LLM; this is it, in English
and Spanish (Session 16 puts an LLM in front of it for any other wording). What it recognises:

* a deposit: *I added 1237,57€ to your cash*, *deposited 500 USD*, *he ingresado 1.000 €*;
* a dated withdrawal: *in 15 days I need 1,000 €*, *en 15 días necesito retirar 1.000 €*,
  *withdraw 500 EUR by 2026-12-20*, *retirar 300 € el 20/12/2026*;
* a setting: *set thesis.caps.position 0.20*, *pon thesis.caps.position a 0.20*;
* an answer: *Q3 postpone*, *answer Q3 reduce*, *responde Q3 proceed*.

Amounts take either decimal mark: with both a point and a comma the last one is the decimal mark;
with one of them, a group of exactly three digits after it is a thousands separator (1.000 and
1,000 are a thousand), anything else a decimal mark (1237,57). A message without a currency is in
the base currency. Whatever it reads is echoed for confirmation, so a misreading costs a retry,
never a record. Anything else is ``None``: the caller says it did not understand.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from ..domain.owner_commands import (
    AnswerCommand,
    DepositCommand,
    OwnerCommand,
    SettingCommand,
    WithdrawalCommand,
)

#: Currency words and symbols → ISO code.
CURRENCIES = {
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "$": "USD",
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "dólar": "USD",
    "dólares": "USD",
    "£": "GBP",
    "gbp": "GBP",
    "chf": "CHF",
}
DAYS_PER_WEEK = 7
_CURRENCY = "|".join(sorted((re.escape(word) for word in CURRENCIES), key=len, reverse=True))
_NUMBER = r"\d[\d.,  ]*\d|\d"
_AMOUNT = re.compile(
    rf"(?:(?P<pre>{_CURRENCY})\s*(?P<a1>{_NUMBER}))|(?:(?P<a2>{_NUMBER})\s*(?P<post>{_CURRENCY})?)",
    re.IGNORECASE,
)
_DEPOSIT = re.compile(
    r"\b(added|add|deposited|deposit|transferred in|put in|ingresad[oa]|ingresé|ingreso|"
    r"he ingresado|añadí|he añadido|añadido|metí|he metido)\b",
    re.IGNORECASE,
)
_WITHDRAW = re.compile(
    r"\b(withdraw|withdrawal|take out|need|retirar|retiro|retirada|sacar|necesito)\b",
    re.IGNORECASE,
)
_RELATIVE = re.compile(
    r"\b(?:in|en|within|dentro de)\s+(?P<count>\d+)\s+(?P<unit>days?|d[ií]as?|weeks?|semanas?)\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b")
_DMY_DATE = re.compile(r"\b(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{4})\b")
_SETTING = re.compile(
    r"^\s*/?(?:set|pon|cambia|change)\s+(?P<key>[a-z][a-z0-9_]*(?:\.[A-Za-z0-9_]+)+)"
    r"\s+(?:to\s+|a\s+|=\s*)?(?P<value>\S.*?)\s*$",
    re.IGNORECASE,
)
_ANSWER = re.compile(
    r"^\s*/?(?:answer|responde|respuesta|respondo)?\s*q(?P<id>\d+)\s*[:=]?\s+(?P<option>[\w-]+)\s*$",
    re.IGNORECASE,
)
_GROUPS_OF_THREE = re.compile(r"^\d{1,3}([.,]\d{3})+$")


def parse_amount(text: str) -> Decimal | None:
    """A written amount as a number, either decimal mark; ``None`` when it is not one."""
    digits = re.sub(r"\s", "", text)
    if "." in digits and "," in digits:
        decimal_mark = "." if digits.rfind(".") > digits.rfind(",") else ","
        thousands = "," if decimal_mark == "." else "."
        digits = digits.replace(thousands, "").replace(decimal_mark, ".")
    elif _GROUPS_OF_THREE.match(digits):
        digits = digits.replace(".", "").replace(",", "")
    else:
        digits = digits.replace(",", ".")
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _money(text: str, base_currency: str) -> tuple[Decimal, str] | None:
    """The first amount in ``text`` that is not part of a date or a count of days."""
    cleaned = _ISO_DATE.sub(" ", _DMY_DATE.sub(" ", _RELATIVE.sub(" ", text)))
    for match in _AMOUNT.finditer(cleaned):
        raw = match["a1"] or match["a2"]
        amount = parse_amount(raw)
        if amount is None:
            continue
        word = (match["pre"] or match["post"] or "").lower()
        return amount, CURRENCIES.get(word, base_currency)
    return None


def _due(text: str, today: date) -> date | None:
    relative = _RELATIVE.search(text)
    if relative is not None:
        count = int(relative["count"])
        unit = relative["unit"].lower()
        days = count * DAYS_PER_WEEK if unit.startswith(("week", "semana")) else count
        return today + timedelta(days=days)
    for pattern in (_ISO_DATE, _DMY_DATE):
        found = pattern.search(text)
        if found is not None:
            try:
                return date(int(found["y"]), int(found["m"]), int(found["d"]))
            except ValueError:
                return None
    return None


def parse_command(text: str, *, today: date, base_currency: str) -> OwnerCommand | None:
    """The command ``text`` asks for, or ``None`` when the grammar does not recognise it."""
    try:
        return _parse(text, today=today, base_currency=base_currency)
    except ValidationError:
        return None


def _parse(text: str, *, today: date, base_currency: str) -> OwnerCommand | None:
    setting = _SETTING.match(text)
    if setting is not None:
        return SettingCommand(key=setting["key"], value=setting["value"])
    answer = _ANSWER.match(text)
    if answer is not None:
        return AnswerCommand(question_id=int(answer["id"]), option=answer["option"])
    money = _money(text, base_currency)
    if money is None:
        return None
    amount, currency = money
    if _WITHDRAW.search(text):
        due = _due(text, today)
        if due is None or due < today:
            return None
        return WithdrawalCommand(amount=amount, currency=currency, due=due)
    if _DEPOSIT.search(text):
        return DepositCommand(amount=amount, currency=currency)
    return None


def describe(command: OwnerCommand) -> str:
    """The command in plain words, for the owner to confirm."""
    if isinstance(command, DepositCommand):
        return f"record a deposit of {command.amount} {command.currency}"
    if isinstance(command, WithdrawalCommand):
        return (
            f"reserve {command.amount} {command.currency} for a withdrawal by "
            f"{command.due.isoformat()}"
        )
    if isinstance(command, SettingCommand):
        return f"set {command.key} to {command.value}"
    return f"answer Q{command.question_id} with {command.option}"
