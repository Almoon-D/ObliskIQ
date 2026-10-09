#!/usr/bin/env python3
"""Turn a directory of raw IBKR **MCP** responses into a capture the collectors read.

Option-chain history cannot be retrieved retrospectively, and until IBKR approve
the OAuth application the Client Portal route — the only one this build can
authenticate on its own — is unreachable. Every day in between is permanently
lost. The MCP connector serves chains today, from a session that holds it. This
script is the second half of that bridge: a session records raw responses, this
turns them into the layout ``FixtureMarketData`` reads, and the ordinary
collectors run over it writing ``source='mcp'``.

See ``obliskiq.collect.mcp_capture`` for the translation rules and for why this is
a bridge with an end rather than a second architecture.

Recording the raw responses
---------------------------

From a session with the IBKR MCP connector, save each response verbatim into one
directory. File names carry the meaning; there is no metadata to keep in step::

    option_parameters_<underlying_conid>.json      get_option_parameters
    option_data_<anything>.json                    get_option_data, one per expiry
    snapshot_<contract_conid>.json                 get_price_snapshot
    futures_<ROOT>.json                            search_futures
    history_<conid>_<STEP>.json                    get_price_history

Then a small ``manifest.json`` in the same directory, naming only what a filename
cannot::

    {
      "session": "2026-08-24",
      "underlyings": [
        {"symbol": "ASML", "conid": 117589399, "exchange": "FTA",
         "trading_class": "ASL"}
      ],
      "futures": {"CL": 212921504}
    }

``trading_class`` is optional and pins the class where a date lists several — ASML
lists four on one date. Left out, the alphabetically first regular class wins,
which is deterministic but may not be the one you meant.

Which fields to request
-----------------------

For an **underlying** snapshot, ask for ``implied_vol_underlying`` alongside
``implied_volatility_percentile``. The percentile is the rank of the current IV,
and IBKR serves it even when that IV is invalid — where the rank then reads
``0.0`` rather than being omitted. A genuine zeroth percentile reads ``0.0`` too
(observed on ASML with ``is_valid: true``), so the two are indistinguishable
without the validity flag and the decoder drops the percentile rather than guess.
Requesting one field costs nothing; omitting it discards the percentile entirely.

Also worth requesting: ``historical_vol``, ``top_status``, ``last``, ``bid_ask``
and ``volume``. For an **option** contract: ``implied_vol``,
``option_open_interest`` and ``volume`` — note that ``option_volume`` and
``volume`` are the wrong way round on an OPT contract relative to IBKR's own
documentation, so the decoder tries both.

Bars, and the windows to take them at
-------------------------------------

``history_<conid>_<STEP>.json`` feeds ``obliskiq-collect backfill``. The whole
window arrives in ONE call on this route — verified 2026-09-03, a
``period=TWO_YEARS``/``step=ONE_DAY`` request returning the full two years — so
take ``ONE_DAY`` at ``data.daily_window_years`` and ``ONE_WEEK`` at
``data.weekly_window_years`` and there is nothing to chunk. ``STEP`` in the
filename is the collector's own bar code (``1d``, ``1w``, ``1m``), not IBKR's.

Running it::

    python scripts/build_mcp_capture.py --raw capture/raw --out capture/2026-08-24
    obliskiq-collect chain --source mcp-capture --fixtures capture/2026-08-24 --only ASML
    obliskiq-collect backfill --source mcp-capture --fixtures capture/2026-08-24

The right of every option conid is read from the ``option_data`` responses, which
carry both sides per strike, so nothing has to be declared twice.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from obliskiq.collect.mcp_capture import (
    CaptureBuilder,
    CaptureError,
    expirations_of,
    select_expirations,
)
from obliskiq.config import load_config

#: Filename prefixes, and what each one holds.
PARAMETERS_PREFIX = "option_parameters_"
OPTION_DATA_PREFIX = "option_data_"
SNAPSHOT_PREFIX = "snapshot_"
FUTURES_PREFIX = "futures_"
HISTORY_PREFIX = "history_"


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaptureError(f"could not read {path}: {exc}") from exc


def _option_data_for(raw: Path, expiration_id: str) -> dict[str, Any] | None:
    """The ``get_option_data`` response recorded for one expiration.

    Matched on content rather than on a filename convention: the expiration id
    contains ``@`` and ``/``, which do not survive a file name, and a scheme that
    mangles them is a scheme that silently pairs the wrong chain with the wrong
    expiry.
    """
    wanted_expiry = expiration_id.split("/")[3] if expiration_id.count("/") >= 3 else None
    for path in sorted(raw.glob(f"{OPTION_DATA_PREFIX}*.json")):
        body = _read(path)
        if not isinstance(body, dict):
            continue
        recorded = str(body.get("expiration_id") or "")
        if recorded and recorded == expiration_id:
            return body
        # No echoed id: fall back to the compact expiry inside the file name.
        if wanted_expiry and wanted_expiry in path.name:
            return body
    return None


def build(raw: Path, out: Path, config_path: str | None) -> int:
    manifest = _read(raw / "manifest.json")
    if not isinstance(manifest, dict):
        raise CaptureError("manifest.json must be a JSON object")

    # Deliberately NOT load_settings(): this reads recorded JSON and writes more
    # JSON. It never opens the store, so demanding OBLISK_DB_PATH would make an
    # offline translation step fail for want of a database it does not touch.
    resolved = config_path or os.environ.get("OBLISK_CONFIG_PATH")
    if not resolved:
        raise CaptureError(
            "pass --config, or set OBLISK_CONFIG_PATH. The tenor window comes from "
            "config.yaml so the capture covers exactly the expiries the collector wants."
        )
    config = load_config(Path(resolved))
    tenor = config.section("tenor")
    dte_min = tenor.require_int("entry_dte_min")
    dte_max = tenor.require_int("entry_dte_max")

    session_text = str(manifest.get("session") or "").strip()
    if not session_text:
        raise CaptureError("manifest.json must carry a 'session' date (YYYY-MM-DD)")
    session = date.fromisoformat(session_text)

    builder = CaptureBuilder()
    captured_expiries = 0
    captured_quotes = 0

    for entry in manifest.get("underlyings") or []:
        symbol = str(entry["symbol"]).upper()
        conid = int(entry["conid"])
        exchange = str(entry["exchange"])
        trading_class = entry.get("trading_class")

        builder.add_underlying(symbol, conid)

        underlying_snapshot = raw / f"{SNAPSHOT_PREFIX}{conid}.json"
        if underlying_snapshot.is_file():
            builder.add_quote(conid, _read(underlying_snapshot))
            captured_quotes += 1

        parameters = _read(raw / f"{PARAMETERS_PREFIX}{conid}.json")
        wanted = select_expirations(
            expirations_of(parameters),
            session=session,
            dte_min=dte_min,
            dte_max=dte_max,
            trading_class=trading_class,
        )
        if not wanted:
            print(
                f"  {symbol}: no expiry inside the {dte_min}-{dte_max} DTE window",
                file=sys.stderr,
            )
        for expiration in wanted:
            option_data = _option_data_for(raw, expiration.expiration_id)
            if option_data is None:
                print(
                    f"  {symbol} {expiration.expiry}: no option_data recorded; skipping",
                    file=sys.stderr,
                )
                continue
            builder.add_expiration(
                symbol, conid, expiration, option_data, exchange=exchange
            )
            captured_expiries += 1

            # Both sides per strike, so the right of every conid is known here
            # and never has to be declared in the manifest.
            for row in option_data.get("contracts") or []:
                for right, prefix in (("C", "call"), ("P", "put")):
                    option_conid = row.get(f"{prefix}_contract_id")
                    if option_conid is None:
                        continue
                    path = raw / f"{SNAPSHOT_PREFIX}{int(option_conid)}.json"
                    if not path.is_file():
                        continue
                    builder.add_quote(int(option_conid), _read(path), right=right)
                    captured_quotes += 1

    for root, _ in (manifest.get("futures") or {}).items():
        path = raw / f"{FUTURES_PREFIX}{str(root).upper()}.json"
        if not path.is_file():
            print(f"  {root}: no futures ladder recorded; skipping", file=sys.stderr)
            continue
        builder.add_futures(str(root), _read(path))
        for row in _read(path).get("contracts") or []:
            conid = row.get("contract_id")
            if conid is None:
                continue
            snapshot = raw / f"{SNAPSHOT_PREFIX}{int(conid)}.json"
            if snapshot.is_file():
                builder.add_quote(int(conid), _read(snapshot))
                captured_quotes += 1

    for path in sorted(raw.glob(f"{HISTORY_PREFIX}*.json")):
        stem = path.stem[len(HISTORY_PREFIX) :]
        conid_text, _, step = stem.partition("_")
        if not conid_text.isdigit() or not step:
            print(f"  {path.name}: cannot read a conid and a step from the name", file=sys.stderr)
            continue
        builder.add_bars(int(conid_text), step, _read(path))

    written = builder.write(out)
    print(
        f"capture written to {written}: {len(builder.conids)} underlying(s), "
        f"{captured_expiries} expiry/expiries, {captured_quotes} quote(s), "
        f"{len(builder.futures)} futures root(s), {len(builder.bars)} bar series"
    )
    print(
        "\nrun the collectors over it with:\n"
        f"  obliskiq-collect chain --source mcp-capture --fixtures {written}\n"
        f"  obliskiq-collect curve --source mcp-capture --fixtures {written}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", required=True, help="directory of recorded MCP responses")
    parser.add_argument("--out", required=True, help="directory to write the capture into")
    parser.add_argument("--config", help="path to config.yaml; defaults to $OBLISK_CONFIG_PATH")
    args = parser.parse_args(argv)
    return build(Path(args.raw), Path(args.out), args.config)


if __name__ == "__main__":
    raise SystemExit(main())
