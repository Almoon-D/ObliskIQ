"""The base of the domain: frozen models, UTC datetimes and the shared validated types.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from obliskiq.domain.base import (
    Conid,
    CountryCode,
    CurrencyCode,
    DomainModel,
    NonEmptyText,
    UtcDatetime,
)


class _Sample(DomainModel):
    when: UtcDatetime
    price: float
    currency: CurrencyCode
    country: CountryCode
    label: NonEmptyText
    conid: Conid


def _sample(**overrides: object) -> _Sample:
    fields: dict[str, object] = {
        "when": datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        "price": 1.5,
        "currency": "EUR",
        "country": "IE",
        "label": "x",
        "conid": 1,
    }
    fields.update(overrides)
    return _Sample.model_validate(fields)


def test_a_model_is_frozen():
    sample = _sample()
    with pytest.raises(ValidationError):
        sample.price = 2.0  # type: ignore[misc]


def test_a_model_refuses_an_unknown_field():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _sample(surprise=1)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_model_refuses_a_non_finite_number(bad):
    with pytest.raises(ValidationError):
        _sample(price=bad)


def test_a_naive_datetime_is_refused():
    with pytest.raises(ValidationError):
        _sample(when=datetime(2026, 10, 9, 12, 0))


def test_a_datetime_is_normalised_to_utc():
    plus_two = timezone(timedelta(hours=2))
    sample = _sample(when=datetime(2026, 10, 9, 14, 0, tzinfo=plus_two))
    assert sample.when == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert sample.when.utcoffset() == timedelta(0)


@pytest.mark.parametrize("bad", ["eur", "EU", "EURO", "E1R", ""])
def test_a_currency_must_be_three_capital_letters(bad):
    with pytest.raises(ValidationError, match="ISO 4217"):
        _sample(currency=bad)


@pytest.mark.parametrize("bad", ["ie", "IRL", "I", ""])
def test_a_country_must_be_two_capital_letters(bad):
    with pytest.raises(ValidationError, match="ISO 3166"):
        _sample(country=bad)


@pytest.mark.parametrize("bad", ["", "   "])
def test_text_must_not_be_blank(bad):
    with pytest.raises(ValidationError, match="must not be empty"):
        _sample(label=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_a_conid_is_positive(bad):
    with pytest.raises(ValidationError):
        _sample(conid=bad)
