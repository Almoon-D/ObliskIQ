"""Changing one value of the owner's panel while keeping its comments.

``config/owner.yaml`` is written for a person: plain-language comments above each setting. A
YAML library that loads and dumps the file would drop them, so a change edits the text instead:
it finds the key's line by indentation and replaces its value (keeping a trailing comment), or
adds the missing keys at the end of the block that should hold them. The edit is then parsed back
and must equal the file's data with that one value changed; anything else (a flow mapping, an
anchor, a layout the editor does not follow) is refused, and the owner edits the file by hand.

Writing is atomic: a temporary file beside the panel, flushed, then renamed over it, keeping the
panel's permissions.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import copy
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError

#: Spaces per nesting level in keys the editor adds (the panel's own style).
INDENT = 2
_COMMENT = re.compile(r"\s+#.*$")
_KEY_LINE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_]+):(?P<rest>.*)$")


class OwnerFileError(ConfigError):
    """The owner's panel cannot be changed safely by the editor."""


def render_value(value: Any) -> str:
    """``value`` as one line of YAML: a string double-quoted, anything else in flow style."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    text: str = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10**6)
    return text.removesuffix("\n").removesuffix("\n...")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _content(line: str) -> bool:
    """A line that holds YAML (not blank, not only a comment)."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _trailing_comment(rest: str) -> str:
    """The `` # ...`` after a plain scalar, or ``""``; quoted values keep none (a ``#`` inside
    quotes is text)."""
    if '"' in rest or "'" in rest:
        return ""
    found = _COMMENT.search(rest)
    return "" if found is None else found.group(0)


def _block_end(lines: list[str], start: int, parent_indent: int) -> int:
    """The index after the last line of the block that begins at ``start`` and is indented
    deeper than ``parent_indent``."""
    end = start
    for index in range(start, len(lines)):
        if _content(lines[index]) and _indent(lines[index]) <= parent_indent:
            break
        if _content(lines[index]):
            end = index + 1
    return end


def _find_key(lines: list[str], start: int, end: int, key: str) -> int | None:
    """The line in ``[start, end)`` that holds ``key:`` at the block's own indentation."""
    level: int | None = None
    for index in range(start, end):
        if not _content(lines[index]):
            continue
        level = _indent(lines[index]) if level is None else level
        match = _KEY_LINE.match(lines[index])
        if match and len(match["indent"]) == level and match["key"] == key:
            return index
    return None


def edit_text(text: str, path: Sequence[str], value: Any) -> str:
    """``text`` with the setting at ``path`` set to ``value``; refused when the result does not
    parse back to the file's data with exactly that change."""
    if not path:
        raise OwnerFileError("a setting has a key")
    try:
        before = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise OwnerFileError(f"the owner's file is not valid YAML: {exc}") from None
    if not isinstance(before, dict):
        raise OwnerFileError("the owner's file does not hold a mapping")
    lines = text.splitlines()
    start, end, depth, parent_indent = 0, len(lines), 0, -1
    while True:  # each pass descends one key; the last one returns
        found = _find_key(lines, start, end, path[depth])
        if found is None:
            break
        if depth == len(path) - 1:
            match = _KEY_LINE.match(lines[found])
            assert match is not None  # _find_key matched it
            tail = _block_end(lines, found + 1, len(match["indent"]))
            comment = _trailing_comment(match["rest"])
            lines[found : max(tail, found + 1)] = [
                f"{match['indent']}{path[depth]}: {render_value(value)}{comment}"
            ]
            return _verified(lines, before, path, value)
        parent_indent = _indent(lines[found])
        start, end = found + 1, _block_end(lines, found + 1, parent_indent)
        depth += 1
    child_indent = parent_indent + INDENT if depth else 0
    for index in range(start, end):
        if _content(lines[index]):
            child_indent = _indent(lines[index])
            break
    added: list[str] = []
    for offset, key in enumerate(path[depth:]):
        pad = " " * (child_indent + offset * INDENT)
        last = depth + offset == len(path) - 1
        added.append(f"{pad}{key}: {render_value(value)}" if last else f"{pad}{key}:")
    if depth == 0:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", *added] if lines else added
    else:
        lines[end:end] = added
    return _verified(lines, before, path, value)


def _verified(lines: list[str], before: dict[str, Any], path: Sequence[str], value: Any) -> str:
    text = "\n".join(lines) + "\n"
    expected = copy.deepcopy(before)
    node = expected
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value
    try:
        after = yaml.safe_load(text)
    except yaml.YAMLError:
        after = None
    if after != expected:
        raise OwnerFileError(
            "the owner's file has a layout the editor cannot change safely; "
            f"set {'.'.join(path)} by editing the file by hand"
        )
    return text


def write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``: a flushed temporary file renamed over it, keeping its
    permissions (owner read and write when it is new)."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
