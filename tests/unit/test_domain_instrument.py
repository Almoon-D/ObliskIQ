"""The instrument model.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from obliskiq.domain.enums import AssetClass, InstrumentRole


def test_a_valid_instrument(make_instrument):
    instrument = make_instrument()
    assert instrument.conid == 756733
    assert instrument.asset_class is AssetClass.ETF
    assert InstrumentRole.OPTION_UNDERLYING in instrument.roles


def test_unknown_attributes_are_none_not_defaults(make_instrument):
    instrument = make_instrument(ucits=None, kid_status=None, lot_size=None, domicile=None)
    assert instrument.ucits is None
    assert instrument.kid_status is None
    assert instrument.lot_size is None


def test_an_instrument_needs_a_role(make_instrument):
    with pytest.raises(ValidationError):
        make_instrument(roles=set())


@pytest.mark.parametrize("lot_size", [0, -100])
def test_a_lot_size_is_positive(make_instrument, lot_size):
    with pytest.raises(ValidationError):
        make_instrument(lot_size=lot_size)


def test_superseding_names_the_replacement_and_the_moment(make_instrument):
    when = datetime(2026, 6, 1, tzinfo=UTC)
    instrument = make_instrument(superseded_by=999, superseded_at=when)
    assert instrument.superseded_by == 999


def test_superseded_by_and_at_travel_together(make_instrument):
    when = datetime(2026, 6, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="together"):
        make_instrument(superseded_by=999)
    with pytest.raises(ValidationError, match="together"):
        make_instrument(superseded_at=when)


def test_an_instrument_cannot_supersede_itself(make_instrument):
    with pytest.raises(ValidationError, match="itself"):
        make_instrument(superseded_by=756733, superseded_at=datetime(2026, 6, 1, tzinfo=UTC))


def test_an_instrument_cannot_be_superseded_before_it_existed(make_instrument):
    with pytest.raises(ValidationError, match="before it was created"):
        make_instrument(superseded_by=999, superseded_at=datetime(2025, 1, 1, tzinfo=UTC))
