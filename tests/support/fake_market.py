"""A fake of IBKR's market-data reads for the collectors, on top of :class:`FakeBroker`.

It adds daily history (served inside the requested ``period`` ending at ``startTime``, so the
reader's backward walk terminates the way it does live), option sections in the contract search,
the strike ladder, the per-strike contract info that names an option's expiries, the futures
ladder and the market scanner. A test arranges the market and asserts on ``requests``. Shapes are
IBKR's documented ones and lite's readings, not live captures (VERIFY-LIVE).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
import respx
from fake_broker import BASE, FakeBroker

#: Snapshot field ids beyond the broker's (config/defaults/ibkr.yaml).
UNDERLYING_IV, DELTA, OPEN_INTEREST, VOLUME = "7283", "7308", "7638", "87"

_START = re.compile(r"(?P<day>\d{8})-")


@dataclass
class Option:
    """One listed option contract."""

    conid: int
    expiry: date
    multiplier: float | None = 100.0


@dataclass
class Chain:
    """An underlying's listed options on one venue."""

    months: list[str]
    #: The venues the contract search lists for it.
    exchanges: list[str]
    #: The venues that answer its strikes and contracts (by default, the listed ones).
    serves: list[str] | None = None
    #: (month) -> strikes listed, calls and puts alike.
    strikes: dict[str, list[float]] = field(default_factory=dict)
    #: (month, strike, right) -> the contracts at that strike, one per expiry.
    contracts: dict[tuple[str, float, str], list[Option]] = field(default_factory=dict)


class FakeMarket(FakeBroker):
    """The broker plus the market-data endpoints the collectors read."""

    def __init__(self, router: respx.MockRouter) -> None:
        super().__init__(router)
        #: Daily bars by conid: (session, open, high, low, close, volume), stamped at 14:30 UTC.
        self.history: dict[int, list[tuple[date, float, float, float, float, float]]] = {}
        #: History requests IBKR refuses (no data) by conid.
        self.history_refused: set[int] = set()
        #: Option chains by underlying conid; their months join the contract search's rows.
        self.chains: dict[int, Chain] = {}
        #: Futures ladders by root: (conid, expiry).
        self.futures_ladders: dict[str, list[tuple[int, date]]] = {}
        #: Scanner answers by location: (conid, symbol, listing exchange).
        self.scans: dict[str, list[tuple[int, str, str]]] = {}
        #: Every market-data request: (path, query parameters).
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._mount_market(router)

    def _mount_market(self, router: respx.MockRouter) -> None:
        base = re.escape(BASE)
        router.get(url__regex=rf"{base}/iserver/marketdata/history(\?.*)?$").mock(
            side_effect=self._history
        )
        router.get(url__regex=rf"{base}/iserver/secdef/strikes(\?.*)?$").mock(
            side_effect=self._strikes
        )
        router.get(url__regex=rf"{base}/iserver/secdef/info(\?.*)?$").mock(
            side_effect=self._option_info
        )
        router.get(url__regex=rf"{base}/trsrv/futures(\?.*)?$").mock(side_effect=self._futures)
        router.post(url=f"{BASE}/iserver/scanner/run").mock(side_effect=self._scan)

    # ── arranging ──

    def bars(self, conid: int, closes: list[float], *, last: date) -> None:
        """Weekday bars ending on ``last``, one per close, each spanning 1 % around its close."""
        days: list[date] = []
        day = last
        while len(days) < len(closes):
            if day.weekday() < 5:
                days.append(day)
            day -= timedelta(days=1)
        self.history[conid] = [
            (d, c, c * 1.01, c * 0.99, c, 1000.0)
            for d, c in zip(reversed(days), closes, strict=True)
        ]

    def option_quote(self, conid: int, *, bid: float, ask: float, iv: float, delta: float) -> None:
        self.quotes[conid] = {
            "conid": conid,
            "84": str(bid),
            "86": str(ask),
            "31": str((bid + ask) / 2),
            "6509": "RP",
            "7633": f"{iv * 100:.1f}%",
            DELTA: str(delta),
            OPEN_INTEREST: "1200",
            VOLUME: "35",
        }

    def asked(self, path: str) -> list[dict[str, str]]:
        return [params for asked, params in self.requests if asked == path]

    def counts(self) -> Counter[str]:
        return Counter(path for path, _ in self.requests)

    # ── answering ──

    def _note(self, request: httpx.Request, path: str) -> dict[str, str]:
        params = dict(request.url.params)
        self.requests.append((path, params))
        return params

    def _search(self, request: httpx.Request) -> httpx.Response:
        response = super()._search(request)
        rows = response.json()
        for row in rows:
            chain = self.chains.get(int(row["conid"]))
            if chain is not None:
                row["sections"].append(
                    {
                        "secType": "OPT",
                        "months": ";".join(chain.months),
                        "exchange": ";".join(chain.exchanges),
                    }
                )
        return httpx.Response(200, json=rows)

    def _history(self, request: httpx.Request) -> httpx.Response:
        params = self._note(request, "history")
        conid = int(params["conid"])
        if conid in self.history_refused or conid not in self.history:
            return httpx.Response(500, json={"error": "Chart data unavailable"})
        bars = self.history[conid]
        span = int(params["period"].rstrip("d"))
        start = _START.match(params.get("startTime") or "")
        end = (
            datetime.strptime(start.group("day"), "%Y%m%d").date()
            if start is not None
            else bars[-1][0]
        )
        served = [b for b in bars if end - timedelta(days=span) < b[0] <= end]
        data = [
            {
                "t": int(datetime.combine(d, time(14, 30), tzinfo=UTC).timestamp() * 1000),
                "o": o,
                "h": h,
                "l": low,
                "c": c,
                "v": v,
            }
            for d, o, h, low, c, v in served
        ]
        return httpx.Response(200, json={"data": data, "mdAvailability": "S"})

    def _strikes(self, request: httpx.Request) -> httpx.Response:
        params = self._note(request, "strikes")
        chain = self.chains.get(int(params["conid"]))
        if chain is None or params["exchange"] not in (chain.serves or chain.exchanges):
            return httpx.Response(200, json={"call": [], "put": []})
        listed = chain.strikes.get(params["month"], [])
        return httpx.Response(200, json={"call": listed, "put": listed})

    def _option_info(self, request: httpx.Request) -> httpx.Response:
        params = self._note(request, "option_info")
        chain = self.chains.get(int(params["conid"]))
        if chain is None or params["exchange"] not in (chain.serves or chain.exchanges):
            return httpx.Response(200, json=[])
        key = (params["month"], float(params["strike"]), params["right"])
        rows: list[dict[str, Any]] = [
            {
                "conid": option.conid,
                "maturityDate": option.expiry.strftime("%Y%m%d"),
                "strike": float(params["strike"]),
                "right": params["right"],
                "multiplier": option.multiplier,
                "local_symbol": f"OPT {option.expiry:%y%m%d} {params['right']}{params['strike']}",
            }
            for option in chain.contracts.get(key, [])
        ]
        return httpx.Response(200, json=rows)

    def _futures(self, request: httpx.Request) -> httpx.Response:
        params = self._note(request, "futures")
        body = {
            root: [
                {"conid": conid, "expirationDate": int(f"{expiry:%Y%m%d}"), "symbol": root}
                for conid, expiry in self.futures_ladders.get(root, [])
            ]
            for root in params["symbols"].split(",")
        }
        return httpx.Response(200, json=body)

    def _scan(self, request: httpx.Request) -> httpx.Response:
        body = self._json(request)
        self.requests.append(("scanner", {k: str(v) for k, v in body.items()}))
        contracts = [
            {"con_id": conid, "symbol": symbol, "listing_exchange": exchange, "sec_type": "STK"}
            for conid, symbol, exchange in self.scans.get(body["location"], [])
        ]
        return httpx.Response(200, json={"contracts": contracts})
