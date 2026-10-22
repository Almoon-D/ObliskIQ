"""The base of every configuration model, and the identifier type sections share.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_]*")


class FrozenModel(BaseModel):
    """Base for configuration models: immutable; unknown keys and non-finite numbers are errors."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def check_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            "must be a lower-case identifier (letters, digits, underscores), such as es"
        )
    return value


type Identifier = Annotated[str, AfterValidator(check_identifier)]
