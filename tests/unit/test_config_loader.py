"""Loading the configuration: defaults, owner overrides, provenance and loud failure.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from pathlib import Path

import pytest

from obliskiq.config import ConfigInvalid, ConfigProblem, ObliskConfig, load_config
from obliskiq.config.loader import (
    _source_for,
    _suggest,
    display_path,
    flatten,
    merge_into,
    read_yaml_mapping,
)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _problems(config_dir: Path, **kwargs: object) -> list[str]:
    with pytest.raises(ConfigInvalid) as raised:
        load_config(config_dir, **kwargs)  # type: ignore[arg-type]
    return [str(problem) for problem in raised.value.problems]


# ── the shipped configuration ──


def test_the_shipped_configuration_loads(repo_root):
    """Config-load smoke test: the files in the repository validate as shipped."""
    loaded = load_config(repo_root / "config")
    settings = loaded.settings
    assert settings.account.base_currency == "EUR"
    assert settings.jurisdiction == "es"
    assert settings.logging.level == "INFO"
    assert settings.logging.format == "json"
    assert settings.paths.settings_audit_log == "settings-audit.jsonl"
    assert loaded.files == (
        "config/defaults/logging.yaml",
        "config/defaults/paths.yaml",
        "config/owner.yaml",
    )


def test_every_value_knows_its_source(config_dir):
    leaves = {key: source for key, _value, source in load_config(config_dir).leaves()}
    assert leaves == {
        "account.base_currency": "config/owner.yaml",
        "jurisdiction": "config/owner.yaml",
        "logging.format": "config/defaults/logging.yaml",
        "logging.level": "config/defaults/logging.yaml",
        "paths.settings_audit_log": "config/defaults/paths.yaml",
    }


def test_the_settings_are_frozen(config_dir):
    from pydantic import ValidationError

    settings = load_config(config_dir).settings
    with pytest.raises(ValidationError):
        settings.jurisdiction = "ch"  # type: ignore[misc]


# ── overrides ──


def test_the_owner_overrides_a_default(config_dir):
    _write(
        config_dir / "owner.yaml",
        "account: {base_currency: EUR}\njurisdiction: es\nlogging:\n  level: DEBUG\n",
    )
    loaded = load_config(config_dir)
    assert loaded.settings.logging.level == "DEBUG"
    assert loaded.settings.logging.format == "json", "an untouched default survives"
    assert loaded.sources["logging.level"] == "config/owner.yaml"
    assert loaded.sources["logging.format"] == "config/defaults/logging.yaml"


def test_a_section_header_with_everything_commented_out_overrides_nothing(config_dir):
    _write(
        config_dir / "owner.yaml",
        "account: {base_currency: EUR}\njurisdiction: es\nlogging:\n  # level: DEBUG\n",
    )
    loaded = load_config(config_dir)
    assert loaded.settings.logging.level == "INFO"
    assert loaded.sources["logging.level"] == "config/defaults/logging.yaml"


def test_the_environment_overrides_the_owner(config_dir):
    loaded = load_config(
        config_dir, overrides={"logging.format": ("text", "environment (OBLISK_LOG_FORMAT)")}
    )
    assert loaded.settings.logging.format == "text"
    assert loaded.sources["logging.format"] == "environment (OBLISK_LOG_FORMAT)"


def test_a_mapping_replacing_a_scalar_records_new_sources():
    base: dict[str, object] = {"a": 1}
    sources = {"a": "defaults"}
    merge_into(base, {"a": {"b": 2}}, "owner", sources)
    assert base == {"a": {"b": 2}}
    assert sources == {"a.b": "owner"}


def test_a_list_replaces_rather_than_merges():
    base: dict[str, object] = {"codes": [1, 2]}
    sources = {"codes": "defaults"}
    merge_into(base, {"codes": [3]}, "owner", sources)
    assert base == {"codes": [3]}
    assert sources == {"codes": "owner"}


def test_merging_does_not_alias_the_overlay():
    overlay = {"a": {"b": [1]}}
    base: dict[str, object] = {}
    merge_into(base, overlay, "owner", {})
    overlay["a"]["b"].append(2)
    assert base == {"a": {"b": [1]}}


def test_flatten_keeps_empty_mappings_as_leaves():
    assert flatten({"a": {"b": 1, "c": {}}, "d": []}) == {"a.b": 1, "a.c": {}, "d": []}


# ── loud, readable failure ──


def test_an_invalid_value_names_file_key_and_problem(config_dir):
    _write(config_dir / "owner.yaml", "account: {base_currency: EURO}\njurisdiction: es\n")
    assert _problems(config_dir) == [
        "config/owner.yaml: account.base_currency: must be a three-letter ISO 4217 currency "
        "code in capitals, such as EUR (got 'EURO')"
    ]


def test_an_unknown_key_is_refused_with_a_suggestion(config_dir):
    _write(
        config_dir / "owner.yaml",
        "account: {base_currency: EUR, base_curency: USD}\njurisdiction: es\nacount: {}\n",
    )
    problems = _problems(config_dir)
    assert (
        "config/owner.yaml: account.base_curency: is not a known setting "
        "(did you mean 'base_currency'?)"
    ) in problems
    assert any(
        problem.startswith("config/owner.yaml: acount: is not a known") for problem in problems
    )


def test_an_unknown_key_without_a_near_match_has_no_suggestion(config_dir):
    _write(config_dir / "owner.yaml", "account: {base_currency: EUR}\njurisdiction: es\nzzz: 1\n")
    assert _problems(config_dir) == ["config/owner.yaml: zzz: is not a known setting"]


def test_a_section_where_text_is_expected_is_refused(config_dir):
    _write(config_dir / "owner.yaml", "account: {base_currency: EUR}\njurisdiction: {x: 1}\n")
    assert _problems(config_dir) == ["config/owner.yaml: jurisdiction: must be text"]


def test_a_missing_setting_is_reported(config_dir):
    _write(config_dir / "owner.yaml", "jurisdiction: es\n")
    assert _problems(config_dir) == [
        "config/owner.yaml: account: is required but not set in any file"
    ]


def test_an_empty_owner_file_reports_what_is_missing(config_dir):
    _write(config_dir / "owner.yaml", "# nothing yet\n")
    problems = _problems(config_dir)
    assert "config/owner.yaml: account: is required but not set in any file" in problems
    assert "config/owner.yaml: jurisdiction: is required but not set in any file" in problems


def test_a_yaml_boolean_where_text_is_expected_gets_a_hint(config_dir):
    _write(config_dir / "owner.yaml", "account: {base_currency: EUR}\njurisdiction: no\n")
    (problem,) = _problems(config_dir)
    assert "jurisdiction: must be text (got False" in problem
    assert "quote it" in problem


def test_a_bad_value_in_a_defaults_file_is_located_there(config_dir):
    _write(config_dir / "defaults" / "paths.yaml", "paths:\n  settings_audit_log: ../escape\n")
    (problem,) = _problems(config_dir)
    assert problem.startswith("config/defaults/paths.yaml: paths.settings_audit_log: must be")


@pytest.mark.parametrize("value", ["/abs/log.jsonl", "../up.jsonl", "dir/", "a\\\\b.jsonl"])
def test_the_audit_log_must_stay_inside_the_data_dir(config_dir, value):
    _write(config_dir / "defaults" / "paths.yaml", f'paths:\n  settings_audit_log: "{value}"\n')
    assert "relative path inside the data directory" in _problems(config_dir)[0]


def test_duplicate_keys_are_refused(config_dir):
    _write(
        config_dir / "owner.yaml",
        "account:\n  base_currency: EUR\n  base_currency: USD\njurisdiction: es\n",
    )
    (problem,) = _problems(config_dir)
    assert problem.startswith("config/owner.yaml: is not valid YAML: line 3")
    assert "duplicate key 'base_currency'" in problem


def test_a_syntax_error_gives_the_line(config_dir):
    _write(config_dir / "owner.yaml", "account: [\n")
    (problem,) = _problems(config_dir)
    assert problem.startswith("config/owner.yaml: is not valid YAML: line 2")


def test_non_text_keys_are_refused(config_dir):
    _write(
        config_dir / "owner.yaml", "account:\n  yes: 1\n  base_currency: EUR\njurisdiction: es\n"
    )
    assert _problems(config_dir) == [
        "config/owner.yaml: account.True: keys must be text; quote yes, no, on, off and numbers"
    ]


def test_a_non_mapping_file_is_refused(config_dir):
    _write(config_dir / "owner.yaml", "- just\n- a list\n")
    assert _problems(config_dir) == [
        "config/owner.yaml: must hold a mapping of settings at the top level"
    ]


def test_a_missing_owner_file_is_reported(config_dir):
    (config_dir / "owner.yaml").unlink()
    assert _problems(config_dir) == ["config/owner.yaml: file not found"]


def test_missing_defaults_are_reported(config_dir):
    for path in (config_dir / "defaults").glob("*.yaml"):
        path.unlink()
    assert _problems(config_dir) == ["config/defaults: holds no expert defaults (*.yaml)"]


def test_a_section_defined_in_two_defaults_files_is_refused(config_dir):
    _write(config_dir / "defaults" / "zz-extra.yaml", "logging:\n  level: DEBUG\n")
    assert _problems(config_dir) == [
        "config/defaults/zz-extra.yaml: logging: is also defined in "
        "config/defaults/logging.yaml; each section lives in one defaults file"
    ]


def test_all_problems_are_reported_together(config_dir):
    _write(config_dir / "defaults" / "broken.yaml", "a: [\n")
    _write(config_dir / "owner.yaml", "- list\n")
    problems = _problems(config_dir)
    assert len(problems) == 2


def test_the_message_lists_every_problem(config_dir):
    _write(config_dir / "owner.yaml", "account: {base_currency: eur}\njurisdiction: ES\n")
    with pytest.raises(ConfigInvalid) as raised:
        load_config(config_dir)
    text = str(raised.value)
    assert text.startswith("configuration is invalid (2 problems):")
    assert "account.base_currency" in text
    assert "jurisdiction" in text


def test_an_unreadable_file_is_reported(tmp_path):
    directory = tmp_path / "owner.yaml"
    directory.mkdir()
    with pytest.raises(ConfigInvalid, match="cannot be read"):
        read_yaml_mapping(directory, "owner.yaml")


def test_an_unhashable_key_is_refused(tmp_path):
    path = tmp_path / "x.yaml"
    _write(path, "? [a, b]\n: 1\n")
    with pytest.raises(ConfigInvalid, match="a key must be text"):
        read_yaml_mapping(path, "x.yaml")


def test_a_reader_error_without_a_position_is_reported(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_bytes(b"key: \x07\n")
    with pytest.raises(ConfigInvalid, match="is not valid YAML"):
        read_yaml_mapping(path, "x.yaml")


def test_paths_outside_the_config_parent_are_shown_whole(tmp_path):
    outside = Path("/elsewhere/owner.yaml")
    assert display_path(outside, tmp_path / "config") == str(outside)


def test_a_problem_without_a_key_reads_cleanly():
    assert str(ConfigProblem("config/owner.yaml", "", "file not found")) == (
        "config/owner.yaml: file not found"
    )


def test_a_problem_is_located_at_its_nearest_known_ancestor():
    sources = {"codes": "config/owner.yaml"}
    assert _source_for("codes.0", sources, "fallback") == "config/owner.yaml"
    assert _source_for("other.key", sources, "fallback") == "fallback"


def test_no_suggestion_is_offered_below_a_field_that_is_not_a_section():
    assert _suggest(ObliskConfig, ["jurisdiction", "extra"]) == ""
    assert _suggest(ObliskConfig, ["acount"]) == " (did you mean 'account'?)"
