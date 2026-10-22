"""The universe in the store: seeded from ``config/universe.yaml``, corrected by supersession, and
what ingestion refuses recorded.

Ported from lite's ``collect/universe.py`` (``UniverseSync``), on Session 3's identity rules:

* **Seed.** A pinned instrument is confirmed by IBKR's contract details (its currency and listing
  venue, ``ibkr/identity.py``), never re-searched, and written to ``instruments`` with its roles
  and attributes. One whose confirmation fails is not seeded, so it is not collected; one already
  seeded stays, with its history.
* **Pin.** An instrument without a conid is resolved through the contract search (exact ticker,
  then the declared venue, then the declared currency) and reported with the conid it resolves
  to: ``obliskiq universe pin`` writes that conid into the file (:func:`pin_in_file`). It is not
  collected until pinned, because a ticker is not an identity.
* **Supersede.** A pin the owner corrected (the same symbol on the same venue, a new conid)
  supersedes the stored instrument: nothing is deleted, the old row keeps its history and names
  its replacement. An instrument that failed to confirm supersedes nothing.
* **Reject.** A product whose resolved description reads as leveraged or inverse
  (``instruments.leveraged_inverse_markers``) never enters the store, and the refusal is recorded
  in ``universe_rejections`` so an exclusion is auditable rather than a row that never appeared.

A stock candidate a thesis pinned is seeded from its resolution (``store/universe.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from collections.abc import Callable
from collections.abc import Collection as Venues
from datetime import datetime
from pathlib import Path

from ...config.schema import ObliskConfig
from ...config.schema_universe import UniverseInstrument
from ...domain.enums import AssetClass, InstrumentRole
from ...domain.instrument import Instrument
from ...domain.universe import Rejection, RejectionOrigin
from ...errors import ConfigError
from ...ibkr.client import Secdef
from ...ibkr.identity import resolve_identity
from ...ibkr.models import ContractIdentity
from ...store import repos
from ...store import universe as universe_repo
from ...store.db import Store
from ...store.worker import StoreWorker
from .plumbing import Collection, Pacer, failure_text
from .targets import refused_as_leveraged, venue_of

STEP = "universe"

#: The contract search's security type for each asset class (an ETF searches as a stock).
SEARCH_SEC_TYPE: dict[AssetClass, str] = {
    AssetClass.STOCK: "STK",
    AssetClass.ETF: "STK",
    AssetClass.ETC: "STK",
    AssetClass.INDEX: "IND",
    AssetClass.FUND: "FUND",
    AssetClass.BOND: "BOND",
}


def _stored(declared: UniverseInstrument, identity: ContractIdentity, at: datetime) -> Instrument:
    attrs = declared.attributes
    return Instrument(
        conid=identity.conid,
        symbol=declared.symbol,
        listing_exchange=declared.listing_exchange,
        currency=declared.currency,
        asset_class=declared.asset_class,
        domicile=attrs.domicile,
        ucits=attrs.ucits,
        kid_status=attrs.kid,
        share_class=attrs.share_class,
        lot_size=attrs.lot_size,
        option_exchange=declared.option_exchange,
        history_exchange=declared.history_exchange,
        roles=frozenset(declared.roles),
        superseded_by=None,
        created_at=at,
        superseded_at=None,
    )


def seed_instrument(store: Store, instrument: Instrument, *, at: datetime) -> list[int]:
    """Write ``instrument`` and supersede every standing instrument of the same symbol and venue
    under another conid. Returns the conids superseded; one transaction."""
    with store.transaction():
        repos.upsert_instrument(store, instrument)
        standing = repos.list_instruments(store, as_of=at.date(), include_superseded=False)
        replaced = [
            held.conid
            for held in standing
            if held.conid != instrument.conid
            and held.symbol == instrument.symbol
            and held.listing_exchange == instrument.listing_exchange
        ]
        for conid in replaced:
            repos.supersede_instrument(store, conid, instrument.conid, at=at)
    return replaced


class UniverseSync:
    """Seeds the store's instruments from the universe and records what ingestion refused."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        secdef: Secdef,
        worker: StoreWorker,
        pacer: Pacer,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._secdef = secdef
        self._worker = worker
        self._pacer = pacer
        self._now = now

    async def run(self, venues: Venues[str] | None = None, *, dry_run: bool = False) -> Collection:
        """Seed the declared instruments listed on ``venues`` (every venue: ``None``) and the
        pinned stock candidates."""
        collection = Collection(STEP)
        at = self._now()
        rejections: list[Rejection] = []
        superseded: list[str] = []
        for declared in self._settings.universe.instruments:
            venue = venue_of(self._settings, declared.listing_exchange)
            if venues is not None and venue not in venues:
                continue
            marker = refused_as_leveraged(self._settings, declared)
            if marker is not None:
                rejections.append(
                    Rejection(
                        rejected_at=at,
                        symbol=declared.symbol,
                        origin=RejectionOrigin.DECLARED,
                        listing_exchange=declared.listing_exchange,
                        conid=declared.conid,
                        description=declared.description,
                        reason=f"reads as a leveraged or inverse product (marker {marker!r})",
                    )
                )
                continue
            identity = await self._identity(declared, collection)
            await self._pacer.pace()
            if identity is None or declared.conid is None:
                continue
            if not dry_run:
                replaced = await self._worker.run(
                    seed_instrument, _stored(declared, identity, at), at=at
                )
                superseded += [
                    f"{declared.symbol}: {conid} by {identity.conid}" for conid in replaced
                ]
            collection.succeeded(declared.symbol, 1, conid=identity.conid)
        seeded = 0 if dry_run else await self._worker.run(_seed_candidates, at=at)
        if rejections and not dry_run:
            await self._worker.run(universe_repo.record_rejections, rejections)
        collection.notes.update(
            {
                "rejected": [r.symbol for r in rejections],
                "superseded": superseded,
                "candidates_seeded": seeded,
            }
        )
        return collection

    async def _identity(
        self, declared: UniverseInstrument, collection: Collection
    ) -> ContractIdentity | None:
        sec_type = SEARCH_SEC_TYPE.get(declared.asset_class)
        if sec_type is None:
            collection.failed(declared.symbol, f"a {declared.asset_class} is not resolved here")
            return None

        async def resolve() -> ContractIdentity:
            return await resolve_identity(
                self._secdef,
                declared.symbol,
                listing_exchange=declared.listing_exchange,
                currency=declared.currency,
                sec_type=sec_type,
                pinned_conid=declared.conid,
            )

        try:
            identity = await self._pacer.retry(declared.symbol, resolve)
        except Exception as exc:  # one instrument never stops the step
            what = "its pin does not confirm" if declared.conid else "no single contract resolves"
            collection.failed(declared.symbol, f"{what}: {failure_text(exc)}")
            return None
        if declared.conid is None:
            collection.succeeded(
                declared.symbol,
                0,
                degraded=True,
                reason=(
                    f"not pinned: resolves to conid {identity.conid}; `obliskiq universe pin "
                    f"{declared.symbol}` writes it, and it is collected once pinned"
                ),
                resolved=identity.conid,
            )
        return identity


def _seed_candidates(store: Store, *, at: datetime) -> int:
    """Seed every pinned stock candidate the store has not yet seeded, as a tradable share."""
    standing = {
        held.conid
        for held in repos.list_instruments(store, as_of=at.date(), include_superseded=True)
    }
    seeded = 0
    for resolution in universe_repo.candidate_resolutions(store).values():
        if not resolution.pinned or resolution.conid is None or resolution.conid in standing:
            continue
        if resolution.symbol is None or resolution.listing_exchange is None:
            continue
        if resolution.currency is None:
            continue
        repos.upsert_instrument(
            store,
            Instrument(
                conid=resolution.conid,
                symbol=resolution.symbol,
                listing_exchange=resolution.listing_exchange,
                currency=resolution.currency,
                asset_class=AssetClass.STOCK,
                domicile=None,
                ucits=None,
                kid_status=None,
                share_class=None,
                lot_size=None,
                option_exchange=None,
                history_exchange=None,
                roles=frozenset({InstrumentRole.TRADABLE_CANDIDATE}),
                superseded_by=None,
                created_at=at,
                superseded_at=None,
            ),
        )
        seeded += 1
    return seeded


#: An instrument's block in universe.yaml: its ``- symbol:`` line up to the next one.
_BLOCK = re.compile(r"^  - symbol: (?P<symbol>\S+)\s*$", re.MULTILINE)
_CONID_LINE = re.compile(r"^    conid: null\s*$", re.MULTILINE)


def pin_in_file(path: Path, symbol: str, conid: int) -> None:
    """Write ``conid`` into ``symbol``'s ``conid: null`` line in ``path``, leaving every other
    line (and every comment) as it was. Refused when the symbol is not declared once or is
    already pinned."""
    text = path.read_text(encoding="utf-8")
    starts = [match for match in _BLOCK.finditer(text) if match.group("symbol") == symbol]
    if len(starts) != 1:
        raise ConfigError(f"{symbol} is declared {len(starts)} times in {path.name}, not once")
    following = _BLOCK.search(text, starts[0].end())
    end = following.start() if following is not None else len(text)
    block = text[starts[0].start() : end]
    line = _CONID_LINE.search(block)
    if line is None:
        raise ConfigError(f"{symbol} in {path.name} has no `conid: null` to pin")
    pinned = block[: line.start()] + f"    conid: {int(conid)}" + block[line.end() :]
    path.write_text(text[: starts[0].start()] + pinned + text[end:], encoding="utf-8")
