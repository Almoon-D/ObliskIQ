"""One computed leaf before it is stored, and the rules that make it honest.

A leaf is computed, or it is ``n/a`` with a reason naming the missing input (which series, how
many observations, how many were needed), or it is ``inapplicable`` because a declaration says
no collector could ever fill it. Never a silent proxy, never an estimate presented as data.
:class:`Cell` is lite's ``indicators.Cell``; :func:`to_measurement` adds what the store keeps
beside it: the entity, the inputs read and the hash of their fingerprint.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime

from ..domain.enums import MarketDataStatus
from ..domain.measurement import EntityKind, Measurement, MeasurementStatus
from .vocabulary import INAPPLICABLE_CODES, Reason, Units


@dataclass(frozen=True, slots=True)
class Cell:
    """One leaf: its value and status, the window and sample behind it, and why if absent.

    ``window`` names the span and the sample actually behind the value, and is the join key of a
    leaf's history: compare two sessions only where it matches. ``n_obs`` is the count behind the
    value, never the window asked for; ``None`` where the leaf carries no sample.
    """

    leaf: str
    family: str
    value: float | None = None
    status: MeasurementStatus = MeasurementStatus.OK
    reason: str | None = None
    window: str | None = None
    units: Units | None = None
    reason_code: Reason | None = None
    n_obs: int | None = None
    market_data_status: MarketDataStatus | None = None

    @classmethod
    def measured(
        cls,
        leaf: str,
        family: str,
        value: float,
        *,
        units: Units,
        window: str | None = None,
        n_obs: int | None = None,
        reason: str | None = None,
        market_data_status: MarketDataStatus | None = None,
    ) -> Cell:
        return cls(
            leaf=leaf,
            family=family,
            value=value,
            reason=reason,
            window=window,
            units=units,
            n_obs=n_obs,
            market_data_status=market_data_status,
        )

    @classmethod
    def absent(
        cls, leaf: str, family: str, reason: str, *, code: Reason, window: str | None = None
    ) -> Cell:
        """Collectible and not collected, or refused on too little: it stays in the coverage
        denominator, because something is supposed to arrive."""
        if code in INAPPLICABLE_CODES:
            raise ValueError(
                f"{code.value} is a structural code; a leaf no collector could fill is "
                "inapplicable, not absent"
            )
        return cls(
            leaf=leaf,
            family=family,
            status=MeasurementStatus.NA,
            reason=reason,
            window=window,
            reason_code=code,
        )

    @classmethod
    def inapplicable(
        cls, leaf: str, family: str, reason: str, *, code: Reason, window: str | None = None
    ) -> Cell:
        """A leaf no collector could ever fill, as a declaration says. Left out of the coverage
        denominator and reported as the ambition gap instead."""
        if code not in INAPPLICABLE_CODES:
            raise ValueError(
                f"{code.value} is not a structural reason code: an inapplicable leaf claims no "
                "collector could ever fill it, which is a claim about a declaration"
            )
        return cls(
            leaf=leaf,
            family=family,
            status=MeasurementStatus.INAPPLICABLE,
            reason=reason,
            window=window,
            reason_code=code,
        )

    @classmethod
    def refused(
        cls, leaf: str, family: str, reason: str, *, code: Reason, window: str | None = None
    ) -> Cell:
        """``inapplicable`` for a structural code, ``n/a`` for any other, decided by the code."""
        build = cls.inapplicable if code in INAPPLICABLE_CODES else cls.absent
        return build(leaf, family, reason, code=code, window=window)

    def stale(self, reason: str) -> Cell:
        """Downgrade a measured leaf whose newest input is older than its window."""
        if self.status is not MeasurementStatus.OK:
            return self
        return replace(
            self, status=MeasurementStatus.STALE, reason=reason, reason_code=Reason.STALE
        )

    def labelled(self, status: MarketDataStatus | None) -> Cell:
        """Fill in the quote state behind the value, unless the family set the one it read."""
        if status is None or self.market_data_status is not None:
            return self
        return replace(self, market_data_status=status)

    def finite(self) -> Cell:
        """A non-finite value never reaches the store as a measurement: the backstop for a
        family that did not guard its own arithmetic."""
        if self.value is None or math.isfinite(self.value):
            return self
        return replace(
            self,
            value=None,
            status=MeasurementStatus.NA,
            reason=(
                f"{self.leaf} computed {self.value!r}, which is not a finite number; reported "
                "absent rather than as a measurement nobody could defend"
            ),
            reason_code=Reason.NON_FINITE,
        )


def inputs_hash(fingerprint: Mapping[str, object]) -> str:
    """The SHA-256 of an input fingerprint (sorted-key JSON; dates as ISO text)."""
    text = json.dumps(fingerprint, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def to_measurement(
    cell: Cell,
    *,
    entity_kind: EntityKind,
    entity_id: str,
    as_of: date,
    source: str | None,
    fingerprint: Mapping[str, object],
    computed_at: datetime,
) -> Measurement:
    """The stored row of ``cell``. The fingerprint names the inputs read and their extent (which
    rows, up to when, how many), so a rebuild on the same store reproduces the hash."""
    leaf = cell.finite()
    return Measurement(
        entity_kind=entity_kind,
        entity_id=entity_id,
        indicator=leaf.family,
        leaf=leaf.leaf,
        as_of=as_of,
        value=leaf.value,
        status=leaf.status,
        reason=leaf.reason,
        reason_code=None if leaf.reason_code is None else leaf.reason_code.value,
        n_obs=leaf.n_obs,
        window=leaf.window,
        units=None if leaf.units is None else leaf.units.value,
        source=source,
        market_data_status=leaf.market_data_status,
        inputs_hash=inputs_hash({"leaf": leaf.leaf, **fingerprint}),
        computed_at=computed_at,
    )
