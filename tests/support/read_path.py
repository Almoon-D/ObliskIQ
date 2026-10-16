"""The read path the preflight walks, mocked from tests/fixtures/ibkr for any base URL and
account, so a test can drive ``run_read_only`` through either transport.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from pathlib import Path
from typing import Any

import httpx
import respx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ibkr"
SPY = 756733
STK = [{"secType": "STK"}]


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def mock_read_path(
    router: respx.MockRouter, *, base: str, account: str, accounts: Any = None
) -> None:
    """Every endpoint the read-only preflight walks, answered from the fixtures."""
    router.post(f"{base}/iserver/auth/ssodh/init").mock(
        return_value=httpx.Response(200, json={"authenticated": True, "competing": False})
    )
    router.get(f"{base}/iserver/accounts").mock(
        return_value=httpx.Response(200, json={"accounts": [account]})
    )
    router.get(f"{base}/portfolio/accounts").mock(
        return_value=httpx.Response(
            200, json=accounts if accounts is not None else _fixture("accounts.json")
        )
    )
    router.get(f"{base}/portfolio/{account}/positions/0").mock(
        return_value=httpx.Response(200, json=_fixture("positions_page0.json"))
    )
    router.get(f"{base}/portfolio/{account}/positions/1").mock(
        return_value=httpx.Response(200, json=[])
    )
    router.get(f"{base}/portfolio/{account}/ledger").mock(
        return_value=httpx.Response(200, json=_fixture("ledger.json"))
    )
    router.post(f"{base}/iserver/secdef/search").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"conid": "999", "symbol": "SPY", "description": "MEXI", "sections": STK},
                {"conid": str(SPY), "symbol": "SPY", "description": "ARCA", "sections": STK},
            ],
        )
    )
    router.get(f"{base}/iserver/contract/{SPY}/info").mock(
        return_value=httpx.Response(
            200,
            json={
                "con_id": SPY,
                "symbol": "SPY",
                "currency": "USD",
                "valid_exchanges": "SMART,ARCA",
            },
        )
    )
    router.get(f"{base}/iserver/marketdata/snapshot").mock(
        return_value=httpx.Response(
            200,
            json=[{"conid": SPY, "31": "C671.20", "84": "671.19", "86": "671.21", "6509": "DPB"}],
        )
    )
    router.get(f"{base}/iserver/marketdata/history").mock(
        return_value=httpx.Response(
            200, json={"data": [{"t": 1791552600000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}]}
        )
    )
    router.post(f"{base}/iserver/contract/rules").mock(
        return_value=httpx.Response(200, json=_fixture("rules.json"))
    )
    router.get(f"{base}/iserver/contract/{SPY}/algos").mock(
        return_value=httpx.Response(200, json=_fixture("algos.json"))
    )
    router.get(f"{base}/contract/trading-schedule").mock(
        return_value=httpx.Response(200, json=_fixture("trading_schedule.json"))
    )
