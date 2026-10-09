"""The base of every configuration model.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Base for configuration models: immutable; unknown keys and non-finite numbers are errors."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
