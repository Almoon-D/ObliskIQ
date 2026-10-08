"""Translate recorded IBKR **MCP** responses into the shape the collectors read.

Why this exists
---------------

Option-chain history cannot be retrieved retrospectively. (Futures curves can,
from per-contract history — see ``curve_collector`` — so the chain is the one
absolute here.) The collectors are built on the Client Portal Web API because
that is the route this build can authenticate on its own.

**One half of the original reason has since been falsified and the other has
not.** The premise was that the MCP connector "lives inside a chat session", so a
VM could never call it. IBKR now publishes a Custom Connector — the same tools
behind a remote HTTPS endpoint — and it answers a headless container with no
browser present, which this repository verified on 2026-09-03. What has *not*
been established is that this build can mint and hold the OAuth 2.0 bearer token
that endpoint takes; until it can, the connector is reachable by a human-present
session and not by a scheduled one. So this bridge still has a job, and the
collection route of record is unchanged. See ``ibkr/connector.py`` and
``docs/DECISIONS.md``.

What it changes is the cost of waiting. Until OAuth is approved the CP route is
unreachable, so *no* photograph is taken at all, and each of those days is gone
permanently. The MCP connector serves chains today. So: a human-present session
records raw MCP responses, this module translates them into the layout
:class:`~obliskiq.collect.source.FixtureMarketData` already reads, and the
ordinary collectors run over them writing ``source='mcp'``.

**This is a bridge with an end, not a second architecture.** It exists because
approval is pending. Once the CP route collects on a schedule, a day covered only
by this path is a day that depended on somebody being present, which is exactly
the fragility the VM exists to remove. Rows are tagged ``mcp`` so those days can
be identified — and, if wanted, dropped — later.

The two routes are not the same API
-----------------------------------

The translation is real work, not a rename. Observed live on 2026-08-24, and
recorded in ``references/ibkr-playbook.md``:

* **Chain retrieval inverts.** CP is per-*month*: one ``/iserver/secdef/strikes``
  call for the ladder, then one ``/iserver/secdef/info`` per strike. MCP is
  per-*expiration*: ``get_option_data`` returns a row per strike carrying **both**
  the call and the put conid, bounded by ``min_strike``/``max_strike``. One MCP
  call replaces a ladder call plus N strike calls.
* **Strikes arrive as strings** (``"1400"``), as they do on CP.
* **Response keys are hyphenated** (``option-open-interest``), where requests use
  underscores.
* **Open interest arrives as a PAIR**, ``{callInterest, putInterest}``, on a
  single contract's snapshot. The side that does not belong to the contract reads
  ``0`` — not absent. Reading the wrong side, or summing them, silently produces a
  real-looking number for the wrong instrument, so :func:`open_interest_for`
  selects by the contract's own right.
* **Implied vol is per STRIKE, not per contract.** The 1500 call and the 1500 put
  returned byte-identical IV, while the 1600 call differed — put-call parity
  holding, not a stuck field. Recorded because the identical pair reads like a bug
  and is not one.
* **The ``-1.0`` / ``is_valid: false`` sentinel is not a liquidity artefact.** It
  came back on an ASML 1400 call carrying 654 open interest. Never read
  ``annual_iv`` without ``is_valid``.
* **Session volume is NESTED, in both of its forms.** An option's is
  ``{"option-volume": {"volume": 1, ...}}`` and an underlying's is
  ``{"volume": {"volume": 164455.0}}``. Reading the outer value hands a dict to
  ``safe_int``, which returns ``None`` — a real reading silently becoming "no
  volume" in a column the turnover indicator reads.
* **Expiry dates repeat across trading classes.** ASML lists ``AS9``, ``AS9C``,
  ``ASL`` and ``ASLM`` on one date — four, where an earlier sweep recorded two.
  A capture must name the class it took.
* **The IV percentile is served even when the IV it ranks is invalid**, and the
  rank then reads ``0.0`` rather than being omitted. A capture must therefore
  request ``implied_vol_underlying`` alongside the percentile and carry its
  ``is_valid``: without it the two cases below cannot be told apart, and one of
  them is the most valuable reading on the board.
* **A ``0.0`` percentile is legitimate, and looks identical to the broken one.**
  Observed 2026-09-03: ASML returned ``high_13w`` and ``high_26w`` of exactly
  ``0.0`` with ``is_valid: true`` — a true zeroth percentile, the current IV being
  the lowest of its window. Discarding zeros to be safe would throw away precisely
  the coiled names this machine hunts. ``is_valid`` is the only discriminator, so
  it is checked and the magnitude never is.

Everything here is a pure function over decoded JSON. No I/O, no network, no
clock: the writer at the bottom is the only thing that touches a filesystem, and
it does nothing but serialise what these functions return.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ..errors import ObliskError
from ..logging_setup import get_logger
from ..util.numeric import safe_float, safe_int

log = get_logger("collect.mcp_capture")

#: IBKR's MMMYY option-month form, e.g. ``DEC26``. Upper case, three letters.
_MONTH_ABBREVIATIONS = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)

#: ``chain_snapshots.right`` values, and the ``get_option_data`` key prefix each
#: one reads. The response carries both sides on one row.
_RIGHT_PREFIX = {"C": "call", "P": "put"}

#: The ``option-open-interest`` slot belonging to each right. The other slot is
#: present and reads 0, so this selection cannot be skipped.
_OPEN_INTEREST_SLOT = {"C": "callInterest", "P": "putInterest"}

#: IBKR's compact date form, as used by ``expirations[].date`` and
#: ``last_trading_date``.
_COMPACT_DATE_FORMAT = "%Y%m%d"

#: How many characters ``_COMPACT_DATE_FORMAT`` renders. Checked explicitly
#: because ``strptime`` is LENIENT about field widths when the format has no
#: separators: it reads "202612" as 2026-01-02 rather than refusing it. IBKR
#: serves ``contract_month`` ("202612") right beside ``last_trading_date``
#: ("20261229"), so a lenient parse turns the wrong field into a plausible,
#: silently wrong date instead of an error.
_COMPACT_DATE_LENGTH = 8

#: Two-digit years in the MMMYY option-month form.
_YEARS_PER_CENTURY = 100

#: Epoch SECONDS is what ``datetime.timestamp`` gives; the bar decoder wants
#: milliseconds, because that is what the Client Portal route serves.
_MILLISECONDS_PER_SECOND = 1000.0

#: ``underlying_snapshots`` column -> the ``implied-volatility-percentile`` key it
#: reads. All three windows are carried: the store has had columns for 13w and 26w
#: since migration 005 and only 52w was ever written, so two thirds of a field the
#: connector serves for free were being discarded at the decoder.
#:
#: The windows are NOT nested and must not be read as if they were. Their
#: denominators are rolling counts of that instrument's own sessions, so a 26-week
#: percentile can sit BELOW the 13-week one with nothing wrong.
_IV_PERCENTILE_WINDOWS = {
    "iv_percentile_13w": "high_13w",
    "iv_percentile_26w": "high_26w",
    "iv_percentile_52w": "high_52w",
}


class CaptureError(ObliskError):
    """A recorded response is not the shape this module knows how to read."""


def month_key(when: date) -> str:
    """``date(2026, 12, 18)`` -> ``'DEC26'``.

    The collectors key strikes and contracts by IBKR's option month. MCP reports
    an expiry date instead, so the month is derived rather than read.
    """
    return f"{_MONTH_ABBREVIATIONS[when.month - 1]}{when.year % _YEARS_PER_CENTURY:02d}"


def parse_compact_date(value: Any) -> date | None:
    """``'20261218'`` -> ``date(2026, 12, 18)``. ``None`` when unreadable.

    Returns ``None`` rather than raising: one unparseable expiry in a listing
    should cost that expiry, not the whole capture.
    """
    text = str(value or "").strip()
    if len(text) != _COMPACT_DATE_LENGTH:
        return None
    try:
        return datetime.strptime(text, _COMPACT_DATE_FORMAT).date()
    except ValueError:
        return None


@dataclass(frozen=True)
class Expiration:
    """One row of ``get_option_parameters``'s ``expirations`` array."""

    expiration_id: str
    expiry: date
    trading_class: str
    regular: bool

    @property
    def month(self) -> str:
        return month_key(self.expiry)


def expirations_of(body: Mapping[str, Any]) -> list[Expiration]:
    """Decode ``get_option_parameters``. Unreadable rows are dropped, with a warning."""
    out: list[Expiration] = []
    for row in body.get("expirations") or []:
        expiry = parse_compact_date(row.get("date"))
        expiration_id = str(row.get("id") or "").strip()
        if expiry is None or not expiration_id:
            log.warning("dropping an unreadable expiration row", extra={"row": dict(row)})
            continue
        out.append(
            Expiration(
                expiration_id=expiration_id,
                expiry=expiry,
                trading_class=str(row.get("trading_class") or ""),
                regular=bool(row.get("regular")),
            )
        )
    return out


def select_expirations(
    expirations: Sequence[Expiration],
    *,
    session: date,
    dte_min: int,
    dte_max: int,
    trading_class: str | None = None,
) -> list[Expiration]:
    """The expirations worth capturing: inside the tenor window, one class per date.

    ``trading_class`` pins the class explicitly. Left unset, the alphabetically
    first *regular* class on each date wins — deterministic, so two captures of
    the same day agree, and never a weekly sitting beside its monthly.

    The window is the collector's own ``tenor.entry_dte_min``/``entry_dte_max``.
    Capturing outside it would spend market-data calls on contracts the mandate
    cannot trade.
    """
    wanted = [
        candidate
        for candidate in expirations
        if dte_min <= (candidate.expiry - session).days <= dte_max
        and (trading_class is None or candidate.trading_class == trading_class)
    ]
    by_date: dict[date, Expiration] = {}
    for candidate in sorted(wanted, key=lambda e: (e.expiry, not e.regular, e.trading_class)):
        by_date.setdefault(candidate.expiry, candidate)
    return [by_date[key] for key in sorted(by_date)]


def strike_ladder(body: Mapping[str, Any]) -> dict[str, list[float]]:
    """``get_option_data`` -> ``{'call': [...], 'put': [...]}``.

    A side is listed only where that side actually has a contract id. An
    unlisted put at a listed call's strike is a real thing, and inventing it
    would send the collector looking for a conid that does not exist.
    """
    out: dict[str, list[float]] = {"call": [], "put": []}
    for row in body.get("contracts") or []:
        strike = safe_float(row.get("strike"), None)
        if strike is None:
            continue
        for right, prefix in _RIGHT_PREFIX.items():
            if safe_int(row.get(f"{prefix}_contract_id"), None) is not None:
                out[_RIGHT_PREFIX[right]].append(strike)
    return {side: sorted(set(values)) for side, values in out.items()}


def contract_rows(body: Mapping[str, Any], *, expiry: date, right: str) -> list[dict[str, Any]]:
    """``get_option_data`` -> the rows ``option_contracts`` returns on the CP route.

    ``maturityDate`` is the key the chain collector reads, and it is written in
    ISO form because that is what :func:`~obliskiq.util.timeutil.parse_date`
    round-trips without ambiguity.
    """
    if right not in _RIGHT_PREFIX:
        raise CaptureError(f"right must be one of {sorted(_RIGHT_PREFIX)}, not {right!r}")
    prefix = _RIGHT_PREFIX[right]
    rows: list[dict[str, Any]] = []
    for row in body.get("contracts") or []:
        strike = safe_float(row.get("strike"), None)
        conid = safe_int(row.get(f"{prefix}_contract_id"), None)
        if strike is None or conid is None:
            continue
        rows.append(
            {
                "strike": strike,
                "conid": conid,
                "maturityDate": expiry.isoformat(),
                "right": right,
                "exchange": body.get("exchange"),
                "currency": body.get("currency"),
            }
        )
    return rows


def open_interest_for(snapshot: Mapping[str, Any], *, right: str) -> int | None:
    """This contract's own open interest, from the pair MCP returns.

    ``option-open-interest`` carries ``{callInterest, putInterest}`` on a single
    contract's snapshot, and the side that does not apply reads ``0`` rather than
    being absent. Taking the wrong slot yields 0 — a plausible "no interest"
    reading for a contract that has plenty.
    """
    if right not in _OPEN_INTEREST_SLOT:
        raise CaptureError(f"right must be one of {sorted(_OPEN_INTEREST_SLOT)}, not {right!r}")
    block = snapshot.get("option-open-interest")
    if not isinstance(block, Mapping):
        return None
    return safe_int(block.get(_OPEN_INTEREST_SLOT[right]), None)


def implied_vol_of(snapshot: Mapping[str, Any]) -> float | None:
    """Annualised implied vol as a decimal, or ``None`` where IBKR declined to quote.

    ``is_valid`` is authoritative and is never inferred from the magnitude. IBKR
    serves ``-1.0`` with ``is_valid: false`` for "no implied vol here", observed
    on a contract carrying 654 open interest — so this is not a thin-contract
    special case, and a reader that trusts the number because it looks numeric
    stores a sentinel as data.
    """
    block = snapshot.get("implied-vol")
    if not isinstance(block, Mapping):
        return None
    valid = block.get("is_valid", block.get("isValid"))
    if valid is not True:
        return None
    value = safe_float(block.get("annual_iv", block.get("annualIv")), None)
    if value is None or not math.isfinite(value):
        return None
    return value


def quote_row(snapshot: Mapping[str, Any], *, right: str | None = None) -> dict[str, Any]:
    """One MCP snapshot -> the row ``FixtureMarketData`` decodes into a ``Quote``.

    Absent is ``None`` throughout, never zero. A field IBKR omitted because it
    timed out and a field genuinely at zero are different facts, and the whole
    ``Quote`` contract rests on not conflating them.
    """
    bid_ask = _block(snapshot, "bid-ask")
    last = _block(snapshot, "last")

    row: dict[str, Any] = {
        "bid": safe_float(bid_ask.get("bid"), None),
        "ask": safe_float(bid_ask.get("ask"), None),
        "last": safe_float(last.get("price"), None),
        "implied_vol": implied_vol_of(snapshot),
        "delta": None,
        "volume": _volume_of(snapshot),
        "market_data_status": _top_status(snapshot),
    }
    if right is not None:
        row["open_interest"] = open_interest_for(snapshot, right=right)
    row.update(_underlying_fields(snapshot))
    return row


def _block(snapshot: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """One nested block of a snapshot, or an empty mapping when absent."""
    value = snapshot.get(key)
    return value if isinstance(value, Mapping) else {}


def _volume_of(snapshot: Mapping[str, Any]) -> int | None:
    """Session volume, from whichever key carries it — and it is NESTED.

    Both forms wrap the number one level deep: an option's is
    ``{"option-volume": {"volume": 1, "trades": 1, "total_cost": ...}}`` and an
    underlying's is ``{"volume": {"volume": 136099.0}}``. Reading the outer value
    hands a dict to ``safe_int``, which returns ``None`` — a real reading of
    136,099 silently becoming "no volume", in a column the turnover indicator
    reads.

    ``option_volume`` and ``volume`` are also the wrong way round on an OPT
    contract relative to IBKR's own documentation — observed live, and recorded
    in ``references/ibkr-playbook.md`` — so both keys are tried rather than one
    being assumed.
    """
    for key in ("option-volume", "volume"):
        block = _block(snapshot, key)
        if "volume" in block:
            return safe_int(block.get("volume"), None)
    return None


def _top_status(snapshot: Mapping[str, Any]) -> str | None:
    """``top-status`` as reported, or ``None`` where it was not.

    MCP omits the field entirely rather than reporting a state, and its own
    documentation says frozen and delayed-frozen prices are not served at all. So
    absence here means "the route said nothing", which is emphatically not the
    same as realtime — see migration 006.
    """
    block = snapshot.get("top-status")
    if isinstance(block, Mapping):
        block = block.get("status", block.get("code"))
    if block is None:
        return None
    text = str(block).strip()
    return text or None


def underlying_iv_is_valid(snapshot: Mapping[str, Any]) -> bool:
    """Whether the snapshot affirmatively reports a VALID current underlying IV.

    Absence is false, not unknown-and-therefore-fine. A percentile is the rank of
    the *current* reading within its window, so a response that does not carry the
    current reading's validity cannot establish that its percentile means anything
    — and an untested gate is never a passed gate. This mirrors
    :func:`implied_vol_of`, which already refuses an absent ``implied-vol`` block
    rather than reading around it.
    """
    block = snapshot.get("implied-vol-underlying")
    if not isinstance(block, Mapping):
        return False
    return block.get("is_valid", block.get("isValid")) is True


def _underlying_fields(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Historical vol and the vendor IV percentiles, where the snapshot carried them.

    ``historical-vol.annual_pct`` is a FRACTION despite the ``_pct`` name, and
    ``implied-volatility-percentile`` values are fractions too — both confirmed
    live on the connector route. Neither is rescaled here; dividing either by a
    hundred is the documented way to get this wrong.

    **The percentiles are gated on the current IV's validity, and that gate is
    load-bearing.** IBKR serves a populated ``implied-volatility-percentile``
    even when ``implied-vol-underlying`` reads ``is_valid: false``, and the rank
    of an invalid reading comes back as ``0.0`` rather than being omitted —
    observed on three EUR-listed names outside venue hours.

    A ``0.0`` cannot be recognised by its own value, and that is the whole
    difficulty. Observed live on 2026-09-03: ASML returned ``high_13w`` and
    ``high_26w`` of exactly ``0.0`` with ``is_valid: true`` and a current IV of
    0.3779 — a genuine zeroth percentile, meaning the current IV is the lowest of
    its window, which is the most interesting reading this field can produce.
    The invalid case and the maximally-compressed case are byte-identical in the
    percentile itself, so ``is_valid`` is the ONLY thing that separates them.

    Hence: no magnitude test, ever. Dropping zeros would discard exactly the
    coiled names the grid exists to find, and keeping them ungated puts a quote
    IBKR declined to give at the top of the compression screen.
    """
    out: dict[str, Any] = {}
    historical = _block(snapshot, "historical-vol")
    if historical:
        out["historical_vol_30d"] = safe_float(historical.get("annual_pct"), None)
    percentile = _block(snapshot, "implied-volatility-percentile")
    if percentile and underlying_iv_is_valid(snapshot):
        for column, key in _IV_PERCENTILE_WINDOWS.items():
            out[column] = safe_float(percentile.get(key), None)
    elif percentile:
        log.warning(
            "dropping an IV percentile whose underlying IV is not affirmatively valid",
            extra={"windows": sorted(percentile)},
        )
    return out


def bars_from_history(body: Mapping[str, Any]) -> dict[str, Any]:
    """``get_price_history`` -> the ``{"data": [...]}`` shape the bar decoder reads.

    **The two routes disagree structurally here, and this is not cosmetic.** CP
    returns a row per bar — ``data: [{t, o, h, l, c, v}, ...]`` with ``t`` in epoch
    milliseconds. MCP returns PARALLEL COLUMNS — ``time[]``, ``open[]``,
    ``high[]``, ``low[]``, ``close[]``, ``volume[]`` — with ``time`` as ISO-8601
    strings. Passing an MCP body to ``_bars_from_body`` unchanged yields zero bars
    and no error, because it looks for a ``data`` key that is simply not there.

    Columns are zipped only as far as the SHORTEST of them. A bar assembled from
    a ragged tail would pair one day's close with another day's volume, which is
    a fabricated bar rather than a missing one.
    """
    times = list(body.get("time") or [])
    if not times:
        return {"data": []}
    columns = {key: list(body.get(key) or []) for key in ("open", "high", "low", "close", "volume")}
    usable = min([len(times)] + [len(values) for values in columns.values() if values])

    data: list[dict[str, Any]] = []
    for index in range(usable):
        stamp = _epoch_ms(times[index])
        if stamp is None:
            continue
        bar: dict[str, Any] = {"t": stamp}
        for key, short in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c")):
            values = columns[key]
            bar[short] = safe_float(values[index], None) if index < len(values) else None
        volumes = columns["volume"]
        bar["v"] = safe_float(volumes[index], None) if index < len(volumes) else None
        data.append(bar)
    return {"data": data}


def _epoch_ms(value: Any) -> float | None:
    """An ISO-8601 instant -> epoch milliseconds. ``None`` when unreadable."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp() * _MILLISECONDS_PER_SECOND


def futures_rows(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``search_futures`` -> the rows the curve collector's ladder decoder reads.

    ``last_trading_date`` is IBKR's compact form and becomes ``expirationDate``.
    The ladder is returned UNSORTED — confirmed live, with one root's front
    month arriving mid-list — and ``_futures_from_rows`` sorts it, so
    nothing here depends on the order it arrived in.

    ``symbol`` is deliberately dropped. It is identical for every expiry rather
    than a per-contract code, so carrying it through would put a value that
    cannot distinguish two contracts into a column that looks like it can.
    """
    rows: list[dict[str, Any]] = []
    for row in body.get("contracts") or []:
        conid = safe_int(row.get("contract_id"), None)
        expiry = parse_compact_date(row.get("last_trading_date"))
        if conid is None or expiry is None:
            continue
        rows.append(
            {
                "conid": conid,
                "expirationDate": expiry.isoformat(),
                "exchange": row.get("exchange"),
                "currency": row.get("currency"),
            }
        )
    return rows


# ── the writer ──


@dataclass
class CaptureBuilder:
    """Accumulates translated responses and writes the fixture layout.

    The layout is :class:`~obliskiq.collect.source.FixtureMarketData`'s, so the
    collectors run over a capture unchanged. Nothing is invented: a directory
    this produces contains exactly what was recorded, and a collector asking for
    something absent raises rather than collecting nothing successfully.
    """

    conids: dict[str, int]
    #: symbol -> {"conid": int, "months": [...], "exchanges": [...]}. The conid is
    #: part of the section rather than only of ``conids`` because the chain
    #: collector asks for the OPT section belonging to the conid the universe
    #: holds: a symbol names several listings and their option venues differ.
    option_months: dict[str, dict[str, Any]]
    strikes: dict[str, dict[str, list[float]]]
    contracts: dict[str, list[dict[str, Any]]]
    quotes: dict[str, dict[str, Any]]
    futures: dict[str, list[dict[str, Any]]]
    bars: dict[str, dict[str, Any]]
    positions: list[dict[str, Any]]

    def __init__(self) -> None:
        self.conids = {}
        self.option_months = {}
        self.strikes = {}
        self.contracts = {}
        self.quotes = {}
        self.futures = {}
        self.bars = {}
        self.positions = []

    def add_underlying(self, symbol: str, conid: int) -> None:
        self.conids[symbol.upper()] = int(conid)

    def add_expiration(
        self,
        symbol: str,
        conid: int,
        expiration: Expiration,
        option_data: Mapping[str, Any],
        *,
        exchange: str,
    ) -> None:
        """Fold one ``get_option_data`` response into the capture."""
        self.add_underlying(symbol, conid)
        month = expiration.month
        entry = self.option_months.setdefault(
            symbol.upper(), {"conid": int(conid), "months": [], "exchanges": []}
        )
        entry["conid"] = int(conid)
        if month not in entry["months"]:
            entry["months"].append(month)
        if exchange not in entry["exchanges"]:
            entry["exchanges"].append(exchange)

        ladder_key = f"{conid}_{month}"
        ladder = self.strikes.setdefault(ladder_key, {"call": [], "put": []})
        for side, values in strike_ladder(option_data).items():
            ladder[side] = sorted(set(ladder[side]) | set(values))

        for right in _RIGHT_PREFIX:
            key = f"{conid}_{month}_{right}"
            rows = self.contracts.setdefault(key, [])
            known = {(row["strike"], row["maturityDate"]) for row in rows}
            for row in contract_rows(option_data, expiry=expiration.expiry, right=right):
                if (row["strike"], row["maturityDate"]) not in known:
                    rows.append(row)

    def add_quote(
        self, conid: int, snapshot: Mapping[str, Any], *, right: str | None = None
    ) -> None:
        self.quotes[str(int(conid))] = quote_row(snapshot, right=right)

    def add_futures(self, root: str, body: Mapping[str, Any]) -> None:
        self.futures[str(root).upper()] = futures_rows(body)

    def add_bars(self, conid: int, step: str, body: Mapping[str, Any]) -> None:
        """Recorded OHLCV, translated out of MCP's columnar form.

        A body already in the row-per-bar shape is passed through, so a recorded
        CP response and a recorded MCP response can sit in the same capture.
        """
        payload = dict(body) if body.get("data") is not None else bars_from_history(body)
        self.bars[f"{int(conid)}_{step}"] = payload

    def write(self, out_dir: str | Path) -> Path:
        """Serialise the capture. Returns the directory written."""
        root = Path(out_dir)
        root.mkdir(parents=True, exist_ok=True)
        _dump(root / "conids.json", self.conids)
        _dump(root / "option_months.json", self.option_months)
        _dump(root / "quotes.json", self.quotes)
        _dump(root / "futures.json", self.futures)
        _dump(root / "positions.json", self.positions)
        for name, payload in self.strikes.items():
            _dump(root / "strikes" / f"{name}.json", payload)
        for name, rows in self.contracts.items():
            _dump(root / "contracts" / f"{name}.json", rows)
        for name, payload in self.bars.items():
            _dump(root / "bars" / f"{name}.json", payload)
        return root


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
