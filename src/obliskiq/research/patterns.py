"""What ``research/pattern_registry.yaml`` records about one hypothesis, for an activation
condition to read (DECISIONS D66).

The registry is the record of every hypothesis's lifecycle and outcome (MASTER_PLAN §5.9); this
reads the fields an activation condition needs and nothing else (Session 15 adds the loader of
the whole registry). A hypothesis with no entry is ``None``: not registered, so not sufficient.
An entry the reader cannot interpret raises: a malformed record is never read as an outcome.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..domain.research import RegisteredHypothesis
from ..errors import ObliskError

#: The registry, relative to the repository root.
REGISTRY_PATH = Path("research") / "pattern_registry.yaml"


class RegistryError(ObliskError):
    """The research registry cannot be read as a record of outcomes."""


def registered_hypothesis(path: Path, hypothesis: str) -> RegisteredHypothesis | None:
    """The registry's record of ``hypothesis``; ``None`` when no entry names it (or no registry
    exists at ``path``, as in an installed wheel)."""
    if not path.is_file():
        return None
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RegistryError(f"{path} cannot be read: {exc}") from None
    patterns = data.get("patterns") if isinstance(data, dict) else None
    if not isinstance(patterns, list):
        raise RegistryError(f"{path} holds no list of patterns")
    found = [
        entry
        for entry in patterns
        if isinstance(entry, dict) and entry.get("hypothesis") == hypothesis
    ]
    if not found:
        return None
    if len(found) > 1:
        raise RegistryError(f"{path} records {hypothesis} {len(found)} times")
    entry = found[0]
    outcome = entry.get("outcome") or {}
    activation = entry.get("activation") or {}
    record = {
        "hypothesis": hypothesis,
        "status": entry.get("status"),
        "decision": outcome.get("decision") if isinstance(outcome, dict) else None,
        "min_median_premiums": (
            activation.get("median_premiums") if isinstance(activation, dict) else None
        ),
        "source": f"{path.name}: {entry.get('id')}",
    }
    try:
        return RegisteredHypothesis.model_validate(record)
    except ValidationError as exc:
        raise RegistryError(f"{path}: the entry for {hypothesis} cannot be read: {exc}") from None
