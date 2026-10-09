"""The entrypoint, the encoder, and the collector subprocess boundary.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import pytest

from obliskiq.server.cli import EXIT_FAILED, build_parser, main
from obliskiq.server.collectors import CollectorRunner, CollectorsDisabled
from obliskiq.server.encode import to_jsonable

# ── the entrypoint ──


def test_the_transport_must_be_named():
    """No default. Guessing means a misconfigured deploy binds to the wrong thing."""
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_only_the_two_transports_are_accepted():
    assert build_parser().parse_args(["stdio"]).transport == "stdio"
    assert build_parser().parse_args(["http"]).transport == "http"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sse"])


def test_http_refuses_to_start_unauthenticated_off_loopback(monkeypatch, tmp_path, capsys):
    """The refusal is the whole point of the tool. Nothing binds first."""
    monkeypatch.setenv("OBLISK_DB_PATH", str(tmp_path / "oblisk.db"))
    monkeypatch.setenv("OBLISK_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("OBLISK_MCP_PORT", "8000")
    monkeypatch.delenv("OBLISK_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OBLISK_ENV_FILE", str(tmp_path / "absent.env"))

    assert main(["http"]) == EXIT_FAILED
    assert "OBLISK_MCP_AUTH_TOKEN" in capsys.readouterr().err


def test_a_broken_config_path_fails_rather_than_defaulting(monkeypatch, tmp_path):
    monkeypatch.setenv("OBLISK_DB_PATH", str(tmp_path / "oblisk.db"))
    monkeypatch.setenv("OBLISK_ENV_FILE", str(tmp_path / "absent.env"))
    assert main(["stdio", "--config", str(tmp_path / "nope.yaml")]) == EXIT_FAILED


# ── the collector boundary ──


def test_the_argv_is_a_list_and_no_shell_is_involved(config):
    argv = CollectorRunner(config).build_argv("daily", only=["SPY"], skip=["macro"])
    assert isinstance(argv, list)
    assert argv[1:] == ["daily", "--only", "SPY", "--skip", "macro"]
    assert argv[0].endswith("obliskiq-collect")


def test_a_task_outside_the_allowed_list_is_rejected(config):
    with pytest.raises(ValueError, match="allowed_tasks"):
        CollectorRunner(config).build_argv("daily; rm -rf /")


def test_a_symbol_cannot_become_a_command(config):
    """A supplied symbol lands as one argv element, whatever it contains."""
    argv = CollectorRunner(config).build_argv("chain", only=["SPY; rm -rf /"])
    assert "SPY; rm -rf /" in argv
    assert argv.count("--only") == 1


def test_disabling_collectors_refuses_the_spawn(config):
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["server"]["collectors"]["enabled"] = False
    with pytest.raises(CollectorsDisabled, match="own timer"):
        CollectorRunner(Config(data)).build_argv("daily")


def test_the_collector_child_never_sees_the_auth_token():
    """A credential is not handed to a process that cannot need it."""
    from obliskiq.server.collectors import child_environment

    child = child_environment(
        {
            "OBLISK_MCP_AUTH_TOKEN": "secret",
            "OBLISK_MCP_HOST": "0.0.0.0",
            "OBLISK_DB_PATH": "/var/lib/obliskiq/o.db",
            "FRED_API_KEY": "k",
        }
    )
    assert "OBLISK_MCP_AUTH_TOKEN" not in child
    assert "OBLISK_MCP_HOST" not in child
    # What the collectors DO need survives.
    assert child["OBLISK_DB_PATH"] == "/var/lib/obliskiq/o.db"
    assert child["FRED_API_KEY"] == "k"


# ── the encoder ──


def test_nan_and_infinity_become_null_not_numbers():
    """A NaN serialised as a number is a lie in the shape of data."""
    assert to_jsonable(float("nan")) is None
    assert to_jsonable(float("inf")) is None
    assert to_jsonable(float("-inf")) is None
    assert to_jsonable(1.5) == 1.5


def test_dates_and_tuples_survive_the_wire():
    @dataclass(frozen=True)
    class Row:
        when: date
        stamped: datetime
        tags: tuple[str, ...]
        value: float | None

    encoded = to_jsonable(Row(date(2026, 1, 15), datetime(2026, 1, 15, 9, 30), ("a", "b"), None))
    assert encoded == {
        "when": "2026-01-15",
        "stamped": "2026-01-15T09:30:00",
        "tags": ["a", "b"],
        "value": None,
    }


def test_nested_structures_are_converted_all_the_way_down():
    encoded = to_jsonable({"rows": [{"d": date(2026, 1, 15), "v": float("nan")}]})
    assert encoded == {"rows": [{"d": "2026-01-15", "v": None}]}


def test_derived_properties_are_encoded_alongside_fields():
    """The engines put their verdicts in properties. Fields alone ship the
    evidence without the conclusion."""

    @dataclass(frozen=True)
    class Verdict:
        conditions_met: int
        required: int

        @property
        def fires(self) -> bool:
            return self.conditions_met >= self.required

        @property
        def _hidden(self) -> str:  # pragma: no cover - must not be encoded
            return "private"

    encoded = to_jsonable(Verdict(2, 2))
    assert encoded == {"conditions_met": 2, "required": 2, "fires": True}


def test_a_property_that_raises_is_omitted_not_fatal():
    @dataclass(frozen=True)
    class Fragile:
        value: int

        @property
        def ratio(self) -> float:
            return self.value / 0

    assert to_jsonable(Fragile(1)) == {"value": 1}


def test_an_uncalled_method_is_an_error_not_a_string():
    """Stringifying it would ship "<bound method ...>" as if it were data."""

    class Thing:
        def provenance(self) -> str:
            return "measured"

    with pytest.raises(TypeError, match="without being called"):
        to_jsonable(Thing().provenance)


def test_numpy_scalars_become_python_numbers():
    numpy = pytest.importorskip("numpy")
    assert to_jsonable(numpy.float64(2.5)) == 2.5
    assert to_jsonable(numpy.int64(3)) == 3
    assert to_jsonable(numpy.float64(math.nan)) is None
