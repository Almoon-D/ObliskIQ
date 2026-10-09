"""
Strategy Universe — Single Source of Truth (v6.1.0)
===================================================

The mega/large-cap equity universe traded by ``StrategyEngineV1`` plus the ETF
sleeves whose implied-volatility history we persist every pipeline run. This
module exists so the verdict runner
(``scripts/run_strategy_v1_verdict.py``) and the Phase-1 IV snapshot collector
(``modules/market/iv_snapshot_collector.py``) share ONE list — DRY: one place
to update the tradable universe.

SURVIVORSHIP-BIAS NOTE (carried verbatim from the verdict doc):
    ``STRATEGY_UNIVERSE`` is a CURRENT-day constituent snapshot (as of 2026-06).
    yfinance serves no delisted tickers, so firms that failed/were acquired
    during 2014-2026 are absent, and using today's membership leaks knowledge of
    which firms grew large. Both biases inflate backtest results; the verdict
    quantifies the expected inflation instead of hiding it. Point-in-time
    discipline WITHIN the list is enforced by ``StrategyEngineV1`` (a ticker
    enters the candidate universe only once it has >= 252 trading days of
    history as of the decision date, and the $50M ADV liquidity gate uses
    trailing data only).
"""

from __future__ import annotations

from typing import List, Tuple

# -----------------------------------------------------------------------------
# Tradable equity universe (S&P 100-style mega/large-cap snapshot)
# -----------------------------------------------------------------------------
STRATEGY_UNIVERSE: Tuple[str, ...] = (
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AIG", "AMD", "AMGN", "AMT", "AMZN",
    "AVGO", "AXP", "BA", "BAC", "BK", "BKNG", "BLK", "BMY", "BRK-B", "C",
    "CAT", "CHTR", "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS",
    "CVX", "DE", "DHR", "DIS", "DUK", "EMR", "FDX", "GD", "GE", "GILD",
    "GM", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTC", "INTU", "ISRG",
    "JNJ", "JPM", "KO", "LIN", "LLY", "LMT", "LOW", "MA", "MCD", "MDLZ",
    "MDT", "MET", "META", "MMM", "MO", "MRK", "MS", "MSFT", "NEE", "NFLX",
    "NKE", "NOW", "NVDA", "ORCL", "PEP", "PFE", "PG", "PLTR", "PM", "PYPL",
    "QCOM", "RTX", "SBUX", "SCHW", "SO", "SPG", "T", "TGT", "TJX", "TMO",
    "TMUS", "TSLA", "TXN", "UNH", "UNP", "UPS", "USB", "V", "VZ", "WFC",
    "WMT", "XOM",
)

#: Equity benchmark + bond leg used by the verdict runner's benchmarks.
BENCHMARK: str = "SPY"
BOND_ETF: str = "TLT"  # 60/40 benchmark leg

#: Strategy v1.1 (H2): the cash-parking sleeve. ALL strategy cash (the
#: defensive sleeve when the trend gate fires AND the 15%/risk-off cash
#: floors) is held in BIL (SPDR Bloomberg 1-3 Month T-Bill ETF, inception
#: 2007 — covers the verdict ingest window) instead of earning 0%.
#: T-bills are a PARKING SPOT, not a bet: rotating into long-duration TLT
#: during risk-off would be a DIRECTIONAL duration position and is
#: explicitly NOT part of v1.1 (recorded as a v2 hypothesis in the verdict).
CASH_ETF: str = "BIL"

#: All tickers the verdict runner ingests (universe + benchmark/sleeve legs).
ALL_TICKERS: Tuple[str, ...] = STRATEGY_UNIVERSE + (BENCHMARK, BOND_ETF, CASH_ETF)

# -----------------------------------------------------------------------------
# ETF sleeves tracked for IV history (Phase-1 IV snapshot collector)
# -----------------------------------------------------------------------------
#: Liquid index / asset-class ETFs we always want IV history for.
CORE_ETFS: Tuple[str, ...] = ("SPY", "QQQ", "IWM", "TLT", "GLD")

#: SPDR sector ETFs (11 GICS sectors).
SECTOR_ETFS: Tuple[str, ...] = (
    "XLK", "XLE", "XLF", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
)


def iv_snapshot_universe() -> List[str]:
    """
    Deduped, order-stable union of CORE_ETFS + SECTOR_ETFS + STRATEGY_UNIVERSE.

    This is exactly the set the Phase-1 IV snapshot collector persists each run
    (ATM IV + 30d realized vol + IV rank + 25-delta skew), so IV-rank history
    accrues for the same names the strategy can actually trade plus the index
    and sector sleeves we reference for regime context.
    """
    seen: dict[str, None] = {}
    for ticker in (*CORE_ETFS, *SECTOR_ETFS, *STRATEGY_UNIVERSE):
        seen[ticker] = None
    return list(seen)
