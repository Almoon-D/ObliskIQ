"""``obliskiq llm``: the extractor's status and one run by hand, with and without its key, in
process and without a network; and the service's verdict of an extractor run.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx
from httpx import Response

from obliskiq.cli import llm as llm_cli
from obliskiq.cli.app import EXIT_OK, run
from obliskiq.config.loader import load_config
from obliskiq.domain.sources import NewsItem
from obliskiq.llm.extractor import ExtractorRun
from obliskiq.orchestration.service import _judge_extraction
from obliskiq.store import open_store
from obliskiq.store import sources as source_repo

NOW = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)
COMPLETIONS = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(llm_cli, "utc_now", lambda: NOW)
    settings = load_config(config_dir).settings
    with open_store(settings, path) as store:
        source_repo.save_news_items(
            store,
            [
                NewsItem(
                    item_id="a" * 64,
                    feed="fed_press",
                    kind="central_bank",
                    region="us",
                    symbol=None,
                    guid=None,
                    link="https://example.com/a",
                    title="Fed raises rates",
                    summary=None,
                    published_at=None,
                    collected_at=NOW - timedelta(hours=1),
                    raw="<item/>",
                    fallback_tone=None,
                )
            ],
        )
    return path


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue() + err.getvalue()


def test_status_names_the_model_the_prompt_and_the_queue(data_dir: Path) -> None:
    status, out = _run("llm", "status")
    assert status == EXIT_OK
    assert "model gemini-3.1-flash-lite (gemini); prompt hash " in out
    assert "items waiting: 1 (at most 100 a run)" in out
    assert "spent this month: 0.0000 of 5.00 USD" in out
    assert "extractions of this prompt: none" in out


def test_extract_without_its_key_is_skipped_and_with_it_reads_the_queue(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, out = _run("llm", "extract")
    assert status == 2
    assert "extract: skipped: GEMINI_API_KEY is unset: no call made, 1 items wait" in out
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-a-secret")
    event = {
        "entities": ["Federal Reserve"],
        "region": "us",
        "sector": None,
        "type": "monetary_policy",
        "tone": -0.2,
        "novelty": 0.5,
        "sources": ["https://example.com/a"],
    }
    reply = {
        "choices": [{"message": {"content": json.dumps(event)}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 80},
    }
    with respx.mock as router:
        router.post(COMPLETIONS).mock(return_value=Response(200, json=reply))
        status, out = _run("llm", "extract")
    assert status == EXIT_OK
    assert "extract: ok: 1 items read" in out
    assert "ok 1, invalid 0, failed 0" in out
    _, after = _run("llm", "status")
    assert "extractions of this prompt: 1 ok" in after
    assert "items waiting: 0" in after


@pytest.mark.parametrize(
    ("result", "verdict"),
    [
        (ExtractorRun("skipped", "no key"), "skipped"),
        (ExtractorRun("ok", "3 items read", ok=3), "ok"),
        (ExtractorRun("ok", "3 items read", ok=2, failed=1), "degraded"),
        (ExtractorRun("budget_spent", "spent", ok=1, cost_usd=0.1234567), "degraded"),
    ],
)
def test_the_service_judges_an_extractor_run(result: ExtractorRun, verdict: str) -> None:
    outcome = _judge_extraction(result)
    assert outcome.name == "extract"
    assert outcome.status == verdict
    assert outcome.reason == result.reason
    assert outcome.detail["cost_usd"] == round(result.cost_usd, 6)
