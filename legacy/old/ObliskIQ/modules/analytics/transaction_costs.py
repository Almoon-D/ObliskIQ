"""
Transaction Cost Model — Single Source of Truth (v5.38.0)
=========================================================

Realistic modeling of trading costs for BOTH position sizing and backtesting.

All cost CONSTANTS live in modules/config/settings.py (RiskManagement). This
module turns those constants into the cost FUNCTIONS that the rest of the
system shares:

    PaperTrader (modules/execution/paper_trader.py)          ──┐
    Canonical backtester (modules/testing/walk_forward...)   ──┤──> these helpers
    TransactionCostModel (position sizing)                   ──┘

There is exactly one cost model. Paper trading and backtesting charge identical
costs because they call the same functions.

Cost components
---------------
- Equity: bid/ask half-spread + slippage + square-root market impact (ADV) +
  IBKR per-share commission.
- Options: per-leg HALF-SPREAD by liquidity tier (the silent killer — 1-3%
  round-trip even on liquid names) + IBKR per-contract commission, with an
  open-interest liquidity gate (reject if OI < settings.options_min_open_interest).

Jim Simons: "If you can't measure it, you can't trust it."
"""

import math
from typing import Dict, Optional, Tuple, Union

from modules.config.settings import config

# Liquidity threshold for equities (market cap). Large caps are "liquid".
LIQUID_MARKET_CAP_THRESHOLD: float = 10_000_000_000  # $10B

# Underlyings whose listed options trade penny-wide with deep books. Option
# legs on these names get the tighter "liquid" half-spread tier.
LIQUID_OPTIONS_UNDERLYINGS: frozenset = frozenset(
    {"SPY", "QQQ", "IWM", "DIA", "VIX", "SPX", "NDX", "RUT", "EEM", "XLF", "XLE"}
)

# Per-leg slippage default (liquid equity), re-exported for the backtester so it
# imports the single source rather than redeclaring a constant.
DEFAULT_SLIPPAGE_PCT: float = config.risk.slippage_liquid


# ---------------------------------------------------------------------------
# Shared cost FUNCTIONS (imported by PaperTrader and the canonical backtester)
# ---------------------------------------------------------------------------

def calculate_commission(quantity: float, price: float) -> float:
    """
    IBKR-style equity commission for ONE leg, from settings constants.

    - $0.005/share (commission_per_share)
    - $1.00 minimum per order (min_commission)
    - capped at 1% of trade value (max_commission_pct)

    Args:
        quantity: Number of shares (fractional allowed for backtests)
        price: Execution price per share

    Returns:
        Commission in dollars for this leg (0.0 for degenerate inputs)
    """
    if quantity <= 0 or price <= 0:
        return 0.0
    risk = config.risk
    commission = quantity * risk.commission_per_share
    commission = max(commission, risk.min_commission)
    commission = min(commission, price * quantity * risk.max_commission_pct)
    return round(commission, 2)


def market_impact_pct(
    order_value_dollars: float, avg_daily_volume_dollars: Optional[float]
) -> float:
    """
    Square-root market-impact approximation (Almgren-style).

        impact = coefficient * sqrt(order_value / ADV_dollars)

    Captures the reality that costs grow with order size relative to liquidity.
    Returns 0.0 when ADV is unknown/non-positive (caller falls back to the flat
    slippage tier only).

    Args:
        order_value_dollars: Notional traded on this leg
        avg_daily_volume_dollars: Average daily *dollar* volume of the name

    Returns:
        Impact as a fraction of notional, capped at settings.market_impact_cap
    """
    if (
        not avg_daily_volume_dollars
        or avg_daily_volume_dollars <= 0
        or order_value_dollars <= 0
    ):
        return 0.0
    risk = config.risk
    participation = order_value_dollars / avg_daily_volume_dollars
    impact = risk.market_impact_coefficient * math.sqrt(participation)
    return min(impact, risk.market_impact_cap)


def equity_slippage_pct(
    order_value_dollars: float,
    avg_daily_volume_dollars: Optional[float] = None,
    is_liquid: bool = True,
) -> float:
    """
    Total per-leg equity slippage: flat liquidity tier + square-root impact.

    Args:
        order_value_dollars: Notional traded on this leg
        avg_daily_volume_dollars: Average daily dollar volume (enables impact)
        is_liquid: True for large-cap/liquid names (tighter base slippage)

    Returns:
        Slippage as a fraction of notional
    """
    risk = config.risk
    base = risk.slippage_liquid if is_liquid else risk.slippage_illiquid
    return base + market_impact_pct(order_value_dollars, avg_daily_volume_dollars)


def is_option_liquid(symbol: Optional[str]) -> bool:
    """True if the underlying's listed options are in the deep-market tier."""
    if not symbol:
        return False
    return symbol.upper() in LIQUID_OPTIONS_UNDERLYINGS


def option_half_spread_pct(
    symbol: Optional[str] = None, is_liquid_underlying: Optional[bool] = None
) -> float:
    """
    Per-leg option half-spread (round-trip = 2x).

    Tier resolution:
    - explicit ``is_liquid_underlying`` wins,
    - else inferred from ``symbol`` via LIQUID_OPTIONS_UNDERLYINGS,
    - else defaults to the conservative single-name tier (most ObliskIQ option
      plays are single-name ITM).

    Returns:
        Half-spread as a fraction of option notional
    """
    risk = config.risk
    if is_liquid_underlying is None:
        is_liquid_underlying = is_option_liquid(symbol)
    return (
        risk.options_half_spread_liquid
        if is_liquid_underlying
        else risk.options_half_spread_single_name
    )


def option_open_interest_ok(open_interest: Optional[int]) -> bool:
    """
    Liquidity gate: True when the leg may trade.

    Per our own rule, reject option legs with open interest below
    settings.options_min_open_interest. ``None`` (unknown OI) is treated as
    acceptable so missing data does not silently block every option.
    """
    if open_interest is None:
        return True
    return open_interest >= config.risk.options_min_open_interest


class OptionLiquidityError(ValueError):
    """Raised when an option leg fails the open-interest liquidity gate."""


# ---------------------------------------------------------------------------
# TransactionCostModel — position-sizing oriented breakdown
# ---------------------------------------------------------------------------

class TransactionCostModel:
    """
    Models realistic transaction costs including:
    - Bid-ask spread (equity) / half-spread (options — the silent killer)
    - Market impact / slippage (square-root of participation)
    - Commissions (equity per-share, option per-contract)
    - Options open-interest liquidity gate
    """

    def __init__(self):
        self.risk = config.risk

    def calculate_total_cost(
        self,
        symbol: str,
        position_size_dollars: float,
        market_cap: float,
        is_option: bool = False,
        num_contracts: int = 0,
        avg_daily_volume_dollars: Optional[float] = None,
        is_liquid_underlying: Optional[bool] = None,
        open_interest: Optional[int] = None,
    ) -> Dict[str, Union[float, str, bool]]:
        """
        Calculate total transaction costs for a trade (single leg).

        Args:
            symbol: Ticker symbol
            position_size_dollars: Dollar amount of position
            market_cap: Company market cap (equity liquidity estimation)
            is_option: Whether this is an options trade
            num_contracts: Number of option contracts (if applicable)
            avg_daily_volume_dollars: Avg daily dollar volume (enables impact)
            is_liquid_underlying: Force the option liquidity tier (optional)
            open_interest: Option open interest for the liquidity gate

        Returns:
            Dict with cost breakdown. For options that fail the open-interest
            gate, ``rejected`` is True and costs are zero.
        """
        if is_option:
            return self._option_cost(
                symbol=symbol,
                position_size_dollars=position_size_dollars,
                num_contracts=num_contracts,
                is_liquid_underlying=is_liquid_underlying,
                open_interest=open_interest,
            )
        return self._equity_cost(
            position_size_dollars=position_size_dollars,
            market_cap=market_cap,
            avg_daily_volume_dollars=avg_daily_volume_dollars,
        )

    def _equity_cost(
        self,
        position_size_dollars: float,
        market_cap: float,
        avg_daily_volume_dollars: Optional[float],
    ) -> Dict[str, Union[float, str, bool]]:
        is_liquid = self._is_liquid(market_cap)
        spread_pct = (
            self.risk.bid_ask_spread_liquid
            if is_liquid
            else self.risk.bid_ask_spread_illiquid
        )
        slippage_pct = equity_slippage_pct(
            position_size_dollars, avg_daily_volume_dollars, is_liquid
        )

        spread_cost = position_size_dollars * spread_pct
        slippage_cost = position_size_dollars * slippage_pct
        commission = self.risk.commission_per_trade

        return self._assemble(
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            commission=commission,
            position_size_dollars=position_size_dollars,
            liquidity_tier="liquid" if is_liquid else "illiquid",
        )

    def _option_cost(
        self,
        symbol: str,
        position_size_dollars: float,
        num_contracts: int,
        is_liquid_underlying: Optional[bool],
        open_interest: Optional[int],
    ) -> Dict[str, Union[float, str, bool]]:
        # Liquidity gate: reject illiquid option legs outright.
        if not option_open_interest_ok(open_interest):
            return {
                "spread_cost": 0.0,
                "slippage_cost": 0.0,
                "commission": 0.0,
                "total_cost": 0.0,
                "total_cost_pct": 0.0,
                "liquidity_tier": "rejected",
                "rejected": True,
                "reject_reason": (
                    f"open_interest {open_interest} < "
                    f"{self.risk.options_min_open_interest} minimum"
                ),
            }

        liquid = (
            is_liquid_underlying
            if is_liquid_underlying is not None
            else is_option_liquid(symbol)
        )
        half_spread = option_half_spread_pct(symbol, liquid)

        # The half-spread IS the dominant execution cost for options. We book it
        # as spread_cost; there is no separate equity-style slippage term.
        spread_cost = position_size_dollars * half_spread
        commission = num_contracts * self.risk.options_commission_per_contract

        result = self._assemble(
            spread_cost=spread_cost,
            slippage_cost=0.0,
            commission=commission,
            position_size_dollars=position_size_dollars,
            liquidity_tier="liquid_option" if liquid else "single_name_option",
        )
        result["rejected"] = False
        return result

    @staticmethod
    def _assemble(
        spread_cost: float,
        slippage_cost: float,
        commission: float,
        position_size_dollars: float,
        liquidity_tier: str,
    ) -> Dict[str, Union[float, str, bool]]:
        total_cost = spread_cost + slippage_cost + commission
        total_cost_pct = (
            (total_cost / position_size_dollars) * 100
            if position_size_dollars > 0
            else 0
        )
        return {
            "spread_cost": round(spread_cost, 2),
            "slippage_cost": round(slippage_cost, 2),
            "commission": round(commission, 2),
            "total_cost": round(total_cost, 2),
            "total_cost_pct": round(total_cost_pct, 4),
            "liquidity_tier": liquidity_tier,
        }

    def adjust_position_for_costs(
        self, target_position_dollars: float, transaction_costs: Dict[str, float]
    ) -> float:
        """
        Adjust position size downward to account for transaction costs.

        Args:
            target_position_dollars: Desired position size
            transaction_costs: Output from calculate_total_cost()

        Returns:
            Adjusted position size (slightly smaller to cover costs)
        """
        total_cost = transaction_costs["total_cost"]
        adjusted = target_position_dollars - total_cost
        return max(0, adjusted)

    def calculate_breakeven_move(
        self, entry_price: float, transaction_costs_pct: float
    ) -> Tuple[float, float]:
        """
        Calculate how much the underlying must move to break even.

        Costs are paid on BOTH entry and exit, so the round-trip drag is 2x the
        single-leg percentage.

        Args:
            entry_price: Entry price per share/contract
            transaction_costs_pct: Single-leg cost % (from calculate_total_cost)

        Returns:
            Tuple of (breakeven_price, breakeven_move_pct)
        """
        total_cost_roundtrip_pct = transaction_costs_pct * 2
        breakeven_price = entry_price * (1 + total_cost_roundtrip_pct / 100)
        breakeven_move_pct = total_cost_roundtrip_pct
        return (round(breakeven_price, 2), round(breakeven_move_pct, 4))

    def _is_liquid(self, market_cap: float) -> bool:
        """True if the equity is liquid (large cap) based on market cap."""
        return market_cap >= LIQUID_MARKET_CAP_THRESHOLD
