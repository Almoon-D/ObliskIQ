"""The order path's configuration refuses what it must never accept.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_execution import (
    ExecutionConfig,
    ModesConfig,
    OrdersConfig,
    ReplyPolicyConfig,
)
from obliskiq.domain.enums import AssetClass


def test_the_shipped_order_path_configuration(shipped_settings: ObliskConfig) -> None:
    execution = shipped_settings.execution
    assert execution.modes.initial.value == "shadow"
    assert execution.reply_policy.no_market_data_message_id == "o354"
    confirmed = {entry.message_id for entry in execution.reply_policy.confirm}
    assert "o354" not in confirmed
    assert shipped_settings.discipline.entry_order_type == "limit_only"
    covered = {cls for rule in execution.quote_policy for cls in rule.asset_classes}
    assert covered == {
        AssetClass.OPTION,
        AssetClass.STOCK,
        AssetClass.ETF,
        AssetClass.ETC,
        AssetClass.BOND,
        AssetClass.FUND,
    }


@pytest.mark.parametrize("mode", ["live-small", "live"])
def test_a_deployment_cannot_start_live(mode: str) -> None:
    with pytest.raises(ValidationError):
        ModesConfig(initial=mode)  # type: ignore[arg-type]


def test_the_no_market_data_warning_is_never_confirmed_unconditionally() -> None:
    with pytest.raises(ValidationError):
        ReplyPolicyConfig.model_validate(
            {
                "confirm": [{"message_id": "o354", "when": "any"}],
                "no_market_data_message_id": "o354",
            }
        )
    with pytest.raises(ValidationError):
        ReplyPolicyConfig.model_validate(
            {
                "confirm": [
                    {"message_id": "o1", "when": "any"},
                    {"message_id": "o1", "when": "any"},
                ],
                "no_market_data_message_id": "o354",
            }
        )
    with pytest.raises(ValidationError):
        ReplyPolicyConfig.model_validate({"confirm": [], "no_market_data_message_id": "354"})


@pytest.mark.parametrize(
    "orders",
    [
        {"coid_max_length": 15, "max_reply_rounds": 1, "trades_lookup_days": 1},
        {"coid_max_length": 37, "max_reply_rounds": 1, "trades_lookup_days": 1},
        {"coid_max_length": 36, "max_reply_rounds": 0, "trades_lookup_days": 1},
        {"coid_max_length": 36, "max_reply_rounds": 1, "trades_lookup_days": 8},
    ],
)
def test_order_limits_stay_inside_what_ibkr_and_the_coid_allow(orders: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OrdersConfig.model_validate(orders)


def test_an_asset_class_has_one_quote_policy(shipped_settings: ObliskConfig) -> None:
    data = shipped_settings.execution.model_dump(mode="json")
    data["quote_policy"].append({"policy": "nav_priced", "asset_classes": ["stock"]})
    with pytest.raises(ValidationError, match="stock"):
        ExecutionConfig.model_validate(data)


def test_the_collar_ceiling_is_not_below_its_base(shipped_settings: ObliskConfig) -> None:
    from obliskiq.config.schema_execution import CollarConfig

    data = shipped_settings.execution.collar.model_dump()
    data["max_fraction"] = data["base_fraction"] / 2
    with pytest.raises(ValidationError, match="max_fraction"):
        CollarConfig.model_validate(data)
