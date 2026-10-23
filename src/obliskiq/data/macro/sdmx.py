"""The SDMX-JSON decoder and SDMX period parsing, network-free.

Ported from lite's ``macro/fetchers.py`` (``_from_sdmx_json`` and its helpers). Three shapes are
properties of SDMX rather than of one publisher, and each was met live (lite,
references/macro-playbook.md):

* **two envelopes.** SDMX-JSON 1.0 (the ECB) puts ``dataSets`` and ``structure`` at the top of the
  body; 2.0 (the OECD, and the BIS) nests them under ``data``, with the structure sometimes a list
  (``structures``). Reading one shape found nothing in the other, and nothing read as an empty
  series, so the envelope is detected rather than configured.
* **the time axis by exact id.** ``TIME_HORIZ`` is a content dimension that once sat before
  ``TIME_PERIOD``; a prefix match took it for the time axis. Only ``TIME_PERIOD`` and ``TIME``
  are time.
* **an under-specified key.** A key that leaves a dimension open returns several series under one
  id; merging them is refused, naming the dimensions to pin.

A period that does not parse drops its observation: one on the wrong date is worse than one
missing. A period maps to the first day of the period it names; when it became public is the
release rule's question, never this module's.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from typing import Any

from ...errors import UpstreamError
from ...logging_setup import get_logger
from ...util.numeric import safe_float
from ...util.timeutil import parse_date

log = get_logger("data.macro")

#: The SDMX ids that mean "this dimension is the time axis": an exact match, never a prefix.
TIME_DIMENSION_IDS = frozenset({"TIME_PERIOD", "TIME"})
#: Months in a quarter and in a half year, for ``2026-Q1`` and ``2026-S2``.
MONTHS_PER_QUARTER = 3
MONTHS_PER_HALF = 6

type Point = tuple[date, float | None]


def parse_period(period: str) -> date | None:
    """An SDMX period's first day: ``2026-01-15``, ``2026-01``, ``2026-Q1``, ``2026-S2``,
    ``2026``."""
    text = str(period).strip()
    full = parse_date(text)
    if full is not None:
        return full
    try:
        if "-Q" in text:
            year, quarter = text.split("-Q")
            return date(int(year), (int(quarter) - 1) * MONTHS_PER_QUARTER + 1, 1)
        if "-S" in text:
            year, half = text.split("-S")
            return date(int(year), (int(half) - 1) * MONTHS_PER_HALF + 1, 1)
        parts = text.split("-")
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1)
        if len(parts) == 1 and parts[0].isdigit():
            return date(int(parts[0]), 1, 1)
    except ValueError:
        return None
    return None


def envelope(body: Any) -> tuple[list[Any], dict[str, Any]]:
    """The data sets and the structure, from either SDMX-JSON envelope."""
    if not isinstance(body, dict):
        return [], {}
    if body.get("dataSets") is not None:
        return list(body.get("dataSets") or []), dict(body.get("structure") or {})
    nested = body.get("data") or {}
    if not isinstance(nested, dict):
        return [], {}
    structures = nested.get("structures") or []
    structure = structures[0] if structures else (nested.get("structure") or {})
    return list(nested.get("dataSets") or []), dict(structure or {})


def _refuse_under_specified(dimensions: list[Any], time_position: int, series_id: str) -> None:
    plural = [
        str(dimension.get("id"))
        for position, dimension in enumerate(dimensions)
        if position != time_position and len(dimension.get("values") or []) > 1
    ]
    if plural:
        raise UpstreamError(
            f"SDMX series {series_id} is under-specified: the response varies over {plural} as "
            "well as time, so it holds several series; merging them would put all their values "
            f"under one id. Pin those dimensions in the key of {series_id}."
        )


def _time_index(key: str, time_position: int) -> int | None:
    parts = key.split(":")
    if time_position >= len(parts):
        return None
    try:
        return int(parts[time_position])
    except ValueError:
        return None


def decode(body: Any, series_id: str) -> list[Point]:
    """Every observation of the one series ``body`` holds, oldest first."""
    data_sets, structure = envelope(body)
    if not data_sets:
        return []
    dimensions = (structure.get("dimensions") or {}).get("observation") or []
    periods: list[str] = []
    time_position = 0
    for position, dimension in enumerate(dimensions):
        if str(dimension.get("id", "")).upper() in TIME_DIMENSION_IDS:
            periods = [str(value.get("id")) for value in dimension.get("values") or []]
            time_position = position
            break
    if not periods:
        return []
    _refuse_under_specified(dimensions, time_position, series_id)
    observations: dict[str, Any] = {}
    series = data_sets[0].get("series") or {}
    if len(series) > 1:
        log.warning(
            "SDMX response carries several series for one key; only the first is used",
            extra={"series_id": series_id, "series_returned": len(series)},
        )
    for entry in list(series.values())[:1]:
        observations.update(entry.get("observations") or {})
    if not observations:
        observations = data_sets[0].get("observations") or {}
    points: list[Point] = []
    for key, payload in observations.items():
        index = _time_index(key, time_position)
        if index is None or index >= len(periods):
            continue
        reference = parse_period(periods[index])
        if reference is None:
            continue
        value = payload[0] if isinstance(payload, list) and payload else None
        points.append((reference, safe_float(value, None)))
    points.sort(key=lambda point: point[0])
    return points
