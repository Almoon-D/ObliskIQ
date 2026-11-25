"""Changing a setting: the panel's text edited with its comments kept, validated with the whole
configuration, permitted for its channel, written atomically and audited (MASTER_PLAN §5.17).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from obliskiq.config import load_config
from obliskiq.config.audit import SettingsAuditLog
from obliskiq.config.changes import (
    CLI,
    MCP,
    SettingsRefusal,
    apply,
    check_remote,
    file_hash,
    owner_overrides,
    parse_value,
    propose,
    settings_fingerprint,
)
from obliskiq.config.loader import ConfigInvalid
from obliskiq.config.owner_file import OwnerFileError, edit_text, render_value, write_atomically
from obliskiq.config.schema_server import RemoteSettingsConfig

AT = datetime(2026, 11, 16, 9, tzinfo=UTC)

PANEL = """\
# The owner's settings.

account:
  # The base currency.
  base_currency: EUR

jurisdiction: es  # Spain

thesis:
  caps:
    position: "0.25"
  # the entry window
  entry_window_days: 20

market_data:
  plan:
    - opra_l1
    - eurex_core_l1
"""


# ── the editor ──


def test_a_value_is_replaced_in_place_and_every_comment_stays() -> None:
    text = edit_text(PANEL, ["thesis", "caps", "position"], "0.20")
    assert '    position: "0.20"' in text
    assert "# The owner's settings." in text
    assert "  # The base currency." in text
    assert "  # the entry window" in text
    assert yaml.safe_load(text)["thesis"]["caps"]["position"] == "0.20"


def test_a_trailing_comment_is_kept() -> None:
    text = edit_text(PANEL, ["jurisdiction"], "fr")
    assert 'jurisdiction: "fr"  # Spain' in text


def test_a_block_list_is_replaced_by_one_flow_line() -> None:
    text = edit_text(PANEL, ["market_data", "plan"], ["opra_l1"])
    assert "  plan: [opra_l1]" in text
    assert "eurex_core_l1" not in text


def test_a_missing_key_is_added_at_the_end_of_its_block() -> None:
    text = edit_text(PANEL, ["thesis", "caps", "sector"], "0.30")
    lines = text.splitlines()
    assert lines[lines.index('    position: "0.25"') + 1] == '    sector: "0.30"'
    data = yaml.safe_load(text)
    assert data["thesis"]["caps"] == {"position": "0.25", "sector": "0.30"}
    assert data["thesis"]["entry_window_days"] == 20


def test_missing_parents_are_added_under_an_existing_section() -> None:
    text = edit_text(PANEL, ["account", "extra", "deep"], 3)
    assert yaml.safe_load(text)["account"] == {"base_currency": "EUR", "extra": {"deep": 3}}
    assert "  extra:\n    deep: 3" in text


def test_a_missing_section_is_appended_at_the_end() -> None:
    text = edit_text(PANEL + "\n\n", ["capital", "budgets", "option_premium"], "0.03")
    assert text.endswith('\ncapital:\n  budgets:\n    option_premium: "0.03"\n')
    assert yaml.safe_load(text)["capital"]["budgets"]["option_premium"] == "0.03"


def test_an_empty_panel_takes_its_first_setting() -> None:
    assert edit_text("", ["core", "variant"], "A") == 'core:\n  variant: "A"\n'


def test_an_empty_block_takes_a_child() -> None:
    text = edit_text("core:\n", ["core", "variant"], "A")
    assert yaml.safe_load(text) == {"core": {"variant": "A"}}


def test_a_layout_the_editor_cannot_follow_is_refused() -> None:
    with pytest.raises(OwnerFileError, match="edit"):
        edit_text("thesis: {caps: {position: '0.25'}}\n", ["thesis", "caps", "position"], "0.2")


@pytest.mark.parametrize(
    ("text", "message"),
    [("a: [unclosed\n", "not valid YAML"), ("- a list\n", "does not hold a mapping")],
)
def test_a_panel_that_is_not_a_mapping_is_refused(text: str, message: str) -> None:
    with pytest.raises(OwnerFileError, match=message):
        edit_text(text, ["a"], 1)


def test_a_change_needs_a_key() -> None:
    with pytest.raises(OwnerFileError, match="key"):
        edit_text(PANEL, [], 1)


def test_a_quoted_value_keeps_no_comment_marker_as_comment() -> None:
    text = edit_text('note: "a # b"\n', ["note"], "c")
    assert text == 'note: "c"\n'


@pytest.mark.parametrize(
    ("value", "rendered"),
    [("0.25", '"0.25"'), (3, "3"), (True, "true"), (["a", "b"], "[a, b]"), (1.5, "1.5")],
)
def test_values_render_on_one_line(value: object, rendered: str) -> None:
    assert render_value(value) == rendered


def test_writing_keeps_the_panels_permissions(tmp_path: Path) -> None:
    path = tmp_path / "owner.yaml"
    path.write_text("a: 1\n", encoding="utf-8")
    path.chmod(0o640)
    write_atomically(path, "a: 2\n")
    assert path.read_text(encoding="utf-8") == "a: 2\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_a_new_panel_is_private(tmp_path: Path) -> None:
    path = tmp_path / "owner.yaml"
    write_atomically(path, "a: 1\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_failed_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owner.yaml"
    path.write_text("a: 1\n", encoding="utf-8")

    def broken(self: Path, target: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        write_atomically(path, "a: 2\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["owner.yaml"]


# ── the change ──


def test_a_change_is_validated_and_diffed(config_dir: Path) -> None:
    loaded = load_config(config_dir)
    change = propose(loaded, "thesis.caps.position", "0.20")
    assert change.describe() == "thesis.caps.position: 0.25 -> 0.20"
    assert change.read_hash == file_hash(config_dir / "owner.yaml")
    assert "# ─────" in change.text  # the panel's comments survive


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("nope.key", "1", "there is no setting"),
        ("capital.budgets.core", "2", "would be invalid"),
        ("thesis.caps.position", "0.25", "already 0.25"),
        ("thesis.caps.position", "  ", "needs a value"),
        ("thesis.max_positions", "[unclosed", "not a value"),
    ],
)
def test_a_change_that_cannot_be_made_is_refused(
    config_dir: Path, key: str, value: str, message: str
) -> None:
    with pytest.raises(SettingsRefusal, match=message):
        propose(load_config(config_dir), key, value)


def test_a_panel_the_editor_cannot_change_refuses_the_change(config_dir: Path) -> None:
    (config_dir / "owner.yaml").write_text(
        "account: {base_currency: EUR}\njurisdiction: es\nthesis: {caps: {position: '0.25'}}\n",
        encoding="utf-8",
    )
    with pytest.raises(SettingsRefusal, match="by hand"):
        propose(load_config(config_dir), "thesis.caps.position", "0.2")


def test_a_panel_outside_the_config_directory_is_read_and_changed(
    config_dir: Path, tmp_path: Path
) -> None:
    owner = tmp_path / "machine" / "owner.yaml"
    owner.parent.mkdir()
    owner.write_text((config_dir / "owner.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (config_dir / "owner.yaml").unlink()
    loaded = load_config(config_dir, owner_file=owner)
    assert loaded.owner_file == owner
    change = propose(loaded, "thesis.max_positions", "6")
    assert change.describe() == "thesis.max_positions: 8 -> 6"


def test_a_missing_panel_is_an_empty_one(config_dir: Path, tmp_path: Path) -> None:
    loaded = load_config(config_dir)
    missing = loaded.__class__(
        settings=loaded.settings,
        sources=loaded.sources,
        config_dir=loaded.config_dir,
        files=loaded.files,
        owner_file=tmp_path / "absent.yaml",
    )
    with pytest.raises(SettingsRefusal, match="would be invalid"):
        propose(missing, "thesis.max_positions", "6")


@pytest.mark.parametrize(
    ("raw", "current", "value"),
    [("0.20", "0.25", "0.20"), ('"0.20"', "0.25", "0.20"), ("6", 8, 6), ("[a, b]", [], ["a", "b"])],
)
def test_a_value_takes_the_settings_kind(raw: str, current: object, value: object) -> None:
    assert parse_value(raw, current) == value  # type: ignore[arg-type]


def test_applying_writes_and_audits(config_dir: Path, tmp_path: Path) -> None:
    loaded = load_config(config_dir)
    audit = SettingsAuditLog(tmp_path / "audit.jsonl")
    change = propose(loaded, "thesis.caps.position", "0.20")
    (entry,) = apply(change, loaded, audit, actor="owner", channel=CLI, reason="test", at=AT)
    assert (entry.key, entry.old, entry.new, entry.channel) == (
        "thesis.caps.position",
        "0.25",
        "0.20",
        CLI,
    )
    assert load_config(config_dir).settings.thesis.caps.position == Decimal("0.20")


def test_a_panel_changed_meanwhile_is_not_overwritten(config_dir: Path, tmp_path: Path) -> None:
    loaded = load_config(config_dir)
    change = propose(loaded, "thesis.caps.position", "0.20")
    with (config_dir / "owner.yaml").open("a", encoding="utf-8") as handle:
        handle.write("# edited by hand\n")
    audit = SettingsAuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(SettingsRefusal, match="changed meanwhile"):
        apply(change, loaded, audit, actor="owner", channel=CLI, reason=None, at=AT)
    assert audit.entries() == ()


def test_a_panel_that_cannot_be_written_is_refused(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = load_config(config_dir)
    change = propose(loaded, "thesis.caps.position", "0.20")

    def broken(path: Path, text: str) -> None:
        raise OSError("read-only")

    monkeypatch.setattr("obliskiq.config.changes.write_atomically", broken)
    audit = SettingsAuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(SettingsRefusal, match="could not be written"):
        apply(change, loaded, audit, actor="owner", channel=CLI, reason=None, at=AT)


def test_the_overrides_name_each_default(config_dir: Path) -> None:
    rows = {
        key: (default, value) for key, default, value in owner_overrides(load_config(config_dir))
    }
    assert rows["core.variant"] == ("A", "A")
    assert rows["jurisdiction"] == (None, "es")


def test_a_fingerprint_follows_the_settings(config_dir: Path) -> None:
    before = load_config(config_dir)
    change = propose(before, "thesis.max_positions", "6")
    apply(
        change,
        before,
        SettingsAuditLog(config_dir / "a.jsonl"),
        actor="o",
        channel=CLI,
        reason=None,
        at=AT,
    )
    after = load_config(config_dir)
    assert settings_fingerprint(before.settings) != settings_fingerprint(after.settings)


def test_a_missing_file_hashes_as_empty(tmp_path: Path) -> None:
    (tmp_path / "empty").write_bytes(b"")
    assert file_hash(tmp_path / "absent") == file_hash(tmp_path / "empty")


# ── what remote channels may change ──


def _rules() -> RemoteSettingsConfig:
    return RemoteSettingsConfig.model_validate(
        {
            "tighten": [
                {"key": "thesis.caps.position", "tighter": "lower"},
                {"key": "execution.pretrade.cash_floor", "tighter": "higher"},
            ],
            "bounded": [{"key": "thesis.capital_share", "min": "0", "max": "0.10"}],
        }
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("thesis.caps.position", "0.20"),
        ("execution.pretrade.cash_floor", "50"),
        ("thesis.capital_share", "0.05"),
    ],
)
def test_a_remote_change_may_tighten_or_stay_in_bounds(
    config_dir: Path, key: str, value: str
) -> None:
    check_remote(propose(load_config(config_dir), key, value), _rules())


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("thesis.caps.position", "0.30", "loosens a risk limit"),
        ("execution.pretrade.cash_floor", "10", "loosens a risk limit"),
        ("thesis.capital_share", "0.15", "only between 0 and 0.10"),
        ("thesis.max_positions", "6", "cannot be changed remotely"),
    ],
)
def test_a_remote_change_that_loosens_or_leaves_its_bounds_is_refused(
    config_dir: Path, key: str, value: str, message: str
) -> None:
    with pytest.raises(SettingsRefusal, match=message):
        check_remote(propose(load_config(config_dir), key, value), _rules())


def test_a_remote_rule_on_text_that_is_no_number_is_refused(config_dir: Path) -> None:
    rules = RemoteSettingsConfig.model_validate(
        {"tighten": [{"key": "core.variant", "tighter": "lower"}], "bounded": []}
    )
    with pytest.raises(SettingsRefusal, match="not a number"):
        check_remote(propose(load_config(config_dir), "core.variant", "B"), rules)


def test_the_channels_are_named() -> None:
    assert {CLI, MCP} == {"cli", "mcp"}


# ── the schema of the rules ──


def _write_remote(config_dir: Path, body: str) -> None:
    path = config_dir / "defaults" / "server.yaml"
    text = path.read_text(encoding="utf-8")
    head = text[: text.index("remote_settings:")]
    path.write_text(head + body, encoding="utf-8")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "remote_settings:\n  tighten: [{key: thesis.nope, tighter: lower}]\n  bounded: []\n",
            "is not a setting",
        ),
        (
            "remote_settings:\n  tighten: [{key: core.variant, tighter: lower}]\n  bounded: []\n",
            "is not a number",
        ),
        (
            "remote_settings:\n  tighten: [{key: thesis.caps.position, tighter: lower}]\n"
            "  bounded: [{key: thesis.caps.position, min: '0', max: '1'}]\n",
            "one rule per setting",
        ),
        (
            "remote_settings:\n  tighten: []\n  bounded: [{key: thesis.capital_share, min: '1', "
            "max: '0'}]\n",
            "is above max",
        ),
    ],
)
def test_remote_rules_are_checked(config_dir: Path, body: str, message: str) -> None:
    _write_remote(config_dir, body)
    with pytest.raises(ConfigInvalid, match=message):
        load_config(config_dir)


def test_a_remote_rule_on_a_switch_is_no_number(config_dir: Path) -> None:
    _write_remote(
        config_dir,
        "remote_settings:\n  tighten: [{key: equity_risk.trailing_stop_enabled, tighter: lower}]"
        "\n  bounded: []\n",
    )
    with pytest.raises(ConfigInvalid, match="is not a number"):
        load_config(config_dir)


def test_a_remote_rule_on_a_list_is_no_number(config_dir: Path) -> None:
    _write_remote(
        config_dir,
        "remote_settings:\n  tighten: [{key: market_data.plan, tighter: lower}]\n  bounded: []\n",
    )
    with pytest.raises(ConfigInvalid, match="is not a number"):
        load_config(config_dir)


def test_permissions_stay_private_after_a_change(config_dir: Path, tmp_path: Path) -> None:
    owner = config_dir / "owner.yaml"
    owner.chmod(0o600)
    loaded = load_config(config_dir)
    change = propose(loaded, "thesis.max_positions", "6")
    apply(
        change,
        loaded,
        SettingsAuditLog(tmp_path / "a.jsonl"),
        actor="o",
        channel=CLI,
        reason=None,
        at=AT,
    )
    assert stat.S_IMODE(owner.stat().st_mode) == 0o600


def test_a_change_is_read_with_the_environments_overrides(config_dir: Path) -> None:
    loaded = load_config(
        config_dir, overrides={"logging.format": ("text", "environment (OBLISK_LOG_FORMAT)")}
    )
    change = propose(loaded, "thesis.max_positions", "6")
    assert [c.key for c in change.changes] == ["thesis.max_positions"]
