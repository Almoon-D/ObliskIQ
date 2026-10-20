"""Managed capital, Session 6's minimal form (MASTER_PLAN §5.15).

``capital.mode`` says how much of the account the bot manages: ``whole_account``, its whole net
liquidation value; ``cap``, at most ``capital.cap_base`` of it, in the base currency, so a large
paper balance behaves like the real account size and minimum trades, integer shares and
commissions are simulated honestly. A NAV that cannot be read manages nothing: the rebalancer then
plans no trade (honest degradation, never a guessed size).

Session 7 builds investable capital on this (managed less reserved less unmanaged), the
cash-flow ledger and the sleeve budgets.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from decimal import Decimal

from ..config.schema_runtime import CapitalConfig

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class ManagedCapital:
    """What the bot manages, in the base currency, and how it was decided."""

    mode: str
    nav: Decimal
    managed: Decimal
    detail: str


def managed_capital(capital: CapitalConfig, nav: Decimal | None) -> ManagedCapital | str:
    """The managed capital for an account NAV; why there is none when the NAV is unreadable."""
    if nav is None or not nav.is_finite() or nav <= ZERO:
        return "the account's net liquidation value cannot be read, so nothing is managed"
    mode = capital.mode
    if mode == "whole_account":
        return ManagedCapital(mode, nav, nav, "the whole account")
    cap = capital.cap_base
    if cap is None:  # the schema refuses it; kept so the type is narrowed honestly
        return "capital.mode cap names no cap_base"
    if nav < cap:
        return ManagedCapital(mode, nav, nav, f"the whole account, below the cap {cap}")
    return ManagedCapital(mode, nav, cap, f"capped at {cap}")
