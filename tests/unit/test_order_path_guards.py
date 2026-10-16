"""Structural guards on the order path.

* I1: under ``src/`` only the pre-trade gate (``execution/pretrade.py``) constructs a
  ``PretradeClearance`` or records one; the store reads back what the gate recorded and nothing
  else builds one.
* The broker-write endpoints are spelled only in the order client, and only the write boundary
  calls its placement.
* No plain market order: the order body can only carry a limit-capped order type, for any terms.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
import re
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from obliskiq.domain.orders import (
    AlgoLimitPrice,
    LimitPrice,
    OrderSide,
    OrderTerms,
    StopLimitPrice,
    TimeInForce,
)
from obliskiq.ibkr.orders import LIMIT_CAPPED_ORDER_TYPES, order_body

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "src" / "obliskiq"
GATE = "execution/pretrade.py"
STORE = "store/orders.py"
ORDER_CLIENT = "ibkr/orders.py"
CLEARANCE = "PretradeClearance"

#: Endpoints that write to the broker (place, modify, cancel, reply, suppress).
_WRITE_ENDPOINT = re.compile(r"iserver/(reply|questions|account/\{account\}/orders?)")


def _modules() -> dict[str, ast.Module]:
    return {
        path.relative_to(SOURCE_DIR).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(SOURCE_DIR.rglob("*.py"))
    }


def clearance_constructions(tree: ast.Module) -> list[tuple[int, str]]:
    """Every way ``tree`` could build a clearance: a call of the class, of one of its model
    constructors, an alias of the name, or the name as a string."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == CLEARANCE) or (
                isinstance(func, ast.Attribute) and func.attr == CLEARANCE
            ):
                found.append((node.lineno, "call"))
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == CLEARANCE
            ):
                found.append((node.lineno, func.attr))
        elif isinstance(node, ast.alias) and node.name == CLEARANCE and node.asname:
            found.append((getattr(node, "lineno", 0), "alias"))
        elif isinstance(node, ast.Constant) and node.value == CLEARANCE:
            found.append((node.lineno, "string"))
    return found


def test_only_the_gate_constructs_a_clearance() -> None:
    offenders = {}
    for module, tree in _modules().items():
        found = clearance_constructions(tree)
        if module == GATE:
            continue
        if module == STORE:
            # Reading back the record the gate wrote is not issuing one.
            found = [item for item in found if item[1] != "model_validate_json"]
        if found:
            offenders[module] = found
    assert not offenders, f"only {GATE} may construct a PretradeClearance: {offenders}"


def test_the_scan_catches_each_way_of_building_one() -> None:
    source = (
        "from x import PretradeClearance as P\n"
        "a = PretradeClearance(x=1)\n"
        "b = PretradeClearance.model_construct()\n"
        "c = domain.PretradeClearance(x=1)\n"
        "d = getattr(domain, 'PretradeClearance')\n"
    )
    kinds = [kind for _, kind in clearance_constructions(ast.parse(source))]
    assert sorted(kinds) == ["alias", "call", "call", "model_construct", "string"]


def test_only_the_gate_records_a_clearance() -> None:
    callers = {
        module
        for module, tree in _modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
        and (node.attr if isinstance(node, ast.Attribute) else node.id) == "record_clearance"
    }
    assert callers <= {GATE}, f"only {GATE} records clearances: {sorted(callers)}"


def test_the_write_endpoints_live_in_the_order_client() -> None:
    holders = {
        module
        for module, tree in _modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _WRITE_ENDPOINT.search(node.value)
    }
    assert holders == {ORDER_CLIENT}


def _callers(attribute: str) -> set[str]:
    return {
        module
        for module, tree in _modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == attribute and module != ORDER_CLIENT
    }


def _receivers(attribute: str) -> dict[str, set[str]]:
    """For each module, the names of what ``attribute`` is called on (``self._client``)."""
    found: dict[str, set[str]] = {}
    for module, tree in _modules().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == attribute:
                receiver = node.value
                name = (
                    receiver.attr
                    if isinstance(receiver, ast.Attribute)
                    else (receiver.id if isinstance(receiver, ast.Name) else "?")
                )
                found.setdefault(module, set()).add(name)
    return found


def test_only_the_write_boundary_places_or_modifies() -> None:
    assert _callers("place_idempotent") == {"execution/boundary.py"}
    # The order client's modify is the boundary's to call; the exit coordinator modifies a stop
    # through the boundary.
    assert _callers("modify") == {"execution/boundary.py", "execution/exits.py"}
    assert _receivers("modify")["execution/exits.py"] == {"_boundary"}
    assert _receivers("modify")["execution/boundary.py"] == {"_client"}


def test_no_market_order_type_is_spelled_in_the_order_client() -> None:
    text = (SOURCE_DIR / ORDER_CLIENT).read_text(encoding="utf-8")
    for spelling in ('"MKT"', '"MARKET"', '"STP"', '"MIT"', '"MOC"', '"TRAIL"'):
        assert spelling not in text
    assert {"LMT", "STOP_LIMIT"} == LIMIT_CAPPED_ORDER_TYPES


_AMOUNTS = st.decimals(min_value=Decimal("0.01"), max_value=Decimal(100000), places=2)


@st.composite
def _terms(draw: st.DrawFn) -> OrderTerms:
    limit = draw(_AMOUNTS)
    price = draw(
        st.sampled_from(
            [
                LimitPrice(limit=limit),
                StopLimitPrice(stop=limit, limit=limit),
                AlgoLimitPrice(strategy="Adaptive", limit=limit),
            ]
        )
    )
    conversion = draw(st.booleans()) and price.kind == "limit"
    side = draw(st.sampled_from(OrderSide))
    in_cash = conversion or (side is OrderSide.BUY and draw(st.booleans()))
    size = draw(_AMOUNTS)
    return OrderTerms(
        conid=draw(st.integers(min_value=1, max_value=10**9)),
        side=side,
        quantity=None if in_cash else size,
        cash_quantity=size if in_cash else None,
        price=price,
        tif=draw(st.sampled_from(TimeInForce)),
        venue=draw(st.sampled_from(["SMART", "IBIS", "LSE"])),
        conversion=conversion,
    )


@given(_terms())
def test_every_order_body_is_limit_capped(terms: OrderTerms) -> None:
    body = order_body(terms, coid="c", account_id="DU1")
    assert body["orderType"] in LIMIT_CAPPED_ORDER_TYPES
    assert body["price"] > 0
    assert sum(key in body for key in ("quantity", "cashQty", "fxQty")) == 1
