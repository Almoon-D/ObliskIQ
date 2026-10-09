"""config.yaml loads, and the keys code depends on exist.

The point is not that YAML parses. It is that every key referenced anywhere in
the package is present, so a missing threshold fails the build rather than
surfacing as a KeyError on the VM at 3am.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import pytest

from obliskiq.config import Config, load_config
from obliskiq.errors import ConfigError


def test_config_loads_and_is_a_mapping(config):
    assert isinstance(config, Config)
    assert config.require_str("account.base_currency") == "EUR"


def test_missing_key_raises_rather_than_defaulting(config):
    with pytest.raises(ConfigError, match="missing config key"):
        config.get("tenor.does_not_exist")


def test_explicit_default_is_honoured(config):
    assert config.get("tenor.does_not_exist", "fallback") == "fallback"


def test_type_checks_reject_the_wrong_shape(config):
    with pytest.raises(ConfigError, match="must be a number"):
        config.require_float("account.base_currency")
    with pytest.raises(ConfigError, match="must be an integer"):
        config.require_int("option_sleeve.budget_pct")


def test_booleans_are_not_accepted_as_numbers():
    cfg = Config({"flag": True})
    with pytest.raises(ConfigError):
        cfg.require_float("flag")
    assert cfg.require_bool("flag") is True


def test_section_returns_a_bound_namespace(config):
    tenor = config.section("tenor")
    assert tenor.require_int("entry_dte_min") == config.require_int("tenor.entry_dte_min")


def test_require_choice_rejects_an_unlisted_value(config):
    with pytest.raises(ConfigError, match="must be one of"):
        config.require_choice("account.base_currency", ["USD", "GBP"])


@pytest.mark.parametrize(
    "key",
    [
        # Session 1 additions.
        "positions.external_data_stale_days",
        "commodities.agri_linkage.track_fertiliser_to_crop_lag",
        "commodities.agri_linkage.lag_months",
        "commodities.nitrogen_input_driver",
        # Engineering parameters that keep literals out of the code. The log
        # FORMAT is not among them: it is a per-deployment choice and comes from
        # OBLISK_LOG_FORMAT, not from config.yaml.
        "runtime.logging.utc_timestamps",
        "runtime.sqlite.journal_mode",
        "runtime.sqlite.busy_timeout_ms",
        "http.retries",
        "http.max_delay_seconds",
        "numerics.bsm.day_count_basis",
        "numerics.bsm.min_tau_years",
        "numerics.implied_vol.seed_method",
        "numerics.implied_vol.max_newton_iterations",
        "numerics.implied_vol.max_bisection_iterations",
        "numerics.implied_vol.min_vega_for_reported_iv",
        "macro.risk_free_rate.series_by_currency",
        "macro.risk_free_rate.on_failure",
        "ibkr.cp_api.base_path",
        "ibkr.cp_api.oauth.realm",
        "ibkr.cp_api.session.tickle_interval_seconds",
        "ibkr.cp_api.marketdata.snapshot_fields",
        "ibkr.cp_api.secdef.exact_ticker_match_first",
    ],
)
def test_required_keys_exist(config, key):
    assert config.get(key) is not None


def test_agriculture_bucket_was_split_into_three(config):
    buckets = config.require_list("commodities.buckets")
    assert "agriculture" not in buckets, "the single agriculture bucket was replaced"
    for bucket in ("agri_inputs", "grains_oilseeds", "softs"):
        assert bucket in buckets


def test_seasonal_sensitive_names_only_real_buckets(config):
    """A retired bucket name left in seasonal_sensitive would silently match nothing."""
    buckets = set(config.require_list("commodities.buckets"))
    seasonal = config.require_list("commodities.history.seasonal_sensitive")
    dangling = [
        name
        for name in seasonal
        if name not in buckets and name not in {"natural_gas", "heating_oil", "gasoline"}
    ]
    assert not dangling, f"seasonal_sensitive names non-existent buckets: {dangling}"


def test_risk_free_rate_has_a_series_for_the_base_currency(config):
    base = config.require_str("account.base_currency")
    series = config.get("macro.risk_free_rate.series_by_currency")
    assert base in series, f"no risk-free-rate series for the base currency {base}"


def test_config_file_missing_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.yaml")


def test_malformed_yaml_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("a:\n  - b\n c: broken\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)
