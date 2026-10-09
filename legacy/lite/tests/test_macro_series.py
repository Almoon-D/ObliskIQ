"""Every series something needs is a series something collects.

This file exists because of a failure that looked like success. A macro run
reported 53 of 53 targets ok and wrote 59,173 rows, and the store still held no
USD or EUR risk-free rate: ``macro.risk_free_rate.series_by_currency`` named
``DTB3`` and ``ECBESTRVOLWGTTRMDMNRT``, and neither was declared under
``macro.series``, so no collector ever asked for them.

Nothing errored. ``cached_risk_free_rate`` returned ``None``, every option gate
needing a rate reported itself untested, and an untested gate on a young store is
indistinguishable from an untested gate on a broken one. That is the same class as
the orphan-config-key failure ``test_no_unread_config_keys`` exists for, one level
along: there, a key nothing reads; here, a series nothing pulls.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations


def _declared_ids(config) -> set[str]:
    return {str(entry["id"]) for entry in config.require_list("macro.series")}


def test_every_configured_risk_free_rate_series_is_collected(config):
    """The pricer's discount rate has to be in the store, not only in the config.

    A currency whose rate series is never pulled prices nothing: the solve refuses
    the session, the theta budget reports untested, and both look like a data gap
    rather than a wiring one.
    """
    configured = config.section("macro.risk_free_rate").get("series_by_currency", {})
    missing = sorted(
        f"{currency} -> {series}"
        for currency, series in dict(configured).items()
        if str(series) not in _declared_ids(config)
    )
    assert not missing, (
        "macro.risk_free_rate.series_by_currency names series that macro.series "
        f"never collects, so the rate is unreadable for those currencies: {missing}"
    )


def test_the_check_is_not_vacuous(config):
    """Both halves have to be non-empty, or the assertion above proves nothing."""
    assert _declared_ids(config)
    assert dict(config.section("macro.risk_free_rate").get("series_by_currency", {}))


def test_every_declared_series_id_is_unique(config):
    """A duplicate id is a second declaration that silently wins or loses."""
    ids = [str(entry["id"]) for entry in config.require_list("macro.series")]
    duplicates = sorted({name for name in ids if ids.count(name) > 1})
    assert not duplicates, f"macro.series declares these ids more than once: {duplicates}"


#: What a market row in universe.yaml can name. The same five build_grid reads
#: for macro freshness, and the only route from a market to a macro series.
MARKET_SERIES_KEYS = (
    "policy_rate_series",
    "long_rate_series",
    "cpi_series",
    "fx_series",
    "cot_series",
)


def _markets() -> dict:
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / "universe.yaml").read_text())["markets"]


def test_every_macro_series_a_market_names_is_collected(config):
    """The mirror of ``test_every_macro_series_has_a_consumer``, and the sharper half.

    That one catches a series config declares and nothing reads, which wastes a
    request. This one catches a series a MARKET reads and nothing declares, which
    is silent: ``macro_series`` returns an empty frame, the indicator reports the
    series absent, and an absent series on a young store reads exactly like one
    that has not been collected yet. A typo in an id would survive a green run.

    ``null`` is fine and is the point of the check being on the name rather than
    the key: a market that declares no CPI is a stated coverage gap, and a market
    that declares one nobody collects is a mistake.
    """
    declared = _declared_ids(config)
    missing = sorted(
        f"{market}.{key} -> {spec[key]}"
        for market, spec in _markets().items()
        for key in MARKET_SERIES_KEYS
        if spec.get(key) and str(spec[key]) not in declared
    )
    assert not missing, (
        "universe.yaml points markets at macro series that macro.series never "
        f"collects, so those cells read as absent rather than as broken: {missing}"
    )


def test_the_market_check_is_not_vacuous():
    """There are markets, and they do name series."""
    markets = _markets()
    assert markets
    assert any(spec.get(key) for spec in markets.values() for key in MARKET_SERIES_KEYS)
