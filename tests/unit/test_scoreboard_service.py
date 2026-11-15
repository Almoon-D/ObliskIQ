"""The scoreboard as the service and the CLI run it: the locked rules, the daily, weekly and
monthly runs after the digest, the reports and their Telegram summary, the configuration of the
references and the promotion ladder.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import shutil
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.config.schema import ObliskConfig as Schema
from obliskiq.config.schema_scoreboard import ReferenceConfig
from obliskiq.domain.orders import ExecutionMode, Sleeve
from obliskiq.domain.tax import TaxYearSummary
from obliskiq.errors import PreregistrationError
from obliskiq.execution.modes import Modes
from obliskiq.orchestration.scoreboard_jobs import ScoreboardJobs, write_report
from obliskiq.reporting import scoreboard as report
from obliskiq.reporting.alerts import AlertBook
from obliskiq.reporting.reporter import Reporter
from obliskiq.research.prereg import ScoreboardPrereg, ScoreboardSpec, load_any
from obliskiq.research.registry import lock_path
from obliskiq.scoreboard.layers import RegistryLayer
from obliskiq.scoreboard.performance import IndexPoint, Series, SleevePnl
from obliskiq.scoreboard.promotion import Criterion, Verdict
from obliskiq.scoreboard.rules import H13_PATH, RULES_PATH, load_locked
from obliskiq.scoreboard.run import Changes, GradeBook, PerformanceView
from obliskiq.scoreboard.service import NEXT_LEVEL, Scoreboard, previous_month
from obliskiq.scoreboard.tca import analyse
from obliskiq.store import runs as run_repo
from obliskiq.store import scoreboard as repo
from obliskiq.store.scoreboard import AllocationEvent, Call, LayerEvent, LayerMonth, Look
from obliskiq.store.worker import StoreWorker

D = Decimal
NOW = datetime(2026, 11, 2, 22, tzinfo=UTC)


class Sent:
    def __init__(self, ok: bool = True) -> None:
        self.messages: list[tuple[str, str]] = []
        self.ok = ok

    async def send(self, subject: str, body: str) -> bool:
        self.messages.append((subject, body))
        return self.ok


def research_copy(repo_root: Path, tmp_path: Path, *, registry: str | None = None) -> Path:
    root = tmp_path / "repo"
    folder = root / "research" / "preregistrations"
    folder.mkdir(parents=True)
    for name in ("scoreboard.md", "scoreboard.lock", "thesis-book.md", "thesis-book.lock"):
        shutil.copy(repo_root / "research" / "preregistrations" / name, folder / name)
    (root / "research" / "pattern_registry.yaml").write_text(
        registry or "patterns: []\n", encoding="utf-8"
    )
    return root


# ── the locked rules ──


def test_the_rules_are_locked_at_their_merge(repo_root: Path) -> None:
    # The lock records the text merged in its own pull request (#71); `verify_lock` also asks
    # git for that commit, which a shallow checkout does not hold.
    path = repo_root / RULES_PATH
    lock = json.loads(lock_path(path).read_text(encoding="utf-8"))
    assert lock["merge_commit"] == "d4ac3bcaf4844d2902be2421ad9399581ac1dada"
    prereg = load_locked(path)
    assert lock["sha256"] == prereg.sha256
    assert prereg.spec.dependence.rho == D("0.25")
    assert prereg.spec.budgets.thesis_book == "thesis-book"


def test_rules_without_their_lock_or_changed_decide_nothing(
    repo_root: Path, tmp_path: Path
) -> None:
    root = research_copy(repo_root, tmp_path)
    path = root / RULES_PATH
    lock_path(path).unlink()
    with pytest.raises(PreregistrationError, match="no readable lock"):
        load_locked(path)
    shutil.copy(repo_root / "research" / "preregistrations" / "scoreboard.lock", lock_path(path))
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(PreregistrationError, match="not the text"):
        load_locked(path)
    h13 = root / H13_PATH
    shutil.copy(h13, path)
    lock_path(path).write_text(
        json.dumps({"sha256": __import__("hashlib").sha256(h13.read_bytes()).hexdigest()}),
        encoding="utf-8",
    )
    with pytest.raises(PreregistrationError, match="not the scoreboard"):
        load_locked(path)


def test_the_spec_refuses_incoherent_rules(repo_root: Path) -> None:
    found = load_any(repo_root / RULES_PATH)
    assert isinstance(found, ScoreboardPrereg)
    data = found.spec.model_dump(mode="json")
    low = json.loads(json.dumps(data))
    low["dependence"]["rho"] = "0.1"
    with pytest.raises(ValidationError, match="below its estimate"):
        ScoreboardSpec.model_validate(low)
    cheap = json.loads(json.dumps(data))
    cheap["calibration"]["min_effective"] = "10"
    with pytest.raises(ValidationError, match="grading minimum"):
        ScoreboardSpec.model_validate(cheap)
    late = json.loads(json.dumps(data))
    late["monitoring"]["first_look_month"] = 200
    with pytest.raises(ValidationError, match="first look"):
        ScoreboardSpec.model_validate(late)
    for boundary, field, value, message in (
        ("gap", "distribution", "normal", "Student"),
        ("twin", "action", "freeze_new_entries", "normal"),
    ):
        bad = json.loads(json.dumps(data))
        bad["monitoring"][boundary][field] = value
        with pytest.raises(ValidationError, match=message):
            ScoreboardSpec.model_validate(bad)


# ── configuration ──


def test_a_reference_s_weights_add_up_and_name_each_member_once() -> None:
    with pytest.raises(ValidationError, match="add up to 1"):
        ReferenceConfig.model_validate({"name": "x", "members": [{"conid": 1, "weight": "0.5"}]})
    with pytest.raises(ValidationError, match="twice"):
        ReferenceConfig.model_validate(
            {"name": "x", "members": [{"conid": 1, "weight": "0.5"}, {"conid": 1, "weight": "0.5"}]}
        )


def test_a_reference_member_must_be_in_the_universe(shipped_settings: ObliskConfig) -> None:
    data = shipped_settings.model_dump(mode="json")
    data["scoreboard"]["references"][0]["members"][0]["conid"] = 999999991
    with pytest.raises(ValidationError, match="no instrument of the universe pins"):
        Schema.model_validate(data)


# ── the reports ──


def inputs(**changes: Any) -> report.ReportInputs:
    from obliskiq.quant.inference import MeanEstimate
    from obliskiq.research.decide import Outcome
    from obliskiq.scoreboard.calibration import CalibrationState
    from obliskiq.scoreboard.grading import PanelRecord, ProbabilityScore, Record

    estimate = MeanEstimate(30, 0.01, 0.002, None, None, (0.007, 0.013))
    empty = MeanEstimate(0, None, None, None, None, None)
    from obliskiq.research.decide import Decision, Label

    decision = Decision(
        Outcome.SUFFICIENT, (Label.POSITIVE,), (0.007, 0.013), 0.005, 0.9, 0.8, (), ()
    )
    records = {
        "signal": Record("signal", 40, 2, 1, 37, 30, 31.5, 0.2, estimate, 0.005, decision),
        "young": Record("young", 3, 3, 0, 0, 0, 0.0, None, empty, 0.005, None),
    }
    panel = PanelRecord(
        "panel",
        400,
        10,
        9.3,
        estimate,
        empty,
        empty,
        empty,
        None,
        ProbabilityScore(400, 10, 9.3, 0.24, 0.25, 0.04, "climatology", ()),
    )
    book = GradeBook(
        date(2026, 11, 2),
        records,
        {"panel": CalibrationState("panel", False, 0.0, 400, 10, 9.3, 0.04, None, "too few")},
        panel,
    )
    view = PerformanceView(
        date(2026, 10, 1),
        date(2026, 11, 2),
        Series(
            "account",
            (IndexPoint(date(2026, 10, 1), D(1)), IndexPoint(date(2026, 11, 2), D("1.02"))),
            None,
        ),
        (Series("global equity", (), "no price of 1 on 2026-10-01"),),
        [SleevePnl(Sleeve.CORE, D(1), D(2), D(100), D("0.5"), (7,))],
    )
    base = report.ReportInputs(
        as_of=date(2026, 11, 2),
        mode=ExecutionMode.PAPER,
        grades=book,
        grades_missing=None,
        performance=view,
        tca=analyse([]),
        promotion=Verdict(
            ExecutionMode.LIVE_SMALL, (Criterion("8 weeks of paper", None, "undated"),)
        ),
        looks=[
            Look("p", "2026-10", "gap", -5.0, -3.0, 0.001, True, "lags", NOW),
            Look("p", "2026-10", "twin", None, None, 0.001, False, "missing", NOW),
            Look("q", "2026-10", "gap", 1.0, -3.0, 0.001, False, "fine", NOW),
        ],
        allocation=[AllocationEvent(NOW, "thesis_book", D("0.10"), "allocation rule", "first")],
        shares={"thesis_book": D("0.10")},
        tax=TaxYearSummary(
            tax_year=2026,
            income=D(1),
            gains=D(2),
            base=D(3),
            tax_before_credit=D("0.6"),
            foreign_tax_credit=D(0),
            tax_due=D("0.6"),
            carried_in_used=D(0),
            carried_out=(),
            expired=D(0),
            rate_table_covers_year=False,
            payment_date=date(2027, 6, 30),
        ),
        notes=("a note",),
    )
    return replace(base, **changes)


def test_the_weekly_report_states_counts_beside_effective_observations() -> None:
    text = report.weekly(inputs())
    assert text.startswith("# Scoreboard, week to 2026-11-02")
    assert "| signal | 40 | 37 | 2 | 1 | 30 | 31.5 |" in text
    assert "sufficient (positive)" in text
    assert "pending (below the minimum" in text
    assert "uncalibrated" in text
    assert "400 graded forecasts in 10 cohorts: 9.3 effective" in text
    assert "account: +2.00%" in text
    assert "no price of 1" in text
    assert "aspiration of 50% a year" in text
    assert "a distance, not a forecast" in text
    assert "1 unmarked" in text
    assert "[UNMEASURED]" not in text
    assert "8 weeks of paper: unmeasured" in text
    assert "a note" in text
    assert "Monitoring" not in text


def test_the_monthly_report_adds_looks_capital_and_tax() -> None:
    text = report.monthly(inputs())
    assert text.startswith("# Scoreboard, month to 2026-11-02")
    assert "p gap (2026-10): BREACH" in text
    assert "not evaluable" in text
    assert "inside" in text
    assert "thesis_book: 10.00% of investable capital" in text
    assert "tax due 0.60" in text
    assert "does not cover the year" in text
    bare = report.monthly(
        inputs(looks=[], shares={}, allocation=[], tax="no tax book", promotion=None)
    )
    assert "No active layer had a look" in bare
    assert "the reserve holds it all" in bare
    assert "Not computed: no tax book." in bare
    assert "top of the ladder" in bare
    assert "Not computed." in report.monthly(inputs(tax=None))


def test_a_report_says_what_it_could_not_compute() -> None:
    from obliskiq.scoreboard.performance import Series

    view = replace(inputs().performance, sleeves="no USD rate", account=Series("account", (), "x"))
    text = report.weekly(inputs(grades=None, grades_missing="no lock", performance=view))
    assert "Not graded: no lock." in text
    assert "sleeves: not computed (no USD rate)" in text
    empty = replace(inputs().grades, records={}, calibration={}, panel=None)
    text = report.weekly(inputs(grades=empty))
    assert "No layer has a call yet." in text
    assert "none sizes from them" in text
    assert "No forecast of the weekly panel" in text
    tca = analyse([])
    assert "0 broker fills measured" in "\n".join(report._tca(tca))


def test_the_telegram_summary_is_short() -> None:
    subject, body = report.summary(inputs(), kind="weekly")
    assert subject == "ObliskIQ weekly scoreboard 2026-11-02"
    assert "signal: 37 graded, 31.5 effective" in body
    assert "1 monitoring breaches" in body
    many = {f"l{i}": inputs().grades.records["young"] for i in range(9)}  # type: ignore[union-attr]
    _, body = report.summary(
        inputs(grades=replace(inputs().grades, records=many), looks=[], promotion=None),
        kind="monthly",
    )
    assert "and 3 more layers" in body
    assert "monitoring" not in body
    _, body = report.summary(inputs(grades=None, grades_missing="no lock"), kind="weekly")
    assert "Not graded: no lock." in body


# ── the service ──


@pytest.fixture
def board_parts(
    store_worker: StoreWorker,
    shipped_settings: ObliskConfig,
    calendars: Calendars,
    repo_root: Path,
    tmp_path: Path,
) -> tuple[Scoreboard, Modes, Path]:
    root = research_copy(repo_root, tmp_path)
    board = Scoreboard(
        shipped_settings, store_worker, root=root, calendars=calendars, tax=None, now=lambda: NOW
    )
    return board, Modes(store_worker, shipped_settings, profile="paper"), root


async def test_without_a_repository_the_scoreboard_decides_nothing(
    store_worker: StoreWorker, shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    board = Scoreboard(
        shipped_settings, store_worker, root=None, calendars=calendars, tax=None, now=lambda: NOW
    )
    assert "installed wheel" in str(board.rules())
    assert board.layers() == []
    assert isinstance(await board.grades(NOW.date()), str)
    changes, alerts = await board.daily(NOW.date())
    assert isinstance(changes, str)
    assert alerts == []
    month, alerts = await board.month_end("2026-10")
    assert isinstance(month, str)
    assert alerts == []


async def test_the_h13_file_must_be_the_thesis_book_s(
    board_parts: tuple[Scoreboard, Modes, Path],
) -> None:
    board, _, root = board_parts
    (root / H13_PATH).write_text("changed", encoding="utf-8")
    assert "thesis-book.md is not the text its lock records" in str(board.rules())
    (root / H13_PATH.with_suffix(".lock")).unlink()
    assert "no readable lock (thesis-book.lock)" in str(board.rules())
    shutil.copy(root / RULES_PATH, root / H13_PATH)
    shutil.copy(root / RULES_PATH.with_suffix(".lock"), root / H13_PATH.with_suffix(".lock"))
    assert "not a forward book" in str(board.rules())
    (root / RULES_PATH).write_text("changed", encoding="utf-8")
    assert "not the text" in str(board.rules())


async def test_the_daily_and_monthly_runs_and_their_alerts(
    board_parts: tuple[Scoreboard, Modes, Path], store_worker: StoreWorker
) -> None:
    board, modes, _ = board_parts
    await store_worker.run(repo.record_call, Call("signal", 1, "up", NOW - timedelta(days=60), "d"))
    changes, alerts = await board.daily(NOW.date())
    assert not isinstance(changes, str)
    assert alerts == []
    month, alerts = await board.month_end("2026-10")
    assert not isinstance(month, str)
    assert [s.layer for s in month.shares] == ["thesis_book"]
    assert [a.kind for a in alerts] == ["scoreboard.allocation"]
    flagged = board._alerts(Changes(events=[LayerEvent(NOW, "p", "freeze", "s", "gap")]), NOW)
    assert flagged[0].severity.value == "critical"
    assert not flagged[0].condition
    grades = await board.grades(NOW.date())
    assert isinstance(grades, GradeBook)
    assert "signal" in grades.records
    assert NEXT_LEVEL[ExecutionMode.SHADOW] is ExecutionMode.PAPER
    assert await board.verdict(ExecutionMode.LIVE) is None
    verdict = await board.verdict(await modes.current())
    assert verdict is not None
    assert not verdict.passed
    assert len(board.sessions(date(2026, 11, 2), date(2026, 11, 8))) == 5
    assert previous_month(date(2026, 1, 15)) == "2025-12"


async def test_the_report_inputs_and_the_tax_book(
    board_parts: tuple[Scoreboard, Modes, Path], store_worker: StoreWorker
) -> None:
    board, _modes, _ = board_parts
    await store_worker.run(
        repo.record_look, Look("p", "2026-10", "gap", 1.0, -2.0, 0.001, False, "d", NOW)
    )
    await store_worker.run(
        repo.record_allocation, AllocationEvent(NOW, "thesis_book", D("0.1"), "a", "r")
    )
    found = await board.inputs(
        mode=ExecutionMode.SHADOW, since=date(2026, 10, 1), as_of=NOW.date(), monthly=True
    )
    assert found.grades is not None
    assert found.tax == "no tax book"
    assert [look.layer for look in found.looks] == ["p"]
    assert len(found.allocation) == 1
    weekly = await board.inputs(
        mode=ExecutionMode.SHADOW, since=date(2026, 10, 26), as_of=NOW.date(), monthly=False
    )
    assert weekly.tax is None
    assert weekly.looks == []
    assert weekly.shares == {"thesis_book": D("0.1")}

    class Book:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        async def engine(self) -> Any:
            from obliskiq.errors import TaxError

            if self.fail:
                raise TaxError("no facts")
            from types import SimpleNamespace

            summary = SimpleNamespace(tax_year=2026)
            return SimpleNamespace(
                ledger=SimpleNamespace(settled=lambda: [summary], provisional=lambda _year: "p")
            )

    board._tax = Book(fail=True)  # type: ignore[assignment]
    assert await board._tax_summary(2026) == "no facts"
    board._tax = Book(fail=False)  # type: ignore[assignment]
    assert (await board._tax_summary(2026)).tax_year == 2026
    assert await board._tax_summary(2027) == "p"


# ── the jobs after the digest ──


async def test_the_jobs_report_once_a_week_and_once_a_month(
    board_parts: tuple[Scoreboard, Modes, Path], store_worker: StoreWorker, tmp_path: Path
) -> None:
    board, modes, _ = board_parts
    sent = Sent()
    reporter = Reporter(sent, AlertBook(tmp_path / "alerts.json"))
    jobs = ScoreboardJobs(
        board,
        worker=store_worker,
        modes=modes,
        reporter=reporter,
        data_dir=tmp_path,
        now=lambda: NOW,
    )
    await jobs.after_digest(date(2026, 11, 2))
    subjects = [s for s, _ in sent.messages]
    assert subjects.count("ObliskIQ weekly scoreboard 2026-11-02") == 1
    assert subjects.count("ObliskIQ monthly scoreboard 2026-11-02") == 1
    assert any("allocation" in s for s in subjects)
    assert (tmp_path / "reports" / "scoreboard-weekly-2026-11-02.md").is_file()
    assert (tmp_path / "reports" / "scoreboard-monthly-2026-11-02.md").is_file()
    await jobs.after_digest(date(2026, 11, 3))
    commands = [
        r.command for r in await store_worker.run(run_repo.runs_since, since=date(2026, 11, 1))
    ]
    assert commands.count("scoreboard:daily") == 2
    assert commands.count("scoreboard:weekly") == 1
    assert commands.count("scoreboard:monthly") == 1


async def test_an_undelivered_summary_is_degraded_and_rules_missing_are_skipped(
    store_worker: StoreWorker, shipped_settings: ObliskConfig, calendars: Calendars, tmp_path: Path
) -> None:
    board = Scoreboard(
        shipped_settings, store_worker, root=None, calendars=calendars, tax=None, now=lambda: NOW
    )
    reporter = Reporter(Sent(ok=False), AlertBook(tmp_path / "alerts.json"))
    jobs = ScoreboardJobs(
        board,
        worker=store_worker,
        modes=Modes(store_worker, shipped_settings, profile="paper"),
        reporter=reporter,
        data_dir=tmp_path,
        now=lambda: NOW,
    )
    assert await jobs.daily(NOW.date()) == "skipped"
    assert await jobs.weekly(NOW.date()) == "degraded"
    assert await jobs.monthly(NOW.date()) == "degraded"
    assert write_report(tmp_path, "weekly", NOW.date(), "x").read_text(encoding="utf-8") == "x"


def test_the_layer_months_record_the_mode(store_worker: StoreWorker) -> None:
    month = LayerMonth("l", "2026-10", ExecutionMode.PAPER, 0, None, None, None, NOW)
    assert month.mode is ExecutionMode.PAPER
    layer = RegistryLayer(
        "l", "shadow", Sleeve.SATELLITE, "layer", None, (), None, None, None, None, None
    )
    assert layer.status == "shadow"


async def test_the_daily_run_sends_what_the_lifecycle_raises(
    board_parts: tuple[Scoreboard, Modes, Path], store_worker: StoreWorker, tmp_path: Path
) -> None:
    board, modes, _ = board_parts
    sent = Sent()
    reporter = Reporter(sent, AlertBook(tmp_path / "alerts.json"))
    jobs = ScoreboardJobs(
        board,
        worker=store_worker,
        modes=modes,
        reporter=reporter,
        data_dir=tmp_path,
        now=lambda: NOW,
    )

    async def daily(_day: date) -> tuple[Changes, list[Any]]:
        event = LayerEvent(NOW, "pat", "active", "scoreboard", "promoted")
        return Changes(events=[event]), board._alerts(Changes(events=[event]), NOW)

    board.daily = daily  # type: ignore[method-assign]
    assert await jobs.daily(NOW.date()) == "ok"
    assert sent.messages[0][0] == "ObliskIQ alert: scoreboard.active"
