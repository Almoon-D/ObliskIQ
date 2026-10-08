"""The risk-free rate: never a hardcoded default, always the right currency.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy

import httpx
import pytest

from conftest import without_backoff
from obliskiq.config import Config
from obliskiq.errors import ConfigError, UpstreamError
from obliskiq.macro.rates import RiskFreeRateProvider


def fred_response(value: str, date: str = "2026-08-21"):
    return httpx.Response(200, json={"observations": [{"date": date, "value": value}]})


def provider(config, store, handler, *, api_key="testkey"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RiskFreeRateProvider(without_backoff(config), store, client=client, fred_api_key=api_key)


def config_with(config, **overrides):
    data = copy.deepcopy(config.as_dict())
    data["macro"]["risk_free_rate"].update(overrides)
    return Config(data)


async def test_the_rate_is_fetched_and_returned_as_a_decimal(config, store):
    """FRED publishes percent; the engine takes decimals. Convert once, here."""
    rfr = provider(config, store, lambda r: fred_response("3.25"))
    rate = await rfr.get("USD")
    assert rate.rate == pytest.approx(0.0325)
    assert rate.percent == pytest.approx(3.25)
    assert rate.stale is False


async def test_the_base_currency_is_the_default_and_is_not_usd(config, store):
    """The original hardcoded DTB3, the US 3-month bill, while the account's base
    currency is EUR and its cash yields €STR. Discounting an EUR option at a USD
    rate is simply the wrong rate."""
    seen = {}

    def handler(request):
        seen["series"] = request.url.params["series_id"]
        return fred_response("2.10")

    rfr = provider(config, store, handler)
    rate = await rfr.get()
    assert rate.currency == "EUR"
    assert seen["series"] != "DTB3"
    assert seen["series"] == config.get("macro.risk_free_rate.series_by_currency")["EUR"]


async def test_each_currency_uses_its_own_series(config, store):
    seen = []

    def handler(request):
        seen.append(request.url.params["series_id"])
        return fred_response("1.0")

    rfr = provider(config, store, handler)
    await rfr.get("EUR")
    await rfr.get("USD")
    assert len(set(seen)) == 2


async def test_an_unconfigured_currency_raises_rather_than_guessing(config, store):
    rfr = provider(config, store, lambda r: fred_response("1.0"))
    with pytest.raises(ConfigError, match="never guessed"):
        await rfr.get("ZWL")


async def test_a_failure_raises_instead_of_returning_4_point_5(config, store):
    """The heart of it. The original returned a hardcoded 4.5 when FRED failed, and
    that number flows into every d2, and therefore into prob_target_at_entry and
    the zombie test."""
    rfr = provider(config, store, lambda r: httpx.Response(500, text="down"))
    with pytest.raises(UpstreamError) as excinfo:
        await rfr.get("USD")
    assert "no default" in str(excinfo.value)
    assert "4.5" not in str(excinfo.value)


async def test_a_missing_api_key_raises_rather_than_defaulting(config, store):
    rfr = provider(config, store, lambda r: fred_response("1.0"), api_key=None)
    with pytest.raises(UpstreamError, match="FRED_API_KEY is unset"):
        await rfr.get("USD")


async def test_no_default_rate_appears_anywhere_in_the_module():
    import ast
    import inspect

    from obliskiq.macro import rates

    tree = ast.parse(inspect.getsource(rates))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert 4.5 not in literals


async def test_the_rate_is_cached_in_the_store_and_survives_a_restart(config, store):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return fred_response("3.25")

    first = provider(config, store, handler)
    await first.get("USD")

    # A brand-new provider over the same store: no second network call.
    second = provider(config, store, handler)
    rate = await second.get("USD")
    assert calls["n"] == 1
    assert rate.rate == pytest.approx(0.0325)


async def test_last_known_mode_serves_a_stale_value_and_says_so(config, store):
    live = provider(config, store, lambda r: fred_response("3.25"))
    await live.get("USD")

    stale_config = config_with(config, on_failure="last_known", cache_ttl_seconds=0)
    broken = provider(stale_config, store, lambda r: httpx.Response(503))
    rate = await broken.get("USD")
    assert rate.stale is True
    assert rate.rate == pytest.approx(0.0325)


async def test_error_mode_does_not_serve_a_stale_value_silently(config, store):
    live = provider(config, store, lambda r: fred_response("3.25"))
    await live.get("USD")

    expired = config_with(config, cache_ttl_seconds=0)
    broken = provider(expired, store, lambda r: httpx.Response(503))
    with pytest.raises(UpstreamError):
        await broken.get("USD")


async def test_a_fred_placeholder_observation_is_skipped(config, store):
    """FRED writes '.' for a missing value."""
    rfr = provider(
        config,
        store,
        lambda r: httpx.Response(
            200, json={"observations": [{"date": "2026-08-21", "value": "."}]}
        ),
    )
    with pytest.raises(UpstreamError, match="no usable observation"):
        await rfr.get("USD")


async def test_repeated_failures_enter_a_cooldown(config, store):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500)

    rfr = provider(config, store, handler)
    for _ in range(3):
        with pytest.raises(UpstreamError):
            await rfr.get("USD")
    # The first attempt exhausts its retries; the rest short-circuit on cooldown.
    assert calls["n"] == config.require_int("http.retries") + 1


async def test_provenance_is_reported_with_the_rate(config, store):
    rfr = provider(config, store, lambda r: fred_response("3.25", "2026-08-21"))
    rate = await rfr.get("USD")
    assert rate.series_id == "DTB3"
    assert rate.reference_date == "2026-08-21"
    assert rate.fetched_at
