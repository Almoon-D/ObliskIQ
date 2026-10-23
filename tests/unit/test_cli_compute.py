"""``obliskiq compute``: the steps by hand, their exit codes and run records, a dry run, a failing
step, and the isolation the layer promises: it runs with every socket refused, and its import
graph names no network client (lite's ``tests/test_compute_isolation.py`` and
``tests/test_compute_cli.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
import io
import logging
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from measure_rig import AS_OF, seed

from obliskiq.calendars.service import Calendars
from obliskiq.cli import compute as compute_cli
from obliskiq.cli.app import run
from obliskiq.config.loader import load_config
from obliskiq.domain.measurement import EntityKind
from obliskiq.grid.equity_grid import EquityGridBuilder
from obliskiq.orchestration.runlog import EXIT_DEGRADED, EXIT_FAILED
from obliskiq.store import measurements, open_store
from obliskiq.store import runs as runs_repo

SOURCE_DIR = Path(__file__).resolve().parents[2] / "src"
#: Everything that could open a connection (``ssl`` and ``asyncio`` open nothing on their own).
NETWORK_MODULES = ("httpx", "httpcore", "requests", "urllib.request", "socket")
#: The modules whose isolation is asserted.
ISOLATED = ("obliskiq.orchestration.compute",)


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path, calendars: Calendars
) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    # The session's calendars, already built: building every venue again costs seconds.
    monkeypatch.setattr(
        compute_cli, "Calendars", SimpleNamespace(from_settings=lambda *_, **__: calendars)
    )
    return path


@pytest.fixture
def seeded(data_dir: Path, config_dir: Path) -> Path:
    settings = load_config(config_dir).settings
    with open_store(settings, data_dir) as store:
        seed(store, settings)
    return data_dir


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue() + err.getvalue()


def test_all_runs_every_step_and_a_young_store_is_degraded_not_failed(
    seeded: Path, config_dir: Path
) -> None:
    status, out = _run("compute", "all", "--as-of", AS_OF.isoformat())
    assert status == EXIT_DEGRADED, out
    assert "solve-iv: 1 underlying(s) solved" in out
    assert "equity grid for 2026-10-22" in out
    assert "macro board:" in out
    assert "most_extended:" in out
    assert "census for 2026-10-22: untested" in out
    assert out.rstrip().endswith("run: degraded")
    settings = load_config(config_dir).settings
    with open_store(settings, seeded) as store:
        for kind in EntityKind:
            assert measurements.measured_session(store, kind, as_of=AS_OF) == AS_OF, kind
        (record,) = runs_repo.runs_for(store, command="compute all", session_date=AS_OF)
        assert record.status == "degraded"
        assert [step.name for step in runs_repo.steps_of(store, record.run_id)] == [
            "solve-iv",
            "measure",
            "rankings",
            "census",
        ]


def test_a_dry_run_computes_and_writes_nothing(seeded: Path, config_dir: Path) -> None:
    status, out = _run("compute", "measure", "--as-of", AS_OF.isoformat(), "--dry-run")
    assert status in (0, EXIT_DEGRADED), out
    assert "equity grid for 2026-10-22" in out
    settings = load_config(config_dir).settings
    with open_store(settings, seeded) as store:
        assert measurements.measured_session(store, EntityKind.EQUITY_CELL, as_of=AS_OF) is None
        assert runs_repo.runs_for(store, command="compute measure", session_date=AS_OF) == []


def test_rankings_without_a_built_grid_is_degraded(data_dir: Path) -> None:
    status, out = _run("compute", "rankings", "--as-of", AS_OF.isoformat())
    assert status == EXIT_DEGRADED
    assert "no ranked row" in out


def test_a_step_that_raises_fails_the_run_and_the_next_step_still_runs(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(EquityGridBuilder, "build", broken)
    status, out = _run("compute", "all", "--as-of", AS_OF.isoformat())
    assert status == EXIT_FAILED
    assert "measure: error RuntimeError: boom" in out
    assert "census for 2026-10-22" in out


# ── isolation ──


def test_the_whole_compute_run_works_with_every_socket_refused(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the kernel does under ``PrivateNetwork=true``, done in process: no network socket, no
    name resolution, no connection. A local pair stays (asyncio's loop wakes itself through one,
    and a private network namespace keeps them)."""
    real_socket = socket.socket

    def network_socket(family: int = socket.AF_INET, *args: Any, **kwargs: Any) -> socket.socket:
        if family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("obliskiq compute opened a network socket")
        return real_socket(family, *args, **kwargs)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("obliskiq compute reached for the network")

    monkeypatch.setattr(socket, "socket", network_socket)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    status, out = _run("compute", "all", "--as-of", AS_OF.isoformat())
    assert status == EXIT_DEGRADED, out


def test_the_socket_refusal_would_notice_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive control: without it a layer that did nothing would pass the test above."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("socket opened")

    monkeypatch.setattr(socket, "create_connection", refuse)
    with pytest.raises(AssertionError, match="socket opened"):
        socket.create_connection(("127.0.0.1", 9))


def _module_path(name: str) -> Path | None:
    relative = Path(*name.split("."))
    for candidate in (
        SOURCE_DIR / relative.with_suffix(".py"),
        SOURCE_DIR / relative / "__init__.py",
    ):
        if candidate.is_file():
            return candidate
    return None


def _imports(path: Path, module: str) -> set[str]:
    """Every module imported at module scope (function bodies and TYPE_CHECKING blocks run only
    when called, so they cannot open anything on import)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if isinstance(child, ast.If) and getattr(child.test, "id", None) == "TYPE_CHECKING":
                continue
            if isinstance(child, ast.Import):
                found.update(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    base = package.rsplit(".", child.level - 1)[0] if child.level > 1 else package
                    prefix = f"{base}.{child.module}" if child.module else base
                else:
                    prefix = child.module or ""
                found.add(prefix)
                found.update(f"{prefix}.{alias.name}" for alias in child.names)
            else:
                visit(child)

    visit(tree)
    return found


def _reachable(entry: str) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    queue = [entry]
    while queue:
        module = queue.pop()
        if module in graph:
            continue
        path = _module_path(module)
        if path is None:
            continue
        graph[module] = _imports(path, module)
        parts = module.split(".")
        parents = [".".join(parts[: n + 1]) for n in range(1, len(parts) - 1)]
        queue += [
            n for n in [*graph[module], *parents] if n.startswith("obliskiq") and n not in graph
        ]
    return graph


def test_the_compute_graph_is_walkable() -> None:
    """The vacuity guard: a walk that found nothing would pass every assertion below."""
    graph = _reachable(ISOLATED[0])
    assert len(graph) > 20
    assert "obliskiq.grid.equity_grid" in graph
    assert "obliskiq.store.db" in graph


@pytest.mark.parametrize("forbidden", NETWORK_MODULES)
def test_nothing_the_compute_run_imports_names_a_network_client(forbidden: str) -> None:
    graph = _reachable(ISOLATED[0])
    offenders = {
        module: sorted(
            name for name in imports if name == forbidden or name.startswith(f"{forbidden}.")
        )
        for module, imports in graph.items()
        if any(name == forbidden or name.startswith(f"{forbidden}.") for name in imports)
    }
    assert not offenders, f"the compute run imports {forbidden} through {offenders}"


def test_the_compute_run_imports_without_an_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """If httpx were not installed at all, the compute run would still import."""
    import builtins
    import importlib

    for name in [
        m
        for m in sys.modules
        if m.startswith("obliskiq") or m.split(".")[0] in ("httpx", "httpcore")
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".")[0] in ("httpx", "httpcore", "requests"):
            raise ImportError(f"{name} is not installed on this host")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    importlib.import_module(ISOLATED[0])
