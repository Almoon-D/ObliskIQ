"""``config/sources.yaml`` and ``config/llm.yaml`` refuse what cannot be meant: a consumer that
names nothing, a built source without its release rule or consumer, a declared one without its
condition, a series addressed the wrong way, duplicates, a halt feed mapped to a venue that does
not take it; and the records the sources produce refuse their impossible states.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import copy
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from obliskiq.config.loader import ConfigInvalid, load_config
from obliskiq.config.schema_llm import LlmConfig
from obliskiq.config.schema_sources import SourcesConfig
from obliskiq.domain.sources import CotReport, EventExtraction, ExtractionStatus

ROOT = Path(__file__).resolve().parents[2]


def shipped(name: str, section: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))
    return data[section]


SOURCES = shipped("sources.yaml", "sources")
LLM = shipped("llm.yaml", "llm")


def sources_with(change: Callable[[dict[str, Any]], None]) -> None:
    data = copy.deepcopy(SOURCES)
    change(data)
    SourcesConfig.model_validate(data)


def test_the_shipped_files_validate() -> None:
    assert SourcesConfig.model_validate(SOURCES).built()
    assert LlmConfig.model_validate(LLM).extractor_provider().key_env == "GEMINI_API_KEY"


def _provider(name: str, **fields: Any) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        data["providers"][name].update(fields)

    return change


def _series(position: int, **fields: Any) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        data["macro"]["series"][position].update(fields)

    return change


def _series_named(series_id: str, **fields: Any) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        (series,) = [s for s in data["macro"]["series"] if s["id"] == series_id]
        series.update(fields)

    return change


def _set(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return change


def _duplicate(path: tuple[str, ...]) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        node = data
        for key in path:
            node = node[key]
        node.append(copy.deepcopy(node[0]))

    return change


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_provider("fred", consumers=["view macro_board"]), "must be hypothesis:<id>"),
        (_provider("fred", consumers=["hypothesis:six"]), "named by its registry id"),
        (_provider("fred", consumers=["view:nowhere"]), "which is not declared"),
        (_provider("fred", credential="fred key"), "environment variable name"),
        (_provider("fred", release_date_policy=None), "a built source states"),
        (_provider("fred", consumers=[]), "names what consumes it"),
        (_provider("fred", activation="when wanted"), "its activation is null"),
        (_provider("eurostat", activation=None), "the condition that would build it"),
        (_provider("eurostat", consumers=["view:macro_board"]), "a declared source has no"),
        (_series(0, id="dgs10"), "upper-case letters"),
        (_series(0, value_vintage=None), "value_vintage is FRED's"),
        (_series(0, key="X"), "addressed by its id"),
        (_duplicate(("macro", "series")), "more than once"),
        (_set(("positioning", "percentile_min_weeks"), 999), "cannot exceed"),
        (_duplicate(("catalysts", "events")), "more than once"),
        (_series_named("ECB_DFR", source="eurostat"), "is no built macro source"),
        (
            _provider(
                "gdelt",
                status="declared",
                activation="later",
                consumers=[],
                schedule=[],
                release_date_policy=None,
                release_lag_days=None,
                stale_after_hours=None,
            ),
            "must be a built events source",
        ),
    ],
)
def test_a_source_that_cannot_be_meant_is_refused(
    change: Callable[[dict[str, Any]], None], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        sources_with(change)


def test_two_contracts_on_one_market_code_and_a_feed_s_odd_fields_are_refused() -> None:
    def same_code(data: dict[str, Any]) -> None:
        contracts = data["positioning"]["contracts"]
        contracts["COT_GOLD"]["code"] = contracts["COT_SILVER"]["code"]

    with pytest.raises(ValidationError, match="more than once"):
        sources_with(same_code)
    with pytest.raises(ValidationError, match="names its symbol"):
        sources_with(_set(("news", "feeds", "fed_press", "symbol"), "FED"))
    with pytest.raises(ValidationError, match="only an issuer's filings feed"):
        sources_with(_set(("news", "feeds", "fed_press", "read_filing"), True))
    with pytest.raises(ValidationError, match="does not declare as its credential"):
        sources_with(_set(("news", "feeds", "fed_press", "user_agent_env"), "OTHER_AGENT"))


def test_an_llm_file_naming_what_it_does_not_declare_is_refused() -> None:
    data = copy.deepcopy(LLM)
    data["models"]["gemini_flash_lite"]["provider"] = "nobody"
    data["extractor"]["model"] = "nothing"
    with pytest.raises(ValidationError, match=r"not declared.*is not a declared model"):
        LlmConfig.model_validate(data)


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    return target


def _edit(config: Path, old: str, new: str) -> None:
    path = config / "sources.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "markets: {NASDAQ: XNAS, NYSE: XNYS}",
            "markets: {NASDAQ: XNAS, LSE: XLON}",
            "does not accept a single-stock feed",
        ),
        (
            "markets: {NASDAQ: XNAS, NYSE: XNYS}",
            "markets: {NASDAQ: XNAS, NOWHERE: XZZZ}",
            "XZZZ is not a venue",
        ),
        ("time_venue: XNYS", "time_venue: XZZZ", "time_venue: XZZZ is not a venue"),
    ],
)
def test_the_halt_feed_maps_only_venues_that_take_it(
    config_copy: Path, old: str, new: str, message: str
) -> None:
    _edit(config_copy, old, new)
    with pytest.raises(ConfigInvalid, match=message):
        load_config(config_copy)


# ── the records ──


def test_a_report_released_before_its_positions_and_an_extraction_out_of_shape_are_refused() -> (
    None
):
    report: dict[str, Any] = {
        "contract_code": "088691",
        "report_date": date(2026, 10, 13),
        "release_date": date(2026, 10, 16),
        "market_name": "GOLD",
        "open_interest": 1000,
        "noncomm_long": 600,
        "noncomm_short": 400,
        "comm_long": None,
        "comm_short": 200,
        "nonrept_long": None,
        "nonrept_short": None,
        "source": "cftc_socrata",
        "collected_at": datetime(2026, 10, 23, tzinfo=UTC),
    }
    assert CotReport(**report).net_commercial_share is None
    with pytest.raises(ValidationError, match="released on or after"):
        CotReport(**report | {"release_date": date(2026, 10, 12)})
    extraction: dict[str, Any] = {
        "item_id": "a" * 64,
        "produced_at": datetime(2026, 10, 23, tzinfo=UTC),
        "provider": "gemini",
        "model_id": "m",
        "prompt_version": 1,
        "prompt_hash": "f" * 64,
        "status": ExtractionStatus.OK,
        "attempts": 1,
        "output": None,
        "error": None,
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": 0.0,
    }
    with pytest.raises(ValidationError, match="exactly when it is ok"):
        EventExtraction(**extraction)
    with pytest.raises(ValidationError, match="says why"):
        EventExtraction(**extraction | {"status": ExtractionStatus.FAILED})
