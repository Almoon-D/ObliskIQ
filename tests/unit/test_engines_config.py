"""The engines' configuration and the sleeve's records refuse what cannot work: lite's values
shipped as lite set them, a reason or a wrapper listed twice, a wrapper order with nothing but the
futures-tracking wrapper, an entry window starting at the minimum hold, a surface tenor or delta
outside what the chain collector buys, a position closing without its reason or before it opened,
a ledger event without its owner, a shadow entry priced off a later chain.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from options_rig import position
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema import ObliskConfig as Schema
from obliskiq.config.schema_engines import CommoditiesConfig, OptionSleeveConfig
from obliskiq.config.schema_grid import OptionSurfaceConfig
from obliskiq.config.schema_instruments import TenorConfig
from obliskiq.domain.enums import OptionRight
from obliskiq.domain.options import LedgerEvent, LedgerEventType, ShadowEntry

STAMP = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)


def test_the_ported_values_are_lites(shipped_settings: ObliskConfig) -> None:
    sleeve = shipped_settings.option_sleeve
    assert sleeve.loss_penalty_multiple == Decimal("2.0")
    assert sleeve.qualifying_exit_reasons == ["target", "time_window", "trailing_stop"]
    assert (sleeve.min_payoff_ratio, sleeve.max_concurrent_positions) == (3.0, 5)
    assert sleeve.min_premium_base_currency == Decimal(20)
    zombie = shipped_settings.zombie_test
    assert (zombie.max_delta, zombie.max_dte, zombie.consecutive_sessions_required) == (
        0.15,
        45,
        2,
    )
    assert zombie.book_penalty_at == "detection"
    risk = shipped_settings.equity_risk
    assert (risk.stop_atr_multiple, risk.trailing_stop_atr_multiple, risk.atr_period) == (
        2.5,
        3.0,
        20,
    )
    commodities = shipped_settings.commodities
    assert commodities.term_structure.contango_max_annualised_for_futures_wrapper == 0.05
    assert commodities.preferred_wrapper_order[-1] == "futures_tracking_wrapper"
    tenor = shipped_settings.tenor
    assert (tenor.catalyst_buffer_days, tenor.hold_days_min) == (30, 70)


def test_a_qualifying_reason_is_listed_once(shipped_settings: ObliskConfig) -> None:
    data = shipped_settings.option_sleeve.model_dump(mode="json")
    data["qualifying_exit_reasons"] = ["target", "target"]
    with pytest.raises(ValidationError, match="lists a reason twice"):
        OptionSleeveConfig.model_validate(data)


@pytest.mark.parametrize(
    ("order", "match"),
    [
        (["producer_equity", "producer_equity"], "lists a wrapper twice"),
        (["futures_tracking_wrapper"], "besides the futures-tracking one"),
    ],
)
def test_the_wrapper_order_offers_a_wrapper_the_ceiling_cannot_refuse(
    shipped_settings: ObliskConfig, order: list[str], match: str
) -> None:
    data = shipped_settings.commodities.model_dump(mode="json")
    with pytest.raises(ValidationError, match=match):
        CommoditiesConfig.model_validate({**data, "preferred_wrapper_order": order})


def test_the_entry_window_starts_above_the_minimum_hold(shipped_settings: ObliskConfig) -> None:
    tenor = shipped_settings.tenor.model_dump(mode="json")
    with pytest.raises(ValidationError, match="above hold_days_min"):
        TenorConfig.model_validate({**tenor, "hold_days_min": tenor["entry_dte_min"]})


def test_the_surface_reads_distinct_ascending_deltas() -> None:
    with pytest.raises(ValidationError, match="distinct and ascending"):
        OptionSurfaceConfig.model_validate({"tenors_days": [95], "deltas": [0.25, 0.10]})


@pytest.mark.parametrize(
    ("surface", "match"),
    [
        ({"tenors_days": [30, 180]}, r"tenors_days: every tenor must sit inside"),
        ({"tenors_days": [95, 400]}, r"tenors_days: every tenor must sit inside"),
        ({"deltas": [0.01, 0.25]}, r"deltas: every wing delta must sit inside"),
        ({"deltas": [0.10, 0.50]}, r"deltas: every wing delta must sit inside"),
    ],
)
def test_the_surface_reads_what_the_chain_collector_buys(
    shipped_settings: ObliskConfig, surface: dict[str, Any], match: str
) -> None:
    data = shipped_settings.model_dump(mode="json")
    data["grid"]["option_surface"] |= surface
    with pytest.raises(ValidationError, match=match):
        Schema.model_validate(data)


# ── the sleeve's records ──


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"closed_on": date(2026, 11, 2)}, "closes with the reason"),
        ({"exit_reason": "target"}, "closes with the reason"),
        (
            {"closed_on": date(2026, 9, 1), "exit_reason": "target"},
            "cannot close before it opened",
        ),
        ({"close_by": date(2027, 3, 1)}, "close_by falls between"),
        ({"close_by": date(2026, 9, 1)}, "close_by falls between"),
    ],
)
def test_a_position_is_coherent(changes: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        position(**changes)


def test_a_position_knows_its_right_and_whether_it_is_open() -> None:
    call = position()
    assert call.is_call
    assert call.is_open
    put = position(right=OptionRight.PUT, closed_on=date(2026, 11, 2), exit_reason="zombie")
    assert not put.is_call
    assert not put.is_open


def event(**changes: Any) -> LedgerEvent:
    fields: dict[str, Any] = {
        "event_id": None,
        "event_on": date(2026, 10, 22),
        "event_type": LedgerEventType.PREMIUM_COMMITTED,
        "position_id": 1,
        "conid": None,
        "amount": Decimal(300),
        "penalty_added": Decimal(0),
        "penalty_balance_after": Decimal(0),
        "exit_reason": None,
        "conversion": None,
        "note": "a commitment",
        "recorded_at": STAMP,
    }
    return LedgerEvent(**(fields | changes))


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        (
            {"event_type": LedgerEventType.QUALIFYING_GAIN, "conid": 8314},
            "names its instrument and exit reason",
        ),
        (
            {
                "event_type": LedgerEventType.QUALIFYING_GAIN,
                "position_id": None,
                "exit_reason": "target",
            },
            "names its instrument and exit reason",
        ),
        ({"position_id": None}, "event belongs to a position"),
    ],
)
def test_a_ledger_event_names_what_it_belongs_to(changes: dict[str, Any], match: str) -> None:
    assert event().position_id == 1
    with pytest.raises(ValidationError, match=match):
        event(**changes)


def test_a_shadow_entry_is_priced_off_a_chain_collected_by_its_session() -> None:
    fields: dict[str, Any] = {
        "as_of": date(2026, 10, 22),
        "underlying_conid": 756733,
        "expiry": date(2027, 1, 27),
        "strike": 525.0,
        "right": OptionRight.CALL,
        "snapshot_date": date(2026, 10, 22),
        "option_conid": None,
        "bid": 9.9,
        "ask": 10.1,
        "mid": 10.0,
        "iv": 0.19,
        "delta": 0.4,
        "multiplier": 100.0,
        "currency": "USD",
        "premium_base": Decimal("855.94"),
        "iv_percentile": 0.1,
        "market_data_status": None,
        "untested": ("price_ceiling",),
        "activation": "shadow: evidence fails",
        "computed_at": STAMP,
    }
    assert ShadowEntry(**fields).snapshot_date == fields["as_of"]
    with pytest.raises(ValidationError, match="collected by its session"):
        ShadowEntry(**(fields | {"snapshot_date": date(2026, 10, 23)}))
