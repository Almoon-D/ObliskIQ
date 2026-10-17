"""The strategies a pre-registration may name, built from its specification.

``fixed_weights`` rebalances to the same weights at every date (the band decides what trades);
``buy_and_hold`` buys its weights on the first date and never trades again, so its tax waits for
the horizon. Session 4c adds the core's variants beside them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from ..errors import PreregistrationError
from .simulate import MarketView

type Weights = Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class FixedWeights:
    """The same target weights at every rebalance date."""

    name: str
    weights: Weights

    def targets(self, view: MarketView, holdings: Mapping[str, Decimal]) -> Weights | None:
        del view, holdings  # the targets never change
        return self.weights


@dataclass(frozen=True, slots=True)
class BuyAndHold:
    """Its weights on the first date, then nothing."""

    name: str
    weights: Weights

    def targets(self, view: MarketView, holdings: Mapping[str, Decimal]) -> Weights | None:
        del view
        return None if holdings else self.weights


type Builder = Callable[[str, Mapping[str, Any]], FixedWeights | BuyAndHold]


def _weights(name: str, spec: Mapping[str, Any]) -> Weights:
    raw = spec.get("weights")
    if not isinstance(raw, Mapping) or not raw:
        raise PreregistrationError(f"strategy {name} needs a mapping of weights")
    try:
        return {str(key): Decimal(str(value)) for key, value in raw.items()}
    except ArithmeticError:
        raise PreregistrationError(f"strategy {name} has a weight that is not a number") from None


#: The strategies a pre-registration may name, by ``strategy``.
STRATEGIES: Final[Mapping[str, Builder]] = {
    "fixed_weights": lambda name, spec: FixedWeights(name, _weights(name, spec)),
    "buy_and_hold": lambda name, spec: BuyAndHold(name, _weights(name, spec)),
}


def build_strategy(name: str, spec: Mapping[str, Any]) -> FixedWeights | BuyAndHold:
    kind = spec.get("strategy")
    builder = STRATEGIES.get(str(kind))
    if builder is None:
        raise PreregistrationError(
            f"strategy {name} names {kind!r}; known: {', '.join(sorted(STRATEGIES))}"
        )
    return builder(name, spec)
