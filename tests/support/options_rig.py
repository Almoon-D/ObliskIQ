"""An offline options store: one shipped option underlying (SPY), its spot, a priced chain at two
expiries inside the entry window, the euro reference rate against the dollar, and optionally the
surface's implied-vol percentile, written through the store's own repositories.

The chain is priced through the system's own BSM on a gently skewed smile, so every contract has a
mid, a delta and an implied vol that agree with each other; nothing here is a market capture.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import AssetClass, InstrumentRole, MarketDataStatus, OptionRight
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.macro import MacroObservation
from obliskiq.domain.market import Bar, ChainRow
from obliskiq.domain.measurement import EntityKind, Measurement, MeasurementStatus
from obliskiq.domain.options import OptionPosition
from obliskiq.quant.bsm import BsmInputs, bsm_greeks, bsm_price
from obliskiq.store import measurements as measurements_repo
from obliskiq.store import repos
from obliskiq.store.db import Store

AS_OF = date(2026, 10, 22)
STAMP = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)
SPY = 756733
SPOT = 500.0
EURUSD = 1.1683
#: The two expiries collected: inside the entry window, near the surface's 95 and 180 days.
EXPIRIES = (AS_OF + timedelta(days=97), AS_OF + timedelta(days=178))
STRIKES = (400.0, 425.0, 450.0, 475.0, 500.0, 525.0, 550.0, 575.0, 600.0)


@dataclass(frozen=True, slots=True)
class Seeded:
    rows: tuple[ChainRow, ...]


def _iv(strike: float) -> float:
    """An equity skew: implied vol falls as the strike rises."""
    return 0.20 - 0.10 * (strike - SPOT) / SPOT


def seed_options(
    store: Store,
    settings: ObliskConfig,
    *,
    as_of: date = AS_OF,
    iv_percentile: float | None = 0.1,
    multiplier: float | None = 100.0,
    rate: bool = True,
    open_interest: int = 2000,
) -> Seeded:
    bsm = BsmInputs.from_config(settings)
    repos.upsert_instrument(
        store,
        Instrument(
            conid=SPY,
            symbol="SPY",
            listing_exchange="ARCA",
            currency="USD",
            asset_class=AssetClass.ETF,
            domicile="US",
            ucits=False,
            kid_status=None,
            share_class=None,
            lot_size=None,
            option_exchange=None,
            history_exchange="ARCA",
            roles=frozenset({InstrumentRole.OPTION_UNDERLYING, InstrumentRole.DATA}),
            superseded_by=None,
            created_at=datetime(2024, 1, 2, tzinfo=UTC),
            superseded_at=None,
        ),
    )
    repos.upsert_bars(
        store,
        [
            Bar(
                conid=SPY,
                session_date=as_of - timedelta(days=back),
                open=SPOT,
                high=SPOT * 1.01,
                low=SPOT * 0.99,
                close=SPOT,
                volume=1e6,
                source="fixture",
            )
            for back in range(5)
        ],
    )
    rows: list[ChainRow] = []
    for expiry in EXPIRIES:
        dte = (expiry - as_of).days
        for strike in STRIKES:
            right = OptionRight.PUT if strike < SPOT else OptionRight.CALL
            sigma = _iv(strike)
            is_call = right is OptionRight.CALL
            price = float(bsm_price(SPOT, strike, dte, sigma, 0.04, 0.0, is_call, bsm))
            delta = float(bsm_greeks(SPOT, strike, dte, sigma, 0.04, 0.0, is_call, bsm).delta)
            half = max(price * 0.01, 0.01)
            rows.append(
                ChainRow(
                    snapshot_date=as_of,
                    underlying_conid=SPY,
                    expiry=expiry,
                    strike=strike,
                    right=right,
                    option_conid=None,
                    option_symbol=None,
                    option_exchange="CBOE",
                    bid=round(price - half, 2),
                    ask=round(price + half, 2),
                    last=round(price, 2),
                    iv=sigma,
                    delta=delta,
                    open_interest=open_interest,
                    volume=100,
                    sigma_used=None,
                    sigma_source=None,
                    source="fixture",
                    collected_at=STAMP,
                    market_data_status=MarketDataStatus.REALTIME,
                    market_data_raw="REALTIME",
                    quote_delay_seconds=None,
                    multiplier=multiplier,
                )
            )
    repos.upsert_chain_rows(store, rows)
    if rate:
        repos.upsert_macro(
            store,
            [
                MacroObservation(
                    series_id="EURUSD",
                    source="ecb",
                    reference_date=as_of,
                    release_date=as_of,
                    value=EURUSD,
                    frequency="daily",
                    units="USD per EUR",
                    collected_at=STAMP,
                )
            ],
        )
    if iv_percentile is not None:
        measurements_repo.upsert_measurements(
            store,
            [
                Measurement(
                    entity_kind=EntityKind.INSTRUMENT,
                    entity_id=str(SPY),
                    indicator="option_surface",
                    leaf="iv_percentile",
                    as_of=as_of,
                    value=iv_percentile,
                    status=MeasurementStatus.OK,
                    reason=None,
                    reason_code=None,
                    n_obs=252,
                    window="52w (252 snapshots)",
                    units="percentile",
                    source=f"chain:{SPY}",
                    market_data_status=MarketDataStatus.REALTIME,
                    inputs_hash="0" * 64,
                    computed_at=STAMP,
                )
            ],
        )
    return Seeded(tuple(rows))


def position(**overrides: Any) -> OptionPosition:
    """An unnumbered, open one-contract call position (premium 300 in the base currency)."""
    fields: dict[str, Any] = {
        "position_id": None,
        "underlying_conid": SPY,
        "option_conid": None,
        "expiry": date(2027, 2, 19),
        "strike": 100.0,
        "right": OptionRight.CALL,
        "contracts": 1,
        "multiplier": 100.0,
        "premium_per_unit": Decimal("3.00"),
        "currency": "EUR",
        "premium_base": Decimal("300"),
        "fx_to_base": None,
        "opened_on": date(2026, 10, 1),
        "close_by": date(2027, 2, 4),
        "target": None,
        "prob_target_at_entry": None,
        "catalyst_date": None,
        "intent_id": None,
    }
    fields.update(overrides)
    return OptionPosition(**fields)
