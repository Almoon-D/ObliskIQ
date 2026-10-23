"""An offline fixture store for the measurement layer: deterministic synthetic market data written
through the store's own repositories, for the shipped universe's instruments.

Nothing here is a capture of a real market: bars are a seeded random walk with coherent OHLC,
the option chain is priced through the system's own BSM on a skewed smile, and the macro series,
CFTC reports and curves are synthetic with explicit release dates. What the store holds is
therefore known exactly, which is what lets a test say which leaf must be measured, absent or
inapplicable and why.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import numpy as np

from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import AssetClass, InstrumentRole, MarketDataStatus, OptionRight
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.macro import MacroObservation
from obliskiq.domain.market import Bar, ChainRow, CurvePoint, Snapshot
from obliskiq.domain.sources import CotReport, EarningsDate
from obliskiq.domain.universe import Holding, HoldingsFile
from obliskiq.quant.bsm import BsmInputs, bsm_greeks, bsm_price
from obliskiq.store import repos
from obliskiq.store import sources as sources_repo
from obliskiq.store import universe as universe_repo
from obliskiq.store.db import Store

#: The session every build is made for: a Thursday.
AS_OF = date(2026, 10, 22)
STAMP = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)
CREATED = datetime(2024, 1, 2, tzinfo=UTC)
SOURCE = "fixture"

#: Shipped universe instruments the rig seeds: conid, symbol, venue code, currency, class, roles.
SPX = 416904
QQQ = 320227571
XLE = 4215217
DAX = 825711
XIU = 74580921
IWDA = 78999785
SEEDED: dict[int, tuple[str, str, str, AssetClass]] = {
    SPX: ("SPX", "CBOE", "USD", AssetClass.INDEX),
    QQQ: ("QQQ", "NASDAQ", "USD", AssetClass.ETF),
    XLE: ("XLE", "ARCA", "USD", AssetClass.ETF),
    DAX: ("DAX", "EUREX", "EUR", AssetClass.INDEX),
    XIU: ("XIU", "TSE", "CAD", AssetClass.ETF),
    IWDA: ("IWDA", "LSEETF", "USD", AssetClass.ETF),
}
#: Business days of history (about two and a half years).
SESSIONS = 640


def business_days(end: date, count: int) -> list[date]:
    """``count`` weekdays ending on ``end``, oldest first (holidays are not modelled)."""
    days: list[date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def walk(seed: int, count: int, *, start: float, vol: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0, vol / np.sqrt(252), count)
    return start * np.exp(np.cumsum(returns))


def make_bars(
    conid: int,
    days: list[date],
    *,
    seed: int,
    start: float = 100.0,
    vol: float = 0.2,
    volume: bool = True,
    restate_opens: bool = False,
) -> list[Bar]:
    """Coherent OHLC bars: each open gaps off the previous close, the range brackets both."""
    rng = np.random.default_rng(seed + 1)
    closes = walk(seed, len(days), start=start, vol=vol)
    bars: list[Bar] = []
    previous = start
    for index, day in enumerate(days):
        close = float(closes[index])
        opened = previous if restate_opens else previous * float(np.exp(rng.normal(0, 0.004)))
        spread = abs(float(rng.normal(0, 0.006))) + 0.002
        high = max(opened, close) * (1 + spread)
        low = min(opened, close) * (1 - spread)
        bars.append(
            Bar(
                conid=conid,
                session_date=day,
                open=opened,
                high=high,
                low=low,
                close=close,
                volume=float(1_000_000 + 1000 * index) if volume else None,
                source=SOURCE,
            )
        )
        previous = close
    return bars


def instrument(conid: int) -> Instrument:
    symbol, exchange, currency, asset_class = SEEDED[conid]
    return Instrument(
        conid=conid,
        symbol=symbol,
        listing_exchange=exchange,
        currency=currency,
        asset_class=asset_class,
        domicile=None,
        ucits=None,
        kid_status=None,
        share_class=None,
        lot_size=None,
        option_exchange=None,
        history_exchange=None,
        roles=frozenset({InstrumentRole.DATA}),
        superseded_by=None,
        created_at=CREATED,
        superseded_at=None,
    )


def observation(
    series_id: str,
    source: str,
    reference: date,
    value: float,
    *,
    release: date | None,
    frequency: str,
    units: str,
) -> MacroObservation:
    return MacroObservation(
        series_id=series_id,
        source=source,
        reference_date=reference,
        release_date=release,
        value=value,
        frequency=frequency,
        units=units,
        collected_at=STAMP,
    )


def daily_series(
    series_id: str,
    source: str,
    days: list[date],
    *,
    seed: int,
    start: float,
    vol: float,
    units: str,
    lag_days: int = 0,
    level: bool = False,
) -> list[MacroObservation]:
    """A daily series: a random walk in level (``level``) or in log (a price-like rate)."""
    rng = np.random.default_rng(seed)
    values = (
        start + np.cumsum(rng.normal(0, vol, len(days)))
        if level
        else walk(seed, len(days), start=start, vol=vol)
    )
    return [
        observation(
            series_id,
            source,
            day,
            float(value),
            release=day + timedelta(days=lag_days),
            frequency="daily",
            units=units,
        )
        for day, value in zip(days, values, strict=True)
        if day + timedelta(days=lag_days) <= AS_OF
    ]


def monthly_series(
    series_id: str,
    source: str,
    *,
    months: int,
    start: float,
    step: float,
    units: str,
    lag_days: int,
) -> list[MacroObservation]:
    """A monthly series referenced on the first of each month, released ``lag_days`` later."""
    out: list[MacroObservation] = []
    year, month = AS_OF.year, AS_OF.month
    references: list[date] = []
    for _ in range(months):
        references.append(date(year, month, 1))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    for index, reference in enumerate(reversed(references)):
        release = reference + timedelta(days=lag_days)
        if release > AS_OF:
            continue
        out.append(
            observation(
                series_id,
                source,
                reference,
                start + step * index,
                release=release,
                frequency="monthly",
                units=units,
            )
        )
    return out


def chain(
    underlying: int,
    spot: float,
    *,
    session: date,
    expiries: list[date],
    bsm: BsmInputs,
    with_iv: bool = True,
    rate: float = 0.04,
) -> list[ChainRow]:
    """A two-sided chain priced through BSM on a put-skewed smile: out-of-the-money puts above
    the ATM vol, calls below, with deltas from the same model."""
    rows: list[ChainRow] = []
    strikes = [round(spot * (0.70 + 0.025 * i), 2) for i in range(25)]
    for expiry in expiries:
        dte = (expiry - session).days
        for right in (OptionRight.CALL, OptionRight.PUT):
            is_call = right is OptionRight.CALL
            for strike in strikes:
                moneyness = strike / spot - 1.0
                sigma = 0.22 - 0.25 * moneyness
                price = float(bsm_price(spot, strike, dte, sigma, rate, 0.0, is_call, bsm))
                delta = float(bsm_greeks(spot, strike, dte, sigma, rate, 0.0, is_call, bsm).delta)
                if price <= 0.05:
                    continue
                rows.append(
                    ChainRow(
                        snapshot_date=session,
                        underlying_conid=underlying,
                        expiry=expiry,
                        strike=strike,
                        right=right,
                        option_conid=None,
                        option_symbol=None,
                        option_exchange="SMART",
                        bid=price * 0.99,
                        ask=price * 1.01,
                        last=price,
                        iv=sigma if with_iv else None,
                        delta=delta if with_iv else None,
                        open_interest=5000,
                        volume=100,
                        sigma_used=None,
                        sigma_source=None,
                        source=SOURCE,
                        collected_at=STAMP,
                        market_data_status=MarketDataStatus.DELAYED,
                        market_data_raw="DELAYED",
                        quote_delay_seconds=900,
                        multiplier=100.0,
                    )
                )
    return rows


def cot_reports(code: str, *, weeks: int) -> list[CotReport]:
    """Weekly reports on Tuesdays, released the Friday after."""
    tuesday = AS_OF - timedelta(days=(AS_OF.weekday() - 1) % 7)
    out: list[CotReport] = []
    for index in range(weeks):
        report = tuesday - timedelta(weeks=weeks - 1 - index)
        release = report + timedelta(days=3)
        if release > AS_OF:
            continue
        out.append(
            CotReport(
                contract_code=code,
                report_date=report,
                release_date=release,
                market_name="fixture",
                open_interest=1_000_000,
                noncomm_long=300_000 + 1000 * index,
                noncomm_short=200_000,
                comm_long=None,
                comm_short=None,
                nonrept_long=None,
                nonrept_short=None,
                source="cftc_socrata",
                collected_at=STAMP,
            )
        )
    return out


@dataclass
class MeasureRig:
    """What a seeded store holds, for assertions."""

    days: list[date]
    bars: dict[int, list[Bar]] = field(default_factory=dict)


def seed(store: Store, settings: ObliskConfig) -> MeasureRig:
    """Seed ``store`` with the rig's instruments, bars, snapshots, chain, macro, COT and curves."""
    days = business_days(AS_OF, SESSIONS)
    rig = MeasureRig(days=days)
    bsm = BsmInputs.from_config(settings)
    for offset, conid in enumerate(sorted(SEEDED)):
        repos.upsert_instrument(store, instrument(conid))
        index = SEEDED[conid][3] is AssetClass.INDEX
        bars = make_bars(conid, days, seed=offset + 7, start=100.0 + 10 * offset, volume=not index)
        repos.upsert_bars(store, bars)
        rig.bars[conid] = bars
    # QQQ: a year and more of snapshots carrying an implied vol, and today's chain.
    qqq = rig.bars[QQQ]
    repos.upsert_snapshots(
        store,
        [
            Snapshot(
                snapshot_date=bar.session_date,
                conid=QQQ,
                last=bar.close,
                bid=bar.close * 0.999,
                ask=bar.close * 1.001,
                volume=1000,
                implied_vol=0.18 + 0.02 * np.sin(index / 20.0),
                implied_vol_source=SOURCE,
                historical_vol_30d=None,
                dividend_yield=None,
                dividend_yield_basis=None,
                implied_vol_solved=None,
                implied_vol_solved_source=None,
                source=SOURCE,
                collected_at=STAMP,
                market_data_status=MarketDataStatus.DELAYED,
                market_data_raw="DELAYED",
                quote_delay_seconds=900,
            )
            for index, bar in enumerate(qqq[-300:])
        ],
    )
    repos.upsert_chain_rows(
        store,
        chain(
            QQQ,
            qqq[-1].close,
            session=AS_OF,
            expiries=[AS_OF + timedelta(days=95), AS_OF + timedelta(days=180)],
            bsm=bsm,
        ),
    )
    # XIU: a chain the route priced without implied vols (the session's solve inverts it).
    xiu = rig.bars[XIU]
    repos.upsert_chain_rows(
        store,
        chain(
            XIU,
            xiu[-1].close,
            session=AS_OF,
            expiries=[AS_OF + timedelta(days=120)],
            bsm=bsm,
            with_iv=False,
        ),
    )
    repos.upsert_macro(store, macro(days))
    sources_repo.upsert_cot_reports(store, cot_reports("13874A", weeks=160))
    repos.upsert_curve_points(store, curves())
    holdings(store)
    return rig


def macro(days: list[date]) -> list[MacroObservation]:
    out: list[MacroObservation] = []
    out += daily_series("EURUSD", "ecb", days, seed=101, start=1.08, vol=0.07, units="USD per EUR")
    out += daily_series("EURCAD", "ecb", days, seed=102, start=1.47, vol=0.06, units="CAD per EUR")
    out += daily_series(
        "DFF", "fred", days, seed=103, start=4.3, vol=0.01, units="%", lag_days=1, level=True
    )
    out += daily_series(
        "DGS10", "fred", days, seed=104, start=4.1, vol=0.05, units="%", lag_days=1, level=True
    )
    out += daily_series(
        "DGS2", "fred", days, seed=105, start=3.9, vol=0.05, units="%", lag_days=1, level=True
    )
    out += daily_series(
        "DTB3", "fred", days, seed=106, start=4.2, vol=0.01, units="%", lag_days=1, level=True
    )
    out += daily_series(
        "ECB_DFR", "ecb", days, seed=107, start=2.0, vol=0.005, units="%", level=True
    )
    out += daily_series(
        "BAA10Y", "fred", days, seed=108, start=1.8, vol=0.02, units="%", lag_days=1, level=True
    )
    out += daily_series(
        "VIXCLS", "fred", days, seed=109, start=17.0, vol=0.6, units="Index", lag_days=1
    )
    out += daily_series(
        "VXVCLS", "fred", days, seed=110, start=19.0, vol=0.4, units="Index", lag_days=1
    )
    out += monthly_series(
        "CPIAUCSL",
        "fred",
        months=36,
        start=300.0,
        step=0.8,
        units="Index 1982-1984=100",
        lag_days=42,
    )
    out += monthly_series(
        "IRLTLT01DEM156N", "fred", months=36, start=2.4, step=0.01, units="%", lag_days=35
    )
    out += monthly_series(
        "IRSTCI01CAM156N", "fred", months=36, start=2.75, step=0.0, units="%", lag_days=20
    )
    out += monthly_series(
        "CP0000DEM086NEST",
        "ecb",
        months=36,
        start=120.0,
        step=0.2,
        units="Index 2015=100",
        lag_days=40,
    )
    return out


def curves() -> list[CurvePoint]:
    out: list[CurvePoint] = []
    for offset in range(130):
        day = AS_OF - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for rung, (months, price) in enumerate(
            ((2, 70.0 + offset * 0.05), (3, 71.0 + offset * 0.05))
        ):
            out.append(
                CurvePoint(
                    snapshot_date=day,
                    root="CL",
                    expiry=day + timedelta(days=30 * months),
                    price=price,
                    annualised_roll=None if rung == 0 else 0.12,
                    conid=None,
                    exchange="NYMEX",
                    currency="USD",
                    source=SOURCE,
                    collected_at=STAMP,
                    market_data_status=MarketDataStatus.DELAYED,
                    market_data_raw="DELAYED",
                    quote_delay_seconds=900,
                )
            )
    return out


def holdings(store: Store) -> None:
    """XLE's holdings file: two names above the two-stock flag."""
    as_of = AS_OF - timedelta(days=2)
    lines = [
        Holding(
            fund_symbol="XLE",
            as_of=as_of,
            line=index,
            ticker=ticker,
            name=name,
            asset_class="Equity",
            issuer_sector="Energy",
            sector="energy",
            location="United States",
            country="US",
            exchange="New York Stock Exchange Inc.",
            ibkr_exchange="NYSE",
            currency="USD",
            weight=weight,
            candidate_key=None,
        )
        for index, (ticker, name, weight) in enumerate(
            (
                ("XOM", "EXXON MOBIL CORP", 0.23),
                ("CVX", "CHEVRON CORP", 0.18),
                ("COP", "CONOCO", 0.07),
            )
        )
    ]
    universe_repo.save_holdings(
        store,
        HoldingsFile(
            fund_symbol="XLE",
            as_of=as_of,
            issuer="iShares",
            url="https://example.invalid/xle.csv",
            downloaded_at=STAMP,
            content_sha256="0" * 64,
            rows=len(lines),
        ),
        lines,
    )


def earnings(symbol: str, reports: list[datetime]) -> list[EarningsDate]:
    return [
        EarningsDate(
            symbol=symbol, collected_on=AS_OF, report_at=report, source="fixture", grade="research"
        )
        for report in reports
    ]
