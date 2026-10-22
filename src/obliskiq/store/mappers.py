"""Rows to domain models and back.

A stored row that cannot be read as a domain object (an unknown enum member, a negative price
a CHECK should have refused, a stamp in the wrong shape) is a corrupt store, not a missing
value: reading it raises :class:`StoreError` naming the table, and never returns a default.

Stamps are UTC and second-resolution; a value with a finer resolution is cut to the second when
it is written. Dates are ``YYYY-MM-DD`` text.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from ..domain.enums import (
    AssetClass,
    InstrumentRole,
    KidStatus,
    MarketDataStatus,
    OptionRight,
    ShareClass,
)
from ..domain.instrument import Instrument
from ..domain.macro import MacroObservation
from ..domain.market import Bar, ChainRow, CurvePoint, Snapshot
from ..errors import StoreError
from ..util.timeutil import parse_stamp, utc_stamp

INSTRUMENT_COLUMNS = (
    "conid, symbol, listing_exchange, currency, asset_class, domicile, ucits, kid_status, "
    "share_class, lot_size, option_exchange, history_exchange, roles, superseded_by, "
    "created_at, superseded_at"
)
BAR_COLUMNS = "conid, session_date, open, high, low, close, volume, source"
SNAPSHOT_COLUMNS = (
    "snapshot_date, conid, last, bid, ask, volume, implied_vol, implied_vol_source, "
    "historical_vol_30d, dividend_yield, dividend_yield_basis, implied_vol_solved, "
    "implied_vol_solved_source, source, collected_at, market_data_status, market_data_raw, "
    "quote_delay_seconds"
)
CHAIN_COLUMNS = (
    'snapshot_date, underlying_conid, expiry, strike, "right", option_conid, option_symbol, '
    "option_exchange, bid, ask, last, iv, delta, open_interest, volume, sigma_used, "
    "sigma_source, source, collected_at, market_data_status, market_data_raw, "
    "quote_delay_seconds, multiplier"
)
CURVE_COLUMNS = (
    "snapshot_date, root, expiry, price, annualised_roll, conid, exchange, currency, source, "
    "collected_at, market_data_status, market_data_raw, quote_delay_seconds"
)
MACRO_COLUMNS = (
    "series_id, source, reference_date, release_date, value, frequency, units, collected_at"
)


def date_text(value: date) -> str:
    return value.isoformat()


def optional_date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def stamp_text(value: datetime) -> str:
    return utc_stamp(value)


def optional_stamp_text(value: datetime | None) -> str | None:
    return None if value is None else utc_stamp(value)


def cutoff_text(as_of: date) -> str:
    """The first instant after ``as_of``: a row stamped before it existed on ``as_of``."""
    return utc_stamp(datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=UTC))


def read_date(text: str, table: str, column: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise StoreError(
            f"{table}.{column} holds {text!r}, which is not a YYYY-MM-DD date"
        ) from None


def read_optional_date(text: str | None, table: str, column: str) -> date | None:
    return None if text is None else read_date(text, table, column)


def read_stamp(text: str, table: str, column: str) -> datetime:
    parsed = parse_stamp(text)
    if parsed is None:
        raise StoreError(f"{table}.{column} holds {text!r}, which is not a UTC stamp")
    return parsed


def read_optional_stamp(text: str | None, table: str, column: str) -> datetime | None:
    return None if text is None else read_stamp(text, table, column)


def _status(text: str | None) -> MarketDataStatus | None:
    return None if text is None else MarketDataStatus(text)


def _build[T](table: str, row: sqlite3.Row, make: Callable[[], T]) -> T:
    try:
        return make()
    except ValueError as error:  # a bad enum member or a model validation failure
        raise StoreError(f"{table} row {tuple(row)[:3]!r} cannot be read: {error}") from None


# ── instruments ──


def instrument_params(instrument: Instrument) -> tuple[object, ...]:
    return (
        instrument.conid,
        instrument.symbol,
        instrument.listing_exchange,
        instrument.currency,
        instrument.asset_class.value,
        instrument.domicile,
        None if instrument.ucits is None else int(instrument.ucits),
        None if instrument.kid_status is None else instrument.kid_status.value,
        None if instrument.share_class is None else instrument.share_class.value,
        instrument.lot_size,
        instrument.option_exchange,
        instrument.history_exchange,
        json.dumps(sorted(role.value for role in instrument.roles)),
        instrument.superseded_by,
        stamp_text(instrument.created_at),
        optional_stamp_text(instrument.superseded_at),
    )


def instrument_from_row(row: sqlite3.Row, *, as_of_cutoff: str | None = None) -> Instrument:
    """The instrument as the row holds it, or as it stood before ``as_of_cutoff``.

    A supersession recorded at or after the cutoff had not happened yet, so the instrument is
    read as still standing.
    """
    superseded_at = row["superseded_at"]
    superseded_by = row["superseded_by"]
    if as_of_cutoff is not None and superseded_at is not None and superseded_at >= as_of_cutoff:
        superseded_at = superseded_by = None
    return _build(
        "instruments",
        row,
        lambda: Instrument(
            conid=row["conid"],
            symbol=row["symbol"],
            listing_exchange=row["listing_exchange"],
            currency=row["currency"],
            asset_class=AssetClass(row["asset_class"]),
            domicile=row["domicile"],
            ucits=None if row["ucits"] is None else bool(row["ucits"]),
            kid_status=None if row["kid_status"] is None else KidStatus(row["kid_status"]),
            share_class=None if row["share_class"] is None else ShareClass(row["share_class"]),
            lot_size=row["lot_size"],
            option_exchange=row["option_exchange"],
            history_exchange=row["history_exchange"],
            roles=frozenset(InstrumentRole(role) for role in json.loads(row["roles"])),
            superseded_by=superseded_by,
            created_at=read_stamp(row["created_at"], "instruments", "created_at"),
            superseded_at=read_optional_stamp(superseded_at, "instruments", "superseded_at"),
        ),
    )


# ── bars ──


def bar_params(bar: Bar) -> tuple[object, ...]:
    return (
        bar.conid,
        date_text(bar.session_date),
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.source,
    )


def bar_from_row(row: sqlite3.Row) -> Bar:
    return _build(
        "bars",
        row,
        lambda: Bar(
            conid=row["conid"],
            session_date=read_date(row["session_date"], "bars", "session_date"),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            source=row["source"],
        ),
    )


# ── snapshots ──


def _quote_params(row: Snapshot | ChainRow | CurvePoint) -> tuple[object, ...]:
    return (
        row.source,
        stamp_text(row.collected_at),
        None if row.market_data_status is None else row.market_data_status.value,
        row.market_data_raw,
        row.quote_delay_seconds,
    )


def snapshot_params(snapshot: Snapshot) -> tuple[object, ...]:
    return (
        date_text(snapshot.snapshot_date),
        snapshot.conid,
        snapshot.last,
        snapshot.bid,
        snapshot.ask,
        snapshot.volume,
        snapshot.implied_vol,
        snapshot.implied_vol_source,
        snapshot.historical_vol_30d,
        snapshot.dividend_yield,
        snapshot.dividend_yield_basis,
        snapshot.implied_vol_solved,
        snapshot.implied_vol_solved_source,
        *_quote_params(snapshot),
    )


def snapshot_from_row(row: sqlite3.Row) -> Snapshot:
    return _build(
        "snapshots",
        row,
        lambda: Snapshot(
            snapshot_date=read_date(row["snapshot_date"], "snapshots", "snapshot_date"),
            conid=row["conid"],
            last=row["last"],
            bid=row["bid"],
            ask=row["ask"],
            volume=row["volume"],
            implied_vol=row["implied_vol"],
            implied_vol_source=row["implied_vol_source"],
            historical_vol_30d=row["historical_vol_30d"],
            dividend_yield=row["dividend_yield"],
            dividend_yield_basis=row["dividend_yield_basis"],
            implied_vol_solved=row["implied_vol_solved"],
            implied_vol_solved_source=row["implied_vol_solved_source"],
            source=row["source"],
            collected_at=read_stamp(row["collected_at"], "snapshots", "collected_at"),
            market_data_status=_status(row["market_data_status"]),
            market_data_raw=row["market_data_raw"],
            quote_delay_seconds=row["quote_delay_seconds"],
        ),
    )


# ── chain rows ──


def chain_params(row: ChainRow) -> tuple[object, ...]:
    return (
        date_text(row.snapshot_date),
        row.underlying_conid,
        date_text(row.expiry),
        row.strike,
        row.right.value,
        row.option_conid,
        row.option_symbol,
        row.option_exchange,
        row.bid,
        row.ask,
        row.last,
        row.iv,
        row.delta,
        row.open_interest,
        row.volume,
        row.sigma_used,
        row.sigma_source,
        *_quote_params(row),
        row.multiplier,
    )


def chain_row_from_row(row: sqlite3.Row) -> ChainRow:
    return _build(
        "chain_snapshots",
        row,
        lambda: ChainRow(
            snapshot_date=read_date(row["snapshot_date"], "chain_snapshots", "snapshot_date"),
            underlying_conid=row["underlying_conid"],
            expiry=read_date(row["expiry"], "chain_snapshots", "expiry"),
            strike=row["strike"],
            right=OptionRight(row["right"]),
            option_conid=row["option_conid"],
            option_symbol=row["option_symbol"],
            option_exchange=row["option_exchange"],
            bid=row["bid"],
            ask=row["ask"],
            last=row["last"],
            iv=row["iv"],
            delta=row["delta"],
            open_interest=row["open_interest"],
            volume=row["volume"],
            sigma_used=row["sigma_used"],
            sigma_source=row["sigma_source"],
            source=row["source"],
            collected_at=read_stamp(row["collected_at"], "chain_snapshots", "collected_at"),
            market_data_status=_status(row["market_data_status"]),
            market_data_raw=row["market_data_raw"],
            quote_delay_seconds=row["quote_delay_seconds"],
            multiplier=row["multiplier"],
        ),
    )


# ── curves ──


def curve_params(point: CurvePoint) -> tuple[object, ...]:
    return (
        date_text(point.snapshot_date),
        point.root,
        date_text(point.expiry),
        point.price,
        point.annualised_roll,
        point.conid,
        point.exchange,
        point.currency,
        *_quote_params(point),
    )


def curve_point_from_row(row: sqlite3.Row) -> CurvePoint:
    return _build(
        "curves",
        row,
        lambda: CurvePoint(
            snapshot_date=read_date(row["snapshot_date"], "curves", "snapshot_date"),
            root=row["root"],
            expiry=read_date(row["expiry"], "curves", "expiry"),
            price=row["price"],
            annualised_roll=row["annualised_roll"],
            conid=row["conid"],
            exchange=row["exchange"],
            currency=row["currency"],
            source=row["source"],
            collected_at=read_stamp(row["collected_at"], "curves", "collected_at"),
            market_data_status=_status(row["market_data_status"]),
            market_data_raw=row["market_data_raw"],
            quote_delay_seconds=row["quote_delay_seconds"],
        ),
    )


# ── macro ──


def macro_params(observation: MacroObservation) -> tuple[object, ...]:
    return (
        observation.series_id,
        observation.source,
        date_text(observation.reference_date),
        optional_date_text(observation.release_date),
        observation.value,
        observation.frequency,
        observation.units,
        stamp_text(observation.collected_at),
    )


def macro_from_row(row: sqlite3.Row) -> MacroObservation:
    return _build(
        "macro",
        row,
        lambda: MacroObservation(
            series_id=row["series_id"],
            source=row["source"],
            reference_date=read_date(row["reference_date"], "macro", "reference_date"),
            release_date=read_optional_date(row["release_date"], "macro", "release_date"),
            value=row["value"],
            frequency=row["frequency"],
            units=row["units"],
            collected_at=read_stamp(row["collected_at"], "macro", "collected_at"),
        ),
    )
