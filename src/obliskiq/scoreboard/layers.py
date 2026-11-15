"""Layers on the running system: the pattern lifecycle, the monthly monitoring looks and their
actions, and the capital each layer is given (research/preregistrations/scoreboard.md; DECISIONS
D62, D65, D149, D155, D156; MASTER_PLAN §5.9, §5.19).

**A layer** is a registry entry in ``shadow`` or ``active`` (``research/pattern_registry.yaml``),
or the thesis book. On the running system its status is its latest ``active`` or ``shadow`` event
(``layer_events``), else the registry's; it is frozen while a gap breach's freeze stands.

**The pattern lifecycle.** Every day each shadow entry's calls are graded at zero weight. It
becomes active only when its outcome is sufficient (or it was inconclusive positive and the pooled
estimate now is), its activation condition holds and its forward record has the minimum sample
and does not show it losing; the change is logged and the owner alerted. Only a new
pre-registered test retires an entry; nothing here does.

**Monitoring.** At each month end every active layer's month is looked at against its gap and its
twin boundaries (``monitor.py``), once; a gap breach freezes its new entries, a twin breach sends
it back to shadow at zero weight and its share to nothing. Each is alerted.

**Capital.** At each month end each layer's live-account share is set by the allocation rule
(``capital/allocation.py``); a change is recorded with its reason and reported.

An activation condition and a pooled re-test are each layer's own (D66, D155): a layer with no
condition or re-test registered here meets neither, so it neither activates nor steps.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import NormalDist
from typing import Any, Literal

import yaml

from ..capital.allocation import (
    ShareDecision,
    StepEvidence,
    StepRule,
    layer_key,
)
from ..domain.orders import ExecutionMode, Sleeve
from ..errors import ObliskError
from ..research.prereg import PatternsSpec, ScoreboardSpec
from ..store.h13 import BOOK, H13Look
from ..store.scoreboard import LayerEvent, LayerMonth, Look
from .grading import Record
from .monitor import (
    Backtest,
    LookResult,
    gap_look,
    is_look,
    look_alpha,
    months_since,
    twin_look,
)

ZERO = Decimal(0)
_NORMAL = NormalDist()
#: The actor of the scoreboard's own lifecycle events.
SCOREBOARD_ACTOR = "scoreboard"

type Status = Literal["shadow", "active"]
#: Which budget rule a layer's capital follows: the stock book's (D149) only for the entry that
#: declares itself the Hunter's systematic book; any other layer's otherwise.
type Budget = Literal["stock_book", "layer"]
_BUDGETS = ("stock_book", "layer")
_LIVE = frozenset({ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE})


class LayerRegistryError(ObliskError):
    """The research registry cannot be read as layers."""


@dataclass(frozen=True, slots=True)
class RegistryLayer:
    """A registry entry the scoreboard runs: in shadow or active, with its outcome and what its
    monitoring needs. ``estimate`` and ``interval`` are per its primary metric; ``metric`` names
    it."""

    id: str
    status: Status
    sleeve: Sleeve
    budget: Budget
    decision: str | None
    labels: tuple[str, ...]
    theta: float | None
    estimate: float | None
    interval: tuple[float, float] | None
    metric: str | None
    backtest: Backtest | None


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def registry_layers(path: Path) -> list[RegistryLayer]:
    """The entries of the registry in ``shadow`` or ``active``; none when there is no registry
    (an installed wheel). A malformed entry raises: it is never read as an outcome."""
    if not path.is_file():
        return []
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        found: list[RegistryLayer] = []
        for entry in data["patterns"]:
            status = entry.get("status")
            if status not in ("shadow", "active"):
                continue
            outcome = entry.get("outcome") or {}
            interval = outcome.get("interval")
            monitoring = entry.get("monitoring")
            budget = entry.get("budget", "layer")
            if budget not in _BUDGETS:
                raise ValueError(f"{entry['id']}: budget {budget!r} is not one of {_BUDGETS}")
            found.append(
                RegistryLayer(
                    id=str(entry["id"]),
                    status=status,
                    sleeve=Sleeve(entry.get("sleeve", Sleeve.SATELLITE.value)),
                    budget=budget,
                    decision=outcome.get("decision"),
                    labels=tuple(outcome.get("labels") or ()),
                    theta=_float(outcome.get("theta")),
                    estimate=_float(outcome.get("estimate")),
                    interval=None if interval is None else (float(interval[0]), float(interval[1])),
                    metric=outcome.get("metric"),
                    backtest=None
                    if monitoring is None
                    else Backtest(
                        float(monitoring["mean"]), float(monitoring["sd"]), float(monitoring["se"])
                    ),
                )
            )
        return found
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError, IndexError) as exc:
        raise LayerRegistryError(f"{path} cannot be read as layers: {exc}") from None


@dataclass(frozen=True, slots=True)
class LayerState:
    """A layer's status on the running system, when it last became active, and its freeze."""

    status: Status
    activated_at: datetime | None
    frozen: bool


def layer_state(registry_status: Status, events: Sequence[LayerEvent]) -> LayerState:
    status: Status = registry_status
    activated: datetime | None = None
    frozen = False
    for event in events:
        if event.event == "active":
            status, activated = "active", event.at
        elif event.event == "shadow":
            status, frozen = "shadow", False
        elif event.event == "freeze":
            frozen = True
        else:
            frozen = False
    return LayerState(status, activated, frozen)


# ── the pattern lifecycle ──


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promote: bool
    reason: str


def pooled(
    historical: tuple[float, float],
    forward: tuple[float, float],
    *,
    alpha: float,
) -> tuple[float, tuple[float, float]]:
    """The inverse-variance mean of two estimates given as (estimate, standard error), and its
    interval at level ``1 - 2 alpha``."""
    (h, sh), (f, sf) = historical, forward
    if sh <= 0 or sf <= 0:
        raise ValueError("a pooled estimate needs two positive standard errors")
    wh, wf = 1 / sh**2, 1 / sf**2
    mean = (h * wh + f * wf) / (wh + wf)
    se = (wh + wf) ** -0.5
    z = _NORMAL.inv_cdf(1 - alpha)
    return mean, (mean - z * se, mean + z * se)


def promotion_decision(
    layer: RegistryLayer,
    forward: Record,
    *,
    activation: bool,
    spec: PatternsSpec,
    alpha: float,
) -> PromotionDecision:
    """Whether a shadow entry becomes active now."""
    if forward.cohorts < spec.min_cohorts or forward.effective < float(spec.min_effective):
        return PromotionDecision(
            False,
            f"its forward record has {forward.cohorts} cohorts and {forward.effective:.1f} "
            f"effective observations ({spec.min_cohorts} and {spec.min_effective} needed)",
        )
    upper = None if forward.estimate.ci is None else forward.estimate.ci[1]
    if upper is None or upper <= float(spec.shadow_upper_above):
        return PromotionDecision(False, "its forward record shows it losing")
    sufficient = layer.decision == "sufficient"
    if not sufficient and layer.decision == "inconclusive" and "positive" in layer.labels:
        if (
            spec.pool
            and layer.metric == "signed_excess_log_return"
            and layer.estimate is not None
            and layer.interval is not None
            and layer.theta is not None
            and forward.estimate.mean is not None
            and forward.estimate.se is not None
        ):
            z = _NORMAL.inv_cdf(1 - alpha)
            se_h = (layer.interval[1] - layer.interval[0]) / (2 * z)
            _, (low, _) = pooled(
                (layer.estimate, se_h), (forward.estimate.mean, forward.estimate.se), alpha=alpha
            )
            sufficient = low >= layer.theta
        if not sufficient:
            return PromotionDecision(
                False, "inconclusive positive, and the pooled estimate is not sufficient"
            )
    if not sufficient:
        return PromotionDecision(False, f"its outcome is {layer.decision or 'not recorded'}")
    if not activation:
        return PromotionDecision(False, "its activation condition does not hold")
    return PromotionDecision(True, "a sufficient outcome, its activation condition and its record")


# ── monitoring ──


@dataclass(frozen=True, slots=True)
class MonthLook:
    """The looks a layer's month makes, and the events they call for."""

    looks: tuple[LookResult, ...]
    events: tuple[LayerEvent, ...]


def month_looks(
    layer: str,
    *,
    state: LayerState,
    months: Sequence[LayerMonth],
    backtest: Backtest | None,
    spec: ScoreboardSpec,
    month: str,
    at: datetime,
) -> MonthLook | None:
    """The looks of an active layer at ``month``'s end; ``None`` when it is not a look month
    (or the layer is not active)."""
    if state.status != "active" or state.activated_at is None:
        return None
    first = state.activated_at.strftime("%Y-%m")
    n = months_since(first, month)
    if n < 1 or not is_look(n, spec.monitoring):
        return None
    record = [m for m in months if first <= m.month <= month]
    found = {m.month for m in record}
    if len(found) != n:
        missing = f"{n - len(found)} of {n} months since activation have no record"
        alpha_gap = _spent(spec, n, "gap")
        alpha_twin = _spent(spec, n, "twin")
        return MonthLook(
            (
                LookResult("gap", n, None, None, alpha_gap, missing),
                LookResult("twin", n, None, None, alpha_twin, missing),
            ),
            (),
        )
    gap = gap_look(record, spec.monitoring)
    twin = (
        twin_look(record, backtest, spec.monitoring)
        if backtest is not None
        else LookResult(
            "twin",
            n,
            None,
            None,
            _spent(spec, n, "twin"),
            "the registry records no backtest distribution for it",
        )
    )
    events: list[LayerEvent] = []
    if twin.breached:
        events.append(
            LayerEvent(
                at, layer, "shadow", SCOREBOARD_ACTOR, f"twin breach at month {n}: {twin.detail}"
            )
        )
    elif gap.breached and not state.frozen:
        events.append(
            LayerEvent(
                at, layer, "freeze", SCOREBOARD_ACTOR, f"gap breach at month {n}: {gap.detail}"
            )
        )
    return MonthLook((gap, twin), tuple(events))


def _spent(spec: ScoreboardSpec, month: int, boundary: str) -> float:
    alpha = spec.monitoring.gap.alpha if boundary == "gap" else spec.monitoring.twin.alpha
    return look_alpha(float(alpha), month, spec.monitoring)


def as_record(layer: str, month: str, result: LookResult, at: datetime) -> Look:
    return Look(
        layer=layer,
        month=month,
        boundary=result.boundary,
        statistic=result.statistic,
        critical=result.critical,
        spent=result.spent,
        breached=result.breached,
        detail=result.detail,
        at=at,
    )


# ── capital ──


def stock_book_rule(spec: ScoreboardSpec, *, cash_floor: Decimal) -> StepRule:
    book = spec.budgets.stock_book
    return StepRule(
        first=book.first_share,
        step=book.share_step,
        ceiling=Decimal(1) - cash_floor,
        every_months=book.every_months,
        min_months_first=book.min_months_first,
        min_months_between=book.min_months_between,
        min_closed_first=book.min_closed_first,
        min_closed_between=book.min_closed_between,
    )


def layer_rule(spec: ScoreboardSpec, *, sleeve_budget: Decimal) -> StepRule:
    other = spec.budgets.other_layer
    return StepRule(
        first=other.first_share_of_sleeve * sleeve_budget,
        step=other.share_step_of_sleeve * sleeve_budget,
        ceiling=sleeve_budget,
        every_months=other.every_months,
        min_months_first=other.min_months_first,
        min_months_between=other.min_months_between,
        min_closed_first=other.min_closed_first,
        min_closed_between=other.min_closed_between,
    )


def step_evidence(
    *,
    layer: RegistryLayer,
    state: LayerState,
    months: Sequence[LayerMonth],
    looks: Sequence[Look],
    shares: Sequence[tuple[datetime, Decimal]],
    activation: bool,
    retest: bool | None,
) -> StepEvidence:
    """What the allocation rule reads of a registry layer at a month end: its live-account months
    and closed positions in all and since its last change of share."""
    live = [m for m in months if m.mode in _LIVE]
    last_change = shares[-1][0].strftime("%Y-%m") if shares else None
    since = [m for m in live if last_change is None or m.month > last_change]
    gap_looks = [look for look in looks if look.boundary == "gap" and look.evaluable]
    return StepEvidence(
        sufficient=layer.decision == "sufficient",
        activation=activation and state.status == "active",
        twin_breached=state.status == "shadow"
        and any(look.boundary == "twin" and look.breached for look in looks),
        gap_breach_outstanding=state.frozen or bool(gap_looks and gap_looks[-1].breached),
        live_months=len(live),
        months_since_step=len(since),
        closed=sum(m.closed for m in live),
        closed_since_step=sum(m.closed for m in since),
        steps_taken=max(0, sum(1 for _, s in shares if s > ZERO) - 1),
        retest_sufficient=retest,
    )


@dataclass(frozen=True, slots=True)
class ThesisBookCapital:
    """What the thesis book's live share rests on: the owner's exploration share, H13's step
    and ceiling, and the book's looks recorded on its paper and live records."""

    exploration: Decimal
    share_step: Decimal
    share_ceiling: Decimal
    looks: Sequence[H13Look]


def thesis_book_share(capital: ThesisBookCapital) -> ShareDecision:
    """The thesis book's live-account share under H13 (DECISIONS D156, D173): nothing once a
    live look sent it back to shadow, or while its paper record is below its boundary; else the
    owner's exploration share and a step for each live look that stepped, to the ceiling. A look
    not evaluable changes nothing."""
    book = [look for look in capital.looks if look.group == BOOK]
    for record, words in (("live", "its live record"), ("paper", "its paper record")):
        back = next((look for look in book if look.record == record and look.back), None)
        if back is not None:
            return ShareDecision(
                ZERO,
                True,
                f"{words} fell below H13's boundary at the look of {back.month}: back to shadow "
                f"({back.detail})",
            )
    steps = [look for look in book if look.record == "live" and look.step]
    share = min(capital.share_ceiling, capital.exploration + capital.share_step * len(steps))
    if not steps:
        return ShareDecision(
            share, True, "the owner's exploration share: no live look of H13 has stepped"
        )
    return ShareDecision(
        share,
        True,
        f"the owner's exploration share and {len(steps)} steps at H13's live looks "
        f"(the last at {steps[-1].month}), at most {capital.share_ceiling}",
    )


def allocation_layers(layers: Sequence[RegistryLayer]) -> dict[str, RegistryLayer]:
    """The registry layers by their allocation key (``sleeve:id``)."""
    return {layer_key(layer.sleeve, layer.id): layer for layer in layers}


#: A layer's activation condition and its pooled re-test, by registry id (D66, D155). Empty:
#: no layer is built that has either.
type Activation = Callable[[RegistryLayer], bool]
type Retest = Callable[[RegistryLayer, Sequence[LayerMonth]], bool | None]


def no_activation(_: RegistryLayer) -> bool:
    return False


def no_retest(_: RegistryLayer, __: Sequence[LayerMonth]) -> bool | None:
    return None
