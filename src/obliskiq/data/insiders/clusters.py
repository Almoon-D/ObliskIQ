"""The inputs of an insider-purchase cluster, point in time on the filing date.

Network-free. Over the filings of one issuer dated in the trailing window that ends on ``as_of``
(a filing is public on its filing date, up to two business days after the trade): the
open-market purchases (code ``P``, acquired, not derivative) of owners who are officers or
directors, their count, the distinct buyers and their roles, the shares and the value. Lines
whose price the filer left to a footnote are counted, not valued. Calling a cluster is the rule of
H2 and of the Hunter's component (Cohen, Malloy and Pomorski 2012), not this function's.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from datetime import date, timedelta

from ...domain.sources import ClusterInputs, InsiderFiling


def cluster_inputs(
    filings: Iterable[InsiderFiling],
    *,
    issuer_cik: int,
    as_of: date,
    window_days: int,
    purchase_code: str,
) -> ClusterInputs:
    """The cluster inputs of ``issuer_cik`` known on ``as_of``."""
    first_day = as_of - timedelta(days=window_days - 1)
    counted: list[date] = []
    buyers: set[int] = set()
    officers: set[int] = set()
    directors: set[int] = set()
    shares = value = 0.0
    unpriced = 0
    for filing in filings:
        if filing.issuer_cik != issuer_cik or not first_day <= filing.filed_on <= as_of:
            continue
        insiders = [o for o in filing.owners if o.is_officer or o.is_director]
        purchases = [
            t
            for t in filing.transactions
            if not t.derivative and t.code == purchase_code and t.acquired
        ]
        if not insiders or not purchases:
            continue
        counted.append(filing.filed_on)
        buyers.update(o.owner_cik for o in insiders)
        officers.update(o.owner_cik for o in insiders if o.is_officer)
        directors.update(o.owner_cik for o in insiders if o.is_director)
        for line in purchases:
            shares += line.shares or 0.0
            if line.shares is None or line.price is None:
                unpriced += 1
            else:
                value += line.shares * line.price
    return ClusterInputs(
        issuer_cik=issuer_cik,
        as_of=as_of,
        window_days=window_days,
        filings=len(counted),
        buyers=len(buyers),
        officers=len(officers),
        directors=len(directors),
        shares=shares,
        value=value,
        unpriced_lines=unpriced,
        first_filed=min(counted, default=None),
        last_filed=max(counted, default=None),
    )
