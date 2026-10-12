"""Contract identity: a conid pinned with its listing venue and currency, or nothing.

A ticker is not an identity. Every listing of one company answers the same bare ticker, and
``/iserver/secdef/search`` ranks rows differently at different hours [CP ✓ 2026-09-10: ASML,
SAN and TTE resolved to their New York lines at 20:05 UTC and to the European ones at 09:48]. A
row resolved to the wrong line collects a real series for the wrong instrument, which no later
check notices. So:

* a resolution must end at exactly one row after the exact-ticker and declared-venue rules, and
  ``/iserver/contract/{conid}/info`` must confirm the declared currency; zero or several rows, or
  a currency that differs, refuse with :class:`AmbiguousContractError` — nothing is guessed;
* a pinned conid is confirmed against the declaration, never re-searched;
* a correction supersedes the old instrument in the store, never deletes it: history collected
  against the old conid stays, and the old row names its replacement. A symbol that failed to
  resolve supersedes nothing (an outage must not retire instruments).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import datetime

from ..store import Store
from ..store.repos import get_instrument, supersede_instrument
from .client import Secdef
from .errors import AmbiguousContractError
from .models import ContractIdentity, ContractInfo

#: How many candidate conids a refusal names.
SHOWN_CANDIDATES = 5


def _listed_on(info: ContractInfo, exchange: str) -> bool:
    venue = exchange.upper()
    if info.listing_exchange is not None:
        return info.listing_exchange.upper() == venue
    return venue in {code.upper() for code in info.valid_exchanges}


async def resolve_identity(
    secdef: Secdef,
    symbol: str,
    *,
    listing_exchange: str,
    currency: str,
    sec_type: str = "STK",
    pinned_conid: int | None = None,
) -> ContractIdentity:
    """The one contract a declaration names, confirmed by IBKR's contract details."""
    if pinned_conid is not None:
        conid = int(pinned_conid)
    else:
        matches = await secdef.search(symbol, sec_type=sec_type)
        picked = secdef.pick(matches, symbol, exchange=listing_exchange)
        if len(picked) != 1:
            shown = ", ".join(str(m.conid) for m in picked[:SHOWN_CANDIDATES]) or "none"
            raise AmbiguousContractError(
                f"{symbol} on {listing_exchange} resolved to {len(picked)} contracts ({shown}); "
                "pin its conid"
            )
        conid = picked[0].conid
    info = await secdef.info(conid)
    if info.currency != currency:
        raise AmbiguousContractError(
            f"{symbol} conid {conid} trades in {info.currency or 'an unreported currency'}, "
            f"not the declared {currency}"
        )
    if not _listed_on(info, listing_exchange):
        raise AmbiguousContractError(
            f"{symbol} conid {conid} is not listed on the declared {listing_exchange}"
        )
    return ContractIdentity(
        conid=conid, symbol=symbol.upper(), listing_exchange=listing_exchange, currency=currency
    )


def reconcile_identity(
    store: Store, *, held_conid: int, resolved: ContractIdentity | None, at: datetime
) -> bool:
    """Supersede ``held_conid`` by the resolved conid when they differ. Returns whether it did.

    The replacement must already be in the store (the caller upserts it first); the old row is
    kept, with its history, and names its replacement. ``resolved`` ``None`` (the symbol failed
    to resolve) and an unchanged conid change nothing.
    """
    if resolved is None or resolved.conid == held_conid:
        return False
    if get_instrument(store, resolved.conid, as_of=at.date()) is None:
        raise AmbiguousContractError(
            f"the replacement conid {resolved.conid} is not in the store; upsert it before "
            "superseding"
        )
    supersede_instrument(store, held_conid, resolved.conid, at=at)
    return True
