"""The closed vocabularies and the market-data labelling rules.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import pytest

from obliskiq.domain.enums import (
    MarketDataQuality,
    MarketDataStatus,
    market_data_quality,
    worst_market_data_quality,
)

LIVE = MarketDataQuality.LIVE
INDICATIVE = MarketDataQuality.INDICATIVE
UNREPORTED = MarketDataQuality.UNREPORTED


@pytest.mark.parametrize(
    ("flag", "status"),
    [
        ("REALTIME", MarketDataStatus.REALTIME),
        (" realtime ", MarketDataStatus.REALTIME),
        ("DELAYED", MarketDataStatus.DELAYED),
        ("FROZEN", MarketDataStatus.FROZEN),
        ("FROZEN_DELAYED", MarketDataStatus.FROZEN),
        ("delayed-frozen", MarketDataStatus.FROZEN),
        ("DPB", None),
        ("", None),
        (None, None),
    ],
)
def test_a_route_flag_is_decoded_or_left_unclassified(flag, status):
    assert MarketDataStatus.from_route_flag(flag) is status


@pytest.mark.parametrize(
    ("status", "quality"),
    [
        (MarketDataStatus.REALTIME, LIVE),
        (MarketDataStatus.DELAYED, INDICATIVE),
        (MarketDataStatus.FROZEN, INDICATIVE),
        (MarketDataStatus.NONE, UNREPORTED),
        (None, UNREPORTED),
    ],
)
def test_only_a_realtime_quote_is_live(status, quality):
    assert market_data_quality(status) is quality


@pytest.mark.parametrize(
    ("qualities", "worst"),
    [
        ([LIVE, LIVE], LIVE),
        ([LIVE, INDICATIVE, UNREPORTED], INDICATIVE),
        ([LIVE, UNREPORTED], UNREPORTED),
        ([], None),
    ],
)
def test_a_table_is_labelled_by_its_weakest_row(qualities, worst):
    assert worst_market_data_quality(qualities) is worst
