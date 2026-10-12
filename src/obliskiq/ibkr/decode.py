"""Decoding IBKR's responses into the read models: pure functions, no I/O.

The dangerous failure in this layer is a value that parses cleanly and means something else, or
an absent value that becomes a zero. So:

* ``None`` means absent everywhere; a missing bid is not a bid of zero.
* Non-finite numbers (``NaN``, ``inf``, ``1e999``) are absent, never measurements.
* Prices may carry thousands separators and a prefix: ``C`` is the previous close and ``H`` a
  halted contract [CP ✓ 2026-09-10: XLP last ``"C83.05"``]; the prefix is kept as
  :class:`~obliskiq.ibkr.models.LastKind`, never silently stripped.
* Units are declared, never guessed from magnitude: implied vol arrives as ``"13.126%"``
  [CP ✓ 2026-09-10]; ``auto`` reads a ``%`` suffix as percent and anything else by the fallback.
  A value past its plausible range is a unit error and is dropped with a warning; IBKR's ``-1.0``
  "no implied vol" sentinel is caught by a floor, since it sits inside any ceiling.
* ``<id>_raw`` is preferred over the display string: field 87 shows ``"53.4K"`` beside
  ``"87_raw": 53400.0`` [CP ✓ 2026-09-10], and the abbreviation would make liquid names' volume
  absent.
* A crossed book (bid above ask) is flagged and has no mid.
* Market-data availability (field 6509) is a letter code [CP ✓ 2026-09-10: ``"DP"``, ``"DPB"``,
  ``"ZBd"``, ``"RBd"``]; its FIRST character decides realtime | delayed | frozen | none (IBKR,
  "Market Data Availability"). A delayed quote carries IBKR's documented 15-20 minute bound,
  labelled as a bound. An unrecognised code decodes to no status, with the raw code kept.
* Section lists are split on ``;`` and ``,`` alike [CP ✓ 2026-09-10: both are ``;``].

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..config.schema_ibkr import IbkrConfig, SnapshotField
from ..domain.enums import MarketDataStatus
from ..logging_setup import get_logger
from ..util.numeric import safe_float, safe_int
from .errors import DecodeError
from .models import (
    Account,
    Algo,
    AlgoParameter,
    BondFilter,
    BondIssuer,
    CashBalance,
    ContractInfo,
    ContractMatch,
    ContractRules,
    ContractSection,
    DelayBasis,
    FuturesRung,
    History,
    HistoryBar,
    IncrementRule,
    LastKind,
    Ledger,
    NavReading,
    OptionStrikes,
    Position,
    Quote,
    ScannerContract,
    ScannerParams,
    ScanType,
    ScheduledSession,
    TradingSchedule,
)

log = get_logger("ibkr.decode")

#: The fields whose arrival means a snapshot row has QUOTED, and so the only ones that end the
#: settle poll. Timed in lite [CP ✓ 2026-09-15]: on a cold option the status and all four Greeks
#: arrive at about 1.3 s and the prices at about 7 s, so settling is defined by inclusion.
QUOTE_FIELD_NAMES = ("last", "bid", "ask")

#: A percent figure divided by this is a decimal. A unit fact.
PERCENT_TO_DECIMAL = 100.0

#: Milliseconds per second, for IBKR's epoch-millisecond stamps.
MS_PER_SECOND = 1000.0

#: Price prefixes IBKR puts on field 31 (IBKR field table: C previous close, H halted; lite read
#: H and L both as halted bounds).
PRICE_PREFIXES: Mapping[str, LastKind] = {
    "C": LastKind.PRIOR_CLOSE,
    "H": LastKind.HALTED,
    "L": LastKind.HALTED,
}

#: The first character of a 6509 availability code (IBKR, "Market Data Availability").
AVAILABILITY_CODES: Mapping[str, MarketDataStatus] = {
    "R": MarketDataStatus.REALTIME,
    "D": MarketDataStatus.DELAYED,
    "Z": MarketDataStatus.FROZEN,
    "Y": MarketDataStatus.FROZEN,  # frozen delayed: frozen is the stricter claim
    "N": MarketDataStatus.NONE,  # not subscribed
    "O": MarketDataStatus.NONE,  # the market-data API acknowledgement is not signed
}

#: A 6509 code: one availability letter, then up to three of the qualifiers IBKR documents
#: (P snapshot, p consolidated, B book, d performance details).
_AVAILABILITY_CODE_RE = re.compile(rf"[{''.join(AVAILABILITY_CODES)}][pPBd]{{0,3}}")

#: The ledger's consolidated row, beside the per-currency rows.
LEDGER_BASE_KEY = "BASE"

#: The exchange rate a per-currency ledger row carries when it IS the base currency. Compared
#: exactly: anything else is a currency the consolidation converted [CP ✓ 2026-09-10].
LEDGER_BASE_RATE = 1.0

_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_DATE_KEY_RE = re.compile(r"[0-9]{8}")

_COUNT_FIELDS = ("volume", "open_interest")
_VOL_FIELDS = ("implied_vol", "historical_vol_30d")
_YIELD_FIELDS = ("dividend_yield",)
_FRACTION_FIELDS = ("implied_vol_percentile",)


# ── shared helpers ──


def field_map(fields: Iterable[SnapshotField]) -> dict[str, str]:
    """Name → field id, without the names that have no verified id."""
    return {field.name: field.id for field in fields if field.id is not None}


def raw_or_formatted(row: Mapping[str, Any], field_id: str) -> Any:
    """IBKR's exact ``<id>_raw`` value when present, else the display string."""
    raw = row.get(f"{field_id}_raw")
    return row.get(field_id) if raw is None else raw


def split_list(raw: Any) -> tuple[str, ...]:
    """One of IBKR's delimited lists, split on either separator it uses."""
    if raw is None:
        return ()
    return tuple(part for piece in re.split(r"[;,]", str(raw)) if (part := piece.strip()))


def to_decimal(raw: Any) -> Decimal | None:
    """An exact amount, or ``None`` for absent, unparseable, non-finite or overflowing input.

    A float is read through its shortest ``repr``, so ``0.1`` becomes ``Decimal("0.1")`` and not
    the binary expansion; thousands separators are removed from text.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(repr(raw)) if math.isfinite(raw) else None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    # A finite Decimal can still be absurd (``1e999``): an amount no float can hold is not an
    # amount, the same rule safe_float applies.
    if not value.is_finite() or not math.isfinite(float(value)):
        return None
    return value


def currency_code(raw: Any) -> str | None:
    text = str(raw).strip().upper() if raw is not None else ""
    return text if _CURRENCY_RE.fullmatch(text) else None


def epoch_ms_to_utc(raw: Any) -> datetime | None:
    stamp = safe_float(raw, None)
    if stamp is None or stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp / MS_PER_SECOND, tz=UTC)
    except OverflowError, OSError, ValueError:
        return None


def epoch_s_to_utc(raw: Any) -> datetime | None:
    stamp = safe_float(raw, None)
    if stamp is None or stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp, tz=UTC)
    except OverflowError, OSError, ValueError:
        return None


def yyyymmdd(raw: Any) -> date | None:
    """IBKR's ``YYYYMMDD`` dates; ``None`` for anything else."""
    text = str(raw).strip() if raw is not None else ""
    if not _DATE_KEY_RE.fullmatch(text):
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC).date()
    except ValueError:
        return None


def _text(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


# ── market data ──


def decode_price(raw: Any) -> tuple[float | None, LastKind | None]:
    """A price and what it is: a trade, the previous close (``C``) or halted (``H``)."""
    if raw is None or isinstance(raw, bool):
        return None, None
    if isinstance(raw, int | float):
        value = safe_float(raw, None)
        return value, (LastKind.TRADE if value is not None else None)
    text = str(raw).strip()
    kind = LastKind.TRADE
    if text and text[0] in PRICE_PREFIXES:
        kind = PRICE_PREFIXES[text[0]]
        text = text[1:]
    if text and not (text[0].isdigit() or text[0] in "-+."):
        return None, None  # an unknown prefix is not read as a number
    value = safe_float(text, None)
    return value, (kind if value is not None else None)


def decode_availability(
    flag: Any, *, delayed_bound_seconds: int
) -> tuple[MarketDataStatus | None, str | None, int | None, DelayBasis | None]:
    """``(status, raw flag, delay seconds, delay basis)`` from a 6509 code or a route spelling."""
    raw = _text(flag)
    if raw is None:
        return None, None, None, None
    if _AVAILABILITY_CODE_RE.fullmatch(raw):
        status: MarketDataStatus | None = AVAILABILITY_CODES[raw[0]]
    else:
        # Route spellings (REALTIME, FROZEN_DELAYED) through the domain enum, whole word only;
        # anything else stays undecoded.
        status = MarketDataStatus.from_route_flag(raw)
    if status is None:
        return None, raw, None, None
    if status is MarketDataStatus.REALTIME:
        return status, raw, 0, DelayBasis.REALTIME
    if status is MarketDataStatus.DELAYED:
        return status, raw, delayed_bound_seconds, DelayBasis.DOCUMENTED_BOUND
    return status, raw, None, None


def _units(raw: Any, declared: str, fallback: str) -> str:
    if declared != "auto":
        return declared
    looks_percent = isinstance(raw, str) and raw.strip().endswith("%")
    return "percent" if looks_percent else fallback


def _plausible(value: float | None, ceiling: float, name: str, conid: int) -> float | None:
    """A value past its plausible magnitude is a unit error, not data."""
    if value is None or abs(value) <= ceiling:
        return value
    log.warning(
        "snapshot field beyond its plausible range; dropped",
        extra={"field": name, "value": value, "ceiling": ceiling, "conid": conid},
    )
    return None


def _volatility(raw: Any, name: str, conid: int, ibkr: IbkrConfig) -> float | None:
    value = safe_float(raw, None)
    if value is None:
        return None
    units = _units(
        raw, ibkr.marketdata.implied_vol_units, ibkr.marketdata.implied_vol_units_fallback
    )
    if units == "percent":
        value /= PERCENT_TO_DECIMAL
    if value < ibkr.marketdata.implied_vol_min_plausible:
        log.warning(
            "volatility below its floor (IBKR's -1 sentinel); dropped",
            extra={"field": name, "value": value, "conid": conid},
        )
        return None
    return _plausible(value, ibkr.marketdata.implied_vol_max_plausible, name, conid)


def _dividend_yield(raw: Any, conid: int, ibkr: IbkrConfig) -> float | None:
    value = safe_float(raw, None)
    if value is None:
        return None
    units = _units(
        raw, ibkr.marketdata.dividend_yield_units, ibkr.marketdata.dividend_yield_units_fallback
    )
    if units == "percent":
        value /= PERCENT_TO_DECIMAL
    if value < 0.0:
        log.warning("negative dividend yield (a sentinel); dropped", extra={"conid": conid})
        return None
    return _plausible(value, ibkr.marketdata.dividend_yield_max_plausible, "dividend_yield", conid)


def decode_quote(
    conid: int, row: Mapping[str, Any], fields: Mapping[str, str], ibkr: IbkrConfig
) -> Quote:
    """One snapshot row, decoded against ``fields`` (name → field id)."""
    values: dict[str, Any] = {}
    last_kind: LastKind | None = None
    for name, field_id in fields.items():
        raw = raw_or_formatted(row, field_id)
        if name in QUOTE_FIELD_NAMES:
            price, kind = decode_price(raw)
            values[name] = price
            if name == "last":
                last_kind = kind
        elif name in _COUNT_FIELDS:
            count = safe_int(raw, None)
            values[name] = count if count is None or count >= 0 else None
        elif name in _VOL_FIELDS:
            values[name] = _volatility(raw, name, conid, ibkr)
        elif name in _YIELD_FIELDS:
            values[name] = _dividend_yield(raw, conid, ibkr)
        elif name in _FRACTION_FIELDS:
            values[name] = safe_float(raw, None)
        elif name == "market_data_status":
            values[name] = raw
        else:
            values[name] = safe_float(raw, None)
    status, flag, delay, basis = decode_availability(
        values.get("market_data_status"),
        delayed_bound_seconds=ibkr.marketdata.delayed_quote_bound_seconds,
    )
    bid, ask = values.get("bid"), values.get("ask")
    crossed = bid is not None and ask is not None and bid > ask
    if crossed:
        log.warning("crossed book: bid above ask", extra={"conid": conid, "bid": bid, "ask": ask})
    return Quote(
        conid=conid,
        last=values.get("last"),
        last_kind=last_kind,
        bid=bid,
        ask=ask,
        crossed=crossed,
        volume=values.get("volume"),
        open_interest=values.get("open_interest"),
        implied_vol=values.get("implied_vol"),
        historical_vol_30d=values.get("historical_vol_30d"),
        implied_vol_percentile=values.get("implied_vol_percentile"),
        dividend_yield=values.get("dividend_yield"),
        delta=_plausible(values.get("delta"), ibkr.marketdata.delta_max_plausible, "delta", conid),
        gamma=values.get("gamma"),
        vega=values.get("vega"),
        theta=values.get("theta"),
        market_data_status=status,
        status_flag=flag,
        delay_seconds=delay,
        delay_basis=basis,
        updated_at=epoch_ms_to_utc(row.get("_updated")),
    )


def decode_history(conid: int, body: Any, *, delayed_bound_seconds: int) -> History:
    """``data[]`` of ``{t (epoch ms), o, h, l, c, v}``, sorted; undated bars are dropped."""
    data = body if isinstance(body, Mapping) else {}
    bars: list[HistoryBar] = []
    for row in data.get("data") or []:
        if not isinstance(row, Mapping):
            continue
        start = epoch_ms_to_utc(row.get("t"))
        if start is None:
            continue
        bars.append(
            HistoryBar(
                start=start,
                open=safe_float(row.get("o"), None),
                high=safe_float(row.get("h"), None),
                low=safe_float(row.get("l"), None),
                close=safe_float(row.get("c"), None),
                volume=safe_float(row.get("v"), None),
            )
        )
    bars.sort(key=lambda bar: bar.start)
    status, flag, _delay, _basis = decode_availability(
        data.get("mdAvailability"), delayed_bound_seconds=delayed_bound_seconds
    )
    return History(conid=conid, bars=tuple(bars), status_flag=flag, market_data_status=status)


# ── portfolio ──


def decode_accounts(body: Any) -> list[Account]:
    out: list[Account] = []
    for row in body if isinstance(body, list) else []:
        if not isinstance(row, Mapping):
            continue
        account_id = _text(row.get("id") or row.get("accountId"))
        if account_id is None:
            continue
        out.append(
            Account(
                account_id=account_id,
                currency=currency_code(row.get("currency")),
                account_type=_text(row.get("type")),
            )
        )
    return out


def decode_positions(rows: Any) -> list[Position]:
    """One page of positions. An empty body is no positions; anything unreadable raises.

    Positions feed reconciliation and the cash checks, so a page with a row that cannot be read
    is refused whole rather than returned short: a short read would look complete.
    """
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise DecodeError("a positions page that is not a list: the positions read is incomplete")
    out: list[Position] = []
    for row in rows:
        conid = safe_int(row.get("conid"), None) if isinstance(row, Mapping) else None
        quantity = to_decimal(row.get("position")) if isinstance(row, Mapping) else None
        if not isinstance(row, Mapping) or conid is None or conid <= 0 or quantity is None:
            raise DecodeError(
                "a position row without a conid or a quantity: the positions read is incomplete"
            )
        out.append(
            Position(
                conid=conid,
                quantity=quantity,
                market_price=to_decimal(row.get("mktPrice")),
                market_value=to_decimal(row.get("mktValue")),
                average_cost=to_decimal(row.get("avgCost")),
                currency=currency_code(row.get("currency")),
                asset_class=_text(row.get("assetClass")),
                description=_text(row.get("contractDesc")),
                listing_exchange=_text(row.get("listingExchange")),
            )
        )
    return out


def ledger_base_currency(ledger: Mapping[str, Any]) -> str | None:
    """The currency the BASE row is consolidated into.

    The BASE row names itself ``"BASE"`` on both ``currency`` and ``secondkey``
    [CP ✓ 2026-09-10], so a label is believed only when it is a real code; otherwise the single
    per-currency row whose exchange rate is exactly one identifies it. None or several: ``None``.
    """
    row = ledger.get(LEDGER_BASE_KEY)
    if isinstance(row, Mapping):
        for key in ("currency", "secondkey"):
            label = currency_code(row.get(key))
            if label is not None and label != LEDGER_BASE_KEY:
                return label
    candidates = [
        code
        for key, entry in ledger.items()
        if isinstance(entry, Mapping)
        and (code := currency_code(key)) is not None
        and safe_float(entry.get("exchangerate"), None) == LEDGER_BASE_RATE
    ]
    return candidates[0] if len(candidates) == 1 else None


def decode_ledger(body: Any, *, base_currency: str) -> Ledger:
    """NAV in the configured base currency only, and cash per currency."""
    ledger = body if isinstance(body, Mapping) else {}
    balances: list[CashBalance] = []
    unreadable: list[str] = []
    for key, entry in ledger.items():
        code = currency_code(key)
        if code is None or not isinstance(entry, Mapping):
            continue
        cash = to_decimal(entry.get("cashbalance"))
        if cash is None:
            unreadable.append(code)
            continue
        balances.append(
            CashBalance(currency=code, cash=cash, settled_cash=to_decimal(entry.get("settledcash")))
        )
    balances.sort(key=lambda balance: balance.currency)
    identified = ledger_base_currency(ledger)
    base_row = ledger.get(LEDGER_BASE_KEY)
    nav: NavReading | None = None
    refusal: str | None = None
    value = (
        to_decimal(base_row.get("netliquidationvalue")) if isinstance(base_row, Mapping) else None
    )
    if not isinstance(base_row, Mapping):
        refusal = "the ledger has no BASE row"
    elif value is None:
        refusal = "the BASE row carries no readable net liquidation value"
    elif identified is None:
        refusal = "the ledger's base currency cannot be identified"
    elif identified != base_currency:
        refusal = (
            f"the ledger's base currency is {identified}, not the configured {base_currency}; "
            "a NAV in another currency is refused, not converted"
        )
    else:
        nav = NavReading(nav=value, currency=identified)
    return Ledger(
        nav=nav,
        nav_refusal=refusal,
        base_currency=identified,
        balances=tuple(balances),
        unreadable_currencies=tuple(sorted(unreadable)),
    )


# ── contracts ──


def _sections(raw: Any) -> tuple[ContractSection, ...]:
    out: list[ContractSection] = []
    for section in raw if isinstance(raw, list) else []:
        if not isinstance(section, Mapping):
            continue
        sec_type = _text(section.get("secType"))
        if sec_type is None:
            continue
        out.append(
            ContractSection(
                sec_type=sec_type.upper(),
                months=split_list(section.get("months")),
                exchanges=split_list(section.get("exchange")),
            )
        )
    return tuple(out)


def decode_search(body: Any) -> list[ContractMatch]:
    """``/iserver/secdef/search``. A miss is an OBJECT (``{"error": "No symbol found"}``), a hit
    a list [CP ✓ 2026-09-10]; a miss decodes to no matches."""
    if not isinstance(body, list):
        if body:
            log.info("the security search returned no list of matches")
        return []
    out: list[ContractMatch] = []
    for row in body:
        if not isinstance(row, Mapping):
            continue
        conid = safe_int(row.get("conid"), None)
        if conid is None or conid <= 0:
            continue
        issuers = tuple(
            BondIssuer(issuer_id=str(issuer["id"]), name=_text(issuer.get("name")))
            for issuer in row.get("issuers") or []
            if isinstance(issuer, Mapping) and issuer.get("id") is not None
        )
        out.append(
            ContractMatch(
                conid=conid,
                symbol=str(row.get("symbol") or row.get("ticker") or "").strip().upper(),
                company_name=_text(row.get("companyName") or row.get("companyHeader")),
                description=_text(row.get("description")),
                sections=_sections(row.get("sections")),
                issuers=issuers,
            )
        )
    return out


def decode_contract_info(conid: int, body: Any) -> ContractInfo:
    data = body if isinstance(body, Mapping) else {}
    underlying = safe_int(data.get("underlying_con_id"), None)
    return ContractInfo(
        conid=safe_int(data.get("con_id") or data.get("conid"), None) or conid,
        symbol=_text(data.get("symbol") or data.get("ticker")),
        sec_type=_text(data.get("instrument_type") or data.get("secType")),
        exchange=_text(data.get("exchange")),
        listing_exchange=_text(data.get("listingExchange") or data.get("listing_exchange")),
        valid_exchanges=split_list(data.get("valid_exchanges") or data.get("validExchanges")),
        currency=currency_code(data.get("currency")),
        company_name=_text(data.get("company_name") or data.get("companyName")),
        trading_class=_text(data.get("trading_class")),
        local_symbol=_text(data.get("local_symbol")),
        multiplier=safe_float(data.get("multiplier"), None),
        maturity_date=_text(data.get("maturity_date") or data.get("maturityDate")),
        underlying_conid=underlying if underlying is not None and underlying > 0 else None,
    )


def _strings(raw: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in raw if item is not None) if isinstance(raw, list) else ()


def decode_rules(conid: int, is_buy: bool, body: Any) -> ContractRules:
    """``/iserver/contract/rules``; some answers nest the rules under ``rules``."""
    data = body if isinstance(body, Mapping) else {}
    if isinstance(data.get("rules"), Mapping):
        data = data["rules"]
    increments: list[IncrementRule] = []
    for rule in data.get("incrementRules") or []:
        if not isinstance(rule, Mapping):
            continue
        edge = safe_float(rule.get("lowerEdge"), None)
        step = safe_float(rule.get("increment"), None)
        if edge is not None and step is not None and step > 0:
            increments.append(IncrementRule(lower_edge=edge, increment=step))
    algo_eligible = data.get("algoEligible")
    magnifier = safe_int(data.get("priceMagnifier"), None)
    return ContractRules(
        conid=conid,
        is_buy=is_buy,
        order_types=_strings(data.get("orderTypes")),
        order_types_outside_rth=_strings(data.get("orderTypesOutside")),
        cash_quantity_order_types=_strings(data.get("cqtTypes")),
        tif_types=_strings(data.get("tifTypes")),
        algo_eligible=algo_eligible if isinstance(algo_eligible, bool) else None,
        size_increment=safe_float(data.get("sizeIncrement"), None),
        increment_rules=tuple(increments),
        price_magnifier=magnifier,
        cash_currency=currency_code(data.get("cashCcy")),
        error=_text(data.get("error")),
    )


def decode_algos(body: Any) -> tuple[Algo, ...]:
    data = body if isinstance(body, Mapping) else {}
    out: list[Algo] = []
    for algo in data.get("algos") or []:
        if not isinstance(algo, Mapping) or _text(algo.get("id")) is None:
            continue
        parameters = tuple(
            AlgoParameter(
                parameter_id=str(param["id"]),
                name=_text(param.get("name")),
                value_class=_text(param.get("valueClassName")),
                required=(
                    str(param["required"]).lower() == "true"
                    if param.get("required") is not None
                    else None
                ),
                legal_values=_strings(param.get("legalStrings")),
            )
            for param in algo.get("parameters") or []
            if isinstance(param, Mapping) and param.get("id") is not None
        )
        out.append(
            Algo(algo_id=str(algo["id"]), name=_text(algo.get("name")), parameters=parameters)
        )
    return tuple(out)


def decode_trading_schedule(conid: int, exchange: str | None, body: Any) -> TradingSchedule:
    """``/contract/trading-schedule``: ``liquid_hours`` (epoch seconds) per ``YYYYMMDD`` date."""
    data = body if isinstance(body, Mapping) else {}
    schedules = data.get("schedules")
    sessions: list[ScheduledSession] = []
    days: list[date] = []
    for key, entry in schedules.items() if isinstance(schedules, Mapping) else []:
        day = yyyymmdd(key)
        if day is None:
            continue
        days.append(day)
        hours = entry.get("liquid_hours") if isinstance(entry, Mapping) else None
        for span in hours if isinstance(hours, list) else []:
            if not isinstance(span, Mapping):
                continue
            opening = epoch_s_to_utc(span.get("opening"))
            closing = epoch_s_to_utc(span.get("closing"))
            if opening is None or closing is None or closing <= opening:
                continue
            sessions.append(ScheduledSession(day=day, open=opening, close=closing))
    sessions.sort(key=lambda session: session.open)
    return TradingSchedule(
        conid=conid,
        exchange=exchange,
        timezone=_text(data.get("exchange_time_zone")),
        sessions=tuple(sessions),
        first_day=min(days) if days else None,
        last_day=max(days) if days else None,
    )


def decode_scanner_params(body: Any) -> ScannerParams:
    data = body if isinstance(body, Mapping) else {}

    def codes(items: Any, key: str) -> tuple[str, ...]:
        rows = items if isinstance(items, list) else []
        return tuple(
            str(item[key])
            for item in rows
            if isinstance(item, Mapping) and item.get(key) is not None
        )

    scan_types = tuple(
        ScanType(
            code=str(item["code"]),
            name=_text(item.get("display_name")),
            instruments=_strings(item.get("instruments")),
        )
        for item in data.get("scan_type_list") or []
        if isinstance(item, Mapping) and item.get("code") is not None
    )
    locations: list[str] = []
    for tree in data.get("location_tree") or []:
        if not isinstance(tree, Mapping):
            continue
        locations += codes(tree.get("locations"), "type")
    return ScannerParams(
        scan_types=scan_types,
        instruments=codes(data.get("instrument_list"), "type"),
        filters=codes(data.get("filter_list"), "code"),
        locations=tuple(locations),
    )


def decode_scanner_run(body: Any) -> tuple[ScannerContract, ...]:
    data = body if isinstance(body, Mapping) else {}
    out: list[ScannerContract] = []
    for row in data.get("contracts") or []:
        if not isinstance(row, Mapping):
            continue
        conid = safe_int(row.get("con_id") or row.get("conidex"), None)
        if conid is None or conid <= 0:
            continue
        out.append(
            ScannerContract(
                conid=conid,
                symbol=_text(row.get("symbol")),
                listing_exchange=_text(row.get("listing_exchange")),
                sec_type=_text(row.get("sec_type")),
                company_name=_text(row.get("company_name")),
            )
        )
    return tuple(out)


def decode_futures(body: Any, roots: Sequence[str]) -> dict[str, tuple[FuturesRung, ...]]:
    """``/trsrv/futures``: a ladder per root, sorted by expiry (IBKR's order is not)."""
    data = body if isinstance(body, Mapping) else {}
    out: dict[str, tuple[FuturesRung, ...]] = {}
    for root in roots:
        rungs: list[FuturesRung] = []
        for row in data.get(root) or []:
            if not isinstance(row, Mapping):
                continue
            conid = safe_int(row.get("conid"), None)
            expiry = yyyymmdd(row.get("expirationDate"))
            if conid is None or conid <= 0 or expiry is None:
                continue
            rungs.append(
                FuturesRung(root=root, conid=conid, expiry=expiry, symbol=_text(row.get("symbol")))
            )
        rungs.sort(key=lambda rung: rung.expiry)
        out[root] = tuple(rungs)
    return out


def decode_strikes(body: Any) -> OptionStrikes:
    """Strikes arrive as strings; unparseable ones are dropped, never zeroed."""
    data = body if isinstance(body, Mapping) else {}

    def side(key: str) -> tuple[float, ...]:
        values = (safe_float(v, None) for v in data.get(key) or [])
        return tuple(v for v in values if v is not None)

    return OptionStrikes(call=side("call"), put=side("put"))


def decode_bond_filters(body: Any) -> tuple[BondFilter, ...]:
    data = body if isinstance(body, Mapping) else {}
    out: list[BondFilter] = []
    for item in data.get("bondFilters") or []:
        if not isinstance(item, Mapping) or _text(item.get("displayText")) is None:
            continue
        values = tuple(
            str(option["value"])
            for option in item.get("options") or []
            if isinstance(option, Mapping) and option.get("value") is not None
        )
        out.append(BondFilter(name=str(item["displayText"]), values=values))
    return tuple(out)
