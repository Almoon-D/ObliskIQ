"""Risk-free rate lookup.

Ported from IBKR-Portmanteau's ``MarketFacade.get_risk_free_rate``, with two
changes that matter.

**It returned 4.5 when FRED failed.** A hardcoded 4.5% is a magic number, and one
that flows straight into every d2 and therefore into ``prob_target_at_entry`` and
the zombie test. A rate that could not be fetched now raises, or returns the last
value actually observed if ``macro.risk_free_rate.on_failure`` says so — and in
that case the result says how old it is. Silent substitution is what the ledger
exists to prevent.

**It was hardcoded to DTB3, the US 3-month bill.** The account's base currency is
EUR and its cash yields €STR, so discounting an EUR-denominated option at a USD
rate is simply the wrong rate. The series is now a per-currency lookup under
``macro.risk_free_rate.series_by_currency``.

The rate is cached in the ``macro`` table, so a restart does not re-hit FRED and
the value survives the process. The original cached it in memory only.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import Config
from ..errors import ERROR_EXCERPT_CHARS, ConfigError, UpstreamError
from ..logging_setup import get_logger
from ..store import Store
from ..util.http import RetryPolicy, get_with_retry
from ..util.numeric import safe_float
from .series import (
    PERCENT_TO_DECIMAL,
    SOURCE,
    RiskFreeRate,
    cached_risk_free_rate,
    risk_free_rate_as_of,
    series_for_currency,
)

log = get_logger("macro.rates")

#: The store-reading half of this module now lives in :mod:`obliskiq.macro.series`,
#: which names no HTTP client, so the compute layer can read a stored rate without
#: dragging httpx into a graph that runs with ``PrivateNetwork=true``. They are
#: re-exported here because this is where every existing caller imports them from,
#: and because moving a name is not a reason to break one. Listed in ``__all__``
#: so the re-export is deliberate rather than an import the linter should remove.
__all__ = [
    "PERCENT_TO_DECIMAL",
    "SOURCE",
    "RiskFreeRate",
    "RiskFreeRateProvider",
    "cached_risk_free_rate",
    "risk_free_rate_as_of",
    "series_for_currency",
]

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

#: The one status code this module treats as a body worth parsing. Anything else
#: raises: a rate has no plausible substitute, so a 404 must not become a number.
HTTP_OK = 200

#: How much of an error body to quote back in an exception message.


class RiskFreeRateProvider:
    """Fetches, caches and serves the risk-free rate per currency."""

    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        client: httpx.AsyncClient,
        fred_api_key: str | None,
    ) -> None:
        self._config = config
        self._store = store
        self._client = client
        self._api_key = fred_api_key
        self._policy = RetryPolicy.from_config(config)

        cfg = config.section("macro.risk_free_rate")
        self._series_by_currency: dict[str, str] = {
            str(k).upper(): str(v) for k, v in cfg.get("series_by_currency").items()
        }
        self._units = cfg.require_str("units")
        self._cache_ttl = cfg.require_float("cache_ttl_seconds")
        self._failure_cooldown = cfg.require_float("failure_cooldown_seconds")
        self._on_failure = cfg.require_choice("on_failure", ["error", "last_known"])
        self._cooldown_until = 0.0

    def series_for(self, currency: str) -> str:
        series = self._series_by_currency.get(currency.upper())
        if series is None:
            raise ConfigError(
                f"no risk-free-rate series configured for {currency!r}. Add it under "
                "macro.risk_free_rate.series_by_currency — the rate is never guessed."
            )
        return series

    async def get(self, currency: str | None = None) -> RiskFreeRate:
        """Return the risk-free rate for ``currency``, defaulting to the base currency."""
        ccy = (currency or self._config.require_str("account.base_currency")).upper()
        series_id = self.series_for(ccy)

        cached = self._read_cached(series_id)
        if cached is not None and not self._is_expired(cached["collected_at"]):
            return self._to_rate(ccy, series_id, cached, stale=False)

        if time.time() < self._cooldown_until:
            return self._fall_back(ccy, series_id, cached, "upstream is in cooldown")

        try:
            observation = await self._fetch(series_id)
        except UpstreamError as exc:
            self._cooldown_until = time.time() + self._failure_cooldown
            return self._fall_back(ccy, series_id, cached, str(exc))

        self._write_cached(series_id, observation)
        return self._to_rate(ccy, series_id, observation, stale=False)

    # ── internals ──

    async def _fetch(self, series_id: str) -> dict[str, Any]:
        if not self._api_key:
            raise UpstreamError(
                f"FRED_API_KEY is unset, so {series_id} cannot be fetched. The risk-free "
                "rate has no default value."
            )
        response = await get_with_retry(
            self._client,
            FRED_OBSERVATIONS_URL,
            policy=self._policy,
            params={
                "series_id": series_id,
                "api_key": self._api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
        )
        if response.status_code != HTTP_OK:
            raise UpstreamError(
                f"FRED returned HTTP {response.status_code} for {series_id}: "
                f"{response.text[:ERROR_EXCERPT_CHARS]}"
            )
        try:
            observations = response.json().get("observations", [])
        except ValueError as exc:
            raise UpstreamError(f"FRED returned a non-JSON body for {series_id}") from exc

        # FRED writes "." for a missing observation; safe_float maps it to None.
        for row in observations:
            value = safe_float(row.get("value"), None)
            if value is not None:
                return {
                    "value": value,
                    "reference_date": str(row.get("date")),
                    "collected_at": _utc_now(),
                }
        raise UpstreamError(f"FRED returned no usable observation for {series_id}")

    def _read_cached(self, series_id: str) -> dict[str, Any] | None:
        row = self._store.execute(
            "SELECT value, reference_date, collected_at FROM macro "
            "WHERE series_id = ? AND source = ? ORDER BY reference_date DESC LIMIT 1",
            (series_id, SOURCE),
        ).fetchone()
        if row is None:
            return None
        return {
            "value": row["value"],
            "reference_date": row["reference_date"],
            "collected_at": row["collected_at"],
        }

    def _write_cached(self, series_id: str, observation: dict[str, Any]) -> None:
        with self._store.transaction() as connection:
            connection.execute(
                "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
                "frequency, units, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (series_id, source, reference_date) DO UPDATE SET "
                "value = excluded.value, collected_at = excluded.collected_at",
                (
                    series_id,
                    SOURCE,
                    observation["reference_date"],
                    # FRED's observations endpoint does not carry a release date; the
                    # full macro_pull uses the releases endpoint for that. A daily rate
                    # series releases on its reference date, so they coincide here.
                    observation["reference_date"],
                    observation["value"],
                    # NULL, not a guess. This cache writes whatever series
                    # macro.risk_free_rate.series_by_currency names, and not all
                    # of them are daily — CAD resolves to a MONTHLY id, because
                    # FRED serves no daily Canadian short rate. Stamping "daily"
                    # here put a false cadence in a column other readers trust:
                    # the cross-asset correlation matrix refuses a daily-against-
                    # monthly pair, and it can only do that if the column is
                    # honest. NULL is the column's own word for "this row does
                    # not declare one", and the full macro_pull writes the
                    # declared frequency for the same series.
                    None,
                    self._units,
                    observation["collected_at"],
                ),
            )

    def _is_expired(self, collected_at: str) -> bool:
        try:
            stamped = datetime.strptime(collected_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return True
        age = (datetime.now(UTC) - stamped).total_seconds()
        return age > self._cache_ttl

    def _to_rate(
        self, currency: str, series_id: str, observation: dict[str, Any], *, stale: bool
    ) -> RiskFreeRate:
        value = float(observation["value"])
        rate = value / PERCENT_TO_DECIMAL if self._units == "percent" else value
        return RiskFreeRate(
            currency=currency,
            series_id=series_id,
            rate=rate,
            reference_date=str(observation["reference_date"]),
            fetched_at=str(observation["collected_at"]),
            stale=stale,
        )

    def _fall_back(
        self,
        currency: str,
        series_id: str,
        cached: dict[str, Any] | None,
        detail: str,
    ) -> RiskFreeRate:
        if self._on_failure == "last_known" and cached is not None:
            log.warning(
                "risk-free rate is stale; serving the last observed value",
                extra={"series_id": series_id, "currency": currency, "detail": detail},
            )
            return self._to_rate(currency, series_id, cached, stale=True)
        raise UpstreamError(
            f"could not obtain the {currency} risk-free rate ({series_id}): {detail}. "
            "There is no default: a substituted rate would flow into every d2, and "
            "therefore into prob_target_at_entry and the zombie test."
        )


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
