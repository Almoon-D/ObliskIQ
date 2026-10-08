"""
CFTC Commitment of Traders (COT) Analyzer
===========================================
Fetches and analyzes CFTC COT reports to detect positioning extremes.

Soros Philosophy:
- Positioning extremes = crowded trades = reflexivity opportunity
- 90th/10th percentile positioning often precedes reversals
- "When everyone is positioned one way, who's left to buy/sell?"

Data Source (REAL — v6.10.0, no longer a mock framework):
- CFTC publishes free weekly COT reports (Tuesday data, released Fridays 3:30pm ET)
- Legacy futures-only ANNUAL archives (downloadable, multi-year):
  https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm
  Per-year ZIP:  https://www.cftc.gov/files/dea/history/deacot{YEAR}.zip  (1986–present)
  Each ZIP holds a single ``annual.txt`` CSV (Non-Commercial = large spec,
  Commercial = hedgers, Nonreportable = small spec).
- Reports show net positioning for: Commercials, Large Specs, Small Specs

History store (mirrors the IV-snapshot pattern, v6.1.0):
- Weekly rows are UPSERTed into the warehouse ``cot_history`` table and the
  large-spec crowding percentile is a REAL trailing-3y rank
  (``DataWarehouse.cot_percentile_with_status``), with an honest 52-week warm-up
  fallback. Backfill via ``scripts/backfill_cot_history.py``; see docs/COT_REAL.md.

Key Metrics:
- Net positioning (long - short) as % of open interest
- Percentile rank vs 3-year history (real, from cot_history)
- Positioning concentration (how crowded)

Historical IC:
- COT extremes have IC ≈ 0.06-0.10 for major futures (validated 2000-2020)
- Works best for: commodities, currencies, bonds
- Less reliable for: equity indices (everyone can be bullish together)
"""

import aiohttp
import asyncio
import csv
import pandas as pd
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import io
import logging
import zipfile

from ObliskIQ.modules.exceptions import APIError

logger = logging.getLogger(__name__)

# Legacy futures-only annual.txt column names (confirmed against the CFTC 2024
# archive; the public-reporting header is stable). Whitespace is stripped before
# matching, so a stray leading space in the file header is tolerated.
_COL_NAME = "Market and Exchange Names"
_COL_DATE = "As of Date in Form YYYY-MM-DD"
_COL_CODE = "CFTC Contract Market Code"
_COL_OI = "Open Interest (All)"
_COL_NC_LONG = "Noncommercial Positions-Long (All)"
_COL_NC_SHORT = "Noncommercial Positions-Short (All)"
_COL_C_LONG = "Commercial Positions-Long (All)"
_COL_C_SHORT = "Commercial Positions-Short (All)"
_COL_NR_LONG = "Nonreportable Positions-Long (All)"
_COL_NR_SHORT = "Nonreportable Positions-Short (All)"


def _to_int(value: Any) -> Optional[int]:
    """Parse a CFTC integer cell ('1,234' / '.' / '' / None) → int or None."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s in ("", ".", "n/a", "NA"):
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _parse_report_date(value: Any) -> Optional[date]:
    """Parse the 'As of Date in Form YYYY-MM-DD' cell → date or None."""
    if value is None:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_cot_annual_zip(
    raw_bytes: bytes, tracked: Optional[Dict[str, Dict[str, str]]] = None
) -> pd.DataFrame:
    """
    Parse a CFTC legacy futures-only annual ZIP (``deacot{YEAR}.zip`` → a single
    ``annual.txt`` CSV) into the normalized COT schema, filtered to the tracked
    contracts. SHARED by the live fetch and ``scripts/backfill_cot_history.py``
    (DRY). Returns ALL weekly rows for the tracked contracts (the backfill stores
    them all; the live fetch keeps the latest week per contract).

    Output columns match ``analyze_positioning``'s expected input:
    ``contract_code, contract_name, report_date (date), open_interest,
    large_spec_long/short, commercial_long/short, small_spec_long/short``.
    """
    tracked = tracked or CFTCCOTAnalyzer.TRACKED_CONTRACTS
    code_to_name = {meta["cftc_code"]: meta["name"] for meta in tracked.values()}

    zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    member = next((n for n in zf.namelist() if n.lower().endswith(".txt")), None)
    if member is None:
        raise APIError("CFTC annual ZIP contains no .txt member")
    text = zf.read(member).decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        reader.fieldnames = [f.strip() for f in reader.fieldnames]

    out: List[Dict[str, Any]] = []
    for row in reader:
        code = (row.get(_COL_CODE) or "").strip()
        name = code_to_name.get(code)
        if name is None:
            continue  # not a tracked contract
        oi = _to_int(row.get(_COL_OI))
        rdate = _parse_report_date(row.get(_COL_DATE))
        if rdate is None or not oi:
            continue
        out.append(
            {
                "contract_code": code,
                "contract_name": name,
                "report_date": rdate,
                "open_interest": oi,
                "large_spec_long": _to_int(row.get(_COL_NC_LONG)),
                "large_spec_short": _to_int(row.get(_COL_NC_SHORT)),
                "commercial_long": _to_int(row.get(_COL_C_LONG)),
                "commercial_short": _to_int(row.get(_COL_C_SHORT)),
                "small_spec_long": _to_int(row.get(_COL_NR_LONG)),
                "small_spec_short": _to_int(row.get(_COL_NR_SHORT)),
            }
        )
    return pd.DataFrame(out)


class PositioningExtreme(Enum):
    """Positioning extreme classification"""
    EXTREME_LONG = "extreme_long"  # >90th percentile
    LONG = "long"  # 70-90th percentile
    NEUTRAL = "neutral"  # 30-70th percentile
    SHORT = "short"  # 10-30th percentile
    EXTREME_SHORT = "extreme_short"  # <10th percentile


@dataclass
class COTPositioning:
    """COT positioning data for a single contract"""
    contract_name: str
    cftc_code: str
    report_date: datetime

    # Net positioning (long - short)
    commercial_net: int  # Hedgers (usually contrarian)
    large_spec_net: int  # Hedge funds, CTAs (trend followers)
    small_spec_net: int  # Retail (usually wrong at extremes)

    # Open interest
    open_interest: int

    # Percentile ranks (vs 3-year history)
    commercial_percentile: Optional[float]
    large_spec_percentile: Optional[float]
    small_spec_percentile: Optional[float]

    # Extreme classification
    positioning_extreme: PositioningExtreme
    crowding_score: float  # 0-1 (how concentrated is positioning)

    # Signal
    signal: str  # "contrarian_long", "contrarian_short", "neutral"
    confidence: float
    interpretation: str

    # Warm-up / provenance status for the percentile (v6.10.0). Honest string,
    # e.g. "COT percentile from 156 weekly observations" or
    # "COT percentile warming up: 14/52 weeks". None percentile => no contrarian vote.
    percentile_status: Optional[str] = None


@dataclass
class COTAnalysis:
    """Complete COT analysis across contracts"""
    timestamp: datetime
    report_date: datetime

    # Individual contracts
    contracts: List[COTPositioning] = field(default_factory=list)

    # Aggregated signals
    extreme_long_positions: List[str] = field(default_factory=list)
    extreme_short_positions: List[str] = field(default_factory=list)

    # Cross-market insights
    risk_appetite_score: float = 0.0  # Aggregate risk sentiment
    crowded_trades: List[Tuple[str, str]] = field(default_factory=list)  # (contract, direction)

    def print_summary(self):
        """Print COT analysis summary"""
        print(f"\n{'='*70}")
        print("CFTC COMMITMENT OF TRADERS ANALYSIS")
        print(f"{'='*70}")
        print(f"Report Date: {self.report_date.strftime('%Y-%m-%d')}")
        print(f"Analysis Time: {self.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}")

        print(f"\n📊 POSITIONING EXTREMES")
        print(f"  Extreme Long: {len(self.extreme_long_positions)} contracts")
        for contract in self.extreme_long_positions[:5]:
            print(f"    - {contract}")

        print(f"  Extreme Short: {len(self.extreme_short_positions)} contracts")
        for contract in self.extreme_short_positions[:5]:
            print(f"    - {contract}")

        if self.crowded_trades:
            print(f"\n⚠️  CROWDED TRADES (Contrarian Opportunity)")
            for contract, direction in self.crowded_trades[:10]:
                emoji = "🔴" if direction == "long" else "🟢"
                print(f"  {emoji} {contract}: Extreme {direction.upper()} crowding")

        print(f"\n🎲 RISK APPETITE")
        if self.risk_appetite_score > 0.6:
            print(f"  Score: {self.risk_appetite_score:.2f} - HIGH RISK APPETITE")
            print(f"  Interpretation: Speculators aggressively long risk assets")
        elif self.risk_appetite_score < 0.4:
            print(f"  Score: {self.risk_appetite_score:.2f} - LOW RISK APPETITE")
            print(f"  Interpretation: Speculators defensive, risk-off positioning")
        else:
            print(f"  Score: {self.risk_appetite_score:.2f} - NEUTRAL")

        print(f"{'='*70}\n")


class CFTCCOTAnalyzer:
    """
    Analyzes CFTC Commitment of Traders reports for positioning extremes.

    NOTE: This is a framework implementation. In production, you would:
    1. Fetch actual CFTC COT data files (TXT format from CFTC website)
    2. Parse the fixed-width format
    3. Store in database for historical tracking

    For now, this provides the analysis framework and demonstrates the logic.
    """

    # Major contracts to track
    TRACKED_CONTRACTS = {
        # Equity Indices
        "SP500": {"name": "S&P 500", "cftc_code": "13874+", "asset_class": "equity"},
        "NASDAQ": {"name": "Nasdaq 100", "cftc_code": "20974+", "asset_class": "equity"},

        # Bonds (codes verified against the CFTC 2024 legacy futures-only archive,
        # v6.10.0: US30Y was previously '043607' = "ULTRA UST 10Y" — the WRONG
        # contract. The classic 30-year CBOT "UST BOND" futures is '020601'.)
        "US10Y": {"name": "10-Year Treasury", "cftc_code": "043602", "asset_class": "bonds"},
        "US30Y": {"name": "30-Year Treasury", "cftc_code": "020601", "asset_class": "bonds"},

        # Commodities - Energy
        "CL": {"name": "Crude Oil", "cftc_code": "067651", "asset_class": "commodity"},
        "NG": {"name": "Natural Gas", "cftc_code": "023651", "asset_class": "commodity"},

        # Commodities - Metals
        "GC": {"name": "Gold", "cftc_code": "088691", "asset_class": "commodity"},
        "SI": {"name": "Silver", "cftc_code": "084691", "asset_class": "commodity"},
        "HG": {"name": "Copper", "cftc_code": "085692", "asset_class": "commodity"},

        # Currencies
        "DX": {"name": "US Dollar Index", "cftc_code": "098662", "asset_class": "currency"},
        "EUR": {"name": "Euro", "cftc_code": "099741", "asset_class": "currency"},
    }

    # CFTC data URL (legacy report)
    CFTC_BASE_URL = "https://www.cftc.gov/dea/futures/deacmxsf.htm"

    # Per-year legacy futures-only annual archive (downloadable, 1986–present).
    COT_HISTORY_URL_TEMPLATE = "https://www.cftc.gov/files/dea/history/deacot{year}.zip"

    def __init__(self, lookback_weeks: int = 156):  # 3 years
        """
        Initialize COT analyzer.

        Args:
            lookback_weeks: Weeks of history for percentile calculation (default: 3 years)
        """
        self.lookback_weeks = lookback_weeks
        self.historical_data: Dict[str, pd.DataFrame] = {}

    async def fetch_latest_cot_report(
        self, session: aiohttp.ClientSession
    ) -> Optional[pd.DataFrame]:
        """
        Fetch the latest REAL CFTC COT report (v6.10.0).

        Downloads the current-year legacy futures-only annual archive
        (``deacot{YEAR}.zip`` → ``annual.txt``), parses it with the shared
        ``parse_cot_annual_zip``, and returns the **latest weekly row per tracked
        contract** in the normalized schema ``analyze_positioning`` expects. Early
        in January the current-year file can be sparse, so the prior year is used
        as a fallback.

        Network/parse failure degrades to ``None`` (Phase 1 then reports
        ``unavailable`` and the Hunter's crowding component self-zeros) — it never
        raises into the pipeline. Set ``ENABLE_MOCK_COT_DATA=true`` for an
        explicit offline-dev fallback (still degrades to 0 weight by design).

        Returns:
            DataFrame with the latest week per contract, or None.
        """
        import os

        if os.getenv("ENABLE_MOCK_COT_DATA", "false").lower() == "true":
            logger.warning("⚠️  DEV MODE: mock COT data (ENABLE_MOCK_COT_DATA=true)")
            mock_data = {
                "contract_code": ["067651", "088691", "13874+"],
                "contract_name": ["Crude Oil", "Gold", "S&P 500"],
                "report_date": [datetime.now(timezone.utc).date() - timedelta(days=3)] * 3,
                "commercial_long": [100000, 50000, 200000],
                "commercial_short": [150000, 30000, 180000],
                "large_spec_long": [80000, 60000, 150000],
                "large_spec_short": [40000, 20000, 100000],
                "small_spec_long": [20000, 15000, 40000],
                "small_spec_short": [15000, 10000, 35000],
                "open_interest": [200000, 100000, 400000],
            }
            return pd.DataFrame(mock_data)

        try:
            year = datetime.now(timezone.utc).year
            df = await self._download_and_parse_year(session, year)
            if df is None or df.empty:
                # Current-year file may be empty/sparse early in January.
                df = await self._download_and_parse_year(session, year - 1)
            if df is None or df.empty:
                logger.warning("CFTC COT: no rows parsed for tracked contracts")
                return None
            # Keep the latest report_date per contract (the live "current week").
            df = (
                df.sort_values("report_date")
                .groupby("contract_code", as_index=False)
                .tail(1)
                .reset_index(drop=True)
            )
            return df
        except Exception as exc:  # never abort Phase 1 on a data-feed hiccup
            logger.warning(f"CFTC COT fetch failed, degrading: {exc}")
            return None

    async def _download_and_parse_year(
        self, session: aiohttp.ClientSession, year: int
    ) -> Optional[pd.DataFrame]:
        """Download + parse one year's legacy annual ZIP. None on any failure."""
        url = self.COT_HISTORY_URL_TEMPLATE.format(year=year)
        raw = await self._download_bytes(session, url)
        if not raw:
            return None
        try:
            return parse_cot_annual_zip(raw, self.TRACKED_CONTRACTS)
        except Exception as exc:
            logger.warning(f"CFTC COT parse failed for {year}: {exc}")
            return None

    async def _download_bytes(
        self, session: aiohttp.ClientSession, url: str
    ) -> Optional[bytes]:
        """GET ``url`` and return the full body, or None on non-200 / error."""
        headers = {"User-Agent": "Mozilla/5.0 (ObliskIQ COT)"}
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"CFTC COT {url} → HTTP {resp.status}")
                    return None
                return await resp.read()
        except Exception as exc:
            logger.warning(f"CFTC COT download error {url}: {exc}")
            return None

    def analyze_positioning(
        self,
        cot_data: pd.DataFrame,
        warehouse: Optional[Any] = None,
        as_of: Optional[date] = None,
    ) -> COTAnalysis:
        """
        Analyze COT positioning for extremes (v6.10.0 — REAL historical percentile).

        The large-spec crowding percentile is the trailing-3y rank of the current
        net-spec-%-of-OI within ``cot_history`` (``warehouse.cot_percentile_with_status``),
        honest 52-week warm-up included. When ``warehouse`` is None, or a contract
        is still warming up, that contract's percentile is ``None`` → classified
        NEUTRAL → casts NO contrarian vote (the Hunter's crowding component then
        self-zeros for that asset).

        Args:
            cot_data: Latest COT report rows (normalized schema).
            warehouse: DataWarehouse for the historical percentile (None = degrade).
            as_of: Point-in-time reference date (None = today). Threaded so the
                replay can rank only against history dated <= as_of.

        Returns:
            COTAnalysis with extremes and contrarian signals.
        """
        timestamp = datetime.now(timezone.utc)
        report_date = cot_data['report_date'].iloc[0] if 'report_date' in cot_data.columns else timestamp

        contracts: List[COTPositioning] = []
        extreme_long = []
        extreme_short = []
        crowded_trades = []

        for _, row in cot_data.iterrows():
            # Calculate net positioning
            commercial_net = row['commercial_long'] - row['commercial_short']
            large_spec_net = row['large_spec_long'] - row['large_spec_short']
            small_spec_net = row['small_spec_long'] - row['small_spec_short']

            open_interest = row['open_interest']
            cftc_code = str(row['contract_code'])
            contract_name = row['contract_name']

            # REAL percentile: rank current net-spec-%-of-OI within trailing-3y
            # history. None (no warehouse / warming up) => no contrarian vote.
            net_spec_pct_oi = (large_spec_net / open_interest * 100) if open_interest else 0.0
            if warehouse is not None:
                large_spec_percentile, _n, percentile_status = warehouse.cot_percentile_with_status(
                    cftc_code, net_spec_pct_oi, as_of=as_of
                )
            else:
                large_spec_percentile, percentile_status = None, "COT history unavailable (no warehouse) — 0 weight"

            # Calculate crowding score (concentration magnitude, independent of pctl)
            concentration = abs(large_spec_net) / open_interest if open_interest > 0 else 0
            crowding_score = min(1.0, concentration * 2)  # 50%+ concentration = max crowding

            if large_spec_percentile is None:
                # Warming up / no history → honest neutral, no contrarian vote.
                positioning_extreme = PositioningExtreme.NEUTRAL
                signal, confidence = "neutral", 0.0
                interpretation = f"{contract_name}: {percentile_status}. No contrarian vote."
            else:
                positioning_extreme = self._classify_positioning_extreme(large_spec_percentile)
                signal, confidence, interpretation = self._generate_cot_signal(
                    contract_name=contract_name,
                    large_spec_net=large_spec_net,
                    large_spec_percentile=large_spec_percentile,
                    positioning_extreme=positioning_extreme,
                    crowding_score=crowding_score,
                )

            positioning = COTPositioning(
                contract_name=contract_name,
                cftc_code=cftc_code,
                report_date=report_date,
                commercial_net=commercial_net,
                large_spec_net=large_spec_net,
                small_spec_net=small_spec_net,
                open_interest=open_interest,
                commercial_percentile=None,  # Would calculate from history
                large_spec_percentile=large_spec_percentile,
                small_spec_percentile=None,
                positioning_extreme=positioning_extreme,
                crowding_score=crowding_score,
                signal=signal,
                confidence=confidence,
                interpretation=interpretation,
                percentile_status=percentile_status,
            )

            contracts.append(positioning)

            # Track extremes
            if positioning_extreme == PositioningExtreme.EXTREME_LONG:
                extreme_long.append(contract_name)
                crowded_trades.append((contract_name, "long"))
            elif positioning_extreme == PositioningExtreme.EXTREME_SHORT:
                extreme_short.append(contract_name)
                crowded_trades.append((contract_name, "short"))

        # Calculate aggregate risk appetite
        risk_appetite = self._calculate_risk_appetite(contracts)

        return COTAnalysis(
            timestamp=timestamp,
            report_date=report_date,
            contracts=contracts,
            extreme_long_positions=extreme_long,
            extreme_short_positions=extreme_short,
            crowded_trades=crowded_trades,
            risk_appetite_score=risk_appetite,
        )

    def _classify_positioning_extreme(self, percentile: float) -> PositioningExtreme:
        """Classify positioning based on percentile rank"""
        if percentile >= 90:
            return PositioningExtreme.EXTREME_LONG
        elif percentile >= 70:
            return PositioningExtreme.LONG
        elif percentile <= 10:
            return PositioningExtreme.EXTREME_SHORT
        elif percentile <= 30:
            return PositioningExtreme.SHORT
        else:
            return PositioningExtreme.NEUTRAL

    def _generate_cot_signal(
        self,
        contract_name: str,
        large_spec_net: int,
        large_spec_percentile: float,
        positioning_extreme: PositioningExtreme,
        crowding_score: float,
    ) -> Tuple[str, float, str]:
        """
        Generate contrarian trading signal from COT positioning.

        Soros Logic:
        - Extreme long positioning (>90th percentile) = too crowded = sell signal
        - Extreme short positioning (<10th percentile) = too bearish = buy signal

        Returns:
            (signal, confidence, interpretation)
        """
        if positioning_extreme == PositioningExtreme.EXTREME_LONG:
            signal = "contrarian_short"
            confidence = min(0.9, 0.6 + crowding_score * 0.3)
            interpretation = (
                f"⚠️ EXTREME LONG positioning in {contract_name} ({large_spec_percentile:.0f}th percentile). "
                f"Large speculators heavily long. Crowding score: {crowding_score:.2f}. "
                f"CONTRARIAN SIGNAL: Consider SHORT exposure. Risk of crowded trade unwind."
            )

        elif positioning_extreme == PositioningExtreme.EXTREME_SHORT:
            signal = "contrarian_long"
            confidence = min(0.9, 0.6 + crowding_score * 0.3)
            interpretation = (
                f"⚠️ EXTREME SHORT positioning in {contract_name} ({large_spec_percentile:.0f}th percentile). "
                f"Large speculators heavily short. Crowding score: {crowding_score:.2f}. "
                f"CONTRARIAN SIGNAL: Consider LONG exposure. Risk of short squeeze."
            )

        elif positioning_extreme in [PositioningExtreme.LONG, PositioningExtreme.SHORT]:
            signal = "weak_contrarian"
            confidence = 0.4 + crowding_score * 0.2
            direction = "long" if positioning_extreme == PositioningExtreme.LONG else "short"
            interpretation = (
                f"Elevated {direction} positioning in {contract_name} ({large_spec_percentile:.0f}th percentile). "
                f"Watch for potential reversal if positioning becomes more extreme."
            )

        else:
            signal = "neutral"
            confidence = 0.2
            interpretation = (
                f"Neutral positioning in {contract_name} ({large_spec_percentile:.0f}th percentile). "
                f"No crowding detected. No contrarian signal."
            )

        return signal, confidence, interpretation

    def _calculate_risk_appetite(self, contracts: List[COTPositioning]) -> float:
        """
        Calculate aggregate risk appetite from COT positioning.

        High risk appetite = speculators long equities, short bonds
        Low risk appetite = speculators short equities, long bonds
        """
        equity_positioning = 0.0
        bond_positioning = 0.0
        n_equity = 0
        n_bond = 0

        for contract in contracts:
            # Simplified: assume contracts with "S&P", "Nasdaq" are equities
            # Assume "Treasury" are bonds
            if any(x in contract.contract_name for x in ["S&P", "Nasdaq"]):
                equity_positioning += contract.large_spec_percentile or 50
                n_equity += 1
            elif "Treasury" in contract.contract_name:
                bond_positioning += contract.large_spec_percentile or 50
                n_bond += 1

        if n_equity == 0 and n_bond == 0:
            return 0.5  # Neutral

        avg_equity = equity_positioning / n_equity if n_equity > 0 else 50
        avg_bond = bond_positioning / n_bond if n_bond > 0 else 50

        # Risk appetite: high if long equities, short bonds
        # Scale 0-1: 0=risk-off, 1=risk-on
        risk_appetite = (avg_equity / 100 + (100 - avg_bond) / 100) / 2

        return risk_appetite


# Convenience function
async def analyze_cot_positioning() -> COTAnalysis:
    """
    Analyze CFTC Commitment of Traders positioning.

    Returns:
        COTAnalysis with extremes and signals
    """
    async with aiohttp.ClientSession() as session:
        analyzer = CFTCCOTAnalyzer()

        # Fetch latest report
        cot_data = await analyzer.fetch_latest_cot_report(session)

        if cot_data is None or cot_data.empty:
            logger.warning("No COT data available")
            return COTAnalysis(
                timestamp=datetime.now(timezone.utc),
                report_date=datetime.now(timezone.utc),
            )

        # Analyze positioning
        analysis = analyzer.analyze_positioning(cot_data)

        return analysis


# Test function
async def test_cot_analyzer():
    """Test COT analyzer"""
    print("=" * 70)
    print("CFTC COMMITMENT OF TRADERS ANALYSIS")
    print("=" * 70)
    print("\n⚠️  NOTE: Using mock data for demonstration")
    print("In production, this would fetch real CFTC reports\n")

    analysis = await analyze_cot_positioning()
    analysis.print_summary()

    # Print individual contracts
    print("\n" + "=" * 70)
    print("INDIVIDUAL CONTRACT ANALYSIS")
    print("=" * 70)

    for contract in analysis.contracts:
        print(f"\n{contract.contract_name}")
        print(f"  Net Position: {contract.large_spec_net:,} contracts")
        print(f"  Percentile: {contract.large_spec_percentile:.0f}th")
        print(f"  Extreme: {contract.positioning_extreme.value}")
        print(f"  Signal: {contract.signal.upper()}")
        print(f"  Confidence: {contract.confidence:.1%}")
        print(f"  {contract.interpretation}")

    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(test_cot_analyzer())
