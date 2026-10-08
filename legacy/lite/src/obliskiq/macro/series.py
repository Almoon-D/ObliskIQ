"""Declared macro series, and the observation shape every fetcher returns.

Every series the collectors pull is declared under ``macro.series`` in
config.yaml with its **native** frequency. Nothing is upsampled: a monthly CPI
print interpolated onto a daily grid is a fabrication that then propagates into
every percentile derived from it.

The two dates
-------------

``reference_date`` is the period an observation describes. ``release_date`` is
when it became public. Downstream aligns on **release**, because aligning a CPI
print to its reference month means every percentile computed from it knows the
number weeks before the market did.

``release_date`` is therefore never silently set equal to ``reference_date``. Each
source declares a ``release_date_policy``:

``from_api``            the source states its first-publication date (FRED/ALFRED).
``reference_plus_lag``  a published, fixed schedule (CFTC Friday, EIA Wednesday).
``same_as_reference``   legitimate only for a series released on its own reference
                        date — a daily market observation, and nothing else.
``unknown``             ``release_date`` stays NULL rather than being invented.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..config import Config
from ..errors import ConfigError
from ..store import Store

#: Legal values of ``macro.sources.<source>.release_date_policy``.
POLICY_FROM_API = "from_api"
POLICY_REFERENCE_PLUS_LAG = "reference_plus_lag"
POLICY_SAME_AS_REFERENCE = "same_as_reference"
POLICY_UNKNOWN = "unknown"

RELEASE_DATE_POLICIES = (
    POLICY_FROM_API,
    POLICY_REFERENCE_PLUS_LAG,
    POLICY_SAME_AS_REFERENCE,
    POLICY_UNKNOWN,
)

#: Native frequencies a series may declare. Never converted between.
FREQUENCIES = ("daily", "weekly", "monthly", "quarterly", "annual")

#: Which vintage of a FRED series supplies the VALUE. The release date always
#: comes from the initial release; this says nothing about it.
#:
#: ``initial`` is the default and is what a point-in-time store wants: the number
#: as first printed. ``latest`` exists for one reason, and it is a property of the
#: series rather than a preference — an index the publisher REBASES. Eurostat
#: rebased its HICP to 2025=100 during 2026, so ALFRED's initial-release column
#: holds 132.8 for 2025-12 and 100.6 for 2026-01. A year-on-year rate taken
#: across that boundary is not a revision artefact, it is a division of one unit
#: by another, and it reads as a 22% deflation that nothing downstream can catch.
#: The latest vintage is one base throughout, which is the only shape from which a
#: ratio of two periods means anything.
VALUE_VINTAGE_INITIAL = "initial"
VALUE_VINTAGE_LATEST = "latest"
VALUE_VINTAGES = (VALUE_VINTAGE_INITIAL, VALUE_VINTAGE_LATEST)

#: The group whose window comes from commodities.history rather than
#: macro.collection, because a 5-year percentile is misleading on a 10-20 year cycle.
GROUP_COMMODITY_HISTORY = "commodity_history"


@dataclass(frozen=True)
class SeriesSpec:
    """One declared series."""

    id: str
    source: str
    frequency: str
    group: str
    #: Source-specific addressing: an ECB flow/key, a CFTC market code, an EIA
    #: route. Absent for FRED, where the id is the address.
    key: str | None = None
    #: Per-series release policy, overriding the source's. A source setting has
    #: to cover everything that source serves, and that is too coarse where one
    #: source serves both a daily market observation and a slower statistic:
    #: ECB publishes its FX reference rates on the day they describe, while its
    #: other series carry no first-publication date at all. Absent means the
    #: source's own policy applies.
    release_date_policy: str | None = None
    release_lag_days: int | None = None
    #: Which vintage supplies the value; see :data:`VALUE_VINTAGES`. FRED only —
    #: no other source here serves more than one vintage of an observation.
    value_vintage: str = VALUE_VINTAGE_INITIAL

    @property
    def is_commodity_history(self) -> bool:
        return self.group == GROUP_COMMODITY_HISTORY


@dataclass(frozen=True)
class SourcePolicy:
    """How one source is treated."""

    name: str
    enabled: bool
    release_date_policy: str
    release_lag_days: int

    def for_series(self, spec: SeriesSpec) -> SourcePolicy:
        """This policy, or the series' own override where it declares one.

        Resolved here rather than in each fetcher so that every source goes
        through one rule and a fetcher still receives a plain ``SourcePolicy``.
        """
        if spec.release_date_policy is None and spec.release_lag_days is None:
            return self
        return SourcePolicy(
            name=self.name,
            enabled=self.enabled,
            release_date_policy=spec.release_date_policy or self.release_date_policy,
            release_lag_days=(
                self.release_lag_days if spec.release_lag_days is None else spec.release_lag_days
            ),
        )

    def release_date_for(self, reference: date, *, from_api: date | None = None) -> str | None:
        """The release date to store, or ``None`` when it is genuinely unknown.

        Returning ``None`` is the point. A NULL release date is a coverage gap
        downstream code can see; a reference date wearing a release date's name is
        look-ahead bias nobody can detect afterwards.
        """
        if self.release_date_policy == POLICY_FROM_API:
            return from_api.isoformat() if from_api else None
        if self.release_date_policy == POLICY_REFERENCE_PLUS_LAG:
            return (reference + timedelta(days=self.release_lag_days)).isoformat()
        if self.release_date_policy == POLICY_SAME_AS_REFERENCE:
            return reference.isoformat()
        return None


@dataclass(frozen=True)
class Observation:
    """One value, as every fetcher returns it."""

    series_id: str
    source: str
    reference_date: str
    release_date: str | None
    value: float | None
    frequency: str
    units: str | None = None


def load_series(config: Config) -> list[SeriesSpec]:
    """Every declared series, validated. A malformed entry raises rather than being skipped."""
    out: list[SeriesSpec] = []
    for index, row in enumerate(config.require_list("macro.series")):
        if not isinstance(row, dict):
            raise ConfigError(f"macro.series[{index}] is not a mapping")
        for field in ("id", "source", "frequency", "group"):
            if not row.get(field):
                raise ConfigError(f"macro.series[{index}] is missing {field!r}")
        frequency = str(row["frequency"])
        if frequency not in FREQUENCIES:
            raise ConfigError(
                f"macro.series[{index}] ({row['id']}) declares frequency {frequency!r}; "
                f"it must be one of {list(FREQUENCIES)}. The frequency recorded is the "
                "source's own — nothing here is ever resampled."
            )
        override = row.get("release_date_policy")
        if override is not None and str(override) not in RELEASE_DATE_POLICIES:
            raise ConfigError(
                f"macro.series[{index}] ({row['id']}) declares release_date_policy "
                f"{override!r}; it must be one of {list(RELEASE_DATE_POLICIES)}."
            )
        lag = row.get("release_lag_days")
        vintage = str(row.get("value_vintage", VALUE_VINTAGE_INITIAL))
        if vintage not in VALUE_VINTAGES:
            raise ConfigError(
                f"macro.series[{index}] ({row['id']}) declares value_vintage "
                f"{vintage!r}; it must be one of {list(VALUE_VINTAGES)}."
            )
        out.append(
            SeriesSpec(
                id=str(row["id"]),
                source=str(row["source"]).lower(),
                frequency=frequency,
                group=str(row["group"]),
                key=str(row["key"]) if row.get("key") else None,
                release_date_policy=str(override) if override is not None else None,
                release_lag_days=int(lag) if lag is not None else None,
                value_vintage=vintage,
            )
        )
    if not out:
        raise ConfigError("macro.series is empty; there is nothing to collect")
    return out


def load_source_policies(config: Config) -> dict[str, SourcePolicy]:
    policies: dict[str, SourcePolicy] = {}
    sources = config.section("macro.sources")
    for name in sources.as_dict():
        cfg = sources.section(name)
        policy = cfg.require_choice("release_date_policy", RELEASE_DATE_POLICIES)
        policies[name] = SourcePolicy(
            name=name,
            enabled=cfg.require_bool("enabled"),
            release_date_policy=str(policy),
            release_lag_days=cfg.require_int("release_lag_days"),
        )
    return policies


# ── the risk-free rate, as READ from the store ──
#
# These live here, in the network-free module, rather than beside the fetcher in
# rates.py. The compute unit runs with PrivateNetwork=true and
# tests/test_compute_isolation.py enforces that nothing in its import graph even
# NAMES an HTTP client — so a compute-layer caller needing a stored rate cannot
# import the module that also holds the httpx-based provider. Splitting on "reads
# the store" against "fetches over the network" is what keeps both honest, and
# rates.py imports these back so there is still ONE definition of each.

#: The route these rows were collected by. FRED serves every configured
#: risk-free series, the ECB ones included, through its own mirrors.
SOURCE = "fred"

#: FRED publishes rate series in percent. Converted once, here.
PERCENT_TO_DECIMAL = 100.0


@dataclass(frozen=True)
class RiskFreeRate:
    """A risk-free rate and its provenance.

    ``rate`` is a decimal (0.0325 is 3.25%), matching the convention in
    :mod:`obliskiq.quant.bsm`. FRED publishes percent; the conversion happens here,
    once, rather than at each call site.
    """

    currency: str
    series_id: str
    rate: float
    reference_date: str
    fetched_at: str
    stale: bool

    @property
    def percent(self) -> float:
        return self.rate * PERCENT_TO_DECIMAL


def series_for_currency(config: Config, currency: str | None = None) -> tuple[str, str]:
    """The currency actually used, and the series that carries its rate."""
    ccy = (currency or config.require_str("account.base_currency")).upper()
    series = config.section("macro.risk_free_rate").get("series_by_currency", {}).get(ccy)
    if series is None:
        raise ConfigError(
            f"no risk-free-rate series configured for {ccy!r}. Add it under "
            "macro.risk_free_rate.series_by_currency — the rate is never guessed."
        )
    return ccy, str(series)


def cached_risk_free_rate(
    store: Store, config: Config, currency: str | None = None
) -> RiskFreeRate | None:
    """The last rate the collectors wrote, without touching the network.

    The MCP server and the computation layer both need a discount rate and
    neither may make an HTTP request — the compute unit runs under
    ``PrivateNetwork=true``. This reads what ``macro_pull`` already stored and
    returns ``None`` when nothing has been collected, so a caller reports the
    position as unmeasurable rather than discounting at a number nobody chose.
    """
    ccy, series_id = series_for_currency(config, currency)
    row = store.execute(
        "SELECT value, reference_date, collected_at FROM macro "
        "WHERE series_id = ? AND source = ? ORDER BY reference_date DESC LIMIT 1",
        (series_id, SOURCE),
    ).fetchone()
    if row is None or row["value"] is None:
        return None
    units = config.require_str("macro.risk_free_rate.units")
    value = float(row["value"])
    return RiskFreeRate(
        currency=ccy,
        series_id=series_id,
        rate=value / PERCENT_TO_DECIMAL if units == "percent" else value,
        reference_date=str(row["reference_date"]),
        fetched_at=str(row["collected_at"]),
        # Read from the store rather than fetched, so it is by definition as old
        # as the last collector run. The caller is told, not reassured.
        stale=True,
    )


def risk_free_rate_as_of(
    store: Store, config: Config, as_of: date, currency: str | None = None
) -> RiskFreeRate | None:
    """The rate as it was KNOWN on ``as_of``, not the latest one in the store.

    :func:`cached_risk_free_rate` answers "what is the rate now" and is right for
    a position being assessed today. It is wrong for anything that reprices a PAST
    session, because it reads ``ORDER BY reference_date DESC`` with no upper bound:
    solving session T through it discounts T at a rate published after T, which is
    a model reading its own future.

    Bounded on BOTH dates, and they are different questions. ``reference_date`` is
    the day the rate applies to; ``release_date`` is the day it became public. A
    figure for the 1st published on the 5th did not exist on the 2nd, so a
    backtest of the 2nd may not use it — this is the same vintage discipline the
    macro collector already stores ``release_date`` for.

    ``None`` when no rate was knowable on that date. Callers must FAIL the row
    rather than substitute one: ``macro.risk_free_rate.on_failure`` is ``error``
    and explicitly never a literal fallback, and a solved implied vol carries the
    rate straight into the answer.
    """
    ccy, series_id = series_for_currency(config, currency)
    stamp = as_of.isoformat()
    row = store.execute(
        "SELECT value, reference_date, collected_at FROM macro "
        "WHERE series_id = ? AND source = ? AND value IS NOT NULL "
        "AND reference_date <= ? "
        "AND (release_date IS NULL OR release_date <= ?) "
        "ORDER BY reference_date DESC LIMIT 1",
        (series_id, SOURCE, stamp, stamp),
    ).fetchone()
    if row is None or row["value"] is None:
        return None
    units = config.require_str("macro.risk_free_rate.units")
    value = float(row["value"])
    return RiskFreeRate(
        currency=ccy,
        series_id=series_id,
        rate=value / PERCENT_TO_DECIMAL if units == "percent" else value,
        reference_date=str(row["reference_date"]),
        fetched_at=str(row["collected_at"]),
        # Point-in-time by construction rather than merely old. The caller is
        # being told this is the vintage for `as_of`, not that it is current.
        stale=True,
    )
