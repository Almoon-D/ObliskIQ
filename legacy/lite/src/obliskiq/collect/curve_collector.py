"""Daily futures term-structure snapshot.

Whether backwardation is steepening or flattening carries more information than
its level, so what matters is the series rather than any one reading.

Not the same problem as option chains — a corrected premise
-----------------------------------------------------------

This collector was built on the belief that curve-shape history could not be
recovered later, which put it alongside the chain collector as irreplaceable.
That is wrong. ``get_price_history`` works on an individual futures contract, and
``search_futures`` takes ``include_expired=true``, which returns expired
contracts whose history is also served. The curve on any past date can therefore
be rebuilt from each then-listed rung's bars.

Running daily is still the right default — one snapshot against a call per rung
per day — but a missed day is a recoverable inconvenience here, and a permanent
hole only for option chains.

Priced from snapshots, which this account is not entitled to
------------------------------------------------------------

Rungs are priced from ``quotes()``. Observed live 2026-08-24: COMEX futures
snapshots return ``{}`` outright while the same contracts' daily bars serve at
``delayed: 600``. So without a futures market-data subscription this writes a
complete ladder carrying no price and no roll yield, and reports **degraded**
rather than ``ok`` for it. Pricing each rung from its last daily bar instead
would work with the entitlements the account actually has.

Data use only
-------------

A futures contract is a prohibited instrument and is never returned as a
tradeable candidate in any code path. The curve is collected because it drives
**wrapper selection**: in contango beyond
``commodities.term_structure.contango_max_annualised_for_futures_wrapper``, a
futures-tracking product bleeds roll over a 90-200 day hold whether or not the
thesis is right, and the answer is producer equity or a physical ETC instead. See
``instruments.futures_data_use``.

Sign convention
---------------

``annualised_roll`` is computed for each deferred contract against the front, from
the point of view of a long holder rolling forward::

    roll(n) = (P_front / P_n) ** (365 / (t_n - t_front)) - 1

* **positive => backwardation.** The front is dearer than the deferred, so
  rolling forward buys a cheaper contract. Supportive of long exposure.
* **negative => contango.** Rolling forward buys a dearer contract. This is the
  bleed that kills a futures-tracking wrapper.
* The **front contract is NULL**: there is nothing to roll it against, and
  writing 0.0 there would put a fictional flat point into every curve.

Classification against the config threshold belongs to ``commodity_engine``.
This collector stores the shape; it does not judge it.

.. warning::
   UNVERIFIED. ``/trsrv/futures`` and its response shape are documentation-derived.
   Futures require the NATIVE exchange; SMART returns empty.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import Config
from ..errors import NoMatchingTargets, StoreError
from ..logging_setup import get_logger
from ..store import Store
from ..util.timeutil import parse_date, utc_stamp, utc_today
from .retry import TargetRetryPolicy, pace, with_target_retry
from .runlog import CollectorRun
from .source import FuturesContract, MarketData, Quote

log = get_logger("collect.curve")

COLLECTOR_NAME = "curve"


@dataclass(frozen=True)
class CurvePoint:
    """One rung of a stored curve."""

    root: str
    expiry: str
    price: float | None
    annualised_roll: float | None
    conid: int
    exchange: str | None
    currency: str | None
    #: The market-data state the route reported for this rung's quote, or None
    #: where it reported nothing. See migration 006.
    market_data_status: str | None = None


def tracked_roots(store: Store, *, only: Sequence[str] | None = None) -> list[dict[str, Any]]:
    rows = store.execute(
        "SELECT root, exchange, currency, bucket FROM futures_roots WHERE tracked = 1 ORDER BY root"
    ).fetchall()
    roots = [dict(row) for row in rows]
    if only:
        wanted = {s.upper() for s in only}
        roots = [r for r in roots if str(r["root"]).upper() in wanted]
    return roots


def annualised_roll(
    *,
    front_price: float,
    deferred_price: float,
    days_apart: int,
    calendar_days_per_year: float,
) -> float | None:
    """Annualised roll yield of a deferred contract against the front.

    ``None`` when it cannot be computed rather than 0.0: a curve point with an
    unusable price is missing, not flat, and a fictional flat point would drag
    every downstream contango classification toward "fine".
    """
    if front_price <= 0 or deferred_price <= 0 or days_apart <= 0:
        return None
    return (front_price / deferred_price) ** (calendar_days_per_year / days_apart) - 1


class CurveCollector:
    """Writes one day of term structure for every tracked commodity root."""

    def __init__(self, store: Store, config: Config, market_data: MarketData) -> None:
        self._store = store
        self._config = config
        self._market_data = market_data
        self._policy = TargetRetryPolicy.from_config(config)
        curve = config.section("collectors.curve")
        self._max_expiries = curve.require_int("max_expiries_per_root")
        self._min_expiries_for_roll = curve.require_int("min_expiries_for_roll")
        self._calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")

    async def run(
        self,
        run: CollectorRun,
        *,
        only: Sequence[str] | None = None,
        dry_run: bool = False,
        today: date | None = None,
    ) -> int:
        session = today or utc_today()
        roots = tracked_roots(self._store, only=only)
        if not roots:
            # A run reporting success over zero roots is indistinguishable from a
            # healthy one in the log.
            # Same discipline as assert_seeded, applied to the table this
            # collector actually reads.
            if only:
                raise NoMatchingTargets(
                    f"none of the requested roots {sorted(only)} is tracked. Declare "
                    "them under futures_roots in universe.yaml, or drop --only."
                )
            raise StoreError(
                "no futures roots are tracked, so there is nothing to collect. "
                "Declare them under futures_roots in universe.yaml and seed with:\n"
                "    obliskiq-collect universe"
            )

        written = 0
        for row in roots:
            root = str(row["root"])
            try:
                rows = await with_target_retry(
                    self._policy,
                    root,
                    lambda r=row: self._collect_one(r, session, dry_run=dry_run, run=run),
                )
            except Exception as exc:
                run.failed(root, repr(exc))
                continue
            written += rows
            await pace(self._policy)
        return written

    async def _collect_one(
        self, row: dict[str, Any], session: date, *, dry_run: bool, run: CollectorRun
    ) -> int:
        root = str(row["root"])
        ladder_by_root = await self._market_data.futures_ladder([root])
        await pace(self._policy)
        ladder = (ladder_by_root.get(root) or [])[: self._max_expiries]
        if not ladder:
            raise ValueError(
                f"{root} returned no futures ladder. Futures need the NATIVE exchange; "
                "SMART returns empty."
            )

        quotes = await self._market_data.quotes([contract.conid for contract in ladder])
        await pace(self._policy)
        points = self._build_curve(root, ladder, quotes, row)

        if not dry_run:
            self._write(points, session)

        priced = [p for p in points if p.price is not None]
        # A ladder with no prices on it is not a curve. The expiry list is
        # retrievable at any time; the PRICES are the part that has to be taken
        # today, and roll yield — the only thing this collector exists to
        # measure — cannot be computed from none of them. Reporting `ok` here
        # would put a healthy-looking line in the log on a night when nothing of
        # value was collected, which is the failure this package keeps fixing.
        #
        # Observed live 2026-08-24: COMEX gold snapshots return {} outright on
        # this account while its daily bars serve at delayed: 600, so this is an
        # entitlement gap rather than a transient one. See docs/archive/SESSION-7-LIVE.md.
        run.succeeded(
            root,
            len(points),
            degraded=not priced,
            reason=(
                None
                if priced
                else (
                    "the ladder listed but not one rung carried a price, so there is no "
                    "curve shape and no roll yield. Futures need the NATIVE exchange, and "
                    "a snapshot returning nothing at all points at a market-data "
                    "entitlement rather than a bad request."
                )
            ),
            expiries=len(points),
            priced=len(priced),
            front_roll=points[1].annualised_roll if len(points) > 1 else None,
            shape=_describe_shape(points),
        )
        return len(points)

    def _build_curve(
        self,
        root: str,
        ladder: list[FuturesContract],
        quotes: dict[int, Quote],
        row: dict[str, Any],
    ) -> list[CurvePoint]:
        prices = {
            contract.conid: _reference_price(quotes.get(contract.conid)) for contract in ladder
        }
        front = ladder[0]
        front_price = prices.get(front.conid)
        front_expiry = parse_date(front.expiry)

        if len(ladder) < self._min_expiries_for_roll:
            log.info(
                "root lists too few expiries to have a curve shape",
                extra={"root": root, "expiries": len(ladder)},
            )

        points: list[CurvePoint] = []
        for index, contract in enumerate(ladder):
            price = prices.get(contract.conid)
            roll: float | None = None
            expiry = parse_date(contract.expiry)
            # index 0 is the front: nothing to roll it against, so NULL, not 0.0.
            if (
                index > 0
                and front_price is not None
                and price is not None
                and expiry is not None
                and front_expiry is not None
            ):
                roll = annualised_roll(
                    front_price=front_price,
                    deferred_price=price,
                    days_apart=(expiry - front_expiry).days,
                    calendar_days_per_year=self._calendar_days_per_year,
                )
            points.append(
                CurvePoint(
                    root=root,
                    expiry=contract.expiry,
                    price=price,
                    annualised_roll=roll,
                    conid=contract.conid,
                    exchange=contract.exchange or row.get("exchange"),
                    currency=contract.currency or row.get("currency"),
                    market_data_status=_status_of(quotes.get(contract.conid)),
                )
            )
        return points

    def _write(self, points: list[CurvePoint], session: date) -> None:
        """Append-only across days, idempotent within one."""
        if not points:
            return
        stamp = utc_stamp()
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO curve_snapshots (
                    snapshot_date, root, expiry, price, annualised_roll, contract_id,
                    exchange, currency, source, collected_at, market_data_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (snapshot_date, root, expiry) DO UPDATE SET
                    price = excluded.price,
                    annualised_roll = excluded.annualised_roll,
                    contract_id = excluded.contract_id,
                    exchange = excluded.exchange,
                    currency = excluded.currency,
                    source = excluded.source,
                    collected_at = excluded.collected_at,
                    market_data_status = excluded.market_data_status
                """,
                [
                    (
                        session.isoformat(),
                        point.root,
                        point.expiry,
                        point.price,
                        point.annualised_roll,
                        point.conid,
                        point.exchange,
                        point.currency,
                        self._market_data.source,
                        stamp,
                        point.market_data_status,
                    )
                    for point in points
                ],
            )


def _reference_price(quote: Quote | None) -> float | None:
    """Mid where both sides are quoted, else last. Never a fabricated price."""
    return quote.reference_price if quote is not None else None


def _status_of(quote: Quote | None) -> str | None:
    """The market-data state this rung's quote carried, if it carried one."""
    return quote.market_data_status if quote is not None else None


def _describe_shape(points: list[CurvePoint]) -> str:
    """A one-word note for the run log. Classification proper is the engine's job."""
    rolls = [p.annualised_roll for p in points if p.annualised_roll is not None]
    if not rolls:
        return "unpriced"
    average = sum(rolls) / len(rolls)
    if average > 0:
        return "backwardation"
    if average < 0:
        return "contango"
    return "flat"
