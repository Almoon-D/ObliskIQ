"""``obliskiq kill on|off|status`` and ``obliskiq mode show|set``, run in-process.

The kill switch must work when everything else fails: ``kill on`` with a configuration that
does not load still sets the file flag.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
from pathlib import Path

import pytest

from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.execution.killswitch import kill_switch_path


@pytest.fixture(autouse=True)
def _restore_root_logging():
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> Path:
    path = tmp_path / "data"
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "paper")
    return path


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue(), err.getvalue()


def test_kill_on_status_and_off(data_dir: Path) -> None:
    status, out, _ = _run("kill", "status")
    assert (status, out.splitlines()[0]) == (EXIT_OK, "kill switch OFF")
    status, out, _ = _run("kill", "on", "--reason", "drill")
    assert status == EXIT_OK
    assert "kill switch ON" in out
    assert "warning" not in out
    assert kill_switch_path(data_dir).exists()
    status, out, _ = _run("kill", "status")
    assert "kill switch ON" in out
    assert "database flag  on" in out
    status, out, _ = _run("kill", "off", "--reason", "drill over")
    assert (status, out.strip()) == (EXIT_OK, "kill switch OFF")
    assert not kill_switch_path(data_dir).exists()


def test_kill_off_needs_a_reason(data_dir: Path) -> None:
    with pytest.raises(SystemExit):
        _run("kill", "off")


def test_kill_on_works_when_the_configuration_does_not_load(
    data_dir: Path, config_dir: Path
) -> None:
    (config_dir / "owner.yaml").write_text("account: [broken\n", encoding="utf-8")
    status, out, err = _run("kill", "on")
    assert status == EXIT_OK
    assert "kill switch ON" in out
    assert "database flag was not set" in out
    assert "obliskiq:" in err
    assert kill_switch_path(data_dir).exists()
    # Every other command still refuses a configuration that does not load.
    assert _run("kill", "status")[0] == EXIT_INVALID


def test_kill_on_without_a_data_directory_fails_loudly(
    data_dir: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OBLISK_DATA_DIR")
    status, _, err = _run("kill", "on")
    assert status == EXIT_INVALID
    assert "OBLISK_DATA_DIR" in err
    (config_dir / "owner.yaml").write_text("account: [broken\n", encoding="utf-8")
    status, _, err = _run("kill", "on")
    assert status == EXIT_INVALID
    assert "OBLISK_DATA_DIR" in err


def test_kill_on_with_an_unwritable_data_directory_reports_it(data_dir: Path) -> None:
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    data_dir.write_text("a file, not a directory", encoding="utf-8")
    status, out, _ = _run("kill", "on")
    assert status == EXIT_INVALID
    assert "NOT set" in out


def test_kill_on_with_one_flag_failing_warns(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import obliskiq.cli.app as app

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(app, "set_file_flag", refuse)
    status, out, _ = _run("kill", "on")
    assert status == EXIT_OK
    assert "warning: the file flag could not be written" in out


def test_kill_off_that_leaves_a_flag_fails(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import obliskiq.cli.app as app

    _run("kill", "on")
    monkeypatch.setattr(app, "clear_file_flag", lambda _data_dir: None)
    status, out, _ = _run("kill", "off", "--reason", "r")
    assert status == EXIT_INVALID
    assert "still ON" in out


def test_kill_status_with_an_unreadable_database(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import obliskiq.cli.app as app
    from obliskiq.errors import StoreError

    def refuse(*args: object, **kwargs: object) -> None:
        raise StoreError("locked")

    monkeypatch.setattr(app, "open_store", refuse)
    status, out, _ = _run("kill", "status")
    assert status == EXIT_OK
    assert "kill switch ON" in out
    assert "unreadable" in out


def test_mode_show_and_set(data_dir: Path) -> None:
    assert _run("mode", "show")[1].strip() == "execution mode shadow"
    status, out, _ = _run("mode", "set", "paper", "--reason", "ten clean days")
    assert (status, out.strip()) == (EXIT_OK, "execution mode shadow -> paper")
    assert _run("mode", "show")[1].strip() == "execution mode paper"
    assert _run("mode", "set", "paper", "--reason", "again")[1].strip() == (
        "execution mode is already paper"
    )
    status, out, _ = _run("mode", "set", "live", "--reason", "too soon")
    assert status == EXIT_INVALID
    assert "promotion_gate_unavailable" in out
    status, out, _ = _run("mode", "set", "shadow", "--reason", "pause")
    assert (status, out.strip()) == (EXIT_OK, "execution mode paper -> shadow")


def test_a_store_failure_is_reported_without_a_traceback(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import obliskiq.cli.app as app
    from obliskiq.errors import StoreError

    def refuse(*args: object, **kwargs: object) -> None:
        raise StoreError("the store cannot open")

    monkeypatch.setattr(app, "open_store", refuse)
    status, _, err = _run("kill", "off", "--reason", "r")
    assert status == EXIT_INVALID
    assert "cannot open" in err
