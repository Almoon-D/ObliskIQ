"""Loading the configuration: expert defaults, then the owner's panel, then the environment.

``config/defaults/*.yaml`` hold the expert defaults; each top-level section lives in exactly
one of them. ``config/universe.yaml`` holds the universe (its one section, ``universe``).
``config/owner.yaml`` is the owner's panel and overrides any default, section by section and key
by key (a list or a scalar replaces what it overrides; mappings merge). The environment may
override the logging block for one process. The merged tree is validated
into :class:`~obliskiq.config.schema.ObliskConfig`; every value remembers where it came from.

Failure is loud and readable: every problem names the file, the dotted key and what is
wrong, in plain words, and all problems found are reported together.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import copy
import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from ..errors import ConfigError
from .schema import ObliskConfig

DEFAULTS_DIR = "defaults"
UNIVERSE_FILE = "universe.yaml"
UNIVERSE_SECTION = "universe"
OWNER_FILE = "owner.yaml"
YAML_GLOB = "*.yaml"


@dataclass(frozen=True)
class ConfigProblem:
    """One thing wrong with the configuration, located by file and dotted key."""

    source: str
    key: str
    message: str

    def __str__(self) -> str:
        if self.key:
            return f"{self.source}: {self.key}: {self.message}"
        return f"{self.source}: {self.message}"


class ConfigInvalid(ConfigError):
    """The configuration failed to load; ``problems`` lists every reason found."""

    def __init__(self, problems: Sequence[ConfigProblem]) -> None:
        self.problems = tuple(problems)
        count = len(self.problems)
        header = f"configuration is invalid ({count} problem{'' if count == 1 else 's'}):"
        super().__init__("\n".join([header, *(f"  {problem}" for problem in self.problems)]))


@dataclass(frozen=True)
class LoadedConfig:
    """The validated configuration and the source of every value in it."""

    settings: ObliskConfig
    sources: Mapping[str, str]
    config_dir: Path
    files: tuple[str, ...]

    def leaves(self) -> list[tuple[str, Any, str]]:
        """Every setting as ``(dotted key, value, source)``, sorted by key."""
        flat = flatten(self.settings.model_dump(mode="json"))
        return [(key, value, self.sources[key]) for key, value in sorted(flat.items())]


class _StrictLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses a duplicate key, which would otherwise silently win."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in mapping
        except TypeError:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "a key must be text",
                key_node.start_mark,
            ) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def strict_yaml(text: str) -> Any:
    """``text`` parsed as YAML, refusing duplicate keys; YAML errors propagate."""
    return yaml.load(text, Loader=_StrictLoader)


def display_path(path: Path, config_dir: Path) -> str:
    """``path`` relative to the parent of the config directory, e.g. ``config/owner.yaml``."""
    try:
        return str(path.relative_to(config_dir.parent))
    except ValueError:
        return str(path)


def read_yaml_mapping(path: Path, display: str) -> dict[str, Any]:
    """A YAML file whose top level is a mapping with text keys. An empty file is ``{}``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigInvalid([ConfigProblem(display, "", "file not found")]) from None
    except OSError as exc:
        raise ConfigInvalid([ConfigProblem(display, "", f"cannot be read: {exc}")]) from None
    try:
        data = yaml.load(text, Loader=_StrictLoader)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark is not None else ""
        raise ConfigInvalid(
            [ConfigProblem(display, "", f"is not valid YAML: {where}{exc.problem}")]
        ) from None
    except yaml.YAMLError as exc:
        raise ConfigInvalid([ConfigProblem(display, "", f"is not valid YAML: {exc}")]) from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigInvalid(
            [ConfigProblem(display, "", "must hold a mapping of settings at the top level")]
        )
    bad = _non_text_keys(data)
    if bad:
        raise ConfigInvalid(
            [
                ConfigProblem(display, key, "keys must be text; quote yes, no, on, off and numbers")
                for key in bad
            ]
        )
    return data


def _non_text_keys(node: Mapping[Any, Any], prefix: str = "") -> list[str]:
    found: list[str] = []
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if not isinstance(key, str):
            found.append(dotted)
        elif isinstance(value, dict):
            found.extend(_non_text_keys(value, f"{dotted}."))
    return found


def flatten(node: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested mappings to ``{dotted key: leaf}``. An empty mapping is itself a leaf."""
    flat: dict[str, Any] = {}
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            flat.update(flatten(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def _record_sources(value: Any, dotted: str, source: str, sources: dict[str, str]) -> None:
    for old in [key for key in sources if key == dotted or key.startswith(f"{dotted}.")]:
        del sources[old]
    if isinstance(value, Mapping) and value:
        for key in flatten(value, f"{dotted}."):
            sources[key] = source
    else:
        sources[dotted] = source


def merge_into(
    base: dict[str, Any],
    overlay: Mapping[str, Any],
    source: str,
    sources: dict[str, str],
    prefix: str = "",
) -> None:
    """Merge ``overlay`` into ``base`` in place, recording ``source`` for every leaf it sets.

    Mappings merge key by key; anything else (a scalar, a list, a mapping replacing a
    scalar) replaces what was there. An empty section over a mapping — a bare ``logging:``
    header whose keys are all commented out — overrides nothing.
    """
    for key, value in overlay.items():
        dotted = f"{prefix}{key}"
        current = base.get(key)
        if isinstance(current, dict) and (value is None or value == {}):
            # An empty section (a bare `logging:` header) overrides nothing.
            continue
        if isinstance(value, Mapping) and isinstance(current, dict):
            # The mapping is about to hold keys, so it is no longer a leaf itself.
            sources.pop(dotted, None)
            merge_into(current, value, source, sources, f"{dotted}.")
        else:
            base[key] = copy.deepcopy(value)
            _record_sources(value, dotted, source, sources)


def _nested(dotted: str, value: Any) -> dict[str, Any]:
    head, _, rest = dotted.partition(".")
    return {head: _nested(rest, value) if rest else value}


def load_config(
    config_dir: Path, *, overrides: Mapping[str, tuple[Any, str]] | None = None
) -> LoadedConfig:
    """Load and validate the configuration in ``config_dir``.

    ``overrides`` maps a dotted key to ``(value, source)``; the environment uses it for the
    logging block. Raises :class:`ConfigInvalid` with every problem found.
    """
    if not config_dir.is_dir():
        raise ConfigInvalid(
            [ConfigProblem(str(config_dir.absolute()), "", "configuration directory not found")]
        )
    problems: list[ConfigProblem] = []
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    files: list[str] = []

    defaults_dir = config_dir / DEFAULTS_DIR
    default_files = sorted(defaults_dir.glob(YAML_GLOB)) if defaults_dir.is_dir() else []
    if not default_files:
        raise ConfigInvalid(
            [
                ConfigProblem(
                    display_path(defaults_dir, config_dir), "", "holds no expert defaults (*.yaml)"
                )
            ]
        )
    section_home: dict[str, str] = {}
    for path in default_files:
        display = display_path(path, config_dir)
        files.append(display)
        try:
            data = read_yaml_mapping(path, display)
        except ConfigInvalid as exc:
            problems.extend(exc.problems)
            continue
        for section, value in data.items():
            if section in section_home:
                problems.append(
                    ConfigProblem(
                        display,
                        section,
                        f"is also defined in {section_home[section]}; "
                        "each section lives in one defaults file",
                    )
                )
                continue
            section_home[section] = display
            merge_into(merged, {section: value}, display, sources)

    problems += _merge_universe(config_dir, merged, sources, section_home, files)

    owner_display = display_path(config_dir / OWNER_FILE, config_dir)
    files.append(owner_display)
    try:
        merge_into(
            merged,
            read_yaml_mapping(config_dir / OWNER_FILE, owner_display),
            owner_display,
            sources,
        )
    except ConfigInvalid as exc:
        problems.extend(exc.problems)
    if problems:
        raise ConfigInvalid(problems)

    for dotted, (value, source) in (overrides or {}).items():
        merge_into(merged, _nested(dotted, value), source, sources)

    try:
        settings = ObliskConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigInvalid(
            explain_validation_error(exc, ObliskConfig, sources, owner_display)
        ) from None
    return LoadedConfig(
        settings=settings,
        sources=MappingProxyType(dict(sources)),
        config_dir=config_dir,
        files=tuple(files),
    )


def _merge_universe(
    config_dir: Path,
    merged: dict[str, Any],
    sources: dict[str, str],
    section_home: dict[str, str],
    files: list[str],
) -> list[ConfigProblem]:
    """Merge ``config/universe.yaml``, whose one top-level section is ``universe``."""
    path = config_dir / UNIVERSE_FILE
    display = display_path(path, config_dir)
    files.append(display)
    try:
        data = read_yaml_mapping(path, display)
    except ConfigInvalid as exc:
        return list(exc.problems)
    problems = [
        ConfigProblem(
            display, key, f"is not a section of the universe file; it holds only {UNIVERSE_SECTION}"
        )
        for key in sorted(set(data) - {UNIVERSE_SECTION})
    ]
    if UNIVERSE_SECTION in section_home:
        problems.append(
            ConfigProblem(
                section_home[UNIVERSE_SECTION],
                UNIVERSE_SECTION,
                f"is defined in {display}; it cannot also be a default",
            )
        )
    if problems:
        return problems
    merge_into(merged, data, display, sources)
    return []


def explain_validation_error(
    exc: ValidationError,
    model: type[BaseModel],
    sources: Mapping[str, str],
    default_source: str,
) -> list[ConfigProblem]:
    """Turn a pydantic error into owner-readable problems, each located in its file."""
    problems: list[ConfigProblem] = []
    for error in exc.errors(include_url=False):
        loc = [str(part) for part in error["loc"]]
        key = ".".join(loc)
        kind = error["type"]
        if kind == "missing":
            problems.append(
                ConfigProblem(default_source, key, "is required but not set in any file")
            )
            continue
        source = _source_for(key, sources, default_source)
        if kind == "extra_forbidden":
            problems.append(
                ConfigProblem(source, key, "is not a known setting" + _suggest(model, loc))
            )
            continue
        given = error.get("input")
        if given is None and kind in {"model_type", "dict_type"}:
            problems.append(ConfigProblem(source, key, "is empty; set the settings under it"))
            continue
        message = str(error["msg"]).removeprefix("Value error, ")
        message = message.replace("Input should be a valid string", "must be text")
        message = message.replace("Input should be", "must be")
        if isinstance(given, bool) and kind == "string_type":
            message = (
                f"{message} (got {given!r}: YAML reads yes, no, on and off as booleans; quote it)"
            )
        elif isinstance(given, str | int | float | bool):
            message = f"{message} (got {given!r})"
        problems.append(ConfigProblem(source, key, message))
    return problems


def _source_for(key: str, sources: Mapping[str, str], default: str) -> str:
    if key in sources:
        return sources[key]
    for candidate, source in sorted(sources.items()):
        if candidate.startswith(f"{key}."):
            return source
    parts = key.split(".")
    while len(parts) > 1:
        parts.pop()
        ancestor = ".".join(parts)
        if ancestor in sources:
            return sources[ancestor]
    return default


def _suggest(model: type[BaseModel], loc: Sequence[str]) -> str:
    current: type[BaseModel] = model
    for part in loc[:-1]:
        field = current.model_fields.get(part)
        annotation = field.annotation if field is not None else None
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            return ""
        current = annotation
    close = difflib.get_close_matches(loc[-1], list(current.model_fields), n=1)
    return f" (did you mean '{close[0]}'?)" if close else ""
