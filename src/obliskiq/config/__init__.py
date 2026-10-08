"""Configuration: the expert defaults in ``config/defaults/*.yaml`` and the owner's panel in
``config/owner.yaml``, merged and validated into one frozen model, plus the append-only
settings-change audit log. This package owns the shape and loading of values; the values
themselves live in ``config/``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from .loader import ConfigInvalid, ConfigProblem, LoadedConfig, load_config
from .schema import ObliskConfig

__all__ = ["ConfigInvalid", "ConfigProblem", "LoadedConfig", "ObliskConfig", "load_config"]
