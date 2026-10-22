"""The gate's directory of instruments and the prohibited categories.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Any

import pytest
from traded import ETF, EXPIRY, STOCK, instrument, option

from obliskiq.config.schema_instruments import InstrumentsConfig
from obliskiq.domain.enums import AssetClass
from obliskiq.execution.instruments import UniverseDirectory, prohibited_reasons


def test_the_universe_directory_describes_its_pinned_instruments(shipped_settings: Any) -> None:
    directory = UniverseDirectory.from_settings(shipped_settings)
    assert directory.describe(7089) is not None
    assert directory.describe(7089).symbol == "FCX"
    assert directory.describe(1) is None
    assert {"AEB", "IBIS", "LSEETF", "NYSE"} <= set(directory.listing_exchanges())
    on_fcx = option(800001, directory.describe(7089))
    assert UniverseDirectory.from_settings(shipped_settings, options=[on_fcx]).describe(800001)


def test_nothing_ordinary_is_prohibited(shipped_settings: Any) -> None:
    assert prohibited_reasons(STOCK, None, shipped_settings) == []
    assert prohibited_reasons(option(10, STOCK), STOCK, shipped_settings) == []


@pytest.mark.parametrize(
    "description", ["ProShares UltraPro QQQ", "Direxion Daily Bear 3x", "an inverse fund"]
)
def test_the_markers_match_the_description_whatever_its_case(
    shipped_settings: Any, description: str
) -> None:
    leveraged = instrument(5, AssetClass.ETF, description=description)
    (reason,) = prohibited_reasons(leveraged, None, shipped_settings)
    assert "leveraged or inverse" in reason


def test_the_ticker_is_never_matched(shipped_settings: Any) -> None:
    assert prohibited_reasons(instrument(6, symbol="ULTRA"), None, shipped_settings) == []


def test_futures_and_their_options_are_prohibited(shipped_settings: Any) -> None:
    future = instrument(7, AssetClass.FUTURE)
    assert prohibited_reasons(future, None, shipped_settings) == [
        f"{future.symbol} is a futures contract"
    ]
    (reason,) = prohibited_reasons(option(8, future), future, shipped_settings)
    assert "option on a futures contract" in reason


def test_an_option_on_a_leveraged_product_is_prohibited(shipped_settings: Any) -> None:
    leveraged = instrument(9, AssetClass.ETF, description="2x leveraged")
    (reason,) = prohibited_reasons(option(10, leveraged), leveraged, shipped_settings)
    assert leveraged.symbol in reason


def test_the_mandate_cannot_be_configured_away() -> None:
    markers = ["2X"]
    every = [
        "futures_contracts",
        "futures_options",
        "short_options",
        "leveraged_etps",
        "inverse_etps",
    ]
    with pytest.raises(ValueError, match="missing"):
        InstrumentsConfig(
            prohibited=every[:-1], allow_futures_options=False, leveraged_inverse_markers=markers
        )
    with pytest.raises(ValueError, match="never-exercise rule"):
        InstrumentsConfig(
            prohibited=every, allow_futures_options=True, leveraged_inverse_markers=markers
        )


def test_an_option_names_its_underlying_and_expiry_and_only_an_option_does() -> None:
    with pytest.raises(ValueError, match="only an option"):
        instrument(11, AssetClass.OPTION)
    with pytest.raises(ValueError, match="only an option"):
        instrument(12, underlying=1, expiry=EXPIRY)
    with pytest.raises(ValueError, match="only an option"):
        instrument(13, underlying=1)
    with pytest.raises(ValueError, match="its own underlying"):
        instrument(14, AssetClass.OPTION, underlying=14, expiry=EXPIRY)
    with pytest.raises(ValueError, match="positive"):
        instrument(15, multiplier=0)
    assert ETF.asset_class is AssetClass.ETF


def test_only_the_configured_categories_are_checked(shipped_settings: Any) -> None:
    only_futures = InstrumentsConfig.model_construct(
        prohibited=["futures_contracts"],
        allow_futures_options=False,
        leveraged_inverse_markers=["2X"],
    )
    settings = shipped_settings.model_copy(update={"instruments": only_futures})
    leveraged = instrument(16, AssetClass.ETF, description="2x leveraged")
    assert prohibited_reasons(leveraged, None, settings) == []
