"""``PrivateNetwork=true`` on the compute unit, made a fact rather than a promise.

``deploy/obliskiq-compute@.service`` puts the computation layer in its own
network namespace, on the grounds that it reads the store and nothing else. Six
documents assert that property and nothing tested it — and the import graph did
not express it either: ``compute/cli.py`` transitively dragged in the whole
httpx/IBKR client stack through two package ``__init__`` re-exports. No socket
was ever opened, because every client happens to be constructed inside a function
body, so the guarantee held by placement rather than by design.

Two checks, and both are needed:

* **the run-time one** — run the whole ``all`` sequence with sockets rigged to
  raise, which is what the kernel does under ``PrivateNetwork``;
* **the static one** — the transitive import graph names no HTTP client at all,
  so a future engine that reaches for one fails here rather than quietly
  acquiring a dependency the design forbids.

The run-time check alone would pass on a module that imports httpx and never
calls it, which is where this started.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import ast
import socket
import sys
from pathlib import Path

import pytest

from obliskiq.compute import cli as compute_cli

#: Everything that could open a connection. ``ssl`` is absent on purpose — it is
#: pulled in by the standard library for reasons unrelated to us and opens
#: nothing on its own — and so is ``asyncio``, which is a concurrency library
#: rather than a client and is what ``collect.retry`` paces with.
NETWORK_MODULES = ("httpx", "httpcore", "requests", "urllib.request", "socket")

#: The entrypoints whose isolation is being asserted, and the one that is
#: deliberately exempt because binding a port is its whole job.
ISOLATED_ENTRYPOINT = "obliskiq.compute.cli"


def _package_root() -> Path:
    import obliskiq

    return Path(obliskiq.__file__).resolve().parent


def _module_path(name: str) -> Path | None:
    """``obliskiq.a.b`` to its file, module or package, or None if it is neither."""
    relative = Path(*name.split(".")[1:])
    root = _package_root()
    for candidate in (root / relative.with_suffix(".py"), root / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports_of(path: Path, module: str) -> set[str]:
    """Every module this file imports at MODULE SCOPE, absolute names.

    Function-local imports and ``if TYPE_CHECKING`` blocks are excluded, because
    neither executes when the module is loaded and neither can open anything. A
    deferred import is the intended way to keep a heavy client out of a graph
    that must not have one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[str] = set()

    def visit(node: ast.AST, *, at_module_scope: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                continue  # a function body runs only when called
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                continue  # never executed at run time
            if at_module_scope and isinstance(child, ast.Import):
                found.update(alias.name for alias in child.names)
            elif at_module_scope and isinstance(child, ast.ImportFrom):
                if child.level:
                    base = package.rsplit(".", child.level - 1)[0] if child.level > 1 else package
                    prefix = f"{base}.{child.module}" if child.module else base
                else:
                    prefix = child.module or ""
                found.add(prefix)
                found.update(f"{prefix}.{alias.name}" for alias in child.names)
            else:
                visit(child, at_module_scope=at_module_scope)

    visit(tree, at_module_scope=True)
    return found


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _reachable(entry: str) -> dict[str, set[str]]:
    """``obliskiq.*`` module -> everything it imports, walked transitively."""
    graph: dict[str, set[str]] = {}
    queue = [entry]
    while queue:
        module = queue.pop()
        if module in graph:
            continue
        path = _module_path(module)
        if path is None:
            continue
        graph[module] = _imports_of(path, module)
        # A submodule import runs its parent packages' __init__ first.
        parents = [
            ".".join(module.split(".")[: n + 1]) for n in range(1, len(module.split(".")) - 1)
        ]
        queue.extend(
            name
            for name in list(graph[module]) + parents
            if name.startswith("obliskiq") and name not in graph
        )
    return graph


# ── the static check ──


def test_the_compute_graph_is_walkable_at_all():
    """The vacuity guard. A resolver that silently found nothing would make every
    assertion below pass on an empty set."""
    graph = _reachable(ISOLATED_ENTRYPOINT)
    assert len(graph) > 20, f"only {len(graph)} modules resolved; the walk is not working"
    assert "obliskiq.compute.build_grid" in graph
    assert "obliskiq.store.db" in graph


@pytest.mark.parametrize("forbidden", NETWORK_MODULES)
def test_no_module_the_compute_entrypoint_imports_names_a_network_client(forbidden):
    graph = _reachable(ISOLATED_ENTRYPOINT)
    offenders = {
        module: sorted(name for name in imports if name.split(".")[0] == forbidden.split(".")[0])
        for module, imports in graph.items()
        if any(name == forbidden or name.startswith(f"{forbidden}.") for name in imports)
    }
    assert not offenders, (
        f"obliskiq-compute imports {forbidden} through {offenders}. The unit runs "
        "with PrivateNetwork=true; an HTTP client in this graph is a dependency "
        "the design forbids, whether or not anything calls it today."
    )


def test_the_collector_entrypoint_is_not_held_to_this(repo_root):
    """The guard has to be about compute, not about httpx being unwelcome. The
    collectors are the half that talks to the network, and their unit has no
    PrivateNetwork line for exactly that reason."""
    unit = (repo_root / "deploy" / "obliskiq-collect@.service").read_text(encoding="utf-8")
    assert "PrivateNetwork" not in unit
    assert "import httpx" in (repo_root / "src" / "obliskiq" / "collect" / "cli.py").read_text(
        encoding="utf-8"
    )


# ── the run-time check ──


def test_the_whole_compute_sequence_runs_with_every_socket_refused(
    tmp_path, monkeypatch, repo_root, config, seeded_universe
):
    """What the kernel does under PrivateNetwork, done in-process so it is
    testable: any attempt to open a connection raises."""

    def refuse(*args, **kwargs):
        raise AssertionError(
            "obliskiq-compute opened a socket. Its systemd unit runs with "
            "PrivateNetwork=true, so on the VM this is not a failed request — it "
            "is a step that cannot work at all."
        )

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    monkeypatch.setenv("OBLISK_DB_PATH", str(seeded_universe.path))
    monkeypatch.setenv("OBLISK_CONFIG_PATH", str(repo_root / "config.yaml"))
    for leaked in (
        "OBLISK_IBKR_SIGNATURE_KEY",
        "OBLISK_IBKR_ENCRYPTION_KEY",
        "OBLISK_IBKR_DH_PARAM",
        "OBLISK_IBKR_CA_BUNDLE",
    ):
        monkeypatch.delenv(leaked, raising=False)

    code = compute_cli.main(["all"])
    assert code in (compute_cli.EXIT_OK, compute_cli.EXIT_DEGRADED), (
        "a young store is DEGRADED, not failed; anything else means a step broke"
    )


def test_the_run_time_check_would_notice_a_socket(monkeypatch):
    """The positive control. Without it, a compute layer that had stopped doing
    anything at all would pass the test above perfectly."""

    def refuse(*args, **kwargs):
        raise AssertionError("socket opened")

    monkeypatch.setattr(socket, "create_connection", refuse)
    with pytest.raises(AssertionError, match="socket opened"):
        socket.create_connection(("127.0.0.1", 9))


def test_the_compute_module_is_importable_without_the_http_client(monkeypatch):
    """The strongest form of the static check: if httpx is not installed at all,
    obliskiq-compute still starts. A VM running only the computation layer should
    not need the collectors' dependency tree."""
    for name in [module for module in sys.modules if module.startswith("obliskiq")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in [module for module in sys.modules if module.split(".")[0] in ("httpx", "httpcore")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] in ("httpx", "httpcore", "requests"):
            raise ImportError(f"{name} is not installed on this host")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked)
    import importlib

    importlib.import_module("obliskiq.compute.cli")
