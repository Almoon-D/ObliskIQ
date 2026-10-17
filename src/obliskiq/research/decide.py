"""The decision rule of a pre-registered test (DECISIONS D62).

Before any inference the data must pass the pre-registered adequacy checks; if one fails, or no
interval could be computed, the outcome is **not evaluable** and nothing is concluded. Otherwise,
with the interval [L, U] of the primary effect and the smallest effect worth having, theta,
exactly one of:

- **sufficient** — L >= theta;
- **insufficient** — U < theta (labelled *negative* when U < 0);
- **inconclusive** — L < theta <= U (labelled *positive* when L > 0, *sign uncertain*
  otherwise).

The three are exhaustive and mutually exclusive for any L <= U by construction: the code tests
L against theta, then U against theta, and nothing else. Power is a label beside the outcome,
never an outcome: an inconclusive result under low power is reported as such and is never read
as evidence against.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    NOT_EVALUABLE = "not evaluable"
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    INCONCLUSIVE = "inconclusive"


class Label(StrEnum):
    NEGATIVE = "negative"
    POSITIVE = "positive"
    SIGN_UNCERTAIN = "sign uncertain"
    LOW_POWER = "low power"
    POWER_UNKNOWN = "power unknown"


@dataclass(frozen=True, slots=True)
class AdequacyCheck:
    """One pre-registered check on the data, and what it found."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome, its labels, the numbers it rests on, and what it cannot conclude."""

    outcome: Outcome
    labels: tuple[Label, ...]
    interval: tuple[float, float] | None
    theta: float
    power: float | None
    target_power: float
    checks: tuple[AdequacyCheck, ...]
    cannot_conclude: tuple[str, ...]


#: What each outcome leaves open, stated in every report.
_LIMITS = {
    Outcome.NOT_EVALUABLE: (
        "Nothing: the data failed a pre-registered adequacy check or gave no interval, so no "
        "statement about the effect is made, for or against.",
    ),
    Outcome.SUFFICIENT: (
        "That the effect persists after the sample: the interval describes this history only.",
        "How large the effect is beyond its interval, or that it exceeds theta by any margin "
        "beyond L.",
    ),
    Outcome.INSUFFICIENT: (
        "That the effect is zero: only that an effect of theta or more is ruled out at this level.",
    ),
    Outcome.INCONCLUSIVE: (
        "Whether the effect reaches theta: the interval holds values on both sides of it.",
    ),
}


def decide(
    *,
    interval: tuple[float, float] | None,
    theta: float,
    power: float | None,
    target_power: float,
    checks: tuple[AdequacyCheck, ...],
) -> Decision:
    """Apply the rule of D62. ``theta`` must be positive (the smallest effect worth having)."""
    if not math.isfinite(theta) or theta <= 0:
        raise ValueError(f"theta is a positive effect, got {theta}")
    if not 0 < target_power < 1:
        raise ValueError(
            f"a target power is a probability strictly between 0 and 1, got {target_power}"
        )
    if interval is not None:
        low, high = interval
        if not (math.isfinite(low) and math.isfinite(high)) or low > high:
            raise ValueError(f"an interval is two finite ends with L <= U, got {interval}")
    labels: list[Label] = []
    if interval is None or not all(check.passed for check in checks):
        outcome = Outcome.NOT_EVALUABLE
    else:
        low, high = interval
        if low >= theta:
            outcome = Outcome.SUFFICIENT
        elif high < theta:
            outcome = Outcome.INSUFFICIENT
            if high < 0:
                labels.append(Label.NEGATIVE)
        else:
            outcome = Outcome.INCONCLUSIVE
            labels.append(Label.POSITIVE if low > 0 else Label.SIGN_UNCERTAIN)
        if power is None:
            labels.append(Label.POWER_UNKNOWN)
        elif power < target_power:
            labels.append(Label.LOW_POWER)
    limits = list(_LIMITS[outcome])
    if outcome is Outcome.INCONCLUSIVE and Label.LOW_POWER in labels:
        limits.append(
            "Anything against the effect: the test had low power to detect theta, so an "
            "inconclusive result is not evidence that the effect is absent."
        )
    return Decision(
        outcome=outcome,
        labels=tuple(labels),
        interval=interval,
        theta=theta,
        power=power,
        target_power=target_power,
        checks=checks,
        cannot_conclude=tuple(limits),
    )
