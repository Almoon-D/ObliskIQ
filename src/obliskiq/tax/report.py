"""The annual tax report: a CSV of the year's realised results and the losses still deferred, for
the owner's return (MASTER_PLAN §5.16). Not tax advice: an estimate under the profile's rules.

One row per realisation (a security's or a currency's lot sold in the year), then one row per lot
that carries a deferred loss and per loss still waiting for a homogeneous purchase, then the
year's summary (settled, or provisional while the year is open). Amounts are in the base currency.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import csv
import io
from collections.abc import Sequence
from datetime import date, timedelta

from ..domain.tax import OpenLossWindow, Realisation, TaxLot, TaxYearSummary

COLUMNS = (
    "section",
    "security",
    "lot_id",
    "acquired_on",
    "disposed_on",
    "quantity",
    "cost",
    "proceeds",
    "gain",
    "released",
    "deferred",
    "counted",
    "note",
)


def annual_csv(
    tax_year: int,
    *,
    realisations: Sequence[Realisation],
    lots: Sequence[TaxLot],
    windows: Sequence[OpenLossWindow],
    summary: TaxYearSummary,
    settled: bool,
    identities: dict[str, str],
) -> str:
    """The report as CSV text; ``identities`` names each security key (an ISIN, a currency)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)

    def name(key: str) -> str:
        known = identities.get(key)
        return key if known is None else f"{known} ({key})"

    for r in realisations:
        if r.disposed_on.year != tax_year:
            continue
        writer.writerow(
            (
                "realised",
                name(r.security),
                r.lot_id,
                r.acquired_on.isoformat(),
                r.disposed_on.isoformat(),
                r.quantity,
                r.cost,
                r.proceeds,
                r.gain,
                r.released,
                r.deferred,
                r.counted,
                "",
            )
        )
    for lot in lots:
        if lot.deferred_loss > 0:
            writer.writerow(
                (
                    "deferred_on_lot",
                    name(lot.security),
                    lot.lot_id,
                    lot.acquired_on.isoformat(),
                    "",
                    lot.quantity,
                    lot.cost,
                    "",
                    "",
                    "",
                    lot.deferred_loss,
                    "",
                    "a loss deferred by the wash-sale rule, released as these units are sold",
                )
            )
    for window in windows:
        writer.writerow(
            (
                "deferred_pending",
                name(window.security),
                "",
                "",
                window.sold_on.isoformat(),
                window.units,
                "",
                "",
                "",
                "",
                window.units * window.loss_per_unit,
                "",
                "a loss deferred if homogeneous units are bought by "
                f"{window.closes_on.isoformat()}",
            )
        )
    state = "settled" if settled else "provisional"
    covered = "" if summary.rate_table_covers_year else "; the rate table does not cover the year"
    writer.writerow(
        (
            "summary",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            summary.gains,
            "",
            "",
            summary.base,
            f"{state}: income {summary.income}, gains {summary.gains}, tax {summary.tax_due}, "
            f"carried out {sum(c.amount for c in summary.carried_out)}, paid "
            f"{summary.payment_date.isoformat()}{covered}; not tax advice",
        )
    )
    return out.getvalue()


def year_bounds(tax_year: int) -> tuple[date, date]:
    """The first and last day of ``tax_year`` (a calendar year)."""
    return date(tax_year, 1, 1), date(tax_year + 1, 1, 1) - timedelta(days=1)
