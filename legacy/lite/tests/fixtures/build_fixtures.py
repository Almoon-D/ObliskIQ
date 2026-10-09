"""Regenerate the recorded-response fixtures the collectors are tested against.

Run from the repository root::

    python tests/fixtures/build_fixtures.py

Response SHAPES follow IBKR's documentation. The VALUES are constructed so that
each behaviour under test is reachable rather than incidental:

* ``SPY`` has ample daily history      -> the primary realised-vol sigma rung
* ``QQQ`` has enough for the short tenor only -> the fallback realised rung
* ``FCX`` has almost none and no vendor IV   -> the configured default rung
* ``ALB`` has history but NO snapshot        -> the last-daily-close spot fallback
* ``CL`` is in backwardation, ``NG`` in contango, ``HG`` flat, ``CC`` single-expiry
* the account holds an OPT, a FOP, a BOND and a CASH line, none of which is an
  underlying root

Expiry months bracket the tenor window on purpose: JAN26 is inside 90 DTE and
DEC26 beyond 200 for a 2026-01-15 session, so both must be filtered out.

.. warning:: UNVERIFIED. These are documentation-shaped, not recorded from a live
   session — there is no live IBKR access in the sandbox this was built in.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "ibkr"

#: A fixed session date keeps every fixture deterministic. Tests pass it explicitly.
SESSION = date(2026, 1, 15)

#: JAN26 sits inside the 90-DTE floor, DEC26 beyond the 200-DTE ceiling. APR26 and
#: JUN26 are the two that survive the tenor filter.
MONTHS = ["JAN26", "APR26", "JUN26", "DEC26"]
EXPIRY = {"JAN26": "20260116", "APR26": "20260417", "JUN26": "20260619", "DEC26": "20261218"}

CONIDS = {
    "SPY": 756733,
    "QQQ": 320227571,
    "IWM": 9579970,
    "EEM": 13851622,
    "XLE": 27983,
    "GDX": 41533656,
    "FCX": 8158,
    "ALB": 4046,
    "CCJ": 6928,
    "NTR": 300894744,
    "MOS": 39281231,
    "ASML": 117589399,
    "SAN": 43645865,
    "TTE": 13718,
    "SPX": 416904,
    "SX5E": 27065049,
    "DAX": 825711,
    "IBEX35": 12193495,
    "UKX": 12089845,
    "N225": 13228094,
    "TOPIX": 13228095,
    "K200": 15187141,
    "HSI": 1854084,
    "XIN9": 137851301,
    "TA35": 172604153,
    "IBOV": 173399222,
    "AS51": 13977222,
    "TOP40": 61319367,
}

#: (conid, spot, strike step). The ladder is deliberately wider than any band the
#: collector will select, so the narrowing is visible in the output.
CHAIN_SPECS = {
    "SPY": (CONIDS["SPY"], 601.20, 10.0),
    "QQQ": (CONIDS["QQQ"], 520.50, 10.0),
    "FCX": (CONIDS["FCX"], 45.00, 2.0),
    "ALB": (CONIDS["ALB"], 78.00, 5.0),
}
LADDER_SPAN = 0.45

#: (daily bar count, starting price, per-step vol)
BAR_PLAN = {
    CONIDS["SPY"]: (320, 600.0, 0.008),
    CONIDS["QQQ"]: (14, 519.0, 0.011),
    CONIDS["FCX"]: (3, 44.8, 0.020),
    CONIDS["ALB"]: (40, 77.5, 0.014),
}

OPTION_CONID_BASE = 700_000_000
FUTURES_CONID_BASE = 800_000_000


def epoch_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def daily_series(seed: int, count: int, *, start: float, vol: float, end: date = SESSION):
    """``count`` weekday bars ending at ``end``, on a deterministic walk."""
    out, price, step = [], start, 0
    day = end - timedelta(days=count * 7 // 5 + 7)
    while len(out) < count:
        if day.weekday() < 5:
            price *= math.exp(math.sin((seed + step) * 1.7) * vol)
            out.append(
                {
                    "t": epoch_ms(day),
                    "o": round(price * 0.995, 4),
                    "h": round(price * 1.01, 4),
                    "l": round(price * 0.99, 4),
                    "c": round(price, 4),
                    "v": 1_000_000 + step * 137,
                }
            )
            step += 1
        day += timedelta(days=1)
    return out


def ladder(centre: float, *, span: float, step: float) -> list[float]:
    strike, out = round(centre * (1 - span) / step) * step, []
    while strike <= centre * (1 + span):
        if strike > 0:
            out.append(round(strike, 2))
        strike += step
    return out


def write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")


def main() -> None:
    write(ROOT / "conids.json", CONIDS)

    # SPY's vendor IV is a bare decimal, QQQ's a percent-suffixed string: the exact
    # hazard the field-definition audit exists for. FCX has none, ALB no quote at all.
    quotes = {
        str(CONIDS["SPY"]): {
            "bid": 601.10,
            "ask": 601.30,
            "last": 601.20,
            "implied_vol": 0.1842,
            "volume": 41_000_000,
        },
        str(CONIDS["QQQ"]): {
            "bid": 520.40,
            "ask": 520.60,
            "last": 520.50,
            "implied_vol": 0.2210,
            "volume": 28_000_000,
        },
        str(CONIDS["FCX"]): {"bid": 44.95, "ask": 45.05, "last": 45.00, "volume": 9_100_000},
    }

    # Each entry names the conid its OPT section came from. The chain collector
    # asks for the section belonging to the conid the universe holds, because a
    # symbol search returns every listing and the exchange list travelling with
    # the months belongs to whichever one matched — which is how SAN and TTE came
    # to report option months and serve zero strikes on 2026-09-10.
    write(
        ROOT / "option_months.json",
        {
            "SPY": {
                "conid": CONIDS["SPY"],
                "months": MONTHS,
                "exchanges": ["ARCA", "SMART", "CBOE"],
            },
            "QQQ": {"conid": CONIDS["QQQ"], "months": MONTHS, "exchanges": ["NASDAQ", "SMART"]},
            "FCX": {"conid": CONIDS["FCX"], "months": MONTHS, "exchanges": ["SMART", "CBOE"]},
            "ALB": {"conid": CONIDS["ALB"], "months": MONTHS, "exchanges": ["SMART"]},
        },
    )

    option_conid = OPTION_CONID_BASE
    for symbol, (conid, spot, step) in CHAIN_SPECS.items():
        strikes = ladder(spot, span=LADDER_SPAN, step=step)
        for month in MONTHS:
            # Strikes come back as STRINGS, per the playbook. Cast before arithmetic.
            write(
                ROOT / "strikes" / f"{conid}_{month}.json",
                {"call": [str(s) for s in strikes], "put": [str(s) for s in strikes]},
            )
            for right in ("C", "P"):
                rows = []
                for strike in strikes:
                    option_conid += 1
                    rows.append(
                        {
                            "conid": option_conid,
                            "symbol": symbol,
                            "strike": strike,
                            "right": right,
                            "maturityDate": EXPIRY[month],
                            "exchange": "SMART",
                            "currency": "USD",
                        }
                    )
                    away = abs(strike - spot) / spot
                    quotes[str(option_conid)] = {
                        "bid": round(max(0.05, spot * 0.040 * math.exp(-4 * away)), 2),
                        "ask": round(max(0.10, spot * 0.045 * math.exp(-4 * away)), 2),
                        "last": round(max(0.07, spot * 0.042 * math.exp(-4 * away)), 2),
                        "implied_vol": round(0.18 + away * 0.35, 4),
                        "delta": round((1 if right == "C" else -1) * 0.5 * math.exp(-2 * away), 4),
                        "open_interest": max(10, int(9000 * math.exp(-6 * away))),
                        "volume": max(1, int(1200 * math.exp(-6 * away))),
                    }
                write(ROOT / "contracts" / f"{conid}_{month}_{right}.json", rows)

    futures, futures_conid = {}, FUTURES_CONID_BASE

    def curve(root: str, front: float, slope: float, exchange: str, rungs: int = 6):
        nonlocal futures_conid
        rows = []
        for index in range(rungs):
            futures_conid += 1
            expiry = date(2026, 2, 1) + timedelta(days=30 * index)
            rows.append(
                {
                    "conid": futures_conid,
                    "symbol": f"{root}{expiry:%b%y}".upper(),
                    "expirationDate": expiry.strftime("%Y%m%d"),
                    "exchange": exchange,
                    "currency": "USD",
                }
            )
            quotes[str(futures_conid)] = {"last": round(front * (1 + slope) ** index, 3)}
        return rows

    futures["CL"] = curve("CL", 78.50, -0.012, "NYMEX")  # deferred cheaper: backwardation
    futures["NG"] = curve("NG", 3.10, +0.030, "NYMEX")  # deferred dearer:  contango
    futures["HG"] = curve("HG", 4.35, 0.0, "COMEX")  # flat
    futures["CC"] = curve("CC", 5900.0, 0.0, "NYBOT", rungs=1)  # no curve shape at all
    write(ROOT / "futures.json", futures)

    # Per-conid daily bars where the sigma ladder depends on the count; weekly and
    # monthly fall through to the generic files, which is all the backfill needs.
    for conid, (count, start, vol) in BAR_PLAN.items():
        write(
            ROOT / "bars" / f"{conid}_1d.json",
            {"symbol": str(conid), "data": daily_series(conid, count, start=start, vol=vol)},
        )
    for bar, count in (("1d", 60), ("1w", 120), ("1m", 60)):
        write(
            ROOT / "bars" / f"{bar}.json",
            {"symbol": "generic", "data": daily_series(7, count, start=100.0, vol=0.01)},
        )

    # The OPT, FOP, BOND and CASH lines are the point: a holding is not an
    # underlying root and must never become one.
    write(
        ROOT / "positions.json",
        [
            {
                "conid": CONIDS["SPY"],
                "ticker": "SPY",
                "assetClass": "STK",
                "currency": "USD",
                "position": 100,
                "listingExchange": "ARCA",
            },
            {
                "conid": 265598,
                "ticker": "AAPL",
                "assetClass": "STK",
                "currency": "USD",
                "position": 50,
                "listingExchange": "NASDAQ",
            },
            {
                "conid": 495512563,
                "ticker": "IREN",
                "assetClass": "STK",
                "currency": "USD",
                "position": 300,
                "listingExchange": "NASDAQ",
            },
            {
                "conid": 712345678,
                "ticker": "SPY 260417C00620000",
                "assetClass": "OPT",
                "currency": "USD",
                "position": 2,
                "listingExchange": "SMART",
            },
            {
                "conid": 712345679,
                "ticker": "CL FUT OPT",
                "assetClass": "FOP",
                "currency": "USD",
                "position": 1,
                "listingExchange": "NYMEX",
            },
            {
                "conid": 712345680,
                "ticker": "STRUCTURED NOTE 2029",
                "assetClass": "BOND",
                "currency": "EUR",
                "position": 10000,
                "listingExchange": "SBF",
            },
            {
                "conid": 712345681,
                "ticker": "EUR.USD",
                "assetClass": "CASH",
                "currency": "EUR",
                "position": 5000,
                "listingExchange": "IDEALPRO",
            },
        ],
    )

    write(ROOT / "quotes.json", quotes)

    # The account ledger, keyed by currency with a consolidated BASE row beside
    # the per-currency ones. netliquidationvalue arrives as a STRING on this
    # route; the figures here are round invented ones, because a fixture is
    # evidence about our parsing and never about this account.
    #
    # The SHAPE, though, is measured [CP 2026-09-10] and was wrong here before:
    # this file used to give the BASE row `secondkey: "EUR"`, which is the code
    # we hoped it carried. Live, BOTH `currency` and `secondkey` on that row read
    # "BASE", and the only thing naming the base currency is the per-currency row
    # whose exchangerate is one. A fixture that encodes the hope tests nothing.
    write(
        ROOT / "ledger.json",
        {
            "acctId": "U0000000",
            "BASE": {
                "currency": "BASE",
                "secondkey": "BASE",
                "netliquidationvalue": "1000000",
                "cashbalance": "250000",
                "exchangerate": 1.0,
            },
            "EUR": {
                "currency": "EUR",
                "netliquidationvalue": "600000",
                "exchangerate": 1.0,
            },
            "USD": {
                "currency": "USD",
                "netliquidationvalue": "400000",
                "exchangerate": "0.92",
            },
        },
    )

    total = sum(p.stat().st_size for p in ROOT.rglob("*.json"))
    print(f"{len(list(ROOT.rglob('*.json')))} fixture files, {total:,} bytes")


if __name__ == "__main__":
    main()
