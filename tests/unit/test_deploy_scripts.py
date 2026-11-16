"""``deploy/update.sh``, ``backup.sh`` and ``restore.sh`` against stub docker, systemctl, rclone and
curl: an update that is healthy stays, one that is not (or that loses the kill switch's state) is
rolled back to the previous tag with the store restored and the owner alerted; a backup is checked,
compressed, pushed off the machine and pruned; a restore puts the store back.

The stubs keep their state in files: which tags start unhealthy, the kill switch the running
container reports, and every call made. ``sqlite3`` and ``zstd`` are stand-ins over Python's own
``sqlite3`` and ``compression.zstd``, so the tests need neither binary on the machine.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import os
import sqlite3
from contextlib import closing
import stat
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

DOCKER = r"""#!/usr/bin/env bash
# docker: pull, exec and run, answering from the stub's state files.
echo "docker $*" >> "$STUB/calls"
tag=$(sed -n 's/^OBLISK_IMAGE_TAG=//p' "$OBLISK_ETC/image.env")
case "$1" in
  pull) [ -e "$STUB/pull_fails" ] && exit 1; exit 0 ;;
  exec)
    shift 2
    case "$*" in
      "obliskiq health --since"*) grep -qx "$tag" "$STUB/bad_tags" 2>/dev/null && exit 1; exit 0 ;;
      "obliskiq status") exit 0 ;;
      "obliskiq kill status")
        [ -e "$STUB/down" ] && exit 1
        if [ -e "$STUB/kill_after" ] && [ "$tag" != "$(cat "$STUB/first_tag")" ]; then
          cat "$STUB/kill_after"
        else
          cat "$STUB/kill"
        fi
        exit 0 ;;
    esac ;;
  run) [ -e "$STUB/run_fails" ] && exit 1; cat "$STUB/kill"; exit 0 ;;
esac
exit 0
"""

SYSTEMCTL = r"""#!/usr/bin/env bash
# systemctl: a start of a new tag runs that tag's "migration" on the store.
echo "systemctl $*" >> "$STUB/calls"
tag=$(sed -n 's/^OBLISK_IMAGE_TAG=//p' "$OBLISK_ETC/image.env")
if [ "$1" = start ] && [ "$tag" != "$(cat "$STUB/first_tag")" ]; then
  "$SQLITE3" "$OBLISK_DATA/obliskiq.db" "CREATE TABLE IF NOT EXISTS migrated_by_new (x);"
fi
exit 0
"""

RECORDER = r"""#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$STUB/calls"
[ -e "$STUB/$(basename "$0")_fails" ] && exit 1
exit 0
"""

SQLITE3 = r"""#!{python}
# sqlite3 stand-in: `.backup 'dest'`, PRAGMA integrity_check and plain SQL.
import os, sqlite3, sys
db, command = sys.argv[1], sys.argv[2]
if os.path.exists(os.path.join(os.environ["STUB"], "corrupt")) and "integrity" in command:
    print("*** in database main *** page 3 is never used")
    sys.exit(0)
connection = sqlite3.connect(db)
if command.startswith(".backup"):
    target = sqlite3.connect(command.split(None, 1)[1].strip("'"))
    connection.backup(target)
    target.close()
else:
    for row in connection.execute(command):
        print("|".join(str(value) for value in row))
    connection.commit()
connection.close()
"""

ZSTD = r"""#!{python}
# zstd stand-in: `-q -T0 -o FILE` compresses stdin; `-q -d -c FILE` decompresses to stdout.
import sys
from compression import zstd
args = sys.argv[1:]
if "-d" in args:
    sys.stdout.buffer.write(zstd.decompress(open(args[-1], "rb").read()))
else:
    open(args[args.index("-o") + 1], "wb").write(zstd.compress(sys.stdin.buffer.read()))
"""


def _write(path: Path, text: str, *, executable: bool = False) -> None:
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Machine:
    """A machine's directories and the stubs, with the release 0.27.0 running."""

    def __init__(self, root: Path) -> None:
        self.etc = root / "etc"
        self.data = root / "data"
        self.backups = root / "backups"
        self.stub = root / "stub"
        self.bin = root / "bin"
        for folder in (self.etc / "owner", self.data, self.stub, self.bin):
            folder.mkdir(parents=True)
        _write(
            self.etc / "image.env",
            "OBLISK_IMAGE=ghcr.io/almoon-d/obliskiq\nOBLISK_IMAGE_TAG=0.27.0\n",
        )
        _write(
            self.etc / "obliskiq.env",
            "TELEGRAM_BOT_TOKEN=1:t\nTELEGRAM_CHAT_ID=42\nOBLISK_BACKUP_REMOTE=b2:obliskiq\n",
        )
        _write(self.etc / "owner" / "owner.yaml", "core:\n  variant: D50\n")
        _write(self.stub / "first_tag", "0.27.0")
        _write(self.stub / "kill", "kill switch OFF\n")
        _write(self.stub / "calls", "")
        _write(self.data / "PAUSED", '{"actor": "owner"}\n')
        with closing(sqlite3.connect(self.data / "obliskiq.db")) as db:
            db.execute("CREATE TABLE book (conid INTEGER)")
            db.execute("INSERT INTO book VALUES (163606916)")
            db.commit()
        python = sys.executable
        _write(self.bin / "docker", DOCKER, executable=True)
        _write(self.bin / "systemctl", SYSTEMCTL, executable=True)
        _write(self.bin / "rclone", RECORDER, executable=True)
        _write(self.bin / "curl", RECORDER, executable=True)
        _write(self.bin / "logger", RECORDER, executable=True)
        _write(self.bin / "sqlite3", SQLITE3.replace("{python}", python), executable=True)
        _write(self.bin / "zstd", ZSTD.replace("{python}", python), executable=True)

    def env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STUB": str(self.stub),
            "OBLISK_ETC": str(self.etc),
            "OBLISK_DATA": str(self.data),
            "OBLISK_BACKUP_DIR": str(self.backups),
            "OBLISK_UID": str(os.getuid()),
            "OBLISK_UPDATE_TIMEOUT": "1",
            "OBLISK_UPDATE_POLL": "0",
            "SQLITE3": str(self.bin / "sqlite3"),
            "ZSTD": str(self.bin / "zstd"),
            "HOME": str(self.stub),
        }

    def run(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(DEPLOY / script), *args],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def tag(self) -> str:
        text = (self.etc / "image.env").read_text(encoding="utf-8")
        return text.split("OBLISK_IMAGE_TAG=")[1].strip()

    def calls(self) -> list[str]:
        return (self.stub / "calls").read_text(encoding="utf-8").splitlines()

    def tables(self) -> set[str]:
        with closing(sqlite3.connect(self.data / "obliskiq.db")) as db:
            return {row[0] for row in db.execute("SELECT name FROM sqlite_master")}


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    return Machine(tmp_path)


# ── update.sh ──


def test_a_healthy_update_stays(machine: Machine) -> None:
    done = machine.run("update.sh", "v0.28.0")
    assert done.returncode == 0, done.stderr
    assert machine.tag() == "0.28.0"
    calls = machine.calls()
    assert calls[0] == "docker pull ghcr.io/almoon-d/obliskiq:0.28.0"
    assert "systemctl stop obliskiq.service obliskiq-cockpit.service" in calls
    assert "systemctl start obliskiq.service obliskiq-cockpit.service" in calls
    assert any(c.startswith("docker exec obliskiq obliskiq health --since") for c in calls)
    assert len(list(machine.backups.glob("obliskiq-*.tar.zst"))) == 1
    assert "migrated_by_new" in machine.tables()
    assert not any(c.startswith("curl") for c in calls)
    assert not any(c.startswith("rclone") for c in calls)  # the update's backup stays local


def test_an_unhealthy_update_is_rolled_back_with_the_store_restored(machine: Machine) -> None:
    _write(machine.stub / "bad_tags", "0.28.0\n")
    done = machine.run("update.sh", "0.28.0")
    assert done.returncode == 1
    assert machine.tag() == "0.27.0"
    assert machine.tables() == {"book"}  # the new version's migration is undone
    calls = machine.calls()
    assert calls.count("systemctl stop obliskiq.service obliskiq-cockpit.service") == 2
    assert calls.count("systemctl start obliskiq.service obliskiq-cockpit.service") == 2
    (alert,) = [c for c in calls if c.startswith("curl")]
    assert "update to 0.28.0 failed (no fresh, healthy heartbeat within 1 s)" in alert
    assert "rolled back to 0.27.0" in alert
    assert (machine.data / "PAUSED").exists()


def test_an_update_that_loses_the_kill_switch_is_rolled_back(machine: Machine) -> None:
    _write(machine.stub / "kill", "kill switch ON\n")
    _write(machine.stub / "kill_after", "kill switch OFF\n")
    done = machine.run("update.sh", "0.28.0")
    assert done.returncode == 1
    assert machine.tag() == "0.27.0"
    (alert,) = [c for c in machine.calls() if c.startswith("curl")]
    assert "the kill switch was 'kill switch ON' and is 'kill switch OFF'" in alert


def test_the_kill_switch_is_read_from_a_stopped_service_too(machine: Machine) -> None:
    _write(machine.stub / "down", "")
    _write(machine.stub / "bad_tags", "0.28.0\n")
    done = machine.run("update.sh", "0.28.0")
    assert done.returncode == 1
    assert any(
        c.startswith("docker run --rm") and c.endswith("kill status") for c in machine.calls()
    )


def test_a_kill_switch_that_cannot_be_read_refuses_the_update(machine: Machine) -> None:
    _write(machine.stub / "down", "")
    _write(machine.stub / "run_fails", "")
    done = machine.run("update.sh", "0.28.0")
    assert done.returncode == 1
    assert machine.tag() == "0.27.0"
    assert not any(c.startswith("systemctl") for c in machine.calls())
    assert any("kill switch's state cannot be read" in c for c in machine.calls())


def test_a_tag_that_cannot_be_pulled_changes_nothing(machine: Machine) -> None:
    _write(machine.stub / "pull_fails", "")
    done = machine.run("update.sh", "0.28.0")
    assert done.returncode == 1
    assert machine.tag() == "0.27.0"
    assert not any(c.startswith("systemctl") for c in machine.calls())
    assert any("could not be pulled" in c for c in machine.calls() if c.startswith("curl"))


def test_a_failed_backup_refuses_the_update(machine: Machine) -> None:
    _write(machine.stub / "corrupt", "")
    done = machine.run("update.sh", "0.28.0")
    assert done.returncode == 1
    assert machine.tag() == "0.27.0"
    assert not any(c.startswith("systemctl") for c in machine.calls())


def test_the_running_tag_is_not_updated_again(machine: Machine) -> None:
    done = machine.run("update.sh", "0.27.0")
    assert done.returncode == 0
    assert "already on 0.27.0" in done.stderr
    assert machine.calls() == []


def test_an_update_needs_an_installed_machine(machine: Machine) -> None:
    (machine.etc / "image.env").unlink()
    assert machine.run("update.sh", "0.28.0").returncode == 2
    _write(machine.etc / "image.env", "nothing\n")
    assert machine.run("update.sh", "0.28.0").returncode == 2


# ── backup.sh and restore.sh ──


def test_a_backup_is_checked_compressed_pushed_and_pruned(machine: Machine) -> None:
    machine.backups.mkdir()
    for n in range(16):
        old = machine.backups / f"obliskiq-2026010{n:02d}T000000Z.tar.zst"
        old.write_bytes(b"")
        os.utime(old, (n, n))
    done = machine.run("backup.sh")
    assert done.returncode == 0, done.stderr
    archive = Path(done.stdout.strip())
    assert archive.exists()
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert f"rclone copy {archive} b2:obliskiq" in machine.calls()
    assert len(list(machine.backups.glob("obliskiq-*.tar.zst"))) == 14


def test_a_backup_without_an_off_machine_target_alerts(machine: Machine) -> None:
    _write(machine.etc / "obliskiq.env", "TELEGRAM_BOT_TOKEN=1:t\nTELEGRAM_CHAT_ID=42\n")
    done = machine.run("backup.sh")
    assert done.returncode == 1
    assert any("OBLISK_BACKUP_REMOTE is not set" in c for c in machine.calls())


def test_a_push_that_fails_alerts(machine: Machine) -> None:
    _write(machine.stub / "rclone_fails", "")
    done = machine.run("backup.sh")
    assert done.returncode == 1
    assert any("could not be pushed" in c for c in machine.calls() if c.startswith("curl"))


def test_a_damaged_copy_is_never_kept_as_a_backup(machine: Machine) -> None:
    _write(machine.stub / "corrupt", "")
    done = machine.run("backup.sh")
    assert done.returncode == 1
    assert list(machine.backups.glob("*.tar.zst")) == []
    assert any("failed its integrity check" in c for c in machine.calls())


def test_an_alert_without_telegram_goes_to_the_journal_only(machine: Machine) -> None:
    (machine.etc / "obliskiq.env").unlink()
    done = machine.run("backup.sh")
    assert done.returncode == 1
    assert not any(c.startswith("curl") for c in machine.calls())
    assert any(c.startswith("logger") for c in machine.calls())


def test_a_restore_puts_the_store_and_the_settings_back(machine: Machine) -> None:
    archive = Path(machine.run("backup.sh", "--local-only").stdout.strip())
    with closing(sqlite3.connect(machine.data / "obliskiq.db")) as db:
        db.execute("DELETE FROM book")
        db.commit()
    (machine.data / "PAUSED").unlink()
    _write(machine.data / "KILL_SWITCH", "{}")
    _write(machine.etc / "owner" / "owner.yaml", "core:\n  variant: A\n")
    done = machine.run("restore.sh", str(archive))
    assert done.returncode == 0, done.stderr
    with closing(sqlite3.connect(machine.data / "obliskiq.db")) as db:
        assert db.execute("SELECT conid FROM book").fetchall() == [(163606916,)]
    assert (machine.data / "PAUSED").exists()
    assert not (machine.data / "KILL_SWITCH").exists()
    assert "D50" in (machine.etc / "owner" / "owner.yaml").read_text(encoding="utf-8")
    assert machine.calls()[-1] == "systemctl start obliskiq.service obliskiq-cockpit.service"


def test_a_damaged_archive_is_not_restored(machine: Machine) -> None:
    archive = Path(machine.run("backup.sh", "--local-only").stdout.strip())
    _write(machine.stub / "corrupt", "")
    done = machine.run("restore.sh", str(archive))
    assert done.returncode == 1
    assert any("holds a damaged obliskiq.db" in c for c in machine.calls())
    assert machine.calls()[-1].startswith("systemctl start")


def test_a_restore_needs_an_archive(machine: Machine) -> None:
    assert machine.run("restore.sh", str(machine.backups / "absent.tar.zst")).returncode == 2


# ── the units ──


def test_the_units_run_the_image_the_update_writes() -> None:
    for unit in ("obliskiq.service", "obliskiq-cockpit.service"):
        text = (DEPLOY / "systemd" / unit).read_text(encoding="utf-8")
        assert "EnvironmentFile=/etc/obliskiq/image.env" in text
        assert "${OBLISK_IMAGE}:${OBLISK_IMAGE_TAG}" in text
        assert "--env-file /etc/obliskiq/obliskiq.env" in text
        assert "--log-driver journald" in text
    cockpit = (DEPLOY / "systemd" / "obliskiq-cockpit.service").read_text(encoding="utf-8")
    assert "cockpit serve" in cockpit
    assert "--network host" in cockpit
    timer = (DEPLOY / "systemd" / "obliskiq-backup.timer").read_text(encoding="utf-8")
    assert "Persistent=true" in timer
    update = (DEPLOY / "update.sh").read_text(encoding="utf-8")
    assert "CONTAINER=obliskiq\n" in update


def test_the_machine_env_template_names_only_known_variables(repo_root: Path) -> None:
    known = {
        line.split("=", 1)[0]
        for line in (repo_root / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    }
    names = {
        line.split("=", 1)[0]
        for line in (DEPLOY / "obliskiq.env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    }
    assert names - known == {"OBLISK_BACKUP_REMOTE"}
    assert all(
        not line.split("=", 1)[1] or line.startswith("OBLISK_IBKR_")
        for line in (DEPLOY / "obliskiq.env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )
