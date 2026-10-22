"""Every structural key of ``config/universe.yaml`` has a reader in ``src/obliskiq/``.

Ported from lite's ``tests/test_no_unread_universe_keys.py``. ``test_no_unread_config_keys`` scans
the dotted keys of ``config/defaults`` and ``owner.yaml``; the universe is shaped differently and
is read by name: its ``markets``, ``regions`` and ``cells`` keys are data (``us``, ``japan``), its
lists (instruments, futures roots, proxies, unmapped cells) carry field names on every entry. So
this collects the field names, never the identifiers, and asks of each whether the code reads it
as an attribute. The schema refuses an unknown key (lite's truncated flow mapping, whose tail
became a key of its own, cannot load here); this finds the key that loads and nothing reads.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
UNIVERSE = ROOT / "config" / "universe.yaml"
SOURCE_DIR = ROOT / "src" / "obliskiq"

#: Mappings keyed by data: their keys are identifiers, their children's keys structural.
DATA_KEYED = {"markets", "regions", "cells"}


def structural_keys(node: Any, parent: str | None = None) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if parent not in DATA_KEYED:
                found.add(str(key))
            found |= structural_keys(value, str(key))
    elif isinstance(node, list):
        for item in node:
            found |= structural_keys(item, parent)
    return found


def unread(keys: set[str], source: str) -> list[str]:
    return sorted(key for key in keys if not re.search(rf"\.{re.escape(key)}\b", source))


def _source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in SOURCE_DIR.rglob("*.py"))


def test_every_structural_key_of_the_universe_is_read() -> None:
    data = yaml.safe_load(UNIVERSE.read_text(encoding="utf-8"))
    keys = structural_keys(data)
    assert {"instruments", "grid", "macro_board", "attributes", "exposures", "holdings"} <= keys
    assert "japan" not in keys  # a market's name is data
    orphans = unread(keys, _source())
    assert not orphans, f"universe.yaml keys nothing in src/obliskiq/ reads: {orphans}"


def test_the_scan_finds_a_key_nothing_reads() -> None:
    """Positive control: a key no code names is reported, whatever market it sits under."""
    data = {"universe": {"grid": {"markets": {"atlantis": {"a_field_nobody_reads": 1}}}}}
    keys = structural_keys(data)
    assert "atlantis" not in keys
    assert unread(keys, "x.universe\ny.grid\nz.markets") == ["a_field_nobody_reads"]
