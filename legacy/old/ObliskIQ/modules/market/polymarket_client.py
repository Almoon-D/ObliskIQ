"""
Polymarket client — prediction-market probabilities for the Asymmetry Hunter.
============================================================================

Feeds component (g) "Polymarket divergence": the gap between the crowd's
market-implied probability and our own data-implied stance (e.g. Polymarket
prices 80% "no Fed cut" while our rate/regime signals scream pivot).

Free public **Gamma API** (``https://gamma-api.polymarket.com``) — no key. We
track only a small, hand-editable curated list (``config/polymarket_markets.json``)
of macro-relevant markets. Each run we:

1. Fetch the current "Yes" probability for each curated market.
2. Persist a daily snapshot to the warehouse so probability VELOCITY (today vs
   the prior snapshot) is available to the Hunter and reports.
3. Hand the raw probabilities + velocity back; the Phase 1 layer computes the
   divergence-vs-our-data and applies the circuit breaker.

Design contract (mirrors the other market clients):
* All HTTP via ``modules.utils.http.fetch_with_retries`` (retries + size guard).
* Graceful skip: any unreachable / unparseable market is dropped, never raised.
* The component honestly degrades to 0 weight upstream when no data is returned.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules.utils.http import fetch_with_retries

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "polymarket_markets.json"


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class PolymarketClient:
    """Fetches curated Polymarket probabilities and snapshots them to the warehouse."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        config_path: Optional[Path] = None,
        warehouse: Any = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._config_path = Path(config_path) if config_path else _CONFIG_PATH
        self._warehouse = warehouse  # lazily constructed in persist() if needed
        self.markets = self._load_curated_markets()

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    def _load_curated_markets(self) -> List[Dict[str, Any]]:
        """Load the hand-editable curated market list ({} -> empty, never raises)."""
        try:
            with open(self._config_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            markets = payload.get("markets", [])
            return [m for m in markets if isinstance(m, dict) and m.get("slug")]
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning(f"polymarket_markets.json load skipped: {exc}")
            return []

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_yes_probability(market_payload: Dict[str, Any]) -> Optional[float]:
        """
        Extract the 'Yes' implied probability (0-1) from a Gamma market object.

        Gamma returns ``outcomes`` and ``outcomePrices`` as JSON-encoded string
        lists, e.g. ``'["Yes","No"]'`` and ``'["0.82","0.18"]'``.
        """
        outcomes = market_payload.get("outcomes")
        prices = market_payload.get("outcomePrices")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = None
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = None
        if isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == len(prices):
            for label, price in zip(outcomes, prices):
                if str(label).strip().lower() == "yes":
                    return _as_float(price)
            # Binary market without an explicit "Yes": take the first leg.
            if prices:
                return _as_float(prices[0])
        # Some payloads expose a single ``lastTradePrice``.
        return _as_float(market_payload.get("lastTradePrice"))

    async def fetch_snapshot(self, session: Any) -> List[Dict[str, Any]]:
        """
        Fetch the current "Yes" probability for each curated market.

        Returns a list of ``{id, slug, question, yes_means, data_stance,
        prob_yes}`` dicts. Unreachable / unparseable markets are dropped.
        """
        out: List[Dict[str, Any]] = []
        for m in self.markets:
            slug = m.get("slug")
            url = f"{self.base_url}/markets"
            payload = await fetch_with_retries(
                session, "GET", url, params={"slug": slug}, timeout=10
            )
            # Gamma returns a list for ?slug= queries.
            market_obj: Optional[Dict[str, Any]] = None
            if isinstance(payload, list) and payload:
                market_obj = payload[0]
            elif isinstance(payload, dict):
                market_obj = payload
            if not isinstance(market_obj, dict):
                logger.debug(f"Polymarket: no payload for {slug}, skipping")
                continue
            prob = self._parse_yes_probability(market_obj)
            if prob is None:
                logger.debug(f"Polymarket: unparseable price for {slug}, skipping")
                continue
            out.append(
                {
                    "id": m.get("id", slug),
                    "slug": slug,
                    "question": m.get("question", slug),
                    "yes_means": m.get("yes_means"),
                    "data_stance": m.get("data_stance"),
                    "prob_yes": round(float(prob), 4),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Persistence + velocity
    # ------------------------------------------------------------------
    def _get_warehouse(self) -> Any:
        if self._warehouse is None:
            from modules.data.warehouse import DataWarehouse

            self._warehouse = DataWarehouse()
        return self._warehouse

    def persist(self, markets: List[Dict[str, Any]], snapshot_date: Optional[date] = None) -> None:
        """
        Snapshot today's probabilities and stamp each with its velocity
        (today's prob minus the prior stored snapshot's prob). Best-effort:
        warehouse failures degrade to no velocity, never raise.
        """
        if not markets:
            return
        snap_date = snapshot_date or datetime.now(timezone.utc).date()
        try:
            wh = self._get_warehouse()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Polymarket warehouse unavailable, velocity skipped: {exc}")
            return
        rows: List[Dict[str, Any]] = []
        for m in markets:
            velocity: Optional[float] = None
            try:
                velocity = wh.get_polymarket_velocity(m["id"], current_prob=m["prob_yes"])
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"Polymarket velocity read failed for {m['id']}: {exc}")
            m["velocity"] = round(velocity, 4) if velocity is not None else None
            rows.append(
                {
                    "date": snap_date,
                    "market_id": m["id"],
                    "question": m.get("question"),
                    "prob_yes": m["prob_yes"],
                    "velocity": m["velocity"],
                }
            )
        try:
            wh.store_polymarket_snapshot(rows)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Polymarket snapshot store failed: {exc}")
