"""
GDELT client — geopolitical pulse for the Asymmetry Hunter.
==========================================================

Feeds component (h) "GDELT pulse": a per-region geopolitical anomaly score that
maps to affected assets (energy, defense, regional ETFs, gold) via a static
mapping (``config/gdelt_regions.json``).

Free **GDELT 2.0 DOC API** (``https://api.gdeltproject.org/api/v2/doc/doc``) —
no key. For each region/theme we pull the recent article-VOLUME and average-TONE
timelines and keep only the latest point. A **Welford** running mean/variance
accumulator (incremental, cheap — persisted in the warehouse) turns each latest
reading into a z-score; a region is anomalous when its volume z-score spikes
*and* tone is negative (geopolitical stress). Until the accumulator has seen
``gdelt_min_history`` observations the region reports ``warming_up`` and casts no
vote — honest degradation, never a fabricated z-score.

Design contract (mirrors the other market clients):
* All HTTP via ``modules.utils.http.fetch_with_retries``.
* Graceful skip: unreachable / unparseable regions are dropped, never raised.
* Daily snapshots + running stats persisted so the anomaly survives restarts.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from modules.utils.http import fetch_with_retries

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "gdelt_regions.json"
#: Observations required before a region's z-score is trusted (else warming_up).
MIN_HISTORY = 20
#: Stop asking after this many regions in a row came back empty (GDELT answering 429 to a shared CI
#: IP). Each failing query spends ~60-80 s in retry backoff: Paper Run #184 lost ~7 minutes to the
#: 5 regions x 2 queries all failing — hammering a throttled endpoint gets nothing and slows the run.
GIVE_UP_AFTER_DARK_REGIONS = 2


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


class GDELTClient:
    """Pulls per-region GDELT volume/tone and turns it into anomaly z-scores."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        config_path: Optional[Path] = None,
        warehouse: Any = None,
        min_history: int = MIN_HISTORY,
    ) -> None:
        self.base_url = base_url or DEFAULT_BASE_URL
        self._config_path = Path(config_path) if config_path else _CONFIG_PATH
        self._warehouse = warehouse
        self.min_history = max(2, int(min_history))
        cfg = self._load_config()
        self.regions: Dict[str, Dict[str, Any]] = cfg.get("regions", {})

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    def _load_config(self) -> Dict[str, Any]:
        try:
            with open(self._config_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning(f"gdelt_regions.json load skipped: {exc}")
            return {}

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _latest_timeline_value(payload: Any) -> Optional[float]:
        """Extract the most recent value from a GDELT timeline JSON payload."""
        if not isinstance(payload, dict):
            return None
        timeline = payload.get("timeline")
        if not isinstance(timeline, list) or not timeline:
            return None
        series = timeline[0]
        data = series.get("data") if isinstance(series, dict) else None
        if not isinstance(data, list) or not data:
            return None
        # Data is chronological; take the last finite value.
        for point in reversed(data):
            val = _as_float(point.get("value")) if isinstance(point, dict) else None
            if val is not None:
                return val
        return None

    async def _fetch_mode(self, session: Any, query: str, mode: str) -> Optional[float]:
        params = {
            "query": query,
            "mode": mode,
            "format": "json",
            "timespan": "7d",
        }
        payload = await fetch_with_retries(session, "GET", self.base_url, params=params, timeout=15)
        return self._latest_timeline_value(payload)

    # ------------------------------------------------------------------
    # Welford running stats (persisted in the warehouse)
    # ------------------------------------------------------------------
    def _get_warehouse(self) -> Any:
        if self._warehouse is None:
            from modules.data.warehouse import DataWarehouse

            self._warehouse = DataWarehouse()
        return self._warehouse

    @staticmethod
    def _welford_update(
        stats: Optional[Dict[str, float]], x: float
    ) -> Tuple[Dict[str, float], Optional[float]]:
        """
        Incremental Welford update. Returns (new_stats, z_score).

        ``z_score`` is None until count >= 2 (need a variance estimate).
        """
        count = float(stats["count"]) if stats else 0.0
        mean = float(stats["mean"]) if stats else 0.0
        m2 = float(stats["m2"]) if stats else 0.0
        # z-score is computed against the distribution BEFORE this observation,
        # so a genuine spike registers instead of being absorbed into the mean.
        z: Optional[float] = None
        if count >= 2:
            variance = m2 / (count - 1.0)
            std = math.sqrt(variance) if variance > 0 else 0.0
            if std > 0:
                z = (x - mean) / std
        count += 1.0
        delta = x - mean
        mean += delta / count
        m2 += delta * (x - mean)
        return {"count": count, "mean": mean, "m2": m2}, z

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def fetch_pulse(
        self, session: Any, snapshot_date: Optional[date] = None
    ) -> Dict[str, Any]:
        """
        Fetch volume/tone for every configured region, update running stats, and
        return ``{status, regions: {R: {volume, tone, volume_z, tone_z,
        anomalous, status}}, worst_region}``. Best-effort; never raises.
        """
        snap_date = snapshot_date or datetime.now(timezone.utc).date()
        try:
            wh = self._get_warehouse()
        except Exception as exc:  # pragma: no cover - defensive
            wh = None
            logger.warning(f"GDELT warehouse unavailable, stats not persisted: {exc}")

        regions_out: Dict[str, Any] = {}
        worst_region: Optional[str] = None
        worst_z = 0.0
        rows: List[Dict[str, Any]] = []

        dark_in_a_row = 0
        for region, spec in self.regions.items():
            query = spec.get("query")
            if not query:
                continue
            if dark_in_a_row >= GIVE_UP_AFTER_DARK_REGIONS:
                regions_out[region] = {"status": "unavailable"}  # not asked: the feed is throttling this run
                continue
            volume = await self._fetch_mode(session, query, "timelinevolraw")
            tone = await self._fetch_mode(session, query, "timelinetone")
            if volume is None and tone is None:
                regions_out[region] = {"status": "unavailable"}
                dark_in_a_row += 1
                continue
            dark_in_a_row = 0

            volume_z: Optional[float] = None
            tone_z: Optional[float] = None
            warming = False
            if wh is not None:
                if volume is not None:
                    vstats = self._safe_get_stats(wh, region, "volume")
                    new_v, volume_z = self._welford_update(vstats, volume)
                    self._safe_set_stats(wh, region, "volume", new_v)
                    if new_v["count"] < self.min_history:
                        warming = True
                if tone is not None:
                    tstats = self._safe_get_stats(wh, region, "tone")
                    new_t, tone_z = self._welford_update(tstats, tone)
                    self._safe_set_stats(wh, region, "tone", new_t)

            status = "warming_up" if warming else "success"
            anomalous = (
                not warming
                and volume_z is not None
                and tone is not None
                and tone < 0.0
            )
            regions_out[region] = {
                "volume": round(volume, 2) if volume is not None else None,
                "tone": round(tone, 2) if tone is not None else None,
                "volume_z": round(volume_z, 2) if volume_z is not None else None,
                "tone_z": round(tone_z, 2) if tone_z is not None else None,
                "anomalous": bool(anomalous),
                "status": status,
            }
            if anomalous and volume_z is not None and volume_z > worst_z:
                worst_z = volume_z
                worst_region = region
            rows.append(
                {
                    "date": snap_date,
                    "region": region,
                    "volume": volume,
                    "tone": tone,
                    "volume_z": volume_z,
                    "tone_z": tone_z,
                }
            )

        if wh is not None and rows:
            try:
                wh.store_gdelt_pulse(rows)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"GDELT pulse store failed: {exc}")

        if not regions_out:
            return {"status": "unavailable", "regions": {}, "worst_region": None}
        any_live = any(
            r.get("status") == "success" for r in regions_out.values() if isinstance(r, dict)
        )
        return {
            "status": "success" if any_live else "warming_up",
            "regions": regions_out,
            "worst_region": worst_region,
        }

    # ------------------------------------------------------------------
    # Warehouse stat helpers (isolated so failures degrade, not raise)
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_get_stats(wh: Any, region: str, metric: str) -> Optional[Dict[str, float]]:
        try:
            return wh.get_gdelt_running_stats(region, metric)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"GDELT stats read failed ({region}/{metric}): {exc}")
            return None

    @staticmethod
    def _safe_set_stats(wh: Any, region: str, metric: str, stats: Dict[str, float]) -> None:
        try:
            wh.upsert_gdelt_running_stats(
                region, metric, stats["count"], stats["mean"], stats["m2"]
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"GDELT stats write failed ({region}/{metric}): {exc}")
