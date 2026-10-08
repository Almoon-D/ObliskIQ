"""Instrument classification shared by ingestion and by the candidate gates.

Two rules live here, and they are deliberately in one place rather than two.

**Leveraged and inverse ETPs are rejected at INGESTION.** Not at recommendation
time. An instrument that never enters the universe cannot be recommended by a
future code path that forgot to check, and there is always a future code path.
Daily-reset leverage decays badly over the configured entry tenor window, so
there is no holding period in this mandate where one is appropriate.

**Futures contracts are never tradeable candidates.** The curve is a data call
and stays permitted — ``instruments.futures_data_use`` — but the contract itself
carries undefined loss and violates the no-leverage mandate. The two are easy to
conflate when implementing the ban, and conflating them blinds wrapper selection.

The marker list comes from ``instruments.leveraged_inverse_markers`` and is
matched against an instrument's resolved DESCRIPTION, never against its ticker.
A ticker promises nothing: "BULL" is a substring of plenty of innocent names,
and no exchange guarantees that a 3x product spells it out in four characters.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config

#: ``universe.asset_class`` for a futures contract. Prohibited as a position;
#: its curve remains a data call.
ASSET_CLASS_FUTURE = "FUT"

#: ``universe.asset_class`` for an index. A level, not an instrument.
ASSET_CLASS_INDEX = "IND"

#: Config list naming what may never be held.
PROHIBITED_KEY = "instruments.prohibited"

PROHIBITED_FUTURES = "futures_contracts"
PROHIBITED_FUTURES_OPTIONS = "futures_options"
PROHIBITED_LEVERAGED = "leveraged_etps"
PROHIBITED_INVERSE = "inverse_etps"

REASON_LEVERAGED = "leveraged ETP: embedded leverage, daily reset decays over the entry tenor"
REASON_INVERSE = "inverse ETP: embedded leverage, daily reset decays over the entry tenor"
REASON_MARKER = "name matches a leveraged/inverse marker in instruments.leveraged_inverse_markers"


@dataclass(frozen=True)
class Rejection:
    """Why an instrument may not enter the universe."""

    symbol: str
    reason: str
    detail: str | None = None


class LeveragedInverseFilter:
    """Decides whether an instrument is a leveraged or inverse ETP.

    Declared rows say so outright with ``leveraged:`` / ``inverse:``. Rows folded
    in from the live account have only whatever description the broker returns,
    so those are matched against the configured markers.
    """

    def __init__(self, config: Config) -> None:
        prohibited = {str(item) for item in config.require_list(PROHIBITED_KEY)}
        self._leveraged_banned = PROHIBITED_LEVERAGED in prohibited
        self._inverse_banned = PROHIBITED_INVERSE in prohibited
        self._markers = tuple(
            str(marker).upper()
            for marker in config.require_list("instruments.leveraged_inverse_markers")
        )

    @property
    def markers(self) -> tuple[str, ...]:
        return self._markers

    @property
    def active(self) -> bool:
        """False only if the prohibition were lifted in config, which it is not."""
        return self._leveraged_banned or self._inverse_banned

    def check_declared(self, symbol: str, *, leveraged: bool, inverse: bool) -> Rejection | None:
        """Reject a declared row that flags itself leveraged or inverse."""
        if leveraged and self._leveraged_banned:
            return Rejection(symbol, REASON_LEVERAGED, "declared leveraged: true")
        if inverse and self._inverse_banned:
            return Rejection(symbol, REASON_INVERSE, "declared inverse: true")
        return None

    def check_description(self, symbol: str, description: str | None) -> Rejection | None:
        """Reject a row whose resolved description matches a configured marker.

        Matching is on the description because that is the only field with any
        semantics. A ``None`` description matches nothing: an unnamed instrument
        is not evidence of leverage, and rejecting on absence would silently drop
        every row the resolver failed to name.
        """
        if not self.active or not description:
            return None
        haystack = description.upper()
        for marker in self._markers:
            if marker in haystack:
                return Rejection(symbol, REASON_MARKER, f"{description!r} contains {marker!r}")
        return None


def is_futures_contract(asset_class: str | None) -> bool:
    """True for a futures contract, which is never a tradeable candidate."""
    return str(asset_class or "").upper() == ASSET_CLASS_FUTURE


def is_index(asset_class: str | None) -> bool:
    """True for an index level, which is a measurement and not an instrument."""
    return str(asset_class or "").upper() == ASSET_CLASS_INDEX
