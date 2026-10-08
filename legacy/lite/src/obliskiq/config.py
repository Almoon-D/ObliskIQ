"""config.yaml loader.

config.yaml owns every threshold, window, floor and cap. This module is the only
way code reads them, and it fails loudly on a missing key rather than supplying a
default — a default in code is a magic number that has learned to hide.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

_MISSING = object()


class Config:
    """Read-only view over config.yaml, addressed by dotted key path."""

    def __init__(self, data: Mapping[str, Any], source: Path | None = None) -> None:
        if not isinstance(data, Mapping):
            raise ConfigError(f"config root must be a mapping, got {type(data).__name__}")
        self._data = dict(data)
        self._source = source

    @property
    def source(self) -> Path | None:
        return self._source

    def get(self, path: str, default: Any = _MISSING) -> Any:
        """Return the value at a dotted path, e.g. ``tenor.entry_dte_min``.

        Raises ConfigError when the path is absent and no default was given. Pass a
        default only where absence is genuinely meaningful, never to paper over a
        key that ought to exist.
        """
        node: Any = self._data
        walked: list[str] = []
        for part in path.split("."):
            walked.append(part)
            if not isinstance(node, Mapping) or part not in node:
                if default is _MISSING:
                    raise ConfigError(
                        f"missing config key '{path}'"
                        + (
                            f" (resolved as far as '{'.'.join(walked[:-1])}')"
                            if len(walked) > 1
                            else ""
                        )
                        + (f" in {self._source}" if self._source else "")
                    )
                return default
            node = node[part]
        return node

    def section(self, path: str) -> Config:
        """Return a sub-tree as its own Config, so callers can bind a namespace once."""
        node = self.get(path)
        if not isinstance(node, Mapping):
            raise ConfigError(f"config key '{path}' is not a section (got {type(node).__name__})")
        return Config(node, self._source)

    def require_float(self, path: str) -> float:
        value = self.get(path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"config key '{path}' must be a number, got {value!r}")
        return float(value)

    def require_int(self, path: str) -> int:
        value = self.get(path)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"config key '{path}' must be an integer, got {value!r}")
        return value

    def require_bool(self, path: str) -> bool:
        value = self.get(path)
        if not isinstance(value, bool):
            raise ConfigError(f"config key '{path}' must be a boolean, got {value!r}")
        return value

    def require_str(self, path: str) -> str:
        value = self.get(path)
        if not isinstance(value, str):
            raise ConfigError(f"config key '{path}' must be a string, got {value!r}")
        return value

    def require_list(self, path: str) -> list[Any]:
        value = self.get(path)
        if not isinstance(value, list):
            raise ConfigError(f"config key '{path}' must be a list, got {type(value).__name__}")
        return list(value)

    def require_choice(self, path: str, allowed: Iterable[Any]) -> Any:
        value = self.get(path)
        allowed = list(allowed)
        if value not in allowed:
            raise ConfigError(f"config key '{path}' must be one of {allowed!r}, got {value!r}")
        return value

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def __contains__(self, path: str) -> bool:
        return self.get(path, None) is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Config(source={self._source!r}, keys={sorted(self._data)!r})"


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config.yaml from ``path``, else ``$OBLISK_CONFIG_PATH``, else the repo root."""
    if path is None:
        env_path = os.environ.get("OBLISK_CONFIG_PATH")
        path = env_path if env_path else default_config_path()
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ConfigError(f"config file not found: {resolved}")
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {resolved} is not valid YAML: {exc}") from exc
    if raw is None:
        raise ConfigError(f"config file {resolved} is empty")
    return Config(raw, resolved)


def default_config_path() -> Path:
    """The config.yaml shipped at the repository root."""
    return Path(__file__).resolve().parents[2] / "config.yaml"
