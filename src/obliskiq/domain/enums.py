"""The closed vocabularies of the domain.

Each is a ``StrEnum`` whose values are what the store holds and the configuration spells.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Self


class AssetClass(StrEnum):
    """What an instrument is, in the plan's terms (MASTER_PLAN §5.3)."""

    STOCK = "stock"
    ETF = "etf"
    ETC = "etc"
    BOND = "bond"
    FUND = "fund"
    OPTION = "option"
    INDEX = "index"
    FUTURE = "future"


class InstrumentRole(StrEnum):
    """What the system may do with an instrument. Reading it as data is always allowed."""

    DATA = "data"
    TRADABLE_CANDIDATE = "tradable_candidate"
    OPTION_UNDERLYING = "option_underlying"


class KidStatus(StrEnum):
    """Whether a PRIIPs key information document is available. Unknown is ``None``, not a member."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_REQUIRED = "not_required"


class ShareClass(StrEnum):
    ACCUMULATING = "accumulating"
    DISTRIBUTING = "distributing"


class OptionRight(StrEnum):
    CALL = "C"
    PUT = "P"


class MarketDataStatus(StrEnum):
    """The kind of quote a row holds, decoded from the route's own flag.

    ``None`` (a missing status) means the route said nothing; it never means ``REALTIME``.
    ``NONE`` means the route said there is no market data.
    """

    REALTIME = "realtime"
    DELAYED = "delayed"
    FROZEN = "frozen"
    NONE = "none"

    @classmethod
    def from_route_flag(cls, flag: str | None) -> Self | None:
        """The status a route's verbatim flag stands for; ``None`` when it is not recognised.

        A flag naming both frozen and delayed data is read as frozen, the stricter claim.
        An unrecognised flag is not guessed at: the caller keeps the raw text beside a missing
        status.
        """
        if flag is None:
            return None
        words = set(flag.strip().upper().replace("-", "_").split("_"))
        if "FROZEN" in words:
            return cls.FROZEN
        if "DELAYED" in words:
            return cls.DELAYED
        if "REALTIME" in words:
            return cls.REALTIME
        return None


class MarketDataQuality(StrEnum):
    """How a reader should label a quote: live, indicative, or not reported."""

    LIVE = "live"
    INDICATIVE = "indicative"
    UNREPORTED = "unreported"


#: Worst first. A set of rows is only as good as its weakest member.
_QUALITY_ORDER = (
    MarketDataQuality.INDICATIVE,
    MarketDataQuality.UNREPORTED,
    MarketDataQuality.LIVE,
)


def market_data_quality(status: MarketDataStatus | None) -> MarketDataQuality:
    """The label for one row: only a real-time quote is live."""
    if status is MarketDataStatus.REALTIME:
        return MarketDataQuality.LIVE
    if status in (MarketDataStatus.DELAYED, MarketDataStatus.FROZEN):
        return MarketDataQuality.INDICATIVE
    return MarketDataQuality.UNREPORTED


def worst_market_data_quality(qualities: Iterable[MarketDataQuality]) -> MarketDataQuality | None:
    """The label for a table built from many rows; ``None`` for no rows.

    A table mixing live and frozen quotes is indicative: the weakest row decides.
    """
    seen = set(qualities)
    for quality in _QUALITY_ORDER:
        if quality in seen:
            return quality
    return None
